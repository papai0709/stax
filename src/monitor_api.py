"""
Monitor API for the STAX Dashboard (Story & Test Automation eXtractor)
Provides REST API endpoints for the web dashboard
Former name: ADO Story Extractor
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Dict, Any, List
from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS

from src.agent import StoryExtractionAgent
from src.models import TestCaseExtractionResult, StoryExtractionResult
from src.monitor import EpicChangeMonitor, MonitorConfig
from config.settings import Settings


class MonitorAPI:
    """Flask-based API for monitoring and controlling the story extraction process"""

    def _update_env_file(self, key: str, value: str):
        """Update a value in both .env files (root and config/)"""
        root_dir = os.path.dirname(os.path.dirname(__file__))
        env_paths = [
            os.path.join(root_dir, '.env'),  # Root .env
            os.path.join(root_dir, 'config', '.env')  # Config .env
        ]
        
        for env_path in env_paths:
            if not os.path.exists(env_path):
                continue
                
            # Read current content
            with open(env_path, 'r') as f:
                lines = f.readlines()
            
            # Update or add the key-value pair
            key_found = False
            for i, line in enumerate(lines):
                if line.startswith(f'{key}='):
                    lines[i] = f'{key}={value}\n'
                    key_found = True
                    break
            
            if not key_found:
                lines.append(f'{key}={value}\n')
            
            # Write back to file
            with open(env_path, 'w') as f:
                f.writelines(lines)

    def __init__(self, config: MonitorConfig = None, port: int = 5001):
        # Force reload settings from .env file at startup
        self.monitor = None
        self.is_monitor_running = False
        Settings.reload_config()
        
        self.app = Flask(__name__, template_folder='../templates', static_folder='../static')
        CORS(self.app)
        self.port = port

        self.agent = StoryExtractionAgent()
        Settings.validate()  # Validate settings first
        self.settings = Settings  # Use the class itself, not an instance
        self.logger = logging.getLogger(__name__)

        # Create monitor instance, loading config from file if none provided
        self.monitor = None
        self.monitor_thread = None
        if config is None:
            try:
                with open('config/monitor_config.json', 'r') as f:
                    config_data = json.load(f)
                    # Remove ADO settings that belong to Settings class
                    monitor_settings = {k: v for k, v in config_data.items() 
                                     if not k.startswith('ado_') and k not in ['openai_api_key']}
                    config = MonitorConfig(**monitor_settings)
                    self.logger.info("Loaded monitor configuration from monitor_config.json")
            except Exception as e:
                self.logger.error(f"Failed to load monitor configuration: {str(e)}")
                raise RuntimeError("Monitor configuration is required. Please provide a valid configuration.")

        self.monitor = EpicChangeMonitor(config)

        # Setup routes
        self._setup_routes()

    def _get_epic_processing_status(self, epic_state, story_count: int) -> str:
        """Determine the processing status of an epic based on its state"""
        # Check for errors first
        if epic_state.consecutive_errors > 0:
            return "Error"
        
        # Check if stories have been extracted
        stories_extracted = epic_state.stories_extracted if hasattr(epic_state, 'stories_extracted') else False
        
        if stories_extracted and story_count > 0:
            # Epic has been processed and has stories
            return "Processed"
        elif stories_extracted and story_count == 0:
            # Epic was processed but no stories were created (might be an issue)
            return "Processed (No Stories)"
        elif epic_state.last_snapshot is not None and epic_state.last_check:
            # Epic has been seen before but stories haven't been extracted yet
            return "Changed"
        elif epic_state.last_check is not None:
            # Epic has been checked but not processed yet
            return "New"
        else:
            # Brand new epic that hasn't been processed
            return "New"

    def _setup_routes(self):
        """Setup Flask routes"""

        @self.app.route('/')
        def dashboard():
            """Main dashboard page"""
            return render_template('dashboard.html')

        @self.app.route('/dashboard')
        def dashboard_route():
            """Dashboard page accessible via /dashboard"""
            return render_template('dashboard.html')

        @self.app.route('/api/health')
        def health_check():
            """Health check endpoint"""
            return jsonify({
                'status': 'healthy',
                'timestamp': datetime.now().isoformat(),
                'service': 'STAX API'
            })

        # Config endpoints
        @self.app.route('/api/config', methods=['POST'])
        def update_config():
            """Update the monitor configuration"""
            self.logger.info("[CONFIG-API] 🔄 Configuration update request received")
            if not self.monitor:
                self.logger.error("[CONFIG-API] ❌ Monitor not configured")
                return jsonify({
                    'error': 'Monitor not configured'
                }), 500
            
            try:
                config_data = request.get_json()
                if not config_data:
                    self.logger.error("[CONFIG-API] ❌ No configuration data provided")
                    return jsonify({
                        'error': 'No configuration data provided'
                    }), 400

                self.logger.info(f"[CONFIG-API] 📥 Received configuration data: {config_data}")

                # Get current config from monitor
                current_config = self.monitor.config.__dict__.copy()
                self.logger.info(f"[CONFIG-API] 📋 Current configuration: {current_config}")

                # Track changes for logging
                changes_made = {}

                # Update with new values
                if 'epic_ids' in config_data:
                    # Handle epic_ids specially since they need to be strings
                    old_epic_ids = current_config.get('epic_ids', [])
                    new_epic_ids = [str(epic_id) for epic_id in config_data['epic_ids']]
                    current_config['epic_ids'] = new_epic_ids
                    changes_made['epic_ids'] = {'old': old_epic_ids, 'new': new_epic_ids}
                    self.logger.info(f"[CONFIG-API] 🔄 Epic IDs changed: {old_epic_ids} → {new_epic_ids}")
                    del config_data['epic_ids']  # Remove from config_data to prevent double processing
                
                # Log each configuration change
                for key, new_value in config_data.items():
                    old_value = current_config.get(key)
                    if old_value != new_value:
                        changes_made[key] = {'old': old_value, 'new': new_value}
                        self.logger.info(f"[CONFIG-API] 🔄 {key} changed: {old_value} → {new_value}")
                    else:
                        self.logger.info(f"[CONFIG-API] ➡️ {key} unchanged: {new_value}")

                current_config.update(config_data)
                
                # Create new config object
                self.logger.info("[CONFIG-API] 🔨 Creating new MonitorConfig object")
                new_config = MonitorConfig(**current_config)
                
                # Check if requirement_type changed - need to clear monitored items
                old_requirement_type = getattr(self.monitor.config, 'requirement_type', 'Epic')
                new_requirement_type = new_config.requirement_type
                requirement_type_changed = old_requirement_type != new_requirement_type
                
                # Save the updated config to file
                self.logger.info("[CONFIG-API] 💾 Saving updated config to config/monitor_config.json")
                with open('config/monitor_config.json', 'w') as f:
                    json.dump(current_config, f, indent=4)
                self.logger.info("[CONFIG-API] ✅ Configuration file saved successfully")
                
                # Update monitor with new config
                self.logger.info("[CONFIG-API] 🔄 Updating monitor with new configuration")
                self.monitor.config = new_config
                
                # Clear monitored epics when switching requirement type to force re-discovery
                # Note: processed_epics is now a dict keyed by type, so we don't clear it
                # Each type maintains its own separate tracking
                if requirement_type_changed:
                    self.logger.info(f"[CONFIG-API] 🔄 Requirement type changed: {old_requirement_type} → {new_requirement_type}")
                    self.logger.info(f"[CONFIG-API] 🔄 Clearing monitored items for new type discovery (processed items preserved per type)")
                    self.monitor.monitored_epics.clear()
                    # Save state to persist the current requirement type
                    self.monitor._save_processed_epics()
                
                # If epic_ids were updated, refresh the monitored epics
                if 'epic_ids' in changes_made:
                    self.logger.info("[CONFIG-API] 🎯 Refreshing monitored epics based on updated epic_ids")
                    current_epics = set(self.monitor.monitored_epics.keys())
                    new_epics = set(str(epic_id) for epic_id in changes_made['epic_ids']['new'])
                    
                    # Remove epics that are no longer in the config
                    removed_epics = current_epics - new_epics
                    for epic_id in removed_epics:
                        if epic_id in self.monitor.monitored_epics:
                            self.logger.info(f"[CONFIG-API] ➖ Removing epic {epic_id} from monitoring")
                            del self.monitor.monitored_epics[epic_id]
                    
                    # Add new epics
                    added_epics = new_epics - current_epics
                    for epic_id in added_epics:
                        self.logger.info(f"[CONFIG-API] ➕ Adding epic {epic_id} to monitoring")
                        self.monitor.add_epic(str(epic_id))

                self.logger.info(f"[CONFIG-API] ✅ Configuration update completed successfully. Changes made: {len(changes_made)} items")
                for key, change in changes_made.items():
                    self.logger.info(f"[CONFIG-API] 📋 Final {key}: {change['new']}")
                
                return jsonify({
                    'status': 'success',
                    'message': 'Configuration updated successfully',
                    'config': current_config,
                    'changes_made': changes_made
                })
            except Exception as e:
                self.logger.error(f"[CONFIG-API] ❌ Error updating configuration: {str(e)}")
                return jsonify({
                    'error': f'Failed to update configuration: {str(e)}'
                }), 500

        # Monitor control endpoints
        @self.app.route('/api/monitor/start', methods=['POST'])
        def start_monitor():
            """Start the monitoring service"""
            try:
                if not self.monitor:
                    return jsonify({
                        'success': False,
                        'error': 'Monitor not configured. Please restart the API with monitor configuration.'
                    }), 400

                if self.monitor.is_running:
                    return jsonify({
                        'success': False,
                        'error': 'Monitor is already running'
                    }, 400)

                # Start monitor in a separate thread to avoid blocking the API
                def start_monitor_thread():
                    try:
                        self.monitor.start()
                    except Exception as e:
                        self.logger.error(f"Monitor thread failed: {e}")

                self.monitor_thread = threading.Thread(target=start_monitor_thread, daemon=True)
                self.monitor_thread.start()
                self.is_monitor_running = True

                return jsonify({
                    'success': True,
                    'message': 'Monitor started successfully',
                    'status': 'running'
                })

            except Exception as e:
                self.logger.error(f"Error starting monitor: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to start monitor: {str(e)}'
                }), 500

        @self.app.route('/api/monitor/stop', methods=['POST'])
        def stop_monitor():
            """Stop the monitoring service"""
            try:
                if not self.monitor:
                    return jsonify({
                        'success': False,
                        'error': 'Monitor not configured'
                    }), 400

                # Get current status before stopping
                current_status = self.monitor.get_status()
                was_running = current_status.get('is_running', False)

                # Even if monitor reports not running, try to stop it to ensure cleanup
                try:
                    self.monitor.stop()
                    self.is_monitor_running = False
                except Exception as stop_error:
                    self.logger.error(f"Error during monitor stop: {stop_error}")

                # Stop the monitor thread if it exists
                if self.monitor_thread and self.monitor_thread.is_alive():
                    try:
                        self.monitor_thread.join(timeout=5)  # Wait up to 5 seconds
                    except Exception as thread_error:
                        self.logger.error(f"Error stopping monitor thread: {thread_error}")
                    self.monitor_thread = None

                # Get final status
                final_status = self.monitor.get_status()
                final_status['is_running'] = False  # Ensure this is set

                return jsonify({
                    'success': True,
                    'message': 'Monitor stopped successfully' if was_running else 'Monitor was already stopped',
                    'status': final_status
                })

            except Exception as e:
                self.logger.error(f"Error stopping monitor: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to stop monitor: {str(e)}'
                }), 500

        # Rest of the route handlers follow...

        def shutdown_server():
            """Shutdown function for the Flask server"""
            # First try the development server shutdown function
            success = False
            
            # Try the development server shutdown function
            func = request.environ.get('werkzeug.server.shutdown')
            if func is not None:
                try:
                    self.logger.info("Using Werkzeug shutdown function")
                    func()
                    success = True
                except Exception as e:
                    self.logger.warning(f"Werkzeug shutdown failed: {e}")

            # Try the production server shutdown if previous method failed
            if not success:
                try:
                    from werkzeug.serving import shutdown as werkzeug_shutdown
                    self.logger.info("Using Werkzeug production shutdown")
                    werkzeug_shutdown()
                    success = True
                except Exception as e:
                    self.logger.warning(f"Production server shutdown failed: {e}")

            # Try process termination if previous methods failed
            if not success:
                try:
                    import signal
                    pid = os.getpid()
                    self.logger.info(f"Sending SIGTERM to process {pid}")
                    os.kill(pid, signal.SIGTERM)
                    success = True
                except Exception as e:
                    self.logger.warning(f"SIGTERM failed: {e}")

            # Try sys.exit() as a last resort
            if not success:
                try:
                    import sys
                    self.logger.info("Using sys.exit()")
                    sys.exit(0)
                    success = True
                except Exception as e:
                    self.logger.warning(f"sys.exit() failed: {e}")
            
            return success

        @self.app.route('/api/shutdown', methods=['POST'])
        def shutdown():
            """Shutdown the API server"""
            # Stop the monitor if it's running
            if self.monitor and self.monitor.is_running:
                self.logger.info("Stopping monitor service before shutdown...")
                try:
                    self.monitor.stop()
                except Exception as e:
                    self.logger.warning(f"Error stopping monitor during shutdown: {e}")
            
            # Save any pending state
            if self.monitor:
                try:
                    self.monitor._save_processed_epics()
                except Exception as e:
                    self.logger.warning(f"Error saving state during shutdown: {e}")
            
            # Attempt server shutdown using multiple methods
            self.logger.info("Initiating API server shutdown...")
            
            # 1. Try development server shutdown
            func = request.environ.get('werkzeug.server.shutdown')
            if func is not None:
                try:
                    self.logger.info("Using Werkzeug shutdown function")
                    func()
                    return jsonify({
                        'success': True,
                        'message': 'Server shutdown initiated'
                    })
                except Exception as e:
                    self.logger.warning(f"Development server shutdown failed: {e}")

            # 2. Try production server shutdown
            try:
                from werkzeug.serving import shutdown as werkzeug_shutdown
                self.logger.info("Using Werkzeug production shutdown")
                werkzeug_shutdown()
                return jsonify({
                    'success': True,
                    'message': 'Server shutdown initiated'
                })
            except Exception as e:
                self.logger.warning(f"Production server shutdown failed: {e}")

            # 3. Try process termination
            try:
                import signal
                pid = os.getpid()
                self.logger.info(f"Sending SIGTERM to process {pid}")
                os.kill(pid, signal.SIGTERM)
                return jsonify({
                    'success': True,
                    'message': 'Server shutdown initiated via SIGTERM'
                })
            except Exception as e:
                self.logger.warning(f"SIGTERM failed: {e}")

            # 4. Last resort: sys.exit()
            try:
                import sys
                self.logger.info("Using sys.exit()")
                sys.exit(0)
            except Exception as e:
                self.logger.error(f"All shutdown methods failed: {e}")
                return jsonify({
                    'success': False,
                    'error': 'All shutdown methods failed'
                }), 500

        @self.app.route('/api/monitor/status')
        def get_monitor_status():
            """Get the current status of the monitor"""
            try:
                if not self.monitor:
                    return jsonify({
                        'error': 'Monitor not configured'
                    }), 500
                
                response_data = {
                    'status': 'running' if self.is_monitor_running else 'stopped',
                    'is_running': bool(self.is_monitor_running),  # Ensure boolean
                    'epic_count': len(self.monitor.monitored_epics) if self.monitor.monitored_epics else 0,
                    'last_check': self.monitor.last_check.isoformat() if hasattr(self.monitor, 'last_check') and self.monitor.last_check else None
                }
                self.logger.debug(f"Monitor status: {response_data}")  # Debug log
                return jsonify(response_data)
            except Exception as e:
                self.logger.error(f"Error getting monitor status: {e}")
                return jsonify({
                    'error': f'Failed to get monitor status: {str(e)}'
                }), 500

        @self.app.route('/api/epics/<epic_id>', methods=['DELETE'])
        def remove_epic(epic_id):
            """Remove an EPIC from monitoring"""
            try:
                if not self.monitor:
                    return jsonify({'error': 'Monitor not running'}), 400

                if epic_id not in self.monitor.monitored_epics:
                    return jsonify({'error': f'EPIC {epic_id} not found in monitored EPICs'}), 404

                success = self.monitor.remove_epic(epic_id)
                if success:
                    return jsonify({'message': f'Successfully removed EPIC {epic_id} from monitoring'})
                else:
                    return jsonify({'error': f'Failed to remove EPIC {epic_id}'}), 500

            except Exception as e:
                self.logger.error(f"Error removing EPIC {epic_id}: {str(e)}")
                return jsonify({'error': f'Failed to remove EPIC: {str(e)}'}), 500

        @self.app.route('/api/epics', methods=['GET'])
        def get_epics():
            """Get list of monitored EPICs with details"""
            try:
                if not self.monitor:
                    return jsonify([])

                epics_data = []
                for epic_id, epic_state in self.monitor.monitored_epics.items():
                    try:
                        # Get EPIC details from Azure DevOps
                        epic_info = self.agent.ado_client.get_work_item(int(epic_id))
                        if epic_info:
                            # Count stories for this EPIC
                            story_count = 0
                            try:
                                stories = self.agent.ado_client.get_child_work_items(int(epic_id))
                                story_count = len(stories) if stories else 0
                            except:
                                pass

                            # Determine processing status based on epic state
                            processing_status = self._get_epic_processing_status(epic_state, story_count)
                            
                            epics_data.append({
                                'id': epic_id,
                                'title': epic_info.fields.get('System.Title', f'Epic {epic_id}'),
                                'state': epic_info.fields.get('System.State', 'Unknown'),
                                'processing_status': processing_status,  # New field for processing status
                                'story_count': story_count,
                                'last_changed': epic_state.last_check.isoformat() if epic_state.last_check else None,
                                'consecutive_errors': epic_state.consecutive_errors,
                                'has_snapshot': epic_state.last_snapshot is not None,
                                'stories_extracted': epic_state.stories_extracted if hasattr(epic_state, 'stories_extracted') else False
                            })
                    except Exception as e:
                        self.logger.error(f"Error fetching details for EPIC {epic_id}: {e}")
                        # Still include the EPIC even if we can't get details
                        processing_status = self._get_epic_processing_status(epic_state, 0)
                        epics_data.append({
                            'id': epic_id,
                            'title': f'Epic {epic_id}',
                            'state': 'Unknown',
                            'processing_status': processing_status,  # New field for processing status
                            'story_count': 0,
                            'last_changed': epic_state.last_check.isoformat() if epic_state.last_check else None,
                            'consecutive_errors': epic_state.consecutive_errors,
                            'has_snapshot': epic_state.last_snapshot is not None,
                            'stories_extracted': False
                        })

                return jsonify(epics_data)

            except Exception as e:
                self.logger.error(f"Error getting EPICs: {str(e)}")
                return jsonify([])  # Return empty list instead of error to prevent UI issues
        
        @self.app.route('/api/stats', methods=['GET'])
        def get_stats():
            """Get statistics about monitored EPICs and stories"""
            try:
                if not self.monitor:
                    return jsonify({
                        'total_epics': 0,
                        'changed_epics': 0,
                        'total_stories': 0,
                        'total_test_cases': 0
                    })

                total_epics = len(self.monitor.monitored_epics)
                changed_epics = 0
                total_stories = 0
                total_test_cases = 0

                # Count changed EPICs and stories
                for epic_id, epic_state in self.monitor.monitored_epics.items():
                    try:
                        # Count as changed if it has been processed recently
                        if epic_state.last_check and epic_state.consecutive_errors == 0:
                            # Get stories for this EPIC
                            try:
                                stories = self.agent.ado_client.get_child_work_items(int(epic_id))
                                if stories:
                                    total_stories += len(stories)
                                    # Check if any of these stories have already been extracted
                                    if epic_state.stories_extracted if hasattr(epic_state, 'stories_extracted') else False:
                                        changed_epics += 1
                                    
                                    # Count test cases (child items of stories)
                                    for story in stories:
                                        try:
                                            test_cases = self.agent.ado_client.get_child_work_items(story['id'])
                                            if test_cases:
                                                total_test_cases += len(test_cases)
                                        except:
                                            pass
                            except Exception as e:
                                self.logger.debug(f"Could not get stories for EPIC {epic_id}: {e}")
                    except Exception as e:
                        self.logger.debug(f"Error processing stats for EPIC {epic_id}: {e}")

                return jsonify({
                    'total_epics': total_epics,
                    'changed_epics': changed_epics,
                    'total_stories': total_stories,
                    'total_test_cases': total_test_cases
                })

            except Exception as e:
                self.logger.error(f"Error getting stats: {str(e)}")
                return jsonify({
                    'total_epics': 0,
                    'changed_epics': 0,
                    'total_stories': 0,
                    'total_test_cases': 0
                })

        @self.app.route('/api/config', methods=['GET'])
        def get_config():
            """Get current configuration"""
            try:
                if not self.monitor:
                    return jsonify({'error': 'Monitor not configured'}), 400

                config_dict = {
                    'platform_type': self.settings.PLATFORM_TYPE,
                    'ado_organization': self.settings.ADO_ORGANIZATION,
                    'ado_project': self.settings.ADO_PROJECT,
                    'ado_pat': '***hidden***',  # Don't expose the actual PAT
                    'jira_base_url': getattr(self.settings, 'JIRA_BASE_URL', ''),
                    'jira_username': getattr(self.settings, 'JIRA_USERNAME', ''),
                    'jira_token': '***hidden***',  # Don't expose the actual token
                    'jira_project_key': getattr(self.settings, 'JIRA_PROJECT_KEY', ''),
                    'ai_service_provider': getattr(self.settings, 'AI_SERVICE_PROVIDER', 'OPENAI'),
                    'openai_api_key': '***hidden***',  # Don't expose the actual API key
                    'openai_model': self.settings.OPENAI_MODEL,
                    'azure_openai_endpoint': getattr(self.settings, 'AZURE_OPENAI_ENDPOINT', ''),
                    'azure_openai_api_key': '***hidden***',  # Don't expose the actual API key
                    'azure_openai_deployment_name': getattr(self.settings, 'AZURE_OPENAI_DEPLOYMENT_NAME', ''),
                    'azure_openai_api_version': getattr(self.settings, 'AZURE_OPENAI_API_VERSION', '2024-02-15-preview'),
                    'github_token': '***hidden***',  # Don't expose the actual token
                    'github_model': getattr(self.settings, 'GITHUB_MODEL', 'gpt-4o-mini'),
                    'openai_max_retries': self.settings.OPENAI_MAX_RETRIES,
                    'openai_retry_delay': self.settings.OPENAI_RETRY_DELAY,
                    'requirement_type': self.settings.REQUIREMENT_TYPE,
                    'user_story_type': self.settings.USER_STORY_TYPE,
                    'story_extraction_type': self.settings.STORY_EXTRACTION_TYPE,
                    'test_case_extraction_type': self.settings.TEST_CASE_EXTRACTION_TYPE,
                    'auto_test_case_extraction': self.settings.AUTO_TEST_CASE_EXTRACTION,
                    'check_interval_minutes': self.monitor.config.poll_interval_seconds // 60 if self.monitor.config.poll_interval_seconds else 5,
                    'epic_ids': list(self.monitor.monitored_epics.keys()) if self.monitor.monitored_epics else [],
                    'auto_sync': self.monitor.config.auto_sync if hasattr(self.monitor.config, 'auto_sync') else True,
                    'auto_extract_new_epics': self.monitor.config.auto_extract_new_epics if hasattr(self.monitor.config, 'auto_extract_new_epics') else True,
                    'log_level': getattr(self.monitor.config, 'log_level', 'INFO'),
                    'max_concurrent_syncs': getattr(self.monitor.config, 'max_concurrent_syncs', 3),
                    'retry_attempts': getattr(self.monitor.config, 'retry_attempts', 3),
                    'retry_delay_seconds': getattr(self.monitor.config, 'retry_delay_seconds', 60)
                }

                return jsonify(config_dict)

            except Exception as e:
                self.logger.error(f"Error getting config: {str(e)}")
                return jsonify({'error': f'Failed to get configuration: {str(e)}'}), 500

        @self.app.route('/api/config', methods=['PUT'])
        def update_config_put():
            """Update configuration using PUT method"""
            self.logger.info("[CONFIG-PUT] 🔄 Configuration PUT request received")
            try:
                if not self.monitor:
                    self.logger.error("[CONFIG-PUT] ❌ Monitor not configured")
                    return jsonify({'error': 'Monitor not configured'}), 400

                data = request.get_json()
                if not data:
                    self.logger.error("[CONFIG-PUT] ❌ No configuration data provided")
                    return jsonify({'error': 'No configuration data provided'}), 400

                self.logger.info(f"[CONFIG-PUT] 📥 Received {len(data)} configuration parameters")
                # Log each parameter (hide sensitive values)
                for key, value in data.items():
                    if key in ['ado_pat', 'openai_api_key', 'azure_openai_api_key', 'jira_token', 'github_token']:
                        self.logger.info(f"[CONFIG-PUT] 📋 {key}: ***hidden***")
                    else:
                        self.logger.info(f"[CONFIG-PUT] 📋 {key}: {value}")

                # Update monitor configuration
                config_changes = {}
                if 'check_interval_minutes' in data:
                    old_interval = self.monitor.config.poll_interval_seconds
                    new_interval = data['check_interval_minutes'] * 60
                    self.monitor.config.poll_interval_seconds = new_interval
                    config_changes['poll_interval_seconds'] = {'old': old_interval, 'new': new_interval}
                    self.logger.info(f"[CONFIG-PUT] 🔄 Poll interval changed: {old_interval}s → {new_interval}s")

                if 'auto_sync' in data:
                    old_auto_sync = getattr(self.monitor.config, 'auto_sync', True)
                    new_auto_sync = bool(data['auto_sync'])
                    self.monitor.config.auto_sync = new_auto_sync
                    config_changes['auto_sync'] = {'old': old_auto_sync, 'new': new_auto_sync}
                    self.logger.info(f"[CONFIG-PUT] 🔄 Auto sync changed: {old_auto_sync} → {new_auto_sync}")

                if 'auto_extract_new_epics' in data:
                    old_auto_extract = getattr(self.monitor.config, 'auto_extract_new_epics', True)
                    new_auto_extract = bool(data['auto_extract_new_epics'])
                    self.monitor.config.auto_extract_new_epics = new_auto_extract
                    config_changes['auto_extract_new_epics'] = {'old': old_auto_extract, 'new': new_auto_extract}
                    self.logger.info(f"[CONFIG-PUT] 🔄 Auto extract new epics changed: {old_auto_extract} → {new_auto_extract}")

                # Update requirement_type in monitor config (important for Epic/Feature switching)
                if 'requirement_type' in data:
                    old_requirement_type = getattr(self.monitor.config, 'requirement_type', 'Epic')
                    new_requirement_type = str(data['requirement_type'])
                    self.monitor.config.requirement_type = new_requirement_type
                    config_changes['requirement_type'] = {'old': old_requirement_type, 'new': new_requirement_type}
                    self.logger.info(f"[CONFIG-PUT] 🔄 Requirement type changed: {old_requirement_type} → {new_requirement_type}")
                    # Clear monitored epics when switching requirement type to force re-discovery
                    # Note: processed_epics is now a dict keyed by type, so we don't clear it
                    # Each type maintains its own separate tracking
                    if old_requirement_type != new_requirement_type:
                        self.logger.info(f"[CONFIG-PUT] 🔄 Clearing monitored items for new type discovery (processed items preserved per type)")
                        self.monitor.monitored_epics.clear()
                        # Save state to persist the current requirement type
                        self.monitor._save_processed_epics()

                # Update user_story_type in monitor config
                if 'user_story_type' in data:
                    old_user_story_type = getattr(self.monitor.config, 'user_story_type', 'User Story')
                    new_user_story_type = str(data['user_story_type'])
                    self.monitor.config.user_story_type = new_user_story_type
                    config_changes['user_story_type'] = {'old': old_user_story_type, 'new': new_user_story_type}
                    self.logger.info(f"[CONFIG-PUT] 🔄 User story type changed: {old_user_story_type} → {new_user_story_type}")

                # Update story_extraction_type in monitor config
                if 'story_extraction_type' in data:
                    old_story_extraction_type = getattr(self.monitor.config, 'story_extraction_type', 'User Story')
                    new_story_extraction_type = str(data['story_extraction_type'])
                    self.monitor.config.story_extraction_type = new_story_extraction_type
                    config_changes['story_extraction_type'] = {'old': old_story_extraction_type, 'new': new_story_extraction_type}
                    self.logger.info(f"[CONFIG-PUT] 🔄 Story extraction type changed: {old_story_extraction_type} → {new_story_extraction_type}")

                # Update test_case_extraction_type in monitor config
                if 'test_case_extraction_type' in data:
                    old_test_case_extraction_type = getattr(self.monitor.config, 'test_case_extraction_type', 'Test Case')
                    new_test_case_extraction_type = str(data['test_case_extraction_type'])
                    self.monitor.config.test_case_extraction_type = new_test_case_extraction_type
                    config_changes['test_case_extraction_type'] = {'old': old_test_case_extraction_type, 'new': new_test_case_extraction_type}
                    self.logger.info(f"[CONFIG-PUT] 🔄 Test case extraction type changed: {old_test_case_extraction_type} → {new_test_case_extraction_type}")

                # Update auto_test_case_extraction in monitor config
                if 'auto_test_case_extraction' in data:
                    old_auto_test_case_extraction = getattr(self.monitor.config, 'auto_test_case_extraction', True)
                    new_auto_test_case_extraction = bool(data['auto_test_case_extraction'])
                    self.monitor.config.auto_test_case_extraction = new_auto_test_case_extraction
                    config_changes['auto_test_case_extraction'] = {'old': old_auto_test_case_extraction, 'new': new_auto_test_case_extraction}
                    self.logger.info(f"[CONFIG-PUT] 🔄 Auto test case extraction changed: {old_auto_test_case_extraction} → {new_auto_test_case_extraction}")

                # Handle EPIC IDs
                if 'epic_ids' in data and isinstance(data['epic_ids'], list):
                    self.logger.info("[CONFIG-PUT] 🎯 Processing epic IDs update")
                    # Clear current EPICs and add new ones
                    current_epics = set(self.monitor.monitored_epics.keys())
                    new_epics = set(str(eid) for eid in data['epic_ids'])
                    
                    self.logger.info(f"[CONFIG-PUT] 📊 Current epics: {current_epics}")
                    self.logger.info(f"[CONFIG-PUT] 📊 New epics: {new_epics}")
                    
                    # Remove EPICs not in the new list
                    removed_epics = current_epics - new_epics
                    for epic_id in removed_epics:
                        if epic_id in self.monitor.monitored_epics:
                            self.logger.info(f"[CONFIG-PUT] ➖ Removing epic {epic_id} from monitoring")
                            del self.monitor.monitored_epics[epic_id]
                    
                    # Add new EPICs
                    added_epics = new_epics - current_epics
                    for epic_id in added_epics:
                        self.logger.info(f"[CONFIG-PUT] ➕ Adding epic {epic_id} to monitoring")
                        self.monitor.add_epic(str(epic_id))
                    
                    config_changes['epic_ids'] = {'old': list(current_epics), 'new': list(new_epics)}

                # Save configuration to file
                try:
                    self.logger.info(f"[CONFIG-PUT] 💾 Starting environment file updates for {len(data)} parameters")
                    
                    # Dictionary mapping config keys to their environment variable names
                    config_mapping = {
                        'ado_organization': 'ADO_ORGANIZATION',
                        'ado_project': 'ADO_PROJECT',
                        'ado_pat': 'ADO_PAT',
                        'jira_base_url': 'JIRA_BASE_URL',
                        'jira_username': 'JIRA_USERNAME',
                        'jira_token': 'JIRA_TOKEN',
                        'jira_project_key': 'JIRA_PROJECT_KEY',
                        'ai_service_provider': 'AI_SERVICE_PROVIDER',
                        'openai_api_key': 'OPENAI_API_KEY',
                        'openai_model': 'OPENAI_MODEL',
                        'azure_openai_endpoint': 'AZURE_OPENAI_ENDPOINT',
                        'azure_openai_api_key': 'AZURE_OPENAI_API_KEY',
                        'azure_openai_deployment_name': 'AZURE_OPENAI_DEPLOYMENT_NAME',
                        'azure_openai_api_version': 'AZURE_OPENAI_API_VERSION',
                        'github_token': 'GITHUB_TOKEN',
                        'github_model': 'GITHUB_MODEL',
                        'story_extraction_type': 'ADO_STORY_EXTRACTION_TYPE',
                        'test_case_extraction_type': 'ADO_TEST_CASE_EXTRACTION_TYPE',
                        'auto_test_case_extraction': 'ADO_AUTO_TEST_CASE_EXTRACTION',
                        'openai_max_retries': 'OPENAI_MAX_RETRIES',
                        'openai_retry_delay': 'OPENAI_RETRY_DELAY',
                        'requirement_type': 'ADO_REQUIREMENT_TYPE',
                        'user_story_type': 'ADO_USER_STORY_TYPE'
                    }

                    # Log AI service provider changes
                    if 'ai_service_provider' in data:
                        current_provider = getattr(self.settings, 'AI_SERVICE_PROVIDER', 'OPENAI')
                        new_provider = data['ai_service_provider']
                        if current_provider != new_provider:
                            self.logger.info(f"[CONFIG-PUT] 🔄 AI Service Provider changing: '{current_provider}' → '{new_provider}'")
                        else:
                            self.logger.info(f"[CONFIG-PUT] ✅ AI Service Provider unchanged: '{new_provider}'")

                    # Track environment variable updates
                    env_updates = {}

                    # Process each config setting
                    for config_key, env_var in config_mapping.items():
                        if config_key in data:
                            value = data[config_key]
                            
                            # Get current value for comparison
                            current_value = getattr(self.settings, env_var.replace('ADO_', '').replace('OPENAI_', '').replace('AZURE_OPENAI_', '').replace('GITHUB_', ''), None)
                            
                            # Log configuration updates (with proper masking for sensitive data)
                            if config_key in ['ado_pat', 'openai_api_key', 'azure_openai_api_key', 'jira_token', 'github_token']:
                                if value:
                                    self.logger.info(f"[CONFIG-PUT] 🔒 Updating {config_key} (env: {env_var}) = ***hidden***")
                                else:
                                    self.logger.info(f"[CONFIG-PUT] ⚠️ Skipping empty sensitive value for {config_key}")
                                    continue
                            else:
                                self.logger.info(f"[CONFIG-PUT] 🔄 Updating {config_key} (env: {env_var}): '{current_value}' → '{value}'")
                            
                            # Handle boolean values
                            if config_key == 'auto_test_case_extraction':
                                old_value = value
                                value = str(str(value).lower() == 'true').lower()
                                self.logger.info(f"[CONFIG-PUT] 🔢 Boolean conversion for {config_key}: {old_value} → {value}")
                            # Handle numeric values
                            elif config_key in ['openai_max_retries', 'openai_retry_delay']:
                                old_value = value
                                value = str(value)
                                self.logger.info(f"[CONFIG-PUT] 🔢 String conversion for {config_key}: {old_value} → {value}")
                            # Skip empty sensitive values to prevent accidental clearing
                            elif config_key in ['ado_pat', 'openai_api_key', 'azure_openai_api_key', 'jira_token', 'github_token'] and not value:
                                self.logger.warning(f"[CONFIG-PUT] ⚠️ Skipping empty sensitive value for {config_key}")
                                continue
                            else:
                                value = str(value)

                            # Track the update
                            env_updates[env_var] = {'old': current_value, 'new': value}

                            # Update .env file
                            self.logger.info(f"[CONFIG-PUT] 📝 Updating .env file: {env_var}={value if config_key not in ['ado_pat', 'openai_api_key', 'azure_openai_api_key', 'jira_token'] else '***hidden***'}")
                            self._update_env_file(env_var, value)
                            
                            # Update environment variable
                            os.environ[env_var] = value
                            self.logger.info(f"[CONFIG-PUT] 🌐 Environment variable updated: {env_var}")

                    self.logger.info(f"[CONFIG-PUT] ✅ Completed {len(env_updates)} environment variable updates")

                    # Reload all settings
                    self.logger.info("[CONFIG-PUT] 🔄 Reloading Settings configuration...")
                    Settings.reload_config()
                    self.logger.info("[CONFIG-PUT] ✅ Settings configuration reloaded")
                    
                    # Log the current AI service configuration after reload
                    current_ai_provider = getattr(Settings, 'AI_SERVICE_PROVIDER', 'OPENAI')
                    self.logger.info(f"[CONFIG-PUT] 🤖 Active AI Service Provider: '{current_ai_provider}'")
                    
                    if current_ai_provider == 'AZURE_OPENAI':
                        endpoint = getattr(Settings, 'AZURE_OPENAI_ENDPOINT', 'Not configured')
                        deployment = getattr(Settings, 'AZURE_OPENAI_DEPLOYMENT_NAME', 'Not configured')
                        api_version = getattr(Settings, 'AZURE_OPENAI_API_VERSION', 'Not configured')
                        self.logger.info(f"[CONFIG-PUT] 🔷 Azure OpenAI Endpoint: {endpoint}")
                        self.logger.info(f"[CONFIG-PUT] 🔷 Azure OpenAI Deployment: {deployment}")
                        self.logger.info(f"[CONFIG-PUT] 🔷 Azure OpenAI API Version: {api_version}")
                    elif current_ai_provider == 'GITHUB':
                        github_model = getattr(Settings, 'GITHUB_MODEL', 'Not configured')
                        self.logger.info(f"[CONFIG-PUT] 🔶 GitHub Model: {github_model}")
                    else:
                        openai_model = getattr(Settings, 'OPENAI_MODEL', 'Not configured')
                        self.logger.info(f"[CONFIG-PUT] 🔶 OpenAI Model: {openai_model}")
                    
                    # Log key application settings
                    self.logger.info(f"[CONFIG-PUT] 📋 Story Extraction Type: {Settings.STORY_EXTRACTION_TYPE}")
                    self.logger.info(f"[CONFIG-PUT] 🧪 Test Case Extraction Type: {Settings.TEST_CASE_EXTRACTION_TYPE}")
                    self.logger.info(f"[CONFIG-PUT] ⚙️ Auto Test Case Extraction: {Settings.AUTO_TEST_CASE_EXTRACTION}")
                    
                    config_data = {
                        'ado_organization': Settings.ADO_ORGANIZATION,
                        'ado_project': Settings.ADO_PROJECT,
                        'ado_pat': '***hidden***',  # Don't expose the actual PAT
                        'poll_interval_seconds': self.monitor.config.poll_interval_seconds,
                        'epic_ids': data.get('epic_ids', []),
                        'auto_sync': self.monitor.config.auto_sync,
                        'story_extraction_type': Settings.STORY_EXTRACTION_TYPE,
                        'test_case_extraction_type': Settings.TEST_CASE_EXTRACTION_TYPE,
                        'auto_test_case_extraction': Settings.AUTO_TEST_CASE_EXTRACTION,
                        'requirement_type': Settings.REQUIREMENT_TYPE,
                        'user_story_type': Settings.USER_STORY_TYPE,
                        'openai_model': Settings.OPENAI_MODEL,
                        'openai_max_retries': Settings.OPENAI_MAX_RETRIES,
                        'openai_retry_delay': Settings.OPENAI_RETRY_DELAY,
                        'auto_extract_new_epics': getattr(self.monitor.config, 'auto_extract_new_epics', True),
                        'log_level': getattr(self.monitor.config, 'log_level', 'INFO'),
                        'max_concurrent_syncs': getattr(self.monitor.config, 'max_concurrent_syncs', 3),
                        'retry_attempts': getattr(self.monitor.config, 'retry_attempts', 3),
                        'retry_delay_seconds': getattr(self.monitor.config, 'retry_delay_seconds', 60)
                    }
                    
                    self.logger.info("[CONFIG-PUT] 💾 Saving configuration to config/monitor_config.json")
                    with open('config/monitor_config.json', 'w') as f:
                        json.dump(config_data, f, indent=2)
                    self.logger.info("[CONFIG-PUT] ✅ Configuration file saved successfully")
                    
                    self.logger.info("[CONFIG-PUT] ✅ Configuration update completed successfully")
                except Exception as e:
                    self.logger.error(f"[CONFIG-PUT] ❌ Failed to save configuration: {e}")
                    return jsonify({'error': f'Failed to save configuration: {str(e)}'}), 500

                self.logger.info(f"[CONFIG-PUT] 📊 Configuration update summary:")
                self.logger.info(f"[CONFIG-PUT] 📊 - Monitor config changes: {len(config_changes)}")
                if 'env_updates' in locals():
                    self.logger.info(f"[CONFIG-PUT] 📊 - Environment updates: {len(env_updates)}")
                    for env_var, change in env_updates.items():
                        if env_var in ['ADO_PAT', 'OPENAI_API_KEY', 'AZURE_OPENAI_API_KEY', 'JIRA_TOKEN', 'GITHUB_TOKEN']:
                            self.logger.info(f"[CONFIG-PUT] 📊   {env_var}: ***hidden***")
                        else:
                            self.logger.info(f"[CONFIG-PUT] 📊   {env_var}: {change['old']} → {change['new']}")

                # Check if running in Docker and trigger restart
                import signal
                import time
                
                is_docker = os.path.exists('/.dockerenv') or os.environ.get('RUNNING_IN_DOCKER', 'false').lower() == 'true'
                
                if is_docker:
                    self.logger.info("[CONFIG-PUT] 🐳 Detected running in Docker container")
                    self.logger.info("[CONFIG-PUT] 🔄 Triggering container restart to apply configuration changes...")
                    
                    try:
                        # Create a restart marker file that can be monitored by docker-compose
                        restart_marker = '/tmp/restart_required'
                        with open(restart_marker, 'w') as f:
                            f.write('restart')
                        
                        # Schedule graceful restart - give Flask time to send response
                        def delayed_restart():
                            time.sleep(2)
                            os.kill(os.getpid(), signal.SIGTERM)
                        
                        restart_thread = threading.Thread(target=delayed_restart)
                        restart_thread.daemon = True
                        restart_thread.start()
                        
                        self.logger.info("[CONFIG-PUT] ✅ Restart scheduled - container will restart in 2 seconds")
                        
                        return jsonify({
                            'success': True,
                            'message': 'Configuration updated successfully. Docker container will restart to apply changes.',
                            'docker_restart': True,
                            'changes_made': {**config_changes, **({f"env_{k}": v for k, v in env_updates.items()} if 'env_updates' in locals() else {})}
                        })
                    except Exception as restart_error:
                        self.logger.warning(f"[CONFIG-PUT] ⚠️ Failed to trigger Docker restart: {restart_error}")
                        return jsonify({
                            'success': True,
                            'message': 'Configuration updated successfully (restart failed - please restart manually)',
                            'changes_made': {**config_changes, **({f"env_{k}": v for k, v in env_updates.items()} if 'env_updates' in locals() else {})}
                        })
                else:
                    self.logger.info("[CONFIG-PUT] 💻 Running locally (not in Docker) - no restart needed")
                    
                    return jsonify({
                        'success': True,
                        'message': 'Configuration updated successfully',
                        'changes_made': {**config_changes, **({f"env_{k}": v for k, v in env_updates.items()} if 'env_updates' in locals() else {})}
                    })

            except Exception as e:
                self.logger.error(f"Error updating config: {str(e)}")
                return jsonify({'error': f'Failed to update configuration: {str(e)}'}), 500

        @self.app.route('/api/platform/switch', methods=['POST'])
        def switch_platform():
            """Switch between ADO and JIRA platforms"""
            try:
                data = request.get_json()
                if not data or 'platform_type' not in data:
                    return jsonify({'error': 'Platform type is required'}), 400

                platform_type = data['platform_type'].upper()
                if platform_type not in ['ADO', 'JIRA']:
                    return jsonify({'error': 'Platform type must be ADO or JIRA'}), 400

                # Update .env file
                self._update_env_file('PLATFORM_TYPE', platform_type)
                
                # Reload settings
                Settings.reload_config()
                
                self.logger.info(f"Platform switched to: {platform_type}")
                
                return jsonify({
                    'success': True,
                    'message': f'Platform switched to {platform_type}',
                    'platform_type': platform_type,
                    'requirement_type': Settings.REQUIREMENT_TYPE,
                    'user_story_type': Settings.USER_STORY_TYPE,
                    'story_extraction_type': Settings.STORY_EXTRACTION_TYPE,
                    'test_case_extraction_type': Settings.TEST_CASE_EXTRACTION_TYPE
                })

            except Exception as e:
                self.logger.error(f"Error switching platform: {str(e)}")
                return jsonify({'error': f'Failed to switch platform: {str(e)}'}), 500

        @self.app.route('/api/platform/test-connection', methods=['POST'])
        def test_platform_connection():
            """Test connection to the selected platform"""
            try:
                if Settings.PLATFORM_TYPE == 'JIRA':
                    from src.jira_client import JiraClient
                    jira_client = JiraClient()
                    success = jira_client.test_connection()
                    
                    if success:
                        project_info = jira_client.get_project_info()
                        return jsonify({
                            'success': True,
                            'platform': 'JIRA',
                            'message': 'JIRA connection successful',
                            'project_info': project_info
                        })
                    else:
                        return jsonify({
                            'success': False,
                            'platform': 'JIRA',
                            'error': 'JIRA connection failed'
                        }), 400
                else:
                    # Test ADO connection
                    from src.ado_client import ADOClient
                    ado_client = ADOClient()
                    
                    # Try to get work item types as a connection test
                    try:
                        # This will raise an exception if connection fails
                        work_item_types = ado_client.get_work_item_types()
                        return jsonify({
                            'success': True,
                            'platform': 'ADO',
                            'message': 'ADO connection successful',
                            'project_info': {
                                'name': Settings.ADO_PROJECT,
                                'organization': Settings.ADO_ORGANIZATION,
                                'work_item_types': work_item_types[:5]  # Return first 5 types
                            }
                        })
                    except Exception as e:
                        return jsonify({
                            'success': False,
                            'platform': 'ADO',
                            'error': f'ADO connection failed: {str(e)}'
                        }), 400

            except Exception as e:
                self.logger.error(f"Error testing platform connection: {str(e)}")
                return jsonify({'error': f'Failed to test connection: {str(e)}'}), 500

        @self.app.route('/api/monitor/check', methods=['POST'])
        def force_check():
            """Force a manual check for changes"""
            try:
                if not self.monitor:
                    return jsonify({'error': 'Monitor not configured'}), 400

                # Perform a manual check
                results = self.monitor.force_check()
                changes_detected = sum(1 for r in results.values() if r.get('has_changes', False))
                
                return jsonify({
                    'success': True,
                    'message': f'Manual check completed',
                    'changes_detected': changes_detected,
                    'results': results
                })

            except Exception as e:
                self.logger.error(f"Error in force check: {str(e)}")
                return jsonify({'error': f'Force check failed: {str(e)}'}), 500

        # =====================================
        # Feature Hierarchy API Endpoints
        # =====================================

        @self.app.route('/api/epics/<epic_id>/hierarchy', methods=['GET'])
        def get_epic_hierarchy(epic_id):
            """Get the full Epic → Feature → Story hierarchy for an Epic"""
            try:
                if not self.monitor:
                    return jsonify({'error': 'Monitor not configured'}), 400

                self.logger.info(f"Getting hierarchy for Epic {epic_id}")
                hierarchy = self.monitor.get_epic_with_features(epic_id)
                
                if not hierarchy:
                    return jsonify({'error': f'Could not get hierarchy for Epic {epic_id}'}), 404
                
                return jsonify({
                    'success': True,
                    'epic': hierarchy
                })

            except Exception as e:
                self.logger.error(f"Error getting hierarchy for Epic {epic_id}: {str(e)}")
                return jsonify({'error': f'Failed to get hierarchy: {str(e)}'}), 500

        @self.app.route('/api/epics/<epic_id>/features', methods=['GET'])
        def get_features_from_epic(epic_id):
            """Get all Features from an Epic"""
            try:
                if not self.monitor:
                    return jsonify({'error': 'Monitor not configured'}), 400

                self.logger.info(f"Getting features for Epic {epic_id}")
                features = self.agent.ado_client.get_features_from_epic(int(epic_id))
                
                return jsonify({
                    'success': True,
                    'epic_id': epic_id,
                    'features': features,
                    'feature_count': len(features)
                })

            except Exception as e:
                self.logger.error(f"Error getting features for Epic {epic_id}: {str(e)}")
                return jsonify({'error': f'Failed to get features: {str(e)}'}), 500

        @self.app.route('/api/features/<feature_id>/stories', methods=['GET'])
        def get_stories_from_feature(feature_id):
            """Get all Stories from a Feature"""
            try:
                if not self.monitor:
                    return jsonify({'error': 'Monitor not configured'}), 400

                self.logger.info(f"Getting stories for Feature {feature_id}")
                stories = self.agent.ado_client.get_stories_from_feature(int(feature_id))
                
                # Get feature details
                feature_details = self.agent.ado_client.get_feature_details(int(feature_id))
                
                return jsonify({
                    'success': True,
                    'feature_id': feature_id,
                    'feature': feature_details,
                    'stories': stories,
                    'story_count': len(stories)
                })

            except Exception as e:
                self.logger.error(f"Error getting stories for Feature {feature_id}: {str(e)}")
                return jsonify({'error': f'Failed to get stories: {str(e)}'}), 500

        @self.app.route('/api/epics/<epic_id>/sync-hierarchy', methods=['POST'])
        def sync_epic_hierarchy(epic_id):
            """Sync an Epic with its full Feature → Story hierarchy"""
            try:
                if not self.monitor:
                    return jsonify({'error': 'Monitor not configured'}), 400

                self.logger.info(f"Starting hierarchy sync for Epic {epic_id}")
                result = self.monitor.sync_epic_hierarchy(epic_id)
                
                return jsonify({
                    'success': result.get('success', False),
                    'result': result
                })

            except Exception as e:
                self.logger.error(f"Error syncing hierarchy for Epic {epic_id}: {str(e)}")
                return jsonify({'error': f'Failed to sync hierarchy: {str(e)}'}), 500

        @self.app.route('/api/hierarchy/status', methods=['GET'])
        def get_hierarchy_status():
            """Get the current status of all monitored Epics with their feature hierarchy"""
            try:
                if not self.monitor:
                    return jsonify({'error': 'Monitor not configured'}), 400

                status = self.monitor.get_hierarchy_status()
                
                return jsonify({
                    'success': True,
                    'status': status
                })

            except Exception as e:
                self.logger.error(f"Error getting hierarchy status: {str(e)}")
                return jsonify({'error': f'Failed to get hierarchy status: {str(e)}'}), 500

        @self.app.route('/api/features', methods=['GET'])
        def get_all_features():
            """Get all Features in the project"""
            try:
                if not self.monitor:
                    return jsonify({'error': 'Monitor not configured'}), 400

                self.logger.info("Getting all Features in project")
                features = self.agent.ado_client.get_all_features_in_project()
                
                return jsonify({
                    'success': True,
                    'features': features,
                    'feature_count': len(features)
                })

            except Exception as e:
                self.logger.error(f"Error getting all features: {str(e)}")
                return jsonify({'error': f'Failed to get features: {str(e)}'}), 500

        @self.app.route('/api/stories/<story_id>/test-cases', methods=['POST'])
        def extract_test_cases_for_story(story_id):
            """Extract test cases for a specific story"""
            try:
                # Extract test cases using the agent
                result = self.agent.extract_test_cases_for_story(story_id)
                
                return jsonify({
                    'success': result.extraction_successful,
                    'story_id': result.story_id,
                    'story_title': result.story_title,
                    'test_cases': [tc.dict() for tc in result.test_cases],
                    'total_test_cases': len(result.test_cases),
                    'error': result.error_message if not result.extraction_successful else None
                })

            except Exception as e:
                self.logger.error(f"Error extracting test cases for story {story_id}: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to extract test cases: {str(e)}'
                }), 500

        @self.app.route('/api/stories/<story_id>/test-cases/upload', methods=['POST'])
        def upload_test_cases_for_story(story_id):
            """Upload test cases for a specific story to Azure DevOps"""
            try:
                data = request.get_json()
                test_cases = data.get('test_cases', [])
                work_item_type = data.get('work_item_type', 'Issue')
                
                if not test_cases:
                    return jsonify({
                        'success': False,
                        'error': 'No test cases provided for upload'
                    }), 400

                # Upload test cases to ADO
                uploaded_test_cases = []
                successful_uploads = 0
                
                for i, test_case in enumerate(test_cases):
                    try:
                        # Create the test case in ADO as a child of the story
                        work_item_data = {
                            'System.Title': test_case.get('title', f'Test Case {i+1}'),
                            'System.Description': test_case.get('description', ''),
                            'System.WorkItemType': work_item_type,
                        }
                        
                        # Add test steps and expected result as additional fields for Test Case work items
                        if work_item_type == 'Test Case':
                            # Pass test_steps as additional field for proper formatting
                            if test_case.get('steps') or test_case.get('test_steps'):
                                work_item_data['test_steps'] = test_case.get('test_steps') or test_case.get('steps')
                            
                            if test_case.get('expected_result'):
                                work_item_data['expected_result'] = test_case.get('expected_result')
                        else:
                            # For other work item types (like Issue), add to description
                            if test_case.get('steps') or test_case.get('test_steps'):
                                steps = test_case.get('test_steps') or test_case.get('steps')
                                steps_html = '<ol>' + ''.join(f'<li>{step}</li>' for step in steps) + '</ol>'
                                work_item_data['System.Description'] += f'<br/><strong>Test Steps:</strong><br/>{steps_html}'
                            
                            if test_case.get('expected_result'):
                                work_item_data['System.Description'] += f'<br/><strong>Expected Result:</strong><br/>{test_case["expected_result"]}'
                        
                        # Create the work item
                        created_item = self.agent.ado_client.create_work_item(
                            work_item_type=work_item_type,
                            fields=work_item_data,
                            parent_id=int(story_id)
                        )
                        
                        if created_item and 'id' in created_item:
                            uploaded_test_cases.append({
                                'success': True,
                                'id': created_item['id'],
                                'title': test_case.get('title', f'Test Case {i+1}')
                            })
                            successful_uploads += 1
                        else:
                            uploaded_test_cases.append({
                                'success': False,
                                'error': 'Failed to create work item',
                                'title': test_case.get('title', f'Test Case {i+1}')
                            })
                    except Exception as e:
                        self.logger.error(f"Exception during test case upload for story {story_id}: {str(e)}")
                        uploaded_test_cases.append({
                            'success': False,
                            'error': str(e),
                            'title': test_case.get('title', f'Test Case {i+1}')
                        })
                
                return jsonify({
                    'success': successful_uploads > 0,
                    'story_id': story_id,
                    'uploaded_test_cases': uploaded_test_cases,
                    'successful_uploads': successful_uploads,
                    'total_test_cases': len(test_cases)
                })

            except Exception as e:
                self.logger.error(f"Error uploading test cases for story {story_id}: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to upload test cases: {str(e)}'
                }), 500

        @self.app.route('/api/logs', methods=['GET'])
        def get_logs():
            """Get recent log entries"""
            try:
                lines = int(request.args.get('lines', 50))
                lines = min(lines, 1000)  # Cap at 1000 lines
                
                log_file = 'logs/epic_monitor.log'
                if not os.path.exists(log_file):
                    return jsonify([])
                
                # Read the last N lines from the log file
                logs = []
                try:
                    with open(log_file, 'r') as f:
                        all_lines = f.readlines()
                        recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                        
                        for line in recent_lines:
                            line = line.strip()
                            if line:
                                # Parse log line format: "2025-08-09 07:12:03,405 - MonitorAPI - INFO - Message"
                                try:
                                    parts = line.split(' - ', 3)
                                    if len(parts) >= 4:
                                        timestamp_str = parts[0]
                                        component = parts[1]
                                        level = parts[2].lower()
                                        message = parts[3]
                                        
                                        # Convert timestamp to ISO format
                                        try:
                                            from datetime import datetime
                                            timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S,%f')
                                            iso_timestamp = timestamp.isoformat()
                                        except:
                                            iso_timestamp = timestamp_str
                                        
                                        logs.append({
                                            'timestamp': iso_timestamp,
                                            'level': level,
                                            'component': component,
                                            'message': message
                                        })
                                    else:
                                        # Fallback for malformed lines
                                        logs.append({
                                            'timestamp': datetime.now().isoformat(),
                                            'level': 'info',
                                            'component': 'System',
                                            'message': line
                                        })
                                except Exception as e:
                                    # If parsing fails, add as a raw message
                                    logs.append({
                                        'timestamp': datetime.now().isoformat(),
                                        'level': 'info',
                                        'component': 'System',
                                        'message': line
                                    })
                except Exception as e:
                    self.logger.error(f"Error reading log file: {e}")
                    return jsonify([])
                
                return jsonify(logs)
                
            except Exception as e:
                self.logger.error(f"Error getting logs: {str(e)}")
                return jsonify([])

        @self.app.route('/api/logs/clear', methods=['POST'])
        def clear_logs_display():
            """Clear logs from UI display only (preserves actual log files)"""
            try:
                # This endpoint is for UI-only log clearing
                # We don't actually delete the log files, just return success
                # The frontend will clear its display
                
                return jsonify({
                    'success': True,
                    'message': 'Log display cleared (files preserved)'
                })
                
            except Exception as e:
                self.logger.error(f"Error clearing log display: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to clear log display: {str(e)}'
                }), 500
        
        @self.app.route('/api/test-cases/extract', methods=['POST'])
        def extract_test_cases():
            """Extract test cases for a story"""
            try:
                data = request.get_json()
                story_id = data.get('story_id', '').strip()
                upload_to_ado = data.get('upload_to_ado', True)

                if not story_id:
                    return jsonify({
                        'success': False,
                        'error': 'Story ID is required'
                    }), 400

                # Extract test cases using the agent
                result = self.agent.extract_test_cases_for_story(story_id)

                # If upload is requested and extraction was successful, upload to ADO
                if upload_to_ado and result.extraction_successful and result.test_cases:
                    try:
                        # Upload test cases as Issues (this functionality exists in the agent)
                        upload_result = self.agent.extract_test_cases_as_issues(story_id, upload_to_ado=True)
                        if upload_result.extraction_successful:
                            result = upload_result  # Use the upload result instead
                    except Exception as upload_error:
                        self.logger.error(f"Failed to upload test cases: {upload_error}")
                        # Continue with the extraction result even if upload fails

                return jsonify({
                    'success': result.extraction_successful,
                    'story_id': result.story_id,
                    'story_title': result.story_title,
                    'test_cases': [tc.dict() for tc in result.test_cases],
                    'total_test_cases': len(result.test_cases),
                    'error': result.error_message if not result.extraction_successful else None,
                    'uploaded_to_ado': upload_to_ado and result.extraction_successful
                })

            except Exception as e:
                self.logger.error(f"Error in extract_test_cases endpoint: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Internal server error: {str(e)}'
                }), 500

        @self.app.route('/api/test-cases/preview', methods=['POST'])
        def preview_test_cases():
            """Preview test cases for a story without uploading to ADO"""
            try:
                data = request.get_json()
                story_id = data.get('story_id', '').strip()

                if not story_id:
                    return jsonify({
                        'success': False,
                        'error': 'Story ID is required'
                    }), 400

                # Extract test cases without uploading
                result = self.agent.extract_test_cases_for_story(story_id)

                return jsonify({
                    'success': result.extraction_successful,
                    'story_id': result.story_id,
                    'story_title': result.story_title,
                    'test_cases': [tc.dict() for tc in result.test_cases],
                    'total_test_cases': len(result.test_cases),
                    'error': result.error_message if not result.extraction_successful else None,
                    'preview_mode': True
                })

            except Exception as e:
                self.logger.error(f"Error in preview_test_cases endpoint: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Internal server error: {str(e)}'
                }), 500

        @self.app.route('/api/test-cases/bulk-extract', methods=['POST'])
        def bulk_extract_test_cases():
            """Extract test cases for multiple stories in an epic"""
            try:
                data = request.get_json()
                epic_id = data.get('epic_id', '').strip()
                upload_to_ado = data.get('upload_to_ado', True)

                if not epic_id:
                    return jsonify({
                        'success': False,
                        'error': 'Epic ID is required'
                    }), 400

                # Extract test cases for all stories in the epic
                results = self.agent.extract_test_cases_for_epic_stories(epic_id, upload_to_ado)

                # Process results
                successful_extractions = 0
                total_test_cases = 0
                story_results = []

                for story_id, result in results.items():
                    if result.extraction_successful:
                        successful_extractions += 1
                        total_test_cases += len(result.test_cases)

                    story_results.append({
                        'story_id': result.story_id,
                        'story_title': result.story_title,
                        'success': result.extraction_successful,
                        'test_case_count': len(result.test_cases),
                        'error': result.error_message if not result.extraction_successful else None
                    })

                return jsonify({
                    'success': successful_extractions > 0,
                    'epic_id': epic_id,
                    'total_stories': len(results),
                    'successful_extractions': successful_extractions,
                    'total_test_cases': total_test_cases,
                    'story_results': story_results,
                    'uploaded_to_ado': upload_to_ado
                })

            except Exception as e:
                self.logger.error(f"Error in bulk_extract_test_cases endpoint: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Internal server error: {str(e)}'
                }), 500

        @self.app.route('/api/stories/extract', methods=['POST'])
        def extract_stories():
            """Extract user stories from requirements"""
            try:
                data = request.get_json()
                requirement_id = data.get('requirement_id', '').strip()
                upload_to_ado = data.get('upload_to_ado', True)

                if not requirement_id:
                    return jsonify({
                        'success': False,
                        'error': 'Requirement ID is required'
                    }), 400

                # Extract stories using the agent
                result = self.agent.process_requirement_by_id(requirement_id, upload_to_ado)

                return jsonify({
                    'success': result.extraction_successful,
                    'requirement_id': result.requirement_id,
                    'requirement_title': result.requirement_title,
                    'stories': [story.dict() for story in result.stories],
                    'total_stories': len(result.stories),
                    'error': result.error_message if not result.extraction_successful else None,
                    'uploaded_to_ado': upload_to_ado and result.extraction_successful
                })

            except Exception as e:
                self.logger.error(f"Error in extract_stories endpoint: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Internal server error: {str(e)}'
                }), 500

        @self.app.route('/api/stories/preview', methods=['POST'])
        def preview_stories():
            """Preview user stories from requirements without uploading"""
            try:
                data = request.get_json()
                requirement_id = data.get('requirement_id', '').strip()

                if not requirement_id:
                    return jsonify({
                        'success': False,
                        'error': 'Requirement ID is required'
                    }), 400

                # Preview stories without uploading
                result = self.agent.preview_stories(requirement_id)

                return jsonify({
                    'success': result.extraction_successful,
                    'requirement_id': result.requirement_id,
                    'requirement_title': result.requirement_title,
                    'stories': [story.dict() for story in result.stories],
                    'total_stories': len(result.stories),
                    'error': result.error_message if not result.extraction_successful else None,
                    'preview_mode': True
                })

            except Exception as e:
                self.logger.error(f"Error in preview_stories endpoint: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Internal server error: {str(e)}'
                }), 500

    def run(self, host='0.0.0.0', debug=False):
        """Run the Flask application"""
        self.logger.info(f"Starting Monitor API server on {host}:{self.port}")
        self.app.run(host=host, port=self.port, debug=debug)


def create_app(port=5001):
    """Create and configure the Flask application"""
    api = MonitorAPI(port=port)
    return api.app


if __name__ == '__main__':
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Create and run the API
    api = MonitorAPI(port=5001)
    api.run(debug=True)

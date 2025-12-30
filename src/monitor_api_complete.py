"""
Monitor API for the ADO Story Extractor Dashboard
Provides REST API endpoints for the web dashboard
"""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Any, List
from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
from flasgger import Swagger

from src.agent import StoryExtractionAgent
from src.models import TestCaseExtractionResult, StoryExtractionResult
from src.monitor import EpicChangeMonitor, MonitorConfig
from src.env_utils import EnvFileManager, get_masked_value, is_env_file_writable
from config.settings import Settings


class MonitorAPI:
    """Flask-based API for monitoring and controlling the story extraction process"""

    def __init__(self, config: MonitorConfig = None, port: int = 5001):
        self.app = Flask(__name__, template_folder='../templates', static_folder='../static')
        CORS(self.app)
        self.port = port

        self.agent = StoryExtractionAgent()
        self.settings = Settings()
        self.logger = logging.getLogger(__name__)
        
        # Initialize environment file manager
        self.env_manager = EnvFileManager('config/.env')

        # Create monitor instance, loading config from file if none provided
        self.monitor = None
        self.monitor_thread = None
        if config is None:
            try:
                with open('monitor_config_enhanced.json', 'r') as f:
                    config_data = json.load(f)
                    config = MonitorConfig(**config_data)
                    self.logger.info("Loaded monitor configuration from monitor_config_enhanced.json")
            except Exception as e:
                self.logger.error(f"Failed to load monitor configuration: {str(e)}")
                raise RuntimeError("Monitor configuration is required. Please provide a valid configuration.")

        self.monitor = EpicChangeMonitor(config)

        # Configure Swagger
        swagger_config = {
            "headers": [],
            "specs": [
                {
                    "endpoint": 'apispec',
                    "route": '/apispec.json',
                    "rule_filter": lambda rule: True,
                    "model_filter": lambda tag: True,
                }
            ],
            "static_url_path": "/flasgger_static",
            "swagger_ui": True,
            "specs_route": "/api/docs"
        }

        swagger_template = {
            "swagger": "2.0",
            "info": {
                "title": "ADO Story Monitor API",
                "description": "API for monitoring and extracting stories and test cases from Azure DevOps",
                "version": "1.0.0",
                "contact": {
                    "name": "API Support"
                }
            },
            "basePath": "/",
            "schemes": ["http", "https"]
        }

        self.swagger = Swagger(self.app, config=swagger_config, template=swagger_template)

        # Setup routes
        self._setup_routes()

    def _tail_file(self, f, n):
        """Efficiently read the last n lines from a file object opened in binary mode"""
        try:
            # Get file size
            f.seek(0, 2)  # Go to end of file
            file_size = f.tell()
            
            if file_size == 0:
                return []
            
            # Start from the end and work backwards
            lines_found = []
            buffer_size = min(8192, file_size)  # Read in 8KB chunks
            position = file_size
            
            while len(lines_found) < n and position > 0:
                # Calculate read position
                read_size = min(buffer_size, position)
                position -= read_size
                f.seek(position)
                
                # Read chunk
                chunk = f.read(read_size)
                if isinstance(chunk, bytes):
                    chunk = chunk.decode('utf-8', errors='ignore')
                
                # Split into lines and add to our collection
                chunk_lines = chunk.split('\n')
                
                # If we're not at the beginning of file, the first line might be partial
                if position > 0 and len(lines_found) > 0:
                    # Combine with the first line we already have
                    lines_found[0] = chunk_lines[-1] + lines_found[0]
                    chunk_lines = chunk_lines[:-1]
                
                # Add lines in reverse order (since we're reading backwards)
                lines_found = chunk_lines[::-1] + lines_found
                
                # Remove empty lines from the beginning/end
                while lines_found and not lines_found[0].strip():
                    lines_found.pop(0)
                while lines_found and not lines_found[-1].strip():
                    lines_found.pop()
            
            # Return the last n lines
            return lines_found[-n:] if len(lines_found) > n else lines_found
            
        except Exception as e:
            self.logger.error(f"Error in _tail_file: {e}")
            return []

    def _setup_routes(self):
        """Setup Flask routes"""

        @self.app.route('/')
        def dashboard():
            """Main dashboard page
            ---
            tags:
              - Dashboard
            responses:
              200:
                description: Returns the HTML dashboard page
            """
            return render_template('dashboard.html')

        @self.app.route('/api/health')
        def health_check():
            """Health check endpoint
            ---
            tags:
              - Health
            responses:
              200:
                description: Service health status
                schema:
                  type: object
                  properties:
                    status:
                      type: string
                      example: healthy
                    timestamp:
                      type: string
                      example: "2025-12-30T10:00:00"
                    service:
                      type: string
                      example: "ADO Story Extractor API"
                    monitor_running:
                      type: boolean
                      example: true
            """
            return jsonify({
                'status': 'healthy',
                'timestamp': datetime.now().isoformat(),
                'service': 'ADO Story Extractor API',
                'monitor_running': self.monitor.is_running if self.monitor else False
            })

        # Monitor control endpoints
        @self.app.route('/api/monitor/start', methods=['POST'])
        def start_monitor():
            """Start the monitoring service
            ---
            tags:
              - Monitor
            responses:
              200:
                description: Monitor started successfully
                schema:
                  type: object
                  properties:
                    success:
                      type: boolean
                      example: true
                    message:
                      type: string
                      example: "Monitor started successfully"
                    status:
                      type: object
              400:
                description: Monitor already running or not configured
                schema:
                  type: object
                  properties:
                    success:
                      type: boolean
                      example: false
                    error:
                      type: string
              500:
                description: Failed to start monitor
                schema:
                  type: object
                  properties:
                    success:
                      type: boolean
                      example: false
                    error:
                      type: string
            """
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
                    }), 400

                # Start monitor in a separate thread to avoid blocking the API
                def start_monitor_thread():
                    try:
                        self.monitor.start()
                    except Exception as e:
                        self.logger.error(f"Monitor thread failed: {e}")

                self.monitor_thread = threading.Thread(target=start_monitor_thread, daemon=True)
                self.monitor_thread.start()

                return jsonify({
                    'success': True,
                    'message': 'Monitor started successfully',
                    'status': self.monitor.get_status()
                })

            except Exception as e:
                self.logger.error(f"Error starting monitor: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to start monitor: {str(e)}'
                }), 500

        @self.app.route('/api/monitor/stop', methods=['POST'])
        def stop_monitor():
            """Stop the monitoring service
            ---
            tags:
              - Monitor
            responses:
              200:
                description: Monitor stopped successfully
                schema:
                  type: object
                  properties:
                    success:
                      type: boolean
                      example: true
                    message:
                      type: string
                      example: "Monitor stopped successfully"
              400:
                description: Monitor not running or not configured
              500:
                description: Failed to stop monitor
            """
            try:
                if not self.monitor:
                    return jsonify({
                        'success': False,
                        'error': 'Monitor not configured'
                    }), 400

                if not self.monitor.is_running:
                    return jsonify({
                        'success': False,
                        'error': 'Monitor is not running'
                    }), 400

                self.monitor.stop()

                return jsonify({
                    'success': True,
                    'message': 'Monitor stopped successfully',
                    'status': self.monitor.get_status()
                })

            except Exception as e:
                self.logger.error(f"Error stopping monitor: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to stop monitor: {str(e)}'
                }), 500

        @self.app.route('/api/monitor/status', methods=['GET'])
        def get_monitor_status():
            """Get current monitoring status
            ---
            tags:
              - Monitor
            responses:
              200:
                description: Current monitor status
                schema:
                  type: object
                  properties:
                    is_running:
                      type: boolean
                      example: true
                    check_count:
                      type: integer
                      example: 42
                    last_check:
                      type: string
                      example: "2025-12-30T10:00:00"
                    epic_count:
                      type: integer
                      example: 5
              400:
                description: Monitor not configured
            """
            try:
                if not self.monitor:
                    return jsonify({
                        'is_running': False,
                        'error': 'Monitor not configured'
                    })

                status = self.monitor.get_status()
                return jsonify(status)

            except Exception as e:
                self.logger.error(f"Error getting monitor status: {str(e)}")
                return jsonify({
                    'is_running': False,
                    'error': f'Failed to get monitor status: {str(e)}'
                })

        @self.app.route('/api/epics', methods=['GET'])
        def get_epics():
            """Get list of monitored EPICs with details
            ---
            tags:
              - Epics
            responses:
              200:
                description: List of all monitored epics
                schema:
                  type: array
                  items:
                    type: object
                    properties:
                      id:
                        type: string
                        example: "12345"
                      title:
                        type: string
                        example: "Epic Title"
                      state:
                        type: string
                        example: "Active"
                      story_count:
                        type: integer
                        example: 15
            """
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

                            # Determine monitoring state
                            monitor_state = "Unknown"
                            if epic_state.consecutive_errors > 0:
                                monitor_state = f"Error (retries: {epic_state.consecutive_errors})"
                            elif epic_state.last_snapshot is not None:
                                if epic_state.stories_extracted if hasattr(epic_state, 'stories_extracted') else False:
                                    monitor_state = "Monitored (stories extracted)"
                                else:
                                    monitor_state = "Monitored"
                            else:
                                monitor_state = "Initializing"

                            epics_data.append({
                                'id': epic_id,
                                'title': epic_info.get('fields', {}).get('System.Title', f'Epic {epic_id}'),
                                'state': monitor_state,
                                'ado_state': epic_info.get('fields', {}).get('System.State', 'Unknown'),  # Keep ADO state separately
                                'story_count': story_count,
                                'last_changed': epic_state.last_check.isoformat() if epic_state.last_check else None,
                                'consecutive_errors': epic_state.consecutive_errors,
                                'has_snapshot': epic_state.last_snapshot is not None,
                                'stories_extracted': epic_state.stories_extracted if hasattr(epic_state, 'stories_extracted') else False
                            })
                    except Exception as e:
                        self.logger.error(f"Error fetching details for EPIC {epic_id}: {e}")
                        # Still include the EPIC even if we can't get details
                        # Determine monitoring state for error case
                        monitor_state = "Unknown"
                        if epic_state.consecutive_errors > 0:
                            monitor_state = f"Error (retries: {epic_state.consecutive_errors})"
                        elif epic_state.last_snapshot is not None:
                            if epic_state.stories_extracted if hasattr(epic_state, 'stories_extracted') else False:
                                monitor_state = "Monitored (stories extracted)"
                            else:
                                monitor_state = "Monitored"
                        else:
                            monitor_state = "Initializing"

                        epics_data.append({
                            'id': epic_id,
                            'title': f'Epic {epic_id}',
                            'state': monitor_state,
                            'ado_state': 'Unknown',
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

                # Get current env values for display
                env_vars = self.env_manager.read_env_file()
                
                config_dict = {
                    'ado_org_url': f"https://dev.azure.com/{Settings.ADO_ORGANIZATION}" if Settings.ADO_ORGANIZATION else '',
                    'ado_organization': Settings.ADO_ORGANIZATION or '',
                    'ado_project': Settings.ADO_PROJECT or '',
                    'ado_pat': get_masked_value(Settings.ADO_PAT or ''),  # Masked but shows structure
                    'openai_api_key': get_masked_value(Settings.OPENAI_API_KEY or ''),  # Masked but shows structure
                    'openai_model': getattr(Settings, 'OPENAI_MODEL', 'gpt-4'),
                    'story_extraction_type': getattr(Settings, 'STORY_EXTRACTION_TYPE', 'User Story'),
                    'test_case_extraction_type': getattr(Settings, 'TEST_CASE_EXTRACTION_TYPE', 'Issue'),
                    'check_interval_minutes': self.monitor.config.poll_interval_seconds // 60 if self.monitor.config.poll_interval_seconds else 5,
                    'epic_ids': list(self.monitor.monitored_epics.keys()) if self.monitor.monitored_epics else [],
                    'auto_sync': self.monitor.config.auto_sync if hasattr(self.monitor.config, 'auto_sync') else True,
                    'auto_extract_new_epics': self.monitor.config.auto_extract_new_epics if hasattr(self.monitor.config, 'auto_extract_new_epics') else True,
                    
                    # Add file path information
                    'env_file_path': self.env_manager.get_env_file_path(),
                    'env_file_directory': self.env_manager.get_env_file_directory(),
                    'env_file_writable': is_env_file_writable('config/.env')
                }

                return jsonify(config_dict)

            except Exception as e:
                self.logger.error(f"Error getting config: {str(e)}")
                return jsonify({'error': f'Failed to get configuration: {str(e)}'}), 500

        @self.app.route('/api/config', methods=['PUT'])
        def update_config():
            """Update configuration and .env file"""
            try:
                if not self.monitor:
                    return jsonify({'error': 'Monitor not configured'}), 400

                data = request.get_json()
                if not data:
                    return jsonify({'error': 'No configuration data provided'}), 400

                # Prepare environment variables updates
                env_updates = {}
                
                # Handle Azure DevOps settings - these are required
                if 'ado_organization' in data:
                    if not data['ado_organization'].strip():
                        return jsonify({'error': 'Organization is required'}), 400
                    env_updates['ADO_ORGANIZATION'] = data['ado_organization'].strip()
                
                if 'ado_project' in data:
                    if not data['ado_project'].strip():
                        return jsonify({'error': 'Project Name is required'}), 400
                    env_updates['ADO_PROJECT'] = data['ado_project'].strip()
                
                # Handle sensitive fields - only update if provided and not masked
                if 'ado_pat' in data and data['ado_pat'] and not data['ado_pat'].startswith('*'):
                    env_updates['ADO_PAT'] = data['ado_pat'].strip()
                
                if 'openai_api_key' in data and data['openai_api_key'] and not data['openai_api_key'].startswith('*'):
                    env_updates['OPENAI_API_KEY'] = data['openai_api_key'].strip()
                
                # Handle other OpenAI settings
                if 'openai_model' in data:
                    env_updates['OPENAI_MODEL'] = data['openai_model']
                
                # Handle work item types
                if 'story_extraction_type' in data:
                    env_updates['ADO_STORY_EXTRACTION_TYPE'] = data['story_extraction_type']
                
                if 'test_case_extraction_type' in data:
                    env_updates['ADO_TEST_CASE_EXTRACTION_TYPE'] = data['test_case_extraction_type']

                # Update .env file if we have environment updates
                env_update_success = True
                if env_updates:
                    env_update_success = self.env_manager.update_env_variables(env_updates)
                    if env_update_success:
                        # Reload settings to pick up changes
                        Settings.reload_config()
                        self.logger.info(f"Updated .env file with: {list(env_updates.keys())}")
                    else:
                        self.logger.error("Failed to update .env file")
                        return jsonify({'error': 'Failed to update environment file'}), 500

                # Update monitor configuration
                if 'check_interval_minutes' in data:
                    self.monitor.config.poll_interval_seconds = data['check_interval_minutes'] * 60

                if 'auto_sync' in data:
                    self.monitor.config.auto_sync = bool(data['auto_sync'])

                if 'auto_extract_new_epics' in data:
                    self.monitor.config.auto_extract_new_epics = bool(data['auto_extract_new_epics'])

                # Handle EPIC IDs
                if 'epic_ids' in data and isinstance(data['epic_ids'], list):
                    # Clear current EPICs and add new ones
                    current_epics = set(self.monitor.monitored_epics.keys())
                    new_epics = set(str(eid) for eid in data['epic_ids'])
                    
                    # Remove EPICs not in the new list
                    for epic_id in current_epics - new_epics:
                        if epic_id in self.monitor.monitored_epics:
                            del self.monitor.monitored_epics[epic_id]
                    
                    # Add new EPICs
                    for epic_id in new_epics - current_epics:
                        self.monitor.add_epic(str(epic_id))

                # Save monitor configuration to file
                try:
                    config_data = {
                        'poll_interval_seconds': self.monitor.config.poll_interval_seconds,
                        'epic_ids': data.get('epic_ids', []),
                        'auto_sync': self.monitor.config.auto_sync,
                        'auto_extract_new_epics': getattr(self.monitor.config, 'auto_extract_new_epics', True),
                        'log_level': getattr(self.monitor.config, 'log_level', 'INFO'),
                        'max_concurrent_syncs': getattr(self.monitor.config, 'max_concurrent_syncs', 3),
                        'retry_attempts': getattr(self.monitor.config, 'retry_attempts', 3),
                        'retry_delay_seconds': getattr(self.monitor.config, 'retry_delay_seconds', 60)
                    }
                    
                    with open('config/monitor_config.json', 'w') as f:
                        json.dump(config_data, f, indent=2)
                    
                    self.logger.info("Monitor configuration updated successfully")
                except Exception as e:
                    self.logger.error(f"Failed to save monitor configuration: {e}")

                return jsonify({
                    'success': True,
                    'message': 'Configuration updated successfully',
                    'env_updated': bool(env_updates and env_update_success)
                })

            except Exception as e:
                self.logger.error(f"Error updating config: {str(e)}")
                return jsonify({'error': f'Failed to update configuration: {str(e)}'}), 500

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

        @self.app.route('/api/logs', methods=['GET'])
        def get_logs():
            """Get recent log entries from the monitor log file - optimized for large files"""
            try:
                import os
                from datetime import datetime
                
                log_file_path = 'logs/epic_monitor.log'
                
                # Check if log file exists
                if not os.path.exists(log_file_path):
                    return jsonify({
                        'success': True,
                        'logs': [],
                        'total_entries': 0,
                        'message': 'No log file found'
                    })
                
                # Get the number of lines to return (default 100, max 500 for performance)
                limit = request.args.get('limit', 100, type=int)
                limit = min(max(limit, 10), 500)  # Reduced max from 1000 to 500 for performance
                
                # Use efficient tail reading for large files
                logs = []
                try:
                    # Get file size first to avoid reading huge files entirely
                    file_size = os.path.getsize(log_file_path)
                    
                    # If file is too large (>10MB), read from end using seek
                    if file_size > 10 * 1024 * 1024:  # 10MB threshold
                        with open(log_file_path, 'rb') as f:
                            # Start from end and read backwards to find last N lines
                            lines = self._tail_file(f, limit)
                    else:
                        # For smaller files, read normally
                        with open(log_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            all_lines = f.readlines()
                            lines = all_lines[-limit:] if len(all_lines) > limit else all_lines
                    
                    # Parse log entries efficiently with timeout protection
                    parse_count = 0
                    max_parse_time = 2.0  # Max 2 seconds for parsing
                    start_parse = time.time()
                    
                    for line in lines:
                        # Timeout protection - don't spend too long parsing
                        if time.time() - start_parse > max_parse_time:
                            self.logger.warning(f"Log parsing timeout after {parse_count} entries")
                            break
                            
                        line = line.strip() if isinstance(line, str) else line.decode('utf-8', errors='ignore').strip()
                        if line:
                            # Quick parsing with minimal string operations
                            try:
                                # Fast split - only split what we need
                                if ' - ' in line:
                                    parts = line.split(' - ', 3)
                                    if len(parts) >= 4:
                                        timestamp_str, logger_name, level, message = parts
                                        logs.append({
                                            'timestamp': timestamp_str,  # Don't parse timestamp - just pass as string
                                            'level': level.lower() if level else 'info',
                                            'message': message[:500]  # Truncate long messages
                                        })
                                    else:
                                        logs.append({
                                            'timestamp': 'unknown',
                                            'level': 'info',
                                            'message': line[:500]
                                        })
                                else:
                                    logs.append({
                                        'timestamp': 'unknown',
                                        'level': 'info', 
                                        'message': line[:500]
                                    })
                            except Exception:
                                # On any parsing error, just add the raw line
                                logs.append({
                                    'timestamp': 'unknown',
                                    'level': 'info',
                                    'message': str(line)[:500]
                                })
                        
                        parse_count += 1
                        
                        # Limit total entries processed to prevent memory issues
                        if parse_count >= limit:
                            break
                
                except Exception as io_error:
                    self.logger.error(f"Error reading log file: {str(io_error)}")
                    return jsonify({
                        'success': True,  # Return success with empty logs rather than error
                        'logs': [],
                        'total_entries': 0,
                        'error': f'Could not read log file: {str(io_error)}'
                    })
                
                return jsonify({
                    'success': True,
                    'logs': logs,
                    'total_entries': len(logs),
                    'limit': limit
                })
                
            except Exception as e:
                self.logger.error(f"Error in logs endpoint: {str(e)}")
                return jsonify({
                    'success': True,  # Return success to prevent UI errors
                    'logs': [],
                    'total_entries': 0,
                    'error': f'Logs temporarily unavailable: {str(e)}'
                }), 200  # Return 200 instead of 500 to prevent UI errors

        @self.app.route('/api/logs/clear', methods=['POST'])
        def clear_logs():
            """Clear the log file"""
            try:
                import os
                log_file_path = 'logs/epic_monitor.log'
                
                if os.path.exists(log_file_path):
                    # Clear the log file by opening it in write mode
                    with open(log_file_path, 'w') as f:
                        pass
                    
                    self.logger.info("Log file cleared via API request")
                    return jsonify({
                        'success': True,
                        'message': 'Log file cleared successfully'
                    })
                else:
                    return jsonify({
                        'success': True,
                        'message': 'No log file to clear'
                    })
                    
            except Exception as e:
                self.logger.error(f"Error clearing logs: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to clear logs: {str(e)}'
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
                        
                        # Add test case specific fields if available
                        if test_case.get('steps'):
                            steps_html = '<ol>' + ''.join(f'<li>{step}</li>' for step in test_case['steps']) + '</ol>'
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
    api = MonitorAPI(port=8080)
    api.run(debug=True)

#!/usr/bin/env python3
"""
Background monitoring service for EPIC change detection and automatic synchronization.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, ClassVar
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor
import requests

from src.agent import StoryExtractionAgent
from src.models import EpicSyncResult
from src.enhanced_story_creator import EnhancedStoryCreator
from src.models_enhanced import EnhancedUserStory


@dataclass
class MonitorConfig:
    """Configuration for the EPIC monitor"""
    OPENAI_RETRY_DELAY: ClassVar[int] = int(os.getenv('OPENAI_RETRY_DELAY', 5))
    poll_interval_seconds: int = 300  # 5 minutes default
    max_concurrent_syncs: int = 3
    snapshot_directory: str = "snapshots"
    log_level: str = "INFO"
    epic_ids: List[str] = None
    excluded_epic_ids: List[str] = None  # List of Epic IDs to exclude from automatic monitoring
    auto_sync: bool = True
    auto_extract_new_epics: bool = True  # New option to control story extraction for new epics
    notification_webhook: Optional[str] = None
    retry_attempts: int = 3
    retry_delay_seconds: int = 60
    # OpenAI settings
    openai_model: str = "gpt-4"  # Model to use for content generation
    openai_max_retries: int = 3  # Maximum number of retries for OpenAI calls
    openai_retry_delay: int = 10  # Delay between retries in seconds
    
    # Work item types and settings
    requirement_type: str = "Epic"  # Default work item type for requirements
    user_story_type: str = "User Story"  # Default work item type for user stories
    story_extraction_type: str = "User Story"  # Can be "User Story" or "Task"
    test_case_extraction_type: str = "Test Case"   # Can be "Issue" or "Test Case"
    skip_duplicate_check: bool = False  # Option to skip duplicate checking
    # Test case extraction settings
    auto_test_case_extraction: bool = True  # Whether to automatically extract test cases for new stories
    
    # Enhanced configuration options
    extraction_cooldown_hours: int = 24  # Cooldown period before re-extraction (0 to disable)
    enable_content_hash_comparison: bool = True  # Use hash-based change detection
    
    # Feature hierarchy options (Epic → Feature → Story)
    enable_feature_hierarchy: bool = True  # Enable Epic → Feature → Story hierarchy extraction
    auto_extract_features_from_epic: bool = True  # Auto-extract Features when processing an Epic
    auto_extract_stories_from_feature: bool = True  # Auto-extract Stories when processing a Feature

@dataclass
class FeatureMonitorState:
    """State tracking for a monitored Feature within an Epic"""
    feature_id: str
    epic_id: str  # Parent Epic ID
    title: str = ""
    last_check: Optional[datetime] = None
    last_snapshot: Optional[Dict] = None
    consecutive_errors: int = 0
    stories_extracted: bool = False
    extracted_stories: List[Dict] = None  # List of extracted stories for this feature
    story_count: int = 0


@dataclass
class EpicMonitorState:
    """State tracking for a monitored EPIC"""
    epic_id: str
    last_check: datetime
    last_snapshot: Optional[Dict] = None
    consecutive_errors: int = 0
    last_sync_result: Optional[Dict] = None
    stories_extracted: bool = False  # Track if stories have been extracted for this epic
    extracted_stories: List[Dict] = None  # List of extracted stories for duplicate prevention
    # Feature hierarchy tracking
    features: List[FeatureMonitorState] = None  # List of features under this Epic
    feature_count: int = 0  # Number of features detected
    total_story_count: int = 0  # Total stories across all features


class EpicChangeMonitor:
    """Background service that monitors EPICs for changes and triggers synchronization"""
    
    def __init__(self, config: MonitorConfig):
        self.config = config
        self.agent = StoryExtractionAgent()
        self.story_creator = EnhancedStoryCreator()  # Add enhanced story creator
        self.logger = self._setup_logger()
        self.is_running = False
        self.monitored_epics: Dict[str, EpicMonitorState] = {}
        self.executor = ThreadPoolExecutor(max_workers=config.max_concurrent_syncs)
        self.snapshot_dir = Path(config.snapshot_directory)
        self.snapshot_dir.mkdir(exist_ok=True)
        # ThreadPoolExecutor for async syncs
        self.snapshot_dir.mkdir(exist_ok=True)
        
        # State file to track which epics have been processed
        self.state_file = Path("monitor_state.json")
        self.processed_epics = self._load_processed_epics()

        # Load existing snapshots
        self._load_existing_snapshots()
    
    def _setup_logger(self) -> logging.Logger:
        """Setup logging for the monitor"""
        logger = logging.getLogger("EpicChangeMonitor")
        logger.setLevel(getattr(logging, self.config.log_level.upper()))
        if not logger.handlers:
            # Console handler
            console_handler = logging.StreamHandler()
            console_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)
            # File handler
            log_file = Path("logs") / "epic_monitor.log"
            log_file.parent.mkdir(exist_ok=True)
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(console_formatter)
            logger.addHandler(file_handler)
        return logger
    
    def _load_processed_epics(self) -> Dict[str, Set[str]]:
        """Load the dictionary of processed items keyed by requirement type (Epic, Feature, etc.)"""
        try:
            if self.state_file.exists():
                with open(self.state_file, 'r') as f:
                    state_data = json.load(f)
                # Handle migration from old format (single list) to new format (dict by type)
                if 'processed_epics' in state_data and isinstance(state_data['processed_epics'], list):
                    # Migrate old format: assume old list was for 'Epic' type
                    self.logger.info("Migrating processed_epics from old format to new type-based format")
                    return {'Epic': set(state_data['processed_epics'])}
                elif 'processed_items_by_type' in state_data:
                    # New format: dict keyed by requirement type
                    return {k: set(v) for k, v in state_data['processed_items_by_type'].items()}
        except Exception as e:
            self.logger.error(f"Failed to load processed epics state: {e}")
        return {}
    
    def _save_processed_epics(self):
        """Save the dictionary of processed items by type to state file"""
        try:
            state_data = {
                'processed_items_by_type': {k: list(v) for k, v in self.processed_epics.items()},
                'current_requirement_type': self.config.requirement_type,
                'last_updated': datetime.now().isoformat()
            }
            with open(self.state_file, 'w') as f:
                json.dump(state_data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save processed epics state: {e}")
    
    def _get_processed_items_for_current_type(self) -> Set[str]:
        """Get the set of processed items for the current requirement type"""
        return self.processed_epics.get(self.config.requirement_type, set())
    
    def _add_processed_item(self, item_id: str):
        """Add an item to the processed set for the current requirement type"""
        if self.config.requirement_type not in self.processed_epics:
            self.processed_epics[self.config.requirement_type] = set()
        self.processed_epics[self.config.requirement_type].add(item_id)
    
    def _remove_processed_item(self, item_id: str):
        """Remove an item from the processed set for the current requirement type"""
        if self.config.requirement_type in self.processed_epics:
            self.processed_epics[self.config.requirement_type].discard(item_id)

    def _load_existing_snapshots(self):
        for epic_id in self.config.epic_ids or []:
            snapshot_file = self.snapshot_dir / f"epic_{epic_id}.json"
            if snapshot_file.exists():
                try:
                    with open(snapshot_file, 'r') as f:
                        snapshot_data = json.load(f)
                    stories = snapshot_data.get('stories', [])
                    processed_items = self._get_processed_items_for_current_type()
                    self.monitored_epics[epic_id] = EpicMonitorState(
                        epic_id=epic_id,
                        last_check=datetime.now(),
                        last_snapshot=snapshot_data,
                        stories_extracted=epic_id in processed_items,
                        extracted_stories=stories
                    )
                    self.logger.info(f"Loaded existing snapshot for EPIC {epic_id}")
                except Exception as e:
                    self.logger.error(f"Failed to load snapshot for EPIC {epic_id}: {e}")
                    processed_items = self._get_processed_items_for_current_type()
                    self.monitored_epics[epic_id] = EpicMonitorState(
                        epic_id=epic_id,
                        last_check=datetime.now(),
                        stories_extracted=epic_id in processed_items,
                        extracted_stories=[]
                    )
            else:
                processed_items = self._get_processed_items_for_current_type()
                self.monitored_epics[epic_id] = EpicMonitorState(
                    epic_id=epic_id,
                    last_check=datetime.now(),
                    stories_extracted=epic_id in processed_items,
                    extracted_stories=[]
                )

    def add_epic(self, epic_id: str) -> bool:
        """Add an EPIC to monitoring and trigger immediate check/sync."""
        try:
            if epic_id not in self.monitored_epics:
                # Get initial snapshot
                initial_snapshot = self.agent.get_epic_snapshot(epic_id)
                if initial_snapshot:
                    self.monitored_epics[epic_id] = EpicMonitorState(
                        epic_id=epic_id,
                        last_check=datetime.now(),
                        last_snapshot=initial_snapshot,
                        consecutive_errors=0
                    )
                    self._save_snapshot(epic_id, initial_snapshot)
                    self.logger.info(f"Added EPIC {epic_id} to monitoring and will check for changes immediately.")
                    # Immediately check and sync the new Epic
                    if self._check_epic_changes(epic_id):
                        if self.config.auto_sync:
                            self.logger.info(f"Immediately synchronizing new EPIC {epic_id} after detection.")
                            self._sync_epic(epic_id)
                    return True
                else:
                    self.monitored_epics[epic_id] = EpicMonitorState(
                        epic_id=epic_id,
                        last_check=datetime.now(),
                        last_snapshot=None,
                        consecutive_errors=1
                    )
                    self.logger.warning(f"Added EPIC {epic_id} to monitoring, but could not fetch initial snapshot. Will retry.")
                    return False
            else:
                self.logger.warning(f"EPIC {epic_id} is already being monitored")
                return True
        except Exception as e:
            self.logger.error(f"Failed to add EPIC {epic_id} to monitoring: {e}")
            return False
    
    def remove_epic(self, epic_id: str, exclude_from_auto_monitoring: bool = True) -> bool:
        """Remove an EPIC from monitoring and optionally add to exclusion list"""
        if epic_id in self.monitored_epics:
            del self.monitored_epics[epic_id]
            self.logger.info(f"Removed EPIC {epic_id} from monitoring")
            
            # Add to exclusion list to prevent automatic re-addition
            if exclude_from_auto_monitoring:
                if not self.config.excluded_epic_ids:
                    self.config.excluded_epic_ids = []
                
                if epic_id not in self.config.excluded_epic_ids:
                    self.config.excluded_epic_ids.append(epic_id)
                    self.logger.info(f"Added EPIC {epic_id} to exclusion list")
                    
                    # Save the updated configuration
                    try:
                        save_config_to_file(self.config, "config/monitor_config.json")
                    except Exception as e:
                        self.logger.error(f"Failed to save configuration after excluding EPIC {epic_id}: {e}")
            
            return True
        return False
    
    def _save_snapshot(self, epic_id: str, snapshot_data: Dict):
        """Save snapshot for an epic, including stories"""
        snapshot_file = self.snapshot_dir / f"epic_{epic_id}.json"
        try:
            with open(snapshot_file, 'w') as f:
                json.dump(snapshot_data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save snapshot for EPIC {epic_id}: {e}")

    def _check_epic_exists(self, epic_id: str) -> bool:
        """Check if an EPIC exists in Azure DevOps"""
        try:
            # Try to get the EPIC work item to verify it exists
            work_item = self.agent.ado_client.get_work_item_by_id(epic_id)
            if work_item and work_item.fields:
                # Check if it's the correct Requirement type
                work_item_type = work_item.fields.get("System.WorkItemType")
                if work_item_type == self.config.requirement_type:
                    self.logger.debug(f"Requirement {epic_id} exists in Azure DevOps")
                    return True
                else:
                    self.logger.warning(f"Work item {epic_id} exists but is not a {self.config.requirement_type} (type: {work_item_type})")
                    return False
            else:
                self.logger.warning(f"EPIC {epic_id} not found in Azure DevOps")
                return False
        except requests.exceptions.HTTPError as e:
            # Only treat 404 (Not Found) as EPIC doesn't exist
            if e.response.status_code == 404:
                self.logger.warning(f"EPIC {epic_id} not found in Azure DevOps (404)")
                return False
            else:
                # Other HTTP errors (401, 403, 500, etc.) should not be treated as "doesn't exist"
                self.logger.error(f"HTTP error checking EPIC {epic_id}: {e} - treating as exists")
                return True
        except Exception as e:
            # Check for Azure DevOps specific "work item does not exist" errors
            error_message = str(e).lower()
            if ("does not exist" in error_message or 
                "tf401232" in error_message or 
                "work item not found" in error_message or
                "you do not have permissions to read it" in error_message):
                self.logger.warning(f"EPIC {epic_id} not found in Azure DevOps: {e}")
                return False
            else:
                # Network issues, authentication problems, etc. should not be treated as "doesn't exist"
                self.logger.error(f"Error checking if EPIC {epic_id} exists: {e} - treating as exists to avoid false removal")
                return True

    def _handle_epic_failure(self, epic_id: str, error: str):
        """Handle EPIC failure and potentially remove it from monitoring after 3 retries"""
        epic_state = self.monitored_epics.get(epic_id)
        if not epic_state:
            return

        epic_state.consecutive_errors += 1
        self.logger.warning(f"EPIC {epic_id} failed (attempt {epic_state.consecutive_errors}/3): {error}")

        # Remove EPIC from monitoring after 3 consecutive failures
        if epic_state.consecutive_errors >= 3:
            self.logger.error(f"EPIC {epic_id} has failed {epic_state.consecutive_errors} times. Removing from monitoring.")

            # Check if EPIC still exists in Azure DevOps
            if not self._check_epic_exists(epic_id):
                self.logger.info(f"EPIC {epic_id} no longer exists in Azure DevOps. Removing from monitoring.")
            else:
                self.logger.warning(f"EPIC {epic_id} still exists but has too many failures. Removing from monitoring.")

            # Remove from monitoring
            self._remove_epic_from_monitoring(epic_id)

            # Send notification if webhook is configured
            if self.config.notification_webhook:
                self._send_notification(f"EPIC {epic_id} has been automatically removed from monitoring after 3 consecutive failures.")

    def _remove_epic_from_monitoring(self, epic_id: str):
        """Remove an EPIC from monitoring and clean up associated files"""
        try:
            # Remove from monitored epics
            if epic_id in self.monitored_epics:
                del self.monitored_epics[epic_id]
                self.logger.info(f"Removed EPIC {epic_id} from monitored epics list")

            # Remove from processed items if present
            self._remove_processed_item(epic_id)
            self._save_processed_epics()
            self.logger.info(f"Removed EPIC {epic_id} from processed items list")

            # Remove snapshot file if it exists
            snapshot_file = self.snapshot_dir / f"epic_{epic_id}.json"
            if snapshot_file.exists():
                snapshot_file.unlink()
                self.logger.info(f"Removed snapshot file for EPIC {epic_id}")

            self.logger.info(f"Successfully removed EPIC {epic_id} from all monitoring systems")

        except Exception as e:
            self.logger.error(f"Error removing EPIC {epic_id} from monitoring: {e}")

    def _send_notification(self, message: str):
        """Send notification about EPIC removal (placeholder for webhook implementation)"""
        try:
            self.logger.info(f"Notification: {message}")
            # TODO: Implement actual webhook notification if needed
            # import requests
            # if self.config.notification_webhook:
            #     requests.post(self.config.notification_webhook, json={'message': message})
        except Exception as e:
            self.logger.error(f"Failed to send notification: {e}")

    def _check_epic_changes(self, epic_id: str) -> bool:
        """Check if epic already has stories extracted to prevent duplicates"""
        state = self.monitored_epics.get(epic_id)
        if not state:
            return False

        # If stories already extracted and duplicate check is enabled, skip
        processed_items = self._get_processed_items_for_current_type()
        if not self.config.skip_duplicate_check and epic_id in processed_items:
            self.logger.info(f"Epic {epic_id} already has stories extracted. Skipping to prevent duplicates.")
            return False

        # Check if epic has existing stories in ADO
        try:
            existing_ado_stories = self.agent.ado_client.get_child_stories(int(epic_id))
            if existing_ado_stories:
                self.logger.info(f"Epic {epic_id} already has {len(existing_ado_stories)} stories in ADO. Marking as processed.")
                self._add_processed_item(epic_id)
                state.stories_extracted = True
                self._save_processed_epics()
                return False
        except Exception as e:
            self.logger.error(f"Error checking existing stories for Epic {epic_id}: {e}")

        return True  # Proceed with story extraction

    def _sync_epic(self, epic_id: str) -> EpicSyncResult:
        """Synchronize an EPIC with retry logic"""
        epic_state = self.monitored_epics[epic_id]
        
        for attempt in range(self.config.retry_attempts):
            try:
                self.logger.info(f"Synchronizing EPIC {epic_id} (attempt {attempt + 1})")
                
                result = self.agent.synchronize_epic(
                    epic_id=epic_id,
                    stored_snapshot=epic_state.last_snapshot
                )
                
                if result.sync_successful:
                    # Update snapshot after successful sync
                    new_snapshot = self.agent.get_epic_snapshot(epic_id)
                    if new_snapshot:
                        epic_state.last_snapshot = new_snapshot
                        self._save_snapshot(epic_id, new_snapshot)
                    
                    # Mark epic as processed if stories were created
                    if len(result.created_stories) > 0:
                        self._add_processed_item(epic_id)
                        epic_state.stories_extracted = True
                        self._save_processed_epics()
                    
                    # Store sync result
                    epic_state.last_sync_result = {
                        'timestamp': datetime.now().isoformat(),
                        'success': True,
                        'created_stories': result.created_stories,
                        'updated_stories': result.updated_stories,
                        'unchanged_stories': result.unchanged_stories
                    }
                    
                    self.logger.info(f"Successfully synchronized EPIC {epic_id}")
                    self.logger.info(f"  Created: {len(result.created_stories)} stories")
                    self.logger.info(f"  Updated: {len(result.updated_stories)} stories")
                    self.logger.info(f"  Unchanged: {len(result.unchanged_stories)} stories")
                    
                    return result
                else:
                    self.logger.error(f"Sync failed for EPIC {epic_id}: {result.error_message}")
                    if attempt < self.config.retry_attempts - 1:
                        self.logger.info(f"Retrying in {self.config.retry_delay_seconds} seconds...")
                        time.sleep(self.config.retry_delay_seconds)
                    
            except Exception as e:
                self.logger.error(f"Exception during sync of EPIC {epic_id}: {e}")
                if attempt < self.config.retry_attempts - 1:
                    self.logger.info(f"Retrying in {self.config.retry_delay_seconds} seconds...")
                    time.sleep(self.config.retry_delay_seconds)
        
        # All attempts failed
        epic_state.last_sync_result = {
            'timestamp': datetime.now().isoformat(),
            'success': False,
            'error': f"Failed after {self.config.retry_attempts} attempts"
        }
        
        return EpicSyncResult(
            epic_id=epic_id,
            epic_title="",
            sync_successful=False,
            error_message=f"Failed after {self.config.retry_attempts} attempts"
        )
    
    async def _monitor_loop(self):
        """Main monitoring loop"""
        self.logger.info("Starting EPIC monitoring loop")
        try:
            while self.is_running:
                try:
                    # Auto-detect new Epics at the start of each cycle
                    self.update_monitored_epics()
                    # Check each monitored EPIC
                    sync_tasks = []

                    for epic_id in list(self.monitored_epics.keys()):
                        try:
                            epic_state = self.monitored_epics[epic_id]

                            # Check if EPIC exists before processing
                            if not self._check_epic_exists(epic_id):
                                self.logger.info(f"EPIC {epic_id} no longer exists in Azure DevOps. Removing from monitoring.")
                                self._remove_epic_from_monitoring(epic_id)
                                continue

                            # Reset consecutive errors on successful existence check
                            if epic_state.consecutive_errors > 0:
                                self.logger.info(f"EPIC {epic_id} is accessible again, resetting error count")
                                epic_state.consecutive_errors = 0

                            # Skip if too many consecutive errors (will be removed by _handle_epic_failure)
                            if epic_state.consecutive_errors >= 3:
                                continue

                            # Check for actual content changes using enhanced detection
                            if self._check_for_epic_changes(epic_id):
                                # Only proceed with sync if stories should be extracted
                                if self._should_extract_stories(epic_id):
                                    if self.config.auto_sync:
                                        # Schedule sync
                                        if not asyncio.get_event_loop().is_closed():
                                            future = asyncio.get_event_loop().run_in_executor(
                                                self.executor, self._sync_epic, epic_id
                                            )
                                            sync_tasks.append((epic_id, future))
                                        else:
                                            self.logger.warning("Event loop is closed, skipping scheduling new tasks.")
                                    else:
                                        self.logger.info(f"Changes detected in EPIC {epic_id}, but auto-sync is disabled")
                                else:
                                    self.logger.debug(f"EPIC {epic_id} has changes but stories should not be extracted")
                            else:
                                self.logger.debug(f"EPIC {epic_id} - No content changes detected")

                            # Update last check time
                            epic_state.last_check = datetime.now()

                        except Exception as e:
                            self.logger.error(f"Error processing EPIC {epic_id}: {e}")
                            self._handle_epic_failure(epic_id, str(e))
                            import traceback
                            self.logger.error(traceback.format_exc())

                    # Wait for sync tasks to complete
                    if sync_tasks:
                        self.logger.info(f"Running {len(sync_tasks)} synchronization tasks")
                        for epic_id, future in sync_tasks:
                            try:
                                await future
                            except Exception as e:
                                self.logger.error(f"Sync task failed for EPIC {epic_id}: {e}")
                                import traceback
                                self.logger.error(traceback.format_exc())

                    # Wait before next polling cycle
                    self.logger.debug(f"Monitoring cycle complete, sleeping for {self.config.poll_interval_seconds} seconds")
                    await asyncio.sleep(self.config.poll_interval_seconds)

                except Exception as e:
                    self.logger.error(f"Error in monitoring loop: {e}")
                    import traceback
                    self.logger.error(traceback.format_exc())
                    await asyncio.sleep(60)  # Wait a minute before retrying
        finally:
            self.logger.info("Shutting down executor and cleaning up.")
            self.executor.shutdown(wait=True)
            self.logger.info("Monitor loop exited cleanly.")

    def fetch_all_epic_ids(self) -> List[str]:
        """Fetch all Requirement IDs from Azure DevOps (filtered by work item type)."""
        try:
            self.logger.info(f"Fetching requirements with type: {self.config.requirement_type}")
            requirements = self.agent.ado_client.get_requirements(work_item_type=self.config.requirement_type)
            return [str(req.id) for req in requirements]
        except Exception as e:
            self.logger.error(f"Failed to fetch all Requirements ({self.config.requirement_type}): {e}")
            return []

    def update_monitored_epics(self):
        """Update the monitored Epics set by auto-detecting new Epics."""
        all_epic_ids = set(self.fetch_all_epic_ids())
        current_epic_ids = set(self.monitored_epics.keys())
        new_epics = all_epic_ids - current_epic_ids
        for epic_id in new_epics:
            # Check if Epic is in the exclusion list
            if self.config.excluded_epic_ids and epic_id in self.config.excluded_epic_ids:
                self.logger.info(f"Auto-detect: EPIC {epic_id} is in exclusion list, skipping automatic monitoring")
                continue
                
            self.logger.info(f"Auto-detect: Adding new Epic {epic_id} to monitoring.")
            added_successfully = self.add_epic(epic_id)
            
            # Only extract stories if this epic hasn't been processed before
            processed_items = self._get_processed_items_for_current_type()
            if added_successfully and self.config.auto_extract_new_epics and epic_id not in processed_items:
                self.logger.info(f"Auto-extraction enabled: Extracting stories for new Epic {epic_id}.")
                try:
                    extraction_result = self.agent.synchronize_epic(epic_id)
                    if extraction_result.sync_successful:
                        # Mark epic as processed
                        self._add_processed_item(epic_id)
                        self.monitored_epics[epic_id].stories_extracted = True
                        self._save_processed_epics()
                        
                        self.logger.info(f"Successfully extracted and synchronized {len(extraction_result.created_stories)} stories for new Epic {epic_id}.")
                        self.logger.info(f"  Story IDs: {extraction_result.created_stories}")
                    else:
                        self.logger.error(f"Failed to extract and synchronize stories for new Epic {epic_id}: {extraction_result.error_message}")
                except Exception as e:
                    self.logger.error(f"Exception during extraction for new Epic {epic_id}: {e}")
            elif added_successfully and epic_id in processed_items:
                self.logger.info(f"Epic {epic_id} has already been processed. Skipping story extraction.")
            elif added_successfully:
                self.logger.info(f"Auto-extraction disabled: Skipping story extraction for new Epic {epic_id}. Only monitoring for changes.")
        # Optionally, remove Epics that no longer exist in ADO
        # removed_epics = current_epic_ids - all_epic_ids
        # for epic_id in removed_epics:
        #     self.logger.info(f"Auto-detect: Removing Epic {epic_id} (no longer exists in ADO).")
        #     self.monitored_epics.pop(epic_id, None)

    def start(self):
        """Start the monitoring service"""
        if self.is_running:
            self.logger.warning("Monitor is already running")
            return
        
        self.is_running = True
        self.logger.info("Starting EPIC Change Monitor")

        # Load all existing EPICs from Azure DevOps when monitoring starts
        self._load_all_existing_epics()

        self.logger.info(f"Monitoring {len(self.monitored_epics)} EPICs")
        self.logger.info(f"Poll interval: {self.config.poll_interval_seconds} seconds")
        self.logger.info(f"Auto-sync enabled: {self.config.auto_sync}")
        self.logger.info(f"Auto-extract new epics: {self.config.auto_extract_new_epics}")

        import signal
        import threading
        class GracefulExit(SystemExit):
            pass
        def _shutdown_handler(signum, frame):
            self.logger.info(f"Received shutdown signal ({signum}), shutting down gracefully...")
            raise GracefulExit()

        # Only set up signal handlers in the main thread
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, _shutdown_handler)
            signal.signal(signal.SIGTERM, _shutdown_handler)
        else:
            self.logger.info("Not in main thread, skipping signal handler setup")

        # Run the monitoring loop
        try:
            asyncio.run(self._monitor_loop())
        except GracefulExit:
            self.logger.info("Graceful shutdown requested from signal handler.")
        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal")
        finally:
            self.stop()

    def stop(self):
        """Stop the monitoring service"""
        try:
            # Set the stop flag first to prevent new checks
            self.is_running = False
            
            # Wait for any ongoing checks to complete
            if hasattr(self, '_monitor_thread') and self._monitor_thread is not None:
                try:
                    self._monitor_thread.join(timeout=5)  # Wait up to 5 seconds
                except Exception as e:
                    self.logger.warning(f"Error waiting for monitor thread to stop: {e}")
                self._monitor_thread = None

            # Capture snapshots before stopping
            self.logger.info("Saving snapshots before shutdown")
            for epic_id, state in self.monitored_epics.items():
                try:
                    if state.last_snapshot:
                        self._save_snapshot(epic_id, state.last_snapshot)
                except Exception as e:
                    self.logger.error(f"Error saving snapshot for epic {epic_id}: {e}")

            # Save processed epics state
            try:
                self._save_processed_epics()
            except Exception as e:
                self.logger.error(f"Error saving processed epics state: {e}")
            
            # Clear any ongoing tasks and queues
            if hasattr(self, '_ongoing_checks'):
                self._ongoing_checks.clear()
            
            # Stop any background tasks or timers
            if hasattr(self, '_check_timer'):
                self._check_timer.cancel()
                
            self.logger.info("Monitor service stopped successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Error during monitor stop: {e}")
            self.is_running = False  # Ensure this is set even if something fails
            return False

        self.logger.info("Stopping EPIC Change Monitor")
        self.is_running = False
        self.executor.shutdown(wait=True)
        self.logger.info("EPIC Change Monitor stopped")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.stop()
        sys.exit(0)
    
    def get_status(self) -> Dict:
        """Get current monitoring status"""
        status = {
            'is_running': self.is_running,
            'config': asdict(self.config),
            'monitored_epics': {},
            'statistics': self.get_monitoring_statistics(),
            'last_update': datetime.now().isoformat()
        }
        
        for epic_id, state in self.monitored_epics.items():
            status['monitored_epics'][epic_id] = {
                'last_check': state.last_check.isoformat(),
                'consecutive_errors': state.consecutive_errors,
                'has_snapshot': state.last_snapshot is not None,
                'stories_extracted': state.stories_extracted,
                'last_sync_result': state.last_sync_result,
                'extracted_stories_count': len(state.extracted_stories) if state.extracted_stories else 0
            }
        
        return status

    def get_monitoring_statistics(self) -> Dict:
        """Get detailed monitoring statistics"""
        # Get processed items count for current type
        processed_items = self._get_processed_items_for_current_type()
        stats = {
            'total_epics_monitored': len(self.monitored_epics),
            'epics_with_stories_extracted': len(processed_items),
            'epics_with_errors': 0,
            'epics_with_snapshots': 0,
            'total_extracted_stories': 0,
            'successful_syncs': 0,
            'failed_syncs': 0,
            'current_requirement_type': self.config.requirement_type,
            'processed_items_by_type': {k: len(v) for k, v in self.processed_epics.items()}
        }
        
        for epic_id, state in self.monitored_epics.items():
            if state.consecutive_errors > 0:
                stats['epics_with_errors'] += 1
            
            if state.last_snapshot:
                stats['epics_with_snapshots'] += 1
            
            if state.extracted_stories:
                stats['total_extracted_stories'] += len(state.extracted_stories)
            
            if state.last_sync_result:
                if state.last_sync_result.get('success', False):
                    stats['successful_syncs'] += 1
                else:
                    stats['failed_syncs'] += 1
        
        return stats
    
    def force_check(self, epic_id: Optional[str] = None) -> Dict:
        """Force a check for changes (optionally for specific EPIC)"""
        results = {}
        
        epics_to_check = [epic_id] if epic_id else list(self.monitored_epics.keys())
        
        for eid in epics_to_check:
            if eid in self.monitored_epics:
                try:
                    has_changes = self._check_epic_changes(eid)
                    results[eid] = {
                        'has_changes': has_changes,
                        'check_time': datetime.now().isoformat()
                    }
                    
                    if has_changes and self.config.auto_sync:
                        sync_result = self._sync_epic(eid)
                        results[eid]['sync_result'] = {
                            'success': sync_result.sync_successful,
                            'created_stories': sync_result.created_stories,
                            'updated_stories': sync_result.updated_stories,
                            'error_message': sync_result.error_message
                        }
                except Exception as e:
                    results[eid] = {
                        'error': str(e),
                        'check_time': datetime.now().isoformat()
                    }
        
        return results

    def _load_all_existing_epics(self):
        """Load all existing EPICs from Azure DevOps and add them to monitoring"""
        try:
            self.logger.info("Scanning for all existing EPICs in Azure DevOps...")
            all_epic_ids = self.fetch_all_epic_ids()

            if not all_epic_ids:
                self.logger.warning("No EPICs found in Azure DevOps")
                return

            self.logger.info(f"Found {len(all_epic_ids)} EPICs in Azure DevOps")

            # Add each EPIC to monitoring if not already monitored
            newly_added = 0
            for epic_id in all_epic_ids:
                if epic_id not in self.monitored_epics:
                    # Check if Epic is in the exclusion list
                    if self.config.excluded_epic_ids and epic_id in self.config.excluded_epic_ids:
                        self.logger.info(f"EPIC {epic_id} is in exclusion list, skipping automatic monitoring")
                        continue
                    
                    self.logger.info(f"Adding existing EPIC {epic_id} to monitoring")
                    if self.add_epic(epic_id):
                        newly_added += 1
                else:
                    self.logger.debug(f"EPIC {epic_id} already being monitored")

            self.logger.info(f"Added {newly_added} new EPICs to monitoring")
            self.logger.info(f"Total EPICs being monitored: {len(self.monitored_epics)}")

        except Exception as e:
            self.logger.error(f"Failed to load existing EPICs: {e}")

    def _check_for_epic_changes(self, epic_id: str) -> bool:
        """Check if an EPIC has actual content changes that warrant story extraction/sync"""
        try:
            epic_state = self.monitored_epics.get(epic_id)
            if not epic_state:
                return False

            # Get current snapshot of the EPIC
            current_snapshot = self.agent.get_epic_snapshot(epic_id)
            if not current_snapshot:
                self.logger.warning(f"Could not get current snapshot for EPIC {epic_id}")
                return False

            # If we have no previous snapshot, this is a change (new EPIC)
            if not epic_state.last_snapshot:
                self.logger.info(f"EPIC {epic_id} - No previous snapshot, treating as changed")
                epic_state.last_snapshot = current_snapshot
                self._save_snapshot(epic_id, current_snapshot)
                return True

            # Compare snapshots using content hash for precise change detection
            previous_snapshot = epic_state.last_snapshot
            current_hash = self._calculate_content_hash(current_snapshot)
            previous_hash = self._calculate_content_hash(previous_snapshot)

            # Use hash comparison first for efficiency
            if current_hash and previous_hash and current_hash == previous_hash:
                self.logger.debug(f"EPIC {epic_id} - No changes detected (hash comparison)")
                return False

            # Fallback to detailed comparison if hashes differ or are unavailable
            changes_detected = False
            change_details = []

            # Check for title changes
            if current_snapshot.get('title', '') != previous_snapshot.get('title', ''):
                changes_detected = True
                change_details.append(f"Title changed: '{previous_snapshot.get('title', '')}' -> '{current_snapshot.get('title', '')}'")

            # Check for description changes
            if current_snapshot.get('description', '') != previous_snapshot.get('description', ''):
                changes_detected = True
                change_details.append("Description changed")

            # Check for state changes
            if current_snapshot.get('state', '') != previous_snapshot.get('state', ''):
                changes_detected = True
                change_details.append(f"State changed: '{previous_snapshot.get('state', '')}' -> '{current_snapshot.get('state', '')}'")

            # Check for priority changes
            if current_snapshot.get('priority', '') != previous_snapshot.get('priority', ''):
                changes_detected = True
                change_details.append(f"Priority changed: '{previous_snapshot.get('priority', '')}' -> '{current_snapshot.get('priority', '')}'")

            if changes_detected:
                self.logger.info(f"EPIC {epic_id} - Changes detected:")
                for detail in change_details:
                    self.logger.info(f"  {detail}")

                # Update stored snapshot
                epic_state.last_snapshot = current_snapshot
                self._save_snapshot(epic_id, current_snapshot)
                return True

            self.logger.debug(f"EPIC {epic_id} - No changes detected")
            return False

        except Exception as e:
            self.logger.error(f"Error checking changes for EPIC {epic_id}: {e}")
            return False

    def _should_extract_stories(self, epic_id: str) -> bool:
        """Determine if stories should be extracted for an EPIC based on configuration and state"""
        state = self.monitored_epics.get(epic_id)
        if not state:
            return False

        # If stories were already extracted, skip extraction
        if state.stories_extracted:
            self.logger.info(f"Stories already extracted for EPIC {epic_id}, skipping extraction")
            return False

        # Check cooldown period if configured
        if hasattr(self.config, 'extraction_cooldown_hours') and self.config.extraction_cooldown_hours > 0:
            if not self._check_cooldown_period(epic_id, self.config.extraction_cooldown_hours):
                self.logger.info(f"EPIC {epic_id} is in cooldown period, skipping extraction")
                return False

        # Check if the EPIC has changes that warrant story extraction
        if self.config.skip_duplicate_check or epic_id not in self.processed_epics:
            self.logger.info(f"EPIC {epic_id} has changes, proceeding with story extraction")
            return True
        else:
            self.logger.info(f"EPIC {epic_id} has no changes or duplicates, skipping story extraction")
            return False

    def reset_epic_processed_state(self, epic_id: str) -> bool:
        """Reset the processed state of an EPIC to allow re-extraction if needed"""
        try:
            processed_items = self._get_processed_items_for_current_type()
            if epic_id in processed_items:
                self._remove_processed_item(epic_id)
                self._save_processed_epics()
                
            if epic_id in self.monitored_epics:
                self.monitored_epics[epic_id].stories_extracted = False
                
            self.logger.info(f"Reset processed state for EPIC {epic_id}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to reset processed state for EPIC {epic_id}: {e}")
            return False

    def _calculate_content_hash(self, snapshot: Dict) -> str:
        """Calculate a hash of EPIC content for more precise change detection"""
        try:
            import hashlib
            # Create a stable string representation of the content
            content_parts = [
                snapshot.get('title', ''),
                snapshot.get('description', ''),
                snapshot.get('state', ''),
                snapshot.get('priority', ''),
                snapshot.get('area_path', ''),
                snapshot.get('iteration_path', '')
            ]
            content = '|'.join(str(part) for part in content_parts)
            return hashlib.md5(content.encode('utf-8')).hexdigest()
        except Exception as e:
            self.logger.error(f"Error calculating content hash: {e}")
            return ""

    def _check_cooldown_period(self, epic_id: str, hours: int = 24) -> bool:
        """Check if enough time has passed since last story extraction"""
        try:
            state = self.monitored_epics.get(epic_id)
            if not state or not state.last_sync_result:
                return True
            
            last_sync_str = state.last_sync_result.get('timestamp')
            if not last_sync_str:
                return True
                
            last_sync = datetime.fromisoformat(last_sync_str)
            time_diff = (datetime.now() - last_sync).total_seconds()
            cooldown_seconds = hours * 3600
            
            if time_diff < cooldown_seconds:
                remaining_hours = (cooldown_seconds - time_diff) / 3600
                self.logger.debug(f"EPIC {epic_id} in cooldown: {remaining_hours:.1f} hours remaining")
                return False
                
            return True
        except Exception as e:
            self.logger.error(f"Error checking cooldown period for EPIC {epic_id}: {e}")
            return True  # Default to allowing extraction on error

    # =====================================
    # Feature Hierarchy Extraction Methods
    # =====================================

    def get_epic_with_features(self, epic_id: str) -> Dict:
        """Get Epic with its Features and Stories hierarchy"""
        try:
            self.logger.info(f"Getting Epic {epic_id} with feature hierarchy")
            
            # Ensure the Epic is being monitored
            if epic_id not in self.monitored_epics:
                self.logger.info(f"Epic {epic_id} not in monitored list, adding it first...")
                self.add_epic(epic_id)
            
            hierarchy = self.agent.ado_client.get_epic_hierarchy(int(epic_id))
            
            if not hierarchy or not hierarchy.get('id'):
                self.logger.error(f"Failed to get hierarchy for Epic {epic_id} - empty or invalid response")
                return {}
            
            # Update the monitored epic state with feature information
            if epic_id in self.monitored_epics:
                epic_state = self.monitored_epics[epic_id]
                epic_state.feature_count = len(hierarchy.get('features', []))
                
                # Calculate total stories
                total_stories = len(hierarchy.get('direct_stories', []))
                for feature in hierarchy.get('features', []):
                    total_stories += len(feature.get('stories', []))
                epic_state.total_story_count = total_stories
                
                # Update feature states
                epic_state.features = []
                for feature in hierarchy.get('features', []):
                    feature_state = FeatureMonitorState(
                        feature_id=str(feature['id']),
                        epic_id=epic_id,
                        title=feature.get('title', ''),
                        last_check=datetime.now(),
                        story_count=len(feature.get('stories', []))
                    )
                    epic_state.features.append(feature_state)
            
            self.logger.info(f"Epic {epic_id}: {len(hierarchy.get('features', []))} features, "
                           f"{len(hierarchy.get('direct_stories', []))} direct stories")
            
            return hierarchy
            
        except Exception as e:
            self.logger.error(f"Error getting Epic {epic_id} with features: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return {}

    def extract_features_from_epic(self, epic_id: str) -> List[Dict]:
        """Extract Features from an Epic and optionally extract Stories from each Feature"""
        try:
            if not self.config.enable_feature_hierarchy:
                self.logger.info("Feature hierarchy disabled, skipping feature extraction")
                return []
            
            self.logger.info(f"🔍 Extracting features from Epic {epic_id}")
            
            # Get features from the Epic
            features = self.agent.ado_client.get_features_from_epic(int(epic_id))
            
            if not features:
                self.logger.info(f"No features found for Epic {epic_id}")
                return []
            
            self.logger.info(f"Found {len(features)} features in Epic {epic_id}")
            
            # Process each feature
            extracted_features = []
            for feature in features:
                feature_id = str(feature['id'])
                feature_title = feature.get('title', 'Unknown')
                
                self.logger.info(f"  📁 Feature {feature_id}: {feature_title}")
                
                # Get stories for this feature if auto extraction is enabled
                stories = []
                if self.config.auto_extract_stories_from_feature:
                    stories = self.extract_stories_from_feature(feature_id, epic_id)
                
                extracted_features.append({
                    'id': feature_id,
                    'title': feature_title,
                    'state': feature.get('state', ''),
                    'epic_id': epic_id,
                    'stories': stories,
                    'story_count': len(stories)
                })
            
            # Update Epic state with feature information
            if epic_id in self.monitored_epics:
                epic_state = self.monitored_epics[epic_id]
                epic_state.feature_count = len(extracted_features)
                
                total_stories = 0
                epic_state.features = []
                for feature in extracted_features:
                    feature_state = FeatureMonitorState(
                        feature_id=feature['id'],
                        epic_id=epic_id,
                        title=feature['title'],
                        last_check=datetime.now(),
                        story_count=feature['story_count']
                    )
                    epic_state.features.append(feature_state)
                    total_stories += feature['story_count']
                
                epic_state.total_story_count = total_stories
                
            self.logger.info(f"✅ Extracted {len(extracted_features)} features from Epic {epic_id}")
            return extracted_features
            
        except Exception as e:
            self.logger.error(f"Error extracting features from Epic {epic_id}: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return []

    def extract_stories_from_feature(self, feature_id: str, epic_id: str) -> List[Dict]:
        """Extract Stories from a Feature"""
        try:
            self.logger.info(f"    📋 Extracting stories from Feature {feature_id}")
            
            # Get stories from the feature
            stories = self.agent.ado_client.get_stories_from_feature(int(feature_id))
            
            if not stories:
                self.logger.info(f"    No stories found for Feature {feature_id}")
                return []
            
            self.logger.info(f"    Found {len(stories)} stories in Feature {feature_id}")
            
            extracted_stories = []
            for story in stories:
                story_data = {
                    'id': str(story['id']),
                    'title': story.get('title', 'Unknown'),
                    'state': story.get('state', ''),
                    'feature_id': feature_id,
                    'epic_id': epic_id
                }
                extracted_stories.append(story_data)
                self.logger.debug(f"      📖 Story {story_data['id']}: {story_data['title']}")
            
            return extracted_stories
            
        except Exception as e:
            self.logger.error(f"Error extracting stories from Feature {feature_id}: {e}")
            return []

    def sync_epic_hierarchy(self, epic_id: str) -> Dict:
        """Synchronize an Epic with its full Feature → Story hierarchy
        
        This is the main method for processing the Epic → Feature → Story flow.
        When an Epic is detected:
        1. Extract all Features from the Epic
        2. For each Feature, extract all Stories
        3. Optionally generate new stories via AI if none exist
        """
        try:
            self.logger.info(f"🚀 Starting hierarchy sync for Epic {epic_id}")
            
            result = {
                'epic_id': epic_id,
                'success': False,
                'features_found': 0,
                'features_processed': [],
                'total_stories_found': 0,
                'new_stories_generated': 0,
                'error_message': None
            }
            
            if not self.config.enable_feature_hierarchy:
                self.logger.info("Feature hierarchy disabled, falling back to standard sync")
                sync_result = self._sync_epic(epic_id)
                result['success'] = sync_result.sync_successful
                return result
            
            # Step 1: Get the Epic hierarchy
            hierarchy = self.get_epic_with_features(epic_id)
            
            if not hierarchy:
                result['error_message'] = f"Failed to get hierarchy for Epic {epic_id}"
                return result
            
            features = hierarchy.get('features', [])
            direct_stories = hierarchy.get('direct_stories', [])
            
            result['features_found'] = len(features)
            result['total_stories_found'] = len(direct_stories)
            
            self.logger.info(f"Epic {epic_id} has {len(features)} features and {len(direct_stories)} direct stories")
            
            # Step 2: Process each Feature
            for feature in features:
                feature_id = str(feature['id'])
                feature_title = feature.get('title', 'Unknown')
                feature_stories = feature.get('stories', [])
                
                self.logger.info(f"📁 Processing Feature {feature_id}: {feature_title} ({len(feature_stories)} stories)")
                
                feature_result = {
                    'id': feature_id,
                    'title': feature_title,
                    'stories_found': len(feature_stories),
                    'new_stories_generated': 0
                }
                
                # If auto extract is enabled and no stories exist, generate them
                if self.config.auto_extract_stories_from_feature and len(feature_stories) == 0:
                    self.logger.info(f"  No stories found for Feature {feature_id}, generating via AI...")
                    try:
                        # Use the agent to generate stories for this feature
                        extraction_result = self.agent.process_requirement_by_id(feature_id, upload_to_ado=True)
                        if extraction_result and hasattr(extraction_result, 'stories'):
                            new_count = len(extraction_result.stories)
                            feature_result['new_stories_generated'] = new_count
                            result['new_stories_generated'] += new_count
                            self.logger.info(f"  ✅ Generated {new_count} stories for Feature {feature_id}")
                    except Exception as e:
                        self.logger.error(f"  ❌ Failed to generate stories for Feature {feature_id}: {e}")
                
                result['features_processed'].append(feature_result)
                result['total_stories_found'] += len(feature_stories)
            
            # Step 3: Mark Epic as processed
            if epic_id in self.monitored_epics:
                self.monitored_epics[epic_id].stories_extracted = True
                self._add_processed_item(epic_id)
                self._save_processed_epics()
            
            result['success'] = True
            self.logger.info(f"✅ Hierarchy sync complete for Epic {epic_id}: "
                           f"{result['features_found']} features, {result['total_stories_found']} stories")
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error syncing Epic {epic_id} hierarchy: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return {
                'epic_id': epic_id,
                'success': False,
                'error_message': str(e)
            }

    def get_hierarchy_status(self) -> Dict:
        """Get the current status of all monitored Epics with their feature hierarchy"""
        status = {
            'total_epics': len(self.monitored_epics),
            'total_features': 0,
            'total_stories': 0,
            'epics': []
        }
        
        for epic_id, state in self.monitored_epics.items():
            epic_data = {
                'id': epic_id,
                'features': [],
                'feature_count': getattr(state, 'feature_count', 0),
                'total_story_count': getattr(state, 'total_story_count', 0),
                'stories_extracted': state.stories_extracted,
                'last_check': state.last_check.isoformat() if state.last_check else None
            }
            
            # Add feature details if available
            if hasattr(state, 'features') and state.features:
                for feature_state in state.features:
                    epic_data['features'].append({
                        'id': feature_state.feature_id,
                        'title': feature_state.title,
                        'story_count': feature_state.story_count,
                        'stories_extracted': feature_state.stories_extracted
                    })
            
            status['total_features'] += epic_data['feature_count']
            status['total_stories'] += epic_data['total_story_count']
            status['epics'].append(epic_data)
        
        return status


def load_config_from_file(config_file: str) -> MonitorConfig:
    """Load monitor configuration from JSON file"""
    try:
        with open(config_file, 'r') as f:
            config_data = json.load(f)
        # Filter out ADO and other non-MonitorConfig settings
        monitor_settings = {k: v for k, v in config_data.items() 
                          if not k.startswith('ado_') and k not in ['openai_api_key']}
        return MonitorConfig(**monitor_settings)
    except Exception as e:
        logging.error(f"Failed to load config from {config_file}: {e}")
        return MonitorConfig()


def save_config_to_file(config: MonitorConfig, config_file: str):
    """Save monitor configuration to JSON file"""
    try:
        # Convert dataclass to dict, excluding None values for cleaner JSON
        config_dict = {k: v for k, v in asdict(config).items() if v is not None}
        with open(config_file, 'w') as f:
            json.dump(config_dict, f, indent=2)
        logging.info(f"Configuration saved to {config_file}")
    except Exception as e:
        logging.error(f"Failed to save config to {config_file}: {e}")


def create_default_config(config_file: str = "config/monitor_config.json"):
    """Create a default configuration file"""
    default_config = MonitorConfig(
        poll_interval_seconds=300,  # 5 minutes
        max_concurrent_syncs=3,
        snapshot_directory="snapshots",
        log_level="INFO",
        epic_ids=["12345", "67890"],  # Example EPIC IDs
        auto_sync=True,
        retry_attempts=3,
        retry_delay_seconds=60,
        auto_extract_new_epics=True,
        skip_duplicate_check=False,
        extraction_cooldown_hours=24,
        enable_content_hash_comparison=True
    )

    with open(config_file, 'w') as f:
        json.dump(asdict(default_config), f, indent=2)

    print(f"Created default configuration file: {config_file}")
    return default_config

if __name__ == "__main__":
    # Set up logging configuration
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO'),
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),  # Log to stdout
            logging.FileHandler('logs/enhanced_epic_monitor.log')  # Also log to file
        ]
    )
    logger = logging.getLogger(__name__)
    
    # Log initial startup information
    logger.info("🚀 Starting Enhanced EPIC Monitor")
    logger.info("=" * 50)
    
    try:
        # Load or create default configuration
        config_file = "monitor_config_enhanced.json"
        logger.info(f"📋 Loading configuration from {config_file}")
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                config_data = json.load(f)
                logger.info("✅ Configuration file found")
                logger.debug(f"Configuration data: {json.dumps(config_data, indent=2)}")
                config = MonitorConfig(**config_data)
                logger.info("✅ Configuration loaded successfully")
        else:
            logger.warning(f"⚠️ Configuration file {config_file} not found, creating default")
            config = create_default_config()

        monitor = EpicChangeMonitor(config)
        monitor.start()
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
        monitor.stop()
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        sys.exit(1)

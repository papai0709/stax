#!/usr/bin/env python3
"""
Enhanced EPIC Change Monitor with support for change-based story extraction.

This enhanced version addresses the limitations of the original monitor by:
1. Supporting change-based story extraction for previously processed EPICs
2. Implementing change significance scoring
3. Adding incremental story extraction capabilities
4. Providing manual override options
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
import hashlib

from src.agent import StoryExtractionAgent
from src.models import EpicSyncResult
from src.monitor import MonitorConfig as BaseMonitorConfig, EpicMonitorState
from src.enhanced_story_creator import EnhancedStoryCreator
from src.models_enhanced import EnhancedUserStory


@dataclass
class EnhancedMonitorConfig(BaseMonitorConfig):
    """Enhanced configuration with change-based extraction options"""
    
    # New options for change-based extraction
    enable_change_based_extraction: bool = True
    change_significance_threshold: float = 0.3  # 0.0 to 1.0, threshold for triggering extraction
    max_changes_per_epic: int = 5  # Maximum number of change-based extractions per EPIC
    incremental_extraction: bool = True  # Extract only new/changed content
    manual_override_enabled: bool = True  # Allow manual re-extraction
    
    # Change detection sensitivity
    title_change_weight: float = 0.8  # How much title changes contribute to significance
    description_change_weight: float = 0.6  # How much description changes contribute  
    state_change_weight: float = 0.2  # How much state changes contribute


@dataclass
class EnhancedEpicState(EpicMonitorState):
    """Enhanced state tracking with change history"""
    
    change_extraction_count: int = 0  # Number of times stories were extracted due to changes
    last_significant_change: Optional[datetime] = None
    change_history: List[Dict] = None  # History of detected changes
    last_change_significance: float = 0.0  # Significance score of last change
    
    def __post_init__(self):
        if self.change_history is None:
            self.change_history = []


class EnhancedEpicChangeMonitor:
    """Enhanced EPIC Change Monitor with change-based story extraction"""
    
    def __init__(self, config: EnhancedMonitorConfig):
        self.config = config
        self.agent = StoryExtractionAgent()
        self.story_creator = EnhancedStoryCreator()  # Add enhanced story creator
        self.logger = self._setup_logger()
        self.is_running = False
        self.monitored_epics: Dict[str, EnhancedEpicState] = {}
        self.executor = ThreadPoolExecutor(max_workers=config.max_concurrent_syncs)
        self.snapshot_dir = Path(config.snapshot_directory)
        self.snapshot_dir.mkdir(exist_ok=True)
        
        # State file to track which epics have been processed
        self.state_file = Path("enhanced_monitor_state.json")
        self.processed_epics = self._load_processed_epics()

        # Load existing snapshots
        self._load_existing_snapshots()
    
    def _setup_logger(self) -> logging.Logger:
        """Setup logging for the enhanced monitor"""
        logger = logging.getLogger("EnhancedEpicChangeMonitor")
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
            log_file = Path("logs") / "enhanced_epic_monitor.log"
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
                'last_updated': datetime.now().isoformat(),
                'change_extraction_stats': {
                    epic_id: {
                        'change_extraction_count': state.change_extraction_count,
                        'last_significant_change': state.last_significant_change.isoformat() if state.last_significant_change else None,
                        'last_change_significance': state.last_change_significance
                    }
                    for epic_id, state in self.monitored_epics.items()
                }
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
        """Load existing snapshots with enhanced state"""
        for epic_id in self.config.epic_ids or []:
            snapshot_file = self.snapshot_dir / f"epic_{epic_id}.json"
            processed_items = self._get_processed_items_for_current_type()
            if snapshot_file.exists():
                try:
                    with open(snapshot_file, 'r') as f:
                        snapshot_data = json.load(f)
                    stories = snapshot_data.get('stories', [])
                    self.monitored_epics[epic_id] = EnhancedEpicState(
                        epic_id=epic_id,
                        last_check=datetime.now(),
                        last_snapshot=snapshot_data,
                        stories_extracted=epic_id in processed_items,
                        extracted_stories=stories,
                        change_extraction_count=0,
                        change_history=[]
                    )
                    self.logger.info(f"Loaded existing snapshot for EPIC {epic_id}")
                except Exception as e:
                    self.logger.error(f"Failed to load snapshot for EPIC {epic_id}: {e}")
                    self.monitored_epics[epic_id] = EnhancedEpicState(
                        epic_id=epic_id,
                        last_check=datetime.now(),
                        stories_extracted=epic_id in processed_items,
                        extracted_stories=[],
                        change_extraction_count=0,
                        change_history=[]
                    )
            else:
                self.monitored_epics[epic_id] = EnhancedEpicState(
                    epic_id=epic_id,
                    last_check=datetime.now(),
                    stories_extracted=epic_id in processed_items,
                    extracted_stories=[],
                    change_extraction_count=0,
                    change_history=[]
                )

    def add_epic(self, epic_id: str) -> bool:
        """Add an EPIC to monitoring and trigger immediate check/sync."""
        try:
            if epic_id not in self.monitored_epics:
                processed_items = self._get_processed_items_for_current_type()
                # Get initial snapshot
                initial_snapshot = self.agent.get_epic_snapshot(epic_id)
                if initial_snapshot:
                    self.monitored_epics[epic_id] = EnhancedEpicState(
                        epic_id=epic_id,
                        last_check=datetime.now(),
                        last_snapshot=initial_snapshot,
                        stories_extracted=epic_id in processed_items,
                        extracted_stories=[],
                        change_extraction_count=0,
                        change_history=[]
                    )
                    self._save_snapshot(epic_id, initial_snapshot)
                    self.logger.info(f"Added EPIC {epic_id} to enhanced monitoring and will check for changes immediately.")
                    # Immediately check and sync the new Epic
                    has_changes, significance = self._check_for_epic_changes_enhanced(epic_id)
                    if has_changes and self._should_extract_stories_enhanced(epic_id, significance):
                        self.logger.info(f"Immediately synchronizing new EPIC {epic_id} after detection.")
                        self._sync_epic_enhanced(epic_id, is_change_based=False)
                    return True
                else:
                    self.monitored_epics[epic_id] = EnhancedEpicState(
                        epic_id=epic_id,
                        last_check=datetime.now(),
                        last_snapshot=None,
                        stories_extracted=epic_id in processed_items,
                        extracted_stories=[],
                        change_extraction_count=0,
                        change_history=[]
                    )
                    self.logger.warning(f"Added EPIC {epic_id} to monitoring, but could not fetch initial snapshot. Will retry.")
                    return False
            else:
                self.logger.warning(f"EPIC {epic_id} is already being monitored")
                return True
        except Exception as e:
            self.logger.error(f"Failed to add EPIC {epic_id} to monitoring: {e}")
            return False

    def calculate_change_significance(self, epic_id: str, current_snapshot: Dict, previous_snapshot: Dict) -> float:
        """Calculate the significance of changes between snapshots"""
        
        if not previous_snapshot:
            return 1.0  # New EPIC is always significant
        
        significance = 0.0
        changes = []
        
        # Check title changes
        current_title = current_snapshot.get('title', '')
        previous_title = previous_snapshot.get('title', '')
        if current_title != previous_title:
            # Calculate similarity using simple metric (could be enhanced with fuzzy matching)
            title_similarity = self._calculate_text_similarity(current_title, previous_title)
            title_significance = (1.0 - title_similarity) * self.config.title_change_weight
            significance += title_significance
            changes.append({
                'type': 'title',
                'significance': title_significance,
                'old_value': previous_title,
                'new_value': current_title
            })
        
        # Check description changes  
        current_desc = current_snapshot.get('description', '')
        previous_desc = previous_snapshot.get('description', '')
        if current_desc != previous_desc:
            desc_similarity = self._calculate_text_similarity(current_desc, previous_desc)
            desc_significance = (1.0 - desc_similarity) * self.config.description_change_weight
            significance += desc_significance
            changes.append({
                'type': 'description',
                'significance': desc_significance,
                'old_length': len(previous_desc),
                'new_length': len(current_desc)
            })
        
        # Check state changes
        current_state = current_snapshot.get('state', '')
        previous_state = previous_snapshot.get('state', '')
        if current_state != previous_state:
            state_significance = self.config.state_change_weight
            significance += state_significance
            changes.append({
                'type': 'state',
                'significance': state_significance,
                'old_value': previous_state,
                'new_value': current_state
            })
        
        # Store change details
        if changes:
            change_record = {
                'timestamp': datetime.now().isoformat(),
                'total_significance': significance,
                'changes': changes
            }
            
            epic_state = self.monitored_epics.get(epic_id)
            if epic_state:
                epic_state.change_history.append(change_record)
                epic_state.last_change_significance = significance
                # Keep only last 20 changes
                if len(epic_state.change_history) > 20:
                    epic_state.change_history = epic_state.change_history[-20:]
        
        return min(significance, 1.0)  # Cap at 1.0
    
    def _calculate_text_similarity(self, text1: str, text2: str) -> float:
        """Calculate similarity between two texts (simple implementation)"""
        if not text1 and not text2:
            return 1.0
        if not text1 or not text2:
            return 0.0
        
        # Simple word-based similarity
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        
        if not words1 and not words2:
            return 1.0
        if not words1 or not words2:
            return 0.0
        
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        return len(intersection) / len(union) if union else 0.0

    def _should_extract_stories_enhanced(self, epic_id: str, change_significance: float = 0.0) -> bool:
        """Enhanced logic to determine if stories should be extracted"""
        
        state = self.monitored_epics.get(epic_id)
        if not state:
            return False
        
        # Check if change-based extraction is enabled
        if not self.config.enable_change_based_extraction:
            # Use original logic
            if state.stories_extracted:
                self.logger.info(f"Stories already extracted for EPIC {epic_id}, skipping extraction (change-based extraction disabled)")
                return False
            
            processed_items = self._get_processed_items_for_current_type()
            if self.config.skip_duplicate_check or epic_id not in processed_items:
                return True
            else:
                return False
        
        # Enhanced logic with change-based extraction
        processed_items = self._get_processed_items_for_current_type()
        
        # For new EPICs (never processed)
        if not state.stories_extracted and epic_id not in processed_items:
            self.logger.info(f"EPIC {epic_id} is new, proceeding with initial story extraction")
            return True
        
        # For EPICs with existing stories, check if change is significant enough
        if state.stories_extracted:
            # Check extraction limits
            if state.change_extraction_count >= self.config.max_changes_per_epic:
                self.logger.info(f"EPIC {epic_id} has reached maximum change extractions ({self.config.max_changes_per_epic}), skipping")
                return False
            
            # Check change significance
            if change_significance >= self.config.change_significance_threshold:
                self.logger.info(f"EPIC {epic_id} has significant changes (significance: {change_significance:.2f}, threshold: {self.config.change_significance_threshold}), proceeding with change-based extraction")
                return True
            else:
                self.logger.info(f"EPIC {epic_id} changes not significant enough (significance: {change_significance:.2f}, threshold: {self.config.change_significance_threshold}), skipping extraction")
                return False
        
        # Fallback to original logic
        return self.config.skip_duplicate_check or epic_id not in processed_items

    def _check_for_epic_changes_enhanced(self, epic_id: str) -> tuple[bool, float]:
        """Enhanced change detection with significance scoring"""
        
        try:
            epic_state = self.monitored_epics.get(epic_id)
            if not epic_state:
                return False, 0.0

            # Get current snapshot of the EPIC
            current_snapshot = self.agent.get_epic_snapshot(epic_id)
            if not current_snapshot:
                self.logger.warning(f"Could not get current snapshot for EPIC {epic_id}")
                return False, 0.0

            # If we have no previous snapshot, this is a change (new EPIC)
            if not epic_state.last_snapshot:
                self.logger.info(f"EPIC {epic_id} - No previous snapshot, treating as changed")
                epic_state.last_snapshot = current_snapshot
                self._save_snapshot(epic_id, current_snapshot)
                return True, 1.0

            # Calculate change significance
            previous_snapshot = epic_state.last_snapshot
            significance = self.calculate_change_significance(epic_id, current_snapshot, previous_snapshot)
            
            has_changes = significance > 0.0
            
            if has_changes:
                self.logger.info(f"EPIC {epic_id} - Changes detected with significance: {significance:.2f}")
                
                # Update stored snapshot
                epic_state.last_snapshot = current_snapshot
                epic_state.last_significant_change = datetime.now()
                self._save_snapshot(epic_id, current_snapshot)
                
                return True, significance
            else:
                self.logger.debug(f"EPIC {epic_id} - No changes detected")
                return False, 0.0

        except Exception as e:
            self.logger.error(f"Error checking changes for EPIC {epic_id}: {e}")
            return False, 0.0

    def _sync_epic_enhanced(self, epic_id: str, is_change_based: bool = False) -> EpicSyncResult:
        """Enhanced synchronization with change-based tracking"""
        
        epic_state = self.monitored_epics[epic_id]
        
        for attempt in range(self.config.retry_attempts):
            try:
                self.logger.info(f"Synchronizing EPIC {epic_id} ({'change-based' if is_change_based else 'initial'}) (attempt {attempt + 1})")
                
                # Use incremental extraction for change-based sync if configured
                if is_change_based and self.config.incremental_extraction:
                    result = self._perform_incremental_sync(epic_id, epic_state)
                else:
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
                    
                    # Mark epic as processed and update change tracking
                    if len(result.created_stories) > 0 or len(result.updated_stories) > 0:
                        self._add_processed_item(epic_id)
                        epic_state.stories_extracted = True
                        
                        # Update change-based extraction count
                        if is_change_based:
                            epic_state.change_extraction_count += 1
                        
                        self._save_processed_epics()
                    
                    # Store sync result
                    epic_state.last_sync_result = {
                        'timestamp': datetime.now().isoformat(),
                        'success': True,
                        'created_stories': result.created_stories,
                        'updated_stories': result.updated_stories,
                        'unchanged_stories': result.unchanged_stories,
                        'is_change_based': is_change_based,
                        'change_extraction_count': epic_state.change_extraction_count
                    }
                    
                    sync_type = "change-based" if is_change_based else "initial"
                    self.logger.info(f"Successfully synchronized EPIC {epic_id} ({sync_type})")
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
            'error': f"Failed after {self.config.retry_attempts} attempts",
            'is_change_based': is_change_based
        }
        
        return EpicSyncResult(
            epic_id=epic_id,
            epic_title="",
            sync_successful=False,
            error_message=f"Failed after {self.config.retry_attempts} attempts"
        )
    
    def _perform_incremental_sync(self, epic_id: str, epic_state: EnhancedEpicState) -> EpicSyncResult:
        """Perform incremental synchronization focusing on changed content"""
        
        self.logger.info(f"Performing incremental sync for EPIC {epic_id}")
        
        # For now, use the standard sync but log that it's incremental
        # This can be enhanced to use AI to identify only changed sections
        result = self.agent.synchronize_epic(
            epic_id=epic_id,
            stored_snapshot=epic_state.last_snapshot
        )
        
        # Future enhancement: Use AI to compare old vs new content and extract only new stories
        # For now, the agent's existing logic handles duplicate prevention
        
        return result

    def force_re_extraction(self, epic_id: str) -> bool:
        """Manually force re-extraction of stories for an EPIC"""
        
        if not self.config.manual_override_enabled:
            self.logger.warning(f"Manual override disabled, cannot force re-extraction for EPIC {epic_id}")
            return False
        
        epic_state = self.monitored_epics.get(epic_id)
        if not epic_state:
            self.logger.error(f"EPIC {epic_id} not found in monitored EPICs")
            return False
        
        self.logger.info(f"Forcing re-extraction for EPIC {epic_id} (manual override)")
        
        try:
            # Perform synchronization with change-based flag
            result = self._sync_epic_enhanced(epic_id, is_change_based=True)
            
            if result.sync_successful:
                self.logger.info(f"Manual re-extraction successful for EPIC {epic_id}")
                return True
            else:
                self.logger.error(f"Manual re-extraction failed for EPIC {epic_id}: {result.error_message}")
                return False
                
        except Exception as e:
            self.logger.error(f"Exception during manual re-extraction for EPIC {epic_id}: {e}")
            return False

    def get_change_statistics(self, epic_id: Optional[str] = None) -> Dict:
        """Get statistics about change-based extractions"""
        
        if epic_id:
            # Statistics for specific EPIC
            epic_state = self.monitored_epics.get(epic_id)
            if not epic_state:
                return {}
            
            return {
                'epic_id': epic_id,
                'change_extraction_count': epic_state.change_extraction_count,
                'last_significant_change': epic_state.last_significant_change.isoformat() if epic_state.last_significant_change else None,
                'last_change_significance': epic_state.last_change_significance,
                'change_history_count': len(epic_state.change_history),
                'recent_changes': epic_state.change_history[-5:] if epic_state.change_history else []
            }
        else:
            # Overall statistics
            total_change_extractions = sum(state.change_extraction_count for state in self.monitored_epics.values())
            epics_with_changes = len([state for state in self.monitored_epics.values() if state.change_extraction_count > 0])
            
            return {
                'total_monitored_epics': len(self.monitored_epics),
                'total_change_extractions': total_change_extractions,
                'epics_with_changes': epics_with_changes,
                'average_changes_per_epic': total_change_extractions / len(self.monitored_epics) if self.monitored_epics else 0,
                'configuration': {
                    'change_based_extraction_enabled': self.config.enable_change_based_extraction,
                    'significance_threshold': self.config.change_significance_threshold,
                    'max_changes_per_epic': self.config.max_changes_per_epic,
                    'incremental_extraction_enabled': self.config.incremental_extraction
                }
            }

    def _save_snapshot(self, epic_id: str, snapshot_data: Dict):
        """Save snapshot for an epic with enhanced metadata"""
        snapshot_file = self.snapshot_dir / f"epic_{epic_id}.json"
        try:
            # Add metadata to snapshot
            enhanced_snapshot = {
                **snapshot_data,
                'enhanced_metadata': {
                    'last_updated': datetime.now().isoformat(),
                    'monitor_version': 'enhanced_v1.0'
                }
            }
            
            with open(snapshot_file, 'w') as f:
                json.dump(enhanced_snapshot, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save snapshot for EPIC {epic_id}: {e}")

    # Method to check changes and potentially extract stories  
    def check_and_extract_if_changed(self, epic_id: str) -> Dict:
        """Check for changes and extract stories if significant changes detected"""
        
        try:
            has_changes, significance = self._check_for_epic_changes_enhanced(epic_id)
            
            result = {
                'epic_id': epic_id,
                'has_changes': has_changes,
                'change_significance': significance,
                'stories_extracted': False,
                'extraction_result': None
            }
            
            if has_changes:
                should_extract = self._should_extract_stories_enhanced(epic_id, significance)
                
                if should_extract:
                    self.logger.info(f"Extracting stories for EPIC {epic_id} due to significant changes")
                    
                    sync_result = self._sync_epic_enhanced(epic_id, is_change_based=True)
                    
                    result['stories_extracted'] = sync_result.sync_successful
                    result['extraction_result'] = {
                        'success': sync_result.sync_successful,
                        'created_stories': sync_result.created_stories,
                        'updated_stories': sync_result.updated_stories,
                        'error_message': sync_result.error_message
                    }
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error in check_and_extract_if_changed for EPIC {epic_id}: {e}")
            return {
                'epic_id': epic_id,
                'error': str(e)
            }

# Factory function for creating enhanced monitor
def create_enhanced_monitor(
    enable_change_based_extraction: bool = True,
    change_significance_threshold: float = 0.3,
    max_changes_per_epic: int = 5,
    incremental_extraction: bool = True,
    **kwargs
) -> EnhancedEpicChangeMonitor:
    """Factory function to create an enhanced monitor with sensible defaults"""
    
    # Load base configuration
    base_config_file = kwargs.get('config_file', 'config/monitor_config.json')
    try:
        with open(base_config_file, 'r') as f:
            base_config_data = json.load(f)
    except:
        base_config_data = {}
    
    # Merge with enhanced options
    enhanced_config_data = {
        **base_config_data,
        'enable_change_based_extraction': enable_change_based_extraction,
        'change_significance_threshold': change_significance_threshold,
        'max_changes_per_epic': max_changes_per_epic,
        'incremental_extraction': incremental_extraction,
        **kwargs
    }
    
    config = EnhancedMonitorConfig(**enhanced_config_data)
    return EnhancedEpicChangeMonitor(config)

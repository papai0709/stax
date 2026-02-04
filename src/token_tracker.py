"""
Token Usage Tracker for AI Calls
Provides token usage analytics and TOON optimization metrics without additional API calls
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from collections import deque

logger = logging.getLogger(__name__)


@dataclass
class TokenUsageRecord:
    """Record of token usage for a single AI call"""
    timestamp: str
    call_type: str  # 'story_extraction', 'test_case_extraction', etc.
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    toon_enabled: bool
    estimated_standard_tokens: int  # Estimated tokens if TOON was not used
    tokens_saved: int  # Difference when TOON is enabled
    reduction_percentage: float
    model: str
    provider: str  # 'OPENAI', 'AZURE_OPENAI', 'GITHUB'
    success: bool
    error_message: str = ""
    story_id: str = ""
    story_title: str = ""
    

@dataclass
class TokenUsageStats:
    """Aggregated token usage statistics"""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_tokens_saved: int = 0
    average_reduction_percentage: float = 0.0
    estimated_cost_usd: float = 0.0
    estimated_savings_usd: float = 0.0
    calls_with_toon: int = 0
    calls_without_toon: int = 0
    story_extractions: int = 0
    test_case_extractions: int = 0
    # Estimated tokens if TOON was NOT used (for comparison)
    estimated_tokens_without_toon: int = 0
    estimated_cost_without_toon_usd: float = 0.0


class TokenTracker:
    """
    Singleton class to track token usage across AI calls.
    Provides analytics for the Token Dashboard without making additional API calls.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    # Token cost estimates (per 1K tokens) - Updated pricing
    TOKEN_COSTS = {
        'gpt-4': {'input': 0.03, 'output': 0.06},
        'gpt-4-turbo': {'input': 0.01, 'output': 0.03},
        'gpt-4o': {'input': 0.005, 'output': 0.015},
        'gpt-4o-mini': {'input': 0.00015, 'output': 0.0006},
        'gpt-3.5-turbo': {'input': 0.0005, 'output': 0.0015},
        'gpt-35-turbo': {'input': 0.0005, 'output': 0.0015},  # Azure naming
    }
    
    # TOON token reduction factor (based on analysis: ~57% reduction)
    TOON_REDUCTION_FACTOR = 0.571
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self.logger = logging.getLogger(__name__)
        self.records: deque = deque(maxlen=1000)  # Keep last 1000 records
        self.stats = TokenUsageStats()
        self.data_file = Path("logs/token_usage.json")
        self.data_file.parent.mkdir(exist_ok=True)
        
        # Load existing data if available
        self._load_data()
        self._initialized = True
        self.logger.info("TokenTracker initialized")
    
    def _load_data(self):
        """Load token usage data from file"""
        try:
            if self.data_file.exists():
                with open(self.data_file, 'r') as f:
                    data = json.load(f)
                    
                # Load records
                for record_data in data.get('records', [])[-1000:]:  # Keep last 1000
                    record = TokenUsageRecord(**record_data)
                    self.records.append(record)
                
                # Load stats
                if 'stats' in data:
                    self.stats = TokenUsageStats(**data['stats'])
                    
                self.logger.info(f"Loaded {len(self.records)} token usage records")
        except Exception as e:
            self.logger.error(f"Failed to load token usage data: {e}")
    
    def _save_data(self):
        """Save token usage data to file"""
        try:
            data = {
                'records': [asdict(r) for r in list(self.records)],
                'stats': asdict(self.stats),
                'last_updated': datetime.now().isoformat()
            }
            with open(self.data_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save token usage data: {e}")
    
    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for a given text.
        Uses a simple heuristic: ~4 characters per token for English text.
        This avoids the need for tiktoken dependency.
        """
        if not text:
            return 0
        # Average ~4 characters per token for English text
        # Adjust for code/JSON which tends to have more tokens per character
        char_count = len(text)
        if '{' in text or '[' in text:  # JSON-like content
            return max(1, char_count // 3)
        return max(1, char_count // 4)
    
    def record_usage(
        self,
        call_type: str,
        prompt_text: str,
        response_text: str,
        toon_enabled: bool,
        model: str,
        provider: str,
        success: bool = True,
        error_message: str = "",
        story_id: str = "",
        story_title: str = ""
    ) -> TokenUsageRecord:
        """
        Record token usage for an AI call.
        Estimates tokens from text without making additional API calls.
        """
        with self._lock:
            # Estimate token counts
            prompt_tokens = self.estimate_tokens(prompt_text)
            completion_tokens = self.estimate_tokens(response_text)
            total_tokens = prompt_tokens + completion_tokens
            
            # Calculate TOON savings
            if toon_enabled:
                # When TOON is enabled, estimate what standard would have used
                estimated_standard_tokens = int(prompt_tokens / (1 - self.TOON_REDUCTION_FACTOR))
                tokens_saved = estimated_standard_tokens - prompt_tokens
                reduction_percentage = self.TOON_REDUCTION_FACTOR * 100
            else:
                # When TOON is disabled, estimate what TOON would have saved
                estimated_standard_tokens = prompt_tokens
                potential_toon_tokens = int(prompt_tokens * (1 - self.TOON_REDUCTION_FACTOR))
                tokens_saved = 0  # No savings since TOON wasn't used
                reduction_percentage = 0.0
            
            # Create record
            record = TokenUsageRecord(
                timestamp=datetime.now().isoformat(),
                call_type=call_type,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                toon_enabled=toon_enabled,
                estimated_standard_tokens=estimated_standard_tokens,
                tokens_saved=tokens_saved,
                reduction_percentage=reduction_percentage,
                model=model,
                provider=provider,
                success=success,
                error_message=error_message,
                story_id=story_id,
                story_title=story_title
            )
            
            # Add to records
            self.records.append(record)
            
            # Update stats
            self._update_stats(record)
            
            # Save data periodically (every 10 records)
            if len(self.records) % 10 == 0:
                self._save_data()
            
            self.logger.debug(f"Recorded token usage: {total_tokens} tokens, TOON: {toon_enabled}")
            return record
    
    def _update_stats(self, record: TokenUsageRecord):
        """Update aggregated statistics with a new record"""
        self.stats.total_calls += 1
        
        if record.success:
            self.stats.successful_calls += 1
        else:
            self.stats.failed_calls += 1
        
        self.stats.total_prompt_tokens += record.prompt_tokens
        self.stats.total_completion_tokens += record.completion_tokens
        self.stats.total_tokens += record.total_tokens
        self.stats.total_tokens_saved += record.tokens_saved
        
        # Track estimated tokens WITHOUT TOON for comparison
        # This shows what we would have used if TOON was not enabled
        self.stats.estimated_tokens_without_toon += record.estimated_standard_tokens + record.completion_tokens
        
        if record.toon_enabled:
            self.stats.calls_with_toon += 1
        else:
            self.stats.calls_without_toon += 1
        
        if record.call_type == 'story_extraction':
            self.stats.story_extractions += 1
        elif record.call_type == 'test_case_extraction':
            self.stats.test_case_extractions += 1
        
        # Update average reduction percentage
        if self.stats.calls_with_toon > 0:
            toon_records = [r for r in self.records if r.toon_enabled]
            if toon_records:
                self.stats.average_reduction_percentage = sum(
                    r.reduction_percentage for r in toon_records
                ) / len(toon_records)
        
        # Estimate costs
        self._update_cost_estimates(record)
    
    def _update_cost_estimates(self, record: TokenUsageRecord):
        """Update cost estimates based on model pricing"""
        model_lower = record.model.lower()
        
        # Find matching cost tier
        cost_tier = None
        for tier_name, costs in self.TOKEN_COSTS.items():
            if tier_name in model_lower:
                cost_tier = costs
                break
        
        if not cost_tier:
            # Default to GPT-4 pricing for unknown models
            cost_tier = self.TOKEN_COSTS['gpt-4']
        
        # Calculate actual cost (with TOON)
        input_cost = (record.prompt_tokens / 1000) * cost_tier['input']
        output_cost = (record.completion_tokens / 1000) * cost_tier['output']
        actual_cost = input_cost + output_cost
        
        self.stats.estimated_cost_usd += actual_cost
        
        # Calculate what cost would have been WITHOUT TOON
        input_cost_without_toon = (record.estimated_standard_tokens / 1000) * cost_tier['input']
        cost_without_toon = input_cost_without_toon + output_cost
        self.stats.estimated_cost_without_toon_usd += cost_without_toon
        
        # Calculate savings (if TOON was used)
        if record.toon_enabled and record.tokens_saved > 0:
            saved_input_cost = (record.tokens_saved / 1000) * cost_tier['input']
            self.stats.estimated_savings_usd += saved_input_cost
    
    def get_stats(self) -> Dict:
        """Get current token usage statistics"""
        with self._lock:
            return asdict(self.stats)
    
    def get_recent_records(self, limit: int = 50) -> List[Dict]:
        """Get recent token usage records"""
        with self._lock:
            recent = list(self.records)[-limit:]
            return [asdict(r) for r in reversed(recent)]
    
    def get_dashboard_data(self) -> Dict:
        """Get comprehensive data for the token dashboard"""
        with self._lock:
            # Get records from last 24 hours
            now = datetime.now()
            recent_records = []
            for record in self.records:
                try:
                    record_time = datetime.fromisoformat(record.timestamp)
                    hours_ago = (now - record_time).total_seconds() / 3600
                    if hours_ago <= 24:
                        recent_records.append(record)
                except:
                    pass
            
            # Calculate hourly breakdown
            hourly_usage = {}
            for record in recent_records:
                hour = record.timestamp[:13]  # YYYY-MM-DDTHH
                if hour not in hourly_usage:
                    hourly_usage[hour] = {
                        'tokens': 0,
                        'saved': 0,
                        'calls': 0
                    }
                hourly_usage[hour]['tokens'] += record.total_tokens
                hourly_usage[hour]['saved'] += record.tokens_saved
                hourly_usage[hour]['calls'] += 1
            
            # Calculate by call type
            by_call_type = {}
            for record in list(self.records):
                if record.call_type not in by_call_type:
                    by_call_type[record.call_type] = {
                        'total_calls': 0,
                        'total_tokens': 0,
                        'tokens_saved': 0,
                        'avg_tokens': 0
                    }
                by_call_type[record.call_type]['total_calls'] += 1
                by_call_type[record.call_type]['total_tokens'] += record.total_tokens
                by_call_type[record.call_type]['tokens_saved'] += record.tokens_saved
            
            # Calculate averages
            for call_type in by_call_type:
                if by_call_type[call_type]['total_calls'] > 0:
                    by_call_type[call_type]['avg_tokens'] = (
                        by_call_type[call_type]['total_tokens'] / 
                        by_call_type[call_type]['total_calls']
                    )
            
            # TOON effectiveness
            toon_stats = {
                'enabled_calls': self.stats.calls_with_toon,
                'disabled_calls': self.stats.calls_without_toon,
                'total_tokens_saved': self.stats.total_tokens_saved,
                'average_reduction': self.stats.average_reduction_percentage,
                'estimated_savings_usd': round(self.stats.estimated_savings_usd, 4)
            }
            
            return {
                'stats': asdict(self.stats),
                'recent_records': [asdict(r) for r in list(recent_records)[-20:]],
                'hourly_usage': hourly_usage,
                'by_call_type': by_call_type,
                'toon_stats': toon_stats,
                'toon_enabled': self.stats.calls_with_toon > 0,
                'last_updated': datetime.now().isoformat()
            }
    
    def clear_data(self):
        """Clear all token usage data"""
        with self._lock:
            self.records.clear()
            self.stats = TokenUsageStats()
            self._save_data()
            self.logger.info("Token usage data cleared")
    
    def force_save(self):
        """Force save current data to file"""
        with self._lock:
            self._save_data()


# Global instance accessor
def get_token_tracker() -> TokenTracker:
    """Get the singleton TokenTracker instance"""
    return TokenTracker()

"""
AI Client Factory for OpenAI and Azure OpenAI Service
Provides unified interface for both AI services with automatic provider switching
Includes token usage tracking for the Token Dashboard
"""

from openai import OpenAI, AzureOpenAI
from config.settings import Settings
import time
import logging

logger = logging.getLogger(__name__)

# Token tracking is initialized lazily to avoid circular imports
_token_tracker = None

def _get_tracker():
    """Lazy initialization of token tracker"""
    global _token_tracker
    if _token_tracker is None:
        try:
            from src.token_tracker import get_token_tracker
            _token_tracker = get_token_tracker()
        except Exception as e:
            logger.warning(f"Token tracker not available: {e}")
    return _token_tracker

class AIClientFactory:
    """Factory class for creating AI clients with provider abstraction"""
    
    @staticmethod
    def create_client():
        """Create appropriate AI client based on configuration"""
        provider = Settings.AI_SERVICE_PROVIDER
        logger.info(f"🤖 AI Client Factory: Creating client for provider '{provider}'")
        
        if provider == 'AZURE_OPENAI':
            logger.info(f"🔷 Initializing Azure OpenAI Service client")
            return AzureOpenAIClient()
        elif provider == 'GITHUB':
            logger.info(f"🐙 Initializing GitHub Models client")
            return GitHubModelsClient()
        else:
            logger.info(f"🔶 Initializing OpenAI client")
            return OpenAIClient()

class BaseAIClient:
    """Base class for AI clients"""
    
    def __init__(self):
        self.max_retries = Settings.OPENAI_MAX_RETRIES
        self.retry_delay = Settings.OPENAI_RETRY_DELAY
        self.provider_name = "UNKNOWN"
        self.model_name = "unknown"
    
    def chat_completion(self, messages, temperature=0.7, max_tokens=2000):
        """Abstract method for chat completion"""
        raise NotImplementedError
    
    def track_usage(self, messages, response_text, call_type="general", 
                    toon_enabled=True, success=True, error_message="",
                    story_id="", story_title=""):
        """
        Track token usage for an AI call.
        This is called after each successful chat_completion.
        """
        tracker = _get_tracker()
        if tracker is None:
            return
        
        try:
            # Build prompt text from messages
            prompt_text = ""
            for msg in messages:
                prompt_text += f"{msg.get('role', '')}: {msg.get('content', '')}\n"
            
            tracker.record_usage(
                call_type=call_type,
                prompt_text=prompt_text,
                response_text=response_text,
                toon_enabled=toon_enabled,
                model=self.model_name,
                provider=self.provider_name,
                success=success,
                error_message=error_message,
                story_id=story_id,
                story_title=story_title
            )
        except Exception as e:
            logger.warning(f"Failed to track token usage: {e}")
    
    def _retry_request(self, func, *args, **kwargs):
        """Helper method for retrying requests with exponential backoff"""
        last_exception = None
        
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (2 ** attempt)
                    logger.warning(f"AI request failed (attempt {attempt + 1}/{self.max_retries}), retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                else:
                    logger.error(f"AI request failed after {self.max_retries} attempts: {e}")
        
        raise last_exception

class OpenAIClient(BaseAIClient):
    """Client for standard OpenAI API"""
    
    def __init__(self):
        super().__init__()
        self.client = OpenAI(api_key=Settings.OPENAI_API_KEY)
        self.model = Settings.OPENAI_MODEL
        self.provider_name = "OPENAI"
        self.model_name = self.model
        logger.info(f"Initialized OpenAI client with model: {self.model}")
    
    def chat_completion(self, messages, temperature=0.7, max_tokens=2000):
        """Make chat completion request to OpenAI"""
        def _make_request():
            logger.info(f"🔶 OpenAI: Making chat completion request with model '{self.model}'")
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens
            )
            
            result = response.choices[0].message.content.strip()
            logger.info(f"🔶 OpenAI: Request completed successfully, response length: {len(result)} characters")
            return result
        
        return self._retry_request(_make_request)

class AzureOpenAIClient(BaseAIClient):
    """Client for Azure OpenAI Service"""
    
    def __init__(self):
        super().__init__()
        self.client = AzureOpenAI(
            api_key=Settings.AZURE_OPENAI_API_KEY,
            api_version=Settings.AZURE_OPENAI_API_VERSION,
            azure_endpoint=Settings.AZURE_OPENAI_ENDPOINT
        )
        self.deployment_name = Settings.AZURE_OPENAI_DEPLOYMENT_NAME
        self.model = Settings.AZURE_OPENAI_MODEL
        self.provider_name = "AZURE_OPENAI"
        self.model_name = self.model
        logger.info(f"Initialized Azure OpenAI client with deployment: {self.deployment_name}")
    
    def chat_completion(self, messages, temperature=0.7, max_tokens=2000):
        """Make chat completion request to Azure OpenAI"""
        def _make_request():
            logger.info(f"🔷 Azure OpenAI: Making chat completion request to deployment '{self.deployment_name}'")
            logger.debug(f"🔷 Azure OpenAI: Endpoint={Settings.AZURE_OPENAI_ENDPOINT}, API Version={Settings.AZURE_OPENAI_API_VERSION}")
            
            response = self.client.chat.completions.create(
                model=self.deployment_name,  # Use deployment name as model for Azure
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens
            )
            
            result = response.choices[0].message.content.strip()
            logger.info(f"🔷 Azure OpenAI: Request completed successfully, response length: {len(result)} characters")
            return result
        
        return self._retry_request(_make_request)

# Convenience function for backward compatibility
def get_ai_client():
    """Get AI client instance based on current configuration"""
    return AIClientFactory.create_client()

class GitHubModelsClient(BaseAIClient):
    """Client for GitHub Models (free tier with GitHub PAT)"""
    
    def __init__(self):
        super().__init__()
        self.client = OpenAI(
            base_url=Settings.GITHUB_API_BASE,
            api_key=Settings.GITHUB_TOKEN
        )
        self.model = Settings.GITHUB_MODEL
        self.provider_name = "GITHUB"
        self.model_name = self.model
        logger.info(f"Initialized GitHub Models client with model: {self.model}")
        logger.info(f"Using endpoint: {Settings.GITHUB_API_BASE}")
    
    def chat_completion(self, messages, temperature=0.7, max_tokens=2000):
        """Make chat completion request to GitHub Models"""
        def _make_request():
            logger.info(f"🐙 GitHub Models: Making chat completion request with model '{self.model}'")
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens
            )
            
            result = response.choices[0].message.content.strip()
            logger.info(f"🐙 GitHub Models: Request completed successfully, response length: {len(result)} characters")
            return result
        
        return self._retry_request(_make_request)

# Legacy compatibility functions
def create_openai_client():
    """Legacy function for OpenAI client creation"""
    return OpenAIClient()

def create_azure_openai_client():
    """Legacy function for Azure OpenAI client creation"""
    return AzureOpenAIClient()

def create_github_models_client():
    """Legacy function for GitHub Models client creation"""
    return GitHubModelsClient()

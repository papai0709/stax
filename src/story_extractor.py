import json
import re
import time
import logging
from typing import List

from config.settings import Settings
from src.models import Requirement, StoryExtractionResult, UserStory
from src.models_enhanced import EnhancedUserStory
from src.enhanced_story_creator import EnhancedStoryCreator
from src.ai_client import get_ai_client

class StoryExtractor:
    """AI-powered extractor that analyzes requirements and creates enhanced user stories"""
    
    def __init__(self):
        Settings.validate()
        self.ai_client = get_ai_client()
        self.story_creator = EnhancedStoryCreator()
        self.logger = logging.getLogger("StoryExtractor")
        self.logger.setLevel(logging.DEBUG)
        
        # Log which AI service is being used
        ai_provider = getattr(Settings, 'AI_SERVICE_PROVIDER', 'OPENAI')
        self.logger.info(f"🤖 StoryExtractor: Initialized with AI provider '{ai_provider}'")
        if ai_provider == 'AZURE_OPENAI':
            deployment = getattr(Settings, 'AZURE_OPENAI_DEPLOYMENT_NAME', 'Unknown')
            self.logger.info(f"🔷 StoryExtractor: Using Azure OpenAI deployment '{deployment}'")
        else:
            model = getattr(Settings, 'OPENAI_MODEL', 'Unknown')
            self.logger.info(f"🔶 StoryExtractor: Using OpenAI model '{model}'")
    
    def extract_stories(self, requirement: Requirement, existing_stories: List[dict] = None) -> StoryExtractionResult:
        """Extract enhanced user stories from a requirement using AI, avoiding duplicates"""
        self.logger.info(f"Starting story extraction for requirement: {requirement.id}")
        self.logger.debug(f"Requirement details: {json.dumps(requirement.__dict__, indent=2)}")
        
        try:
            # Enhanced requirement analysis
            requirement_context = self._analyze_requirement_context(requirement)
            self.logger.debug(f"Requirement context: {requirement_context}")
            
            # Get domain-specific guidelines
            domain_guidelines = self._get_domain_guidelines(requirement_context.get('domain', 'general'))
            
            # Analyze stakeholders and user personas
            stakeholders = self._identify_stakeholders(requirement)
            self.logger.debug(f"Identified stakeholders: {stakeholders}")
            
            self.logger.debug("Analyzing requirement with AI...")
            stories = self._analyze_requirement_with_ai(requirement, requirement_context, domain_guidelines, stakeholders)
            self.logger.info(f"Found {len(stories)} potential stories")
            
            # Enhanced story validation and refinement
            stories = self._refine_and_validate_stories(stories, requirement_context)
            
            # Filter out duplicates and convert EnhancedUserStory to UserStory
            filtered_stories = self._filter_duplicate_stories(stories, existing_stories or [])
            
            # Add story prioritization and dependencies
            prioritized_stories = self._prioritize_stories(filtered_stories, requirement_context)
            
            return StoryExtractionResult(
                requirement_id=str(requirement.id),
                requirement_title=requirement.title,
                stories=prioritized_stories,
                extraction_successful=True
            )
        except Exception as e:
            self.logger.error(f"Story extraction failed: {str(e)}")
            return StoryExtractionResult(
                requirement_id=str(requirement.id),
                requirement_title=requirement.title,
                stories=[],
                extraction_successful=False,
                error_message=str(e)
            )
    
    def _analyze_requirement_with_ai(self, requirement: Requirement, context: dict = None, domain_guidelines: dict = None, stakeholders: List[str] = None) -> List[EnhancedUserStory]:
        """Use AI to analyze requirement and extract enhanced user stories with context awareness"""
        
        prompt = self._build_extraction_prompt(requirement, context, domain_guidelines, stakeholders)
        
        try:
            # Build messages for AI call
            messages = [
                {
                    "role": "system",
                    "content": self._get_enhanced_system_prompt(context, domain_guidelines)
                },
                {
                    "role": "user", 
                    "content": prompt
                }
            ]
            
            # Use the unified AI client for chat completion with enhanced system prompt
            content = self.ai_client.chat_completion(
                messages=messages,
                temperature=0.3,
                max_tokens=3000  # Increased token limit for more detailed stories
            )
            
            # Log the raw AI response for debugging
            self.logger.debug(f"Raw AI response: {repr(content)}")
            self.logger.debug(f"AI response length: {len(content)} characters")
            
            # Check if response is empty
            if not content or not content.strip():
                raise Exception("AI returned empty response")
            
            # Track token usage for the dashboard
            self.ai_client.track_usage(
                messages=messages,
                response_text=content,
                call_type="story_extraction",
                toon_enabled=False,  # Story extraction doesn't use TOON yet
                success=True,
                story_id=str(requirement.id),
                story_title=requirement.title
            )
            
            # Clean up the response (remove markdown code blocks if present)
            content = content.strip()
            if content.startswith('```json'):
                content = content[7:]  # Remove ```json
            if content.startswith('```'):
                content = content[3:]   # Remove ```
            if content.endswith('```'):
                content = content[:-3]  # Remove trailing ```
            content = content.strip()
            
            self.logger.debug(f"Cleaned AI response: {repr(content[:200])}...")
            
            # Parse JSON response
            try:
                stories_data = json.loads(content)
            except json.JSONDecodeError as e:
                self.logger.error(f"JSON parse error: {e}")
                self.logger.error(f"Failed to parse response: {content[:500]}...")  # Log first 500 chars
                raise Exception(f"Failed to parse AI response as JSON: {str(e)}")
            
            # Convert to EnhancedUserStory objects
            stories = []
            for story_data in stories_data.get("stories", []):
                # Handle acceptance criteria format
                acceptance_criteria = story_data.get("acceptance_criteria", [])
                if isinstance(acceptance_criteria, str):
                    acceptance_criteria = acceptance_criteria.split("\n")
                
                # Combine description, technical_context, and business_requirements
                description = story_data.get("description", "")
                technical_context = story_data.get("technical_context", "")
                business_requirements = story_data.get("business_requirements", "")
                
                # Format the complete description with HTML formatting
                full_description = description
                if technical_context:
                    full_description += f"<br><br><strong>Technical Context:</strong><br>{technical_context}"
                if business_requirements:
                    full_description += f"<br><br><strong>Business Requirements:</strong><br>{business_requirements}"
                
                # Create an enhanced story with complexity analysis and additional metadata
                story = self.story_creator.create_enhanced_story(
                    heading=story_data["heading"],
                    description=full_description,
                    acceptance_criteria=acceptance_criteria
                )
                
                # Note: story_points is automatically calculated and stored in story.complexity_analysis.story_points
                # by the enhanced_story_creator during complexity analysis
                
                stories.append(story)
            
            self.logger.info(f"Successfully created {len(stories)} enhanced user stories")
            return stories
            
        except json.JSONDecodeError as e:
            raise Exception(f"Failed to parse AI response as JSON: {str(e)}")
        except Exception as e:
            raise Exception(f"AI analysis failed: {str(e)}")
    
    def _build_extraction_prompt(self, requirement: Requirement, context: dict = None, domain_guidelines: dict = None, stakeholders: List[str] = None) -> str:
        """Build the prompt for AI analysis with enhanced context"""
        
        base_prompt = f"""
Please analyze the following requirement and extract user stories from it.

**Requirement Title:** {requirement.title}

**Requirement Description:** 
{requirement.description}
"""
        
        # Add context information if available
        if context:
            base_prompt += f"""
**Context Analysis:**
- Domain: {context.get('domain', 'general')}
- Complexity: {context.get('complexity', 'medium')}
- Scope: {context.get('scope', 'medium')}
- Functional Areas: {', '.join(context.get('functional_areas', []))}
- Technical Components: {', '.join(context.get('technical_components', []))}
- User Interactions: {', '.join(context.get('user_interactions', []))}
- Data Entities: {', '.join(context.get('data_entities', []))}
"""
        
        # Add domain-specific guidelines
        if domain_guidelines:
            base_prompt += f"""
**Domain Guidelines:**
- Common User Personas: {', '.join(domain_guidelines.get('common_personas', []))}
- Key Workflows: {', '.join(domain_guidelines.get('key_workflows', []))}
- Critical Aspects: {', '.join(domain_guidelines.get('critical_aspects', []))}
"""
        
        # Add stakeholder information
        if stakeholders:
            base_prompt += f"""
**Identified Stakeholders:** {', '.join(stakeholders)}
"""
        
        base_prompt += """
**Instructions:**
1. Break down this requirement into 2-6 logical user stories based on complexity and scope
2. Each story should be focused on a single piece of functionality
3. Ensure stories are independent and deliverable
4. Consider the domain context and stakeholders when crafting stories
5. Write clear acceptance criteria that are testable and specific
6. Include edge cases and error scenarios where relevant
7. Consider non-functional requirements (performance, security, usability)

**Story Quality Guidelines:**
- Headlines should be specific and action-oriented
- Descriptions should include both user value and technical context
- Acceptance criteria should cover happy path, edge cases, and error scenarios
- Stories should be sized for 1-3 day development efforts
- Include relevant business rules and constraints

**Required JSON Response Format:**
{
    "stories": [
        {
            "heading": "Specific, action-oriented title",
            "description": "As a [specific user type], I want [specific goal] so that [clear benefit]",
            "technical_context": "Technical details and implementation requirements",
            "business_requirements": "Business rules, constraints, and requirements",
            "acceptance_criteria": [
                "Given [specific context/state] When [specific action] Then [specific outcome] And [additional outcomes]",
                "Given [error condition] When [action] Then [error handling behavior]",
                "Given [edge case] When [action] Then [expected behavior]"
            ],
            "priority": "High|Medium|Low",
            "story_points": "1|2|3|5|8",
            "dependencies": ["Other stories this depends on"],
            "business_value": "Clear statement of business value"
        }
    ]
}

Return only valid JSON, no additional text.
"""
        
        return base_prompt
    
    def _get_enhanced_system_prompt(self, context: dict = None, domain_guidelines: dict = None) -> str:
        """Get enhanced system prompt based on context"""
        
        base_prompt = """You are a senior business analyst and product owner with deep expertise in agile development, user story creation, and domain-driven design.

Your expertise includes:
- Breaking down complex requirements into implementable user stories
- Understanding user journeys and personas across different domains
- Identifying dependencies and story relationships
- Ensuring stories are testable, valuable, and appropriately sized
- Incorporating non-functional requirements and business rules
- Risk-based story prioritization

**CORE PRINCIPLES:**
1. **User-Centric**: Every story should deliver clear value to a specific user type
2. **INVEST Criteria**: Stories should be Independent, Negotiable, Valuable, Estimable, Small, Testable
3. **Definition of Ready**: Stories should have clear acceptance criteria and dependencies
4. **Business Value**: Each story should articulate its business impact
5. **Technical Feasibility**: Consider implementation complexity and constraints

**STORY STRUCTURE REQUIREMENTS:**
- **Heading**: Action-oriented, specific, under 80 characters
- **Description**: Follow "As a [specific persona], I want [specific capability] so that [business value]" format
- **Technical Context**: Separate field with technical details, implementation requirements, and system interactions
- **Business Requirements**: Separate field with business rules, constraints, and domain-specific requirements
- **Acceptance Criteria**: Use Given/When/Then format, cover positive, negative, and edge cases
- **Priority**: Based on business value, risk, and dependencies
- **Story Points**: Relative sizing (1=simple, 2=straightforward, 3=moderate, 5=complex, 8=very complex)
"""
        
        # Add domain-specific context
        if context and context.get('domain') != 'general':
            domain = context.get('domain')
            base_prompt += f"""
**DOMAIN EXPERTISE - {domain.upper()}:**
You have specialized knowledge in {domain} domain including:
- Industry-specific workflows and user journeys
- Regulatory requirements and compliance needs
- Common integration patterns and technical constraints
- Domain-specific security and performance requirements
- Typical user personas and their goals
"""
        
        # Add complexity awareness
        if context and context.get('complexity'):
            complexity = context.get('complexity')
            if complexity == 'high':
                base_prompt += """
**COMPLEXITY AWARENESS:**
This is a high-complexity requirement. Focus on:
- Breaking down into smaller, manageable stories
- Identifying technical risks and dependencies
- Including infrastructure and non-functional stories
- Planning for integration and testing complexity
"""
        
        base_prompt += """
**OUTPUT FORMAT:**
Provide response as valid JSON only. Ensure all stories are well-formed and follow the specified structure.
"""
        
        return base_prompt
    
    def validate_stories(self, stories: List[EnhancedUserStory]) -> List[str]:
        """Validate a list of enhanced user stories"""
        issues = []
        
        for i, story in enumerate(stories):
            story_num = i + 1
            
            # Check heading
            if not story.heading or len(story.heading.strip()) < 5:
                issues.append(f"Story {story_num}: Heading too short or missing")
            
            if len(story.heading) > 100:
                issues.append(f"Story {story_num}: Heading too long (over 100 characters)")
            
            # Check description
            if not story.description or len(story.description.strip()) < 10:
                issues.append(f"Story {story_num}: Description too short or missing")
            
            # Check acceptance criteria
            if not story.acceptance_criteria:
                issues.append(f"Story {story_num}: No acceptance criteria provided")
            elif len(story.acceptance_criteria) < 1:
                issues.append(f"Story {story_num}: At least one acceptance criteria required")
            
            # Check each acceptance criteria
            for j, criteria in enumerate(story.acceptance_criteria):
                if not criteria or len(criteria.strip()) < 5:
                    issues.append(f"Story {story_num}, Criteria {j+1}: Too short or empty")
        
        return issues
    
    def _analyze_requirement_context(self, requirement: Requirement) -> dict:
        """Analyze requirement to extract context for better story generation"""
        text = f"{requirement.title} {requirement.description}".lower()
        
        context = {
            'domain': self._detect_domain(text),
            'complexity': self._assess_complexity(requirement),
            'scope': self._determine_scope(requirement),
            'functional_areas': self._extract_functional_areas(text),
            'technical_components': self._extract_technical_components(text),
            'user_interactions': self._extract_user_interactions(text),
            'data_entities': self._extract_data_entities(text),
            'integration_points': self._extract_integration_points(text),
            'business_rules': self._extract_business_rules(requirement),
            'non_functional_requirements': self._extract_nfr(text)
        }
        
        return context
    
    def _detect_domain(self, text: str) -> str:
        """Detect the application domain for context-specific story generation"""
        domains = {
            'e-commerce': ['shop', 'cart', 'order', 'payment', 'product', 'checkout', 'purchase', 'inventory'],
            'banking': ['account', 'transfer', 'balance', 'transaction', 'loan', 'credit', 'debit', 'interest'],
            'healthcare': ['patient', 'medical', 'appointment', 'prescription', 'diagnosis', 'treatment'],
            'education': ['student', 'course', 'grade', 'assignment', 'enrollment', 'curriculum'],
            'hrms': ['employee', 'payroll', 'leave', 'performance', 'attendance', 'recruitment'],
            'crm': ['customer', 'lead', 'opportunity', 'contact', 'campaign', 'sales'],
            'project_management': ['project', 'task', 'milestone', 'resource', 'timeline', 'gantt'],
            'logistics': ['shipment', 'delivery', 'warehouse', 'tracking', 'route', 'fleet']
        }
        
        for domain, keywords in domains.items():
            if sum(1 for keyword in keywords if keyword in text) >= 2:
                return domain
        return 'general'
    
    def _assess_complexity(self, requirement: Requirement) -> str:
        """Assess the complexity level of the requirement"""
        text = f"{requirement.title} {requirement.description}"
        
        complexity_indicators = {
            'high': ['integration', 'real-time', 'scalable', 'distributed', 'concurrent', 'migration'],
            'medium': ['workflow', 'validation', 'notification', 'reporting', 'dashboard'],
            'low': ['display', 'view', 'list', 'search', 'filter', 'sort']
        }
        
        scores = {}
        for level, indicators in complexity_indicators.items():
            scores[level] = sum(1 for indicator in indicators if indicator in text.lower())
        
        return max(scores, key=scores.get) if any(scores.values()) else 'medium'
    
    def _determine_scope(self, requirement: Requirement) -> str:
        """Determine the scope of the requirement"""
        word_count = len(requirement.description.split())
        
        if word_count < 50:
            return 'small'
        elif word_count < 150:
            return 'medium'
        else:
            return 'large'
    
    def _extract_functional_areas(self, text: str) -> List[str]:
        """Extract functional areas mentioned in the requirement"""
        functional_areas = [
            'authentication', 'authorization', 'user_management', 'data_management',
            'reporting', 'analytics', 'notifications', 'search', 'workflow',
            'integration', 'api', 'ui_ux', 'security', 'audit', 'configuration'
        ]
        
        found_areas = []
        for area in functional_areas:
            if area.replace('_', ' ') in text or area.replace('_', '') in text:
                found_areas.append(area)
        
        return found_areas[:5]  # Limit to top 5
    
    def _extract_technical_components(self, text: str) -> List[str]:
        """Extract technical components mentioned"""
        components = [
            'database', 'api', 'service', 'microservice', 'frontend', 'backend',
            'cache', 'queue', 'scheduler', 'webhook', 'middleware', 'gateway'
        ]
        
        found_components = []
        for component in components:
            if component in text:
                found_components.append(component)
        
        return found_components
    
    def _extract_user_interactions(self, text: str) -> List[str]:
        """Extract user interaction patterns"""
        interactions = [
            'login', 'register', 'create', 'update', 'delete', 'view', 'search',
            'filter', 'sort', 'upload', 'download', 'export', 'import', 'approve'
        ]
        
        found_interactions = []
        for interaction in interactions:
            if interaction in text:
                found_interactions.append(interaction)
        
        return found_interactions
    
    def _extract_data_entities(self, text: str) -> List[str]:
        """Extract data entities that will be involved"""
        import re
        
        # Look for nouns that might be data entities
        entity_patterns = [
            r'\b(user|customer|product|order|invoice|payment|account|profile)\b',
            r'\b(document|file|record|report|data|information)\b',
            r'\b(request|response|message|notification|alert)\b'
        ]
        
        entities = set()
        for pattern in entity_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                entities.add(match.group(1).lower())
        
        return list(entities)[:5]
    
    def _extract_integration_points(self, text: str) -> List[str]:
        """Extract integration points mentioned"""
        integrations = [
            'third_party', 'external_api', 'webhook', 'payment_gateway',
            'email_service', 'sms_service', 'file_storage', 'cdn'
        ]
        
        found_integrations = []
        for integration in integrations:
            if integration.replace('_', ' ') in text or integration.replace('_', '') in text:
                found_integrations.append(integration)
        
        return found_integrations
    
    def _extract_business_rules(self, requirement: Requirement) -> List[str]:
        """Extract business rules from the requirement"""
        text = requirement.description.lower()
        rule_indicators = ['must', 'should', 'shall', 'required', 'mandatory', 'optional', 'if', 'when', 'unless']
        
        sentences = text.split('.')
        business_rules = []
        
        for sentence in sentences:
            if any(indicator in sentence for indicator in rule_indicators):
                business_rules.append(sentence.strip())
        
        return business_rules[:3]  # Limit to top 3
    
    def _extract_nfr(self, text: str) -> List[str]:
        """Extract non-functional requirements"""
        nfr_keywords = [
            'performance', 'security', 'scalability', 'availability', 'reliability',
            'usability', 'maintainability', 'compatibility', 'accessibility'
        ]
        
        found_nfr = []
        for nfr in nfr_keywords:
            if nfr in text:
                found_nfr.append(nfr)
        
        return found_nfr
    
    def _get_domain_guidelines(self, domain: str) -> dict:
        """Get domain-specific guidelines for story generation"""
        guidelines = {
            'e-commerce': {
                'common_personas': ['Customer', 'Admin', 'Seller', 'Support Agent'],
                'key_workflows': ['Browse Products', 'Purchase Flow', 'Order Management', 'Customer Service'],
                'critical_aspects': ['Payment Security', 'Inventory Management', 'User Experience']
            },
            'banking': {
                'common_personas': ['Account Holder', 'Bank Admin', 'Compliance Officer', 'Support Agent'],
                'key_workflows': ['Account Management', 'Transaction Processing', 'Loan Processing', 'Reporting'],
                'critical_aspects': ['Security', 'Compliance', 'Audit Trail', 'Real-time Processing']
            },
            'healthcare': {
                'common_personas': ['Patient', 'Doctor', 'Nurse', 'Admin', 'Pharmacist'],
                'key_workflows': ['Appointment Booking', 'Medical Records', 'Prescription Management', 'Billing'],
                'critical_aspects': ['Privacy (HIPAA)', 'Security', 'Accuracy', 'Accessibility']
            },
            'general': {
                'common_personas': ['User', 'Admin', 'Manager', 'Guest'],
                'key_workflows': ['User Management', 'Data Management', 'Reporting', 'Configuration'],
                'critical_aspects': ['Usability', 'Security', 'Performance', 'Maintainability']
            }
        }
        
        return guidelines.get(domain, guidelines['general'])
    
    def _identify_stakeholders(self, requirement: Requirement) -> List[str]:
        """Identify stakeholders from the requirement"""
        text = f"{requirement.title} {requirement.description}".lower()
        
        stakeholder_patterns = {
            'end_users': ['user', 'customer', 'client', 'visitor', 'guest'],
            'administrators': ['admin', 'administrator', 'manager', 'supervisor'],
            'technical': ['developer', 'system', 'api', 'service'],
            'business': ['business', 'analyst', 'stakeholder', 'owner'],
            'support': ['support', 'help desk', 'agent', 'operator']
        }
        
        identified_stakeholders = []
        for category, keywords in stakeholder_patterns.items():
            if any(keyword in text for keyword in keywords):
                identified_stakeholders.append(category)
        
        return identified_stakeholders if identified_stakeholders else ['end_users']
    
    def _refine_and_validate_stories(self, stories: List[EnhancedUserStory], context: dict) -> List[EnhancedUserStory]:
        """Refine and validate generated stories based on context"""
        refined_stories = []
        
        for story in stories:
            # Validate story completeness
            if self._is_story_complete(story):
                # Enhance with context-specific information
                enhanced_story = self._enhance_story_with_context(story, context)
                refined_stories.append(enhanced_story)
            else:
                self.logger.warning(f"Incomplete story filtered out: {story.heading}")
        
        return refined_stories
    
    def _is_story_complete(self, story: EnhancedUserStory) -> bool:
        """Check if a story meets completeness criteria"""
        return (
            story.heading and len(story.heading.strip()) > 5 and
            story.description and len(story.description.strip()) > 20 and
            story.acceptance_criteria and len(story.acceptance_criteria) > 0
        )
    
    def _enhance_story_with_context(self, story: EnhancedUserStory, context: dict) -> EnhancedUserStory:
        """Enhance story with context-specific information"""
        # Add domain-specific tags or metadata if needed
        # This could be extended to add more context-aware enhancements
        return story
    
    def _filter_duplicate_stories(self, stories: List[EnhancedUserStory], existing_stories: List[dict]) -> List[EnhancedUserStory]:
        """Filter out duplicate stories while preserving EnhancedUserStory objects with complexity analysis"""
        filtered_stories = []
        
        self.logger.debug(f"Checking against {len(existing_stories)} existing stories")
        
        for story in stories:
            # Check for duplicates
            is_duplicate = any(
                story.heading == es.get('heading') and
                story.description == es.get('description') and
                story.acceptance_criteria == es.get('acceptance_criteria')
                for es in existing_stories
            )
            
            if not is_duplicate:
                # Keep the EnhancedUserStory object to preserve complexity analysis and story points
                filtered_stories.append(story)
            else:
                self.logger.debug(f"Duplicate story filtered out: {story.heading}")
        
        return filtered_stories
    
    def _prioritize_stories(self, stories: List[EnhancedUserStory], context: dict) -> List[EnhancedUserStory]:
        """Prioritize stories based on business value and dependencies"""
        # Simple prioritization based on context
        priority_keywords = {
            'high': ['critical', 'essential', 'must', 'required', 'security', 'login', 'payment'],
            'medium': ['should', 'important', 'workflow', 'process', 'management'],
            'low': ['nice to have', 'optional', 'enhancement', 'improvement']
        }
        
        def get_priority_score(story: EnhancedUserStory) -> int:
            text = f"{story.heading} {story.description}".lower()
            
            high_count = sum(1 for keyword in priority_keywords['high'] if keyword in text)
            medium_count = sum(1 for keyword in priority_keywords['medium'] if keyword in text)
            low_count = sum(1 for keyword in priority_keywords['low'] if keyword in text)
            
            return high_count * 3 + medium_count * 2 + low_count * 1
        
        # Sort stories by priority score (highest first)
        return sorted(stories, key=get_priority_score, reverse=True)

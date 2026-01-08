import logging
from typing import List, Optional, Dict, Any

from src.ado_client import ADOClient
from src.story_extractor import StoryExtractor
from src.test_case_extractor import TestCaseExtractor
from src.models import Requirement, StoryExtractionResult, UserStory, ChangeDetectionResult, EpicSyncResult, TestCaseExtractionResult
from src.enhanced_story_creator import EnhancedStoryCreator
from src.models_enhanced import EnhancedUserStory
from config.settings import Settings

class StoryExtractionAgent:
    """Main agent that orchestrates the story extraction and test case generation process"""
    
    def __init__(self):
        self.ado_client = ADOClient()
        self.story_extractor = StoryExtractor()
        self.test_case_extractor = TestCaseExtractor()
        self.story_creator = EnhancedStoryCreator()  # Add enhanced story creator
        self.logger = self._setup_logger()
    
    def process_requirement_by_id(self, requirement_id: str, upload_to_ado: bool = True) -> StoryExtractionResult:
        """Process a single requirement by ID or title (string or int)"""
        print(f"\n[AGENT] Starting to process requirement ID: {requirement_id}")
        try:
            # Try to fetch requirement by ID or title (string or int)
            print("[AGENT] Fetching requirement from Azure DevOps...")
            requirement = self.ado_client.get_requirement_by_id(requirement_id)

            if not requirement:
                error_msg = f"Requirement {requirement_id} not found or access denied"
                print(f"[ERROR] {error_msg}")
                return StoryExtractionResult(
                    requirement_id=requirement_id,
                    requirement_title="",
                    stories=[],
                    extraction_successful=False,
                    error_message=error_msg
                )

            print(f"[AGENT] Found requirement: {requirement.title}")

            # Extract stories
            print("[DEBUG] StoryExtractionAgent: Starting story extraction")
            result = self.story_extractor.extract_stories(requirement)
            
            if not result.extraction_successful:
                print(f"[ERROR] StoryExtractionAgent: Story extraction failed: {result.error_message}")
                return result
            
            print(f"[DEBUG] StoryExtractionAgent: Successfully extracted {len(result.stories)} stories")

            # Upload to ADO if requested
            if upload_to_ado and result.stories:
                print("[DEBUG] StoryExtractionAgent: Starting upload to ADO")
                try:
                    uploaded_story_ids = self._upload_stories_to_ado(result.stories, requirement_id)
                    print(f"[DEBUG] StoryExtractionAgent: Successfully uploaded {len(uploaded_story_ids)} stories")
                except Exception as e:
                    print(f"[ERROR] StoryExtractionAgent: Failed to upload stories: {str(e)}")
                    result.error_message = f"Failed to upload stories: {str(e)}"
                    result.extraction_successful = False
            
            return result

        except Exception as e:
            # Accept string-based IDs (e.g., 'EPIC 1')
            ado_id = requirement_id.strip()
            print(f"[AGENT] Using requirement ID: {ado_id}")
            try:
                # Get all requirements
                requirement = self.ado_client.get_requirement_by_id(ado_id)
                self.logger.info(f"Found requirement to process: {ado_id}")
                result = self.process_requirement_by_id(str(requirement.id), upload_to_ado)
                # Summary
                successful = 1 if result.extraction_successful else 0
                total_stories = len(result.stories)
                print(f"[SUMMARY] Processed 1 requirement. Successful: {successful}, Total stories: {total_stories}")
                return [result]
            except Exception as inner_e:
                print(f"[ERROR] Failed to process requirement {ado_id}: {str(inner_e)}")
                return []

    def preview_stories(self, requirement_id: str) -> StoryExtractionResult:
        """Extract and preview stories without uploading to ADO"""
        return self.process_requirement_by_id(requirement_id, upload_to_ado=False)
    
    def _upload_stories_to_ado(self, stories: List[UserStory], parent_requirement_id: str) -> List[int]:
        """Upload user stories to ADO as child items of the requirement"""
        parent_id = parent_requirement_id  # No numeric parsing anymore
        uploaded_ids = []
        
        for story in stories:
            try:
                # Debug logging to see story type and complexity analysis
                story_type = type(story).__name__
                has_complexity = hasattr(story, 'complexity_analysis') and story.complexity_analysis is not None
                self.logger.info(f"[AGENT] Uploading story type: {story_type}, has complexity: {has_complexity}")
                if has_complexity:
                    self.logger.info(f"[AGENT] Story points: {story.complexity_analysis.story_points}")
                
                story_data = story.to_ado_format()
                self.logger.info(f"[AGENT] Story data fields: {list(story_data.keys())}")
                
                result = self.ado_client.create_user_story(story_data, parent_id)
                
                # Ensure we got a valid ID
                if not isinstance(result, int):
                    self.logger.error(f"Story upload did not return a valid integer ID for '{story.heading}'")
                    continue
                    
                story_id = result
                uploaded_ids.append(story_id)
                self.logger.info(f"Created user story {story_id}: {story.heading}")
                
                # Extract and upload test cases for this story if auto-extraction is enabled
                if Settings.AUTO_TEST_CASE_EXTRACTION:
                    self.logger.info(f"Auto test case extraction enabled - extracting test cases for story {story_id}")
                    try:
                        self._extract_and_upload_test_cases(story, str(story_id))
                    except Exception as test_case_error:
                        self.logger.error(f"Failed to create test cases for story {story_id}: {str(test_case_error)}")
                        # Continue with next story even if test case creation fails
                else:
                    self.logger.info(f"Auto test case extraction disabled - skipping test cases for story {story_id}")
                
            except Exception as e:
                self.logger.error(f"Failed to create user story '{story.heading}': {str(e)}")
                continue
        
        return uploaded_ids
    
    def _extract_and_upload_test_cases(self, user_story: UserStory, parent_story_id: str) -> List[int]:
        """Extract test cases from a user story and upload them to ADO"""
        try:
            self.logger.info(f"Extracting test cases for story: {user_story.heading}")
            
            # Extract test cases using AI
            test_case_result = self.test_case_extractor.extract_test_cases(user_story, parent_story_id)
            
            if not test_case_result.extraction_successful:
                raise Exception(test_case_result.error_message)
            
            self.logger.info(f"Extracted {len(test_case_result.test_cases)} test cases for story {parent_story_id}")
            
            # Upload test cases to ADO
            uploaded_test_case_ids = []
            for test_case in test_case_result.test_cases:
                try:
                    test_case_data = test_case.to_ado_format()
                    # Create test case as child of the user story
                    test_case_id = self.ado_client.create_test_case(test_case_data, parent_story_id)
                    uploaded_test_case_ids.append(test_case_id)
                    self.logger.info(f"Created test case {test_case_id}: {test_case.title}")
                    
                except Exception as tc_error:
                    self.logger.error(f"Failed to upload test case '{test_case.title}': {str(tc_error)}")
                    continue
            
            return uploaded_test_case_ids
            
        except Exception as e:
            self.logger.error(f"Test case extraction failed for story {parent_story_id}: {str(e)}")
            return []
    
    def extract_test_cases_for_story(self, story_id: str) -> TestCaseExtractionResult:
        """Extract test cases for an existing user story by ID"""
        try:
            # First validate that the work item is appropriate for test case extraction
            is_valid, work_item_type = self.ado_client.is_valid_work_item_for_test_extraction(story_id)

            if not is_valid:
                if work_item_type.startswith("Error:"):
                    # Handle API errors
                    return TestCaseExtractionResult(
                        story_id=story_id,
                        story_title="",
                        test_cases=[],
                        extraction_successful=False,
                        error_message=f"Failed to validate work item: {work_item_type}"
                    )
                else:
                    # Handle invalid work item types
                    allowed_types = "User Story or Task"
                    if work_item_type.lower() == 'epic':
                        error_message = (
                            f"❌ Test case extraction not allowed from Epic work items.\n\n"
                            f"📝 **Current work item:** {work_item_type} (ID: {story_id})\n"
                            f"✅ **Allowed types:** {allowed_types}\n\n"
                            f"**Solution:** Please use a {allowed_types} work item ID instead of an Epic ID. "
                            f"Epics are high-level containers and should not have direct test cases. "
                            f"Extract test cases from the User Stories or Tasks that belong to this Epic."
                        )
                    else:
                        error_message = (
                            f"❌ Test case extraction not allowed from '{work_item_type}' work items.\n\n"
                            f"📝 **Current work item:** {work_item_type} (ID: {story_id})\n"
                            f"✅ **Allowed types:** {allowed_types}\n\n"
                            f"**Solution:** Please provide a {allowed_types} work item ID for test case extraction."
                        )

                    return TestCaseExtractionResult(
                        story_id=story_id,
                        story_title="",
                        test_cases=[],
                        extraction_successful=False,
                        error_message=error_message
                    )

            print(f"[AGENT] ✅ Work item {story_id} is valid for test extraction (Type: {work_item_type})")

            # Fetch the user story from ADO
            story_work_item = self.ado_client.get_work_item_by_id(story_id)
            if not story_work_item:
                return TestCaseExtractionResult(
                    story_id=story_id,
                    story_title="",
                    test_cases=[],
                    extraction_successful=False,
                    error_message=f"{work_item_type} {story_id} not found"
                )
            
            # Convert ADO work item to UserStory model
            fields = story_work_item.fields
            user_story = UserStory(
                heading=fields.get("System.Title", ""),
                description=fields.get("System.Description", ""),
                acceptance_criteria=self._extract_acceptance_criteria_from_description(
                    fields.get("System.Description", "")
                )
            )
            
            # Extract test cases
            return self.test_case_extractor.extract_test_cases(user_story, story_id)
            
        except Exception as e:
            return TestCaseExtractionResult(
                story_id=story_id,
                story_title="",
                test_cases=[],
                extraction_successful=False,
                error_message=str(e)
            )
    
    def extract_test_cases_as_issues(self, story_id: str, upload_to_ado: bool = True) -> TestCaseExtractionResult:
        """Extract test cases for a user story and create them using the configured work item type"""
        try:
            print(f"\n[AGENT] Extracting test cases as {Settings.TEST_CASE_EXTRACTION_TYPE} for story ID: {story_id}")

            # Get the user story work item
            work_item = self.ado_client.get_work_item_by_id(story_id)
            if not work_item:
                error_msg = f"User story {story_id} not found"
                return TestCaseExtractionResult(
                    story_id=story_id,
                    story_title="",
                    test_cases=[],
                    extraction_successful=False,
                    error_message=error_msg
                )

            # Convert work item to EnhancedUserStory object
            description = work_item.fields.get("System.Description", "")
            acceptance_criteria = self._extract_acceptance_criteria_from_description(description)
            
            # Create enhanced user story with complexity analysis
            user_story = self.story_creator.create_enhanced_story(
                heading=work_item.fields.get("System.Title", ""),
                description=description,
                acceptance_criteria=acceptance_criteria
            )

            print(f"[AGENT] Found user story: {user_story.heading}")

            # Extract test cases using AI
            print("[AGENT] Extracting test cases with AI...")
            result = self.test_case_extractor.extract_test_cases(user_story, story_id)

            if not result.extraction_successful:
                print(f"[ERROR] Test case extraction failed: {result.error_message}")
                return result

            print(f"[AGENT] Successfully extracted {len(result.test_cases)} test cases")

            # Upload test cases using the configured type to ADO if requested
            if upload_to_ado and result.test_cases:
                print(f"[AGENT] Creating {len(result.test_cases)} test cases as {Settings.TEST_CASE_EXTRACTION_TYPE} in Azure DevOps...")
                created_issues = []

                for i, test_case in enumerate(result.test_cases, 1):
                    try:
                        print(f"[AGENT] Creating test case {i}/{len(result.test_cases)}: {test_case.title}")

                        # Convert TestCase to dict format for ADO client
                        test_case_data = {
                            'title': test_case.title,
                            'description': test_case.description,
                            'test_type': test_case.test_type,
                            'preconditions': test_case.preconditions,
                            'test_steps': test_case.test_steps,
                            'expected_result': test_case.expected_result,
                            'priority': test_case.priority
                        }

                        # Use the new configurable method
                        work_item_id = self.ado_client.create_test_case_with_config(
                            test_case_data=test_case_data,
                            parent_story_id=int(story_id)
                        )

                        created_issues.append(work_item_id)
                        print(f"[AGENT] ✅ Created {Settings.TEST_CASE_EXTRACTION_TYPE} #{work_item_id} for test case: {test_case.title}")

                    except Exception as e:
                        print(f"[ERROR] Failed to create {Settings.TEST_CASE_EXTRACTION_TYPE} for test case '{test_case.title}': {e}")
                        continue

                print(f"[AGENT] Successfully created {len(created_issues)} test case {Settings.TEST_CASE_EXTRACTION_TYPE}s in Azure DevOps")
                result.created_issue_ids = created_issues

            return result

        except Exception as e:
            error_msg = f"Failed to extract test cases as {Settings.TEST_CASE_EXTRACTION_TYPE}s: {str(e)}"
            print(f"[ERROR] {error_msg}")
            return TestCaseExtractionResult(
                story_id=story_id,
                story_title="",
                test_cases=[],
                extraction_successful=False,
                error_message=error_msg
            )

    def extract_test_cases_for_epic_stories(self, epic_id: str, upload_to_ado: bool = True) -> Dict[str, TestCaseExtractionResult]:
        """Extract test cases as issues for all user stories under an epic"""
        try:
            print(f"\n[AGENT] Extracting test cases as issues for all stories in epic: {epic_id}")

            # Get all child stories for the epic
            child_story_ids = self.ado_client.get_child_stories(int(epic_id))

            if not child_story_ids:
                print(f"[WARNING] No child stories found for epic {epic_id}")
                return {}

            print(f"[AGENT] Found {len(child_story_ids)} child stories in epic")

            results = {}
            for story_id in child_story_ids:
                print(f"\n[AGENT] Processing story {story_id}...")
                result = self.extract_test_cases_as_issues(str(story_id), upload_to_ado)
                results[str(story_id)] = result

            return results

        except Exception as e:
            error_msg = f"Failed to extract test cases for epic stories: {str(e)}"
            print(f"[ERROR] {error_msg}")
            return {}

    def _extract_acceptance_criteria_from_description(self, description: str) -> List[str]:
        """Extract acceptance criteria from story description"""
        if not description:
            return []

        # Look for acceptance criteria patterns in the description
        import re
        criteria = []

        # Common patterns for acceptance criteria
        patterns = [
            r"(?:acceptance criteria|ac):\s*(.*?)(?:\n\n|\n(?=[A-Z])|$)",
            r"given.*?when.*?then.*",
            r"as a.*?i want.*?so that.*"
        ]

        for pattern in patterns:
            matches = re.findall(pattern, description, re.IGNORECASE | re.DOTALL)
            for match in matches:
                if match.strip():
                    criteria.append(match.strip())

        # If no structured criteria found, split by common delimiters
        if not criteria:
            lines = description.split('\n')
            for line in lines:
                line = line.strip()
                if line and (line.startswith('-') or line.startswith('*') or line.startswith('•')):
                    criteria.append(line[1:].strip())

        return criteria[:5] if criteria else ["Verify the functionality works as described"]

    def preview_test_cases(self, story_id: str) -> TestCaseExtractionResult:
        """Extract and preview test cases for a user story without uploading to ADO"""
        return self.extract_test_cases_for_story(story_id)
    
    def get_story_with_test_cases(self, story_id: str) -> Dict[str, Any]:
        """Get a user story and its associated test cases"""
        try:
            # Fetch the user story
            story_work_item = self.ado_client.get_work_item_by_id(story_id)
            if not story_work_item:
                return {"error": f"User story {story_id} not found"}
            
            # Convert ADO work item to EnhancedUserStory model
            fields = story_work_item.fields
            description = fields.get("System.Description", "")
            acceptance_criteria = self._extract_acceptance_criteria_from_description(description)
            
            # Create enhanced user story with complexity analysis
            user_story = self.story_creator.create_enhanced_story(
                heading=fields.get("System.Title", ""),
                description=description,
                acceptance_criteria=acceptance_criteria
            )
            
            # Fetch test cases associated with the user story
            test_case_result = self.ado_client.get_test_cases_by_story_id(story_id)
            
            return {
                "user_story": user_story,
                "test_cases": test_case_result
            }
            
        except Exception as e:
            return {"error": str(e)}
    
    def get_requirement_summary(self, requirement_id: str) -> Dict[str, Any]:
        """Get a summary of a requirement and its child stories"""
        try:
            numeric_id = requirement_id  # No numeric parsing
            requirement = self.ado_client.get_requirement_by_id(numeric_id)
            if not requirement:
                return {"error": f"Requirement {requirement_id} not found"}
            
            child_story_ids = self.ado_client.get_child_stories(numeric_id)
            
            return {
                "requirement": {
                    "id": requirement.id,
                    "title": requirement.title,
                    "description": requirement.description[:200] + "..." if len(requirement.description) > 200 else requirement.description,
                    "state": requirement.state
                },
                "child_stories": {
                    "count": len(child_story_ids),
                    "ids": child_story_ids
                }
            }
            
        except Exception as e:
            return {"error": str(e)}
    
    def synchronize_epic(self, epic_id: str, stored_snapshot: Optional[Dict] = None) -> EpicSyncResult:
        """Detect changes in an EPIC and synchronize its tasks"""
        self.logger.info(f"[AGENT] Synchronizing Epic: {epic_id}")
        try:
            # Fetch the requirement (Epic) from ADO
            requirement = self.ado_client.get_requirement_by_id(epic_id)
            if not requirement:
                error_msg = f"[AGENT] Epic {epic_id} not found or access denied"
                self.logger.error(error_msg)
                return EpicSyncResult(
                    epic_id=epic_id,
                    epic_title="",
                    sync_successful=False,
                    error_message=error_msg
                )
            self.logger.info(f"[AGENT] Fetched Epic: {requirement.title}")
            self.logger.info(f"[AGENT] Epic Description: {requirement.description}")
            # Extract stories
            self.logger.info(f"[AGENT] Extracting stories from Epic {epic_id}")
            extraction_result = self.story_extractor.extract_stories(requirement)
            if not extraction_result.extraction_successful:
                self.logger.error(f"[AGENT] Story extraction failed: {extraction_result.error_message}")
                return EpicSyncResult(
                    epic_id=epic_id,
                    epic_title=requirement.title,
                    sync_successful=False,
                    error_message=extraction_result.error_message
                )
            self.logger.info(f"[AGENT] Extracted {len(extraction_result.stories)} stories from Epic {epic_id}")

            # Get existing stories for this epic to avoid duplicates
            self.logger.info(f"[AGENT] Checking for existing stories in Epic {epic_id}")
            existing_stories = self.ado_client.get_existing_user_stories(int(epic_id))
            self.logger.info(f"[AGENT] Found {len(existing_stories)} existing stories in Epic {epic_id}")

            # Analyze what needs to be created, updated, or left unchanged
            stories_to_create, stories_to_update, unchanged_stories = self._analyze_story_changes(
                existing_stories, extraction_result.stories
            )

            self.logger.info(f"[AGENT] Story analysis complete:")
            self.logger.info(f"[AGENT]   - Stories to create: {len(stories_to_create)}")
            self.logger.info(f"[AGENT]   - Stories to update: {len(stories_to_update)}")
            self.logger.info(f"[AGENT]   - Unchanged stories: {len(unchanged_stories)}")

            # Upload stories to ADO
            created_stories = []
            updated_stories = []
            created_test_cases = []
            
            # Create new stories
            if stories_to_create:
                self.logger.info(f"[AGENT] Creating {len(stories_to_create)} new stories for Epic {epic_id}")
                for story in stories_to_create:
                    story_id = None
                    try:
                        # Debug logging to see story type and complexity analysis
                        story_type = type(story).__name__
                        has_complexity = hasattr(story, 'complexity_analysis') and story.complexity_analysis is not None
                        self.logger.info(f"[AGENT] Story type: {story_type}, has complexity: {has_complexity}")
                        if has_complexity:
                            self.logger.info(f"[AGENT] Story points: {story.complexity_analysis.story_points}")
                        
                        # Use existing create_user_story method with proper format
                        story_data = story.to_ado_format()
                        self.logger.info(f"[AGENT] Story data fields: {list(story_data.keys())}")
                        story_id = self.ado_client.create_user_story(story_data, epic_id)
                    except Exception as upload_exc:
                        self.logger.error(f"[AGENT] Failed to upload story '{story.heading}': {upload_exc}")
                        continue
                    
                    if isinstance(story_id, int):
                        created_stories.append(story_id)
                        self.logger.info(f"[AGENT] Successfully created story {story_id}: {story.heading}")
                        
                        # Extract and upload test cases for this story if auto-extraction is enabled
                        if Settings.AUTO_TEST_CASE_EXTRACTION:
                            self.logger.info(f"[AGENT] Auto test case extraction enabled - extracting test cases for story {story_id}")
                            try:
                                test_case_ids = self._extract_and_upload_test_cases(story, str(story_id))
                                created_test_cases.extend(test_case_ids)
                                self.logger.info(f"[AGENT] Created {len(test_case_ids)} test cases for story {story_id}")
                            except Exception as tc_exc:
                                self.logger.error(f"[AGENT] Failed to create test cases for story {story_id}: {tc_exc}")
                        else:
                            self.logger.info(f"[AGENT] Auto test case extraction disabled - skipping test cases for story {story_id}")
                    else:
                        self.logger.error(f"[AGENT] Story upload did not return a valid integer ID for '{story.heading}'")
            else:
                self.logger.info(f"[AGENT] No new stories to create for Epic {epic_id}")

            # Update existing stories that have changed
            if stories_to_update:
                self.logger.info(f"[AGENT] Updating {len(stories_to_update)} existing stories for Epic {epic_id}")
                for update_info in stories_to_update:
                    try:
                        self._update_user_story(update_info['id'], update_info['new_story'])
                        updated_stories.append(update_info['id'])
                        self.logger.info(f"[AGENT] Successfully updated story {update_info['id']}: {update_info['new_story'].heading}")
                    except Exception as update_exc:
                        self.logger.error(f"[AGENT] Failed to update story {update_info['id']}: {update_exc}")
            else:
                self.logger.info(f"[AGENT] No stories to update for Epic {epic_id}")

            # Log unchanged stories
            unchanged_story_ids = [story.id for story in unchanged_stories]
            if unchanged_story_ids:
                self.logger.info(f"[AGENT] {len(unchanged_story_ids)} stories remain unchanged: {unchanged_story_ids}")

            return EpicSyncResult(
                epic_id=epic_id,
                epic_title=requirement.title,
                sync_successful=True,
                created_stories=created_stories,
                updated_stories=updated_stories,
                unchanged_stories=unchanged_story_ids,
                created_test_cases=created_test_cases
            )
        except Exception as e:
            self.logger.error(f"[AGENT] Exception during Epic sync: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return EpicSyncResult(
                epic_id=epic_id,
                epic_title="",
                sync_successful=False,
                error_message=str(e)
            )
    
    def _analyze_story_changes(self, existing_stories, new_stories):
        """Analyze differences between existing and new stories to determine what to create/update"""
        from difflib import SequenceMatcher
        
        stories_to_create = []
        stories_to_update = []
        unchanged_stories = []
        
        # Convert existing stories to a dict for easier lookup
        existing_by_title = {story.title: story for story in existing_stories}
        
        # Check each new story against existing ones
        for new_story in new_stories:
            best_match = None
            best_similarity = 0.0
            
            # Find the best matching existing story by title similarity
            for existing_title, existing_story in existing_by_title.items():
                similarity = SequenceMatcher(None, new_story.heading.lower(), existing_title.lower()).ratio()
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = existing_story
            
            # If we found a good match (similarity > 0.8), consider it for update
            if best_match and best_similarity > 0.8:
                # Check if the content has actually changed
                existing_content = f"{best_match.title} {best_match.description}"
                new_content = f"{new_story.heading} {new_story.description} {' '.join(new_story.acceptance_criteria)}"
                
                content_similarity = SequenceMatcher(None, existing_content.lower(), new_content.lower()).ratio()
                
                if content_similarity < 0.9:  # Content has changed significantly
                    stories_to_update.append({
                        'id': best_match.id,
                        'existing_story': best_match,
                        'new_story': new_story
                    })
                    # Remove from existing dict so it's not considered again
                    del existing_by_title[best_match.title]
                else:
                    unchanged_stories.append(best_match)
                    del existing_by_title[best_match.title]
            else:
                # No good match found, this is a new story
                stories_to_create.append(new_story)
        
        # Any remaining existing stories that weren't matched are considered unchanged
        for remaining_story in existing_by_title.values():
            unchanged_stories.append(remaining_story)
        
        return stories_to_create, stories_to_update, unchanged_stories
    
    def _update_user_story(self, story_id: int, new_story: UserStory):
        """Update an existing user story in ADO"""
        try:
            story_data = new_story.to_ado_format()
            
            # Prepare update document
            document = []
            for field, value in story_data.items():
                document.append({
                    "op": "replace",
                    "path": f"/fields/{field}",
                    "value": value
                })
            
            # Update the work item
            self.ado_client.wit_client.update_work_item(
                document=document,
                id=story_id
            )
            
        except Exception as e:
            raise Exception(f"Failed to update user story {story_id}: {str(e)}")
    
    def get_epic_snapshot(self, epic_id: str) -> Optional[Dict[str, str]]:
        """Get a snapshot of the current EPIC for change tracking"""
        try:
            numeric_id = int(epic_id)  # Convert to integer for ADO API
            snapshot = self.ado_client.detect_changes_in_epic(numeric_id)
            
            if snapshot:
                return {
                    'content_hash': snapshot.content_hash,
                    'last_modified': snapshot.last_modified.isoformat() if snapshot.last_modified else None,
                    'title': snapshot.title,
                    'state': snapshot.state
                }
            return None
            
        except Exception as e:
            self.logger.error(f"Failed to get EPIC snapshot for {epic_id}: {str(e)}")
            return None

    def extract_stories_for_epic(self, epic_id: str, existing_stories: List[dict] = None) -> List[dict]:
        """Extract stories for a given epic, avoiding duplicates."""
        requirement = self.ado_client.get_requirement_by_id(epic_id)
        if not requirement:
            self.logger.error(f"Requirement (Epic) {epic_id} not found.")
            return []
        result = self.story_extractor.extract_stories(requirement, existing_stories)
        if not result.extraction_successful:
            self.logger.error(f"Story extraction failed for Epic {epic_id}: {result.error_message}")
            return []
        # Convert UserStory objects to dicts for snapshot
        return [story.__dict__ for story in result.stories]

    def _setup_logger(self) -> logging.Logger:
        """Setup logging configuration"""
        logger = logging.getLogger("StoryExtractionAgent")
        logger.setLevel(logging.DEBUG)
        
        if not logger.handlers:
            # Add file handler
            file_handler = logging.FileHandler('logs/story_extraction.log')
            file_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            file_handler.setFormatter(file_formatter)
            file_handler.setLevel(logging.DEBUG)
            logger.addHandler(file_handler)
            
            # Add console handler
            console_handler = logging.StreamHandler()
            console_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            console_handler.setFormatter(console_formatter)
            console_handler.setLevel(logging.INFO)
            logger.addHandler(console_handler)
        
        return logger

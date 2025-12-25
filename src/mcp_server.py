"""
MCP Server for Azure DevOps Story and Test Case Extraction
Provides tools for AI assistants to interact with ADO through MCP protocol
"""
import asyncio
import os
import json
from typing import Any, Optional
from mcp.server import Server
from mcp.types import Tool, TextContent
from pydantic import AnyUrl

from src.ado_client import ADOClient
from src.test_case_extractor import TestCaseExtractor
from src.enhanced_story_creator import EnhancedStoryCreator
from src.ai_client import AIClient


class ADOMCPServer:
    """MCP Server for Azure DevOps operations"""
    
    def __init__(self):
        self.server = Server("ado-story-testcase-server")
        self.ado_client: Optional[ADOClient] = None
        self.ai_client: Optional[AIClient] = None
        self.test_case_extractor: Optional[TestCaseExtractor] = None
        self.story_creator: Optional[EnhancedStoryCreator] = None
        
        # Initialize from environment variables
        self._initialize_clients()
        
        # Register handlers
        self._register_handlers()
    
    def _initialize_clients(self):
        """Initialize ADO and AI clients from environment variables"""
        try:
            # Azure DevOps configuration
            ado_org = os.getenv("AZURE_DEVOPS_ORG")
            ado_project = os.getenv("AZURE_DEVOPS_PROJECT")
            ado_pat = os.getenv("AZURE_DEVOPS_PAT")
            
            # OpenAI/Azure OpenAI configuration
            openai_api_key = os.getenv("OPENAI_API_KEY")
            azure_openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
            azure_openai_key = os.getenv("AZURE_OPENAI_API_KEY")
            
            if ado_org and ado_project and ado_pat:
                self.ado_client = ADOClient(
                    organization=ado_org,
                    project=ado_project,
                    personal_access_token=ado_pat
                )
                print(f"[MCP] Initialized ADO client for {ado_org}/{ado_project}")
            
            if openai_api_key or (azure_openai_endpoint and azure_openai_key):
                self.ai_client = AIClient()
                print("[MCP] Initialized AI client")
                
                if self.ado_client:
                    self.test_case_extractor = TestCaseExtractor(
                        ado_client=self.ado_client,
                        ai_client=self.ai_client
                    )
                    self.story_creator = EnhancedStoryCreator(
                        ado_client=self.ado_client,
                        ai_client=self.ai_client
                    )
                    print("[MCP] Initialized extractors and creators")
        except Exception as e:
            print(f"[MCP] Warning: Failed to initialize clients: {e}")
    
    def _register_handlers(self):
        """Register MCP protocol handlers"""
        
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """List available MCP tools"""
            return [
                Tool(
                    name="extract_test_cases",
                    description="Extract test cases from an Azure DevOps work item (Epic, Feature, or Story)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "work_item_id": {
                                "type": "string",
                                "description": "Azure DevOps work item ID to extract test cases from"
                            },
                            "organization": {
                                "type": "string",
                                "description": "Azure DevOps organization name (optional if set in env)"
                            },
                            "project": {
                                "type": "string",
                                "description": "Azure DevOps project name (optional if set in env)"
                            }
                        },
                        "required": ["work_item_id"]
                    }
                ),
                Tool(
                    name="create_enhanced_story",
                    description="Create an enhanced user story in Azure DevOps with AI-generated complexity analysis",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Story title/heading"
                            },
                            "description": {
                                "type": "string",
                                "description": "Story description with business context"
                            },
                            "acceptance_criteria": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of acceptance criteria"
                            },
                            "parent_id": {
                                "type": "string",
                                "description": "Parent work item ID (optional)"
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Tags to apply to the story (optional)"
                            }
                        },
                        "required": ["title", "description", "acceptance_criteria"]
                    }
                ),
                Tool(
                    name="get_work_item",
                    description="Get details of an Azure DevOps work item by ID",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "work_item_id": {
                                "type": "string",
                                "description": "Azure DevOps work item ID"
                            }
                        },
                        "required": ["work_item_id"]
                    }
                ),
                Tool(
                    name="analyze_story_complexity",
                    description="Analyze the complexity of a story and generate story points recommendation",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Story title"
                            },
                            "description": {
                                "type": "string",
                                "description": "Story description"
                            },
                            "acceptance_criteria": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of acceptance criteria"
                            }
                        },
                        "required": ["title", "description", "acceptance_criteria"]
                    }
                )
            ]
        
        @self.server.call_tool()
        async def call_tool(name: str, arguments: Any) -> list[TextContent]:
            """Handle tool execution"""
            try:
                if name == "extract_test_cases":
                    return await self._extract_test_cases(arguments)
                elif name == "create_enhanced_story":
                    return await self._create_enhanced_story(arguments)
                elif name == "get_work_item":
                    return await self._get_work_item(arguments)
                elif name == "analyze_story_complexity":
                    return await self._analyze_story_complexity(arguments)
                else:
                    return [TextContent(
                        type="text",
                        text=json.dumps({"error": f"Unknown tool: {name}"})
                    )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e)})
                )]
    
    async def _extract_test_cases(self, args: dict) -> list[TextContent]:
        """Extract test cases from a work item"""
        if not self.test_case_extractor:
            return [TextContent(
                type="text",
                text=json.dumps({"error": "Test case extractor not initialized. Check environment variables."})
            )]
        
        work_item_id = args.get("work_item_id")
        
        # Get work item details
        work_item = self.ado_client.get_work_item(int(work_item_id))
        
        # Extract test cases
        test_cases = await asyncio.to_thread(
            self.test_case_extractor.extract_test_cases,
            work_item
        )
        
        result = {
            "work_item_id": work_item_id,
            "work_item_title": work_item.get("fields", {}).get("System.Title", ""),
            "test_cases_count": len(test_cases),
            "test_cases": [
                {
                    "title": tc.get("fields", {}).get("System.Title", ""),
                    "id": tc.get("id"),
                    "state": tc.get("fields", {}).get("System.State", ""),
                    "steps": tc.get("fields", {}).get("Microsoft.VSTS.TCM.Steps", "")
                }
                for tc in test_cases
            ]
        }
        
        return [TextContent(
            type="text",
            text=json.dumps(result, indent=2)
        )]
    
    async def _create_enhanced_story(self, args: dict) -> list[TextContent]:
        """Create an enhanced story with AI analysis"""
        if not self.story_creator:
            return [TextContent(
                type="text",
                text=json.dumps({"error": "Story creator not initialized. Check environment variables."})
            )]
        
        title = args.get("title")
        description = args.get("description")
        acceptance_criteria = args.get("acceptance_criteria", [])
        parent_id = args.get("parent_id")
        tags = args.get("tags", [])
        
        # Create the story
        story_data = {
            "heading": title,
            "description": description,
            "acceptance_criteria": acceptance_criteria
        }
        
        created_story = await asyncio.to_thread(
            self.story_creator.create_enhanced_story,
            story_data,
            parent_id=int(parent_id) if parent_id else None,
            tags=tags
        )
        
        result = {
            "id": created_story.get("id"),
            "title": created_story.get("fields", {}).get("System.Title"),
            "story_points": created_story.get("fields", {}).get("Microsoft.VSTS.Scheduling.StoryPoints"),
            "state": created_story.get("fields", {}).get("System.State"),
            "url": created_story.get("url"),
            "complexity": {
                "level": created_story.get("_complexity_level", "Not analyzed"),
                "rationale": created_story.get("_complexity_rationale", "")
            }
        }
        
        return [TextContent(
            type="text",
            text=json.dumps(result, indent=2)
        )]
    
    async def _get_work_item(self, args: dict) -> list[TextContent]:
        """Get work item details"""
        if not self.ado_client:
            return [TextContent(
                type="text",
                text=json.dumps({"error": "ADO client not initialized. Check environment variables."})
            )]
        
        work_item_id = args.get("work_item_id")
        work_item = self.ado_client.get_work_item(int(work_item_id))
        
        result = {
            "id": work_item.get("id"),
            "type": work_item.get("fields", {}).get("System.WorkItemType"),
            "title": work_item.get("fields", {}).get("System.Title"),
            "state": work_item.get("fields", {}).get("System.State"),
            "description": work_item.get("fields", {}).get("System.Description", ""),
            "acceptance_criteria": work_item.get("fields", {}).get("Microsoft.VSTS.Common.AcceptanceCriteria", ""),
            "story_points": work_item.get("fields", {}).get("Microsoft.VSTS.Scheduling.StoryPoints"),
            "tags": work_item.get("fields", {}).get("System.Tags", ""),
            "url": work_item.get("_links", {}).get("html", {}).get("href", "")
        }
        
        return [TextContent(
            type="text",
            text=json.dumps(result, indent=2)
        )]
    
    async def _analyze_story_complexity(self, args: dict) -> list[TextContent]:
        """Analyze story complexity"""
        if not self.ai_client:
            return [TextContent(
                type="text",
                text=json.dumps({"error": "AI client not initialized. Check environment variables."})
            )]
        
        title = args.get("title")
        description = args.get("description")
        acceptance_criteria = args.get("acceptance_criteria", [])
        
        # Use the story creator's complexity analysis
        story_data = {
            "heading": title,
            "description": description,
            "acceptance_criteria": acceptance_criteria
        }
        
        complexity_analysis = await asyncio.to_thread(
            self.story_creator._analyze_complexity,
            story_data
        )
        
        result = {
            "overall_complexity": complexity_analysis.overall_complexity.value,
            "story_points": complexity_analysis.story_points,
            "rationale": complexity_analysis.rationale,
            "factors": [
                {
                    "name": factor.name,
                    "assessment": factor.assessment.value,
                    "impact": factor.impact
                }
                for factor in complexity_analysis.factors
            ]
        }
        
        return [TextContent(
            type="text",
            text=json.dumps(result, indent=2)
        )]
    
    async def run(self):
        """Run the MCP server"""
        from mcp.server.stdio import stdio_server
        
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options()
            )


async def main():
    """Entry point for MCP server"""
    server = ADOMCPServer()
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())

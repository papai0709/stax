# MCP Server Integration for ADO Story & Test Case Extraction

## Overview

This project now includes a **Model Context Protocol (MCP) server** that allows AI assistants (like Claude Desktop) to interact with Azure DevOps directly through standardized tools.

## Features

The MCP server provides the following tools:

### 1. **extract_test_cases**
Extract test cases from an Azure DevOps work item (Epic, Feature, or Story)
- **Input**: `work_item_id`
- **Output**: List of test cases with details

### 2. **create_enhanced_story**
Create an enhanced user story with AI-generated complexity analysis
- **Input**: `title`, `description`, `acceptance_criteria`, `parent_id` (optional), `tags` (optional)
- **Output**: Created story with ID, story points, and complexity analysis

### 3. **get_work_item**
Get details of any Azure DevOps work item by ID
- **Input**: `work_item_id`
- **Output**: Complete work item details

### 4. **analyze_story_complexity**
Analyze story complexity and get story points recommendation
- **Input**: `title`, `description`, `acceptance_criteria`
- **Output**: Complexity analysis with factors and rationale

## Setup Instructions

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Create a `.env` file or set these environment variables:

```bash
# Azure DevOps Configuration
AZURE_DEVOPS_ORG=your-organization-name
AZURE_DEVOPS_PROJECT=your-project-name
AZURE_DEVOPS_PAT=your-personal-access-token

# OpenAI Configuration (Option 1: Direct OpenAI)
OPENAI_API_KEY=your-openai-api-key

# OR Azure OpenAI Configuration (Option 2: Azure OpenAI)
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-azure-openai-key
AZURE_OPENAI_DEPLOYMENT=gpt-4
```

### 3. Configure MCP Client (Claude Desktop)

Add this configuration to your Claude Desktop config file:

**macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "ado-story-testcase": {
      "command": "python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/path/to/ADO_StoryTestCaseExtraction",
      "env": {
        "AZURE_DEVOPS_ORG": "your-organization-name",
        "AZURE_DEVOPS_PROJECT": "your-project-name",
        "AZURE_DEVOPS_PAT": "your-personal-access-token",
        "OPENAI_API_KEY": "your-openai-api-key",
        "AZURE_OPENAI_ENDPOINT": "https://your-resource.openai.azure.com/",
        "AZURE_OPENAI_API_KEY": "your-azure-openai-key",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-4"
      }
    }
  }
}
```

### 4. Run the MCP Server Standalone (Optional)

For testing or development:

```bash
python -m src.mcp_server
```

## Usage Examples

Once configured with Claude Desktop, you can use natural language commands:

### Example 1: Extract Test Cases
```
"Extract test cases from work item 12345"
```

### Example 2: Create Enhanced Story
```
"Create a story titled 'User Authentication' with complexity analysis"
```

### Example 3: Get Work Item Details
```
"Show me details of work item 67890"
```

### Example 4: Analyze Complexity
```
"Analyze the complexity of a story about implementing OAuth2 authentication"
```

## Architecture

```
┌─────────────────┐
│ Claude Desktop  │
│   (MCP Client)  │
└────────┬────────┘
         │ MCP Protocol
         │ (stdio)
┌────────▼────────┐
│  MCP Server     │
│ (mcp_server.py) │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
┌───▼──┐  ┌──▼───┐
│ ADO  │  │  AI  │
│Client│  │Client│
└──────┘  └──────┘
```

## Security Considerations

1. **Never commit** the `.env` file or config files with real credentials
2. Use **Personal Access Tokens (PAT)** with minimal required permissions
3. Store credentials securely using environment variables or secret management
4. The MCP server runs locally on your machine - credentials never leave your system

## Troubleshooting

### Server Not Showing in Claude Desktop
1. Check that the `cwd` path in config is absolute and correct
2. Verify Python can find the module: `python -m src.mcp_server`
3. Check Claude Desktop logs for errors

### Authentication Errors
1. Verify environment variables are set correctly
2. Check ADO PAT has required permissions (Work Items: Read & Write)
3. Test OpenAI/Azure OpenAI credentials separately

### Module Import Errors
1. Ensure all dependencies are installed: `pip install -r requirements.txt`
2. Run from the project root directory
3. Check Python version compatibility (3.8+)

## Docker Support

You can also run the MCP server in Docker:

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "src.mcp_server"]
```

Build and run:
```bash
docker build -t ado-mcp-server .
docker run -it --env-file .env ado-mcp-server
```

## Contributing

When adding new MCP tools:
1. Add tool definition in `_register_handlers()` → `list_tools()`
2. Implement handler method (e.g., `_new_tool_handler()`)
3. Add tool case in `call_tool()` method
4. Update this documentation

## Resources

- [MCP Documentation](https://modelcontextprotocol.io/)
- [Azure DevOps Python API](https://github.com/microsoft/azure-devops-python-api)
- [OpenAI Python SDK](https://github.com/openai/openai-python)

#!/bin/bash

# STAX (Story & Test Automation eXtractor) - Docker Container Startup Script

set -e

# Print banner
echo "============================================================"
echo "  STAX - Story & Test Automation eXtractor"
echo "  Docker Container Starting..."
echo "============================================================"
echo ""

# Function to show usage instructions
show_usage() {
    echo ""
    echo "============================================================"
    echo "  HOW TO RUN THIS CONTAINER"
    echo "============================================================"
    echo ""
    echo "You must provide environment variables when running this container."
    echo ""
    echo "OPTION 1: Using --env-file (Recommended)"
    echo "-------------------------------------------"
    echo "Create a .env file with your configuration, then run:"
    echo ""
    echo "  docker run -d -p 5001:5001 --env-file .env papai0709/stax:latest"
    echo ""
    echo "OPTION 2: Using -e flags"
    echo "-------------------------------------------"
    echo ""
    echo "For Azure DevOps + Azure OpenAI:"
    echo "  docker run -d -p 5001:5001 \\"
    echo "    -e PLATFORM_TYPE=ADO \\"
    echo "    -e ADO_ORGANIZATION=your-org \\"
    echo "    -e ADO_PROJECT=your-project \\"
    echo "    -e ADO_PAT=your-pat-token \\"
    echo "    -e AI_SERVICE_PROVIDER=AZURE_OPENAI \\"
    echo "    -e AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com \\"
    echo "    -e AZURE_OPENAI_API_KEY=your-key \\"
    echo "    -e AZURE_OPENAI_DEPLOYMENT_NAME=your-deployment \\"
    echo "    papai0709/stax:latest"
    echo ""
    echo "For Azure DevOps + OpenAI:"
    echo "  docker run -d -p 5001:5001 \\"
    echo "    -e PLATFORM_TYPE=ADO \\"
    echo "    -e ADO_ORGANIZATION=your-org \\"
    echo "    -e ADO_PROJECT=your-project \\"
    echo "    -e ADO_PAT=your-pat-token \\"
    echo "    -e AI_SERVICE_PROVIDER=OPENAI \\"
    echo "    -e OPENAI_API_KEY=your-openai-key \\"
    echo "    papai0709/stax:latest"
    echo ""
    echo "For Azure DevOps + GitHub Models (Free):"
    echo "  docker run -d -p 5001:5001 \\"
    echo "    -e PLATFORM_TYPE=ADO \\"
    echo "    -e ADO_ORGANIZATION=your-org \\"
    echo "    -e ADO_PROJECT=your-project \\"
    echo "    -e ADO_PAT=your-pat-token \\"
    echo "    -e AI_SERVICE_PROVIDER=GITHUB \\"
    echo "    -e GITHUB_TOKEN=your-github-pat \\"
    echo "    -e GITHUB_MODEL=gpt-4o-mini \\"
    echo "    papai0709/stax:latest"
    echo ""
    echo "For JIRA + any AI provider, use PLATFORM_TYPE=JIRA with:"
    echo "  -e JIRA_BASE_URL=https://yourcompany.atlassian.net"
    echo "  -e JIRA_USERNAME=your-email"
    echo "  -e JIRA_TOKEN=your-api-token"
    echo ""
    echo "============================================================"
    echo ""
}

# Create necessary directories
mkdir -p logs snapshots

# Set proper permissions
chmod 755 logs snapshots

# Check if monitor configuration exists, create default if not
if [ ! -f "monitor_config.json" ]; then
    echo "[STAX] Creating default monitor configuration..."
    cat > monitor_config.json << EOF
{
    "poll_interval_seconds": 300,
    "auto_sync": true,
    "auto_extract_new_epics": true,
    "log_level": "INFO",
    "max_concurrent_syncs": 3,
    "retry_attempts": 3,
    "retry_delay_seconds": 60
}
EOF
fi

# Validate critical environment variables
if [ -z "$PLATFORM_TYPE" ]; then
    echo "[STAX] PLATFORM_TYPE not set, defaulting to ADO"
    export PLATFORM_TYPE="ADO"
fi

VALIDATION_FAILED=false

if [ "$PLATFORM_TYPE" = "ADO" ]; then
    if [ -z "$ADO_ORGANIZATION" ] || [ -z "$ADO_PROJECT" ] || [ -z "$ADO_PAT" ]; then
        echo ""
        echo "[STAX] ERROR: Azure DevOps configuration incomplete!"
        echo ""
        echo "Missing required variables:"
        [ -z "$ADO_ORGANIZATION" ] && echo "  - ADO_ORGANIZATION (your Azure DevOps organization name)"
        [ -z "$ADO_PROJECT" ] && echo "  - ADO_PROJECT (your Azure DevOps project name)"
        [ -z "$ADO_PAT" ] && echo "  - ADO_PAT (your Personal Access Token)"
        VALIDATION_FAILED=true
    else
        echo "[STAX] ✓ Azure DevOps configuration validated"
    fi
elif [ "$PLATFORM_TYPE" = "JIRA" ]; then
    if [ -z "$JIRA_BASE_URL" ] || [ -z "$JIRA_USERNAME" ] || [ -z "$JIRA_TOKEN" ]; then
        echo ""
        echo "[STAX] ERROR: JIRA configuration incomplete!"
        echo ""
        echo "Missing required variables:"
        [ -z "$JIRA_BASE_URL" ] && echo "  - JIRA_BASE_URL (e.g., https://yourcompany.atlassian.net)"
        [ -z "$JIRA_USERNAME" ] && echo "  - JIRA_USERNAME (your email)"
        [ -z "$JIRA_TOKEN" ] && echo "  - JIRA_TOKEN (your API token)"
        VALIDATION_FAILED=true
    else
        echo "[STAX] ✓ JIRA configuration validated"
    fi
else
    echo "[STAX] ERROR: Invalid PLATFORM_TYPE '$PLATFORM_TYPE'. Must be 'ADO' or 'JIRA'"
    VALIDATION_FAILED=true
fi

# Validate AI service configuration
if [ -z "$AI_SERVICE_PROVIDER" ]; then
    echo ""
    echo "[STAX] ERROR: AI_SERVICE_PROVIDER not set!"
    echo "  Must be one of: OPENAI, AZURE_OPENAI, GITHUB"
    VALIDATION_FAILED=true
elif [ "$AI_SERVICE_PROVIDER" = "OPENAI" ]; then
    if [ -z "$OPENAI_API_KEY" ]; then
        echo ""
        echo "[STAX] ERROR: OpenAI configuration incomplete!"
        echo "  Missing: OPENAI_API_KEY"
        VALIDATION_FAILED=true
    else
        echo "[STAX] ✓ OpenAI configuration validated"
    fi
elif [ "$AI_SERVICE_PROVIDER" = "AZURE_OPENAI" ]; then
    if [ -z "$AZURE_OPENAI_ENDPOINT" ] || [ -z "$AZURE_OPENAI_API_KEY" ] || [ -z "$AZURE_OPENAI_DEPLOYMENT_NAME" ]; then
        echo ""
        echo "[STAX] ERROR: Azure OpenAI configuration incomplete!"
        echo ""
        echo "Missing required variables:"
        [ -z "$AZURE_OPENAI_ENDPOINT" ] && echo "  - AZURE_OPENAI_ENDPOINT (your Azure OpenAI endpoint URL)"
        [ -z "$AZURE_OPENAI_API_KEY" ] && echo "  - AZURE_OPENAI_API_KEY (your Azure OpenAI key)"
        [ -z "$AZURE_OPENAI_DEPLOYMENT_NAME" ] && echo "  - AZURE_OPENAI_DEPLOYMENT_NAME (your deployment name)"
        VALIDATION_FAILED=true
    else
        echo "[STAX] ✓ Azure OpenAI configuration validated"
    fi
elif [ "$AI_SERVICE_PROVIDER" = "GITHUB" ]; then
    if [ -z "$GITHUB_TOKEN" ] || [ -z "$GITHUB_MODEL" ]; then
        echo ""
        echo "[STAX] ERROR: GitHub Models configuration incomplete!"
        echo ""
        echo "Missing required variables:"
        [ -z "$GITHUB_TOKEN" ] && echo "  - GITHUB_TOKEN (your GitHub Personal Access Token)"
        [ -z "$GITHUB_MODEL" ] && echo "  - GITHUB_MODEL (e.g., gpt-4o-mini, gpt-4o)"
        VALIDATION_FAILED=true
    else
        echo "[STAX] ✓ GitHub Models configuration validated"
    fi
else
    echo ""
    echo "[STAX] ERROR: Invalid AI_SERVICE_PROVIDER '$AI_SERVICE_PROVIDER'"
    echo "  Must be one of: OPENAI, AZURE_OPENAI, GITHUB"
    VALIDATION_FAILED=true
fi

# If validation failed, show usage and exit
if [ "$VALIDATION_FAILED" = true ]; then
    show_usage
    exit 1
fi

echo ""
echo "[STAX] ✓ All configuration validated successfully"
echo "[STAX] Starting Monitor API on port 5001..."
echo ""

# Start the application
exec python -m src.monitor_api

#!/usr/bin/env python3
"""
Entry point script to run the MCP server
Usage: python run_mcp_server.py
"""
import sys
import asyncio
from pathlib import Path

# Add src directory to path
src_path = Path(__file__).parent / "src"
sys.path.insert(0, str(src_path))

from src.mcp_server import main

if __name__ == "__main__":
    print("Starting ADO Story & Test Case MCP Server...")
    print("Server will communicate via stdio (standard input/output)")
    print("Press Ctrl+C to stop the server")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\nError running server: {e}", file=sys.stderr)
        sys.exit(1)

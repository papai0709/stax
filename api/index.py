"""
Vercel entry point for STAX (Story & Test Automation eXtractor).
Exposes the Flask WSGI `app` for Vercel's Python serverless runtime.
"""

import sys
import os

# Make src/ and config/ importable from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.monitor_api_complete import create_app

# 'app' is the name Vercel looks for
app = create_app()

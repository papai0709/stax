import os
import logging

# Vercel / AWS Lambda sandbox: home dir is read-only.
# Point all SDK credential/cache writes to /tmp, which is always writable.
os.environ.setdefault('HOME', '/tmp')
os.environ.setdefault('XDG_CACHE_HOME', '/tmp/.cache')
os.environ.setdefault('XDG_CONFIG_HOME', '/tmp/.config')
os.environ.setdefault('AZURE_CONFIG_DIR', '/tmp/.azure')
os.environ.setdefault('ADAL_TOKEN_CACHE', '/tmp/.adal/cache.json')

from flask import Flask, jsonify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Lazy-initialised Flask app — avoids eager ADO/AI client setup at import time
# which would fail in serverless environments where credentials come from env vars.

_flask_app = None


def _build_app():
    global _flask_app
    if _flask_app is not None:
        return _flask_app
    try:
        from src.monitor_api_complete import create_app as _create
        _flask_app = _create()
        logger.info("MonitorAPI Flask app created successfully.")
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to initialise MonitorAPI: %s", exc, exc_info=True)
        # Capture exc explicitly — Python 3 deletes the 'as' variable after the
        # except block, so closures defined here cannot reference it directly.
        _init_error = str(exc)
        _flask_app = Flask(__name__)

        @_flask_app.route("/", defaults={"path": ""})
        @_flask_app.route("/<path:path>")
        def _error(path):
            return jsonify({"error": "Application failed to initialise", "detail": _init_error}), 500

    return _flask_app


# WSGI callable expected by Vercel / gunicorn / uwsgi
class _LazyApp:
    """Proxy that defers MonitorAPI construction until the first request."""

    def __call__(self, environ, start_response):
        return _build_app()(environ, start_response)


app = _LazyApp()


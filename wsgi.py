#!/usr/bin/env python3
"""Gunicorn entry point for AutoClaw Proxy.

Replaces Flask development server with Gunicorn + eventlet workers
for production deployment with proper concurrency, graceful restarts,
and resource limits.

Usage:
  gunicorn -c gunicorn_config.py wsgi:app

Or directly:
  python wsgi.py
"""

import os
import sys

# Ensure the autoclaw-autologin directory is on the path
_autoclaw_dir = os.path.dirname(os.path.abspath(__file__))
if _autoclaw_dir not in sys.path:
    sys.path.insert(0, _autoclaw_dir)

from proxy import app  # noqa: E402

if __name__ == "__main__":
    # Development fallback: run with Flask dev server
    from config import PROXY_HOST, PROXY_PORT
    app.run(host=PROXY_HOST, port=PROXY_PORT, debug=False)

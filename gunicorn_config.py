"""Gunicorn configuration for AutoClaw Proxy.

Production settings with eventlet workers for async request handling,
graceful timeouts, and resource limits.

Usage:
  gunicorn -c gunicorn_config.py wsgi:app
"""

import os
import multiprocessing

# ── Server Socket ────────────────────────────────────────────────────
bind = f"{os.environ.get('AUTOCLAW_PROXY_HOST', '127.0.0.1')}:" \
       f"{os.environ.get('AUTOCLAW_PROXY_PORT', '31000')}"

# ── Worker Processes ─────────────────────────────────────────────────
# eventlet workers for async I/O (Flask + requests library)
worker_class = "eventlet"
workers = int(os.environ.get("GUNICORN_WORKERS", str(min(multiprocessing.cpu_count(), 4))))
worker_connections = 1000  # max concurrent connections per eventlet worker
threads = 1  # eventlet is single-threaded per worker (uses greenlets)

# ── Timeouts ─────────────────────────────────────────────────────────
timeout = 120         # worker timeout (seconds) — LLM responses can be slow
graceful_timeout = 30 # graceful shutdown: finish in-flight requests
keepalive = 5         # HTTP Keep-Alive timeout
max_requests = 5000   # restart worker after N requests (prevents memory leaks)
max_requests_jitter = 500  # randomize to avoid all workers restarting at once

# ── Security ─────────────────────────────────────────────────────────
limit_request_line = 8190   # max size of HTTP request line
limit_request_fields = 100  # max number of header fields
limit_request_field_size = 8190  # max size of each header field

# ── Logging ──────────────────────────────────────────────────────────
accesslog = "-"       # log to stdout
errorlog = "-"        # log to stderr
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# ── Process Naming ───────────────────────────────────────────────────
proc_name = "autoclaw-proxy"

# ── Server Mechanics ─────────────────────────────────────────────────
preload_app = True    # load app code before forking workers (saves RAM)
daemon = False        # run in foreground (systemd manages daemonization)
tmp_upload_dir = None  # use default temp dir

# ── Hooks ────────────────────────────────────────────────────────────
def on_starting(server):
    """Log startup info."""
    pass

def when_ready(server):
    """Server is ready to accept connections."""
    print(f"[gunicorn] AutoClaw Proxy ready: {bind} ({workers} eventlet workers)")

def worker_int(worker):
    """Worker received INT/QUIT signal — graceful shutdown."""
    print(f"[gunicorn] Worker {worker.pid} shutting down gracefully")

def worker_abort(worker):
    """Worker timed out and was killed."""
    print(f"[gunicorn] Worker {worker.pid} aborted (timeout)")

#!/usr/bin/env python3
"""
Tiny local job runner so the n8n container (Docker) can start Players +/- jobs on this PC.

n8n calls  POST http://host.docker.internal:8765/run/<job>  with header  X-Runner-Token: <PM_RUNNER_TOKEN>
and gets 202 (started), 409 (that job is already running) or 401/404. GET /status lists the last runs.

Only the jobs in JOBS can be started (fixed argument lists, nothing from the request reaches the command
line). Listens on 127.0.0.1 only; Docker Desktop forwards host.docker.internal to it. The token lives in
.env (PM_RUNNER_TOKEN, created on first start) and in the n8n container's environment.
Logs: "Players PM/logs/<job>_<timestamp>.log".

Start hidden at log-on with start_pm_runner.vbs (shortcut in the Windows Startup folder), or by hand:
  venv/Scripts/python.exe -m players_pm.runner
"""

import datetime as dt
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .common import PM_DIR, PROJECT_DIR

PY = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
HOST, PORT = "127.0.0.1", int(os.environ.get("PM_RUNNER_PORT", "8765"))
JOBS = {
    "injuries": [PY, "-m", "players_pm.injury_report"],        # every 15 min on game days
    "refresh":  [PY, "-m", "players_pm.run", "--site"],         # full pipeline (box scores -> site build)
    "validate": [PY, "-m", "players_pm.validate"],
}
LOG_DIR = os.path.join(PM_DIR, "logs")
ENV_FILE = os.path.join(PROJECT_DIR, ".env")
running, history, lock = {}, [], threading.Lock()


def token():
    tok = os.environ.get("PM_RUNNER_TOKEN")
    if tok:
        return tok
    tok = secrets.token_urlsafe(32)
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"\nPM_RUNNER_TOKEN={tok}\n")
    os.environ["PM_RUNNER_TOKEN"] = tok
    return tok


def launch(name):
    os.makedirs(LOG_DIR, exist_ok=True)
    started = dt.datetime.now()
    log = os.path.join(LOG_DIR, f"{name}_{started:%Y%m%d_%H%M%S}.log")
    rec = {"job": name, "started": started.isoformat(timespec="seconds"), "log": log, "exit": None}

    def work():
        with open(log, "w", encoding="utf-8") as f:
            p = subprocess.run(JOBS[name], cwd=PROJECT_DIR, stdout=f, stderr=subprocess.STDOUT,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with lock:
            rec["exit"] = p.returncode
            rec["finished"] = dt.datetime.now().isoformat(timespec="seconds")
            running.pop(name, None)
            history.append(rec)
            del history[:-50]

    with lock:
        if name in running:
            return None
        running[name] = rec
    threading.Thread(target=work, daemon=True).start()
    return rec


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self):
        return hmac.compare_digest(self.headers.get("X-Runner-Token", ""), TOKEN)

    def do_GET(self):
        if not self._authorised():
            return self._send(401, {"error": "bad token"})
        if self.path.rstrip("/") == "/status":
            with lock:
                return self._send(200, {"running": list(running.values()), "recent": history[-10:]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorised():
            return self._send(401, {"error": "bad token"})
        parts = self.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "run" or parts[1] not in JOBS:
            return self._send(404, {"error": "unknown job", "jobs": sorted(JOBS)})
        rec = launch(parts[1])
        if rec is None:
            return self._send(409, {"error": "already running", "job": parts[1]})
        self._send(202, rec)

    def log_message(self, fmt, *args):
        if sys.stderr:                         # None under pythonw (hidden start)
            sys.stderr.write(f"{dt.datetime.now():%H:%M:%S} {self.address_string()} {fmt % args}\n")


TOKEN = None


def main():
    global TOKEN
    TOKEN = token()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Players +/- runner on http://{HOST}:{PORT} (jobs: {', '.join(JOBS)})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

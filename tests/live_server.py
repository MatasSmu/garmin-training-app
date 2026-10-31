"""Start the real app (uvicorn subprocess) on a free port with its own
throwaway database -- for the smoke and browser tests. Never data/garmin.db."""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from app import config

ROOT = Path(__file__).resolve().parents[1]
AUTH = (config.APP_USERNAME, config.APP_PASSWORD)
PHONE = {  # Playwright context options, roughly a Xiaomi 14T in Chrome
    "viewport": {"width": 393, "height": 851},
    "device_scale_factor": 2.75,
    "is_mobile": True,
    "has_touch": True,
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def live_server(db_path: Path, import_plan: bool = True):
    env = {**os.environ, "APP_DB_PATH": str(db_path)}
    if import_plan:
        subprocess.run([sys.executable, "-m", "app.gym.plan_import"], cwd=ROOT, env=env, check=True,
                       capture_output=True)
    port = _free_port()
    log = open(db_path.with_suffix(".log"), "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            if proc.poll() is not None:
                raise RuntimeError("app exited on start:\n" + db_path.with_suffix(".log").read_text())
            try:
                if httpx.get(base + "/healthz", auth=AUTH, timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                raise RuntimeError("app did not start within 30 s")
            time.sleep(0.2)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

"""Start the complete local stack from PyCharm or the command line."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".devflow"


def wait_for(url: str, process: subprocess.Popen | None = None, timeout: int = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Service exited with code {process.returncode}; see {RUNTIME}")
        try:
            with urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, TimeoutError):
            pass
        time.sleep(1)
    raise RuntimeError(f"Service did not become ready: {url}; see {RUNTIME}")


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    def stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    os.environ.setdefault("NEXT_TELEMETRY_DISABLED", "1")
    os.environ["PATH"] = os.pathsep.join([
        str(Path.home() / ".docker/bin"), "/opt/homebrew/bin", os.environ.get("PATH", "")
    ])
    docker, node = shutil.which("docker"), shutil.which("node")
    if not docker or not node:
        raise RuntimeError("Docker and Node.js must be installed.")
    for port in (8001, 3000):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"Port {port} is already in use. Stop the previous DevFlow run first.")
    RUNTIME.mkdir(exist_ok=True)
    subprocess.run([docker, "compose", "-f", "docker-compose.local.yml", "up", "-d", "--wait"],
                   cwd=ROOT, check=True)
    wait_for("http://127.0.0.1:9092/healthz")
    children: list[subprocess.Popen] = []
    logs = []
    try:
        for name, command, cwd, url in [
            ("backend", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001"],
             ROOT / "backend", "http://127.0.0.1:8001/health"),
            ("frontend", [node, "node_modules/next/dist/bin/next", "dev", "--hostname", "127.0.0.1", "--port", "3000"],
             ROOT / "frontend", "http://127.0.0.1:3000"),
        ]:
            log = (RUNTIME / f"{name}.log").open("a")
            logs.append(log)
            child = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
            children.append(child)
            wait_for(url, child)
            print(f"{name} ready: {url}", flush=True)
        print("DevFlow AI is running: http://127.0.0.1:3000", flush=True)
        print("API documentation: http://127.0.0.1:8001/docs", flush=True)
        print(f"Logs: {RUNTIME}\nStop this run to stop the frontend and backend.", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(1)
        raise RuntimeError("A service stopped unexpectedly; inspect its log.")
    except KeyboardInterrupt:
        print("Stopping DevFlow frontend and backend...", flush=True)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()

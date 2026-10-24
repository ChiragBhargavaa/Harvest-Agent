"""Supervisor: run all 6 LeadReach agents as subprocesses and restart any that exit.

    python scripts/run_all.py      # Ctrl+C / SIGTERM stops every agent

Each agent runs with the current Python, cwd = repo root, and this process's environment. Output lines are
prefixed with the agent's name. A child that exits is restarted after a capped exponential backoff; one that
exits more than MAX_RESTARTS times within RESTART_WINDOW_S seconds is given up on (usually its port is in use).
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ["orchestrator", "lead_finder", "contact_finder", "lead_analyst", "copywriter", "sender"]
PORTS = dict(zip(AGENTS, range(8001, 8007), strict=True))  # the agents' default *_PORT values
MAX_RESTARTS, RESTART_WINDOW_S = 5, 300.0


def _forward(name: str, proc: subprocess.Popen) -> None:
    for line in proc.stdout:
        print(f"[{name}] {line.rstrip()}", flush=True)


def _start(name: str, cmd: list[str]) -> subprocess.Popen:
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    threading.Thread(target=_forward, args=(name, proc), daemon=True).start()
    return proc


def give_up_message(name: str) -> str:
    port = PORTS.get(name)
    hint = f" (port {port} in use? check `lsof -iTCP:{port} -sTCP:LISTEN`)" if port else ""
    return f"[supervisor] {name} keeps exiting{hint}; giving up"


def supervise(cmds: dict[str, list[str]], stop: threading.Event, base_delay: float = 1.0, max_delay: float = 60.0,
              max_restarts: int | None = None, poll: float = 0.5, window: float | None = None) -> dict[str, int]:
    """Run every command until `stop` is set; restart exits with backoff. Returns restart counts per name.
    max_restarts caps restarts per name: in total, or within the last `window` seconds if a window is given."""
    procs = {name: _start(name, cmd) for name, cmd in cmds.items()}
    restarts = dict.fromkeys(cmds, 0)
    recent: dict[str, list[float]] = {name: [] for name in cmds}  # monotonic restart times
    gave_up: set[str] = set()
    due: dict[str, float] = {}  # name -> monotonic time to restart at
    try:
        while not stop.is_set():
            now = time.monotonic()
            for name, proc in procs.items():
                if name in due or name in gave_up or proc.poll() is None:
                    continue
                if window is not None:
                    recent[name] = [t for t in recent[name] if now - t < window]
                count = len(recent[name]) if window is not None else restarts[name]
                if max_restarts is not None and count >= max_restarts:
                    gave_up.add(name)
                    print(give_up_message(name), flush=True)
                    continue
                delay = min(max_delay, base_delay * 2 ** restarts[name])
                print(f"[supervisor] {name} exited ({proc.returncode}); restarting in {delay:.1f}s", flush=True)
                due[name] = now + delay
            for name, at in list(due.items()):
                if now >= at:
                    del due[name]
                    restarts[name] += 1
                    recent[name].append(now)
                    procs[name] = _start(name, cmds[name])
            stop.wait(poll)
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in procs.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return restarts


def main() -> None:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    cmds = {name: [sys.executable, str(ROOT / "agents" / f"{name}.py")] for name in AGENTS}
    supervise(cmds, stop, max_restarts=MAX_RESTARTS, window=RESTART_WINDOW_S)
    print("[supervisor] all agents stopped", flush=True)


if __name__ == "__main__":
    main()

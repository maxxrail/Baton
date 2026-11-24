"""Run the Baton API and the Northwind mock commerce API together.

    uv run python -m scripts.dev              # API on :8000, mock commerce on :8300
    uv run python -m scripts.dev --keep-data  # don't reseed the mock store
    uv run python -m scripts.dev --reload     # auto-reload on code changes

Works on Windows: plain subprocesses, no shell, Ctrl+C stops both.
"""

import argparse
import socket
import subprocess
import sys
import time

from mock_commerce.db import make_session_factory
from mock_commerce.seed import reset_and_seed

BATON_PORT = 8000
MOCK_COMMERCE_PORT = 8300


def _uvicorn(target: str, port: int, reload: bool) -> list[str]:
    cmd = [sys.executable, "-m", "uvicorn", target, "--factory", "--port", str(port)]
    # No uvicorn access log: it prints raw query strings (e.g. ?email=...) without redaction.
    # Baton's middleware logs every request through the redacting structlog pipeline instead.
    cmd += ["--host", "127.0.0.1", "--no-access-log"]
    if reload:
        cmd += ["--reload", "--reload-dir", "src"]
    return cmd


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--keep-data", action="store_true", help="keep the mock store's data")
    parser.add_argument("--reload", action="store_true", help="reload on code changes")
    parser.add_argument("--baton-port", type=int, default=BATON_PORT)
    parser.add_argument("--mock-port", type=int, default=MOCK_COMMERCE_PORT)
    args = parser.parse_args(argv)

    busy = [p for p in (args.baton_port, args.mock_port) if _port_in_use(p)]
    if busy:
        print(
            f"port(s) {busy} already in use. Stop whatever is using them, or pick others with "
            "--baton-port / --mock-port.",
            file=sys.stderr,
        )
        return 2

    if not args.keep_data:
        reset_and_seed(make_session_factory())
        print("mock commerce: reset and seeded")

    procs = [
        subprocess.Popen(_uvicorn("mock_commerce.app:create_app", args.mock_port, args.reload)),
        subprocess.Popen(_uvicorn("baton.api.app:create_app", args.baton_port, args.reload)),
    ]
    print(f"baton API      -> http://localhost:{args.baton_port}/healthz")
    print(f"mock commerce  -> http://localhost:{args.mock_port}/docs")
    print("Ctrl+C to stop")
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        return next(p.returncode for p in procs if p.poll() is not None) or 1
    except KeyboardInterrupt:
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())

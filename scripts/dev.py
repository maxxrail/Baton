"""Run the Baton API, the Northwind mock commerce API and the web app (Vite) together.

    uv run python -m scripts.dev              # API :8000, mock store :8300, web :5173
    uv run python -m scripts.dev --keep-data  # don't reseed the mock store
    uv run python -m scripts.dev --reload     # auto-reload on code changes
    uv run python -m scripts.dev --no-web     # skip Vite (e.g. no Node installed)

Then open http://localhost:5173/demo-store.html (chat) and http://localhost:5173/console.

Channel secrets that aren't configured in .env get dev-only defaults here (console secret,
Twilio auth token, email webhook secret, SMS link secret), so the console and
`scripts.simulate_inbound` work out of the box. They are injected only into these child
processes; a real deployment that forgets them gets 503s instead (see `baton.api.auth` and
`baton.api.channels`).

Works on Windows: plain subprocesses, no shell, Ctrl+C stops everything.
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from baton.config import REPO_ROOT, Settings
from mock_commerce.db import make_session_factory
from mock_commerce.seed import reset_and_seed

BATON_PORT = 8000
MOCK_COMMERCE_PORT = 8300
WEB_PORT = 5173
WEB_DIR = REPO_ROOT / "web"

# Dev-only defaults for secrets .env doesn't set. Never used outside this runner and
# scripts.simulate_inbound.
DEV_CONSOLE_SECRET = "dev-console-secret"
DEV_TWILIO_TOKEN = "dev-twilio-token"
DEV_EMAIL_SECRET = "dev-email-secret"
DEV_LINK_SECRET = "dev-link-secret"


def dev_secrets(settings: Settings) -> dict[str, str]:
    """Env vars to add for secrets that aren't configured anywhere."""
    out: dict[str, str] = {}
    if settings.baton_console_secret is None:
        out["BATON_CONSOLE_SECRET"] = DEV_CONSOLE_SECRET
    if settings.twilio_auth_token is None:
        out["TWILIO_AUTH_TOKEN"] = DEV_TWILIO_TOKEN
    if settings.email_webhook_secret is None:
        out["EMAIL_WEBHOOK_SECRET"] = DEV_EMAIL_SECRET
    if settings.baton_link_secret is None:
        out["BATON_LINK_SECRET"] = DEV_LINK_SECRET
    return out


def find_npm() -> str | None:
    found = shutil.which("npm")
    if found:
        return found
    # Node's Windows installer doesn't always reach PATH in every shell.
    candidate = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "nodejs" / "npm.cmd"
    return str(candidate) if candidate.exists() else None


def _vite(npm: str, port: int) -> list[str]:
    return [npm, "run", "dev", "--", "--port", str(port), "--strictPort"]


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
    parser.add_argument("--web-port", type=int, default=WEB_PORT)
    parser.add_argument("--no-web", action="store_true", help="don't start the Vite dev server")
    args = parser.parse_args(argv)

    ports = [args.baton_port, args.mock_port] + ([] if args.no_web else [args.web_port])
    busy = [p for p in ports if _port_in_use(p)]
    if busy:
        print(
            f"port(s) {busy} already in use. Stop whatever is using them, or pick others with "
            "--baton-port / --mock-port / --web-port.",
            file=sys.stderr,
        )
        return 2

    npm = None if args.no_web else find_npm()
    web = npm is not None and (WEB_DIR / "node_modules").exists()
    if not args.no_web and not web:
        print(
            "web app skipped: "
            + ("npm not found" if npm is None else "run `cd web && npm install`"),
            file=sys.stderr,
        )

    if not args.keep_data:
        reset_and_seed(make_session_factory())
        print("mock commerce: reset and seeded")

    secrets = dev_secrets(Settings())
    api_env = {**os.environ, **secrets, "BATON_WEB_URL": f"http://localhost:{args.web_port}"}
    procs = [
        subprocess.Popen(_uvicorn("mock_commerce.app:create_app", args.mock_port, args.reload)),
        subprocess.Popen(
            _uvicorn("baton.api.app:create_app", args.baton_port, args.reload), env=api_env
        ),
    ]
    if web and npm:
        web_env = {**os.environ, "BATON_API_URL": f"http://localhost:{args.baton_port}"}
        procs.append(subprocess.Popen(_vite(npm, args.web_port), cwd=WEB_DIR, env=web_env))
    print(f"baton API      -> http://localhost:{args.baton_port}/healthz")
    print(f"mock commerce  -> http://localhost:{args.mock_port}/docs")
    if web:
        print(f"demo store     -> http://localhost:{args.web_port}/demo-store.html")
        print(f"console        -> http://localhost:{args.web_port}/console")
    if "BATON_CONSOLE_SECRET" in secrets:
        print(f"console secret -> {DEV_CONSOLE_SECRET} (dev default; set BATON_CONSOLE_SECRET)")
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

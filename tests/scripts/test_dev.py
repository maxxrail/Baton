import socket

import pytest

from scripts import dev


def test_uvicorn_command_uses_current_python_and_factory() -> None:
    cmd = dev._uvicorn("baton.api.app:create_app", 8000, reload=False)
    assert cmd[1:3] == ["-m", "uvicorn"]
    assert "--factory" in cmd
    assert cmd[cmd.index("--port") + 1] == "8000"
    assert "--reload" not in cmd
    assert "--no-access-log" in cmd  # uvicorn access logs would bypass PII redaction
    assert "--reload" in dev._uvicorn("x:y", 1, reload=True)


def test_refuses_to_start_when_a_port_is_taken(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        busy = sock.getsockname()[1]
        assert dev._port_in_use(busy)
        assert dev.main(["--baton-port", str(busy), "--keep-data"]) == 2
    assert "already in use" in capsys.readouterr().err


def test_dev_secrets_only_fill_what_is_not_configured() -> None:
    from baton.config import Settings

    bare = Settings(_env_file=None)  # type: ignore[call-arg]
    assert dev.dev_secrets(bare) == {
        "BATON_CONSOLE_SECRET": dev.DEV_CONSOLE_SECRET,
        "TWILIO_AUTH_TOKEN": dev.DEV_TWILIO_TOKEN,
        "EMAIL_WEBHOOK_SECRET": dev.DEV_EMAIL_SECRET,
        "BATON_LINK_SECRET": dev.DEV_LINK_SECRET,
    }
    configured = Settings(  # type: ignore[call-arg]
        _env_file=None, baton_console_secret="real", twilio_auth_token="real"
    )
    assert dev.dev_secrets(configured) == {
        "EMAIL_WEBHOOK_SECRET": dev.DEV_EMAIL_SECRET,
        "BATON_LINK_SECRET": dev.DEV_LINK_SECRET,
    }


def test_vite_command_pins_the_port() -> None:
    assert dev._vite("npm", 5173) == ["npm", "run", "dev", "--", "--port", "5173", "--strictPort"]

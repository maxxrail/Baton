import shutil
from pathlib import Path

import pytest

from baton.aop.cli import main
from baton.config import get_settings

REPO = Path(__file__).resolve().parents[2]
AOPS = REPO / "aops"
BAD = Path(__file__).parent / "fixtures" / "bad"


def test_validate_clean_directory_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dir", str(AOPS), "validate"]) == 0
    assert "8 AOP(s) checked: 0 error(s), 0 warning(s)" in capsys.readouterr().out


def test_validate_bad_directory_exits_nonzero_and_lists_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--dir", str(BAD), "validate"]) == 1
    out = capsys.readouterr().out
    for code in ("UNKNOWN_TOOL", "BAD_TARGET", "NO_TERMINAL", "PARSE_ERROR", "LONG_STEP"):
        assert code in out
    assert "error   bad_target:1" in out


def test_validate_with_only_warnings_exits_zero(tmp_path: Path) -> None:
    shutil.copy(BAD / "long_step.md", tmp_path / "long_step.md")
    assert main(["--dir", str(tmp_path), "validate"]) == 0


def test_validate_empty_directory_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dir", str(tmp_path), "validate"]) == 1
    assert "no AOP files" in capsys.readouterr().err


def test_missing_directory_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dir", str(tmp_path / "nope"), "validate"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_show_prints_steps_and_transitions(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dir", str(AOPS), "show", "return_item"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("return_item@1  Return or exchange an item")
    assert "-> RESOLVE  (if the order is not delivered)" in out
    assert "tools: create_return" in out
    assert "step prompt" not in out


def test_show_prompts(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dir", str(AOPS), "show", "cancel_order", "--prompts"]) == 0
    out = capsys.readouterr().out
    assert "----- step 4 prompt -----" in out
    assert "### Policies (always apply)" in out


def test_show_bad_aop_lists_its_diagnostics_and_refuses_prompts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--dir", str(BAD), "show", "unknown_tool", "--prompts"]) == 1
    out = capsys.readouterr().out
    assert "UNKNOWN_TOOL" in out
    assert "fix the errors above first" in out


def test_unknown_aop_lists_known_ids(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dir", str(AOPS), "graph", "nope"]) == 1
    assert "Known: cancel_order" in capsys.readouterr().err


def test_compile_records_versions_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from alembic import command
    from alembic.config import Config

    from baton.db import session

    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    session.default_session_factory.cache_clear()
    try:
        assert main(["--dir", str(AOPS), "compile"]) == 0
        assert "8 new version(s)" in capsys.readouterr().out
        assert main(["--dir", str(AOPS), "compile"]) == 0
        assert "0 new version(s)" in capsys.readouterr().out
    finally:
        session.default_session_factory.cache_clear()


def test_compile_on_unmigrated_db_explains_what_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from baton.db import session

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
    get_settings.cache_clear()
    session.default_session_factory.cache_clear()
    try:
        assert main(["--dir", str(AOPS), "compile"]) == 1
        assert "alembic upgrade head" in capsys.readouterr().err
    finally:
        session.default_session_factory.cache_clear()


def test_compile_bad_directory_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--dir", str(BAD), "compile"]) == 1
    assert "compile failed" in capsys.readouterr().out

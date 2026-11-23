from pathlib import Path

import pytest

from baton.prompts import PromptNotFoundError, load_prompt, prompt_version

REPO = Path(__file__).resolve().parents[1]


def test_load_prompt_from_repo() -> None:
    prompt = load_prompt("router", REPO / "prompts")
    assert prompt.name == "router"
    assert prompt.version.startswith("router@")
    assert len(prompt.version) == len("router@") + 8


def test_version_tracks_content_not_line_endings(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_bytes(b"Hello $name\n")
    lf = load_prompt("p", tmp_path).version
    (tmp_path / "p.md").write_bytes(b"Hello $name\r\n")
    assert load_prompt("p", tmp_path).version == lf
    (tmp_path / "p.md").write_bytes(b"Hi $name\n")
    assert load_prompt("p", tmp_path).version != lf
    assert lf == prompt_version("p", "Hello $name\n")


def test_render_fills_placeholders_and_rejects_missing(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text("Refund up to $$100 for $name", encoding="utf-8")
    prompt = load_prompt("p", tmp_path)
    assert prompt.render(name="$5 off") == "Refund up to $100 for $5 off"
    with pytest.raises(KeyError):
        prompt.render()


def test_missing_prompt(tmp_path: Path) -> None:
    with pytest.raises(PromptNotFoundError, match="nope"):
        load_prompt("nope", tmp_path)


def test_default_dir_comes_from_settings() -> None:
    assert load_prompt("aop_step").name == "aop_step"

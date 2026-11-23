"""AOP versioning: recompiling unchanged AOPs adds no rows; editing one adds exactly one."""

import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from baton.aop.compiler import compile_aops
from baton.aop.store import load_version, sync_versions
from baton.db import AOPVersion, make_engine, make_session_factory, session_scope

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    url = f"sqlite:///{(tmp_path / 'v.db').as_posix()}"
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")  # the real migrations, not create_all
    return make_session_factory(make_engine(url))


@pytest.fixture
def aop_dir(tmp_path: Path) -> Path:
    target = tmp_path / "aops"
    shutil.copytree(REPO / "aops", target)
    return target


def _count(factory: sessionmaker[Session]) -> int:
    with session_scope(factory) as s:
        return s.scalar(select(func.count()).select_from(AOPVersion)) or 0


def test_first_compile_stores_every_aop(factory: sessionmaker[Session], aop_dir: Path) -> None:
    bundle = compile_aops(aop_dir, prompts_dir=REPO / "prompts")
    with session_scope(factory) as s:
        result = sync_versions(s, bundle)
    assert len(result.created) == 8
    assert "return_item@1" in result.created
    assert _count(factory) == 8


def test_recompiling_unchanged_aops_creates_no_versions(
    factory: sessionmaker[Session], aop_dir: Path
) -> None:
    with session_scope(factory) as s:
        sync_versions(s, compile_aops(aop_dir, prompts_dir=REPO / "prompts"))
    with session_scope(factory) as s:
        result = sync_versions(s, compile_aops(aop_dir, prompts_dir=REPO / "prompts"))
    assert result.created == []
    assert len(result.unchanged) == 8
    assert _count(factory) == 8


def test_editing_one_aop_creates_exactly_one_version(
    factory: sessionmaker[Session], aop_dir: Path
) -> None:
    with session_scope(factory) as s:
        sync_versions(s, compile_aops(aop_dir, prompts_dir=REPO / "prompts"))

    path = aop_dir / "cancel_order.md"
    path.write_text(path.read_text().replace("version: 1", "version: 2"))
    with session_scope(factory) as s:
        result = sync_versions(s, compile_aops(aop_dir, prompts_dir=REPO / "prompts"))
    assert result.created == ["cancel_order@2"]
    assert result.warnings == []
    assert _count(factory) == 9

    with session_scope(factory) as s:  # and once more: still nothing new
        assert sync_versions(s, compile_aops(aop_dir, prompts_dir=REPO / "prompts")).created == []


def test_edit_without_version_bump_is_stored_and_warned(
    factory: sessionmaker[Session], aop_dir: Path
) -> None:
    with session_scope(factory) as s:
        sync_versions(s, compile_aops(aop_dir, prompts_dir=REPO / "prompts"))
    path = aop_dir / "loyalty_points.md"
    path.write_text(path.read_text().replace("Points can't", "Loyalty points can't"))
    with session_scope(factory) as s:
        result = sync_versions(s, compile_aops(aop_dir, prompts_dir=REPO / "prompts"))
    assert result.created == ["loyalty_points@1"]
    assert len(result.warnings) == 1
    assert "bump 'version'" in result.warnings[0]


def test_line_ending_changes_are_not_a_new_version(
    factory: sessionmaker[Session], aop_dir: Path
) -> None:
    with session_scope(factory) as s:
        sync_versions(s, compile_aops(aop_dir, prompts_dir=REPO / "prompts"))
    path = aop_dir / "return_item.md"
    lf = path.read_bytes().replace(b"\r\n", b"\n")
    path.write_bytes(lf.replace(b"\n", b"\r\n"))  # the same file as a Windows checkout
    with session_scope(factory) as s:
        assert sync_versions(s, compile_aops(aop_dir, prompts_dir=REPO / "prompts")).created == []


def test_stored_json_replays_the_exact_compiled_aop(
    factory: sessionmaker[Session], aop_dir: Path
) -> None:
    bundle = compile_aops(aop_dir, prompts_dir=REPO / "prompts")
    with session_scope(factory) as s:
        sync_versions(s, bundle)
    original = bundle.get("refund_request")
    with session_scope(factory) as s:
        pv = original.step_prompt_version
        assert load_version(s, "refund_request", original.source_hash, pv) == original
        assert load_version(s, "refund_request", "0" * 64, pv) is None


def test_database_rejects_duplicate_hashes(factory: sessionmaker[Session]) -> None:
    def add_twice() -> None:
        with session_scope(factory) as s:
            for _ in range(2):
                s.add(
                    AOPVersion(
                        aop_id="x",
                        version=1,
                        source_hash="h",
                        step_prompt_version="p",
                        compiled={},
                    )
                )

    with pytest.raises(IntegrityError):
        add_twice()


def test_step_prompt_template_change_stores_new_rows_without_bump_warnings(
    factory: sessionmaker[Session], aop_dir: Path, tmp_path: Path
) -> None:
    with session_scope(factory) as s:
        sync_versions(s, compile_aops(aop_dir, prompts_dir=REPO / "prompts"))
    prompts = tmp_path / "prompts"
    shutil.copytree(REPO / "prompts", prompts)
    template = prompts / "aop_step.md"
    template.write_text(template.read_text(encoding="utf-8") + "\nBe brief.\n", encoding="utf-8")
    bundle = compile_aops(aop_dir, prompts_dir=prompts)
    with session_scope(factory) as s:
        result = sync_versions(s, bundle)
    assert len(result.created) == 8  # the stored prompts must match what the agent sees
    assert result.warnings == []  # the AOP files didn't change, so no "bump version" nag
    aop = bundle.get("return_item")
    with session_scope(factory) as s:
        stored = load_version(s, aop.id, aop.source_hash, aop.step_prompt_version)
    assert stored is not None
    assert stored.step(1).prompt.endswith("Be brief.\n")

"""`baton-kb ingest` / `search` against a temp database."""

from pathlib import Path

import pytest

from baton.db import Base, make_engine
from baton.knowledge import cli


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{(tmp_path / 'baton.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def test_ingest_is_idempotent_and_search_explains_ranking(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    Base.metadata.create_all(make_engine(database))
    assert cli.main(["ingest"]) == 0
    first = capsys.readouterr().out
    assert "28 articles, 30 chunks: 30 embedded" in first
    assert cli.main(["ingest"]) == 0
    assert "0 embedded, 30 unchanged, 0 removed" in capsys.readouterr().out

    assert cli.main(["search", "is shipping free over 50 dollars?", "-k", "3"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[1].startswith("1. [kb:shipping-costs]")
    assert "newer policy shipping-costs ranked above free-shipping-offer-2025" in out


def test_unmigrated_database_is_a_clear_error(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["ingest"]) == 2
    assert "alembic upgrade head" in capsys.readouterr().err


def test_bad_article_names_the_file(
    database: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    Base.metadata.create_all(make_engine(database))
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "broken.md").write_text("no frontmatter", encoding="utf-8")
    assert cli.main(["ingest", "--kb-dir", str(kb)]) == 2
    assert "broken.md" in capsys.readouterr().err

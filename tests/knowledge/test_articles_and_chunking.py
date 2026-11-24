from datetime import date
from pathlib import Path

import pytest

from baton.knowledge.articles import KBFormatError, load_articles, parse_article
from baton.knowledge.chunking import (
    MAX_TOKENS,
    chunk_article,
    estimate_tokens,
    split_sections,
)
from tests.knowledge.conftest import KB_DIR

GOOD = """---
id: sample
title: Sample article
category: shipping
updated_at: 2026-01-02
---
# Sample article

Intro paragraph.

## First section

First body.

### Nested

Nested body.

## Second section

Second body.
"""


def _article(text: str = GOOD, name: str = "sample.md"):  # type: ignore[no-untyped-def]
    return parse_article(text, Path(name))


def test_the_help_center_loads_and_has_the_planned_shape() -> None:
    articles = load_articles(KB_DIR)
    assert 25 <= len(articles) <= 30
    assert len({a.id for a in articles}) == len(articles)
    assert {a.meta.category for a in articles} >= {"shipping", "returns", "loyalty", "payments"}
    # The two deliberate conflicts: same topic, older vs newer policy.
    by_id = {a.id: a for a in articles}
    assert by_id["return-policy"].meta.updated_at > by_id["return-policy-2024"].meta.updated_at
    assert (
        by_id["shipping-costs"].meta.updated_at > by_id["free-shipping-offer-2025"].meta.updated_at
    )
    assert "30 days" in by_id["return-policy"].body
    assert "60 days" in by_id["return-policy-2024"].body


def test_parse_reads_frontmatter() -> None:
    article = _article()
    assert article.id == "sample"
    assert article.meta.updated_at == date(2026, 1, 2)
    assert article.body.startswith("# Sample article")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# no frontmatter", "missing frontmatter"),
        ("---\nid: sample\n", "not closed"),
        (GOOD.replace("category: shipping\n", ""), "invalid frontmatter"),
        (GOOD.replace("category: shipping", "category: shipping\nauthor: me"), "invalid"),
        (GOOD.replace("id: sample", "id: Sample Article"), "invalid frontmatter"),
        (GOOD.replace("id: sample", "id: other"), "must match the file name"),
        (GOOD.split("# Sample")[0], "no content"),
    ],
)
def test_bad_articles_fail_with_the_file_name(text: str, message: str) -> None:
    with pytest.raises(KBFormatError, match=message) as info:
        _article(text)
    assert "sample.md" in str(info.value)


def test_readme_is_not_an_article(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# not an article", encoding="utf-8")
    (tmp_path / "sample.md").write_text(GOOD, encoding="utf-8")
    assert [a.id for a in load_articles(tmp_path)] == ["sample"]


def test_sections_keep_the_heading_path() -> None:
    paths = [s.path for s in split_sections(_article())]
    assert paths == [
        ("Sample article",),
        ("Sample article", "First section"),
        ("Sample article", "First section", "Nested"),
        ("Sample article", "Second section"),
    ]


def test_a_short_article_is_one_chunk_titled_by_its_common_path() -> None:
    (chunk,) = chunk_article(_article())
    assert chunk.heading_path == ("Sample article",)
    assert "## First section" in chunk.text
    assert "Second body." in chunk.text
    assert chunk.embed_text().startswith("Sample article\n")


def _long_article() -> str:
    paragraph = " ".join(["word"] * 60)
    sections = "\n\n".join(
        f"## Section {i}\n\n{paragraph}\n\n{paragraph}" for i in range(1, 7)
    )  # ~6 x 160 tokens
    return GOOD.split("# Sample")[0] + f"# Sample article\n\n{sections}\n"


def test_long_articles_split_at_headings_within_the_token_band() -> None:
    chunks = chunk_article(_article(_long_article()))
    assert len(chunks) >= 3
    for chunk in chunks:
        assert estimate_tokens(chunk.text) <= MAX_TOKENS
        assert chunk.text.startswith("## Section")  # never starts mid-section
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_an_oversized_section_splits_at_paragraphs_and_repeats_its_heading() -> None:
    paragraph = " ".join(["word"] * 120)
    body = "\n\n".join([paragraph] * 6)  # ~960 tokens in one section
    text = GOOD.split("# Sample")[0] + f"# Sample article\n\n## Huge\n\n{body}\n"
    chunks = chunk_article(_article(text))
    assert len(chunks) >= 2
    assert all(c.text.startswith("## Huge") for c in chunks)
    assert all(c.heading_path == ("Sample article", "Huge") for c in chunks)
    assert all(estimate_tokens(c.text) <= MAX_TOKENS for c in chunks)


def test_content_hash_tracks_text_and_embedder() -> None:
    (chunk,) = chunk_article(_article())
    (edited,) = chunk_article(_article(GOOD.replace("Second body.", "Changed body.")))
    (redated,) = chunk_article(_article(GOOD.replace("2026-01-02", "2026-03-04")))
    assert chunk.content_hash("e1") != edited.content_hash("e1")
    assert chunk.content_hash("e1") != chunk.content_hash("e2")
    assert chunk.content_hash("e1") == redated.content_hash("e1")  # a date isn't re-embedded

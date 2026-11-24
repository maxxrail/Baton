"""Help-center articles: Markdown files with YAML frontmatter in `data/kb/`.

    ---
    id: return-policy            # must match the file name (return-policy.md)
    title: Return policy
    category: returns
    updated_at: 2026-02-01       # used to prefer the newer article when two conflict
    ---
    # Return policy
    ...

Every key is required and unknown keys are rejected, so a typo fails ingestion instead of
silently dropping metadata. `README.md` in the folder is not an article.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ARTICLE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


class KBFormatError(ValueError):
    """An article file is malformed. The message names the file."""


class ArticleMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ARTICLE_ID_PATTERN, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=50)
    updated_at: date


@dataclass(frozen=True)
class Article:
    meta: ArticleMeta
    body: str
    path: Path

    @property
    def id(self) -> str:
        return self.meta.id


def parse_article(text: str, path: Path) -> Article:
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise KBFormatError(f"{path.name}: missing frontmatter (the file must start with ---)")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise KBFormatError(f"{path.name}: frontmatter is not closed with ---")
    try:
        raw = yaml.safe_load(text[4:end]) or {}
        meta = ArticleMeta.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise KBFormatError(f"{path.name}: invalid frontmatter: {exc}") from None
    if meta.id != path.stem:
        raise KBFormatError(f"{path.name}: id {meta.id!r} must match the file name")
    body = text[end + len("\n---\n") :].strip()
    if not body:
        raise KBFormatError(f"{path.name}: the article has no content")
    return Article(meta=meta, body=body, path=path)


def load_articles(kb_dir: Path) -> list[Article]:
    """Every article in `kb_dir`, sorted by id. Raises `KBFormatError` on the first bad file."""
    if not kb_dir.is_dir():
        raise KBFormatError(f"knowledge base folder not found: {kb_dir}")
    paths = sorted(p for p in kb_dir.glob("*.md") if p.name.lower() != "readme.md")
    return [parse_article(p.read_text(encoding="utf-8"), p) for p in paths]

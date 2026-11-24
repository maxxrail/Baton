"""`baton-kb`: manage the help-center index.

    uv run baton-kb ingest              # (re-)index data/kb/*.md into kb_chunks, incrementally
    uv run baton-kb search "question"   # what the agent would retrieve, with scores

Uses DATABASE_URL, KB_DIR and the configured embedder (BATON_EMBEDDER). The database must be
migrated first (`uv run alembic upgrade head`).
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import inspect

from baton.config import get_settings
from baton.db.session import make_engine, make_session_factory, session_scope
from baton.knowledge.articles import KBFormatError
from baton.knowledge.base import KnowledgeBase
from baton.knowledge.ingest import ingest
from baton.knowledge.retrieve import KnowledgeIndexError
from baton.llm import EmbeddingError, LLMConfigError, get_embedder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baton-kb", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ingest_cmd = sub.add_parser("ingest", help="index the help-center articles")
    ingest_cmd.add_argument("--kb-dir", type=Path, default=None, help="default: KB_DIR")
    search_cmd = sub.add_parser("search", help="show what retrieval returns for a question")
    search_cmd.add_argument("query")
    search_cmd.add_argument("-k", type=int, default=5)
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = make_engine(settings.database_url)
    if not inspect(engine).has_table("kb_chunks"):
        print(
            "baton-kb: the database isn't migrated: run `uv run alembic upgrade head`",
            file=sys.stderr,
        )
        return 2
    sessions = make_session_factory(engine)
    try:
        embedder = get_embedder(settings)
        if args.command == "ingest":
            kb_dir = args.kb_dir or settings.kb_dir
            with session_scope(sessions) as session:
                report = ingest(session, kb_dir, embedder)
            print(f"ingested {kb_dir}: {report.summary()}")
            if report.changed_articles:
                print("changed: " + ", ".join(report.changed_articles))
            return 0
        kb = KnowledgeBase(sessions, embedder, base_url=settings.baton_kb_base_url)
        result = kb.search(args.query, top_k=args.k)
    except (KBFormatError, KnowledgeIndexError, LLMConfigError, EmbeddingError) as exc:
        print(f"baton-kb: {exc}", file=sys.stderr)
        return 2
    if not result.hits:
        print("no results (is the index empty? run `uv run baton-kb ingest`)")
        return 0
    print(f"covered={result.covered} (coverage {result.coverage:.2f})")
    for rank, hit in enumerate(result.hits, start=1):
        print(
            f"{rank}. [kb:{hit.chunk.article_id}] {hit.chunk.section}  "
            f"(rrf {hit.score:.4f}, bm25 #{hit.bm25_rank}, vector #{hit.vector_rank}, "
            f"updated {hit.chunk.updated_at})"
        )
    for newer, older in result.recency_swaps:
        print(f"   newer policy {newer} ranked above {older}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

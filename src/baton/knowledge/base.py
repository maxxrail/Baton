"""`KnowledgeBase`: the help center as the agent and tools see it.

Loads the ingested chunks from the database once (lazily, on the first search) and answers
`search`, `chunk` and `title` lookups from memory. Call `refresh()` after re-ingesting in a
long-running process; `baton-chat` and the API build a fresh one at start-up.
"""

import threading

from sqlalchemy.orm import Session, sessionmaker

from baton.knowledge.retrieve import (
    IndexedChunk,
    Reranker,
    RetrievalConfig,
    Retriever,
    SearchResult,
)
from baton.llm.embedder import Embedder

DEFAULT_BASE_URL = "https://help.northwind.example/articles/"


class KnowledgeBase:
    def __init__(
        self,
        sessions: sessionmaker[Session] | None,
        embedder: Embedder,
        *,
        config: RetrievalConfig | None = None,
        reranker: Reranker | None = None,
        base_url: str = DEFAULT_BASE_URL,
        retriever: Retriever | None = None,
    ) -> None:
        """Pass `sessions` to load the index from the database, or a built `retriever`
        (tests, the eval script)."""
        if sessions is None and retriever is None:
            raise ValueError("KnowledgeBase needs a session factory or a retriever")
        self.sessions = sessions
        self.embedder = embedder
        self.config = config
        self.reranker = reranker
        self.base_url = base_url
        self._retriever = retriever
        self._lock = threading.Lock()

    @property
    def retriever(self) -> Retriever:
        with self._lock:
            if self._retriever is None:
                assert self.sessions is not None
                with self.sessions() as session:
                    self._retriever = Retriever.load(
                        session, self.embedder, config=self.config, reranker=self.reranker
                    )
            return self._retriever

    def refresh(self) -> None:
        """Reload the index from the database on the next search."""
        with self._lock:
            if self.sessions is not None:
                self._retriever = None

    def search(self, query: str, top_k: int | None = None) -> SearchResult:
        return self.retriever.search(query, top_k)

    def chunk(self, chunk_id: int) -> IndexedChunk | None:
        return self.retriever.get(chunk_id)

    def title(self, article_id: str) -> str | None:
        for chunk in self.retriever.chunks:
            if chunk.article_id == article_id:
                return chunk.title
        return None

    def url(self, article_id: str) -> str:
        return f"{self.base_url.rstrip('/')}/{article_id}"

    @property
    def article_ids(self) -> set[str]:
        return self.retriever.article_ids

    def __len__(self) -> int:
        return len(self.retriever)

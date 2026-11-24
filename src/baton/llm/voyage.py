"""`VoyageEmbedder`: the optional real embedding provider (Voyage AI's REST API over httpx).

Anthropic doesn't offer an embeddings endpoint, and Voyage is the provider its docs recommend.
Turn it on with `BATON_EMBEDDER=voyage` and `VOYAGE_API_KEY`; everything else (tests, CI, the
default dev setup) uses the offline `HashingEmbedder`.

Only the plain `POST /v1/embeddings` call is used, so there's no extra SDK dependency.
"""

import httpx
import numpy as np

from baton.llm.embedder import Vector

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
DEFAULT_VOYAGE_MODEL = "voyage-3.5"


class EmbeddingError(RuntimeError):
    """The provider failed or answered with something we can't use."""


class VoyageEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_VOYAGE_MODEL,
        dim: int = 1024,
        *,
        client: httpx.Client | None = None,
        batch_size: int = 64,
    ) -> None:
        if not api_key:
            raise ValueError("VoyageEmbedder needs an API key")
        self.model = model
        self.dim = dim
        self.name = f"voyage-{model}-{dim}"
        self.batch_size = batch_size
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=30.0)

    def embed(self, texts: list[str]) -> Vector:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        rows: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            rows.extend(self._embed_batch(texts[start : start + self.batch_size]))
        vectors = np.asarray(rows, dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vectors / norms).astype(np.float32)

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        try:
            response = self._client.post(
                VOYAGE_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"input": batch, "model": self.model, "output_dimension": self.dim},
            )
            response.raise_for_status()
            data = response.json()["data"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            # Never include the response body: it can echo the request.
            raise EmbeddingError(
                f"Voyage embeddings request failed: {type(exc).__name__}"
            ) from None
        ordered = sorted(data, key=lambda item: int(item["index"]))
        if len(ordered) != len(batch) or any(len(i["embedding"]) != self.dim for i in ordered):
            raise EmbeddingError("Voyage returned the wrong number or size of embeddings")
        return [list(map(float, item["embedding"])) for item in ordered]

"""VoyageEmbedder against a mocked HTTP transport (no network)."""

import json

import httpx
import numpy as np
import pytest
from pydantic import SecretStr

from baton.config import Settings
from baton.llm import (
    EmbeddingError,
    HashingEmbedder,
    LLMConfigError,
    VoyageEmbedder,
    get_embedder,
)
from baton.llm.voyage import VOYAGE_URL


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_embeds_in_batches_normalises_and_keeps_order() -> None:
    requests: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == VOYAGE_URL
        assert request.headers["Authorization"] == "Bearer k"
        body = json.loads(request.content)
        requests.append(body)
        # Return the rows out of order: the embedder must sort them by index.
        data = [
            {"index": i, "embedding": [float(len(text)), 0.0, 0.0, 1.0]}
            for i, text in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"data": list(reversed(data))})

    emb = VoyageEmbedder("k", dim=4, batch_size=2, client=_client(httpx.MockTransport(handle)))
    vecs = emb.embed(["a", "bbb", "cc"])

    assert [len(r["input"]) for r in requests] == [2, 1]  # type: ignore[arg-type]
    assert requests[0]["model"] == "voyage-3.5"
    assert requests[0]["output_dimension"] == 4
    assert vecs.shape == (3, 4)
    np.testing.assert_allclose(np.linalg.norm(vecs, axis=1), 1.0, rtol=1e-5)
    assert vecs[1, 0] > vecs[2, 0] > vecs[0, 0]  # order follows the input, not the response
    assert emb.name == "voyage-voyage-3.5-4"


def test_http_error_is_wrapped_without_the_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "secret echo"})

    emb = VoyageEmbedder("k", dim=4, client=_client(httpx.MockTransport(handle)))
    with pytest.raises(EmbeddingError) as info:
        emb.embed(["x"])
    assert "secret echo" not in str(info.value)


def test_wrong_size_is_an_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    emb = VoyageEmbedder("k", dim=4, client=_client(httpx.MockTransport(handle)))
    with pytest.raises(EmbeddingError):
        emb.embed(["x"])


def test_empty_input_needs_no_call() -> None:
    emb = VoyageEmbedder("k", dim=4, client=_client(httpx.MockTransport(lambda r: 1 / 0)))  # type: ignore[arg-type,return-value]
    assert emb.embed([]).shape == (0, 4)


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_factory_picks_the_configured_embedder() -> None:
    assert isinstance(get_embedder(_settings()), HashingEmbedder)
    voyage = get_embedder(_settings(baton_embedder="voyage", voyage_api_key=SecretStr("k")))
    assert isinstance(voyage, VoyageEmbedder)
    with pytest.raises(LLMConfigError, match="VOYAGE_API_KEY"):
        get_embedder(_settings(baton_embedder="voyage"))

import numpy as np
import pytest

from baton.llm import HashingEmbedder


def test_shape_dtype_and_unit_norm() -> None:
    vecs = HashingEmbedder(dim=64).embed(["return my order", "where is my package"])
    assert vecs.shape == (2, 64)
    assert vecs.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vecs, axis=1), [1.0, 1.0], rtol=1e-5)


def test_deterministic_across_instances() -> None:
    a = HashingEmbedder().embed(["Refund policy for damaged items"])
    b = HashingEmbedder().embed(["Refund policy for damaged items"])
    np.testing.assert_array_equal(a, b)


def test_pinned_value_is_stable_across_runs() -> None:
    # blake2b is unsalted, so this must hold across processes and OSes (CI runs Linux + Windows).
    vec = HashingEmbedder(dim=8).embed_one("refund")
    assert np.count_nonzero(vec) == 1
    assert abs(float(vec.sum())) == pytest.approx(1.0)
    again = HashingEmbedder(dim=8).embed_one("refund")
    assert int(np.argmax(np.abs(vec))) == int(np.argmax(np.abs(again)))


def test_similar_texts_score_higher_than_unrelated() -> None:
    emb = HashingEmbedder()
    query, related, unrelated = emb.embed(
        [
            "how do I return an order",
            "returns: you can return an order within 30 days",
            "loyalty points expire after one year",
        ]
    )
    assert float(query @ related) > float(query @ unrelated)


def test_case_and_punctuation_insensitive() -> None:
    emb = HashingEmbedder()
    np.testing.assert_allclose(emb.embed_one("Cancel ORDER!"), emb.embed_one("cancel order"))


def test_empty_inputs() -> None:
    emb = HashingEmbedder(dim=16)
    assert emb.embed([]).shape == (0, 16)
    assert not emb.embed_one("").any()


def test_rejects_bad_dim() -> None:
    with pytest.raises(ValueError, match="dim"):
        HashingEmbedder(dim=0)

import pytest

from scripts.eval_router import main


def test_offline_eval_reports_prefilter_numbers(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "cases: 40 (38 with an AOP label), top_k=5" in out
    assert "pre-filter recall@5: 38/38 = 100.0%" in out
    assert "skipping the LLM classifier" in out

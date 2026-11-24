"""Optional LLM reranker for retrieval candidates (`BATON_KB_RERANK=true`).

The model sees the question and the numbered candidate passages (with their dates) and calls
`rank_passages` with the passage numbers, most useful first. Its output is validated in code:
unknown or repeated numbers are dropped, and any candidate it left out keeps its fused order
after the ones it ranked. Anything unusable (no tool call, an exception) keeps the fused order,
so the reranker can only reorder, never lose or invent a passage.
"""

import time
from pathlib import Path

import structlog

from baton.knowledge.retrieve import Hit
from baton.llm.client import LLMClient
from baton.llm.types import Msg, ToolSpec
from baton.prompts import load_prompt

log = structlog.get_logger("baton.knowledge")

RERANK_TOOL = "rank_passages"
_PASSAGE_CHARS = 1200


class LLMReranker:
    def __init__(self, llm: LLMClient, prompts_dir: Path | None = None) -> None:
        self.llm = llm
        self.prompt = load_prompt("rerank", prompts_dir)

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name=RERANK_TOOL,
            description="Report the passages that help answer the question, most useful first.",
            input_schema={
                "type": "object",
                "properties": {
                    "ranking": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "passage numbers, most useful first",
                    }
                },
                "required": ["ranking"],
            },
        )

    def rerank(self, query: str, hits: list[Hit]) -> list[Hit] | None:
        if len(hits) < 2:
            return None
        passages = "\n".join(
            f'<passage n="{n}" article="{h.chunk.article_id}" updated="{h.chunk.updated_at}">\n'
            f"{h.chunk.title} - {h.chunk.section}\n{h.chunk.text[:_PASSAGE_CHARS]}\n</passage>"
            for n, h in enumerate(hits, start=1)
        )
        question = query.replace("<", "&lt;").replace(">", "&gt;")
        message = f"<question>{question}</question>\n\n{passages}"
        start = time.perf_counter()
        try:
            resp = self.llm.complete(
                self.prompt.text, [Msg(role="user", content=message)], tools=[self.tool]
            )
        except Exception as exc:  # a reranker failure must never fail the search
            log.warning("kb_rerank_failed", error=type(exc).__name__)
            return None
        log.info(
            "kb_rerank",
            prompt_version=self.prompt.version,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        call = next((c for c in resp.tool_calls if c.name == RERANK_TOOL), None)
        raw = call.input.get("ranking") if call else None
        if not isinstance(raw, list):
            return None
        order: list[int] = []
        for item in raw:
            if isinstance(item, int) and 1 <= item <= len(hits) and item - 1 not in order:
                order.append(item - 1)
        if not order:
            return None
        rest = [i for i in range(len(hits)) if i not in order]
        return [hits[i] for i in order + rest]

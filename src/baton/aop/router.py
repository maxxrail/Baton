"""Pick the AOP that should handle a customer message.

Two stages:
1. **Pre-filter** (cheap, local): embed every AOP's triggers once, score each AOP by its best
   trigger's cosine similarity to the message, and keep the top `top_k`. Tokens shared by most
   AOPs' triggers are dropped first, since they can't tell AOPs apart. The fallback AOP
   (`general_question`) is always kept, so knowledge questions never get filtered out.
2. **Classifier** (LLM): the model sees only those candidates and must answer with structured
   output via the `choose_procedure` tool. The answer is validated in code: an id that wasn't
   offered, a malformed payload or no tool call at all routes to `None` instead of guessing.

Works with any `LLMClient`, including `FakeLLM` in tests.
"""

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from baton.aop.models import AOPBundle
from baton.llm.client import LLMClient
from baton.llm.embedder import Embedder, HashingEmbedder, tokenize
from baton.llm.types import Msg, TextBlock, ToolSpec
from baton.prompts import Prompt, load_prompt

ROUTER_PROMPT = "router"
ROUTE_TOOL = "choose_procedure"
NONE_ID = "none"
DEFAULT_FALLBACK = "general_question"
HISTORY_MESSAGES = 6
HISTORY_DISCOUNT = 0.9
# A token in the triggers of more than this share of AOPs ("customer", "wants", "the") can't
# tell AOPs apart; it only adds noise to a bag-of-words similarity, so the pre-filter drops it.
COMMON_TOKEN_SHARE = 0.5


class Candidate(BaseModel):
    aop_id: str
    score: float


class RouteDecision(BaseModel):
    aop_id: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    candidates: list[str] = Field(default_factory=list)
    prompt_version: str = ""


class _RouterOutput(BaseModel):
    aop_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


def _escape(text: str) -> str:
    """Customer text can't close or open our data tags (`</customer_message>`)."""
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _text_of(msg: Msg) -> str:
    if isinstance(msg.content, str):
        return msg.content
    return " ".join(b.text for b in msg.content if isinstance(b, TextBlock))


class Router:
    def __init__(
        self,
        bundle: AOPBundle,
        llm: LLMClient,
        embedder: Embedder | None = None,
        *,
        top_k: int = 5,
        fallback_aop_id: str | None = DEFAULT_FALLBACK,
        prompt: Prompt | None = None,
        prompts_dir: Path | None = None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self.bundle = bundle
        self.llm = llm
        self.embedder = embedder or HashingEmbedder()
        self.top_k = top_k
        self.fallback_aop_id = fallback_aop_id if fallback_aop_id in bundle.aops else None
        self.prompt = prompt or load_prompt(ROUTER_PROMPT, prompts_dir)

        doc_freq: Counter[str] = Counter()
        for aop in bundle.aops.values():
            doc_freq.update(set(tokenize(" ".join([aop.name, *aop.triggers]))))
        self.common_tokens = frozenset(
            t for t, n in doc_freq.items() if n / max(len(bundle.aops), 1) > COMMON_TOKEN_SHARE
        )

        # One row per trigger (plus the AOP's name), remembering which AOP it belongs to.
        self._owners: list[str] = []
        texts: list[str] = []
        for aop in bundle.aops.values():
            for text in [aop.name, *aop.triggers]:
                self._owners.append(aop.id)
                texts.append(self._informative(text))
        self._trigger_vectors = self.embedder.embed(texts)

    def _informative(self, text: str) -> str:
        return " ".join(t for t in tokenize(text) if t not in self.common_tokens)

    # ------------------------------------------------------------------ stage 1

    def _scores(self, query: str) -> dict[str, float]:
        sims = self._trigger_vectors @ self.embedder.embed([self._informative(query)])[0]
        scores: dict[str, float] = {}
        for owner, sim in zip(self._owners, sims.tolist(), strict=True):
            scores[owner] = max(scores.get(owner, -1.0), float(sim))
        return scores

    def prefilter(self, message: str, history: Sequence[Msg] = ()) -> list[Candidate]:
        """Top-k AOPs by trigger similarity, best first, plus the fallback AOP."""
        scores = self._scores(message)
        previous = [_text_of(m) for m in history if m.role == "user"]
        if previous:
            # A short follow-up ("yes, the blue one") carries no topic words of its own, so the
            # last customer turn is scored too. It's scored separately, not concatenated: the
            # bag-of-words embedder dilutes a topic word in a longer text. A small discount
            # lets the current message win ties when the customer changes topic.
            earlier = self._scores(previous[-1])
            scores = {k: max(v, HISTORY_DISCOUNT * earlier[k]) for k, v in scores.items()}
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        chosen = [Candidate(aop_id=k, score=round(v, 4)) for k, v in ranked[: self.top_k]]
        if self.fallback_aop_id and all(c.aop_id != self.fallback_aop_id for c in chosen):
            chosen.append(
                Candidate(aop_id=self.fallback_aop_id, score=scores[self.fallback_aop_id])
            )
        return chosen

    def best_match(self, message: str) -> str:
        """The pre-filter's top AOP alone: the offline baseline the eval script reports."""
        return self.prefilter(message)[0].aop_id

    # ------------------------------------------------------------------ stage 2

    def _tool(self, candidate_ids: list[str]) -> ToolSpec:
        return ToolSpec(
            name=ROUTE_TOOL,
            description="Report which procedure should handle the customer's message.",
            input_schema={
                "type": "object",
                "properties": {
                    "aop_id": {"type": "string", "enum": [*candidate_ids, NONE_ID]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "rationale": {"type": "string"},
                },
                "required": ["aop_id", "confidence", "rationale"],
                "additionalProperties": False,
            },
        )

    def _system(self, candidate_ids: list[str]) -> str:
        blocks = []
        for aop_id in candidate_ids:
            aop = self.bundle.get(aop_id)
            triggers = "\n".join(f"  - {t}" for t in aop.triggers)
            blocks.append(f"- id: {aop.id}\n  name: {aop.name}\n  use when:\n{triggers}")
        return self.prompt.render(candidates="\n".join(blocks))

    @staticmethod
    def _user_message(message: str, history: Sequence[Msg]) -> Msg:
        recent = [m for m in history if _text_of(m).strip()][-HISTORY_MESSAGES:]
        lines = [
            f"{'customer' if m.role == 'user' else 'agent'}: {_escape(_text_of(m))}" for m in recent
        ]
        transcript = "\n".join(lines) if lines else "(no earlier messages)"
        return Msg(
            role="user",
            content=(
                f"<conversation>\n{transcript}\n</conversation>\n\n"
                f"<customer_message>\n{_escape(message)}\n</customer_message>"
            ),
        )

    @staticmethod
    def _parse(text: str, tool_inputs: list[dict[str, object]]) -> _RouterOutput | None:
        payloads: list[object] = list(tool_inputs)
        if not payloads and text.strip():
            try:  # tolerate a model that answers with bare JSON instead of the tool
                payloads.append(json.loads(text.strip()))
            except json.JSONDecodeError:
                return None
        for payload in payloads:
            try:
                return _RouterOutput.model_validate(payload)
            except ValidationError:
                continue
        return None

    def route(self, message: str, history: Sequence[Msg] = ()) -> RouteDecision:
        candidates = self.prefilter(message, history)
        candidate_ids = [c.aop_id for c in candidates]
        response = self.llm.complete(
            system=self._system(candidate_ids),
            messages=[self._user_message(message, history)],
            tools=[self._tool(candidate_ids)],
            max_tokens=1024,
        )
        tool_inputs = [c.input for c in response.tool_calls if c.name == ROUTE_TOOL]
        output = self._parse(response.text, tool_inputs)

        def decision(aop_id: str | None, confidence: float, rationale: str) -> RouteDecision:
            return RouteDecision(
                aop_id=aop_id,
                confidence=confidence,
                rationale=rationale,
                candidates=candidate_ids,
                prompt_version=self.prompt.version,
            )

        if output is None:
            return decision(None, 0.0, "router output was missing or malformed")
        if output.aop_id == NONE_ID:
            return decision(None, output.confidence, output.rationale)
        if output.aop_id not in candidate_ids:
            return decision(None, 0.0, f"router chose {output.aop_id!r}, which was not offered")
        return decision(output.aop_id, output.confidence, output.rationale)

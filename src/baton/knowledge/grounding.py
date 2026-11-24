"""Grounding check: every factual claim in a knowledge answer must be backed by evidence.

The model drafts a reply; before it's sent, `GroundingChecker.check`:

1. splits the draft into sentences **in code** (`split_sentences`), so the judge labels
   numbered units and never rewrites the reply itself;
2. asks an LLM judge (`prompts/grounding.md`, structured output via the `report_grounding`
   tool) to label each sentence: a factual `claim` or `other` (greeting, question, offer of
   help, restating the customer), and for claims whether the evidence supports it and which
   source ids do;
3. acts on the labels, in code:
   - every claim supported -> the reply passes; a claim backed by a KB article gets a
     `[kb:article_id]` citation if the model forgot one ("pass");
   - some claims unsupported -> those sentences are removed and a short note says the rest
     couldn't be confirmed and offers a teammate ("trimmed");
   - no supported claim left -> the whole reply is replaced by `ABSTAIN_REPLY`, which says
     we're not sure and offers a teammate ("abstained").

Citations to articles that aren't in the evidence are always stripped, so every citation in a
sent reply resolves to an article the agent actually retrieved.

**Fails closed.** If the judge errors, skips sentences, or answers with something unusable,
the unlabelled sentences count as unsupported claims. A broken judge produces an abstention,
never an unchecked answer.

Evidence = the KB chunks retrieved this turn (and in the previous search), plus what the agent
legitimately knows from elsewhere: this turn's account tool results, the conversation state
and the active procedure's policies and steps. Only KB sources produce citations.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from baton.llm.client import LLMClient
from baton.llm.types import Msg, ToolSpec
from baton.prompts import load_prompt
from baton.runtime.language import register

GROUNDING_TOOL = "report_grounding"
KB_PREFIX = "kb:"

CITATION_RE = re.compile(r"\[kb:([a-z0-9][a-z0-9-]*)\]")
_LOOSE_CITATION_RE = re.compile(r"\[\s*kb\s*:\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*\]", re.IGNORECASE)
# A sentence ends at . ! or ? (plus closing quotes/brackets and any citations right after it)
# followed by spaces, or at a line break. "$6.95" and "8 a.m.-8 p.m." don't split.
_BOUNDARY_RE = re.compile(r"(?<=[.!?])[\"')]*(?:\s*\[kb:[a-z0-9-]+\])*[ \t]+|\n+")
_TRAILING_PUNCT_RE = re.compile(r"([.!?][\"')]*)\s*$")

ABSTAIN_REPLY = register(
    "I'm not sure about that, and I'd rather not guess: I couldn't confirm it in our help "
    "center. Would you like me to bring in a teammate who can find out?",
    es="No estoy seguro de eso y prefiero no adivinar: no he podido confirmarlo en nuestro "
    "centro de ayuda. ¿Quiere que le pase con un compañero del equipo que pueda averiguarlo?",
    fr="Je n'en suis pas sûr et je préfère ne pas deviner : je n'ai pas pu le vérifier dans "
    "notre centre d'aide. Voulez-vous que je fasse appel à un membre de l'équipe ?",
)
HEDGE_NOTE = register(
    "I couldn't confirm the rest in our help center, so I've left it out rather than guess. "
    "I can bring in a teammate if you'd like.",
    es="No he podido confirmar el resto en nuestro centro de ayuda, así que lo he omitido para "
    "no adivinar. Si lo desea, puedo pasarle con un compañero del equipo.",
    fr="Je n'ai pas pu vérifier le reste dans notre centre d'aide, je l'ai donc laissé de côté "
    "plutôt que de deviner. Je peux faire appel à un membre de l'équipe si vous le souhaitez.",
)
_EVIDENCE_CHARS = 2500

GroundingAction = Literal["pass", "trimmed", "abstained"]


@dataclass(frozen=True)
class Evidence:
    """One source the judge may use. `source` is `kb:<article_id>` for help-center chunks."""

    source: str
    title: str
    text: str
    updated_at: date | None = None

    @property
    def article_id(self) -> str | None:
        return self.source.removeprefix(KB_PREFIX) if self.source.startswith(KB_PREFIX) else None


class SentenceVerdict(BaseModel):
    n: int
    kind: Literal["claim", "other"]
    supported: bool = False
    sources: list[str] = Field(default_factory=list)


class _JudgeOutput(BaseModel):
    sentences: list[SentenceVerdict]


class GroundingOutcome(BaseModel):
    """What the check did. Stored in the turn trace."""

    action: GroundingAction
    reason: str
    judge_ok: bool
    sentences: list[str] = Field(default_factory=list)
    verdicts: list[SentenceVerdict] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    citations_added: list[str] = Field(default_factory=list)
    citations_removed: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    prompt_version: str = ""


@dataclass(frozen=True)
class GroundingResult:
    text: str
    outcome: GroundingOutcome


def normalize_citations(text: str) -> str:
    """Rewrite sloppy tags (`[KB: Return-Policy]`) as `[kb:return-policy]`, so every tag the
    model writes is recognised, checked and either resolved or stripped, never shown raw."""
    return _LOOSE_CITATION_RE.sub(lambda m: f"[kb:{m.group(1).lower()}]", text)


def citations(text: str) -> list[str]:
    """Article ids cited in `text`, in order of first appearance."""
    return list(dict.fromkeys(CITATION_RE.findall(text)))


def split_sentences(text: str) -> list[str]:
    """Split into sentence segments. Each keeps its trailing whitespace, so `"".join(...)`
    gives back the original text exactly (formatting and line breaks survive edits)."""
    segments, start = [], 0
    for match in _BOUNDARY_RE.finditer(text):
        segments.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        segments.append(text[start:])
    return segments


def _content(segment: str) -> str:
    """The sentence as the judge sees it: citations removed, whitespace collapsed."""
    return re.sub(r"\s+", " ", CITATION_RE.sub("", segment)).strip()


def _add_citations(segment: str, ids: Sequence[str]) -> str:
    """Insert `[kb:id]` tags before the sentence's final punctuation."""
    tags = "".join(f" [kb:{i}]" for i in ids)
    body = segment.rstrip()
    tail = segment[len(body) :]
    match = _TRAILING_PUNCT_RE.search(body)
    if match:
        return f"{body[: match.start()]}{tags}{match.group(1)}{tail}"
    return f"{body}{tags}{tail}"


def _strip_unknown_citations(segment: str, known: set[str]) -> tuple[str, list[str]]:
    """Remove citations to articles that aren't in the evidence."""
    bad = [c for c in citations(segment) if c not in known]
    for article_id in bad:
        segment = re.sub(r"\s?\[kb:" + re.escape(article_id) + r"\]", "", segment)
    return segment, bad


def _escape(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _json_or_none(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None


class GroundingChecker:
    def __init__(self, llm: LLMClient, prompts_dir: Path | None = None) -> None:
        self.llm = llm
        self.prompt = load_prompt("grounding", prompts_dir)

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name=GROUNDING_TOOL,
            description="Report, for every numbered sentence, whether it is a supported claim.",
            input_schema={
                "type": "object",
                "properties": {
                    "sentences": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "n": {"type": "integer"},
                                "kind": {"type": "string", "enum": ["claim", "other"]},
                                "supported": {"type": "boolean"},
                                "sources": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["n", "kind", "supported", "sources"],
                        },
                    }
                },
                "required": ["sentences"],
            },
        )

    def check(
        self,
        reply: str,
        evidence: Sequence[Evidence],
        question: str,
        llm: LLMClient | None = None,
    ) -> GroundingResult:
        segments = split_sentences(reply)
        # Segment index -> sentence number (1-based) for every segment with content.
        numbers: dict[int, int] = {}
        contents: list[str] = []
        for i, segment in enumerate(segments):
            if _content(segment):
                contents.append(_content(segment))
                numbers[i] = len(contents)
        if not contents:
            return self._result(reply, "pass", "nothing to check", True, evidence)

        sources = {e.source for e in evidence}
        kb_ids = {e.article_id for e in evidence if e.article_id}
        verdicts, judge_ok = self._judge(contents, evidence, question, llm or self.llm)
        by_n = {v.n: v for v in verdicts}

        kept: list[str] = []
        removed: list[str] = []
        added: list[str] = []
        stripped: list[str] = []
        supported = 0
        for i, segment in enumerate(segments):
            n = numbers.get(i)
            if n is None:  # whitespace between sentences
                kept.append(segment)
                continue
            # A sentence the judge didn't label counts as an unsupported claim (fail closed).
            verdict = by_n.get(n) or SentenceVerdict(n=n, kind="claim")
            backing = [src for src in verdict.sources if src in sources]
            if verdict.kind == "claim":
                if not (verdict.supported and backing):
                    removed.append(contents[n - 1])
                    continue
                supported += 1
            segment, bad = _strip_unknown_citations(segment, kb_ids)
            stripped.extend(bad)
            if verdict.kind == "claim" and not citations(segment):
                want = [s.removeprefix(KB_PREFIX) for s in backing if s.startswith(KB_PREFIX)]
                if want:
                    segment = _add_citations(segment, want[:2])
                    added.extend(want[:2])
            kept.append(segment)

        def finish(text: str, action: GroundingAction, reason: str) -> GroundingResult:
            return self._result(
                text,
                action,
                reason,
                judge_ok,
                evidence,
                sentences=contents,
                verdicts=verdicts,
                removed=removed,
                added=added,
                stripped=stripped,
            )

        if removed and supported == 0:
            reason = f"{len(removed)} claim(s), none supported by the evidence"
            if not judge_ok:
                reason += " (judge output incomplete or unusable: failing closed)"
            return finish(ABSTAIN_REPLY, "abstained", reason)
        text = "".join(kept).strip()
        if removed:
            reason = f"removed {len(removed)} unsupported claim(s), kept {supported}"
            return finish(f"{text}\n\n{HEDGE_NOTE}", "trimmed", reason)
        return finish(text, "pass", f"{supported} claim(s), all supported")

    # ------------------------------------------------------------------ helpers

    def _judge(
        self,
        sentences: list[str],
        evidence: Sequence[Evidence],
        question: str,
        llm: LLMClient,
    ) -> tuple[list[SentenceVerdict], bool]:
        """The judge's labels, and whether they are complete and well-formed."""
        blocks = []
        for e in evidence:
            updated = f' updated="{e.updated_at}"' if e.updated_at else ""
            blocks.append(
                f'<source id="{e.source}" title="{_escape(e.title)}"{updated}>\n'
                f"{_escape(e.text[:_EVIDENCE_CHARS])}\n</source>"
            )
        numbered = "\n".join(
            f'<sentence n="{n}">{_escape(c)}</sentence>' for n, c in enumerate(sentences, start=1)
        )
        evidence_text = "\n".join(blocks) or "(no evidence)"
        message = (
            f"<evidence>\n{evidence_text}\n</evidence>\n\n"
            f"<customer_question>{_escape(question)}</customer_question>\n\n"
            f"<reply>\n{numbered}\n</reply>"
        )
        try:
            resp = llm.complete(
                self.prompt.text, [Msg(role="user", content=message)], tools=[self.tool]
            )
        except Exception:  # an unavailable judge must not let the answer through unchecked
            return [], False
        call = next((c for c in resp.tool_calls if c.name == GROUNDING_TOOL), None)
        raw: object = call.input if call else _json_or_none(resp.text)
        try:
            parsed = _JudgeOutput.model_validate(raw)
        except ValidationError:
            return [], False
        verdicts = [v for v in parsed.sentences if 1 <= v.n <= len(sentences)]
        complete = {v.n for v in verdicts} == set(range(1, len(sentences) + 1))
        return verdicts, complete

    def _result(
        self,
        text: str,
        action: GroundingAction,
        reason: str,
        judge_ok: bool,
        evidence: Sequence[Evidence],
        *,
        sentences: list[str] | None = None,
        verdicts: list[SentenceVerdict] | None = None,
        removed: list[str] | None = None,
        added: list[str] | None = None,
        stripped: list[str] | None = None,
    ) -> GroundingResult:
        outcome = GroundingOutcome(
            action=action,
            reason=reason,
            judge_ok=judge_ok,
            sentences=sentences or [],
            verdicts=verdicts or [],
            removed=removed or [],
            citations_added=added or [],
            citations_removed=stripped or [],
            evidence=[e.source for e in evidence],
            prompt_version=self.prompt.version,
        )
        return GroundingResult(text=text, outcome=outcome)

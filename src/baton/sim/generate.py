"""Draft scenarios for an AOP with the LLM: `baton-sim generate --aop return_item --n 8`.

The model proposes scenarios (happy, edge, adversarial, off-topic) through the
`propose_scenarios` tool; each one is validated exactly like a hand-written scenario (shape,
AOP, tools, fixtures, placeholders). Valid drafts are written as YAML to
`evals/sims/generated/`; invalid ones are reported and dropped. Nothing here is trusted
automatically: a person reads the drafts and moves the good ones into `evals/sims/`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from baton.aop.models import AOPBundle
from baton.llm.client import LLMClient
from baton.llm.types import Msg, ToolSpec
from baton.prompts import load_prompt
from baton.sim.fixtures import FIXTURE_SPECS
from baton.sim.suite import Scenario, validate_suite
from baton.tools.registry import ToolRegistry

GENERATE_PROMPT = "generate"
GENERATE_TOOL = "propose_scenarios"

GENERATE_SPEC = ToolSpec(
    name=GENERATE_TOOL,
    description="Propose simulation scenarios for the procedure.",
    input_schema={
        "type": "object",
        "properties": {
            "scenarios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "description": {"type": "string"},
                        "persona": {"type": "object"},
                        "expect": {"type": "object"},
                        "max_turns": {"type": "integer"},
                    },
                    "required": ["id", "tags", "persona", "expect"],
                },
            }
        },
        "required": ["scenarios"],
    },
)


@dataclass
class Drafts:
    scenarios: list[Scenario]
    rejected: list[str]  # why each dropped draft was dropped
    prompt_version: str


def _context(bundle: AOPBundle, aop_id: str, registry: ToolRegistry) -> str:
    aop = bundle.get(aop_id)
    steps = "\n".join(f"{s.number}. {s.text}" for s in aop.steps)
    policies = "\n".join(f"- {p}" for p in aop.policies) or "- (none)"
    fixtures = "\n".join(
        f"- {name}: {spec.note or spec.customer_id}" for name, spec in FIXTURE_SPECS.items()
    )
    tools = "\n".join(f"- {t.name}: {t.description}" for t in registry)
    return (
        f'<procedure id="{aop.id}" name="{aop.name}">\n{steps}\nPolicies:\n{policies}\n'
        f"</procedure>\n<fixtures>\n{fixtures}\n</fixtures>\n<tools>\n{tools}\n</tools>"
    )


def generate(
    llm: LLMClient,
    bundle: AOPBundle,
    registry: ToolRegistry,
    aop_id: str,
    n: int = 8,
    prompts_dir: Path | None = None,
) -> Drafts:
    prompt = load_prompt(GENERATE_PROMPT, prompts_dir)
    resp = llm.complete(
        prompt.render(n=str(n)),
        [Msg(role="user", content=_context(bundle, aop_id, registry))],
        [GENERATE_SPEC],
        max_tokens=16000,
    )
    call = next((c for c in resp.tool_calls if c.name == GENERATE_TOOL), None)
    raw = call.input.get("scenarios") if call else None
    if not isinstance(raw, list):
        return Drafts([], ["the model proposed no scenarios"], prompt.version)

    kept: list[Scenario] = []
    rejected: list[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            rejected.append(f"draft {i}: not an object")
            continue
        label = f"draft {i} ({item.get('id', '?')})"
        try:
            sc = Scenario.model_validate({**item, "aop": aop_id, "runs": 3, "source": "draft"})
        except ValidationError as exc:
            first = exc.errors()[0]
            rejected.append(f"{label}: {'.'.join(map(str, first['loc']))}: {first['msg']}")
            continue
        problems = validate_suite([sc], bundle, registry)
        if any(k.id == sc.id for k in kept):
            problems.append("duplicate id")
        if problems:
            rejected.append(f"{label}: {'; '.join(problems)}")
            continue
        kept.append(sc)
    return Drafts(kept, rejected, prompt.version)


def write_drafts(drafts: Drafts, aop_id: str, out_dir: Path, model: str = "") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{aop_id}-{stamp}.yaml"
    header = (
        f"# DRAFT scenarios for {aop_id}, generated {stamp} by {model or 'the LLM'} "
        f"({drafts.prompt_version}).\n"
        "# Not part of any suite until a person reviews them and moves the good ones into\n"
        "# evals/sims/. Check every expectation: the generator can be wrong.\n"
    )
    body = [sc.model_dump(exclude_none=True, exclude_defaults=False) for sc in drafts.scenarios]
    for item in body:
        item.pop("script", None)
    text = yaml.safe_dump({"scenarios": body}, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(header + text, encoding="utf-8")
    return path

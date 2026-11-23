"""AOP endpoints, used by the console's AOP browser (Part 04).

- `GET /aops`: every compiled AOP (summary) plus the bundle hash and any warnings.
- `GET /aops/{id}`: one compiled AOP (steps, transitions, step prompts) and its Mermaid graph.
- `POST /aops/validate`: validate a draft AOP's Markdown without saving it.

The bundle is compiled once when the app starts (see `create_app`), so a broken AOP directory
fails the deploy instead of failing customer conversations.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from baton.aop.compiler import STEP_PROMPT, check_source, compile_aop
from baton.aop.mermaid import to_mermaid
from baton.aop.models import AOPBundle, CompiledAOP, Diagnostic
from baton.aop.validator import has_errors
from baton.prompts import load_prompt
from baton.tools.registry import default_registry

MAX_DRAFT_CHARS = 100_000

router = APIRouter(prefix="/aops", tags=["aops"])


def get_bundle(request: Request) -> AOPBundle:
    bundle: AOPBundle = request.app.state.aop_bundle
    return bundle


BundleDep = Annotated[AOPBundle, Depends(get_bundle)]


class AOPSummary(BaseModel):
    id: str
    name: str
    version: int
    ref: str
    source_hash: str
    requires_verification: bool
    triggers: list[str]
    step_count: int


class AOPListOut(BaseModel):
    bundle_hash: str
    aops: list[AOPSummary]
    diagnostics: list[Diagnostic]


class AOPDetailOut(BaseModel):
    aop: CompiledAOP
    ref: str
    mermaid: str


class ValidateIn(BaseModel):
    markdown: str = Field(max_length=MAX_DRAFT_CHARS)


class ValidateOut(BaseModel):
    ok: bool = Field(description="True when there are no errors (warnings are allowed)")
    diagnostics: list[Diagnostic]
    aop: CompiledAOP | None = Field(description="The compiled draft, when it has no errors")
    mermaid: str | None = Field(description="The draft's graph, whenever it parsed")


@router.get("")
def list_aops(bundle: BundleDep) -> AOPListOut:
    return AOPListOut(
        bundle_hash=bundle.bundle_hash,
        aops=[
            AOPSummary(
                id=a.id,
                name=a.name,
                version=a.version,
                ref=a.ref,
                source_hash=a.source_hash,
                requires_verification=a.requires_verification,
                triggers=a.triggers,
                step_count=len(a.steps),
            )
            for a in sorted(bundle.aops.values(), key=lambda a: a.id)
        ],
        diagnostics=bundle.diagnostics,
    )


@router.get("/{aop_id}")
def get_aop(aop_id: str, bundle: BundleDep) -> AOPDetailOut:
    aop = bundle.aops.get(aop_id)
    if aop is None:
        raise HTTPException(status_code=404, detail=f"unknown AOP {aop_id!r}")
    return AOPDetailOut(aop=aop, ref=aop.ref, mermaid=to_mermaid(aop))


@router.post("/validate")
def validate_draft(body: ValidateIn, request: Request) -> ValidateOut:
    registry = default_registry()
    parsed, diagnostics = check_source(body.markdown, registry)
    ok = not has_errors(diagnostics)
    compiled = None
    if parsed is not None and ok:
        template = load_prompt(STEP_PROMPT, request.app.state.settings.prompts_dir)
        compiled = compile_aop(parsed, registry, template)
    return ValidateOut(
        ok=ok,
        diagnostics=diagnostics,
        aop=compiled,
        mermaid=to_mermaid(parsed) if parsed else None,
    )

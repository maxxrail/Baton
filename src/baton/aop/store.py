"""Persist compiled AOP versions in the `aop_versions` table.

A row is identified by (aop_id, source_hash, step_prompt_version): the compiled JSON includes
the rendered step prompts, so a change to either the AOP file or `prompts/aop_step.md` is a new
row. `sync_versions` is idempotent: recompiling unchanged AOPs writes nothing, and editing one
AOP writes exactly one row.

Compiling is expected to be single-writer (a deploy step or the CLI). Two concurrent syncs of
the same new AOP would make the second fail on the unique constraint rather than duplicate rows.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from baton.aop.models import AOPBundle, CompiledAOP
from baton.db.models import AOPVersion

Key = tuple[str, str, str]  # (aop_id, source_hash, step_prompt_version)


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)  # "aop_id@version" of new rows
    unchanged: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _latest(session: Session, aop_id: str) -> AOPVersion | None:
    """The most recently stored row. After a revert (A, B, A) this is B, which only affects
    the wording of the version-bump warning."""
    stmt = (
        select(AOPVersion)
        .where(AOPVersion.aop_id == aop_id)
        .order_by(AOPVersion.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _known(session: Session) -> tuple[set[Key], set[tuple[str, str]]]:
    rows = session.execute(
        select(AOPVersion.aop_id, AOPVersion.source_hash, AOPVersion.step_prompt_version)
    ).all()
    keys = {(a, h, p) for a, h, p in rows}
    return keys, {(a, h) for a, h, _ in keys}


def sync_versions(session: Session, bundle: AOPBundle) -> SyncResult:
    """Record every compiled AOP not stored yet. The caller owns the transaction."""
    result = SyncResult()
    keys, sources = _known(session)
    for aop in sorted(bundle.aops.values(), key=lambda a: a.id):
        if (aop.id, aop.source_hash, aop.step_prompt_version) in keys:
            result.unchanged.append(aop.ref)
            continue
        latest = _latest(session, aop.id)
        source_is_new = (aop.id, aop.source_hash) not in sources
        if source_is_new and latest is not None and aop.version <= latest.version:
            result.warnings.append(
                f"{aop.id} changed but its version is still {aop.version} (last stored: "
                f"{latest.version}); bump 'version' so traces can tell the two apart"
            )
        session.add(
            AOPVersion(
                aop_id=aop.id,
                version=aop.version,
                source_hash=aop.source_hash,
                step_prompt_version=aop.step_prompt_version,
                compiled=aop.model_dump(mode="json"),
            )
        )
        result.created.append(aop.ref)
    session.flush()
    return result


def load_version(
    session: Session, aop_id: str, source_hash: str, step_prompt_version: str
) -> CompiledAOP | None:
    """The exact compiled AOP a trace ran against."""
    row = session.scalars(
        select(AOPVersion).where(
            AOPVersion.aop_id == aop_id,
            AOPVersion.source_hash == source_hash,
            AOPVersion.step_prompt_version == step_prompt_version,
        )
    ).first()
    return CompiledAOP.model_validate(row.compiled) if row else None

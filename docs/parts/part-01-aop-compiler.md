# Part 01 — AOP language + compiler

## Goal
Let a non-engineer write a support procedure in Markdown, and turn it into a **validated, versioned workflow graph** the runtime can execute and inspect. This is Baton's version of Decagon's Agent Operating Procedures: natural-language flexibility, code-level control.

## The AOP format
AOPs live in `aops/*.md`. Frontmatter + numbered steps in plain English. Tools are referenced with `@tool_name`, branching with "If … go to step N", handoff with `ESCALATE`, completion with `RESOLVE`. Example (`aops/return_item.md`):

```markdown
---
id: return_item
name: Return or exchange an item
version: 1
triggers:
  - customer wants to return, exchange, or send back an item
  - item arrived damaged or wrong
requires_verification: true
policies:
  - Returns are accepted within 30 days of delivery.
  - Never promise a refund before the return is created.
---
1. Confirm which order and item the customer means. Use @get_customer_orders if they don't know the order number.
2. Check the order with @get_order. If the order is not delivered, explain returns start after delivery and RESOLVE.
3. If delivered more than 30 days ago, apologize, explain the policy, and offer to ESCALATE if they insist.
4. Ask whether they want a refund or an exchange.
5. Create the return with @create_return. Tell them the RMA number and that the label is emailed.
6. If they chose refund, tell them it is issued when the item is received. RESOLVE.
```

Write at least these AOPs for Northwind: `return_item`, `where_is_my_order`, `cancel_order`, `change_address`, `refund_request`, `manage_subscription`, `loyalty_points`, `general_question` (KB fallback — no tools, used in Part 03).

## Scope
1. **Models** (`src/baton/aop/models.py`): `AOP` (id, name, version, triggers, requires_verification, policies, steps, source_hash), `Step` (number, text, tool_refs, transitions: list of `(condition_text | None, target)` where target is a step number, `ESCALATE`, or `RESOLVE`).
2. **Parser** (`src/baton/aop/parser.py`): Markdown + YAML frontmatter → `AOP`. Deterministic, no LLM. Extracts `@tool` refs, explicit "go to step N", `ESCALATE`, `RESOLVE`; implicit transition to the next step.
3. **Validator** (`src/baton/aop/validator.py`) returns a list of `Diagnostic(level: error|warning, aop_id, step, code, message)`:
   - `UNKNOWN_TOOL` — `@tool` not in the tool registry (stub the registry interface in `src/baton/tools/registry.py` with names + typed specs for the Northwind endpoints from Part 00; implementations come in Part 02).
   - `BAD_TARGET` — go-to a step that doesn't exist.
   - `UNREACHABLE_STEP`, `NO_TERMINAL` (no path ends in RESOLVE/ESCALATE), `CYCLE_WITHOUT_EXIT`.
   - `SIDE_EFFECT_WITHOUT_VERIFICATION` — AOP uses a side-effecting tool but `requires_verification` is false (error).
   - `DUPLICATE_ID`, `MISSING_TRIGGERS`, warning `LONG_STEP` (>60 words).
4. **Compiler** (`src/baton/aop/compiler.py`): `compile_aops(dir) -> AOPBundle` (all AOPs + diagnostics + bundle hash). Fails if any error. Produces the **step prompt** for each step that Part 02 will inject (step text + policies + allowed tools for this step).
5. **Router** (`src/baton/aop/router.py`): pick the AOP for a customer message. Interface `route(message, history) -> RouteDecision(aop_id | None, confidence, rationale)`. Implementation: LLM classifier over AOP triggers with structured output, plus a cheap embedding pre-filter (top-k by trigger similarity). Works with FakeLLM in tests.
6. **CLI** (`src/baton/aop/cli.py`, exposed as `uv run baton-aop`): `validate` (pretty diagnostics, nonzero exit on error), `graph <id>` (prints Mermaid flowchart), `show <id>`.
7. **Persistence + versioning**: table `aop_versions` (aop_id, version, source_hash, compiled JSON, created_at). Compiling stores new versions only when the hash changes. Every trace from Part 02 will record `aop_id@version`.
8. **API**: `GET /aops`, `GET /aops/{id}` (compiled graph + Mermaid), `POST /aops/validate` (body: markdown → diagnostics). The console in Part 04 will use these.

## Acceptance criteria
- [ ] All 8 Northwind AOPs compile with zero errors.
- [ ] `tests/aop/fixtures/bad/*.md` has one fixture per error code, and each test asserts exactly that code is raised.
- [ ] Parser is property-tested or table-tested on at least 15 step phrasings ("go to step 3", "skip to 3", "then ESCALATE", etc.).
- [ ] `baton-aop graph return_item` prints valid Mermaid (test checks syntax shape).
- [ ] Router picks the correct AOP on a 40-message labeled fixture (`evals/router_cases.yaml`) using FakeLLM driven by the fixture; with a real key, `scripts/eval_router.py` reports accuracy (document the number in PROGRESS.md if a key is available).
- [ ] Recompiling unchanged AOPs creates no new versions; editing one creates exactly one.
- [ ] Lint, types, tests clean.

## Verify
```
uv run pytest -q tests/aop
uv run baton-aop validate
uv run baton-aop graph return_item
uv run pytest -q
```

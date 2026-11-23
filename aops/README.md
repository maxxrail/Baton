# aops/

Agent Operating Procedures: Northwind Goods' support workflows, written in plain-English Markdown
(one `*.md` per procedure). The AOP compiler (`src/baton/aop/`) turns each file into a validated
workflow graph that the agent runtime executes.

```
uv run baton-aop validate           # check every AOP, nonzero exit on any error
uv run baton-aop graph return_item  # Mermaid flowchart
uv run baton-aop show return_item   # steps, transitions, tools (add --prompts for step prompts)
uv run baton-aop compile            # validate + record new versions in the aop_versions table
```

## Format

```markdown
---
id: return_item                 # lower_snake_case, unique
name: Return or exchange an item
version: 1                      # bump when you change the procedure
triggers:                       # when the router should pick this AOP (at least one)
  - customer wants to return, exchange, or send back an item
requires_verification: true     # required if any step uses a tool that changes data
policies:                       # rules shown to the agent on every step
  - Returns are accepted within 30 days of delivery.
---
1. Confirm which order the customer means. Use @get_customer_orders if needed.
2. Check the order with @get_order. If the order is not delivered, explain why and RESOLVE.
3. ...
```

Unknown frontmatter keys are rejected, so a typo like `requires_verfication` fails instead of
silently defaulting to `false`.

Inside a step:

| Write | Meaning |
|---|---|
| `@tool_name` | The step may call this tool. It must exist in the tool registry. |
| `go to step 3`, `skip to 3`, `go back to step 1`, `repeat step 2`, `-> step 4` | Jump to a step. |
| `ESCALATE` (upper case) | Hand the conversation to a human. |
| `RESOLVE` (upper case) | The request is handled; the procedure ends. |
| `If ...,` / `When ...,` / `Unless ...,` / `ESCALATE if ...` | Makes the jump in that sentence conditional. An `if` *before* the jump means "whether" (`Ask if they want a refund, then go to step 4` always goes to 4). |
| `Otherwise ...` | The default exit when no condition holds. |

Write jumps positively. `Never RESOLVE before ...`, `Don't go to step 3` and `..., in which case
RESOLVE` are rejected as `PARSE_ERROR`s, because guessing their meaning would change the graph.
`none` is a reserved id.

**Conditions apply per sentence.** `If it shipped, share tracking and RESOLVE.` is conditional;
`If it shipped, share tracking. RESOLVE.` resolves unconditionally (the validator points this out
when it makes later steps unreachable).

A step with no default exit falls through to the next step. The last step must end with
`RESOLVE` or `ESCALATE` (or an `Otherwise`), or the validator reports `NO_TERMINAL`.

Lower-case "resolve" and "escalate" are ordinary words, so prose like "we'll resolve it" never
ends the procedure by accident.

## Validation codes

| Code | Level | Meaning |
|---|---|---|
| `PARSE_ERROR` | error | Frontmatter or step numbering is malformed (message has the line). |
| `UNKNOWN_TOOL` | error | `@tool` isn't in the tool registry. |
| `BAD_TARGET` | error | A jump goes to a step that doesn't exist. |
| `UNREACHABLE_STEP` | error | No path from step 1 reaches the step. |
| `NO_TERMINAL` | error | The procedure can stop at this step without RESOLVE/ESCALATE. |
| `CYCLE_WITHOUT_EXIT` | error | A loop of steps with no way out. |
| `SIDE_EFFECT_WITHOUT_VERIFICATION` | error | A data-changing tool is used but `requires_verification` is false. |
| `DUPLICATE_ID` | error | Two files share an `id`. |
| `MISSING_TRIGGERS` | error | No triggers, so the router could never pick it. |
| `LONG_STEP` | warning | Step is over 60 words; split it. |

## Versions

`baton-aop compile` records each AOP's `version`, source hash and compiled JSON in the
`aop_versions` table, but only when the hash is new: recompiling unchanged files writes nothing.
Editing a file without bumping `version` still stores the new hash, and `compile` warns you.
Line endings don't count as a change (the hash normalises CRLF to LF).

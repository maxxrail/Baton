# data/kb/

Help-center articles for Northwind Goods, one Markdown file per article. The agent answers
policy and product questions only from these, with citations (`[kb:<id>]`).

```
---
id: return-policy          # must match the file name
title: Return policy
category: returns
updated_at: 2026-02-01     # when two articles disagree, the newer one wins
---
# Return policy
## Return window
...
```

Every frontmatter key is required and unknown keys are rejected. Headings matter: articles are
chunked at `##`/`###` headings (~300-500 tokens per chunk) and each chunk keeps its heading path.

After editing, run `uv run baton-kb ingest`. It only re-embeds chunks whose text changed.

Two pairs conflict **on purpose**, to test that the newer policy wins:
`return-policy` (30 days, 2026) vs `return-policy-2024` (60 days), and `shipping-costs`
(free over $75, 2026) vs `free-shipping-offer-2025` (free over $50).

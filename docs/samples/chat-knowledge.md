# Sample: `baton-chat --trace`, help-center answers (live model)

Recorded 2026-11-24 against `claude-sonnet-5-5`, the local mock store and the help center
ingested with the default offline `HashingEmbedder` (`uv run baton-kb ingest`). These are the two
questions from `docs/parts/part-03-knowledge-rag.md` → Verify, asked in one conversation.
Customer lines were piped to `baton-chat --trace`; the indented lines are each turn's trace.
Citations print as footnotes (the stored reply keeps `[kb:article_id]` tags).

Things to notice:
- **Turn 1:** the agent searches the help center, answers from `shipping-international`, and
  cites it. The grounding judge labels the 3 factual sentences and finds all 3 supported, so
  the reply is sent as written.
- **Turn 2 ("do you price match?"):** the help center has no such article. The agent says it's
  not sure and offers a teammate. The grounding check finds 0 claims, so nothing is removed.
  Retrieval still marked this search `covered`: "price" and "match" each appear somewhere in
  the KB (see Known gaps in `docs/PROGRESS.md`). The prompt rules and the grounding check are
  what keep the agent from inventing a policy.
- **Found by this run:** in an earlier run the model wrote the turn-1 answer in the same
  response as its `advance_step(RESOLVE)` calls, and the runtime sent only the final "Anything
  else?". The loop now keeps text written alongside tool calls (`test_an_answer_written_next_to_resolve_reaches_the_customer`).

```
Northwind Goods support | model claude-sonnet-5-5
conversation e39ee90676b34b60be97aae352174c0e. Type 'exit' to quit.
you> how long does shipping to Canada take?
agent> Standard shipping to Canada takes 6-10 business days after your order leaves the warehouse. [1] Remote areas like Yukon, the Northwest Territories and Nunavut can take up to 15 business days, and expedited shipping isn't available for Canadian addresses [1].

Most orders leave the warehouse within 1-2 business days of being placed. [2] Is there anything else I can help with?

Sources:
[1] International orders and shipping to Canada - https://help.northwind.example/articles/shipping-international
[2] How long does it take to process my order? - https://help.northwind.example/articles/order-processing-times
  route    -> general_question (Customer is asking a general question about shipping times to Canada, which is a shipping policy question.)
  aop      general_question@2  step None -> None  status resolved
  tool     advance_step({'target': '2', 'reason': 'Question is clear: shipping time to Canada'}) -> ok
  tool     search_knowledge({'query': 'how long does shipping to Canada take'}) -> ok
  tool     advance_step({'target': '3', 'reason': 'Found answer in help center'}) -> ok
  tool     advance_step({'target': 'RESOLVE', 'reason': 'Answered question; no teammate requested'}) -> ok
  kb       'how long does shipping to Canada take' -> ['shipping-international', 'order-processing-times', 'tracking-your-order', 'refund-timing', 'return-policy'] (covered)
  grounded pass: 3 claim(s), all supported
  checks   10 passed | tokens 22240 in / 950 out | 9747.1 ms | reply from llm
[conversation resolved]
you> do you price match?
agent> Sorry, I'm not sure about our price matching policy. I couldn't find anything on it in the help center. Would you like me to bring in a teammate who can confirm?
  route    -> general_question (Customer is asking about store policy (price matching), which is a general policy question.)
  aop      general_question@2  step None -> 1  status open
  tool     search_knowledge({'query': 'Do you offer price matching?'}) -> ok
  kb       'Do you offer price matching?' -> ['shipping-costs', 'warranty', 'return-policy', 'return-policy', 'payment-methods'] (covered)
  grounded pass: 0 claim(s), all supported
  checks   7 passed | tokens 12897 in / 520 out | 5894.3 ms | reply from llm
```

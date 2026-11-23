---
id: refund_request
name: Refund request
version: 1
triggers:
  - customer asks for a refund or their money back
  - customer was overcharged, charged twice, or charged the wrong amount
  - an item was missing from the order and the customer wants a partial refund
  - customer asks about the status of a refund
requires_verification: true
policies:
  - Refunds up to $100 can be issued directly; larger refunds need a teammate's approval.
  - A refund can never exceed what remains refundable on the order.
  - Never say a refund is done until the refund tool has succeeded.
---
1. Confirm which order the refund is for and why. Use @get_customer_orders if they don't know the order number.
2. Check the order with @get_order, including refunds already issued and the refundable balance. If they are asking about an earlier refund, share its amount and date and RESOLVE.
3. If they want to send the item back, explain that a return is the right path and how returns work, then RESOLVE.
4. If nothing remains refundable on the order, explain what was already refunded and RESOLVE.
5. Agree on the amount. If it is more than $100, explain that a teammate needs to approve it and ESCALATE.
6. Issue it with @create_refund. Tell them the amount and that it takes 5-10 business days to appear. RESOLVE.

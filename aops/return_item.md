---
id: return_item
name: Return or exchange an item
version: 1
triggers:
  - customer wants to return, exchange, or send back an item
  - item arrived damaged, broken, defective, or wrong
  - item doesn't fit or the customer doesn't like it
  - customer wants a different size or color of something they bought
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

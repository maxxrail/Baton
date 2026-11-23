---
id: where_is_my_order
name: Where is my order
version: 1
triggers:
  - customer asks where their order or package is
  - customer wants tracking information, a tracking number, or a delivery date
  - order is late, has not arrived yet, or shows delivered but is missing
requires_verification: true
policies:
  - Only share tracking details for orders that belong to the verified customer.
  - Never guess a delivery date; share only what the order record shows.
---
1. Find out which order the customer means. Use @get_customer_orders if they don't have the order number.
2. Look up the order with @get_order.
3. If the order is still processing, tell them it hasn't shipped yet, that tracking is emailed when it does, and RESOLVE.
4. If the order has shipped, share the tracking number and ship date and RESOLVE.
5. If the order was cancelled, explain that and any refund issued on it, then RESOLVE.
6. The order shows delivered: share the delivery date. If the customer says it never arrived, apologize and ESCALATE so a teammate can open a carrier claim. Otherwise RESOLVE.

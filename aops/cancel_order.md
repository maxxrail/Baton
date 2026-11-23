---
id: cancel_order
name: Cancel an order
version: 1
triggers:
  - customer wants to cancel an order
  - customer ordered by mistake or changed their mind before it ships
  - customer wants to stop an order from shipping
requires_verification: true
policies:
  - Only orders that are still processing can be cancelled.
  - Cancelling refunds the full amount charged to the original payment method.
  - Always get the customer's explicit confirmation before cancelling.
---
1. Confirm which order to cancel. Use @get_customer_orders if they don't know the order number.
2. Check the order with @get_order. If it has already shipped or been delivered, explain it can't be cancelled and go to step 5.
3. Read back the order number and items and ask the customer to confirm the cancellation. If they change their mind, RESOLVE.
4. Cancel it with @cancel_order. Tell them the refund goes back to the original payment method. RESOLVE.
5. Offer to help with a return once the order is delivered instead. RESOLVE.

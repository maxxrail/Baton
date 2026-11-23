---
id: manage_subscription
name: Manage a subscription
version: 1
triggers:
  - customer wants to pause, resume, skip, or cancel a subscription
  - customer asks when their next subscription shipment is
  - customer wants to stop recurring deliveries or auto-ship
requires_verification: true
policies:
  - Customers can pause, resume or cancel a subscription at any time.
  - Cancelling does not refund shipments that were already sent.
  - Offer pausing as an alternative to cancelling once; never push after they decline.
---
1. Look up their subscriptions with @get_subscriptions and confirm which one they mean.
2. Ask what they want to do: pause, resume, or cancel. If they only want the next ship date, share it and RESOLVE.
3. If they want to pause, pause it with @pause_subscription, confirm it, and RESOLVE.
4. If they want to resume, resume it with @resume_subscription, share the next ship date, and RESOLVE.
5. They want to cancel: offer pausing instead, once. If they accept, go to step 3.
6. Cancel it with @cancel_subscription and confirm it is cancelled. RESOLVE.

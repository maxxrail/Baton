---
id: loyalty_points
name: Loyalty points and rewards
version: 1
triggers:
  - customer asks about their loyalty points or rewards balance
  - customer asks about their rewards tier or how to reach the next tier
  - customer says loyalty points are missing from a purchase
requires_verification: true
policies:
  - Points can't be exchanged for cash.
  - Agents can't add or adjust points; missing points are reviewed by a teammate.
  - Tiers are based on lifetime points - bronze below 1000, silver from 1000, gold from 2500.
---
1. Look up their rewards account with @get_loyalty.
2. Share their points balance, lifetime points and tier. If they ask how to reach the next tier, explain the thresholds.
3. If they say points are missing from a purchase, apologize and ESCALATE so a teammate can review the account.
4. Answer any other questions about the program using the policies above. RESOLVE.

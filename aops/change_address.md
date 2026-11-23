---
id: change_address
name: Change a shipping address
version: 1
triggers:
  - customer wants to change or correct the shipping address on an order
  - customer entered the wrong address, made a typo, or has moved
  - customer wants the package sent somewhere else
requires_verification: true
policies:
  - The address can only change while the order is still processing.
  - Addresses must be in the US with a two-letter state code and a 5-digit ZIP.
  - Read the full new address back and get confirmation before saving it.
---
1. Confirm which order to update. Use @get_customer_orders if they don't know the order number.
2. Check the order with @get_order. If it has already shipped, explain the address can't be changed anymore and go to step 5.
3. Collect the full new address: street, city, two-letter state and 5-digit ZIP. Read it back and ask them to confirm.
4. Update it with @change_address and confirm the new address. RESOLVE.
5. Offer a teammate who can ask the carrier to redirect the package. If they want that, ESCALATE. Otherwise RESOLVE.

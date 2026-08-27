# HLL VIP / Whitelist Update — 2026-08-27

- Website canonical roster now exposes verified HLL player identity and VIP eligibility to Battalion Clerk.
- VIP eligibility: active ASSIGNED Soldier, not separated/archived, with a verified HLL identity.
- Battalion Clerk reconciles VIP status every 5 minutes while RCON is connected.
- Manual server VIPs not previously managed by Battalion Clerk are never removed by reconciliation.
- Clerk-managed VIPs are removed when the Soldier is no longer eligible.
- Reserved VIP slot count defaults to 2 and is configurable with HLL_VIP_RESERVED_SLOTS.
- Added /hll-vip-sync for staff to request an immediate reconciliation.

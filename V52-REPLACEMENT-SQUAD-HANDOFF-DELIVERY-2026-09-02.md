# V52 — Replacement-to-Squad Handoff Discord Delivery

- Battalion Clerk watches the shared `squad_handoff_tasks` table.
- The receiving NCO receives one private Discord handoff notice after Accept & Assign.
- The notice lists the leadership handoff checklist and links directly to `/nco#handoffs`.
- The bot auto-closes stale handoff rows when First 24 Hours is already complete, preventing late/stale DMs.
- Website remains authoritative for creation, scope, and completion state.

# Discord Routing Reset — 2026-08-27

Added a controlled reconfiguration workflow for Website/Battalion Clerk -> Discord channel delivery.

## Commands

- `/discord-routing-reset confirmation:RESET DISCORD ROUTING`
  - Immediately pauses routed Discord delivery.
  - Clears configured welcome, orders, operation reminder, S-3 duty-roster, seeding, personnel-order, report/helpdesk, and permanent Training/Operation/Meeting duty channel bindings.
  - Does NOT delete or reset personnel, Discord links, ranks, assignments, HLL telemetry, ribbons, promotions, credentials, roles, categories, or channels.

- `/discord-routing-status`
  - Shows PAUSED/ACTIVE state and currently assigned routes.

- `/discord-routing-resume confirm:RESUME DISCORD ROUTING`
  - Re-enables automatic delivery after the desired channels are reassigned.

## Safe workflow

1. Run the reset command.
2. Rename/move/rebuild Discord channels as desired.
3. Reassign each required route using the existing channel assignment commands.
4. Run `/discord-routing-status`.
5. Run the resume command.

The pause is persistent in PostgreSQL, so restarting Battalion Clerk while Discord is being reorganized will not accidentally resume routed delivery.

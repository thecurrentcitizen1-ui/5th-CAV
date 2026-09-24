# V102 — Assignment-Only Discord Formation Role Sync
Date: 2026-09-23

- Adds `ASSIGNMENT_ROLE_SYNC_ENABLED`, independent of the legacy global `AUTO_ROLE_SYNC_ENABLED`.
- Only website assignment lifecycle jobs may mutate Discord roles.
- Allowed mutation scope: `A Company`, `B Company`, `C Company`, their current Platoon/Squad roles, `HHC` cleanup when moving to line squads, and `Reserve Platoon`.
- Active line placement requires a real squad assignment before Company/Platoon/Squad roles are added.
- Transfers remove stale managed formation roles and add the new formation roles.
- Unassignment removes managed formation roles.
- Reserve assignment removes line formation roles and adds only `Reserve Platoon`.
- Rank, NCO, MOS, appointments, staff/admin, qualifications, membership, recruiting, NEW ARRIVAL, Veteran, Community Liaison, Server Booster and all other custom/manual roles are never changed by this path.
- Legacy full role automation remains disabled.
- Non-assignment role-sync queue items are consumed in manual mode without Discord mutations.

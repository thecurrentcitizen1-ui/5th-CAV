# Battalion Clerk V95 — Accession Self-Healing / Departure Staff DM

Date: 2026-09-10

## Safe Discord name-assisted application linking
- Clerk now checks approved website applications that are waiting for a Discord identity.
- Automatic linking is allowed only when the supplied application/Discord name produces one exact unique username, display-name, or global-name match in the guild.
- Similar/fuzzy names, zero matches, or multiple matches are never guessed.
- Website-side integrity checks reject a Discord account already tied to a different active recruiting case or Soldier.
- A successful safe link resumes personnel provisioning and login delivery.

## Missing 201 File self-healing
- The approved recruit watcher now checks a dedicated queue for approved, Discord-linked recruits that have no personnel record / 201 File.
- Clerk retries provisioning from the authoritative recruiting case and existing Discord member identity.
- This runs independently of the normal credential-delivery queue, preventing a credentials state from hiding a missing 201 File.

## Discord departure notifications
- When a non-bot member leaves Discord, Clerk still performs the existing authoritative active-personnel departure purge with retry protection.
- After the purge attempt, Clerk DMs all current members holding `Command Staff` or `Admin`.
- Staff who hold both roles receive only one DM.
- The DM identifies the departing Discord member and, when available, their Soldier rank/name, Battle Roster number and last assignment.
- The DM reports whether active 201/personnel cleanup completed or staff attention is required.
- A blocked/failed DM to one staff member cannot stop other notifications or the underlying purge.

## Preservation
- V94 all-day under-50 seeding credit and the 6 PM–9 PM Eastern automated seeding call remain intact.
- RCON telemetry, member-stat filing, intake watches, Weekly Brief, First 30, role sync, retention and recovery systems remain cumulative.
- No new slash command was added; command budget remains below Discord's global limit.

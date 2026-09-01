# V42 — Staff Game Identity Unlink

Added a staff-only Discord repair command for incorrect HLL identity links.

## Command
`/unlink-member-game member:@Soldier`

Authorization uses the Battalion Clerk's existing Command gate: Manage Server or Administrator.

## Behavior
- Resolves the selected Discord member to their linked 1/5 Cavalry Soldier Record.
- Deletes the current verified entry from `hll_personnel_links`.
- Supersedes any pending console identity claim so a corrected identity can be filed immediately.
- Preserves historical match telemetry, research samples, 201 File data, awards, assignments, qualifications, and service records.
- Does not silently reassign or delete historical telemetry that may already have been credited under a wrong identity; that requires a deliberate telemetry repair.

## Relink paths
- Member: `/link-game`
- Staff: `/hll-link-soldier`

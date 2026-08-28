# Guided Assign Soldier Workflow — 2026-08-27

- Added a dedicated Command/S-1 `ASSIGN SOLDIER` workflow at `/staff/assign`.
- Staff selects one Soldier, then Company -> Platoon -> optional Squad -> optional Alpha/Bravo Team -> duty/billet.
- Child formation choices are filtered from the authoritative `unit_nodes` hierarchy; invalid parent/child combinations are rejected server-side.
- Added a live review panel before filing and retained the existing Welcome Packet acceptance gate.
- Filing uses the existing authoritative `process_assignment_action()` path, preserving assignment history, official orders, 201 File/service record, membership activation, Replacement release checks, member notification, and Discord role reconciliation.
- Added a `BEST AVAILABLE` recommendation using current exact-node squad strength and Alpha/Bravo team balance. It is advisory only and never auto-files an assignment.
- Added a Command Desk `NEEDS ASSIGNMENT` queue with direct `ASSIGN` buttons for replacement Soldiers already in READY FOR ASSIGNMENT / ASSIGNMENT PENDING.
- Added `ASSIGN SOLDIER` to the Command quick-task launcher and Soldier search results.
- Changed the Command `WAITING ON ASSIGNMENT` workflow lane to open the guided assignment desk directly.
- Added responsive desktop/mobile styling for the assignment workflow.

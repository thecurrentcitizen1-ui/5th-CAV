
## 2026-08-27 — My Squad Member / Leadership UX Overhaul
- Rebuilt MY SQUAD as a member-first squad hub rather than a large administrative roster.
- Top of page now shows the Soldier's exact formation, personal place in the squad, squad leader, readiness, assigned strength, and fire-team assignment.
- Alpha and Bravo Team are presented as distinct, readable fire-team cards with Team Leader identification, YOUR TEAM highlighting, and direct Soldier Combat Record access.
- Current Soldier is highlighted as YOU; Squad HQ / unassigned-fire-team leadership and support personnel are separated from fire-team rosters.
- Recognized leadership appointments receive a billet-driven LEADERSHIP WORKSPACE with assigned strength, readiness, subordinate/actionable count, and Team Leader vacancy visibility.
- Leadership presentation is billet-driven, while mutating quick actions preserve existing rank + appointment + subordinate-scope permission checks.
- Junior members do not see the dense leadership control roster; leaders receive an additional full control roster for quick reference.
- Added responsive/mobile behavior for the new squad hub, squadmate cards, leadership metrics, and navigation.

## 2026-08-27 — Accessions Pipeline Record Processing Error Repair
- Fixed `/hq/accession-pipeline` querying the nonexistent legacy `personnel_orders` table.
- Assignment/transfer order lookup now uses the authoritative `personnel_documents.document_type` records.
- Welcome Packet completion enrichment recognizes completed or waived task records safely.
- Added a core-data fail-soft fallback: optional Welcome Packet, Discord sync, member-link, or order enrichment failures can no longer take down the entire Accessions page.
- Core recruiting case and personnel lifecycle data remain authoritative; unavailable secondary metrics render as pending/unavailable rather than causing a server error.
- Repair targets the Accessions failure represented by diagnostic reference 9644B82D.

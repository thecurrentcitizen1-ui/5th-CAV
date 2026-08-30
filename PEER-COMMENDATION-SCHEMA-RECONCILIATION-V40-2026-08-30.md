# Peer Commendation Schema Reconciliation V40

- Reconciles every column used by Battalion Clerk and the Website, including legacy tables created by earlier releases.
- Adds/backfills a UUID `id` when missing.
- Uses the proven `website_member_links` lookup pattern and requires only `personnel.id`.
- Removes recruiting-case fallback and optional personnel-column assumptions from the live `/commend` path.
- Returns a bounded database detail with future command errors for direct diagnosis.

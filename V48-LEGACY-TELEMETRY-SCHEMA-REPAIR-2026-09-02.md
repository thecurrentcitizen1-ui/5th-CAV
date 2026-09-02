# V48 — Legacy telemetry schema repair

- Battalion Clerk now explicitly adds `connected_delta_seconds` to an existing `hll_research_samples` table when missing.
- This repairs upgraded databases where `CREATE TABLE IF NOT EXISTS` preserved an older table layout.
- The change is additive/idempotent and preserves all existing research telemetry.
- This keeps the website Seeding Reconciliation reader compatible with the live Clerk telemetry schema.

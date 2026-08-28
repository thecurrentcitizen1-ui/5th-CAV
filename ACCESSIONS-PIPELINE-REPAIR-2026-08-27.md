# ACCESSIONS PIPELINE REPAIR — 27 AUG 2026

Target: Command Post > Accessions (`/hq/accession-pipeline`)

## Root cause repaired
The Accessions enrichment query referenced `personnel_orders`, a table that does not exist in the current website schema. Official assignment and transfer orders are stored in `personnel_documents` using `document_type`.

## Reliability hardening
The Accessions board now attempts its full enriched view first. If an optional subsystem is unavailable during a partial migration/deploy, it falls back to core `recruiting_cases` + `personnel` data so Command can still open and work the pipeline.

## Validation
- Python compile: PASS
- Jinja template parse: PASS
- Nonexistent `personnel_orders` reference removed from Accessions route

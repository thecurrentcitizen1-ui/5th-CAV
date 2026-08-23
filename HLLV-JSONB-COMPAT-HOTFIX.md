# HLLV JSONB Compatibility Hotfix

Fixes telemetry polling after a match is opened when PostgreSQL/asyncpg returns
JSONB role ledgers as JSON strings rather than Python dictionaries.

- Safely decodes `role_seconds` and `role_distance_meters` from dict or JSON text.
- Preserves the platform and ISO-8601 timer compatibility fixes.
- No Railway variable changes required.
- RCON remains read-only.

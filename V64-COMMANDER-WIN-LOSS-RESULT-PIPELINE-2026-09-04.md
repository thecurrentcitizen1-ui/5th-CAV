# V64 — Commander Win/Loss Result Pipeline — 2026-09-04

- Adds explicit winner_side / winner_faction_id / result_verified_at fields to HLL match sessions.
- On match close, files ALLIED or AXIS winner from the last preserved decisive final score.
- No DRAW state is created; equal/missing score evidence remains unresolved rather than inventing a result.
- Backfills completed HLL: Vietnam WDEV sessions with US=2 Allied and NVA=1 Axis faction indexes when historical rows are missing them.
- Backfills winner evidence for historical completed matches that already have a decisive retained score.
- Website V171 consumes this winner evidence and recalculates Commander WIN/LOSS history.

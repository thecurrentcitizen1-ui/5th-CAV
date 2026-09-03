# V53 — Commander Match Result Repair

- Fixes completed match scores being overwritten by the newly loaded map/layer score.
- Active match scores are now preserved continuously on each telemetry poll.
- Map/layer transitions close the old match without applying the new round's score.
- New match sessions retain their first observed score and subsequent live score updates.
- Railway/process restarts can resume a recently observed open match on the same map/mode instead of splitting the round.

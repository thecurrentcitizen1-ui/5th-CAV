# V86 — Combat Roster Server #1 Authority + Voice Diagnostics

- Anchors automatic Discord combat roster match boundaries to `server_1` by default.
- Server #2 remains telemetry/progression capable but cannot start, cancel, or return Combat Roster voice routing.
- Active/completed match lookups are now scoped by `hll_match_sessions.server_key`.
- 60-second staging recheck is therefore immune to Server #2 map changes.
- Match-end Ready Room returns are triggered only by the configured combat-authoritative server.
- Manual `/combat-generate` uses the authoritative combat server's live match.
- Adds `/combat-system-check` with configuration, permissions, live match, Ready Room muster, game-link, side detection, eligibility, binding, and explicit blocker reporting.
- Preserves existing rules: six players may receive a roster but remain in Ready Room; 7+ eligible players enables automatic voice movement; no mid-round reshuffle; late joins wait for the next round.
- Optional Railway override: `COMBAT_ROSTER_SERVER_KEY=server_1` (default). Leave unchanged unless Command intentionally moves Combat Roster authority to another official server.

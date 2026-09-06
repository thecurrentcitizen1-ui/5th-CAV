# V85 — Commander Authoritative Match Ledger + Retroactive Repair — 2026-09-06

- Battalion Clerk creates and maintains `hll_commander_match_ledger`.
- At match close it files each linked Role 20 Commander appearance with server key, Commander seconds, dominant Role 20 team, final score, winning side, and verified WIN/LOSS.
- On startup it retroactively rebuilds all recoverable historical Commander games from retained player-match and research-sample telemetry.
- Dominant team samples while Role 20 is active take precedence over the player's last team in the match.
- Historical rows without enough retained side/winner evidence remain pending rather than receiving a guessed result.

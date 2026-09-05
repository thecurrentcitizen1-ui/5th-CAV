# Battalion Clerk V67 — Live Match Formation Randomizer

## Purpose
Automatically turn the live Discord voice-channel muster into a fresh randomized next-match roster after every completed HLL: Vietnam round.

## Setup command
`/match-formation-setup voice_channel:<voice> text_channel:<text> side:<U.S. side|Non-U.S. side>`

- Watches one selected Discord voice channel.
- Publishes into one selected Discord text channel.
- Automation is active with 5+ non-bot members in the voice channel.
- Setup snapshots the latest already-completed HLL match so deployment/restart never reposts an old round.

## Automatic round flow
1. HLL RCON closes a match in `hll_match_sessions` when the round/layer transitions.
2. Battalion Clerk detects the new completed match.
3. If the selected voice channel has fewer than 5 members, that round is marked processed and no stale roster is posted later.
4. With 5+ members, the live voice membership is shuffled fresh.
5. Battalion Clerk posts the **1/5 CAV — NEXT MATCH FORMATION** embed to the configured text channel.

## Randomization rules
- Website company/platoon/squad assignment does not affect the match shuffle.
- Rank, MOS and permanent in-game role do not affect the match shuffle.
- 5–8 players: randomized infantry formation only.
- 9+ players: one randomized 3-man **SABER — ARMOR** element; one random Tank Commander and two crew.
- U.S. side with 13+ total players: one randomized **AIR CAV — PILOT** is added.
- Remaining personnel are split as evenly as possible into rifle squads, maximum 6 per squad.
- Each rifle squad gets a randomly selected SL for that round.
- Every completed round creates a new random formation.

## Commands
- `/match-formation-setup` — configure voice, text and side; enables automation.
- `/match-formation-status` — show configuration and current live muster count.
- `/match-formation-publish` — immediately force a fresh live shuffle/post.
- `/match-formation-disable` — turn automatic between-round posts off.

## Persistence
Adds:
- `clerk_match_formation_config`
- `clerk_match_formation_history`

The last processed match ID, last post and roster history persist through bot restarts.

# V92 — Server Watch Shared Text Channel

Date: 2026-09-09
Baseline: V91 Server Watch Intelligence

## Added
- Dual delivery for every Server Watch alert: private DMs to `Server Admin` role holders plus one shared Discord text channel.
- Supports stable channel-ID routing with `SERVER_WATCH_CHANNEL_ID`.
- Falls back to channel name `server-watch`, configurable with `SERVER_WATCH_CHANNEL_NAME`.
- Shared alert contains the same player, server, map, combat anomaly, kill-burst, Player ID, and Spawn-Hunt / Movement Analysis evidence as the DM.
- `/server-watch-status` displays shared-channel discovery state.
- `/server-watch-test` confirms both DM and channel delivery.
- Shared channel delivery can continue even when no role recipient is available, as long as the configured channel exists.

## Safety
Server Watch remains human-review only. No automatic punish, kick, or ban action is performed.

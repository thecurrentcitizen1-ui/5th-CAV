# V91 — Server Watch Intelligence / Movement Report
Date: 2026-09-09
Baseline: Battalion Clerk V90 Server Watch Dual-Server Alerts

## Added
- Server Watch now retains a short-lived position trail for active public players on both Server 1 and Server 2.
- Position evidence is isolated from permanent 1/5 Cavalry career telemetry and automatically purged after six hours.
- Suspected-player Server Admin DMs now include a SPAWN-HUNT / MOVEMENT ANALYSIS section.
- The movement section shows sample count/window, route traveled, net displacement, and route directness.
- A configurable DIRECT-ROUTE ANOMALY can add evidence to the Server Watch suspicion score.
- Default movement window: 10 minutes.
- Default direct-route signal: 88% directness across at least 400 meters.

## Important evidence boundary
Battalion Clerk does not claim it knows the exact location of a Garrison or Outpost unless HLL: Vietnam actually exposes such a coordinate/event to RCON. V91 therefore labels this as behavioral movement evidence. It can show that a suspected player repeatedly follows unusually direct routes through the tracked area, but this signal alone never proves cheating and never causes an automatic kick or ban.

## Alert recipients
Every non-bot Discord member holding the exact `Server Admin` role receives the same private Server Watch report.

## Servers
- Server 1: monitored
- Server 2: monitored

## Human review policy
Automatic punishment remains OFF. Server Admins should observe the player, preserve video/log evidence, compare repeated match behavior, and make the moderation decision manually.

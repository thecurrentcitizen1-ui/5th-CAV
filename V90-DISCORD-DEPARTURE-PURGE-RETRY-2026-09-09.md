V90 — Discord Departure Purge Retry — 2026-09-09

- Member leaving the Discord server now triggers the authoritative Website personnel-departure purge endpoint.
- Battalion Clerk retries the purge up to 3 times with short backoff on transient API failures.
- Success/failure is logged clearly so ghost personnel records are less likely to remain after a Discord departure.

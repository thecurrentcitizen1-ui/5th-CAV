# HLLV Timer Compatibility Hotfix

Fixes HLL: Vietnam RCON server-session timer parsing when current builds return ISO-8601 duration strings such as `PT1H30M` instead of integer seconds.

The collector now accepts numeric seconds, numeric strings, `timedelta` values, and ISO-8601 day/hour/minute/second durations.

No Railway variable changes are required.

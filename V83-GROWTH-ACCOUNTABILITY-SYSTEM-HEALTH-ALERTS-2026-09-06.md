# V83 — Growth Accountability + System Health Alerts

- Hourly growth-accountability worker calls the Website's idempotent action engine.
- New/overdue accountability actions notify the configured Personnel Suspense channel.
- Adds `/system-health-channel` for Command-facing failure/recovery alerts.
- Five-minute health watch alerts only on state changes to prevent Discord spam.
- V82 progression audit, dual-server telemetry, recruiting/link reliability and combat roster features are preserved.

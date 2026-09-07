# V88 — Railway Token + Voice Runtime Hardening

- Accepts `DISCORD_TOKEN` as the primary bot token variable, with compatibility fallbacks for `DISCORD_BOT_TOKEN` and `BOT_TOKEN`.
- Adds `PyNaCl` and `davey` runtime dependencies so Discord voice support warnings do not leave the runtime without voice capability.
- The bot still refuses to start when no Discord token exists; a secret cannot and should not be embedded in the package.
- Built cumulatively from V87, preserving the retry-safe Server #1 combat roster repair.

"""Battalion Clerk startup wrapper for compatibility hotfixes."""
from pathlib import Path

import production_runtime_hardening  # patches validated SQL/RCON type issues before imports
import identity_link_compat  # installs HLL identity compatibility before bot startup
import completed_game_integrity  # prevents Offensive timer extensions from inflating completed games
import v100_replacement_intake_runtime

# V100 injects the revised Discord intake immediately before normal bot startup.
# bot.py remains the authoritative Battalion Clerk implementation; only the
# Replacement Intake UI/classes are overridden at runtime.
_bot_path = Path(__file__).with_name("bot.py")
_source = _bot_path.read_text(encoding="utf-8")
_source = v100_replacement_intake_runtime.inject(_source)
exec(compile(_source, str(_bot_path), "exec"), {
    "__name__": "__main__",
    "__file__": str(_bot_path),
    "__package__": None,
})

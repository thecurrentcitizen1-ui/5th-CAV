"""Battalion Clerk startup wrapper for compatibility hotfixes."""
import runpy
import production_runtime_hardening  # patches validated SQL/RCON type issues before imports
import identity_link_compat  # installs HLL identity compatibility before bot startup
import completed_game_integrity  # prevents Offensive timer extensions from inflating completed games
runpy.run_path("bot.py", run_name="__main__")

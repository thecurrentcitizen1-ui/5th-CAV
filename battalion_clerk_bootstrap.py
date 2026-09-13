"""Battalion Clerk startup wrapper for compatibility hotfixes."""
import runpy
import production_runtime_hardening  # patches validated SQL/RCON type issues before imports
import identity_link_compat  # installs HLL identity compatibility before bot startup
runpy.run_path("bot.py", run_name="__main__")

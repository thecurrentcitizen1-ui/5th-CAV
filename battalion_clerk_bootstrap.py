"""Legacy startup shim; production now runs bot.py directly."""
import runpy
runpy.run_path("bot.py", run_name="__main__")

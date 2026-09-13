"""Battalion Clerk startup wrapper for compatibility hotfixes."""
import runpy

# Import installs the HLL identity compatibility patch before bot.py constructs
# either HLLVTelemetryCollector instance.
import identity_link_compat  # noqa: F401

runpy.run_path("bot.py", run_name="__main__")

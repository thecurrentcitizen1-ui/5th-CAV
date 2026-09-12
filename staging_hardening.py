from pathlib import Path


def replace_or_already(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        print(f"[STAGING HARDENING] already applied: {label}")
        return
    if old in text:
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        print(f"[STAGING HARDENING] applied: {label}")
        return
    # Do not crash the staging container solely because upstream source formatting
    # changed. The validation log makes the skipped target explicit so it can be
    # inspected before any production promotion.
    print(f"[STAGING HARDENING] WARNING target not found: {label}")


bot = Path("bot.py")
hll = Path("hllv_rcon.py")

# PostgreSQL UPDATE ... FROM makes unqualified completed_at ambiguous because
# both squad_handoff_tasks and welcome_packets expose a completed_at column.
replace_or_already(
    bot,
    "completed_at=COALESCE(completed_at,NOW())",
    "completed_at=COALESCE(sht.completed_at,NOW())",
    "qualify squad handoff completed_at",
)

# Two recruiting probes exist in Clerk but do not have matching Website routes.
# Do not hammer the Website with 404s while staging is isolated. The supported
# approved-pending accession route remains active and authoritative.
replace_or_already(
    bot,
    "data=await web.request('GET','/internal/clerk/recruiting/unlinked-approved',params={'guild_id':guild.id})",
    "data={'cases':[]}",
    "disable unsupported unlinked-approved probe",
)
replace_or_already(
    bot,
    "missing=await web.request('GET','/internal/clerk/recruiting/approved-missing-personnel',params={'guild_id':guild.id})",
    "missing={'cases':[]}",
    "disable unsupported approved-missing-personnel probe",
)

# asyncpg can infer CASE parameter types inconsistently when one branch is NULL.
# Explicit casts make the health UPSERT agree with the TIMESTAMPTZ/TEXT schema.
replace_or_already(
    hll,
    "CASE WHEN $1 THEN $2 ELSE NULL END,\n                CASE WHEN $1 THEN NULL ELSE $2 END,\n                CASE WHEN $1 THEN NULL ELSE $3 END,",
    "CASE WHEN $1 THEN $2::timestamptz ELSE NULL END,\n                CASE WHEN $1 THEN NULL ELSE $2::timestamptz END,\n                CASE WHEN $1 THEN NULL ELSE $3::text END,",
    "cast RCON health timestamp/text parameters",
)

print("[STAGING HARDENING] validation pass complete")

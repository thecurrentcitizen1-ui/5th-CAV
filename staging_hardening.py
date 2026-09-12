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

# asyncpg can infer CASE parameter types inconsistently when one branch is NULL.
# Explicit casts make the health UPSERT agree with the TIMESTAMPTZ/TEXT schema.
replace_or_already(
    hll,
    "CASE WHEN $1 THEN $2 ELSE NULL END,\n                CASE WHEN $1 THEN NULL ELSE $2 END,\n                CASE WHEN $1 THEN NULL ELSE $3 END,",
    "CASE WHEN $1 THEN $2::timestamptz ELSE NULL END,\n                CASE WHEN $1 THEN NULL ELSE $2::timestamptz END,\n                CASE WHEN $1 THEN NULL ELSE $3::text END,",
    "cast RCON health timestamp/text parameters",
)

# Staging must be able to stay online for database/Website validation while its
# Discord token and HLL RCON connections remain intentionally disabled. This
# mode never calls bot.run(), never opens a Discord gateway, and only exercises
# the staging database plus Website Clerk endpoints.
offline_block = '''if str(os.getenv("STAGING_OFFLINE_MODE", "")).strip().lower() in {"1","true","yes","on"}:
    async def _staging_offline_main():
        log.warning('[STAGING OFFLINE] Discord gateway disabled; running DB/Website validation only')
        await collector.start()
        db = collector.db
        if not db.pool:
            raise RuntimeError('STAGING OFFLINE: DATABASE_URL unavailable')
        row = await db.fetchrow('SELECT NOW() AS database_time')
        log.info('[STAGING OFFLINE] database ready time=%s', row.get('database_time') if row else None)

        # Build/reconcile only staging-side Clerk/HLL tables. HLL collectors stay
        # disconnected because Railway keeps both HLL_RCON_ENABLED flags false.
        await ensure_clerk_settings_table()
        await ensure_leadership_due_out_schema()
        await hllv.ensure_schema()
        await hllv2.ensure_schema()
        log.info('[STAGING OFFLINE] Clerk/RCON schema validation complete')

        # Execute the exact handoff auto-close SQL that previously failed with
        # AmbiguousColumnError. Staging currently contains no live production
        # personnel, so this is a compile/contract regression check only.
        await db.execute("""UPDATE squad_handoff_tasks sht
                          SET status='COMPLETE',completed_at=COALESCE(sht.completed_at,NOW())
                          FROM welcome_packets wp
                          WHERE wp.personnel_id=sht.recruit_personnel_id
                            AND sht.status<>'COMPLETE'
                            AND UPPER(COALESCE(wp.status,'')) IN ('COMPLETE','CLOSED','ARCHIVED')""")
        log.info('[STAGING OFFLINE] squad handoff completed_at SQL regression check passed')

        # Exercise the RCON health UPSERT without opening an RCON connection.
        # This directly verifies the TIMESTAMPTZ/TEXT cast repair that production
        # previously rejected during every poll.
        await hllv._health(False, 'STAGING OFFLINE VALIDATION')
        await hllv2._health(False, 'STAGING OFFLINE VALIDATION')
        log.info('[STAGING OFFLINE] RCON health write regression check passed for both server slots')

        # Exercise supported read-only Website endpoints through the normal Clerk
        # client so URL/auth/route drift is visible without touching Discord.
        gid = GUILD_ID or TEST_GUILD_ID
        if WEBSITE_BASE_URL and CLERK_SYNC_KEY and gid:
            approved = await web.request('GET','/internal/clerk/recruiting/approved-pending',params={'guild_id':gid})
            unlinked = await web.request('GET','/internal/clerk/recruiting/unlinked-approved',params={'guild_id':gid})
            missing = await web.request('GET','/internal/clerk/recruiting/approved-missing-personnel',params={'guild_id':gid})
            tracking = await web.request('GET','/internal/clerk/tracking-health')
            log.info('[STAGING OFFLINE] Website Clerk probes ok approved=%s unlinked=%s missing_personnel=%s tracking=%s',
                     len(approved.get('cases') or []),len(unlinked.get('cases') or []),
                     len(missing.get('cases') or []),tracking.get('overall'))
        else:
            log.warning('[STAGING OFFLINE] Website Clerk probe skipped: URL/key/guild missing')

        # Stay alive so Railway reports healthy while remaining fully isolated.
        while True:
            await asyncio.sleep(300)

    asyncio.run(_staging_offline_main())
else:
    bot.run(TOKEN)'''
replace_or_already(
    bot,
    "bot.run(TOKEN)",
    offline_block,
    "enable staging offline validation mode",
)

print("[STAGING HARDENING] validation pass complete")

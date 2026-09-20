from pathlib import Path


def replace_required(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding='utf-8')
    if new in text:
        print(f'[PRODUCTION HARDENING] already applied: {label}')
        return
    if old not in text:
        raise RuntimeError(f'Production hardening target not found: {label}')
    path.write_text(text.replace(old, new, 1), encoding='utf-8')
    print(f'[PRODUCTION HARDENING] applied: {label}')


def replace_all_required(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding='utf-8')
    if new in text and old not in text:
        print(f'[PRODUCTION HARDENING] already applied: {label}')
        return
    count = text.count(old)
    if count < 1:
        raise RuntimeError(f'Production hardening target not found: {label}')
    path.write_text(text.replace(old, new), encoding='utf-8')
    print(f'[PRODUCTION HARDENING] applied: {label} count={count}')

bot=Path('bot.py')
hll=Path('hllv_rcon.py')

replace_required(
    bot,
    'completed_at=COALESCE(completed_at,NOW())',
    'completed_at=COALESCE(sht.completed_at,NOW())',
    'qualify squad handoff completed_at',
)
replace_required(
    hll,
    "CASE WHEN $1 THEN $2 ELSE NULL END,\n                CASE WHEN $1 THEN NULL ELSE $2 END,\n                CASE WHEN $1 THEN NULL ELSE $3 END,",
    "CASE WHEN $1 THEN $2::timestamptz ELSE NULL END,\n                CASE WHEN $1 THEN NULL ELSE $2::timestamptz END,\n                CASE WHEN $1 THEN NULL ELSE $3::text END,",
    'cast RCON health timestamp/text parameters',
)
replace_all_required(
    bot,
    "app_commands.Choice(name='Steam / PC', value='STEAM'),",
    "app_commands.Choice(name='Steam / PC (Steam)', value='STEAM'),",
    'clarify Steam platform choices',
)
replace_all_required(
    bot,
    "app_commands.Choice(name='Xbox', value='XBOX'),",
    "app_commands.Choice(name='Xbox / Microsoft Store PC', value='XBOX'),",
    'clarify Microsoft platform choices',
)


replace_required(
    bot,
    '        existing=await sync_personnel_identity(member,create_if_missing=False,reason="member_join")\n',
    '        try:\n'
    '            restored=await web.request(\'POST\',\'/internal/clerk/personnel/rejoin\',json={\'guild_id\':member.guild.id,\'discord_user_id\':member.id,\'reason\':\'member_rejoined_discord\'})\n'
    '            if restored.get(\'restored\'):\n'
    '                log.info(\'[PERSONNEL REJOIN RESTORED] member=%s personnel=%s\',member.id,restored.get(\'personnel_id\'))\n'
    '        except Exception as exc:\n'
    '            log.warning(\'[PERSONNEL REJOIN RESTORE FAILED] member=%s error=%s\',member.id,exc)\n'
    '        existing=await sync_personnel_identity(member,create_if_missing=False,reason="member_join")\n',
    'restore temporary Discord departure hold on rejoin',
)
replace_required(
    bot,
    "        cleanup='✅ Active 201 File closed and website/personnel cleanup completed.'\n",
    "        cleanup='✅ 201 File temporarily closed; member access disabled; Soldier removed from active roster and Recruiting Control until Discord rejoin.'\n",
    'clarify temporary Discord departure closure',
)

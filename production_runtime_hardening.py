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

replace_required(
    Path('bot.py'),
    'completed_at=COALESCE(completed_at,NOW())',
    'completed_at=COALESCE(sht.completed_at,NOW())',
    'qualify squad handoff completed_at',
)
replace_required(
    Path('hllv_rcon.py'),
    "CASE WHEN $1 THEN $2 ELSE NULL END,\n                CASE WHEN $1 THEN NULL ELSE $2 END,\n                CASE WHEN $1 THEN NULL ELSE $3 END,",
    "CASE WHEN $1 THEN $2::timestamptz ELSE NULL END,\n                CASE WHEN $1 THEN NULL ELSE $2::timestamptz END,\n                CASE WHEN $1 THEN NULL ELSE $3::text END,",
    'cast RCON health timestamp/text parameters',
)

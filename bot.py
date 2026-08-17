import os
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Tuple, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from collector import DataCollector

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
log = logging.getLogger('battalion-clerk')

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', '0') or 0)
TEST_GUILD_ID = int(os.getenv('TEST_GUILD_ID', '0') or 0)
COMMAND_GUILD_ID = TEST_GUILD_ID or GUILD_ID
WEBSITE_BASE_URL = os.getenv('WEBSITE_BASE_URL', '').strip().rstrip('/')
CLERK_SYNC_KEY = os.getenv('CLERK_SYNC_KEY', '').strip()
BATTALION_TIMEZONE = os.getenv('BATTALION_TIMEZONE', 'America/New_York').strip()
VOICE_FLUSH_SECONDS = max(60, int(os.getenv('VOICE_FLUSH_SECONDS', '300') or 300))
DUTY_TYPES = ('TRAINING', 'OPERATION', 'MEETING')

intents = discord.Intents.none()
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
collector = DataCollector()
collector_started = False
commands_synced = False

# General voice-session telemetry already used by Battalion Clerk.
# key: (guild_id, user_id) -> session metadata
voice_sessions: Dict[Tuple[int, int], dict] = {}

# Duty-credit tracking for the three official duty channels.
# guild_id -> {TRAINING|OPERATION|MEETING: channel_id}
duty_channel_bindings: Dict[int, Dict[str, int]] = {}
# (guild_id, user_id, channel_id) -> chunk start UTC datetime
duty_voice_presence: Dict[Tuple[int, int, int], datetime] = {}

# Announcement automation state. These sets prevent duplicate notices during a normal
# uninterrupted bot process. Persistent channel configuration is stored in PostgreSQL.
announcement_notice_sent = set()
announcement_reminder_sent = set()
announcement_start_sent = set()
announcement_end_sent = set()

# Personnel role changes are deliberately debounced. Staff often add rank, MOS,
# unit, platoon, squad and access roles one after another; waiting 30 seconds
# lets Battalion Clerk read the member's complete role set before opening the 201 File.
PERSONNEL_ROLE_SETTLE_SECONDS = 30
pending_personnel_sync: Dict[Tuple[int, int], asyncio.Task] = {}

RANK_ROLE_CODES = {"PVT","PFC","CPL","SP4","SP5","SGT","SP6","SSG","SFC","SP7","MSG","1SG","SGM","2LT","1LT","CPT","MAJ","LTC"}
MOS_ROLE_CODES = {"00C","11L","11R","11G","11M","91M","12E","76S","11S","11N","19C","19K","67L","67P","67C","67G","11O","11A","11T"}
RANK_ROLE_ALIASES = {
    "PRIVATE":"PVT","PRIVATE FIRST CLASS":"PFC","CORPORAL":"CPL","SERGEANT":"SGT",
    "STAFF SERGEANT":"SSG","SERGEANT FIRST CLASS":"SFC","MASTER SERGEANT":"MSG",
    "FIRST SERGEANT":"1SG","SERGEANT MAJOR":"SGM","SECOND LIEUTENANT":"2LT",
    "FIRST LIEUTENANT":"1LT","CAPTAIN":"CPT","MAJOR":"MAJ","LIEUTENANT COLONEL":"LTC"
}


# ---------------------------------------------------------------------------
# DISCORD BATTALION STRUCTURE BLUEPRINT
# ---------------------------------------------------------------------------
# Divider roles are visual-only. They are never assigned to members and are
# ignored by personnel synchronization. Functional roles remain deliberately
# permission-light; channel/category overwrites provide access while the
# website/personnel database remains authoritative for rank, MOS, assignment,
# and appointments.
DIVIDER_ROLE_NAMES = [
    "──────── BATTALION COMMAND ────────",
    "──────── RANK ────────",
    "──────── APPOINTMENTS ────────",
    "──────── BATTLEFIELD MOS ────────",
    "──────── COMPANY ASSIGNMENT ────────",
    "──────── PLATOON ASSIGNMENT ────────",
    "──────── SQUAD ASSIGNMENT ────────",
    "──────── QUALIFICATIONS ────────",
    "──────── STAFF ACCESS ────────",
]

RANK_ROLE_BLUEPRINT = [
    "LTC", "MAJ", "CPT", "1LT", "2LT", "SGM", "1SG", "MSG", "SFC",
    "SSG", "SGT", "SP7", "SP6", "SP5", "SP4", "CPL", "PFC", "PVT",
]

APPOINTMENT_ROLE_BLUEPRINT = [
    "Battalion Commander", "Battalion Executive Officer",
    "S-1 OIC", "S-1 NCOIC", "S-3 OIC", "S-3 NCOIC", "S-4 OIC", "S-4 NCOIC",
    "Company Commander", "Company Executive Officer", "First Sergeant",
    "Platoon Leader", "Platoon Sergeant", "Squad Leader", "Assistant Squad Leader",
    "Battalion Instructor",
]

MOS_ROLE_BLUEPRINT = [
    "00C — Battalion Commander",
    "11L — Infantry Squad Leader",
    "11R — Rifleman",
    "11G — Grenadier",
    "11M — Machine Gunner",
    "91M — Combat Medic",
    "12E — Combat Engineer",
    "76S — Supply & Support Specialist",
    "11S — Reconnaissance Team Leader",
    "11N — Sniper",
    "19C — Armor Commander",
    "19K — Armor Crewman",
    "67L — Aviation Logistics",
    "67P — Rotary-Wing Pilot",
    "67C — Helicopter Crew Chief",
    "67G — Aerial Door Gunner",
    "11O — Mortar Observer",
    "11A — Mortar Ammunition Bearer",
    "11T — Mortar Gunner",
]

COMPANY_ROLE_BLUEPRINT = [
    "HHC", "A Company", "B Company", "C Company",
]
PLATOON_ROLE_BLUEPRINT = [
    "1st Platoon", "2nd Platoon", "3rd Platoon", "4th Platoon",
]
SQUAD_ROLE_BLUEPRINT = [
    "1st Squad", "2nd Squad", "3rd Squad", "4th Squad",
]
QUALIFICATION_ROLE_BLUEPRINT = [
    "Battalion Instructor", "M16 Qualified", "Mortar Qualified", "Recon Qualified",
    "Aviation Qualified", "Armor Qualified", "Medic Qualified",
]
STAFF_ACCESS_ROLE_BLUEPRINT = [
    "Command Staff", "S-1 Personnel", "S-3 Operations", "S-4 Supply",
]

ROLE_SECTIONS = [
    ("──────── BATTALION COMMAND ────────", ["Command Staff"]),
    ("──────── RANK ────────", RANK_ROLE_BLUEPRINT),
    ("──────── APPOINTMENTS ────────", APPOINTMENT_ROLE_BLUEPRINT),
    ("──────── BATTLEFIELD MOS ────────", MOS_ROLE_BLUEPRINT),
    ("──────── COMPANY ASSIGNMENT ────────", COMPANY_ROLE_BLUEPRINT),
    ("──────── PLATOON ASSIGNMENT ────────", PLATOON_ROLE_BLUEPRINT),
    ("──────── SQUAD ASSIGNMENT ────────", SQUAD_ROLE_BLUEPRINT),
    ("──────── QUALIFICATIONS ────────", QUALIFICATION_ROLE_BLUEPRINT),
    ("──────── STAFF ACCESS ────────", STAFF_ACCESS_ROLE_BLUEPRINT),
]

# name, type where type is text/voice/forum. Forum creation is supported by
# discord.py on compatible Discord guilds; this blueprint intentionally uses
# text and voice only so a fresh server can be built reliably everywhere.
CHANNEL_BLUEPRINT = [
    {
        "category": "REPLACEMENT DETACHMENT",
        "scope": "PUBLIC",
        "channels": [
            ("welcome-to-the-1-5", "text"),
            ("recruiting-office", "text"),
            ("standing-orders", "text"),
            ("enlistment-help", "text"),
            ("replacement-reception", "voice"),
        ],
    },
    {
        "category": "BATTALION HEADQUARTERS",
        "scope": "MEMBER",
        "channels": [
            ("battalion-orders", "text"),
            ("headquarters-notices", "text"),
            ("personnel-orders", "text"),
            ("promotions-and-awards", "text"),
            ("command-post", "voice"),
        ],
    },
    {
        "category": "S-1 PERSONNEL",
        "scope": "S1",
        "channels": [
            ("s1-personnel", "text"),
            ("personnel-actions", "text"),
            ("award-processing", "text"),
            ("replacement-processing", "text"),
            ("s1-in-processing", "voice"),
        ],
    },
    {
        "category": "S-3 OPERATIONS & TRAINING",
        "scope": "S3",
        "channels": [
            ("s3-operations", "text"),
            ("operation-orders", "text"),
            ("training-circulars", "text"),
            ("after-action-reports", "text"),
            ("operations-briefing", "voice"),
            ("the-lz", "voice"),
            ("the-range", "voice"),
        ],
    },
    {
        "category": "S-4 SUPPLY & LOGISTICS",
        "scope": "S4",
        "channels": [
            ("s4-supply", "text"),
            ("arms-room", "text"),
            ("property-book", "text"),
            ("logistics", "text"),
            ("supply-counter", "voice"),
        ],
    },
    {
        "category": "BATTALION COMMAND",
        "scope": "COMMAND",
        "channels": [
            ("command-desk", "text"),
            ("command-actions", "text"),
            ("staff-duty-log", "text"),
            ("command-conference", "voice"),
        ],
    },
]

for _company in ("A", "B", "C"):
    CHANNEL_BLUEPRINT.append({
        "category": f"{_company} COMPANY",
        "scope": f"COMPANY:{_company}",
        "channels": [
            ("company-headquarters", "text"),
            ("1st-platoon", "text"),
            ("2nd-platoon", "text"),
            ("3rd-platoon", "text"),
            ("company-formation", "voice"),
            ("1st-platoon", "voice"),
            ("2nd-platoon", "voice"),
            ("3rd-platoon", "voice"),
        ],
    })


def _role_by_name(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    return discord.utils.get(guild.roles, name=name)


def _member_access_roles(guild: discord.Guild):
    names = set(RANK_ROLE_BLUEPRINT + STAFF_ACCESS_ROLE_BLUEPRINT)
    return [r for r in guild.roles if r.name in names]


def _scope_overwrites(guild: discord.Guild, scope: str):
    everyone = guild.default_role
    overwrites = {everyone: discord.PermissionOverwrite(view_channel=False)}

    if scope == "PUBLIC":
        overwrites[everyone] = discord.PermissionOverwrite(view_channel=True, read_messages=True)
        return overwrites

    if scope == "MEMBER":
        for role in _member_access_roles(guild):
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_messages=True, connect=True)
        return overwrites

    allowed_names = {"Command Staff"}
    if scope == "S1": allowed_names.add("S-1 Personnel")
    elif scope == "S3": allowed_names.add("S-3 Operations")
    elif scope == "S4": allowed_names.add("S-4 Supply")
    elif scope == "COMMAND": pass
    elif scope.startswith("COMPANY:"):
        letter = scope.split(":", 1)[1]
        allowed_names.update({f"{letter} Company", "S-1 Personnel", "S-3 Operations"})

    for name in allowed_names:
        role = _role_by_name(guild, name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_messages=True, connect=True)
    return overwrites


async def build_battalion_roles(guild: discord.Guild):
    created=[]; existing=[]; failed=[]
    me = guild.me
    if not me or not me.guild_permissions.manage_roles:
        raise RuntimeError("Battalion Clerk needs Manage Roles permission.")

    # Create bottom-up so the hierarchy is easy to read even before a position pass.
    for divider, roles in reversed(ROLE_SECTIONS):
        for name in reversed(roles):
            role = _role_by_name(guild, name)
            if role:
                existing.append(name)
            else:
                try:
                    await guild.create_role(name=name, permissions=discord.Permissions.none(), hoist=False, mentionable=False,
                                            reason="Battalion Clerk — 1/5 CAV role structure")
                    created.append(name)
                except Exception as exc:
                    failed.append(f"{name}: {exc}")
        role = _role_by_name(guild, divider)
        if role:
            existing.append(divider)
        else:
            try:
                await guild.create_role(name=divider, permissions=discord.Permissions.none(), hoist=False, mentionable=False,
                                        reason="Battalion Clerk — visual role divider")
                created.append(divider)
            except Exception as exc:
                failed.append(f"{divider}: {exc}")

    # Position the whole managed block immediately below the bot's highest role.
    # Discord does not allow a bot to move roles above its own top role.
    try:
        ordered_names=[]
        for divider, roles in ROLE_SECTIONS:
            ordered_names.append(divider)
            ordered_names.extend(roles)
        ordered_roles=[_role_by_name(guild,n) for n in ordered_names]
        ordered_roles=[r for r in ordered_roles if r and r < me.top_role]
        start=max(1, me.top_role.position-len(ordered_roles))
        positions={role:start+i for i,role in enumerate(reversed(ordered_roles))}
        if positions:
            await guild.edit_role_positions(positions=positions, reason="Battalion Clerk — organize 1/5 CAV role hierarchy")
    except Exception as exc:
        failed.append(f"Role hierarchy arrangement: {exc}")
    return {"created":created,"existing":existing,"failed":failed}


async def build_battalion_channels(guild: discord.Guild):
    created=[]; existing=[]; failed=[]
    me=guild.me
    if not me or not me.guild_permissions.manage_channels:
        raise RuntimeError("Battalion Clerk needs Manage Channels permission.")

    for spec in CHANNEL_BLUEPRINT:
        category=discord.utils.get(guild.categories,name=spec["category"])
        if not category:
            try:
                category=await guild.create_category(spec["category"], overwrites=_scope_overwrites(guild,spec["scope"]),
                                                     reason="Battalion Clerk — 1/5 CAV category structure")
                created.append(f"CATEGORY:{spec['category']}")
            except Exception as exc:
                failed.append(f"CATEGORY {spec['category']}: {exc}")
                continue
        else:
            existing.append(f"CATEGORY:{spec['category']}")
            try:
                await category.edit(overwrites=_scope_overwrites(guild,spec["scope"]),
                                    reason="Battalion Clerk — repair category permissions")
            except Exception as exc:
                failed.append(f"PERMISSIONS {spec['category']}: {exc}")

        for channel_name,channel_type in spec["channels"]:
            if channel_type == "text":
                found=discord.utils.get(category.text_channels,name=channel_name)
            else:
                found=discord.utils.get(category.voice_channels,name=channel_name)
            if found:
                existing.append(f"{channel_type.upper()}:{spec['category']}/{channel_name}")
                continue
            try:
                if channel_type == "text":
                    await guild.create_text_channel(channel_name, category=category, reason="Battalion Clerk — 1/5 CAV channel structure")
                else:
                    await guild.create_voice_channel(channel_name, category=category, reason="Battalion Clerk — 1/5 CAV channel structure")
                created.append(f"{channel_type.upper()}:{spec['category']}/{channel_name}")
            except Exception as exc:
                failed.append(f"{channel_type.upper()} {spec['category']}/{channel_name}: {exc}")
    return {"created":created,"existing":existing,"failed":failed}


def structure_inventory(guild: discord.Guild):
    expected_roles=[]
    for divider,roles in ROLE_SECTIONS:
        expected_roles.append(divider); expected_roles.extend(roles)
    missing_roles=[name for name in expected_roles if not _role_by_name(guild,name)]
    missing_categories=[]; missing_channels=[]
    for spec in CHANNEL_BLUEPRINT:
        cat=discord.utils.get(guild.categories,name=spec["category"])
        if not cat:
            missing_categories.append(spec["category"])
            continue
        for name,kind in spec["channels"]:
            collection=cat.text_channels if kind=="text" else cat.voice_channels
            if not discord.utils.get(collection,name=name):
                missing_channels.append(f"{spec['category']} / {name} ({kind})")
    return {"missing_roles":missing_roles,"missing_categories":missing_categories,"missing_channels":missing_channels,
            "expected_roles":len(expected_roles),"expected_categories":len(CHANNEL_BLUEPRINT),
            "expected_channels":sum(len(x["channels"]) for x in CHANNEL_BLUEPRINT)}

def _role_code_hits(member: discord.Member):
    ranks=[]; mos=[]
    for role in member.roles:
        if role.name == "@everyone": continue
        name=" ".join(role.name.upper().strip().split())
        tokens=set(name.replace("—"," ").replace("-"," ").split())
        for code in RANK_ROLE_CODES:
            if code in tokens or name==code:
                ranks.append((code,role.name))
        for label,code in RANK_ROLE_ALIASES.items():
            if name==label:
                ranks.append((code,role.name))
        for code in MOS_ROLE_CODES:
            if code in tokens or name.startswith(code+" ") or name==code:
                mos.append((code,role.name))
    # keep unique codes while preserving the human role name for diagnostics
    ranks=list({code:name for code,name in ranks}.items())
    mos=list({code:name for code,name in mos}.items())
    return ranks,mos

def validate_personnel_roles(member: discord.Member):
    ranks,mos=_role_code_hits(member)
    problems=[]
    role_names=[" ".join(r.name.upper().strip().split()) for r in member.roles if r.name!="@everyone"]
    companies=[]; platoons=[]; squads=[]
    for name in role_names:
        if name in {"A COMPANY","ALPHA COMPANY","A/1-5 CAV","B COMPANY","BRAVO COMPANY","B/1-5 CAV","C COMPANY","CHARLIE COMPANY","C/1-5 CAV","HHC","HHC/1-5 CAV","HEADQUARTERS & HEADQUARTERS COMPANY"}:
            companies.append(name)
        if any(x in name for x in ("1ST PLATOON","2ND PLATOON","3RD PLATOON","4TH PLATOON")):
            platoons.append(name)
        if any(x in name for x in ("1ST SQUAD","2ND SQUAD","3RD SQUAD","4TH SQUAD")):
            squads.append(name)
    if len(ranks)==0: problems.append("recognized rank role required")
    elif len(ranks)>1: problems.append("multiple rank roles: "+", ".join(code for code,_ in ranks))
    if len(mos)==0: problems.append("recognized battlefield MOS role required")
    elif len(mos)>1: problems.append("multiple primary MOS roles: "+", ".join(code for code,_ in mos))
    if len(companies)>1: problems.append("multiple company roles: "+", ".join(companies))
    if len(platoons)>1: problems.append("multiple platoon roles: "+", ".join(platoons))
    if len(squads)>1: problems.append("multiple squad roles: "+", ".join(squads))
    return {"valid":not problems,"rank":ranks[0][0] if len(ranks)==1 else None,"mos":mos[0][0] if len(mos)==1 else None,"problems":problems,"ranks":ranks,"mos_roles":mos,"company":companies[0] if len(companies)==1 else None,"platoon":platoons[0] if len(platoons)==1 else None,"squad":squads[0] if len(squads)==1 else None}

async def _settled_personnel_sync(guild_id: int, member_id: int, reason: str):
    try:
        await asyncio.sleep(PERSONNEL_ROLE_SETTLE_SECONDS)
        guild = bot.get_guild(guild_id)
        member = guild.get_member(member_id) if guild else None
        if not member or member.bot:
            return
        await collector.upsert_member(member)
        validation=validate_personnel_roles(member)
        if not validation["valid"]:
            log.warning('[PERSONNEL VALIDATION HOLD] member=%s (%s): %s',member.display_name,member.id,'; '.join(validation['problems']))
            return
        log.info('[PERSONNEL VALIDATED] member=%s (%s) rank=%s mos=%s',member.display_name,member.id,validation.get('rank'),validation.get('mos'))
        result = await sync_personnel_identity(member, create_if_missing=True,
                                               reason=reason, deliver_credentials=True)
        log.info('[PERSONNEL ROLE SETTLED] member=%s (%s) result=%s',
                 member.display_name, member.id, (result or {}).get('reason') or ('created' if (result or {}).get('created') else 'synced'))
    except asyncio.CancelledError:
        return
    except Exception as exc:
        log.warning('[PERSONNEL ROLE SETTLE FAILED] guild=%s member=%s error=%s', guild_id, member_id, exc)
    finally:
        pending_personnel_sync.pop((guild_id, member_id), None)

def schedule_personnel_role_sync(member: discord.Member, reason: str = 'roles_settled'):
    key = (member.guild.id, member.id)
    previous = pending_personnel_sync.get(key)
    if previous and not previous.done():
        previous.cancel()
    pending_personnel_sync[key] = asyncio.create_task(_settled_personnel_sync(member.guild.id, member.id, reason))
    log.info('[PERSONNEL ROLE COOLDOWN] member=%s (%s) waiting=%ss', member.display_name, member.id, PERSONNEL_ROLE_SETTLE_SECONDS)


async def ensure_clerk_settings_table():
    """Create the small bot-side settings table without touching website tables."""
    db = getattr(collector, 'db', None)
    if not db or not getattr(db, 'pool', None):
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS clerk_guild_settings (
            guild_id TEXT PRIMARY KEY,
            orders_channel_id TEXT,
            operation_duty_channel_id TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await db.execute("ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS operation_duty_channel_id TEXT")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS clerk_order_routes (
            guild_id TEXT NOT NULL,
            order_type TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (guild_id, order_type)
        )
    """)


async def set_orders_channel(guild_id: int, channel_id: int):
    await ensure_clerk_settings_table()
    db = getattr(collector, 'db', None)
    if not db or not getattr(db, 'pool', None):
        raise RuntimeError('PostgreSQL is not available for Battalion Clerk settings.')
    await db.execute("""
        INSERT INTO clerk_guild_settings (guild_id, orders_channel_id, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (guild_id)
        DO UPDATE SET orders_channel_id = EXCLUDED.orders_channel_id,
                      updated_at = NOW()
    """, str(guild_id), str(channel_id))


async def get_orders_channel_id(guild_id: int) -> Optional[int]:
    await ensure_clerk_settings_table()
    db = getattr(collector, 'db', None)
    if not db or not getattr(db, 'pool', None):
        return None
    async with db.pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT orders_channel_id FROM clerk_guild_settings WHERE guild_id = $1",
            str(guild_id),
        )
    return int(value) if value else None


async def set_operation_duty_channel(guild_id: int, channel_id: int):
    await ensure_clerk_settings_table()
    db=getattr(collector,'db',None)
    if not db or not getattr(db,'pool',None): raise RuntimeError('PostgreSQL is not available for Battalion Clerk settings.')
    await db.execute("""INSERT INTO clerk_guild_settings(guild_id,operation_duty_channel_id,updated_at) VALUES($1,$2,NOW())
                        ON CONFLICT(guild_id) DO UPDATE SET operation_duty_channel_id=EXCLUDED.operation_duty_channel_id,updated_at=NOW()""",str(guild_id),str(channel_id))

async def get_operation_duty_channel_id(guild_id: int) -> Optional[int]:
    await ensure_clerk_settings_table()
    db=getattr(collector,'db',None)
    if not db or not getattr(db,'pool',None): return None
    async with db.pool.acquire() as conn:
        value=await conn.fetchval("SELECT operation_duty_channel_id FROM clerk_guild_settings WHERE guild_id=$1",str(guild_id))
    return int(value) if value else None

async def set_order_route(guild_id: int, order_type: str, channel_id: int):
    await ensure_clerk_settings_table()
    db = getattr(collector, 'db', None)
    await db.execute("""INSERT INTO clerk_order_routes(guild_id,order_type,channel_id,updated_at) VALUES($1,$2,$3,NOW())
                        ON CONFLICT(guild_id,order_type) DO UPDATE SET channel_id=EXCLUDED.channel_id,updated_at=NOW()""",
                     str(guild_id), order_type.upper(), str(channel_id))

async def clear_order_route(guild_id: int, order_type: str):
    db = getattr(collector, 'db', None)
    if db and getattr(db, 'pool', None):
        await db.execute("DELETE FROM clerk_order_routes WHERE guild_id=$1 AND order_type=$2", str(guild_id), order_type.upper())

async def get_order_routes(guild_id: int):
    await ensure_clerk_settings_table()
    db = getattr(collector, 'db', None)
    if not db or not getattr(db, 'pool', None): return {}
    async with db.pool.acquire() as conn:
        rows = await conn.fetch("SELECT order_type,channel_id FROM clerk_order_routes WHERE guild_id=$1", str(guild_id))
    return {str(r['order_type']).upper(): int(r['channel_id']) for r in rows}

def event_timestamp(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def order_heading(event_type: str) -> str:
    kind = (event_type or '').upper()
    if kind == 'OPERATION':
        return 'OPERATIONS NOTICE'
    if kind == 'TRAINING':
        return 'TRAINING CIRCULAR'
    return 'BATTALION NOTICE'


async def post_battalion_order(
    guild: discord.Guild,
    event: dict,
    phase: str,
    *,
    close_summary: Optional[dict] = None,
):
    channel_id = await get_orders_channel_id(guild.id)
    if not channel_id:
        return False
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False

    title = event.get('title') or event.get('event_type') or 'UNNAMED DUTY'
    event_type = (event.get('event_type') or 'DUTY').upper()
    start = event_timestamp(event.get('starts_at'))
    end = event_timestamp(event.get('ends_at'))
    duty_channel_id = event.get('channel_id')
    duty_station = f'<#{duty_channel_id}>' if duty_channel_id else 'AS DIRECTED'

    if phase == 'filed':
        heading = order_heading(event_type)
        body = (
            f'**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n'
            f'**{heading}**\n\n'
            f'**{title.upper()}**\n'
            f'Classification: **{event_type.title()}**\n'
            f'Duty Station: {duty_station}\n'
            f'Step-Off: <t:{int(start.timestamp())}:F>\n' if start else ''
        )
        if end:
            body += f'Duty Period Ends: <t:{int(end.timestamp())}:t>\n'
        body += (
            'Minimum Service Credit: **45 minutes present**\n\n'
            f'All available personnel will report to {duty_station} prior to commencement of duty.\n\n'
            '**BY ORDER OF THE BATTALION COMMANDER**'
        )
    elif phase == 'reminder':
        body = (
            f'**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n'
            f'**15 MINUTES TO STEP-OFF**\n\n'
            f'**{title.upper()}**\n'
            f'Report to {duty_station}.\n'
            f'Step-Off: <t:{int(start.timestamp())}:R>' if start else f'Report to {duty_station}.'
        )
    elif phase == 'start':
        label = 'OPERATION COMMENCED' if event_type == 'OPERATION' else 'DUTY PERIOD COMMENCED'
        body = (
            f'**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n'
            f'**{label}**\n\n'
            f'**{title.upper()}**\n'
            f'{duty_station} is now the designated duty station.\n'
            'Personnel must remain present for **45 qualifying minutes** to receive service credit.'
        )
    else:
        label = 'OPERATION CONCLUDED' if event_type == 'OPERATION' else 'DUTY PERIOD CONCLUDED'
        tracked = (close_summary or {}).get('tracked', 0)
        credited = (close_summary or {}).get('credited', 0)
        body = (
            f'**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n'
            f'**{label}**\n\n'
            f'**{title.upper()}**\n'
            f'Personnel Tracked: **{tracked}**\n'
            f'Personnel Credited: **{credited}**\n\n'
            'Attendance and qualifying service have been forwarded for filing.'
        )

    await channel.send(body[:2000])
    return True


@tasks.loop(seconds=60)
async def personnel_orders_watch():
    """Post newly filed personnel orders to the Discord channels selected by command."""
    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID: continue
        try:
            routes = await get_order_routes(guild.id)
            if not routes: continue
            payload = await web.request('GET','/internal/clerk/orders/pending',params={'guild_id':guild.id})
            for order in payload.get('orders',[]):
                kind = str(order.get('document_type') or '').upper()
                channel_id = routes.get(kind) or routes.get('ALL')
                if not channel_id: continue
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel): continue
                soldier = f"{order.get('rank_code') or ''} {order.get('first_name') or ''} {order.get('last_name') or ''}".strip()
                body = (f"**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n"
                        f"**{order.get('document_number')} — {order.get('title')}**\n\n"
                        f"**SOLDIER:** {soldier}\n"
                        f"**EFFECTIVE:** {order.get('effective_date')}\n"
                        f"**TYPE:** {kind.replace('_',' ')}\n\n"
                        f"{order.get('body_text')}\n\n"
                        f"**{order.get('authority') or 'BY ORDER OF THE BATTALION COMMANDER'}**")
                await channel.send(body[:2000])
                await web.request('POST',f"/internal/clerk/orders/{order['id']}/posted",json={'guild_id':guild.id})
        except Exception as exc:
            log.warning('[PERSONNEL ORDERS] guild=%s error=%s',guild.id,exc)

@tasks.loop(seconds=60)
async def duty_announcement_watch():
    """Publish 15-minute and start notices for scheduled duty periods."""
    now = utc_now()
    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID:
            continue
        try:
            status = await web.request('GET', '/internal/clerk/events/status', params={'guild_id': guild.id})
        except Exception as exc:
            log.warning('[ANNOUNCEMENT WATCH] status failed guild=%s error=%s', guild.id, exc)
            continue

        for event in status.get('events', []):
            event_id = str(event.get('id') or event.get('event_id') or '')
            if not event_id:
                continue
            start = event_timestamp(event.get('starts_at'))
            end = event_timestamp(event.get('ends_at'))
            if not start:
                continue

            seconds_to_start = (start - now).total_seconds()
            # Give a 2-minute polling tolerance around the 15-minute notice.
            if -30 <= seconds_to_start <= 15 * 60 and event_id not in announcement_reminder_sent:
                # If already started, don't back-fill a late 15-minute reminder.
                if seconds_to_start > 0:
                    if await post_battalion_order(guild, event, 'reminder'):
                        announcement_reminder_sent.add(event_id)

            if start <= now and (end is None or now <= end + timedelta(minutes=2)):
                if event_id not in announcement_start_sent:
                    if await post_battalion_order(guild, event, 'start'):
                        announcement_start_sent.add(event_id)


@duty_announcement_watch.before_loop
async def before_announcement_watch():
    await bot.wait_until_ready()


class WebsiteClient:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def request(self, method: str, path: str, *, params=None, json=None):
        await self.start()
        if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY:
            raise RuntimeError('WEBSITE_BASE_URL and CLERK_SYNC_KEY must be configured')
        headers = {'X-Battalion-Clerk-Key': CLERK_SYNC_KEY}
        async with self.session.request(
            method,
            f'{WEBSITE_BASE_URL}{path}',
            params=params,
            json=json,
            headers=headers,
        ) as response:
            try:
                body = await response.json()
            except Exception:
                body = {'ok': False, 'error': await response.text()}
            if response.status >= 400:
                raise RuntimeError(body.get('error') or f'Website returned HTTP {response.status}')
            return body


web = WebsiteClient()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def fmt_duration(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def begin_session(member: discord.Member, channel: discord.abc.GuildChannel, started_at: datetime, recovered: bool = False):
    voice_sessions[(member.guild.id, member.id)] = {
        'started_at': started_at,
        'channel_id': str(channel.id),
        'channel_name': channel.name,
        'recovered': recovered,
    }


async def close_session(member: discord.Member, ended_at: datetime, reason: str):
    session = voice_sessions.pop((member.guild.id, member.id), None)
    if not session:
        log.warning('[VOICE SESSION MISSING] %s (%s) reason=%s', member.display_name, member.id, reason)
        return None

    started_at = session['started_at']
    duration_seconds = max(0, int((ended_at - started_at).total_seconds()))
    payload = {
        'guild_id': str(member.guild.id),
        'discord_user_id': str(member.id),
        'username': member.name,
        'display_name': member.display_name,
        'channel_id': session['channel_id'],
        'channel_name': session['channel_name'],
        'started_at': iso(started_at),
        'ended_at': iso(ended_at),
        'duration_seconds': duration_seconds,
        'duration_hms': fmt_duration(duration_seconds),
        'close_reason': reason,
        'recovered_after_restart': bool(session.get('recovered')),
    }
    await collector.record_event('voice_session', payload)
    return payload


async def load_duty_bindings(guild_id: int):
    data = await web.request('GET', '/internal/clerk/channels', params={'guild_id': guild_id})
    duty_channel_bindings[guild_id] = {
        row['event_type']: int(row['channel_id'])
        for row in data.get('channels', [])
    }
    return data.get('channels', [])


def duty_type_for_channel(guild_id: int, channel_id: int) -> Optional[str]:
    for event_type, cid in duty_channel_bindings.get(guild_id, {}).items():
        if int(cid) == int(channel_id):
            return event_type
    return None



def member_role_names(member: discord.Member):
    return [role.name for role in member.roles if role.name != "@everyone"]

def _guild_role_for_code(guild: discord.Guild, code: str, code_set):
    if not code: return None
    code=code.upper()
    for role in guild.roles:
        name=" ".join(role.name.upper().strip().split())
        tokens=set(name.replace("—"," ").replace("-"," ").split())
        if code in tokens or name.startswith(code+" ") or name==code:
            return role
        if code_set is RANK_ROLE_CODES:
            for label,c in RANK_ROLE_ALIASES.items():
                if c==code and name==label: return role
    return None

async def reconcile_member_roles_from_canonical(member: discord.Member, result: dict):
    """Website personnel row is authoritative after intake; Discord mirrors it."""
    if not result.get('linked'): return
    rank=result.get('rank_code'); mos=result.get('mos_code')
    desired=[]
    rank_role=_guild_role_for_code(member.guild,rank,RANK_ROLE_CODES)
    mos_role=_guild_role_for_code(member.guild,mos,MOS_ROLE_CODES)
    if rank_role: desired.append(rank_role)
    if mos_role: desired.append(mos_role)
    current_ranks,current_mos=_role_code_hits(member)
    remove=[]
    for code,_ in current_ranks:
        if code!=rank:
            r=_guild_role_for_code(member.guild,code,RANK_ROLE_CODES)
            if r: remove.append(r)
    for code,_ in current_mos:
        if code!=mos:
            r=_guild_role_for_code(member.guild,code,MOS_ROLE_CODES)
            if r: remove.append(r)
    # Assignment roles also mirror the canonical website record when matching roles exist.
    unit=(result.get('unit_code') or '').upper().strip(); platoon=(result.get('platoon') or '').upper().strip(); squad=(result.get('squad') or '').upper().strip()
    company_aliases={
        'A/1-5 CAV':{'A COMPANY','ALPHA COMPANY','A/1-5 CAV'},
        'B/1-5 CAV':{'B COMPANY','BRAVO COMPANY','B/1-5 CAV'},
        'C/1-5 CAV':{'C COMPANY','CHARLIE COMPANY','C/1-5 CAV'},
        'HHC/1-5 CAV':{'HHC','HHC/1-5 CAV','HEADQUARTERS & HEADQUARTERS COMPANY'},
    }
    all_company_names=set().union(*company_aliases.values())
    desired_company=company_aliases.get(unit,set())
    for role in member.roles:
        n=" ".join(role.name.upper().strip().split())
        if n in all_company_names and n not in desired_company: remove.append(role)
        if any(x in n for x in ('1ST PLATOON','2ND PLATOON','3RD PLATOON','4TH PLATOON')) and platoon and n!=platoon: remove.append(role)
        if any(x in n for x in ('1ST SQUAD','2ND SQUAD','3RD SQUAD','4TH SQUAD')) and squad and n!=squad: remove.append(role)
    for role in member.guild.roles:
        n=" ".join(role.name.upper().strip().split())
        if n in desired_company or (platoon and n==platoon) or (squad and n==squad):
            desired.append(role)
    try:
        remove=list(dict.fromkeys(remove)); desired=list(dict.fromkeys(desired))
        if remove: await member.remove_roles(*remove,reason='Battalion personnel record is authoritative')
        add=[r for r in desired if r not in member.roles]
        if add: await member.add_roles(*add,reason='Synchronize authoritative battalion personnel record')
    except discord.Forbidden:
        log.warning('[CANONICAL ROLE SYNC BLOCKED] member=%s bot role hierarchy/permissions',member.id)

async def sync_personnel_identity(member: discord.Member, *, create_if_missing=False,
                                  reason="identity_sync", deliver_credentials=True):
    if member.bot:
        return None
    payload={"guild_id":member.guild.id,"discord_user_id":member.id,
             "username":member.name,"display_name":member.display_name,
             "roles":member_role_names(member),"create_if_missing":create_if_missing,
             "reason":reason}
    try:
        result=await web.request("POST","/internal/clerk/personnel/sync",json=payload)
    except Exception as exc:
        log.warning("[PERSONNEL SYNC FAILED] member=%s (%s) error=%s",member.display_name,member.id,exc)
        return None
    if result.get("linked") and not result.get("created"):
        await reconcile_member_roles_from_canonical(member,result)
    if result.get("created"):
        log.info("[201 FILE OPENED] %s (%s) roster=%s rank=%s",
                 member.display_name,member.id,result.get("roster_number"),result.get("rank_code"))
    if deliver_credentials and result.get("roster_number") and result.get("field_code"):
        weapon_line=f"\nIssued M16 Serial No.: **{result.get('weapon_serial')}**" if result.get("weapon_serial") else ""
        login_url=f"{WEBSITE_BASE_URL}/my-soldier-record" if WEBSITE_BASE_URL else "the battalion website — My Soldier Record"
        try:
            await member.send(
                "**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n"
                "**YOUR SOLDIER RECORD HAS BEEN OPENED**\n\n"
                f"Soldier: **{member.display_name}**\n"
                f"Rank: **{result.get('rank_code') or 'FILED'}**\n"
                f"Battle Roster No.: **{result.get('roster_number')}**\n"
                f"Field Code: **{result.get('field_code')}**"
                f"{weapon_line}\n\n"
                "Your 201 File, issued M16, service uniform, assignment, orders, awards, qualifications, and service history are now tied to your battalion record.\n\n"
                f"**MY SOLDIER RECORD:** {login_url}\n\n"
                "Retain your Battle Roster Number and Field Code. Keep your Field Code private.")
        except discord.Forbidden:
            log.warning("[SOLDIER RECORD DM BLOCKED] %s (%s)",member.display_name,member.id)
    return result

async def send_duty_presence_chunk(
    guild_id: int,
    member_id: int,
    channel_id: int,
    started: datetime,
    ended: datetime,
):
    event_type = duty_type_for_channel(guild_id, channel_id)
    if not event_type or ended <= started:
        return None
    guild = bot.get_guild(guild_id)
    member = guild.get_member(member_id) if guild else None
    payload = {
        'guild_id': guild_id,
        'member_id': member_id,
        'discord_user_id': member_id,
        'username': member.name if member else '',
        'display_name': member.display_name if member else '',
        'roles': member_role_names(member) if member else [],
        'channel_id': channel_id,
        'channel_name': event_type.title(),
        'joined_at': iso(started),
        'left_at': iso(ended),
        'session_id': str(uuid.uuid4()),
    }
    return await web.request('POST', '/internal/clerk/attendance', json=payload)


async def flush_duty_presence(*, guild_id: Optional[int] = None, channel_id: Optional[int] = None):
    now = utc_now()
    for key in list(duty_voice_presence.keys()):
        gid, uid, cid = key
        if guild_id is not None and gid != guild_id:
            continue
        if channel_id is not None and cid != channel_id:
            continue
        started = duty_voice_presence.get(key)
        if not started:
            continue
        try:
            await send_duty_presence_chunk(gid, uid, cid, started, now)
            duty_voice_presence[key] = now
        except Exception as exc:
            log.warning('[DUTY FLUSH FAILED] key=%s error=%s', key, exc)


@tasks.loop(seconds=300)
async def flush_duty_chunks():
    await flush_duty_presence()


@flush_duty_chunks.before_loop
async def before_duty_flush():
    await bot.wait_until_ready()


def duty_choice(value: str) -> app_commands.Choice[str]:
    return app_commands.Choice(name=value.title(), value=value)


DUTY_CHOICES = [duty_choice(x) for x in DUTY_TYPES]


async def require_manage_guild(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not interaction.user:
        await interaction.response.send_message('This command must be used inside the battalion server.', ephemeral=True)
        return False
    perms = interaction.user.guild_permissions
    if not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message('Authorization required: Manage Server or Administrator.', ephemeral=True)
        return False
    return True




@bot.tree.command(name='setup-roles', description='Build or repair the complete 1/5 CAV role hierarchy, including divider roles.')
@app_commands.describe(confirm='Set to True to build the battalion role structure')
async def setup_roles(interaction: discord.Interaction, confirm: bool):
    if not await require_manage_guild(interaction): return
    if not confirm:
        await interaction.response.send_message('No changes made. Run `/setup-roles confirm:True` when ready.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try:
        result=await build_battalion_roles(interaction.guild)
        msg=(f"**BATTALION ROLE STRUCTURE COMPLETE**\nCreated: **{len(result['created'])}**\nAlready present: **{len(result['existing'])}**\nFailures: **{len(result['failed'])}**")
        if result['failed']:
            msg += "\n\n**Review:**\n" + "\n".join(f"• {x}" for x in result['failed'][:12])
        msg += "\n\nDivider roles are visual-only and are never assigned to personnel."
        await interaction.followup.send(msg,ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f'Role setup failed: `{exc}`',ephemeral=True)


@bot.tree.command(name='setup-channels', description='Build or repair 1/5 CAV categories, text channels, voice channels, and access scopes.')
@app_commands.describe(confirm='Set to True to build the battalion channel structure')
async def setup_channels(interaction: discord.Interaction, confirm: bool):
    if not await require_manage_guild(interaction): return
    if not confirm:
        await interaction.response.send_message('No changes made. Run `/setup-channels confirm:True` when ready.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try:
        # Access overwrites depend on the roles existing, so make sure the role blueprint exists first.
        roles=await build_battalion_roles(interaction.guild)
        result=await build_battalion_channels(interaction.guild)
        msg=(f"**BATTALION DISCORD STRUCTURE COMPLETE**\nCategories/channels created: **{len(result['created'])}**\nAlready present: **{len(result['existing'])}**\nRole items created/repaired first: **{len(roles['created'])}**\nFailures: **{len(result['failed'])+len(roles['failed'])}**")
        failures=(roles['failed']+result['failed'])[:12]
        if failures: msg += "\n\n**Review:**\n"+"\n".join(f"• {x}" for x in failures)
        await interaction.followup.send(msg,ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f'Channel setup failed: `{exc}`',ephemeral=True)


@bot.tree.command(name='battalion-setup', description='Build all 1/5 CAV roles, categories, channels, dividers, and access scopes.')
@app_commands.describe(confirm='Set to True to build the complete server structure')
async def battalion_setup(interaction: discord.Interaction, confirm: bool):
    if not await require_manage_guild(interaction): return
    if not confirm:
        await interaction.response.send_message('No changes made. Run `/battalion-setup confirm:True` when you are ready to construct the server.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try:
        roles=await build_battalion_roles(interaction.guild)
        channels=await build_battalion_channels(interaction.guild)
        inv=structure_inventory(interaction.guild)
        failures=roles['failed']+channels['failed']
        msg=("**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY**\n**SERVER CONSTRUCTION COMPLETE**\n\n"
             f"Role items created: **{len(roles['created'])}**\n"
             f"Category/channel items created: **{len(channels['created'])}**\n"
             f"Missing roles: **{len(inv['missing_roles'])}**\n"
             f"Missing categories: **{len(inv['missing_categories'])}**\n"
             f"Missing channels: **{len(inv['missing_channels'])}**\n"
             f"Failures: **{len(failures)}**\n\n"
             "The command is safe to run again: existing structure is reused and missing pieces are repaired rather than duplicated.")
        if failures: msg += "\n\n**Review:**\n"+"\n".join(f"• {x}" for x in failures[:12])
        await interaction.followup.send(msg,ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f'Battalion setup failed: `{exc}`',ephemeral=True)


@bot.tree.command(name='structure-status', description='Show missing 1/5 CAV roles, categories, and channels without changing the server.')
async def structure_status(interaction: discord.Interaction):
    if not await require_manage_guild(interaction): return
    inv=structure_inventory(interaction.guild)
    lines=["**1/5 CAV DISCORD STRUCTURE STATUS**",
           f"Expected role items: **{inv['expected_roles']}** — Missing: **{len(inv['missing_roles'])}**",
           f"Expected categories: **{inv['expected_categories']}** — Missing: **{len(inv['missing_categories'])}**",
           f"Expected channels: **{inv['expected_channels']}** — Missing: **{len(inv['missing_channels'])}**"]
    if inv['missing_roles']: lines.append("\n**Missing Roles**\n"+"\n".join(f"• {x}" for x in inv['missing_roles'][:10]))
    if inv['missing_categories']: lines.append("\n**Missing Categories**\n"+"\n".join(f"• {x}" for x in inv['missing_categories'][:10]))
    if inv['missing_channels']: lines.append("\n**Missing Channels**\n"+"\n".join(f"• {x}" for x in inv['missing_channels'][:10]))
    await interaction.response.send_message("\n".join(lines),ephemeral=True)


@bot.tree.command(name='structure-repair', description='Repair missing 1/5 CAV roles, categories, channels, and access without duplicates.')
@app_commands.describe(confirm='Set to True to repair the battalion structure')
async def structure_repair(interaction: discord.Interaction, confirm: bool):
    if not await require_manage_guild(interaction): return
    if not confirm:
        await interaction.response.send_message('No changes made. Run `/structure-repair confirm:True` to repair missing structure.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    roles=await build_battalion_roles(interaction.guild)
    channels=await build_battalion_channels(interaction.guild)
    inv=structure_inventory(interaction.guild)
    await interaction.followup.send(
        f"**STRUCTURE REPAIR COMPLETE**\nCreated/repaired role items: **{len(roles['created'])}**\nCreated/repaired category/channel items: **{len(channels['created'])}**\nRemaining missing roles/categories/channels: **{len(inv['missing_roles'])}/{len(inv['missing_categories'])}/{len(inv['missing_channels'])}**",
        ephemeral=True)


@bot.tree.command(name='personnel-status', description="Check a member's rank/MOS processing state before a 201 File is created.")
@app_commands.describe(member='Member to inspect')
async def personnel_status(interaction: discord.Interaction, member: discord.Member):
    if not await require_manage_guild(interaction): return
    validation=validate_personnel_roles(member)
    pending=(member.guild.id,member.id) in pending_personnel_sync and not pending_personnel_sync[(member.guild.id,member.id)].done()
    state='ROLE SETTLE — 30 SECOND COOLDOWN' if pending else ('READY FOR PERSONNEL PROCESSING' if validation['valid'] else 'PROCESSING HOLD')
    problems='; '.join(validation['problems']) if validation['problems'] else 'NONE'
    await interaction.response.send_message(
        f'**HEADQUARTERS — BATTALION CLERK**\n**PERSONNEL PROCESSING STATUS**\n\n'
        f'Member: **{member.display_name}**\nState: **{state}**\nRank: **{validation.get("rank") or "NOT RESOLVED"}**\nMOS: **{validation.get("mos") or "NOT RESOLVED"}**\nCompany: **{validation.get("company") or "NOT ASSIGNED"}**\nPlatoon: **{validation.get("platoon") or "NOT ASSIGNED"}**\nSquad: **{validation.get("squad") or "NOT ASSIGNED"}**\nHold Reason: **{problems}**', ephemeral=True)

@bot.tree.command(name='personnel-reprocess', description='Restart the 30-second personnel role settle for a member.')
@app_commands.describe(member='Member to reprocess')
async def personnel_reprocess(interaction: discord.Interaction, member: discord.Member):
    if not await require_manage_guild(interaction): return
    schedule_personnel_role_sync(member, reason='manual_reprocess')
    await interaction.response.send_message(
        f'**PERSONNEL REPROCESSING INITIATED — {member.display_name.upper()}**\nBattalion Clerk will wait 30 seconds for rank/MOS/unit roles to settle, validate the complete role set, then create or repair the Soldier record without duplicating existing issue records.', ephemeral=True)

@bot.tree.command(name='personnel-health', description='Show Battalion Clerk personnel-processing health.')
async def personnel_health(interaction: discord.Interaction):
    if not await require_manage_guild(interaction): return
    pending=sum(1 for t in pending_personnel_sync.values() if not t.done())
    try:
        health=await web.request('GET','/health')
        website='ONLINE' if health.get('ok',True) else 'CHECK REQUIRED'
    except Exception:
        website='UNREACHABLE'
    await interaction.response.send_message(
        f'**BATTALION CLERK — SYSTEM HEALTH**\nWebsite Link: **{website}**\nRole Settle Window: **30 seconds**\nMembers Currently Settling: **{pending}**\nVoice Flush: **{VOICE_FLUSH_SECONDS}s**', ephemeral=True)

@bot.tree.command(name='reissue-login', description='Issue a new private Field Code to a linked Soldier and DM it to them.')
@app_commands.describe(member='Member who needs new Soldier Record access')
async def reissue_login(interaction: discord.Interaction, member: discord.Member):
    if not await require_manage_guild(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = await web.request(
            'POST', '/internal/clerk/personnel/reissue-login',
            json={'guild_id': interaction.guild_id, 'discord_user_id': member.id}
        )
    except Exception as exc:
        await interaction.followup.send(f'Reissue failed: {exc}', ephemeral=True)
        return
    if not result.get('ok'):
        await interaction.followup.send(f"Reissue failed: {result.get('error', 'unknown error')}", ephemeral=True)
        return
    login_url = f"{WEBSITE_BASE_URL}/my-soldier-record" if WEBSITE_BASE_URL else 'the battalion website — My Soldier Record'
    message = (
        "**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n"
        "**SOLDIER RECORD ACCESS REISSUED**\n\n"
        f"Battle Roster No.: **{result.get('roster_number')}**\n"
        f"Field Code: **{result.get('field_code')}**\n\n"
        f"**MY SOLDIER RECORD:** {login_url}\n\n"
        "Retain your Battle Roster Number and keep your Field Code private."
    )
    try:
        await member.send(message)
        await interaction.followup.send(
            f'New Soldier Record access was issued and sent privately to **{member.display_name}**.',
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            f'Access was reissued, but I could not DM {member.mention}. '
            f'Battle Roster: **{result.get("roster_number")}**. '
            'Use a secure method to deliver the new Field Code.',
            ephemeral=True,
        )

async def publish_operation_duty_roster(guild: discord.Guild, operation: dict):
    channel_id=await get_operation_duty_channel_id(guild.id)
    channel=guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return False
    assignments=operation.get('assignments') or []
    if not assignments:
        return False
    lines=[]
    for a in assignments:
        name=f"{a.get('rank') or ''} {a.get('last_name') or ''}, {(a.get('first_name') or '')[:1]}.".strip()
        element=f" — {a.get('element')}" if a.get('element') else ''
        lines.append(f"**{name.upper()}** — {a.get('duty_role')}{element}")
    when=operation.get('start_at') or 'TIME TO BE ANNOUNCED'
    text = (
        "**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n"
        "**S-3 OPERATIONS — PRE-OPERATION DUTY ROSTER**\n\n"
        f"**{operation.get('operation_number') or 'OPERATION'} — {operation.get('title')}**\n"
        f"Start: **{when}**\n\n"
        + "\n".join(lines)
        + "\n\nAll personnel will report in accordance with their assigned duty. Changes are controlled by S-3 Operations."
    )
    await channel.send(text)
    await web.request('POST',f"/internal/clerk/operation-duty/{operation.get('operation_id')}/posted",json={})
    return True

@tasks.loop(seconds=60)
async def operation_duty_watch():
    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID: continue
        try:
            if not await get_operation_duty_channel_id(guild.id): continue
            data=await web.request('GET','/internal/clerk/operation-duty/pending',params={'guild_id':guild.id})
            for op in data.get('operations',[]):
                try: await publish_operation_duty_roster(guild,op)
                except Exception as exc: log.warning('[OP DUTY POST FAILED] operation=%s error=%s',op.get('operation_id'),exc)
        except Exception as exc:
            log.warning('[OP DUTY WATCH FAILED] guild=%s error=%s',guild.id,exc)

@operation_duty_watch.before_loop
async def before_operation_duty_watch():
    await bot.wait_until_ready()

@bot.tree.command(name='operation-duty-channel', description='Assign the Discord channel that receives S-3 pre-operation duty rosters.')
@app_commands.describe(channel='Text channel for operation-specific Soldier duty assignments')
async def operation_duty_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_manage_guild(interaction): return
    await set_operation_duty_channel(interaction.guild_id,channel.id)
    await interaction.response.send_message(f'**S-3 operation duty rosters** will be posted to {channel.mention}.',ephemeral=True)

@bot.tree.command(name='operation-duty-status', description='Show the channel receiving S-3 pre-operation duty rosters.')
async def operation_duty_status(interaction: discord.Interaction):
    if not await require_manage_guild(interaction): return
    cid=await get_operation_duty_channel_id(interaction.guild_id); ch=interaction.guild.get_channel(cid) if cid else None
    await interaction.response.send_message(f'**S-3 Duty Roster Channel:** {ch.mention if ch else "NOT ASSIGNED"}',ephemeral=True)

@bot.tree.command(name='publish-operation-duty', description='Immediately publish all pending S-3 operation duty rosters.')
async def publish_operation_duty(interaction: discord.Interaction):
    if not await require_manage_guild(interaction): return
    await interaction.response.defer(ephemeral=True)
    data=await web.request('GET','/internal/clerk/operation-duty/pending',params={'guild_id':interaction.guild_id})
    posted=0
    for op in data.get('operations',[]):
        if await publish_operation_duty_roster(interaction.guild,op): posted+=1
    await interaction.followup.send(f'Published **{posted}** pending operation duty roster(s).',ephemeral=True)

ORDER_ROUTE_TYPES = ['ALL','REPLACEMENT','ASSIGNMENT','PROMOTION','AWARD','APPOINTMENT','LEAVE','RETURN','SEPARATION','TOUR EXTENSION','TRAINING','QUALIFICATION']

@bot.tree.command(name='personnel-orders-channel', description='Assign where a type of personnel order is posted.')
@app_commands.describe(order_type='Order type to route', channel='Discord channel that receives this order type')
@app_commands.choices(order_type=[app_commands.Choice(name=x.title().replace('_',' '), value=x) for x in ORDER_ROUTE_TYPES])
async def personnel_orders_channel(interaction: discord.Interaction, order_type: app_commands.Choice[str], channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message('Manage Server permission is required.', ephemeral=True); return
    await set_order_route(interaction.guild_id, order_type.value, channel.id)
    await interaction.response.send_message(f'**{order_type.value.replace("_"," ").title()} orders** will be posted to {channel.mention}.', ephemeral=True)

@bot.tree.command(name='personnel-orders-status', description='Show personnel-order Discord routing.')
async def personnel_orders_status(interaction: discord.Interaction):
    routes=await get_order_routes(interaction.guild_id)
    if not routes:
        await interaction.response.send_message('No personnel order routes are assigned.', ephemeral=True); return
    lines=[]
    for kind,cid in sorted(routes.items()):
        ch=interaction.guild.get_channel(cid)
        lines.append(f'**{kind.replace("_"," ").title()}** — {ch.mention if ch else f"#{cid}"}')
    await interaction.response.send_message('**PERSONNEL ORDER ROUTING**\n'+"\n".join(lines), ephemeral=True)

@bot.tree.command(name='personnel-orders-clear', description='Stop sending one type of personnel order to Discord.')
@app_commands.choices(order_type=[app_commands.Choice(name=x.title().replace('_',' '), value=x) for x in ORDER_ROUTE_TYPES])
async def personnel_orders_clear(interaction: discord.Interaction, order_type: app_commands.Choice[str]):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message('Manage Server permission is required.', ephemeral=True); return
    await clear_order_route(interaction.guild_id,order_type.value)
    await interaction.response.send_message(f'**{order_type.value.replace("_"," ").title()}** personnel orders will no longer auto-post.', ephemeral=True)

@bot.tree.command(name='orders-channel', description='Assign the text channel that receives official battalion event notices.')
@app_commands.describe(channel='Text channel for Operations Notices, Training Circulars, and Battalion Notices')
async def orders_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_manage_guild(interaction):
        return
    await set_orders_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(
        f'**Battalion orders channel assigned:** {channel.mention}\n'
        'Scheduled duty periods will publish an initial notice, a 15-minute warning, '
        'a commencement notice, and a completion notice.',
        ephemeral=True,
    )


@bot.tree.command(name='orders-channel-status', description='Show the text channel receiving official battalion event notices.')
async def orders_channel_status(interaction: discord.Interaction):
    if not await require_manage_guild(interaction):
        return
    channel_id = await get_orders_channel_id(interaction.guild_id)
    channel = interaction.guild.get_channel(channel_id) if channel_id else None
    await interaction.response.send_message(
        f'**Battalion Orders:** {channel.mention if channel else "NOT ASSIGNED"}',
        ephemeral=True,
    )



@bot.tree.command(name='reset-roster', description='Clear the current personnel roster and rebuild only current rank-role holders.')
@app_commands.describe(confirmation='Type RESET ROSTER exactly')
async def reset_roster(interaction: discord.Interaction, confirmation: str):
    if not await require_manage_guild(interaction):
        return
    if confirmation.strip().upper() != 'RESET ROSTER':
        await interaction.response.send_message(
            'RESET ABORTED. Type `RESET ROSTER` exactly in the confirmation field.',
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        result = await web.request(
            'POST',
            '/internal/clerk/personnel/reset',
            json={'confirmation': 'RESET ROSTER', 'guild_id': interaction.guild_id},
        )
    except Exception as exc:
        await interaction.followup.send(
            f'ROSTER RESET FAILED: `{exc}`',
            ephemeral=True,
        )
        return

    rebuilt = 0
    failed = 0

    # The website itself decides whether each member has a recognized rank role.
    # Members without a rank role remain off the roster.
    for member in interaction.guild.members:
        if member.bot:
            continue
        try:
            sync = await sync_personnel_identity(
                member,
                create_if_missing=False,
                reason='post_reset_rank_roster_rebuild',
                deliver_credentials=True,
            )
            if sync and sync.get('created'):
                rebuilt += 1
        except Exception:
            failed += 1
            log.exception('[POST RESET SYNC FAILED] member=%s (%s)', member.display_name, member.id)

    await interaction.followup.send(
        '**HEADQUARTERS — BATTALION ROSTER RESET COMPLETE**\n'
        f"Prior personnel records cleared: **{result.get('cleared_personnel', 0)}**\n"
        f'Rank-role holders entered on roster: **{rebuilt}**\n'
        f'Sync failures: **{failed}**\n\n'
        'Only personnel presently holding a recognized rank role receive a 201 File.',
        ephemeral=True,
    )


@bot.tree.command(name='duty-channel', description='Assign the permanent voice channel for Training, Operation, or Meeting duty.')
@app_commands.describe(duty_type='Duty category', channel='Voice channel to monitor')
@app_commands.choices(duty_type=DUTY_CHOICES)
async def duty_channel(
    interaction: discord.Interaction,
    duty_type: app_commands.Choice[str],
    channel: discord.VoiceChannel,
):
    if not await require_manage_guild(interaction):
        return
    event_type = duty_type.value
    await web.request('POST', '/internal/clerk/channels', json={
        'guild_id': interaction.guild_id,
        'event_type': event_type,
        'channel_id': channel.id,
        'channel_name': channel.name,
    })
    await load_duty_bindings(interaction.guild_id)
    now = utc_now()
    for member in channel.members:
        if not member.bot:
            duty_voice_presence[(interaction.guild_id, member.id, channel.id)] = now
    await interaction.response.send_message(
        f'**{event_type.title()} duty station assigned:** {channel.mention}\n'
        'Minimum credit remains **45 minutes during a scheduled duty period**.',
        ephemeral=True,
    )


@bot.tree.command(name='duty-channel-status', description='Show the permanent battalion duty voice-channel assignments.')
async def duty_channel_status(interaction: discord.Interaction):
    if not await require_manage_guild(interaction):
        return
    rows = await load_duty_bindings(interaction.guild_id)
    by_type = {row['event_type']: row for row in rows}
    lines = []
    for kind in DUTY_TYPES:
        row = by_type.get(kind)
        channel = interaction.guild.get_channel(int(row['channel_id'])) if row else None
        lines.append(f"**{kind.title()}** — {channel.mention if channel else 'NOT ASSIGNED'}")
    await interaction.response.send_message('\n'.join(lines), ephemeral=True)


@bot.tree.command(name='schedule', description='Schedule an official Training, Operation, or Meeting duty period.')
@app_commands.describe(
    duty_type='Type of official duty',
    title='Event title',
    date='Local date: YYYY-MM-DD',
    time='Local start time: HH:MM (24-hour)',
    duration_minutes='Scheduled duration in minutes',
    operation_id='Optional website Operation UUID for combat-operation filing',
)
@app_commands.choices(duty_type=DUTY_CHOICES)
async def schedule_duty(
    interaction: discord.Interaction,
    duty_type: app_commands.Choice[str],
    title: str,
    date: str,
    time: str,
    duration_minutes: app_commands.Range[int, 45, 720],
    operation_id: Optional[str] = None,
):
    if not await require_manage_guild(interaction):
        return
    event_type = duty_type.value
    if interaction.guild_id not in duty_channel_bindings:
        await load_duty_bindings(interaction.guild_id)
    channel_id = duty_channel_bindings.get(interaction.guild_id, {}).get(event_type)
    if not channel_id:
        await interaction.response.send_message(
            f'No {event_type.title()} voice channel is assigned. Run `/duty-channel` first.',
            ephemeral=True,
        )
        return
    try:
        tz = ZoneInfo(BATTALION_TIMEZONE)
        local_start = datetime.strptime(f'{date} {time}', '%Y-%m-%d %H:%M').replace(tzinfo=tz)
    except Exception:
        await interaction.response.send_message(
            'Use date `YYYY-MM-DD` and time `HH:MM` in 24-hour format.',
            ephemeral=True,
        )
        return

    local_end = local_start + timedelta(minutes=int(duration_minutes))
    channel = interaction.guild.get_channel(channel_id)
    external_id = f'discord:{interaction.guild_id}:{event_type}:{int(local_start.timestamp())}'
    result = await web.request('POST', '/internal/clerk/events', json={
        'external_event_id': external_id,
        'event_type': event_type,
        'title': title.strip(),
        'starts_at': iso(local_start),
        'ends_at': iso(local_end),
        'channel_name': event_type.title(),
        'channel_id': channel_id,
        'operation_id': operation_id or None,
    })
    await interaction.response.send_message(
        '**HEADQUARTERS — DUTY PERIOD FILED**\n'
        f'**{title}**\nType: **{event_type.title()}**\n'
        f"Duty Station: {channel.mention if channel else f'<#{channel_id}>'}\n"
        f'Start: <t:{int(local_start.timestamp())}:F>\n'
        f'End: <t:{int(local_end.timestamp())}:t>\n'
        'Credit Requirement: **45 minutes present**\n'
        f"Record No.: `{result.get('event_id')}`"
    )

    # Publish the official notice to the configured battalion orders channel.
    notice_event = {
        'id': result.get('event_id'),
        'title': title.strip(),
        'event_type': event_type,
        'starts_at': iso(local_start),
        'ends_at': iso(local_end),
        'channel_id': channel_id,
    }
    try:
        if await post_battalion_order(interaction.guild, notice_event, 'filed'):
            announcement_notice_sent.add(str(result.get('event_id')))
    except Exception as exc:
        log.warning('[ORDER NOTICE FAILED] event=%s error=%s', result.get('event_id'), exc)


@bot.tree.command(name='duty-status', description="Show current scheduled duty and each Soldier's credited voice time.")
async def duty_status(interaction: discord.Interaction):
    if not await require_manage_guild(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await flush_duty_presence(guild_id=interaction.guild_id)
    result = await web.request('GET', '/internal/clerk/events/status', params={'guild_id': interaction.guild_id})
    events = result.get('events', [])
    if not events:
        await interaction.followup.send('NO CURRENT DUTY PERIODS ON FILE.', ephemeral=True)
        return
    parts = []
    for event in events[:4]:
        title = event.get('title') or event.get('event_type')
        parts.append(f"**{event.get('event_type')} — {title}**")
        attendance = event.get('attendance') or []
        if not attendance:
            parts.append('No qualifying presence recorded yet.')
        else:
            for row in attendance[:25]:
                minutes = int(row.get('qualifying_seconds') or 0) // 60
                remain = max(0, 45 - minutes)
                state = '**CREDIT EARNED**' if row.get('credited_at') else f'{remain} MIN REQUIRED'
                parts.append(
                    f"{row.get('rank_code') or ''} {row.get('last_name') or ''} — "
                    f'{minutes} MIN — {state}'
                )
        parts.append('')
    await interaction.followup.send('\n'.join(parts)[:1900], ephemeral=True)


@bot.tree.command(name='close-duty', description='Close an official duty period and file final attendance credit.')
@app_commands.describe(event_id='Optional event record UUID. Leave blank to close the nearest active/current event.')
async def close_duty(interaction: discord.Interaction, event_id: Optional[str] = None):
    if not await require_manage_guild(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await flush_duty_presence(guild_id=interaction.guild_id)
    status = await web.request('GET', '/internal/clerk/events/status', params={'guild_id': interaction.guild_id})
    events = status.get('events', [])
    selected = None
    if event_id:
        selected = next((e for e in events if str(e.get('id')) == event_id.strip()), None)
        if not selected:
            await interaction.followup.send(
                'That duty record was not found among current scheduled/active events.',
                ephemeral=True,
            )
            return
    else:
        now = utc_now()

        def distance(event):
            try:
                start = datetime.fromisoformat(str(event['starts_at']).replace('Z', '+00:00'))
                return abs((start.astimezone(timezone.utc) - now).total_seconds())
            except Exception:
                return 10**15

        if events:
            selected = sorted(events, key=distance)[0]

    if not selected:
        await interaction.followup.send('NO CURRENT DUTY PERIOD ON FILE.', ephemeral=True)
        return

    result = await web.request('POST', f"/internal/clerk/events/{selected['id']}/close", json={})
    summary = result.get('summary') or {}

    try:
        await post_battalion_order(interaction.guild, selected, 'end', close_summary=summary)
        announcement_end_sent.add(str(selected.get('id')))
    except Exception as exc:
        log.warning('[ORDER CLOSE NOTICE FAILED] event=%s error=%s', selected.get('id'), exc)

    await interaction.followup.send(
        f"**DUTY PERIOD CLOSED**\n{selected.get('title')}\n"
        f"Soldiers tracked: **{summary.get('tracked', 0)}**\n"
        f"Soldiers credited (45+ min): **{summary.get('credited', 0)}**",
        ephemeral=True,
    )


@bot.event
async def on_ready():
    global collector_started, commands_synced

    if not collector_started:
        await collector.start()
        collector_started = True

    # Synchronize slash commands once per process. TEST_GUILD_ID wins when present
    # so new commands appear in the battalion server immediately.
    if not commands_synced:
        try:
            if COMMAND_GUILD_ID:
                guild_obj = discord.Object(id=COMMAND_GUILD_ID)
                bot.tree.copy_global_to(guild=guild_obj)
                synced = await bot.tree.sync(guild=guild_obj)
                source = 'TEST_GUILD_ID' if TEST_GUILD_ID else 'GUILD_ID'
                log.info('[COMMAND SYNC] synced=%s guild=%s source=%s', len(synced), COMMAND_GUILD_ID, source)
            else:
                synced = await bot.tree.sync()
                log.info('[COMMAND SYNC] synced=%s globally', len(synced))
            commands_synced = True
        except Exception:
            log.exception('[COMMAND SYNC FAILED]')

    if not flush_duty_chunks.is_running():
        flush_duty_chunks.change_interval(seconds=VOICE_FLUSH_SECONDS)
        flush_duty_chunks.start()

    if not duty_announcement_watch.is_running():
        duty_announcement_watch.start()
    if not personnel_orders_watch.is_running():
        personnel_orders_watch.start()
    if not operation_duty_watch.is_running():
        operation_duty_watch.start()

    log.info('Battalion Clerk online as %s (%s)', bot.user, bot.user.id if bot.user else 'unknown')

    now = utc_now()
    voice_sessions.clear()

    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID:
            continue

        await collector.record_event('bot_ready', {
            'guild_id': str(guild.id),
            'guild_name': guild.name,
            'timestamp': iso(now),
        })

        synced_members = 0
        for member in guild.members:
            await collector.upsert_member(member)
            if not member.bot:
                await sync_personnel_identity(member,create_if_missing=False,
                    reason="guild_sync",deliver_credentials=False)
                schedule_personnel_role_sync(member, reason="guild_roles_settled")
            synced_members += 1

        recovered_count = 0
        for channel in guild.voice_channels:
            for member in channel.members:
                if member.bot:
                    continue
                begin_session(member, channel, now, recovered=True)
                recovered_count += 1
                log.info('[VOICE RECOVER] %s (%s) already in #%s', member.display_name, member.id, channel.name)

        try:
            rows = await load_duty_bindings(guild.id)
            for row in rows:
                channel = guild.get_channel(int(row['channel_id']))
                if isinstance(channel, discord.VoiceChannel):
                    for member in channel.members:
                        if not member.bot:
                            duty_voice_presence[(guild.id, member.id, channel.id)] = now
            log.info('[DUTY CHANNELS] guild=%s loaded=%s', guild.id, len(rows))
        except Exception as exc:
            log.warning('[DUTY CHANNEL LOAD FAILED] guild=%s error=%s', guild.id, exc)

        log.info(
            '[GUILD SYNC] guild=%s (%s) members=%s recovered_voice_sessions=%s',
            guild.name,
            guild.id,
            synced_members,
            recovered_count,
        )


@bot.event
async def on_member_join(member: discord.Member):
    if GUILD_ID and member.guild.id != GUILD_ID:
        return
    await collector.upsert_member(member)
    if not member.bot:
        await sync_personnel_identity(member,create_if_missing=False,reason="member_join")
    await collector.record_event('member_join', {
        'guild_id': str(member.guild.id),
        'discord_user_id': str(member.id),
        'username': member.name,
        'display_name': member.display_name,
        'is_bot': member.bot,
        'joined_at': member.joined_at.isoformat() if member.joined_at else iso(utc_now()),
        'timestamp': iso(utc_now()),
    })
    log.info('[MEMBER JOIN] %s (%s)', member.display_name, member.id)


@bot.event
async def on_member_remove(member: discord.Member):
    if GUILD_ID and member.guild.id != GUILD_ID:
        return

    now = utc_now()
    if not member.bot and (member.guild.id, member.id) in voice_sessions:
        session = await close_session(member, now, 'member_left_guild')
        if session:
            log.info(
                '[VOICE SESSION] %s #%s %s (member left server)',
                member.display_name,
                session['channel_name'],
                session['duration_hms'],
            )

    # Flush any duty chunk for this member before removing them.
    for key in list(duty_voice_presence.keys()):
        gid, uid, cid = key
        if gid == member.guild.id and uid == member.id:
            started = duty_voice_presence.pop(key, None)
            if started:
                try:
                    await send_duty_presence_chunk(gid, uid, cid, started, now)
                except Exception as exc:
                    log.warning('[DUTY MEMBER-LEAVE FLUSH FAILED] member=%s error=%s', uid, exc)

    await collector.mark_member_left(member, now)
    if not member.bot:
        try:
            await web.request('POST','/internal/clerk/personnel/departure',json={'guild_id':member.guild.id,'discord_user_id':member.id,'reason':'member_left_discord'})
        except Exception as exc:
            log.warning('[PERSONNEL DEPARTURE FILING FAILED] member=%s error=%s',member.id,exc)
    await collector.record_event('member_leave', {
        'guild_id': str(member.guild.id),
        'discord_user_id': str(member.id),
        'username': member.name,
        'display_name': member.display_name,
        'is_bot': member.bot,
        'timestamp': iso(now),
    })
    log.info('[MEMBER LEAVE] %s (%s)', member.display_name, member.id)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if GUILD_ID and after.guild.id != GUILD_ID:
        return
    before_roles={r.id for r in before.roles}
    after_roles={r.id for r in after.roles}
    if before.name == after.name and before.display_name == after.display_name and before_roles == after_roles:
        return
    await collector.upsert_member(after)
    if not after.bot:
        if before_roles != after_roles:
            schedule_personnel_role_sync(after, reason="discord_roles_settled")
        else:
            # Username/display-name changes can safely update an existing link immediately;
            # they do not trigger creation of a new personnel record.
            await sync_personnel_identity(after,create_if_missing=False,reason="member_identity_update",deliver_credentials=False)
    await collector.record_event('member_identity_update', {
        'guild_id': str(after.guild.id),
        'discord_user_id': str(after.id),
        'username': after.name,
        'display_name': after.display_name,
        'previous_username': before.name,
        'previous_display_name': before.display_name,
        'timestamp': iso(utc_now()),
    })
    log.info('[MEMBER UPDATE] %s (%s)', after.display_name, after.id)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if GUILD_ID and member.guild.id != GUILD_ID:
        return
    if member.bot:
        return

    before_id_str = str(before.channel.id) if before.channel else None
    after_id_str = str(after.channel.id) if after.channel else None
    if before_id_str == after_id_str:
        return

    now = utc_now()
    await collector.upsert_member(member)

    # Existing general voice telemetry.
    if before.channel is None and after.channel is not None:
        begin_session(member, after.channel, now)
        log.info('[VOICE JOIN] %s (%s) -> #%s', member.display_name, member.id, after.channel.name)
        event_type = 'voice_join'
    elif before.channel is not None and after.channel is None:
        session = await close_session(member, now, 'voice_leave')
        duration = session['duration_hms'] if session else 'unknown'
        log.info('[VOICE LEAVE] %s (%s) <- #%s | session=%s', member.display_name, member.id, before.channel.name, duration)
        event_type = 'voice_leave'
    else:
        session = await close_session(member, now, 'voice_move')
        duration = session['duration_hms'] if session else 'unknown'
        begin_session(member, after.channel, now)
        log.info(
            '[VOICE MOVE] %s (%s) #%s -> #%s | prior_session=%s',
            member.display_name,
            member.id,
            before.channel.name,
            after.channel.name,
            duration,
        )
        event_type = 'voice_move'

    await collector.record_event(event_type, {
        'guild_id': str(member.guild.id),
        'discord_user_id': str(member.id),
        'username': member.name,
        'display_name': member.display_name,
        'from_channel_id': before_id_str,
        'from_channel_name': before.channel.name if before.channel else None,
        'to_channel_id': after_id_str,
        'to_channel_name': after.channel.name if after.channel else None,
        'timestamp': iso(now),
    })

    # Official Training / Operation / Meeting duty-credit tracking.
    gid = member.guild.id
    if gid not in duty_channel_bindings:
        try:
            await load_duty_bindings(gid)
        except Exception as exc:
            log.warning('[DUTY BINDING REFRESH FAILED] guild=%s error=%s', gid, exc)
            return

    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None

    if before_id and duty_type_for_channel(gid, before_id):
        key = (gid, member.id, before_id)
        started = duty_voice_presence.pop(key, None)
        if started:
            try:
                await send_duty_presence_chunk(gid, member.id, before_id, started, now)
            except Exception as exc:
                log.warning('[DUTY INTERVAL FAILED] member=%s error=%s', member.id, exc)

    if after_id and duty_type_for_channel(gid, after_id):
        await sync_personnel_identity(member,create_if_missing=False,reason="official_duty_presence")
        duty_voice_presence[(gid, member.id, after_id)] = now


if not TOKEN:
    raise RuntimeError('DISCORD_TOKEN is not set. Add DISCORD_TOKEN in Railway Variables.')

if not WEBSITE_BASE_URL:
    log.warning('WEBSITE_BASE_URL is not set; duty commands will fail until it is configured.')
if not CLERK_SYNC_KEY:
    log.warning('CLERK_SYNC_KEY is not set; duty commands will fail until it is configured.')

log.info(
    '[CONFIG] command_guild=%s source=%s timezone=%s flush=%ss website=%s',
    COMMAND_GUILD_ID or 'GLOBAL',
    'TEST_GUILD_ID' if TEST_GUILD_ID else ('GUILD_ID' if GUILD_ID else 'GLOBAL'),
    BATTALION_TIMEZONE,
    VOICE_FLUSH_SECONDS,
    WEBSITE_BASE_URL or 'NOT SET',
)

bot.run(TOKEN)

import os
import logging
import re
import uuid
import asyncio
import io
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Tuple, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from collector import DataCollector
from hllv_rcon import HLLVTelemetryCollector

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
hllv = HLLVTelemetryCollector(collector)
collector_started = False
commands_synced = False

# General voice-session telemetry already used by Battalion Clerk.
# key: (guild_id, user_id) -> session metadata
voice_sessions: Dict[Tuple[int, int], dict] = {}

# Duty-credit tracking for the three official duty channels.
# guild_id -> {TRAINING|OPERATION|MEETING: channel_id}
duty_channel_bindings: Dict[int, Dict[str, int]] = {}
active_event_channels: Dict[int, Dict[int, str]] = {}
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
# Members in this set are undergoing Battalion Clerk managed-role maintenance.
# on_member_update must not echo transient cleanup states back into the website.
role_sync_suppressed_members = set()

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
    "──────── RECRUITING STATUS ────────",
    "──────── RANK ────────",
    "──────── APPOINTMENTS ────────",
    "──────── BATTLEFIELD MOS ────────",
    "──────── COMPANY ASSIGNMENT ────────",
    "──────── PLATOON ASSIGNMENT ────────",
    "──────── SQUAD ASSIGNMENT ────────",
    "──────── FIRE TEAM ────────",
    "──────── QUALIFICATIONS ────────",
    "──────── STAFF ACCESS ────────",
]

RANK_ROLE_BLUEPRINT = [
    "LTC", "MAJ", "CPT", "1LT", "2LT", "SGM", "1SG", "MSG", "SFC",
    "SSG", "SGT", "SP7", "SP6", "SP5", "SP4", "CPL", "PFC", "PVT",
]

RECRUITING_STATUS_ROLE_BLUEPRINT = [
    "Prospective Replacement", "Replacement Depot",
]
# Old transitional recruiting role from the pre-Welcome-Packet workflow.
# Never create/assign it again; Battalion Clerk removes it from members and
# deletes the empty role when hierarchy permissions allow.
LEGACY_RECRUITING_STATUS_ROLE_NAMES = {"Approved Replacement"}

APPOINTMENT_ROLE_BLUEPRINT = [
    "Battalion Commander", "Battalion Executive Officer",
    "S-1 OIC", "S-1 NCOIC", "S-3 OIC", "S-3 NCOIC", "S-4 OIC", "S-4 NCOIC",
    "Company Commander", "Company Executive Officer", "First Sergeant",
    "Platoon Leader", "Platoon Sergeant", "Squad Leader", "Assistant Squad Leader", "Team Leader",
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
# Formation roles are created ON DEMAND from the authoritative website roster.
# Do not pre-generate every possible platoon/squad combination; that was the main
# source of Discord role clutter. Team assignment remains website-only because
# Company/Platoon/Squad already provide Discord access scope.
PLATOON_ROLE_BLUEPRINT = []
SQUAD_ROLE_BLUEPRINT = []
TEAM_ROLE_BLUEPRINT = []
LEGACY_ASSIGNMENT_ROLE_NAMES = {
    "1st Platoon", "2nd Platoon", "3rd Platoon", "4th Platoon",
    "1st Squad", "2nd Squad", "3rd Squad", "4th Squad",
    "Alpha Team", "Bravo Team",
    "Alpha Company", "A/1-5 CAV", "Bravo Company", "B/1-5 CAV",
    "Charlie Company", "C/1-5 CAV", "HHC/1-5 CAV", "Headquarters & Headquarters Company",
}
MEMBERSHIP_ROLE_BLUEPRINT = ["5th Cavalry Regiment"]
QUALIFICATION_ROLE_BLUEPRINT = [
    "Battalion Instructor", "M16 Qualified", "Mortar Qualified", "Recon Qualified",
    "Aviation Qualified", "Armor Qualified", "Medic Qualified",
]
STAFF_ACCESS_ROLE_BLUEPRINT = [
    "Command Staff", "S-1 Personnel", "S-3 Operations", "S-4 Supply",
]

ROLE_SECTIONS = [
    ("──────── BATTALION COMMAND ────────", ["Command Staff"]),
    ("──────── MEMBERSHIP ────────", MEMBERSHIP_ROLE_BLUEPRINT),
    ("──────── RECRUITING STATUS ────────", RECRUITING_STATUS_ROLE_BLUEPRINT),
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
            ("company-notices", "text"),
            ("company-formation", "voice"),
        ],
    })
    for _platoon in ("1st", "2nd", "3rd", "4th"):
        CHANNEL_BLUEPRINT.append({
            "category": f"{_company} COMPANY • {_platoon.upper()} PLATOON",
            "scope": f"PLATOON:{_company}:{_platoon}",
            "channels": [
                ("platoon-headquarters", "text"),
                ("platoon-orders", "text"),
                ("1st-squad", "text"),
                ("2nd-squad", "text"),
                ("3rd-squad", "text"),
                ("4th-squad", "text"),
                ("platoon-rally", "voice"),
                ("1st-squad", "voice"),
                ("2nd-squad", "voice"),
                ("3rd-squad", "voice"),
                ("4th-squad", "voice"),
            ],
        })



def _normalized_role_name(value: str) -> str:
    return " ".join(str(value or "").upper().strip().split())

def _role_by_name(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    """Resolve managed roles case/spacing-insensitively so cosmetic duplicates are not created."""
    wanted=_normalized_role_name(name)
    exact=discord.utils.get(guild.roles,name=name)
    if exact:
        return exact
    return next((r for r in guild.roles if _normalized_role_name(r.name)==wanted),None)


def _all_managed_role_names():
    names=[]
    for divider,roles in ROLE_SECTIONS:
        names.append(divider); names.extend(roles)
    return names

def _canonical_managed_role_name(value: str) -> Optional[str]:
    wanted=_normalized_role_name(value)
    for name in _all_managed_role_names():
        if _normalized_role_name(name)==wanted:
            return name
    m=re.fullmatch(r"([ABC]) COMPANY • (1ST|2ND|3RD|4TH) PLATOON(?: • (1ST|2ND|3RD|4TH) SQUAD)?",wanted)
    if m:
        base=f"{m.group(1)} Company • {m.group(2).title()} Platoon"
        return base + (f" • {m.group(3).title()} Squad" if m.group(3) else '')
    return None

def _managed_role_group(guild: discord.Guild, canonical_name: str):
    wanted=_normalized_role_name(canonical_name)
    return [r for r in guild.roles if _normalized_role_name(r.name)==wanted]

def _merge_overwrites(a: discord.PermissionOverwrite, b: discord.PermissionOverwrite):
    allow_a,deny_a=a.pair(); allow_b,deny_b=b.pair()
    deny=discord.Permissions(deny_a.value | deny_b.value)
    allow=discord.Permissions((allow_a.value | allow_b.value) & ~deny.value)
    return discord.PermissionOverwrite.from_pair(allow,deny)

async def _preserve_duplicate_role_overwrites(guild: discord.Guild, canonical: discord.Role, duplicate: discord.Role):
    """Copy any duplicate-role channel/category overwrite onto the canonical role before deletion."""
    failures=[]
    for channel in guild.channels:
        if duplicate not in channel.overwrites:
            continue
        try:
            dup_ow=channel.overwrites_for(duplicate)
            canonical_ow=channel.overwrites_for(canonical)
            await channel.set_permissions(canonical,overwrite=_merge_overwrites(canonical_ow,dup_ow),
                                          reason='Battalion Clerk — preserve access while consolidating duplicate managed role')
        except Exception as exc:
            failures.append(f'{channel.name}: {exc}')
    return failures

def _member_access_roles(guild: discord.Guild):
    # A recognized rank is the basic battalion-member access token. Staff roles
    # are also included so staff can reach shared headquarters spaces even when
    # troubleshooting a personnel-role mismatch.
    names = set(RANK_ROLE_BLUEPRINT + STAFF_ACCESS_ROLE_BLUEPRINT)
    return [r for r in guild.roles if r.name in names]


def _overwrite_member(*, view=True, send=True, voice=True):
    return discord.PermissionOverwrite(
        view_channel=view,
        read_messages=view,
        send_messages=send if view else None,
        connect=voice if view else None,
        speak=voice if view else None,
        add_reactions=send if view else None,
        create_public_threads=False if view else None,
        create_private_threads=False if view else None,
        send_messages_in_threads=send if view else None,
    )


def _overwrite_staff(*, view=True):
    return discord.PermissionOverwrite(
        view_channel=view,
        read_messages=view,
        send_messages=True if view else None,
        connect=True if view else None,
        speak=True if view else None,
        add_reactions=True if view else None,
        manage_messages=True if view else None,
        manage_threads=True if view else None,
        move_members=True if view else None,
        mute_members=True if view else None,
        deafen_members=True if view else None,
    )


def _overwrite_functional_appointment():
    # Deliberately does NOT grant view_channel. The Soldier's company/staff
    # assignment controls visibility; the appointment only adds authority once
    # that member already has access to the space.
    return discord.PermissionOverwrite(
        send_messages=True,
        add_reactions=True,
        manage_messages=True,
        manage_threads=True,
        move_members=True,
        mute_members=True,
        deafen_members=True,
    )


COMMAND_APPOINTMENTS = {"Battalion Commander", "Battalion Executive Officer"}
STAFF_APPOINTMENTS = {
    "S1": {"S-1 OIC", "S-1 NCOIC"},
    "S3": {"S-3 OIC", "S-3 NCOIC"},
    "S4": {"S-4 OIC", "S-4 NCOIC"},
}
COMPANY_LEADERSHIP_APPOINTMENTS = {
    "Company Commander", "Company Executive Officer", "First Sergeant",
    "Platoon Leader", "Platoon Sergeant", "Squad Leader", "Assistant Squad Leader", "Team Leader",
}


def _add_roles(overwrites, guild, names, overwrite):
    for name in names:
        role = _role_by_name(guild, name)
        if role:
            overwrites[role] = overwrite


def _scope_overwrites(guild: discord.Guild, scope: str):
    """Authoritative category permission model.

    Rank/MOS/qualification roles carry no guild permissions. Assignment roles
    control visibility. Appointment and staff roles control functional authority.
    """
    everyone = guild.default_role
    overwrites = {
        everyone: discord.PermissionOverwrite(
            view_channel=False, read_messages=False, send_messages=False,
            connect=False, speak=False,
        )
    }

    if scope == "PUBLIC":
        overwrites[everyone] = _overwrite_member(view=True, send=True, voice=True)
        return overwrites

    # Battalion Commander/XO and Command Staff can reach every managed internal area.
    _add_roles(overwrites, guild, {"Command Staff", *COMMAND_APPOINTMENTS}, _overwrite_staff())

    if scope == "MEMBER":
        for role in _member_access_roles(guild):
            # Command Staff already has the stronger overwrite above.
            if role.name != "Command Staff":
                overwrites[role] = _overwrite_member()
        return overwrites

    if scope == "S1":
        _add_roles(overwrites, guild, {"S-1 Personnel", *STAFF_APPOINTMENTS["S1"]}, _overwrite_staff())
    elif scope == "S3":
        _add_roles(overwrites, guild, {"S-3 Operations", *STAFF_APPOINTMENTS["S3"]}, _overwrite_staff())
    elif scope == "S4":
        _add_roles(overwrites, guild, {"S-4 Supply", *STAFF_APPOINTMENTS["S4"]}, _overwrite_staff())
    elif scope == "COMMAND":
        # Only Command Staff / Battalion Commander / Battalion XO were added above.
        pass
    elif scope.startswith("COMPANY:"):
        letter = scope.split(":", 1)[1]
        # Company assignment controls basic visibility.
        _add_roles(overwrites, guild, {f"{letter} Company"}, _overwrite_member())
        # S-1 and S-3 can enter company areas to administer personnel/operations.
        _add_roles(overwrites, guild, {"S-1 Personnel", "S-3 Operations", *STAFF_APPOINTMENTS["S1"], *STAFF_APPOINTMENTS["S3"]}, _overwrite_staff())
        _add_roles(overwrites, guild, COMPANY_LEADERSHIP_APPOINTMENTS, _overwrite_functional_appointment())
    elif scope.startswith("PLATOON:"):
        _, letter, platoon = scope.split(":", 2)
        platoon_role = f"{letter} Company • {platoon} Platoon"
        # Exact company+platoon role is the visibility key. This avoids Discord's
        # additive overwrite behavior accidentally letting B/1st see A/1st.
        _add_roles(overwrites, guild, {platoon_role}, _overwrite_member())
        _add_roles(overwrites, guild, {"S-1 Personnel", "S-3 Operations", *STAFF_APPOINTMENTS["S1"], *STAFF_APPOINTMENTS["S3"]}, _overwrite_staff())
        _add_roles(overwrites, guild, COMPANY_LEADERSHIP_APPOINTMENTS, _overwrite_functional_appointment())

    return overwrites


def _channel_overwrites(guild: discord.Guild, spec: dict, channel_name: str, channel_type: str):
    """Start from category policy, then tighten individual managed channels."""
    overwrites = _scope_overwrites(guild, spec["scope"])
    everyone = guild.default_role

    # Public information sheets are read-only; recruiting/help remain conversational.
    if spec["scope"] == "PUBLIC" and channel_type == "text" and channel_name in {"welcome-to-the-1-5", "standing-orders"}:
        overwrites[everyone] = _overwrite_member(view=True, send=False, voice=False)

    # Headquarters publication channels are read-only to ordinary Soldiers.
    if spec["scope"] == "MEMBER" and channel_type == "text" and channel_name in {
        "battalion-orders", "headquarters-notices", "personnel-orders", "promotions-and-awards"
    }:
        for role in _member_access_roles(guild):
            if role.name in STAFF_ACCESS_ROLE_BLUEPRINT:
                continue
            overwrites[role] = _overwrite_member(view=True, send=False, voice=False)
        # Staff shops may publish the records relevant to them; Command may publish all.
        _add_roles(overwrites, guild, STAFF_ACCESS_ROLE_BLUEPRINT, _overwrite_staff())
        _add_roles(overwrites, guild, {*COMMAND_APPOINTMENTS, *STAFF_APPOINTMENTS["S1"], *STAFF_APPOINTMENTS["S3"], *STAFF_APPOINTMENTS["S4"]}, _overwrite_staff())

    # Squad text/voice channels inside a platoon are visible only to the exact
    # company+platoon+squad assignment role. The parent platoon role is explicitly
    # denied on that channel, while the exact squad role is allowed. A member with
    # both roles therefore sees only their squad channel plus platoon-wide channels.
    if spec["scope"].startswith("PLATOON:") and channel_name in {
        "1st-squad", "2nd-squad", "3rd-squad", "4th-squad"
    }:
        _, letter, platoon = spec["scope"].split(":", 2)
        squad = channel_name.replace("-", " ").title()
        platoon_role = _role_by_name(guild, f"{letter} Company • {platoon} Platoon")
        squad_role = _role_by_name(guild, f"{letter} Company • {platoon} Platoon • {squad}")
        if platoon_role:
            overwrites[platoon_role] = discord.PermissionOverwrite(
                view_channel=False, read_messages=False, send_messages=False,
                connect=False, speak=False,
            )
        if squad_role:
            overwrites[squad_role] = _overwrite_member(view=True, send=True, voice=True)
        # Oversight and leadership retain access to all squads in the platoon.
        _add_roles(overwrites, guild, {"Command Staff", *COMMAND_APPOINTMENTS}, _overwrite_staff())
        _add_roles(overwrites, guild, {"S-1 Personnel", "S-3 Operations", *STAFF_APPOINTMENTS["S1"], *STAFF_APPOINTMENTS["S3"]}, _overwrite_staff())
        _add_roles(overwrites, guild, COMPANY_LEADERSHIP_APPOINTMENTS, _overwrite_functional_appointment())

    return overwrites


async def build_battalion_roles(guild: discord.Guild):
    created=[]; existing=[]; repaired=[]; failed=[]
    me = guild.me
    if not me or not me.guild_permissions.manage_roles:
        raise RuntimeError("Battalion Clerk needs Manage Roles permission.")

    # Rank, MOS, assignment, qualification, divider and staff labels carry no
    # dangerous guild-wide permissions. Access is controlled by channel/category
    # overwrites so rank never equals Discord administrative authority.
    for divider, roles in reversed(ROLE_SECTIONS):
        for name in reversed(roles):
            role = _role_by_name(guild, name)
            if role:
                existing.append(name)
                if role < me.top_role:
                    try:
                        await role.edit(permissions=discord.Permissions.none(), hoist=False, mentionable=False,
                                        reason="Battalion Clerk — enforce managed role safety")
                        repaired.append(name)
                    except Exception as exc:
                        failed.append(f"ROLE PERMISSIONS {name}: {exc}")
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
            if role < me.top_role:
                try:
                    await role.edit(permissions=discord.Permissions.none(), hoist=False, mentionable=False,
                                    reason="Battalion Clerk — enforce visual divider safety")
                    repaired.append(divider)
                except Exception as exc:
                    failed.append(f"DIVIDER PERMISSIONS {divider}: {exc}")
        else:
            try:
                await guild.create_role(name=divider, permissions=discord.Permissions.none(), hoist=False, mentionable=False,
                                        reason="Battalion Clerk — visual role divider")
                created.append(divider)
            except Exception as exc:
                failed.append(f"{divider}: {exc}")

    # Position the managed block immediately below the bot's highest role.
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
    return {"created":created,"existing":existing,"repaired":repaired,"failed":failed}


async def build_battalion_channels(guild: discord.Guild):
    created=[]; existing=[]; repaired=[]; failed=[]
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
                                    reason="Battalion Clerk — enforce category permissions")
                repaired.append(f"PERMISSIONS:{spec['category']}")
            except Exception as exc:
                failed.append(f"PERMISSIONS {spec['category']}: {exc}")

        for channel_name,channel_type in spec["channels"]:
            if channel_type == "text":
                found=discord.utils.get(category.text_channels,name=channel_name)
            else:
                found=discord.utils.get(category.voice_channels,name=channel_name)
            channel_overwrites=_channel_overwrites(guild,spec,channel_name,channel_type)
            if found:
                existing.append(f"{channel_type.upper()}:{spec['category']}/{channel_name}")
                try:
                    await found.edit(overwrites=channel_overwrites, reason="Battalion Clerk — enforce channel permissions")
                    repaired.append(f"{channel_type.upper()}:{spec['category']}/{channel_name}")
                except Exception as exc:
                    failed.append(f"CHANNEL PERMISSIONS {spec['category']}/{channel_name}: {exc}")
                continue
            try:
                if channel_type == "text":
                    await guild.create_text_channel(channel_name, category=category, overwrites=channel_overwrites,
                                                    reason="Battalion Clerk — 1/5 CAV channel structure")
                else:
                    await guild.create_voice_channel(channel_name, category=category, overwrites=channel_overwrites,
                                                     reason="Battalion Clerk — 1/5 CAV channel structure")
                created.append(f"{channel_type.upper()}:{spec['category']}/{channel_name}")
            except Exception as exc:
                failed.append(f"{channel_type.upper()} {spec['category']}/{channel_name}: {exc}")
    return {"created":created,"existing":existing,"repaired":repaired,"failed":failed}



def _managed_role_names():
    names=[]
    for divider,roles in ROLE_SECTIONS:
        names.append(divider)
        names.extend(roles)
    # Include legacy generic platoon/squad roles so a clean reset can remove the old
    # pre-strict-access structure before rebuilding assignment-specific roles.
    names.extend(sorted(LEGACY_ASSIGNMENT_ROLE_NAMES))
    names.extend(sorted(LEGACY_RECRUITING_STATUS_ROLE_NAMES))
    return list(dict.fromkeys(names))


async def reset_battalion_roles(guild: discord.Guild):
    """Delete only Battalion Clerk managed roles/dividers; never touch unrelated roles."""
    me=guild.me
    if not me or not me.guild_permissions.manage_roles:
        raise RuntimeError("Battalion Clerk needs Manage Roles permission.")
    deleted=[]; failed=[]; skipped=[]
    # Delete low-to-high to reduce hierarchy churn.
    managed=[r for r in guild.roles if r.name in set(_managed_role_names())]
    managed.sort(key=lambda r:r.position)
    for role in managed:
        if role >= me.top_role:
            skipped.append(role.name)
            continue
        try:
            await role.delete(reason="Battalion Clerk — authorized managed-role reset")
            deleted.append(role.name)
        except Exception as exc:
            failed.append(f"{role.name}: {exc}")
    return {"deleted":deleted,"failed":failed,"skipped":skipped}

async def cleanup_legacy_platoon_structure(guild: discord.Guild):
    """Remove only old Battalion Clerk platoon channels/roles superseded by strict access."""
    deleted=[]; failed=[]
    me=guild.me
    # Old setup put 1st/2nd/3rd platoon text+voice channels directly in company categories.
    for letter in ("A", "B", "C"):
        category=discord.utils.get(guild.categories, name=f"{letter} COMPANY")
        if not category:
            continue
        for old_name in ("1st-platoon", "2nd-platoon", "3rd-platoon", "4th-platoon"):
            for channel in list(category.channels):
                if channel.name == old_name:
                    try:
                        await channel.delete(reason="Battalion Clerk — replace legacy platoon channel with strict-access platoon category")
                        deleted.append(f"CHANNEL:{letter} COMPANY/{old_name}")
                    except Exception as exc:
                        failed.append(f"{letter} COMPANY/{old_name}: {exc}")
    # Old generic platoon/squad roles cannot safely gate assignment-specific channels
    # because Discord permission overwrites are additive across roles. Remove them.
    if me and me.guild_permissions.manage_roles:
        for role in list(guild.roles):
            if role.name in LEGACY_ASSIGNMENT_ROLE_NAMES and role < me.top_role:
                try:
                    await role.delete(reason="Battalion Clerk — migrate to assignment-specific access role")
                    deleted.append(f"ROLE:{role.name}")
                except Exception as exc:
                    failed.append(f"ROLE {role.name}: {exc}")
    return {"deleted":deleted,"failed":failed}


def structure_inventory(guild: discord.Guild):
    expected_roles=[]
    for divider,roles in ROLE_SECTIONS:
        expected_roles.append(divider); expected_roles.extend(roles)
    missing_roles=[name for name in expected_roles if not _role_by_name(guild,name)]
    managed_norm={_normalized_role_name(x) for x in expected_roles}|{_normalized_role_name(x) for x in LEGACY_ASSIGNMENT_ROLE_NAMES}|{_normalized_role_name(x) for x in LEGACY_RECRUITING_STATUS_ROLE_NAMES}
    grouped={}
    for role in guild.roles:
        key=_normalized_role_name(role.name)
        if key in managed_norm or _is_managed_formation_role_name(role.name):
            grouped.setdefault(key,[]).append(role)
    duplicate_roles=[{"name":key,"count":len(rows),"ids":[r.id for r in rows]} for key,rows in grouped.items() if len(rows)>1]
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
            "duplicate_managed_roles":duplicate_roles,
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

async def recruiting_status_for(member: discord.Member):
    try:
        return await web.request('GET','/internal/clerk/recruiting/status',params={'guild_id':member.guild.id,'discord_user_id':member.id})
    except Exception as exc:
        log.warning('[RECRUITING STATUS FAILED] member=%s error=%s',member.id,exc)
        return {'ok':False,'exists':False}

async def ensure_recruit_status_role(member: discord.Member, approved: bool=False):
    # Approved applicants enter Replacement Depot and are recognized immediately as
    # members of the 5th Cavalry Regiment. Formation/rank/MOS roles still wait for the
    # authoritative Website assignment after Welcome Packet acceptance.
    desired_name='Replacement Depot' if approved else 'Prospective Replacement'
    desired=discord.utils.get(member.guild.roles,name=desired_name)
    remove=[discord.utils.get(member.guild.roles,name=n) for n in RECRUITING_STATUS_ROLE_BLUEPRINT if n!=desired_name]
    remove += [discord.utils.get(member.guild.roles,name=n) for n in LEGACY_RECRUITING_STATUS_ROLE_NAMES]
    try:
        remove=[r for r in remove if r and r in member.roles]
        if remove: await member.remove_roles(*remove,reason='Recruiting case status synchronization')
        add=[]
        if desired and desired not in member.roles:
            add.append(desired)
        if approved:
            membership=await _ensure_dynamic_role(member.guild,'5th Cavalry Regiment')
            if membership and membership not in member.roles:
                add.append(membership)
        if add:
            await member.add_roles(*add,reason='Recruiting case status synchronization')
    except discord.Forbidden:
        log.warning('[RECRUIT ROLE SYNC BLOCKED] member=%s',member.id)


async def clear_recruit_status_roles(member: discord.Member):
    roles=[discord.utils.get(member.guild.roles,name=name) for name in [*RECRUITING_STATUS_ROLE_BLUEPRINT,*LEGACY_RECRUITING_STATUS_ROLE_NAMES]]
    roles=[role for role in roles if role and role in member.roles]
    if not roles: return
    try:
        await member.remove_roles(*roles,reason='Recruiting case closed or converted')
    except discord.Forbidden:
        log.warning('[RECRUIT ROLE CLEAR BLOCKED] member=%s',member.id)

async def cleanup_legacy_recruiting_status_role(guild: discord.Guild):
    """Remove the obsolete Approved Replacement role without touching unrelated roles."""
    removed_from=[]; deleted=[]; failed=[]
    me=guild.me
    for legacy_name in LEGACY_RECRUITING_STATUS_ROLE_NAMES:
        role=discord.utils.get(guild.roles,name=legacy_name)
        if not role:
            continue
        # First strip the obsolete role from every member. Their canonical state will
        # be restored by recruit/personnel reconciliation as Prospective Replacement,
        # Replacement Depot, or full battalion membership.
        for member in list(role.members):
            try:
                if me and me.guild_permissions.manage_roles and role < me.top_role:
                    await member.remove_roles(role,reason='Battalion Clerk — retire obsolete recruiting status role')
                    removed_from.append(str(member.id))
            except Exception as exc:
                failed.append(f'MEMBER {member.id}: {exc}')
        try:
            if me and me.guild_permissions.manage_roles and role < me.top_role and not role.members:
                await role.delete(reason='Battalion Clerk — remove obsolete Approved Replacement role')
                deleted.append(legacy_name)
        except Exception as exc:
            failed.append(f'ROLE {legacy_name}: {exc}')
    return {'removed_from':removed_from,'deleted':deleted,'failed':failed}


def validate_personnel_roles(member: discord.Member):
    ranks,mos=_role_code_hits(member)
    problems=[]
    role_names=[" ".join(r.name.upper().strip().split()) for r in member.roles if r.name!="@everyone"]
    companies=set(); platoons=set(); squads=set(); strict_assignments=[]
    company_alias={
        "A COMPANY":"A", "ALPHA COMPANY":"A", "A/1-5 CAV":"A",
        "B COMPANY":"B", "BRAVO COMPANY":"B", "B/1-5 CAV":"B",
        "C COMPANY":"C", "CHARLIE COMPANY":"C", "C/1-5 CAV":"C",
        "HHC":"HHC", "HHC/1-5 CAV":"HHC", "HEADQUARTERS & HEADQUARTERS COMPANY":"HHC",
    }
    for name in role_names:
        if name in company_alias:
            companies.add(company_alias[name])
        # Strict assignment roles carry their parent organization in the role name.
        # Read those parents without counting the same platoon twice when a member
        # correctly holds Company + Platoon + Squad roles simultaneously.
        m=re.fullmatch(r"([ABC]) COMPANY • (1ST|2ND|3RD|4TH) PLATOON(?: • (1ST|2ND|3RD|4TH) SQUAD)?", name)
        if m:
            co,pl,sq=m.group(1),f"{m.group(2).title()} Platoon",m.group(3)
            companies.add(co); platoons.add((co,pl))
            if sq:
                squads.add((co,pl,f"{sq.title()} Squad"))
                strict_assignments.append((co,pl,f"{sq.title()} Squad"))
            continue
        # Legacy generic assignment roles are still understood for diagnostics.
        m=re.fullmatch(r"(1ST|2ND|3RD|4TH) PLATOON", name)
        if m: platoons.add((None,f"{m.group(1).title()} Platoon"))
        m=re.fullmatch(r"(1ST|2ND|3RD|4TH) SQUAD", name)
        if m: squads.add((None,None,f"{m.group(1).title()} Squad"))
    if len(ranks)==0: problems.append("recognized rank role required")
    elif len(ranks)>1: problems.append("multiple rank roles: "+", ".join(code for code,_ in ranks))
    if len(mos)==0: problems.append("recognized battlefield MOS role required")
    elif len(mos)>1: problems.append("multiple primary MOS roles: "+", ".join(code for code,_ in mos))
    if len(companies)>1: problems.append("multiple company assignments: "+", ".join(sorted(companies)))
    # Multiple unique platoon/squad assignments are conflicts; duplicate parent references are not.
    if len(platoons)>1: problems.append("multiple platoon assignments: "+", ".join(sorted(f"{c or '?'} {p}" for c,p in platoons)))
    if len(squads)>1: problems.append("multiple squad assignments: "+", ".join(sorted(f"{c or '?'} {p or '?'} {q}" for c,p,q in squads)))
    company=next(iter(companies),None)
    platoon=next(iter(platoons), (None,None))[1] if platoons else None
    squad=next(iter(squads), (None,None,None))[2] if squads else None
    return {"valid":not problems,"rank":ranks[0][0] if len(ranks)==1 else None,"mos":mos[0][0] if len(mos)==1 else None,"problems":problems,"ranks":ranks,"mos_roles":mos,"company":company,"platoon":platoon,"squad":squad}

async def _settled_personnel_sync(guild_id: int, member_id: int, reason: str):
    try:
        await asyncio.sleep(PERSONNEL_ROLE_SETTLE_SECONDS)
        guild = bot.get_guild(guild_id)
        member = guild.get_member(member_id) if guild else None
        if not member or member.bot:
            return
        await collector.upsert_member(member)
        # Existing personnel records remain authoritative and may always synchronize.
        existing = await sync_personnel_identity(member, create_if_missing=False, reason=reason, deliver_credentials=False)
        if existing and existing.get('linked'):
            log.info('[PERSONNEL EXISTING SYNC] member=%s (%s)',member.display_name,member.id)
            return
        # New personnel creation is gated by an approved, Discord-verified Recruiting Case.
        recruit = await recruiting_status_for(member)
        case = recruit.get('case') if recruit and recruit.get('exists') else None
        status = str((case or {}).get('status') or '').upper()
        if status in {'DENIED','CLOSED','ENLISTED'}:
            await clear_recruit_status_roles(member)
            log.info('[PERSONNEL CREATION CLOSED] member=%s (%s): recruiting case status=%s',member.display_name,member.id,status or 'NONE')
            return
        if not case or status not in {'REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'}:
            await ensure_recruit_status_role(member,approved=False)
            log.warning('[PERSONNEL CREATION HOLD] member=%s (%s): Replacement Depot / approved recruiting case required',member.display_name,member.id)
            return
        # Approval opens the website-authoritative Replacement Detachment record immediately.
        # Discord rank/MOS/company/platoon/squad roles are outputs of S-1 processing, not
        # prerequisites for creating the Soldier's 201 File.
        await ensure_recruit_status_role(member,approved=True)
        try:
            result=await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/provision",json={
                'guild_id':member.guild.id,'discord_user_id':member.id,'username':member.name,'display_name':member.display_name,'ensure_credentials':False
            })
            if result.get('ok'):
                log.info('[REPLACEMENT PROVISIONED] member=%s (%s) personnel=%s',member.display_name,member.id,result.get('personnel_id'))
            else:
                log.warning('[REPLACEMENT PROVISION HOLD] member=%s (%s): %s',member.display_name,member.id,result.get('error'))
        except Exception as exc:
            log.warning('[REPLACEMENT PROVISION FAILED] member=%s (%s): %s',member.display_name,member.id,exc)
        return
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
            welcome_channel_id TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await db.execute("ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS operation_duty_channel_id TEXT")
    await db.execute("ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS welcome_channel_id TEXT")
    await db.execute("ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS inactivity_warning_days INTEGER DEFAULT 7")
    await db.execute("ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS inactivity_s1_days INTEGER DEFAULT 14")
    await db.execute("ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS inactivity_property_days INTEGER DEFAULT 21")
    await db.execute("ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS inactivity_command_days INTEGER DEFAULT 30")
    await db.execute("ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS operation_rounds_default INTEGER DEFAULT 180")
    await db.execute("ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS operation_reminder_channel_id TEXT")
    await db.execute("ALTER TABLE clerk_guild_settings ADD COLUMN IF NOT EXISTS operation_reminder_minutes TEXT DEFAULT '1440,120,30'")
    await db.execute("""CREATE TABLE IF NOT EXISTS clerk_operation_reminder_notices (
        guild_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        minutes_before INTEGER NOT NULL,
        sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(guild_id,event_id,minutes_before)
    )""")
    await db.execute("""CREATE TABLE IF NOT EXISTS clerk_operation_schedule_notices (
        guild_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        channel_id TEXT,
        event_fingerprint TEXT,
        sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY(guild_id,event_id)
    )""")
    await db.execute("ALTER TABLE clerk_operation_schedule_notices ADD COLUMN IF NOT EXISTS event_fingerprint TEXT")
    await db.execute("""CREATE TABLE IF NOT EXISTS clerk_report_channels (guild_id TEXT NOT NULL, report_type TEXT NOT NULL, channel_id TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(guild_id,report_type))""")
    await db.execute("""CREATE TABLE IF NOT EXISTS clerk_automation_notices (guild_id TEXT NOT NULL, personnel_id TEXT NOT NULL, notice_type TEXT NOT NULL, notice_key TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(guild_id,personnel_id,notice_type,notice_key))""")
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


async def set_operation_reminder_channel(guild_id:int,channel_id:int):
    await ensure_clerk_settings_table()
    db=getattr(collector,'db',None)
    await db.execute("""INSERT INTO clerk_guild_settings(guild_id,operation_reminder_channel_id,updated_at)
      VALUES($1,$2,NOW()) ON CONFLICT(guild_id) DO UPDATE SET operation_reminder_channel_id=EXCLUDED.operation_reminder_channel_id,updated_at=NOW()""",
      str(guild_id),str(channel_id))

async def get_operation_reminder_channel_id(guild_id:int)->Optional[int]:
    await ensure_clerk_settings_table()
    db=getattr(collector,'db',None)
    if not db or not getattr(db,'pool',None): return None
    async with db.pool.acquire() as conn:
        value=await conn.fetchval("SELECT operation_reminder_channel_id FROM clerk_guild_settings WHERE guild_id=$1",str(guild_id))
    return int(value) if value else None

async def set_operation_reminder_minutes(guild_id:int,values):
    await ensure_clerk_settings_table()
    cleaned=sorted({max(5,min(10080,int(v))) for v in values if v is not None},reverse=True) or [1440,120,30]
    db=getattr(collector,'db',None)
    await db.execute("""INSERT INTO clerk_guild_settings(guild_id,operation_reminder_minutes,updated_at)
      VALUES($1,$2,NOW()) ON CONFLICT(guild_id) DO UPDATE SET operation_reminder_minutes=EXCLUDED.operation_reminder_minutes,updated_at=NOW()""",
      str(guild_id),",".join(str(v) for v in cleaned))
    return cleaned

async def get_operation_reminder_minutes(guild_id:int):
    await ensure_clerk_settings_table()
    db=getattr(collector,'db',None)
    if not db or not getattr(db,'pool',None): return [1440,120,30]
    async with db.pool.acquire() as conn:
        raw=await conn.fetchval("SELECT COALESCE(operation_reminder_minutes,'1440,120,30') FROM clerk_guild_settings WHERE guild_id=$1",str(guild_id))
    try:
        return sorted({int(x.strip()) for x in str(raw or '').split(',') if x.strip()},reverse=True) or [1440,120,30]
    except Exception:
        return [1440,120,30]

def format_reminder_interval(minutes:int)->str:
    if minutes%1440==0:
        d=minutes//1440
        return f"{d} DAY" if d==1 else f"{d} DAYS"
    if minutes%60==0:
        h=minutes//60
        return f"{h} HOUR" if h==1 else f"{h} HOURS"
    return f"{minutes} MINUTES"

async def operation_reminder_was_sent(guild_id:int,event_id:str,minutes_before:int)->bool:
    await ensure_clerk_settings_table()
    db=getattr(collector,'db',None)
    async with db.pool.acquire() as conn:
        value=await conn.fetchval("""SELECT 1 FROM clerk_operation_reminder_notices
          WHERE guild_id=$1 AND event_id=$2 AND minutes_before=$3""",
          str(guild_id),str(event_id),int(minutes_before))
    return bool(value)

async def mark_operation_reminder_sent(guild_id:int,event_id:str,minutes_before:int):
    db=getattr(collector,'db',None)
    await db.execute("""INSERT INTO clerk_operation_reminder_notices(guild_id,event_id,minutes_before,sent_at)
      VALUES($1,$2,$3,NOW()) ON CONFLICT DO NOTHING""",
      str(guild_id),str(event_id),int(minutes_before))

async def post_operation_reminder(guild:discord.Guild,event:dict,minutes_before:int):
    channel=await resolve_operation_notice_channel(guild)
    if not isinstance(channel,discord.TextChannel): return False
    start=event_timestamp(event.get('starts_at'))
    if not start: return False
    title=event.get('title') or 'UNNAMED OPERATION'
    duty_id=event.get('channel_id')
    duty=f'<#{duty_id}>' if duty_id else 'AS DIRECTED'
    body=(f"**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\\n"
          f"**OPERATION REMINDER — {format_reminder_interval(minutes_before)} TO STEP-OFF**\\n\\n"
          f"**{title.upper()}**\\n"
          f"Step-Off: <t:{int(start.timestamp())}:F> • <t:{int(start.timestamp())}:R>\\n"
          f"Duty Station: {duty}\\n"
          "Official Credit Requirement: **45 qualifying minutes**\\n\\n"
          "**ALL AVAILABLE PERSONNEL REPORT IN ACCORDANCE WITH ASSIGNMENT.**")
    await channel.send(body[:2000])
    return True

def operation_event_fingerprint(event:dict)->str:
    return "|".join(str(event.get(k) or '') for k in ('title','starts_at','ends_at','channel_id','credit_threshold_minutes'))

async def operation_schedule_notice_was_sent(guild_id:int,event:dict)->bool:
    await ensure_clerk_settings_table()
    db=getattr(collector,'db',None)
    event_id=str(event.get('id') or event.get('event_id') or '')
    async with db.pool.acquire() as conn:
        value=await conn.fetchval("SELECT event_fingerprint FROM clerk_operation_schedule_notices WHERE guild_id=$1 AND event_id=$2",str(guild_id),event_id)
    return bool(value and value==operation_event_fingerprint(event))

async def mark_operation_schedule_notice_sent(guild_id:int,event:dict,channel_id:Optional[int]):
    db=getattr(collector,'db',None)
    event_id=str(event.get('id') or event.get('event_id') or '')
    fingerprint=operation_event_fingerprint(event)
    await db.execute("""INSERT INTO clerk_operation_schedule_notices(guild_id,event_id,channel_id,event_fingerprint,sent_at)
                        VALUES($1,$2,$3,$4,NOW()) ON CONFLICT(guild_id,event_id) DO UPDATE SET
                        channel_id=EXCLUDED.channel_id,event_fingerprint=EXCLUDED.event_fingerprint,sent_at=NOW()""",
                     str(guild_id),event_id,str(channel_id) if channel_id else None,fingerprint)

async def resolve_operation_notice_channel(guild:discord.Guild):
    """Use configured Operation notices first, then safe automatic fallbacks."""
    candidate_ids=[]
    for getter in (get_operation_reminder_channel_id,get_orders_channel_id,get_operation_duty_channel_id):
        try:
            cid=await getter(guild.id)
            if cid and cid not in candidate_ids: candidate_ids.append(cid)
        except Exception:
            pass
    for cid in candidate_ids:
        ch=guild.get_channel(cid)
        if isinstance(ch,discord.TextChannel):
            perms=ch.permissions_for(guild.me) if guild.me else None
            if not perms or perms.send_messages:
                return ch
    if isinstance(guild.system_channel,discord.TextChannel):
        perms=guild.system_channel.permissions_for(guild.me) if guild.me else None
        if not perms or perms.send_messages:
            return guild.system_channel
    for ch in guild.text_channels:
        perms=ch.permissions_for(guild.me) if guild.me else None
        if not perms or perms.send_messages:
            return ch
    return None

async def post_operation_scheduled_notice(guild:discord.Guild,event:dict):
    channel=await resolve_operation_notice_channel(guild)
    if not isinstance(channel,discord.TextChannel): return False
    start=event_timestamp(event.get('starts_at'))
    end=event_timestamp(event.get('ends_at'))
    title=event.get('title') or 'UNNAMED OPERATION'
    duty_id=event.get('channel_id')
    duty=f'<#{duty_id}>' if duty_id else 'AS DIRECTED'
    credit_minutes=int(event.get('credit_threshold_minutes') or 45)
    when=f"<t:{int(start.timestamp())}:F> • <t:{int(start.timestamp())}:R>" if start else "TIME TO BE ANNOUNCED"
    ends=f"<t:{int(end.timestamp())}:t>" if end else "AS DIRECTED"
    body=(f"**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n"
          f"**OPERATION SCHEDULED**\n\n"
          f"**{title.upper()}**\n"
          f"Step-Off: {when}\n"
          f"Estimated End: {ends}\n"
          f"Operation Voice: {duty}\n"
          f"Official Credit Requirement: **{credit_minutes} qualifying minutes**\n"
          f"M16 Service: **tracked from verified HLL server activity while the rifle is issued**\n\n"
          "Battalion Clerk has armed attendance, reminders, HLL telemetry, and issued-weapon tracking for this Operation.")
    await channel.send(body[:2000])
    return channel.id

WELCOME_MESSAGE = """**HEADQUARTERS**
**1ST BATTALION, 5TH CAVALRY REGIMENT**
**1ST CAVALRY DIVISION (AIRMOBILE)**

**REPLACEMENT PERSONNEL — REPORTING NOTICE**

{member_mention}, you have reported to the **1st Battalion, 5th Cavalry Regiment**.

All newly arrived personnel will remain with the **Replacement Detachment** pending completion of battalion in-processing and assignment.

Personnel who have already submitted an enlistment application will have their recruiting case reviewed by Battalion Headquarters. Upon approval, you will receive further instructions concerning your initial rank, MOS, company, platoon, and squad assignment.

Until processing is complete, review the battalion information, standing orders, and reporting instructions available within the server.

**DO NOT DEPART THE REPLACEMENT DETACHMENT UNTIL RELEASED OR ASSIGNED.**

Further instructions will be issued by Battalion Headquarters.

**BY ORDER OF THE BATTALION COMMANDER**
**BATTALION CLERK**
**1/5 CAV**"""

async def set_welcome_channel(guild_id: int, channel_id: int):
    await ensure_clerk_settings_table()
    db = getattr(collector, 'db', None)
    if not db or not getattr(db, 'pool', None):
        raise RuntimeError('PostgreSQL is not available for Battalion Clerk settings.')
    await db.execute(
        """INSERT INTO clerk_guild_settings(guild_id,welcome_channel_id,updated_at) VALUES($1,$2,NOW())
           ON CONFLICT(guild_id) DO UPDATE SET welcome_channel_id=EXCLUDED.welcome_channel_id, updated_at=NOW()""",
        str(guild_id), str(channel_id)
    )

async def clear_welcome_channel(guild_id: int):
    await ensure_clerk_settings_table()
    db = getattr(collector, 'db', None)
    if db and getattr(db, 'pool', None):
        await db.execute("UPDATE clerk_guild_settings SET welcome_channel_id=NULL, updated_at=NOW() WHERE guild_id=$1", str(guild_id))

async def get_welcome_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    await ensure_clerk_settings_table()
    db = getattr(collector, 'db', None)
    channel_id = None
    if db and getattr(db, 'pool', None):
        async with db.pool.acquire() as conn:
            value = await conn.fetchval("SELECT welcome_channel_id FROM clerk_guild_settings WHERE guild_id=$1", str(guild.id))
        channel_id = int(value) if value else None
    channel = guild.get_channel(channel_id) if channel_id else None
    if isinstance(channel, discord.TextChannel):
        return channel
    # The standard battalion structure already creates this public reception channel.
    fallback = discord.utils.get(guild.text_channels, name='welcome-to-the-1-5')
    return fallback if isinstance(fallback, discord.TextChannel) else None

async def post_public_welcome(member: discord.Member):
    if member.bot:
        return False
    channel = await get_welcome_channel(member.guild)
    if not channel:
        log.warning('[WELCOME] no welcome channel configured or named welcome-to-the-1-5 guild=%s', member.guild.id)
        return False
    try:
        await channel.send(
            WELCOME_MESSAGE.format(member_mention=member.mention),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        return True
    except discord.Forbidden:
        log.warning('[WELCOME] bot cannot send to channel=%s guild=%s', channel.id, member.guild.id)
    except Exception as exc:
        log.warning('[WELCOME] post failed member=%s error=%s', member.id, exc)
    return False

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
    credit_minutes = int(event.get('credit_threshold_minutes') or 45)

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
            f'Minimum Service Credit: **{credit_minutes} minutes present**\n\n'
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
            f'Personnel must remain present for **{credit_minutes} qualifying minutes** to receive service credit.'
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
async def clerk_health_watch():
    await collector.start()
    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID:
            continue
        try:
            await collector.db.execute("""CREATE TABLE IF NOT EXISTS clerk_runtime_health(
                guild_id BIGINT PRIMARY KEY,bot_user TEXT,status TEXT NOT NULL DEFAULT 'ONLINE',
                voice_collector_running BOOLEAN NOT NULL DEFAULT FALSE,last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                details_json JSONB NOT NULL DEFAULT '{}'::jsonb)""")
            await collector.db.execute("""INSERT INTO clerk_runtime_health(guild_id,bot_user,status,voice_collector_running,last_seen_at,details_json)
                VALUES($1,$2,'ONLINE',$3,NOW(),$4::jsonb)
                ON CONFLICT(guild_id) DO UPDATE SET bot_user=EXCLUDED.bot_user,status='ONLINE',voice_collector_running=EXCLUDED.voice_collector_running,last_seen_at=NOW(),details_json=EXCLUDED.details_json""",
                guild.id,str(bot.user),flush_duty_chunks.is_running(),json.dumps({'guild_name':guild.name}))
            await collector.db.execute("""CREATE TABLE IF NOT EXISTS discord_channel_directory(
                guild_id BIGINT NOT NULL,channel_id BIGINT NOT NULL,channel_name TEXT NOT NULL,channel_type TEXT NOT NULL DEFAULT 'VOICE',
                category_name TEXT,active BOOLEAN NOT NULL DEFAULT TRUE,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(guild_id,channel_id))""")
            await collector.db.execute("UPDATE discord_channel_directory SET active=FALSE,updated_at=NOW() WHERE guild_id=$1",guild.id)
            for voice_channel in guild.voice_channels:
                await collector.db.execute("""INSERT INTO discord_channel_directory(guild_id,channel_id,channel_name,channel_type,category_name,active,updated_at)
                    VALUES($1,$2,$3,'VOICE',$4,TRUE,NOW()) ON CONFLICT(guild_id,channel_id) DO UPDATE SET channel_name=EXCLUDED.channel_name,category_name=EXCLUDED.category_name,active=TRUE,updated_at=NOW()""",
                    guild.id,voice_channel.id,voice_channel.name,voice_channel.category.name if voice_channel.category else None)
        except Exception as exc:
            log.warning('[CLERK HEALTH] guild=%s error=%s',guild.id,exc)

@clerk_health_watch.before_loop
async def before_clerk_health_watch():
    await bot.wait_until_ready()

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

@tasks.loop(minutes=10)
async def operation_maintenance_watch():
    """Keep operation history, weapon rounds, and archive state reconciled."""
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY:
        return
    try:
        result=await web.request('POST','/internal/clerk/operations/maintenance',json={})
        summary=result.get('summary') or {}
        if int(summary.get('rounds_applied') or 0) or int(summary.get('completed_operations') or 0) or int(summary.get('archived_operations') or 0) or int(summary.get('weapon_timestamp_repairs') or 0):
            log.info('[OPERATION MAINTENANCE] attendance=%s participation=%s full=%s rounds=%s completed=%s archived=%s weapon_ts_repairs=%s weapon_counters=%s',
                     summary.get('attendance_rows',0),summary.get('participation_rows',0),summary.get('full_credit',0),
                     summary.get('rounds_applied',0),summary.get('completed_operations',0),summary.get('archived_operations',0),
                     summary.get('weapon_timestamp_repairs',0),summary.get('weapon_counters_rebuilt',0))
    except Exception as exc:
        log.warning('[OPERATION MAINTENANCE FAILED] %s',exc)


@operation_maintenance_watch.before_loop
async def before_operation_maintenance_watch():
    await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def hll_m16_reconcile_watch():
    """File completed HLL M16 field-use blocks into the issued-rifle ledger."""
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY:
        return
    try:
        result=await web.request('POST','/internal/clerk/weapons/reconcile-hll-rounds',json={'days':365})
        if int(result.get('rounds_applied') or 0):
            log.info('[HLL M16 RECONCILE] players=%s matches=%s blocks=%s rounds=%s weapons=%s',
                     result.get('players',0),result.get('matches',0),result.get('blocks_checked',0),
                     result.get('rounds_applied',0),result.get('weapons_updated',0))
    except Exception as exc:
        log.warning('[HLL M16 RECONCILE FAILED] %s',exc)


@hll_m16_reconcile_watch.before_loop
async def before_hll_m16_reconcile_watch():
    await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def operation_reminder_watch():
    now=utc_now()
    for guild in bot.guilds:
        if GUILD_ID and guild.id!=GUILD_ID:
            continue
        try:
            default_intervals=await get_operation_reminder_minutes(guild.id)
            status=await web.request('GET','/internal/clerk/events/status',params={'guild_id':guild.id})
        except Exception as exc:
            log.warning('[OP REMINDER WATCH] guild=%s error=%s',guild.id,exc)
            continue
        for event in status.get('events',[]):
            if str(event.get('event_type') or '').upper()!='OPERATION':
                continue
            event_id=str(event.get('id') or event.get('event_id') or '')
            start=event_timestamp(event.get('starts_at'))
            if not event_id or not start:
                continue
            seconds=(start-now).total_seconds()
            if seconds<=0:
                continue
            raw_intervals=str(event.get('reminder_minutes') or '').strip()
            intervals=[]
            if raw_intervals:
                for piece in raw_intervals.split(','):
                    try:
                        val=int(piece.strip())
                        if 5 <= val <= 10080: intervals.append(val)
                    except Exception:
                        pass
            intervals=intervals or default_intervals
            for minutes_before in intervals:
                target=minutes_before*60
                if target-90<=seconds<=target+30:
                    try:
                        if await operation_reminder_was_sent(guild.id,event_id,minutes_before):
                            continue
                        if await post_operation_reminder(guild,event,minutes_before):
                            await mark_operation_reminder_sent(guild.id,event_id,minutes_before)
                    except Exception as exc:
                        log.warning('[OP REMINDER POST FAILED] guild=%s event=%s error=%s',guild.id,event_id,exc)

@operation_reminder_watch.before_loop
async def before_operation_reminder_watch():
    await bot.wait_until_ready()


@tasks.loop(seconds=30)
async def duty_announcement_watch():
    """Publish 15-minute and start notices for scheduled duty periods."""
    now = utc_now()
    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID:
            continue
        try:
            await load_duty_bindings(guild.id)
            # If S-3 changed the OPERATION channel from the website, begin tracking
            # members already sitting in that voice channel instead of waiting for
            # their next voice-state change.
            now_seed=utc_now()
            for _event_type,_cid in duty_channel_bindings.get(guild.id,{}).items():
                _channel=guild.get_channel(_cid)
                if isinstance(_channel,discord.VoiceChannel):
                    for _member in _channel.members:
                        if not _member.bot:
                            duty_voice_presence.setdefault((guild.id,_member.id,_channel.id),now_seed)
            status = await web.request('GET', '/internal/clerk/events/status', params={'guild_id': guild.id})
        except Exception as exc:
            log.warning('[ANNOUNCEMENT WATCH] status failed guild=%s error=%s', guild.id, exc)
            continue

        # Event-specific channel cache means each website-scheduled Operation tracks its selected voice channel,
        # even when multiple future/current Operations exist. The old single OPERATION binding remains a fallback.
        active_event_channels[guild.id]={}
        for _event in status.get('events',[]):
            _cid=_event.get('channel_id'); _etype=str(_event.get('event_type') or '').upper()
            if _cid and _etype in {'OPERATION','TRAINING','MEETING'}:
                try: active_event_channels[guild.id][int(_cid)]=_etype
                except Exception: pass

        # Seed anyone already in an event channel after restart/schedule publication.
        now_seed=utc_now()
        for _cid,_etype in active_event_channels.get(guild.id,{}).items():
            _channel=guild.get_channel(_cid)
            if isinstance(_channel,discord.VoiceChannel):
                for _member in _channel.members:
                    if not _member.bot:
                        duty_voice_presence.setdefault((guild.id,_member.id,_cid),now_seed)

        for event in status.get('events', []):
            event_id = str(event.get('id') or event.get('event_id') or '')
            if not event_id:
                continue
            start = event_timestamp(event.get('starts_at'))
            end = event_timestamp(event.get('ends_at'))
            if not start:
                continue

            seconds_to_start = (start - now).total_seconds()
            if str(event.get('event_type') or '').upper()=='OPERATION' and seconds_to_start>0:
                try:
                    if not await operation_schedule_notice_was_sent(guild.id,event):
                        posted_channel_id=await post_operation_scheduled_notice(guild,event)
                        if posted_channel_id:
                            await mark_operation_schedule_notice_sent(guild.id,event,posted_channel_id)
                            announcement_notice_sent.add(event_id)
                except Exception as exc:
                    log.warning('[WEBSITE OP SCHEDULE NOTICE FAILED] event=%s error=%s',event_id,exc)
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


async def send_activity_weapon_round_blocks(guild_id:int, member_id:int, channel_id:int, started:datetime, ended:datetime):
    """Legacy compatibility no-op. Discord voice is attendance evidence only.

    M16 rounds/field service are derived exclusively from verified HLL server
    telemetry. Keeping this helper prevents stale callers from raising errors.
    """
    return 0


async def load_duty_bindings(guild_id: int):
    data = await web.request('GET', '/internal/clerk/channels', params={'guild_id': guild_id})
    duty_channel_bindings[guild_id] = {
        row['event_type']: int(row['channel_id'])
        for row in data.get('channels', [])
    }
    return data.get('channels', [])


def duty_type_for_channel(guild_id: int, channel_id: int) -> Optional[str]:
    event_type=active_event_channels.get(guild_id,{}).get(int(channel_id))
    if event_type:
        return event_type
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

def _is_managed_formation_role_name(name: str) -> bool:
    n=_normalized_role_name(name)
    return bool(re.fullmatch(r"[ABC] COMPANY • (1ST|2ND|3RD|4TH) PLATOON(?: • (1ST|2ND|3RD|4TH) SQUAD)?", n))


def _managed_role_category(name: str) -> str | None:
    n=_normalized_role_name(name)
    if name in MEMBERSHIP_ROLE_BLUEPRINT: return 'MEMBERSHIP'
    if any(_normalized_role_name(name)==_normalized_role_name(x) for x in COMPANY_ROLE_BLUEPRINT): return 'COMPANY'
    if re.fullmatch(r"[ABC] COMPANY • (1ST|2ND|3RD|4TH) PLATOON",n): return 'PLATOON'
    if re.fullmatch(r"[ABC] COMPANY • (1ST|2ND|3RD|4TH) PLATOON • (1ST|2ND|3RD|4TH) SQUAD",n): return 'SQUAD'
    if name in APPOINTMENT_ROLE_BLUEPRINT: return 'APPOINTMENT'
    if name in QUALIFICATION_ROLE_BLUEPRINT: return 'QUALIFICATION'
    if name in STAFF_ACCESS_ROLE_BLUEPRINT: return 'STAFF_ACCESS'
    head=n.split(' — ',1)[0].split(' - ',1)[0].strip()
    if head in RANK_ROLE_CODES or n in RANK_ROLE_ALIASES: return 'RANK'
    if head in MOS_ROLE_CODES: return 'MOS'
    if name in RECRUITING_STATUS_ROLE_BLUEPRINT: return 'RECRUITING'
    if name in LEGACY_RECRUITING_STATUS_ROLE_NAMES: return 'LEGACY'
    if name in LEGACY_ASSIGNMENT_ROLE_NAMES: return 'LEGACY'
    return None


async def _ensure_dynamic_role(guild: discord.Guild, name: str) -> discord.Role | None:
    role=_role_by_name(guild,name)
    if role: return role
    try:
        return await guild.create_role(name=name,permissions=discord.Permissions.none(),hoist=False,mentionable=False,
                                       reason='Battalion Clerk — website-authoritative active formation')
    except discord.Forbidden:
        log.warning('[FORMATION ROLE CREATE BLOCKED] guild=%s role=%s',guild.id,name)
        return None


async def reconcile_member_roles_from_canonical(member: discord.Member, result: dict):
    """Mirror the authoritative website record into Discord and report exactly what changed."""
    if not result.get('linked'):
        return {'ok':False,'error':'personnel record not linked','added':[],'removed':[]}
    rank=result.get('rank_code'); mos=result.get('mos_code')
    lifecycle=str(result.get('lifecycle_state') or '').upper()
    unit=str(result.get('unit_code') or '').upper().strip()
    platoon=str(result.get('platoon') or '').strip()
    squad=str(result.get('squad') or '').strip()
    field_status=str(result.get('field_status') or '').upper().strip()
    desired=[]; remove=[]; created=[]
    rank_role=_guild_role_for_code(member.guild,rank,RANK_ROLE_CODES)
    mos_role=_guild_role_for_code(member.guild,mos,MOS_ROLE_CODES)
    if rank_role: desired.append(rank_role)
    if mos_role: desired.append(mos_role)
    current_ranks,current_mos=_role_code_hits(member)

    company_aliases={
        'A/1-5 CAV':{'A COMPANY','ALPHA COMPANY','A/1-5 CAV'},
        'B/1-5 CAV':{'B COMPANY','BRAVO COMPANY','B/1-5 CAV'},
        'C/1-5 CAV':{'C COMPANY','CHARLIE COMPANY','C/1-5 CAV'},
        'HHC/1-5 CAV':{'HHC','HHC/1-5 CAV','HEADQUARTERS & HEADQUARTERS COMPANY'},
    }
    all_company_names=set().union(*company_aliases.values())
    canonical_company_name={'A/1-5 CAV':'A Company','B/1-5 CAV':'B Company','C/1-5 CAV':'C Company','HHC/1-5 CAV':'HHC'}.get(unit)
    desired_company={_normalized_role_name(canonical_company_name)} if canonical_company_name else set()
    company_letter=unit[:1] if unit[:1] in {'A','B','C'} else None
    pretty_platoon=platoon.title() if platoon else ''
    pretty_squad=squad.title() if squad else ''
    desired_platoon_name=f"{company_letter} Company • {pretty_platoon}" if company_letter and platoon else None
    desired_squad_name=f"{company_letter} Company • {pretty_platoon} • {pretty_squad}" if company_letter and platoon and squad else None
    is_member = bool(field_status=='ASSIGNED' and (platoon or unit.startswith('HHC')) and lifecycle not in {'SEPARATED','ARCHIVED'})

    # Remove stale managed personnel roles. Protected/manual roles are never touched.
    for code,_ in current_ranks:
        if code!=rank:
            r=_guild_role_for_code(member.guild,code,RANK_ROLE_CODES)
            if r: remove.append(r)
    for code,_ in current_mos:
        if code!=mos:
            r=_guild_role_for_code(member.guild,code,MOS_ROLE_CODES)
            if r: remove.append(r)
    for role in member.roles:
        n=_normalized_role_name(role.name)
        if n in all_company_names and n not in desired_company: remove.append(role)
        if _is_managed_formation_role_name(role.name):
            if desired_squad_name and n==_normalized_role_name(desired_squad_name): pass
            elif desired_platoon_name and n==_normalized_role_name(desired_platoon_name): pass
            else: remove.append(role)
        if role.name in LEGACY_ASSIGNMENT_ROLE_NAMES or role.name in LEGACY_RECRUITING_STATUS_ROLE_NAMES: remove.append(role)
        if role.name=='5th Cavalry Regiment' and not (is_member or discord.utils.get(member.roles,name='Replacement Depot')): remove.append(role)

    # Separated/archived Soldiers retain protected Discord roles only.
    managed_appointment_names={'Platoon Sergeant','Squad Leader','Assistant Squad Leader','Team Leader'}
    if lifecycle in {'SEPARATED','ARCHIVED'}:
        for role in member.roles:
            if role.name in managed_appointment_names or _managed_role_category(role.name) in {'COMPANY','PLATOON','SQUAD','MEMBERSHIP'}:
                remove.append(role)
        desired=[]
    else:
        # Company role is a display/access output of the website assignment.
        for role in member.guild.roles:
            if _normalized_role_name(role.name) in desired_company: desired.append(role)
        # Only active formations get Discord roles. No unused future combinations are generated.
        if desired_platoon_name:
            r=await _ensure_dynamic_role(member.guild,desired_platoon_name)
            if r: desired.append(r); created.append(r.name) if r not in member.roles else None
        if desired_squad_name:
            r=await _ensure_dynamic_role(member.guild,desired_squad_name)
            if r: desired.append(r); created.append(r.name) if r not in member.roles else None
        # Team is website-only; this intentionally removes legacy Alpha/Bravo Discord roles.
        if is_member or discord.utils.get(member.roles,name='Replacement Depot'):
            membership=await _ensure_dynamic_role(member.guild,'5th Cavalry Regiment')
            if membership: desired.append(membership)
        if is_member:
            for role in member.roles:
                if role.name in set(RECRUITING_STATUS_ROLE_BLUEPRINT)|LEGACY_RECRUITING_STATUS_ROLE_NAMES: remove.append(role)

        desired_appointment_names=set(result.get('appointment_roles') or []) & managed_appointment_names
        for role in member.roles:
            if role.name in managed_appointment_names and role.name not in desired_appointment_names: remove.append(role)
        for role in member.guild.roles:
            if role.name in desired_appointment_names: desired.append(role)

    remove=list(dict.fromkeys(remove)); desired=list(dict.fromkeys(desired))
    removed_names=[]; added_names=[]
    try:
        actual_remove=[r for r in remove if r in member.roles]
        if actual_remove:
            await member.remove_roles(*actual_remove,reason='Website personnel record is authoritative')
            removed_names=[r.name for r in actual_remove]
        add=[r for r in desired if r not in member.roles]
        if add:
            await member.add_roles(*add,reason='Synchronize authoritative battalion personnel record')
            added_names=[r.name for r in add]
    except discord.Forbidden as exc:
        log.warning('[CANONICAL ROLE SYNC BLOCKED] member=%s bot role hierarchy/permissions',member.id)
        return {'ok':False,'error':'Battalion Clerk role hierarchy/permissions blocked reconciliation','added':added_names,'removed':removed_names,'created':created}
    return {'ok':True,'added':added_names,'removed':removed_names,'created':created,'actual_roles':member_role_names(member),
            'expected':{'rank':rank,'mos':mos,'unit_code':unit,'platoon':platoon,'squad':squad,'fire_team':result.get('fire_team'),'member':is_member}}

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
        try:
            await member.send(
                "**1/5 CAV — MEMBER ACCESS ISSUED**\n\n"
                "Your Battalion personnel record has been created and your member access is now active.\n\n"
                "**Website:** [www.5thcavgaming.com](https://www.5thcavgaming.com)\n\n"
                "**TO LOG IN**\n"
                "1. Open the website above.\n"
                "2. Select **MY SOLDIER RECORD** from the main navigation.\n"
                "3. Enter your **Battle Roster Number**.\n"
                "4. Enter your **Field Code**.\n"
                "5. Select **LOGIN** to open your Soldier Record.\n\n"
                "**YOUR CREDENTIALS**\n"
                f"Battle Roster Number: **{result.get('roster_number')}**\n"
                f"Field Code: **{result.get('field_code')}**"
                f"{weapon_line}\n\n"
                "Keep this information private. Your Battle Roster Number and Field Code provide access to your personal battalion record.\n\n"
                "From your Soldier Record you can review your assignment, MOS, rank, training, qualifications, orders, issued M16, uniform, awards, service history, and other battalion records as they are added throughout your tour.\n\n"
                "If you lose your login information or cannot access your record, contact Battalion Headquarters or S-1 for assistance.\n\n"
                "**BY ORDER OF THE BATTALION COMMANDER**\n"
                "**BATTALION CLERK**\n"
                "**1/5 CAV**"
            )
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


async def organization_cleanup_inventory(guild: discord.Guild):
    inv=structure_inventory(guild)
    legacy=[r for r in guild.roles if any(_normalized_role_name(r.name)==_normalized_role_name(x) for x in LEGACY_ASSIGNMENT_ROLE_NAMES)]
    desired_dynamic=set()
    if WEBSITE_BASE_URL and CLERK_SYNC_KEY:
        try:
            data=await web.request('GET','/internal/clerk/personnel/canonical-roster',params={'guild_id':guild.id})
            for x in data.get('items',[]):
                unit=str(x.get('unit_code') or '').upper().strip(); pl=str(x.get('platoon') or '').strip(); sq=str(x.get('squad') or '').strip(); letter=unit[:1] if unit[:1] in {'A','B','C'} else None
                if letter and pl: desired_dynamic.add(_normalized_role_name(f"{letter} Company • {pl.title()}"))
                if letter and pl and sq: desired_dynamic.add(_normalized_role_name(f"{letter} Company • {pl.title()} • {sq.title()}"))
        except Exception: pass
    dormant=[r for r in guild.roles if _is_managed_formation_role_name(r.name) and _normalized_role_name(r.name) not in desired_dynamic]
    duplicate_details=[]
    for item in inv.get('duplicate_managed_roles',[]):
        canonical=_canonical_managed_role_name(item['name']) or item['name']
        roles=_managed_role_group(guild,canonical)
        duplicate_details.append({
            'canonical':canonical,
            'count':len(roles),
            'member_links':sum(len(r.members) for r in roles),
            'role_ids':[r.id for r in roles],
        })
    return {'duplicates':duplicate_details,'legacy':legacy,'dormant':dormant,'inventory':inv}

async def run_organization_cleanup(guild: discord.Guild):
    """Consolidate duplicate managed roles without changing website personnel authority."""
    me=guild.me
    if not me or not me.guild_permissions.manage_roles:
        raise RuntimeError('Battalion Clerk needs Manage Roles permission.')
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY:
        raise RuntimeError('Website connection is required; cleanup will not run without canonical personnel data.')

    canonical_data=await web.request('GET','/internal/clerk/personnel/canonical-roster',params={'guild_id':guild.id})
    snapshots={int(x['discord_user_id']):x for x in canonical_data.get('items',[]) if x.get('discord_user_id')}
    touched=set(); migrated=0; deleted=[]; renamed=[]; preserved_permissions=0; failures=[]

    # Freeze Discord->website role echo for every linked Soldier while the canonical website state is reapplied.
    for uid in snapshots:
        role_sync_suppressed_members.add((guild.id,uid)); touched.add(uid)
    try:
        # First restore every linked member from the WEBSITE snapshot. This prevents duplicate roles
        # from becoming the source of truth during the maintenance window.
        for uid,snapshot in snapshots.items():
            member=guild.get_member(uid)
            if not member:
                try: member=await guild.fetch_member(uid)
                except Exception: member=None
            if not member: continue
            try:
                await reconcile_member_roles_from_canonical(member,snapshot)
                migrated+=1
            except Exception as exc:
                failures.append(f'MEMBER {uid}: {exc}')

        # Consolidate exact managed-role aliases/case variants, including dynamically
        # created active Platoon/Squad roles. Prefer canonical website-style spelling.
        canonical_names=list(_all_managed_role_names())
        canonical_names.extend([_canonical_managed_role_name(r.name) for r in guild.roles if _is_managed_formation_role_name(r.name)])
        canonical_names=[x for x in dict.fromkeys(canonical_names) if x]
        for canonical_name in canonical_names:
            group=_managed_role_group(guild,canonical_name)
            if not group: continue
            canonical=discord.utils.get(group,name=canonical_name)
            if canonical is None:
                manageable=[r for r in group if r < me.top_role]
                canonical=(manageable[0] if manageable else group[0])
                if canonical < me.top_role:
                    try:
                        old=canonical.name
                        await canonical.edit(name=canonical_name,reason='Battalion Clerk — normalize managed role display name')
                        renamed.append(f'{old} -> {canonical_name}')
                    except Exception as exc:
                        failures.append(f'RENAME {canonical.name}: {exc}')
            for duplicate in list(group):
                if duplicate.id==canonical.id: continue
                if duplicate >= me.top_role:
                    failures.append(f'DUPLICATE ABOVE CLERK {duplicate.name} ({duplicate.id})')
                    continue
                # Preserve any custom/category permission references before moving members and deleting.
                errs=await _preserve_duplicate_role_overwrites(guild,canonical,duplicate)
                if not errs: preserved_permissions += 1
                else: failures.extend([f'OVERWRITE {duplicate.name}: {x}' for x in errs])
                try:
                    members=list(duplicate.members)
                    for member in members:
                        role_sync_suppressed_members.add((guild.id,member.id)); touched.add(member.id)
                        if canonical not in member.roles:
                            await member.add_roles(canonical,reason='Battalion Clerk — consolidate duplicate managed role')
                    await duplicate.delete(reason='Battalion Clerk — duplicate managed role consolidated into canonical role')
                    deleted.append(f'DUPLICATE:{duplicate.name}:{duplicate.id}')
                except Exception as exc:
                    failures.append(f'DUPLICATE {duplicate.name} ({duplicate.id}): {exc}')

        # Remove unused dynamically generated formation roles when they are truly disposable.
        # If a dormant role still owns a channel/category overwrite, preserve it and report it rather
        # than destroying access configuration that may be needed when that formation reactivates.
        desired_dynamic=set()
        for snapshot in snapshots.values():
            unit=str(snapshot.get('unit_code') or '').upper().strip(); pl=str(snapshot.get('platoon') or '').strip(); sq=str(snapshot.get('squad') or '').strip()
            letter=unit[:1] if unit[:1] in {'A','B','C'} else None
            if letter and pl:
                desired_dynamic.add(_normalized_role_name(f"{letter} Company • {pl.title()}"))
            if letter and pl and sq:
                desired_dynamic.add(_normalized_role_name(f"{letter} Company • {pl.title()} • {sq.title()}"))
        for role in list(guild.roles):
            if not _is_managed_formation_role_name(role.name) or _normalized_role_name(role.name) in desired_dynamic:
                continue
            if role >= me.top_role:
                failures.append(f'DORMANT FORMATION ABOVE CLERK {role.name} ({role.id})'); continue
            if role.members:
                failures.append(f'DORMANT FORMATION PRESERVED — {len(role.members)} MEMBER(S) STILL CARRY ROLE {role.name} ({role.id})')
                continue
            overwrite_fail=None
            for channel in list(guild.channels):
                if role not in channel.overwrites: continue
                try:
                    await channel.set_permissions(role,overwrite=None,reason='Battalion Clerk — remove dormant formation permission overwrite')
                except Exception as exc:
                    overwrite_fail=f'{getattr(channel,"name",channel.id)}: {exc}'; break
            if overwrite_fail:
                failures.append(f'DORMANT FORMATION PRESERVED — PERMISSION CLEANUP FAILED {role.name} ({role.id}) {overwrite_fail}')
                continue
            try:
                await role.delete(reason='Battalion Clerk — remove unused website-unassigned formation role')
                deleted.append(f'DORMANT:{role.name}:{role.id}')
            except Exception as exc:
                failures.append(f'DORMANT {role.name}: {exc}')

        # Generic Platoon/Squad/Team roles are obsolete and unsafe for scoped permissions. Website canonical
        # assignment has already been reapplied above, so these can be removed without guessing assignments.
        for role in list(guild.roles):
            if not any(_normalized_role_name(role.name)==_normalized_role_name(x) for x in LEGACY_ASSIGNMENT_ROLE_NAMES):
                continue
            if role >= me.top_role:
                failures.append(f'LEGACY ABOVE CLERK {role.name} ({role.id})'); continue
            try:
                await role.delete(reason='Battalion Clerk — remove obsolete generic formation role after canonical migration')
                deleted.append(f'LEGACY:{role.name}:{role.id}')
            except Exception as exc:
                failures.append(f'LEGACY {role.name}: {exc}')

        # Reapply canonical roles one final time and rebuild channel overwrites to ensure automation,
        # website syncing, and assignment-based Discord access finish in a known-good state.
        for uid,snapshot in snapshots.items():
            member=guild.get_member(uid)
            if member:
                try: await reconcile_member_roles_from_canonical(member,snapshot)
                except Exception as exc: failures.append(f'FINAL MEMBER {uid}: {exc}')
        roles=await build_battalion_roles(guild)
        channels=await build_battalion_channels(guild)
        failures.extend(roles.get('failed',[])); failures.extend(channels.get('failed',[]))
    finally:
        # Keep suppression through the Discord event burst, then resume normal syncing. The website has
        # remained authoritative for the entire operation and no transient role state is written back.
        await asyncio.sleep(2)
        for uid in touched:
            role_sync_suppressed_members.discard((guild.id,uid))

    inv=structure_inventory(guild)
    return {'canonical_members':len(snapshots),'members_reconciled':migrated,'deleted':deleted,'renamed':renamed,
            'preserved_permissions':preserved_permissions,'failures':failures,'inventory':inv}


@bot.tree.command(name='organization-cleanup', description='Safely consolidate duplicate 1/5 CAV roles using the website as authority.')
@app_commands.describe(confirm='Set True to migrate members, preserve permissions, and remove safe duplicate/legacy managed roles')
async def organization_cleanup_command(interaction: discord.Interaction, confirm: bool=False):
    if not await require_manage_guild(interaction): return
    await interaction.response.defer(ephemeral=True)
    preview=await organization_cleanup_inventory(interaction.guild)
    if not confirm:
        await interaction.followup.send(
            '**1/5 CAV ORGANIZATION CLEANUP — PREVIEW**\n'
            f"Duplicate managed name groups: **{len(preview['duplicates'])}**\n"
            f"Obsolete generic Company/Platoon/Squad/Team roles: **{len(preview['legacy'])}**\n"
            f"Dormant generated formation roles: **{len(preview.get('dormant',[]))}**\n\n"
            'No changes were made. The cleanup uses the WEBSITE personnel record as authority, suppresses temporary Discord role-change echo, preserves active-role permissions, archives truly unused formation permission overwrites, then reapplies canonical roles and permissions.\n\n'
            'Run `/organization-cleanup confirm:True` to execute.',ephemeral=True)
        return
    try:
        result=await run_organization_cleanup(interaction.guild)
    except Exception as exc:
        await interaction.followup.send(f'**ORGANIZATION CLEANUP ABORTED**\n`{exc}`\nNo blind cleanup was attempted.',ephemeral=True); return
    inv=result['inventory']
    msg=(
        '**1/5 CAV ORGANIZATION CLEANUP COMPLETE**\n'
        f"Website-linked Soldiers read: **{result['canonical_members']}**\n"
        f"Canonical member reconciliations: **{result['members_reconciled']}**\n"
        f"Roles normalized/renamed: **{len(result['renamed'])}**\n"
        f"Duplicate/legacy roles removed: **{len(result['deleted'])}**\n"
        f"Duplicate permission sets preserved: **{result['preserved_permissions']}**\n"
        f"Remaining duplicate managed role groups: **{len(inv.get('duplicate_managed_roles',[]))}**\n"
        f"Failures requiring review: **{len(result['failures'])}**\n\n"
        '**WEBSITE AUTHORITY PRESERVED** — the cleanup never derives Company/Platoon/Squad/Team from a temporary Discord role state.'
    )
    if result['failures']:
        msg += '\n\n**Review**\n'+'\n'.join(f"• {x}" for x in result['failures'][:12])
    await interaction.followup.send(msg,ephemeral=True)


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
           f"Expected channels: **{inv['expected_channels']}** — Missing: **{len(inv['missing_channels'])}**",
           f"Duplicate managed role names: **{len(inv.get('duplicate_managed_roles',[]))}**"]
    if inv['missing_roles']: lines.append("\n**Missing Roles**\n"+"\n".join(f"• {x}" for x in inv['missing_roles'][:10]))
    if inv['missing_categories']: lines.append("\n**Missing Categories**\n"+"\n".join(f"• {x}" for x in inv['missing_categories'][:10]))
    if inv['missing_channels']: lines.append("\n**Missing Channels**\n"+"\n".join(f"• {x}" for x in inv['missing_channels'][:10]))
    if inv.get('duplicate_managed_roles'):
        lines.append("\n**Duplicate Managed Roles**\n"+"\n".join(f"• {x['name']} — {x['count']} COPIES" for x in inv['duplicate_managed_roles'][:10]))
        lines.append("\nRun `/organization-cleanup` for a no-change preview, then `/organization-cleanup confirm:True` for website-authoritative consolidation.")
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


@bot.tree.command(name='permissions-repair', description='Reapply the approved 1/5 CAV channel and role permission model.')
@app_commands.describe(confirm='Set True to enforce the approved permission model')
async def permissions_repair(interaction: discord.Interaction, confirm: bool):
    if not await require_manage_guild(interaction): return
    if not confirm:
        await interaction.response.send_message('No changes made. Run `/permissions-repair confirm:True` when ready.', ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    roles=await build_battalion_roles(interaction.guild)
    channels=await build_battalion_channels(interaction.guild)
    failures=roles['failed']+channels['failed']
    await interaction.followup.send(
        f"**BATTALION PERMISSION MODEL ENFORCED**\n"
        f"Roles checked/repaired: **{len(roles.get('repaired', []))}**\n"
        f"Categories/channels checked/repaired: **{len(channels.get('repaired', []))}**\n"
        f"Failures: **{len(failures)}**\n\n"
        "Rank/MOS/qualification roles remain permission-neutral. Assignment roles control visibility; staff and appointments control functional authority.",
        ephemeral=True)


@bot.tree.command(name='strict-access-rebuild', description='Migrate company, platoon, and squad areas to strict assignment visibility.')
@app_commands.describe(confirm='Set True to replace legacy access with strict company/platoon/squad access')
async def strict_access_rebuild(interaction: discord.Interaction, confirm: bool):
    if not await require_manage_guild(interaction): return
    if not confirm:
        await interaction.response.send_message(
            'No changes made. Run `/strict-access-rebuild confirm:True` when ready.', ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    cleanup=await cleanup_legacy_platoon_structure(interaction.guild)
    roles=await build_battalion_roles(interaction.guild)
    channels=await build_battalion_channels(interaction.guild)
    inv=structure_inventory(interaction.guild)
    failures=cleanup['failed']+roles['failed']+channels['failed']
    await interaction.followup.send(
        f"**STRICT ASSIGNMENT ACCESS REBUILT**\n"
        f"Legacy items removed: **{len(cleanup['deleted'])}**\n"
        f"New/repaired role items: **{len(roles['created']) + len(roles.get('repaired', []))}**\n"
        f"New/repaired category/channel items: **{len(channels['created']) + len(channels.get('repaired', []))}**\n"
        f"Missing roles/categories/channels: **{len(inv['missing_roles'])}/{len(inv['missing_categories'])}/{len(inv['missing_channels'])}**\n"
        f"Failures: **{len(failures)}**\n\n"
        "Strict access is now enforced at all three levels: company, platoon, and squad. A Company cannot unlock B/C Company; A Company • 1st Platoon cannot unlock another platoon; and A Company • 1st Platoon • 1st Squad cannot see another squad's channels.",
        ephemeral=True)


@bot.tree.command(name='reset-battalion-roles', description='Delete only Battalion Clerk managed roles so they can be rebuilt cleanly.')
@app_commands.describe(confirmation='Type RESET ROLES exactly. This removes managed roles from members.')
async def reset_battalion_roles_command(interaction: discord.Interaction, confirmation: str):
    if not await require_manage_guild(interaction): return
    if confirmation.strip().upper() != 'RESET ROLES':
        await interaction.response.send_message(
            'No changes made. To confirm the destructive reset, run `/reset-battalion-roles confirmation:RESET ROLES`.',
            ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    result=await reset_battalion_roles(interaction.guild)
    msg=(f"**MANAGED ROLE RESET COMPLETE**\nDeleted: **{len(result['deleted'])}**\n"
         f"Skipped above Clerk: **{len(result['skipped'])}**\nFailures: **{len(result['failed'])}**\n\n"
         "Only Battalion Clerk blueprint roles/dividers were targeted. Categories and channels were left in place. "
         "Run `/battalion-setup confirm:True` next to recreate the roles and reapply all strict access permissions.")
    if result['failed']:
        msg += "\n\n**Review:**\n" + "\n".join(f"• {x}" for x in result['failed'][:10])
    await interaction.followup.send(msg, ephemeral=True)


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
    message = (
        "**1/5 CAV — MEMBER ACCESS REISSUED**\n\n"
        "Your Battalion member access has been reissued. Use the credentials below to access your Soldier Record.\n\n"
        "**Website:** [www.5thcavgaming.com](https://www.5thcavgaming.com)\n\n"
        "**TO LOG IN**\n"
        "1. Open the website above.\n"
        "2. Select **MY SOLDIER RECORD** from the main navigation.\n"
        "3. Enter your **Battle Roster Number**.\n"
        "4. Enter your **Field Code**.\n"
        "5. Select **LOGIN** to open your Soldier Record.\n\n"
        "**YOUR CREDENTIALS**\n"
        f"Battle Roster Number: **{result.get('roster_number')}**\n"
        f"Field Code: **{result.get('field_code')}**\n\n"
        "Keep this information private. Your Battle Roster Number and Field Code provide access to your personal battalion record.\n\n"
        "If you cannot access your record, contact Battalion Headquarters or S-1 for assistance.\n\n"
        "**BY ORDER OF THE BATTALION COMMANDER**\n"
        "**BATTALION CLERK**\n"
        "**1/5 CAV**"
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
        channel=await resolve_operation_notice_channel(guild)
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
            data=await web.request('GET','/internal/clerk/operation-duty/pending',params={'guild_id':guild.id})
            for op in data.get('operations',[]):
                try: await publish_operation_duty_roster(guild,op)
                except Exception as exc: log.warning('[OP DUTY POST FAILED] operation=%s error=%s',op.get('operation_id'),exc)
        except Exception as exc:
            log.warning('[OP DUTY WATCH FAILED] guild=%s error=%s',guild.id,exc)

@operation_duty_watch.before_loop
async def before_operation_duty_watch():
    await bot.wait_until_ready()

@bot.tree.command(name='welcome-channel', description='Set the channel where Battalion Clerk welcomes newly arrived personnel.')
@app_commands.describe(channel='Public text channel that receives new-member reporting notices')
async def welcome_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_manage_guild(interaction): return
    await set_welcome_channel(interaction.guild_id, channel.id)
    await interaction.response.send_message(
        f'**Welcome channel assigned:** {channel.mention}\nNew arrivals will receive the 1/5 CAV Replacement Personnel reporting notice there.',
        ephemeral=True,
    )

@bot.tree.command(name='welcome-channel-status', description='Show the channel receiving new-member welcome notices.')
async def welcome_channel_status(interaction: discord.Interaction):
    if not await require_manage_guild(interaction): return
    channel = await get_welcome_channel(interaction.guild)
    await interaction.response.send_message(
        f'**Welcome Channel:** {channel.mention if channel else "NOT ASSIGNED"}',
        ephemeral=True,
    )

@bot.tree.command(name='welcome-channel-clear', description='Clear the configured new-member welcome channel.')
async def welcome_channel_clear(interaction: discord.Interaction):
    if not await require_manage_guild(interaction): return
    await clear_welcome_channel(interaction.guild_id)
    await interaction.response.send_message(
        'Configured welcome channel cleared. Battalion Clerk will fall back to `#welcome-to-the-1-5` if that standard channel exists.',
        ephemeral=True,
    )

@bot.tree.command(name='welcome-preview', description='Preview the exact new-member welcome and approval messages without issuing anything.')
async def welcome_preview(interaction: discord.Interaction):
    if not await require_manage_guild(interaction): return
    await interaction.response.defer(ephemeral=True)
    site=WEBSITE_BASE_URL or 'https://5thcavgaming.com'
    public_preview=WELCOME_MESSAGE.format(member_mention='@NEW-SOLDIER')
    credential_preview=build_recruit_credentials_message(
        {'case_number':'RC-PREVIEW'},
        {'roster_number':'BR-PREVIEW','field_code':'FIELD-CODE-PREVIEW','weapon_serial':'1847002'},
        site=site,
    )
    packet_note=(
        "**WEBSITE WELCOME PACKET**\n"
        f"{site}/welcome-packet\n"
        "Command can preview the exact read-only member packet from Website → S-1 Personnel → Welcome Packet / Onboarding → MEMBER VIEW.\n"
        "This preview sends nothing, creates no credentials, changes no roles, and completes no onboarding tasks."
    )
    chunks=[
        "**WELCOME DELIVERY PREVIEW — NOTHING HAS BEEN ISSUED**\n\n**1. PUBLIC JOIN NOTICE**\n"+public_preview,
        "**2. APPROVAL / PRIVATE CREDENTIAL DM**\n"+credential_preview,
        "**3. MEMBER WEBSITE PACKET**\n"+packet_note,
    ]
    for i,text in enumerate(chunks):
        text=text[:1950]
        if i==0:
            await interaction.followup.send(text,ephemeral=True)
        else:
            await interaction.followup.send(text,ephemeral=True)

@bot.tree.command(name='operation-reminder-channel', description='Assign the channel that receives automatic reminders for scheduled Operations.')
@app_commands.describe(channel='Text channel for Operation reminders')
async def operation_reminder_channel(interaction:discord.Interaction,channel:discord.TextChannel):
    if not await require_manage_guild(interaction):
        return
    await set_operation_reminder_channel(interaction.guild_id,channel.id)
    intervals=await get_operation_reminder_minutes(interaction.guild_id)
    await interaction.response.send_message(
        f'Operation reminders will be posted to {channel.mention}.\\n'
        f'Reminders: **{", ".join(format_reminder_interval(x) for x in intervals)} before step-off**.',
        ephemeral=True)

@bot.tree.command(name='operation-reminder-times', description='Set up to three automatic Operation reminder times.')
@app_commands.describe(first_minutes='First reminder in minutes',second_minutes='Optional second reminder',final_minutes='Optional final reminder')
async def operation_reminder_times(interaction:discord.Interaction,
    first_minutes:app_commands.Range[int,5,10080],
    second_minutes:Optional[app_commands.Range[int,5,10080]]=None,
    final_minutes:Optional[app_commands.Range[int,5,10080]]=None):
    if not await require_manage_guild(interaction):
        return
    values=await set_operation_reminder_minutes(interaction.guild_id,[first_minutes,second_minutes,final_minutes])
    await interaction.response.send_message(
        f'Operation reminder schedule set to **{", ".join(format_reminder_interval(x) for x in values)} before step-off**.',
        ephemeral=True)

@bot.tree.command(name='operation-reminder-status', description='Show the Operation reminder channel and reminder schedule.')
async def operation_reminder_status(interaction:discord.Interaction):
    if not await require_manage_guild(interaction):
        return
    cid=await get_operation_reminder_channel_id(interaction.guild_id)
    ch=interaction.guild.get_channel(cid) if cid else None
    values=await get_operation_reminder_minutes(interaction.guild_id)
    await interaction.response.send_message(
        f'**OPERATION REMINDER SYSTEM**\\n'
        f'Channel: {ch.mention if ch else "NOT ASSIGNED"}\\n'
        f'Reminders: **{", ".join(format_reminder_interval(x) for x in values)} before step-off**\\n'
        'Only scheduled **OPERATION** duty periods trigger these reminders.',
        ephemeral=True)


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
        'Default credit is **45 minutes**; an S-3 scheduled event may set a different requirement.',
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


@bot.tree.command(name='schedule', description='Operations are scheduled from the website S-3 Operations Center.')
async def schedule_duty(interaction: discord.Interaction):
    if not await require_manage_guild(interaction):
        return
    await interaction.response.send_message(
        '**WEBSITE-AUTHORITATIVE OPERATIONS**\n'
        'The Discord `/schedule` workflow has been retired to prevent duplicate or unlinked Operations.\n\n'
        'Schedule and publish the Operation from the **S-3 Operations Center** on the website. '
        'Battalion Clerk automatically receives the Operation ID, selected voice channel, start/end time, '
        'credit threshold, reminders, and ammunition expenditure settings.',
        ephemeral=True,
    )


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
    if str(selected.get('event_type') or '').upper() == 'OPERATION':
        ch=await get_report_channel(interaction.guild,'POST_OPERATION')
        if ch:
            await ch.send(
                f"**POST-OPERATION PROCESSING COMPLETE**\n**{selected.get('title')}**\n"
                f"Tracked: **{summary.get('tracked',0)}** • Verified presence: **{summary.get('participated',0)}** • Official operation credit: **{summary.get('credited',0)}**\n"
                f"Weapon rounds applied: **{summary.get('weapon_rounds_applied',0)}** • AAR task: **{'OPEN' if summary.get('aar_task_opened') else 'ON FILE / NOT REQUIRED'}**"
            )

    try:
        await post_battalion_order(interaction.guild, selected, 'end', close_summary=summary)
        announcement_end_sent.add(str(selected.get('id')))
    except Exception as exc:
        log.warning('[ORDER CLOSE NOTICE FAILED] event=%s error=%s', selected.get('id'), exc)

    await interaction.followup.send(
        f"**DUTY PERIOD CLOSED**\n{selected.get('title')}\n"
        f"Soldiers tracked: **{summary.get('tracked', 0)}**\n"
        f"Soldiers credited (threshold met): **{summary.get('credited', 0)}**",
        ephemeral=True,
    )



async def _publish_role_registry(guild: discord.Guild):
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    me=guild.me
    rows=[]
    for role in guild.roles:
        category=_managed_role_category(role.name)
        if not category or role.is_default(): continue
        rows.append({'role_id':role.id,'role_name':role.name,'role_category':category,
                     'canonical_key':_canonical_managed_role_name(role.name) or role.name,
                     'manageable':bool(me and role < me.top_role)})
    await web.request('POST','/internal/clerk/role-registry',json={'guild_id':guild.id,'roles':rows})


async def _dormant_formation_inventory_rows(guild: discord.Guild):
    me=guild.me
    desired_counts={}
    if WEBSITE_BASE_URL and CLERK_SYNC_KEY:
        data=await web.request('GET','/internal/clerk/personnel/canonical-roster',params={'guild_id':guild.id})
        for x in data.get('items',[]):
            unit=str(x.get('unit_code') or '').upper().strip(); pl=str(x.get('platoon') or '').strip(); sq=str(x.get('squad') or '').strip(); letter=unit[:1] if unit[:1] in {'A','B','C'} else None
            if letter and pl:
                key=_normalized_role_name(f"{letter} Company • {pl.title()}"); desired_counts[key]=desired_counts.get(key,0)+1
            if letter and pl and sq:
                key=_normalized_role_name(f"{letter} Company • {pl.title()} • {sq.title()}"); desired_counts[key]=desired_counts.get(key,0)+1
    rows=[]
    for role in guild.roles:
        if not _is_managed_formation_role_name(role.name): continue
        key=_normalized_role_name(role.name); website_count=desired_counts.get(key,0)
        overwrite_refs=[]
        for channel in guild.channels:
            if role in channel.overwrites:
                overwrite_refs.append({'channel_id':channel.id,'name':getattr(channel,'name',str(channel.id)),'type':channel.__class__.__name__})
        manageable=bool(me and role < me.top_role)
        if website_count>0:
            status='ACTIVE'
        elif not manageable:
            status='BLOCKED'
        elif len(role.members)>0:
            status='NEEDS_REVIEW'
        else:
            status='SAFE_TO_ARCHIVE'
        rows.append({'role_id':role.id,'role_name':role.name,'canonical_key':_canonical_managed_role_name(role.name) or role.name,
                     'member_count':len(role.members),'website_assignment_count':website_count,'overwrite_count':len(overwrite_refs),
                     'overwrites':overwrite_refs,'status':status,'manageable':manageable})
    return rows


async def _publish_dormant_formation_inventory(guild: discord.Guild):
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    rows=await _dormant_formation_inventory_rows(guild)
    await web.request('POST','/internal/clerk/dormant-formations/report',json={'guild_id':guild.id,'roles':rows})


async def _archive_dormant_formation_role(guild: discord.Guild, role_id: int):
    me=guild.me
    role=guild.get_role(int(role_id))
    if not role:
        return {'ok':True,'summary':'Role already absent.'}
    if not _is_managed_formation_role_name(role.name):
        return {'ok':False,'blocked':True,'error':'Role is not a managed formation role.'}
    if not me or role >= me.top_role:
        return {'ok':False,'blocked':True,'error':'Role is above Battalion Clerk or cannot be managed.'}
    # Re-read Website authority immediately before destructive work.
    rows=await _dormant_formation_inventory_rows(guild)
    current=next((x for x in rows if int(x['role_id'])==role.id),None)
    if not current:
        return {'ok':False,'blocked':True,'error':'Formation inventory could not be verified.'}
    if current.get('website_assignment_count',0)>0:
        return {'ok':False,'blocked':True,'error':'Website now has active personnel assigned to this formation.'}
    if len(role.members)>0:
        return {'ok':False,'blocked':True,'error':f'Role still has {len(role.members)} Discord member(s); reconcile personnel first.'}
    # Remove every explicit channel/category overwrite before deleting the role. With no Website assignments
    # and no Discord members, this cannot remove access from an active Soldier. Future reactivation will
    # recreate the role and normal channel/permission repair can reapply the canonical overwrite.
    for channel in list(guild.channels):
        if role not in channel.overwrites: continue
        try:
            await channel.set_permissions(role,overwrite=None,reason='Battalion Clerk — archive dormant formation permission overwrite')
        except Exception as exc:
            return {'ok':False,'blocked':True,'error':f'Could not remove overwrite from {getattr(channel,"name",channel.id)}: {exc}'}
    try:
        name=role.name
        await role.delete(reason='Battalion Clerk — archive Website-unassigned dormant formation role')
        return {'ok':True,'summary':f'Archived dormant formation role {name}.'}
    except Exception as exc:
        return {'ok':False,'error':str(exc)[:400]}


@tasks.loop(minutes=1)
async def clerk_heartbeat_watch():
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    for guild in bot.guilds:
        try:
            pending=sum(1 for t in pending_personnel_sync.values() if not t.done())
            await web.request('POST','/internal/clerk/heartbeat',json={
                'component':'BATTALION_CLERK','status':'ONLINE','version':'2026.08.24-authority-control',
                'details':{'guild_id':guild.id,'members':guild.member_count,'pending_personnel_sync':pending,
                           'hll_collector_started':bool(collector_started)}
            })
            await _publish_role_registry(guild)
            await _publish_dormant_formation_inventory(guild)
        except Exception as exc:
            log.warning('[CLERK HEARTBEAT FAILED] guild=%s error=%s',guild.id,exc)


@clerk_heartbeat_watch.before_loop
async def before_clerk_heartbeat_watch():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def dormant_formation_cleanup_watch():
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    for guild in bot.guilds:
        try:
            data=await web.request('GET','/internal/clerk/dormant-formations/cleanup-pending',params={'guild_id':guild.id})
            for item in data.get('items',[]):
                cleanup_id=item.get('id'); role_id=item.get('role_id')
                if not cleanup_id or not role_id: continue
                result=await _archive_dormant_formation_role(guild,int(role_id))
                await web.request('POST',f'/internal/clerk/dormant-formations/cleanup/{cleanup_id}/complete',json={
                    'ok':bool(result.get('ok')),'blocked':bool(result.get('blocked')),'error':result.get('error')})
            await _publish_dormant_formation_inventory(guild)
        except Exception as exc:
            log.warning('[DORMANT FORMATION CLEANUP WATCH FAILED] guild=%s error=%s',guild.id,exc)


@dormant_formation_cleanup_watch.before_loop
async def before_dormant_formation_cleanup_watch():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def canonical_role_sync_watch():
    """Website personnel state is authoritative; process queued role mirrors safely."""
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    for guild in bot.guilds:
        try:
            data=await web.request('GET','/internal/clerk/role-sync/pending',params={'guild_id':guild.id})
            for item in data.get('items',[]):
                qid=item.get('queue_id') or item.get('id'); uid=item.get('discord_user_id')
                if not qid: continue
                ok=False; error=None
                try:
                    member=guild.get_member(int(uid)) if uid else None
                    if not member and uid:
                        try: member=await guild.fetch_member(int(uid))
                        except Exception: member=None
                    if not member:
                        raise RuntimeError('Discord member not found in guild')
                    recon=await reconcile_member_roles_from_canonical(member,item)
                    ok=bool((recon or {}).get('ok',True))
                    error=(recon or {}).get('error')
                    await web.request('POST','/internal/clerk/personnel/sync-observation',json={
                        'personnel_id':str(item.get('personnel_id')),'guild_id':guild.id,'discord_user_id':member.id,
                        'status':'COMPLETE' if ok else 'BLOCKED','expected':(recon or {}).get('expected') or {},
                        'actual_roles':member_role_names(member),'changes':{'added':(recon or {}).get('added',[]),'removed':(recon or {}).get('removed',[]),'created':(recon or {}).get('created',[])},
                        'error':error,'summary':'Discord roles match the authoritative website record.' if ok else 'Discord reconciliation is blocked and requires Command review.'
                    })
                except Exception as exc:
                    error=str(exc)[:400]
                    log.warning('[ROLE SYNC QUEUE ITEM FAILED] queue=%s error=%s',qid,exc)
                    try:
                        await web.request('POST','/internal/clerk/personnel/sync-observation',json={
                            'personnel_id':str(item.get('personnel_id')),'guild_id':guild.id,'discord_user_id':uid,
                            'status':'FAILED','expected':{},'actual_roles':member_role_names(member) if member else [],
                            'changes':{},'error':error,'summary':'Discord reconciliation failed.'})
                    except Exception: pass
                await web.request('POST',f'/internal/clerk/role-sync/{qid}/complete',json={'ok':ok,'error':error})
        except Exception as exc:
            log.warning('[CANONICAL ROLE SYNC WATCH FAILED] guild=%s error=%s',guild.id,exc)

@tasks.loop(minutes=60)
async def member_record_reminder_watch():
    """Low-noise personal reminders for approaching weapon/qualification suspense."""
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    for guild in bot.guilds:
        try:
            data=await web.request('GET','/internal/clerk/member-reminders',params={'guild_id':guild.id})
            for item in data.get('reminders',[]):
                uid=item.get('discord_user_id'); pid=item.get('personnel_id'); key=item.get('reminder_key')
                if not uid or not pid or not key: continue
                if not await _notice_once(guild.id,pid,'MEMBER_RECORD_REMINDER',key): continue
                member=guild.get_member(int(uid))
                if not member: continue
                try: await member.send(item.get('message') or '**1/5 CAV — PERSONNEL NOTICE**\nA Soldier Record action is approaching its suspense date.')
                except discord.Forbidden: pass
        except Exception as exc:
            log.warning('[MEMBER RECORD REMINDER WATCH FAILED] guild=%s error=%s',guild.id,exc)


@tasks.loop(minutes=1)
async def welcome_packet_watch():
    """Deliver Website-authoritative Welcome Packet phase changes through Discord."""
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY:
        return
    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID:
            continue
        try:
            data=await web.request('GET','/internal/clerk/welcome-packet/notifications',params={'guild_id':guild.id})
            for item in data.get('notifications',[]):
                member=guild.get_member(int(item.get('discord_user_id') or 0))
                ok=False; error=None
                if member:
                    message=(f"**1/5 CAV — {item.get('title') or 'WELCOME PACKET'}**\n"
                             f"{item.get('message') or 'Your onboarding record has been updated.'}\n\n"
                             f"Open your packet: {WEBSITE_BASE_URL}/welcome-packet")
                    try:
                        await member.send(message[:1900]); ok=True
                    except discord.Forbidden:
                        error='Discord direct messages are disabled or blocked for this member'
                    except Exception as exc:
                        error=str(exc)[:500]
                else:
                    error='Linked Discord member is not currently available in the guild'
                if ok and str(item.get('event_type') or '').upper()=='COMPLETE':
                    channel=(discord.utils.get(guild.text_channels,name='headquarters-notices')
                             or discord.utils.get(guild.text_channels,name='personnel-orders')
                             or discord.utils.get(guild.text_channels,name='battalion-orders'))
                    if channel:
                        try: await channel.send(f"**S-1 ONBOARDING COMPLETE**\n{member.mention} has completed the 1/5 CAV Welcome Packet and filed Report for Duty."[:1900])
                        except Exception: pass
                await web.request('POST',f"/internal/clerk/welcome-packet/notifications/{item.get('id')}/delivered",json={'ok':ok,'error':error})
        except Exception as exc:
            log.warning('[WELCOME PACKET WATCH FAILED] guild=%s error=%s',guild.id,exc)


@bot.event
async def on_ready():
    # Retire the pre-Welcome-Packet Approved Replacement Discord role. This is
    # idempotent and only touches the explicitly named legacy managed role.
    if not getattr(bot, '_legacy_recruit_role_cleanup_done', False):
        for guild in bot.guilds:
            if GUILD_ID and guild.id != GUILD_ID:
                continue
            try:
                result=await cleanup_legacy_recruiting_status_role(guild)
                if result.get('deleted') or result.get('removed_from'):
                    log.info('[LEGACY RECRUIT ROLE CLEANUP] guild=%s result=%s',guild.id,result)
            except Exception:
                log.exception('[LEGACY RECRUIT ROLE CLEANUP FAILED] guild=%s',guild.id)
        bot._legacy_recruit_role_cleanup_done=True
    if not applicant_intake_watch.is_running():
        applicant_intake_watch.start()
    if not recruit_status_watch.is_running():
        recruit_status_watch.start()
    if not credential_resend_watch.is_running():
        credential_resend_watch.start()
    if not approved_recruit_watch.is_running():
        approved_recruit_watch.start()
    if not welcome_packet_watch.is_running():
        welcome_packet_watch.start()
    global collector_started, commands_synced

    # Persistent Help Desk buttons survive bot restarts.
    if not getattr(bot, '_helpdesk_views_registered', False):
        bot.add_view(HelpDeskPanelView())
        bot.add_view(HelpDeskTicketView())
        bot.add_view(RecruitApplicationStartView())
        bot.add_view(RecruitPart1View())
        bot.add_view(RecruitPart2View())
        bot.add_view(RecruitPart3View())
        bot._helpdesk_views_registered = True

    if not collector_started:
        await collector.start()
        collector_started = True

    # HLL: Vietnam RCON is a read-only telemetry layer. A failed/unconfigured
    # RCON connection must never prevent Discord or website automation startup.
    try:
        await hllv.start()
    except Exception:
        log.exception('[HLLV RCON STARTUP FAILED]')

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
    if not operation_reminder_watch.is_running():
        operation_reminder_watch.start()
    if not operation_maintenance_watch.is_running():
        operation_maintenance_watch.start()
    if not hll_m16_reconcile_watch.is_running():
        hll_m16_reconcile_watch.start()
    if not personnel_orders_watch.is_running():
        personnel_orders_watch.start()
    if not clerk_health_watch.is_running():
        clerk_health_watch.start()
    if not operation_duty_watch.is_running():
        operation_duty_watch.start()
    if not live_activity_credit_watch.is_running():
        live_activity_credit_watch.start()
    if not inactivity_watch.is_running():
        inactivity_watch.start()
    if not promotion_eligibility_watch.is_running():
        promotion_eligibility_watch.start()
    if not personnel_suspense_watch.is_running():
        personnel_suspense_watch.start()
    if not canonical_role_sync_watch.is_running():
        canonical_role_sync_watch.start()
    if not clerk_heartbeat_watch.is_running():
        clerk_heartbeat_watch.start()
    if not dormant_formation_cleanup_watch.is_running():
        dormant_formation_cleanup_watch.start()
    if not member_record_reminder_watch.is_running():
        member_record_reminder_watch.start()

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




@bot.tree.command(name='activity-channel-add', description='Allow a voice channel to count toward Soldier activity.')
@app_commands.describe(channel='Voice channel to count as activity')
async def activity_channel_add(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not await require_manage_guild(interaction): return
    await collector.start()
    await collector.db.execute("INSERT INTO activity_voice_channels(guild_id,channel_id,channel_name,added_by) VALUES($1,$2,$3,$4) ON CONFLICT(guild_id,channel_id) DO UPDATE SET channel_name=EXCLUDED.channel_name,added_by=EXCLUDED.added_by",interaction.guild.id,channel.id,channel.name,interaction.user.id)
    await interaction.response.send_message(f'Activity tracking enabled for {channel.mention}. Ten or more minutes will count as community activity only.',ephemeral=True)

@bot.tree.command(name='activity-channel-remove', description='Stop a voice channel from counting toward Soldier activity.')
@app_commands.describe(channel='Voice channel to stop counting')
async def activity_channel_remove(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not await require_manage_guild(interaction): return
    await collector.start(); await collector.db.execute("DELETE FROM activity_voice_channels WHERE guild_id=$1 AND channel_id=$2",interaction.guild.id,channel.id)
    await interaction.response.send_message(f'Activity tracking disabled for {channel.mention}.',ephemeral=True)

@bot.tree.command(name='activity-channel-status', description='List voice channels that count toward Soldier activity.')
async def activity_channel_status(interaction: discord.Interaction):
    if not await require_manage_guild(interaction): return
    await collector.start(); rows=await collector.db.fetch("SELECT channel_id,channel_name FROM activity_voice_channels WHERE guild_id=$1 ORDER BY channel_name",interaction.guild.id)
    text='\n'.join(f'• <#{r["channel_id"]}> — {r["channel_name"]}' for r in rows) or 'No activity voice channels are configured.'
    await interaction.response.send_message('**QUALIFYING ACTIVITY VOICE CHANNELS**\n'+text+'\n\nActivity voice is attendance evidence only. Issued M16 field service and estimated expenditure come exclusively from verified HLL server telemetry.',ephemeral=True)

# ---------------------------------------------------------------------------
# DISCORD-FIRST RECRUITING APPLICATION
# Uses buttons + modals rather than message-content parsing, so Battalion Clerk
# does not require the privileged Message Content intent. Each modal writes its
# answers to the Website immediately, making the application resumable.
# ---------------------------------------------------------------------------

RECRUIT_PLATFORM_ALIASES={
    'STEAM':'STEAM','PC':'STEAM','STEAM / PC':'STEAM',
    'XBOX':'XBOX','XBOX SERIES X':'XBOX','XBOX SERIES S':'XBOX',
    'PS5':'PS5','PLAYSTATION':'PS5','PLAYSTATION 5':'PS5','PSN':'PS5'
}
active_recruit_interviews=set()


def _recruit_ephemeral(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None


def _recruit_guild_id(user: discord.abc.User) -> int:
    if GUILD_ID: return GUILD_ID
    for guild in bot.guilds:
        if guild.get_member(user.id): return guild.id
    return bot.guilds[0].id if bot.guilds else 0


async def _recruit_save(user, step:int, answers:dict):
    gid=_recruit_guild_id(user)
    if not gid: raise RuntimeError('Battalion Discord guild is unavailable')
    return await web.request('POST','/internal/clerk/recruiting/intake/save',json={
        'guild_id':gid,'discord_user_id':user.id,'current_step':step,'answers':answers
    })


class RecruitBasicsModal(discord.ui.Modal, title='1/5 CAV Application — Part 1 of 3'):
    age=discord.ui.TextInput(label='Age (optional)',required=False,max_length=2,placeholder='Leave blank if you prefer')
    timezone_name=discord.ui.TextInput(label='Time zone',max_length=60,placeholder='Example: Eastern / EDT')
    game_platform=discord.ui.TextInput(label='Platform',max_length=30,placeholder='STEAM, XBOX, or PS5')
    game_identity=discord.ui.TextInput(label='SteamID64 / Gamertag / PSN ID',max_length=100,placeholder='Steam must be the 17-digit SteamID64')
    hll_experience=discord.ui.TextInput(label='HLL / HLL: Vietnam experience',max_length=80,placeholder='New / Some / Experienced / Very experienced')

    async def on_submit(self, interaction:discord.Interaction):
        platform=RECRUIT_PLATFORM_ALIASES.get(str(self.game_platform.value).strip().upper())
        identity=str(self.game_identity.value).strip()
        if not platform:
            await interaction.response.send_message('Platform must be **STEAM**, **XBOX**, or **PS5**. Press Part 1 and try again.',ephemeral=_recruit_ephemeral(interaction)); return
        if platform=='STEAM' and not (identity.isdigit() and len(identity)==17):
            await interaction.response.send_message('Steam players must enter a **17-digit SteamID64**. Press Part 1 and try again.',ephemeral=_recruit_ephemeral(interaction)); return
        age=str(self.age.value).strip()
        if age and not age.isdigit():
            await interaction.response.send_message('Age must be a number or left blank. Press Part 1 and try again.',ephemeral=_recruit_ephemeral(interaction)); return
        try:
            await _recruit_save(interaction.user,2,{
                'age':age,'timezone_name':str(self.timezone_name.value).strip(),'game_platform':platform,
                'game_identity':identity,'hll_experience':str(self.hll_experience.value).strip()
            })
            await interaction.response.send_message('**PART 1 FILED.** Continue with duty preferences.',view=RecruitPart2View())
        except Exception as exc:
            await interaction.response.send_message(f'Could not save your application: {str(exc)[:300]}',ephemeral=_recruit_ephemeral(interaction))


class RecruitPreferencesModal(discord.ui.Modal, title='1/5 CAV Application — Part 2 of 3'):
    role_interest=discord.ui.TextInput(label='Preferred duty / role',max_length=100,placeholder='Example: Rifleman / Infantry')
    looking_for=discord.ui.TextInput(label='Why do you want assignment to 1/5 CAV?',style=discord.TextStyle.paragraph,max_length=1000)
    play_style=discord.ui.TextInput(label='Preferred style of play',max_length=100,placeholder='Casual organized / Milsim / Competitive / Mixed')
    follows_chain=discord.ui.TextInput(label='Will you follow the chain of command?',max_length=5,placeholder='YES or NO')
    participation=discord.ui.TextInput(label='Typical participation',max_length=100,placeholder='Example: Multiple times per week')

    async def on_submit(self, interaction:discord.Interaction):
        chain=str(self.follows_chain.value).strip().upper()
        if chain not in {'YES','Y','NO','N'}:
            await interaction.response.send_message('Chain of command answer must be **YES** or **NO**. Press Part 2 and try again.',ephemeral=_recruit_ephemeral(interaction)); return
        try:
            await _recruit_save(interaction.user,3,{
                'role_interest':str(self.role_interest.value).strip(),'looking_for':str(self.looking_for.value).strip(),
                'play_style':str(self.play_style.value).strip(),'follows_chain':'YES' if chain in {'YES','Y'} else 'NO',
                'participation':str(self.participation.value).strip()
            })
            await interaction.response.send_message('**PART 2 FILED.** One final section remains.',view=RecruitPart3View())
        except Exception as exc:
            await interaction.response.send_message(f'Could not save your application: {str(exc)[:300]}',ephemeral=_recruit_ephemeral(interaction))


class RecruitFinalModal(discord.ui.Modal, title='1/5 CAV Application — Part 3 of 3'):
    recruited_by=discord.ui.TextInput(label='Recruited by active member?',required=False,max_length=100,placeholder='Name / Discord mention, or NONE')
    applicant_notes=discord.ui.TextInput(label='Additional information for HQ',required=False,style=discord.TextStyle.paragraph,max_length=1000,placeholder='Optional')

    async def on_submit(self, interaction:discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            await _recruit_save(interaction.user,4,{
                'recruited_by':str(self.recruited_by.value).strip() or 'NONE',
                'applicant_notes':str(self.applicant_notes.value).strip()
            })
            gid=_recruit_guild_id(interaction.user)
            result=await web.request('POST','/internal/clerk/recruiting/intake/submit',json={'guild_id':gid,'discord_user_id':interaction.user.id})
            case=result.get('case') or {}
            if result.get('existing_case'):
                text=f"**APPLICATION ALREADY ON FILE — {case.get('case_number','RECRUITING CASE')}**\nStatus: **{str(case.get('status') or '').replace('_',' ')}**"
            else:
                text=(f"**1/5 CAV — APPLICATION FILED**\nRecruiting Case **{case.get('case_number')}** has been forwarded to Battalion Headquarters.\n"
                      f"Status: **AWAITING COMMAND REVIEW**\n\nYou do **not** need to submit another application on the website.")
                if result.get('status_url'): text+=f"\nCase status: {result['status_url']}"
            await interaction.followup.send(text)
            try:
                guild=bot.get_guild(gid); member=guild.get_member(interaction.user.id) if guild else None
                if member: await ensure_recruit_status_role(member,approved=False)
            except Exception as exc:
                log.warning('[DISCORD APPLICATION ROLE FAILED] user=%s error=%s',interaction.user.id,exc)
        except Exception as exc:
            await interaction.followup.send(f'**APPLICATION NOT FILED**\n{str(exc)[:500]}\nYour completed sections were saved. Press **Begin / Resume Application** again to retry.')


class RecruitPart1View(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='PART 1 — IDENTITY & EXPERIENCE',style=discord.ButtonStyle.primary,custom_id='recruit_apply_part1')
    async def part1(self,interaction:discord.Interaction,button:discord.ui.Button):
        await interaction.response.send_modal(RecruitBasicsModal())


class RecruitPart2View(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='PART 2 — DUTY PREFERENCES',style=discord.ButtonStyle.primary,custom_id='recruit_apply_part2')
    async def part2(self,interaction:discord.Interaction,button:discord.ui.Button):
        await interaction.response.send_modal(RecruitPreferencesModal())


class RecruitPart3View(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='PART 3 — FINAL & SUBMIT',style=discord.ButtonStyle.success,custom_id='recruit_apply_part3')
    async def part3(self,interaction:discord.Interaction,button:discord.ui.Button):
        await interaction.response.send_modal(RecruitFinalModal())


async def _begin_or_resume_recruit_application(interaction:discord.Interaction):
    user=interaction.user; gid=_recruit_guild_id(user)
    if not gid:
        await interaction.response.send_message('The battalion Discord server is unavailable right now. Please try again later.',ephemeral=_recruit_ephemeral(interaction)); return
    try:
        data=await web.request('POST','/internal/clerk/recruiting/intake/start',json={
            'guild_id':gid,'discord_user_id':user.id,'username':getattr(user,'name',str(user)),
            'display_name':getattr(user,'display_name',getattr(user,'name',str(user)))
        })
    except Exception as exc:
        await interaction.response.send_message(f'Recruiting intake is temporarily unavailable: {str(exc)[:300]}',ephemeral=_recruit_ephemeral(interaction)); return
    if data.get('existing_member'):
        await interaction.response.send_message('Your Discord account is already linked to an active 1/5 Cavalry Soldier Record. No application is required.',ephemeral=_recruit_ephemeral(interaction)); return
    if data.get('existing_case'):
        case=data.get('case') or {}; url=f"{WEBSITE_BASE_URL}/recruiting/status/{case.get('public_token')}" if WEBSITE_BASE_URL and case.get('public_token') else None
        text=f"**APPLICATION ALREADY ON FILE — {case.get('case_number')}**\nStatus: **{str(case.get('status') or '').replace('_',' ')}**"
        if url: text+=f"\n{url}"
        await interaction.response.send_message(text,ephemeral=_recruit_ephemeral(interaction)); return
    draft=data.get('draft') or {}; step=max(1,min(3,int(draft.get('current_step') or 1)))
    views={1:RecruitPart1View,2:RecruitPart2View,3:RecruitPart3View}
    await interaction.response.send_message(
        f"**1/5 CAV — RECRUITING OFFICE**\nYour application is {'ready to resume' if draft.get('answers') else 'ready to begin'}. "
        f"Complete Part **{step} of 3** below. Each completed section is saved automatically.",view=views[step](),ephemeral=_recruit_ephemeral(interaction))


class RecruitExistingApplicationModal(discord.ui.Modal, title='Link Existing 1/5 CAV Application'):
    case_number=discord.ui.TextInput(label='Application / Case Number',max_length=40,placeholder='Example: RC-...')
    verification_code=discord.ui.TextInput(label='Verification Code',max_length=40,placeholder='Code shown on your website application receipt')

    async def on_submit(self, interaction:discord.Interaction):
        await interaction.response.defer(thinking=True,ephemeral=_recruit_ephemeral(interaction))
        gid=_recruit_guild_id(interaction.user)
        try:
            result=await web.request('POST','/internal/clerk/recruiting/intake/connect-existing',json={
                'guild_id':gid,'discord_user_id':interaction.user.id,'username':getattr(interaction.user,'name',str(interaction.user)),
                'case_number':str(self.case_number.value).strip(),'verification_code':str(self.verification_code.value).strip()
            })
            case=result.get('case') or {}
            url=f"{WEBSITE_BASE_URL}/recruiting/status/{case.get('public_token')}" if WEBSITE_BASE_URL and case.get('public_token') else None
            text=f"**APPLICATION LOCATED — {case.get('case_number')}**\nYour Discord account is now attached to the existing Recruiting Case.\nStatus: **{str(case.get('status') or '').replace('_',' ')}**"
            if url: text+=f"\n{url}"
            await interaction.followup.send(text,ephemeral=_recruit_ephemeral(interaction))
            guild=bot.get_guild(gid); member=guild.get_member(interaction.user.id) if guild else None
            if member: await ensure_recruit_status_role(member,approved=False)
        except Exception as exc:
            await interaction.followup.send(f"**APPLICATION COULD NOT BE LINKED**\n{str(exc)[:500]}\nCheck the case number and verification code from your website application receipt.",ephemeral=_recruit_ephemeral(interaction))


class RecruitApplicationStartView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='BEGIN APPLICATION',style=discord.ButtonStyle.success,custom_id='recruit_apply_start')
    async def start_application(self,interaction:discord.Interaction,button:discord.ui.Button):
        await _begin_or_resume_recruit_application(interaction)

    @discord.ui.button(label='I ALREADY APPLIED',style=discord.ButtonStyle.secondary,custom_id='recruit_apply_existing')
    async def existing_application(self,interaction:discord.Interaction,button:discord.ui.Button):
        await interaction.response.send_modal(RecruitExistingApplicationModal())


@bot.tree.command(name='apply',description='Begin or resume your 1/5 Cavalry enlistment application in Discord.')
async def discord_apply(interaction:discord.Interaction):
    await _begin_or_resume_recruit_application(interaction)


@bot.tree.command(name="application-status", description="Show the recruiting case linked to your Discord account.")
async def application_status(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Run this command inside the 1/5 Cav Discord server.",ephemeral=True); return
    data=await recruiting_status_for(interaction.user)
    if not data.get('exists'):
        try:
            draft=await web.request('GET','/internal/clerk/recruiting/intake/status',params={'guild_id':interaction.guild.id,'discord_user_id':interaction.user.id})
        except Exception:
            draft={}
        if draft.get('exists') and draft.get('draft'):
            step=max(1,min(3,int((draft.get('draft') or {}).get('current_step') or 1)))
            await interaction.response.send_message(f"**APPLICATION DRAFT IN PROGRESS**\nResume at Part **{step} of 3** with **/apply**.",ephemeral=True); return
        app_url=f"{WEBSITE_BASE_URL}/recruiting" if WEBSITE_BASE_URL else "the battalion website"
        await interaction.response.send_message(f"No Recruiting Case is linked to your Discord account. Use **/apply** here in Discord or apply at {app_url}",ephemeral=True); return
    case=data.get('case') or {}
    status=str(case.get('status') or '').upper()
    if status in {'REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'}:
        next_step=f"**NEXT STEP:** Report for Duty — {WEBSITE_BASE_URL}/report-for-duty" if WEBSITE_BASE_URL else '**NEXT STEP:** Use Member Access and Report for Duty.'
    elif status=='ENLISTED':
        next_step='**NEXT STEP:** Open your normal Member Dashboard.'
    elif status=='MORE_INFO_REQUIRED':
        next_step='**NEXT STEP:** Respond to the Headquarters request on your Recruiting Case status page.'
    else:
        next_step='**NEXT STEP:** No action required. Battalion Clerk will notify you when Command acts.'
    await interaction.response.send_message(f"**{case.get('case_number')}**\nStatus: **{status.replace('_',' ')}**\n{next_step}",ephemeral=True)

async def _fetch_guild_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    member=guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def auto_join_approved_recruit(guild: discord.Guild, case: dict) -> Optional[discord.Member]:
    user_id=int(case.get('discord_user_id') or 0)
    if not user_id:
        return None
    existing=await _fetch_guild_member(guild,user_id)
    if existing:
        try:
            await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/join-status",json={'joined':True,'guild_id':guild.id})
        except Exception:
            pass
        return existing
    try:
        auth=await web.request('GET',f"/internal/clerk/recruiting/{case.get('id')}/join-authorization")
        access_token=auth.get('access_token')
        if not access_token:
            raise RuntimeError('Website did not return a Discord guilds.join authorization token')
        await web.start()
        headers={'Authorization':f'Bot {TOKEN}','Content-Type':'application/json','User-Agent':'1-5-Cav-Battalion-Clerk/1.0'}
        payload={'access_token':access_token}
        url=f'https://discord.com/api/v10/guilds/{guild.id}/members/{user_id}'
        async with web.session.put(url,json=payload,headers=headers) as resp:
            body=await resp.text()
            if resp.status not in {201,204}:
                raise RuntimeError(f'Discord add-member HTTP {resp.status}: {body[:300]}')
        await asyncio.sleep(1)
        member=await _fetch_guild_member(guild,user_id)
        if not member:
            raise RuntimeError('Discord accepted the join but the member could not be loaded yet')
        await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/join-status",json={'joined':True,'guild_id':guild.id})
        log.info('[RECRUIT AUTO-JOINED] case=%s member=%s guild=%s',case.get('case_number'),user_id,guild.id)
        return member
    except Exception as exc:
        log.warning('[RECRUIT AUTO-JOIN FAILED] case=%s user=%s error=%s',case.get('case_number'),user_id,exc)
        try:
            await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/join-status",json={'joined':False,'guild_id':guild.id,'error':str(exc)[:500]})
        except Exception:
            pass
        return None


def build_recruit_credentials_message(case: dict, provision: dict, *, site: str | None = None) -> str:
    """Build the exact approval/credential DM used for live delivery and Command preview."""
    roster=provision.get('roster_number') or ((provision.get('roster') or {}).get('roster_number') if isinstance(provision.get('roster'),dict) else None)
    field_code=provision.get('field_code')
    weapon_line=f"\nIssued M16: **{provision.get('weapon_serial')}**" if provision.get('weapon_serial') else "\nIssued M16: **Pending S-4 issue**"
    site=site or WEBSITE_BASE_URL or 'the battalion website'
    return (
        "**APPLICATION APPROVED — REPORT FOR DUTY**\n"
        f"Recruiting Case **{case.get('case_number')}** has been approved by Battalion Headquarters. Your Soldier Record is open and you are attached to **Replacement Detachment** while you complete your Welcome Packet. Permanent Company / Platoon assignment follows after Command accepts that packet.\n\n"
        "**WEBSITE ACCESS**\n"
        f"Website: {site}\n"
        f"Battle Roster Number: **{roster}**\n"
        f"Field Code: **{field_code}**" + weapon_line + "\n\n"
        "Keep these credentials private. Use **Member Access** on the website once, then follow the single next-step screen.\n\n"
        f"**REPORT FOR DUTY**\n{site}/report-for-duty\n"
        "That page shows your account verification, Welcome Packet progress, and permanent assignment status in one place. Discord/game identity verification happens automatically whenever possible."
    )


async def deliver_recruit_credentials(member: discord.Member, case: dict, provision: dict) -> bool:
    resend_pending=bool(case.get('credentials_resend_pending') or provision.get('credentials_resend_pending'))
    if (case.get('credentials_sent_at') and not resend_pending) or provision.get('credentials_already_sent'):
        return True
    roster=provision.get('roster_number') or ((provision.get('roster') or {}).get('roster_number') if isinstance(provision.get('roster'),dict) else None)
    field_code=provision.get('field_code')
    if not roster or not field_code:
        err='Provisioning completed but plaintext Battle Roster credentials were not available for delivery'
        log.warning('[RECRUIT CREDENTIAL HOLD] case=%s %s',case.get('case_number'),err)
        await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/credentials-status",json={'sent':False,'error':err})
        return False
    message=build_recruit_credentials_message(case,provision)
    if resend_pending:
        message=("**BATTALION HEADQUARTERS — LOGIN INFORMATION REISSUED**\n"
                 "Command requested another copy of your Website login information. Use the credentials below; if your Field Code was rotated, the older code is no longer valid.\n\n" + message)
    try:
        await member.send(message)
        await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/credentials-status",json={'sent':True})
        log.info('[RECRUIT CREDENTIALS DELIVERED] case=%s member=%s',case.get('case_number'),member.id)
        return True
    except discord.Forbidden:
        err='Discord direct messages are disabled or blocked for this member'
    except Exception as exc:
        err=str(exc)[:500]
    log.warning('[RECRUIT CREDENTIAL DM FAILED] case=%s member=%s error=%s',case.get('case_number'),member.id,err)
    try:
        await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/credentials-status",json={'sent':False,'error':err})
    except Exception:
        pass
    return False


@tasks.loop(seconds=30)
async def applicant_intake_watch():
    """Admit website applicants to Discord before Command approval.

    This is communications-only intake. The applicant receives no Soldier record, rank,
    MOS, formation, Replacement status, or member credentials until Command approves.
    Status messaging is left to on_member_join/recruit_status_watch so it remains idempotent.
    """
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID:
            continue
        try:
            data=await web.request('GET','/internal/clerk/recruiting/pending-entry',params={'guild_id':guild.id})
            for case in data.get('cases',[]): 
                member=await auto_join_approved_recruit(guild,case)
                if not member:
                    continue
                await collector.upsert_member(member)
                await ensure_recruit_status_role(member,approved=False)
                log.info('[APPLICANT INTAKE COMPLETE] case=%s member=%s guild=%s',case.get('case_number'),member.id,guild.id)
        except Exception as exc:
            log.warning('[APPLICANT INTAKE WATCH FAILED] guild=%s error=%s',guild.id,exc)


@tasks.loop(minutes=1)
async def recruit_status_watch():
    """Deliver Recruiting Case status changes to verified Discord identities."""
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    for guild in bot.guilds:
        try:
            data=await web.request('GET','/internal/clerk/recruiting/notifications',params={'guild_id':guild.id})
            for case in data.get('cases',[]):
                member=guild.get_member(int(case.get('discord_user_id') or 0))
                if not member:
                    # Do not mark it delivered. The notification will be waiting when the user joins.
                    continue
                status=str(case.get('status') or '').upper()
                if status in {'REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'}:
                    # The deterministic approval pipeline owns the approval + credential DM.
                    # Do not send a second generic approval message here.
                    continue
                elif status == 'ENLISTED':
                    await clear_recruit_status_roles(member)
                    message=(f"**REPLACEMENT DETACHMENT — RELEASED TO UNIT**\n"
                             f"Recruiting Case **{case.get('case_number')}** is complete. S-1 has released you to your permanent formation; your website personnel record is now the authoritative source for Discord unit roles.")
                elif status == 'MORE_INFO_REQUIRED':
                    await ensure_recruit_status_role(member,approved=False)
                    status_url=f"{WEBSITE_BASE_URL}/recruiting/status/{case.get('public_token')}"
                    message=(f"**BATTALION HEADQUARTERS — MORE INFORMATION REQUIRED**\n"
                             f"Recruiting Case **{case.get('case_number')}** requires additional information before review can continue.\n\n"
                             f"Respond here: {status_url}")
                elif status == 'DENIED':
                    await clear_recruit_status_roles(member)
                    message=(f"**1/5 CAV RECRUITING CASE CLOSED**\n"
                             f"Recruiting Case **{case.get('case_number')}** was not approved at this time.")
                else:
                    await ensure_recruit_status_role(member,approved=False)
                    message=(f"**APPLICATION RECEIVED — 1/5 CAV**\n"
                             f"Recruiting Case **{case.get('case_number')}** is linked to this Discord account and is awaiting Battalion Headquarters review.\n\n"
                             "No verification code is required. Battalion Clerk will notify you here when your case changes.")
                try:
                    await member.send(message)
                except discord.Forbidden:
                    log.warning('[RECRUIT STATUS DM BLOCKED] member=%s status=%s',member.id,status)
                await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/status-notified",json={'status':status,'guild_id':guild.id})
        except Exception as exc:
            log.warning('[RECRUIT STATUS WATCH FAILED] guild=%s error=%s',guild.id,exc)


async def process_approved_recruit_case(guild: discord.Guild, case: dict, member: discord.Member | None = None) -> bool:
    """Complete one approved recruit accession and deliver login credentials once.

    This is shared by the fast approval watcher and on_member_join so a recruit
    who is already in Discord receives credentials within seconds of approval,
    while a recruit who joins after approval receives them during the join event.
    """
    member=member or await auto_join_approved_recruit(guild,case)
    if not member:
        return False
    await collector.upsert_member(member)
    await ensure_recruit_status_role(member,approved=True)
    try:
        provision=await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/provision",json={
            'guild_id':guild.id,'discord_user_id':member.id,'username':member.name,'display_name':member.display_name,'ensure_credentials':True
        })
    except Exception as exc:
        log.warning('[APPROVED REPLACEMENT PROVISION FAILED] member=%s case=%s error=%s',member.id,case.get('case_number'),exc)
        return False
    delivered=await deliver_recruit_credentials(member,case,provision)
    if delivered:
        try:
            await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/status-notified",json={'status':'APPROVED_AWAITING_PROCESSING','guild_id':guild.id})
        except Exception as exc:
            log.warning('[APPROVAL STATUS NOTIFY FILE FAILED] case=%s error=%s',case.get('case_number'),exc)
    return delivered


@tasks.loop(seconds=10)
async def credential_resend_watch():
    """Staff-requested credential recovery queue; works for Replacement and already-enlisted Soldiers."""
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID:
            continue
        try:
            data=await web.request('GET','/internal/clerk/recruiting/credential-resends',params={'guild_id':guild.id})
            for case in data.get('cases',[]):
                member=guild.get_member(int(case.get('discord_user_id') or 0))
                if not member:
                    log.warning('[CREDENTIAL RESEND HOLD] case=%s Discord member not present',case.get('case_number'))
                    continue
                try:
                    provision=await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/resend-credentials",json={'guild_id':guild.id,'discord_user_id':member.id})
                except Exception as exc:
                    log.warning('[CREDENTIAL RESEND PREP FAILED] case=%s error=%s',case.get('case_number'),exc)
                    continue
                await deliver_recruit_credentials(member,case,provision)
        except Exception as exc:
            log.warning('[CREDENTIAL RESEND WATCH FAILED] guild=%s error=%s',guild.id,exc)


@tasks.loop(seconds=10)
async def approved_recruit_watch():
    """Near-immediate approval pipeline: join -> role -> provision -> credential DM."""
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID:
            continue
        try:
            data=await web.request('GET','/internal/clerk/recruiting/approved-pending',params={'guild_id':guild.id})
            for case in data.get('cases',[]):
                await process_approved_recruit_case(guild,case)
        except Exception as exc:
            log.warning('[APPROVED RECRUIT WATCH FAILED] guild=%s error=%s',guild.id,exc)


@bot.event
async def on_member_join(member: discord.Member):
    if GUILD_ID and member.guild.id != GUILD_ID:
        return
    await collector.upsert_member(member)
    if not member.bot:
        # Public reception notice is independent of recruiting status and is posted once on guild join.
        await post_public_welcome(member)
        existing=await sync_personnel_identity(member,create_if_missing=False,reason="member_join")
        if not (existing and existing.get('linked')):
            recruit=await recruiting_status_for(member)
            case=recruit.get('case') if recruit and recruit.get('exists') else None
            status=str((case or {}).get('status') or '').upper()
            approved=bool(case and status in {'REPLACEMENT_DEPOT','APPROVED_AWAITING_PROCESSING'})
            if status in {'DENIED','CLOSED','ENLISTED'}:
                await clear_recruit_status_roles(member)
            else:
                # Normal recruiting intake: no operational/unit access yet.
                # Pending applicants receive Applicant — Awaiting Review; approval then moves them into the Replacement Detachment workflow.
                await ensure_recruit_status_role(member,approved=approved)
            try:
                if case:
                    if approved:
                        # Resume the approved accession immediately on guild join rather than
                        # waiting for the periodic watcher. Delivery remains idempotent via
                        # credentials_sent_at on the Recruiting Case.
                        await process_approved_recruit_case(member.guild,case,member=member)
                        msg=None
                    elif status=='MORE_INFO_REQUIRED':
                        status_url=f"{WEBSITE_BASE_URL}/recruiting/status/{case.get('public_token')}" if WEBSITE_BASE_URL else "your Recruiting Case status page"
                        msg=f"**1/5 CAV — BATTALION HEADQUARTERS**\nYour verified application requires more information. Respond at: {status_url}"
                    elif status in {'DENIED','CLOSED'}:
                        msg=f"**1/5 CAV — RECRUITING CASE CLOSED**\nRecruiting Case **{case.get('case_number')}** is closed. No battalion recruiting role has been assigned."
                    elif status=='ENLISTED':
                        msg=f"**1/5 CAV — PERSONNEL FILE LOCATED**\nRecruiting Case **{case.get('case_number')}** has already been converted to battalion personnel."
                    else:
                        msg=(f"**1/5 CAV — APPLICATION LOCATED**\nRecruiting Case **{case.get('case_number')}** is linked to this Discord account. "
                             "You now hold **Applicant — Awaiting Review** while Battalion Headquarters reviews your application. No verification code is required. You do not need to keep checking the website; Battalion Clerk will DM you when Command acts on your case.")
                else:
                    app_url=f"{WEBSITE_BASE_URL}/recruiting" if WEBSITE_BASE_URL else "the battalion website — Enlist page"
                    msg=("**WELCOME TO THE 1/5 CAVALRY RECRUITING OFFICE**\n"
                         "If you're here to enlist, Battalion Clerk can process the application privately in Discord.\n\n"
                         "Choose **BEGIN APPLICATION** below, or choose **I ALREADY APPLIED** to attach this Discord account to an application you already filed on the website.\n\n"
                         f"Website option: {app_url}")
                if msg:
                    if not case:
                        await member.send(msg,view=RecruitApplicationStartView())
                    else:
                        await member.send(msg)
                if msg and case and status not in {'ENLISTED'}:
                    await web.request('POST',f"/internal/clerk/recruiting/{case.get('id')}/status-notified",json={'status':status,'guild_id':member.guild.id})
            except discord.Forbidden:
                log.warning('[RECRUIT DM BLOCKED] member=%s',member.id)
                # Safe public fallback: never ask application questions in-channel; only
                # tell the recruit how to open the private Discord application themselves.
                try:
                    welcome=await get_welcome_channel(member.guild)
                    if welcome:
                        await welcome.send(
                            f"{member.mention} Battalion Clerk could not open a private DM. Use **/apply** in this server to begin the private enlistment application, or enable server-member DMs and run **/apply**.",
                            allowed_mentions=discord.AllowedMentions(users=True,roles=False,everyone=False),
                        )
                except Exception as fallback_exc:
                    log.warning('[RECRUIT DM FALLBACK FAILED] member=%s error=%s',member.id,fallback_exc)
            except Exception as exc:
                log.warning('[RECRUIT JOIN STATUS FILE FAILED] member=%s error=%s',member.id,exc)
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
            if (after.guild.id,after.id) in role_sync_suppressed_members:
                log.info('[ROLE SYNC SUPPRESSED] member=%s cleanup maintenance in progress',after.id)
            else:
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



# ---------------------------------------------------------------------------
# AUTOMATION EXPANSION — routed requests, inactivity, promotions, post-op
# ---------------------------------------------------------------------------
REQUEST_CATEGORY_SECTION = {
    'PERSONNEL':'S-1','TRAINING':'S-3','SUPPLY':'S-4','LEADERSHIP':'HQ','TECHNICAL':'HQ'
}
NCO_RANK_PRIORITY = {'CPL':1,'SGT':2,'SSG':3,'SFC':4,'MSG':5,'1SG':6,'SGM':7}

async def set_report_channel(guild_id:int, report_type:str, channel_id:int):
    await ensure_clerk_settings_table(); db=getattr(collector,'db',None)
    if not db or not getattr(db,'pool',None): raise RuntimeError('PostgreSQL is not available.')
    await db.execute("""INSERT INTO clerk_report_channels(guild_id,report_type,channel_id,updated_at) VALUES($1,$2,$3,NOW())
        ON CONFLICT(guild_id,report_type) DO UPDATE SET channel_id=EXCLUDED.channel_id,updated_at=NOW()""",str(guild_id),report_type.upper(),str(channel_id))

async def get_report_channel(guild:discord.Guild, report_type:str):
    await ensure_clerk_settings_table(); db=getattr(collector,'db',None)
    if not db or not getattr(db,'pool',None): return None
    async with db.pool.acquire() as conn:
        v=await conn.fetchval("SELECT channel_id FROM clerk_report_channels WHERE guild_id=$1 AND report_type=$2",str(guild.id),report_type.upper())
    ch=guild.get_channel(int(v)) if v else None
    return ch if isinstance(ch,discord.TextChannel) else None

async def linked_personnel_for_discord(guild_id:int,user_id:int):
    await collector.start(); db=collector.db
    return await db.fetchrow("""SELECT p.*,w.discord_user_id FROM personnel p JOIN website_member_links w ON w.personnel_id=p.id::text
        WHERE w.guild_id::text=$1 AND w.discord_user_id::text=$2""",str(guild_id),str(user_id))

REQUEST_CHOICES=[app_commands.Choice(name=x.title(),value=x) for x in REQUEST_CATEGORY_SECTION]

@bot.tree.command(name='request', description='Submit a routed battalion help or support request.')
@app_commands.describe(category='Office that should receive the request',subject='Short description',details='What you need help with')
@app_commands.choices(category=REQUEST_CHOICES)
async def member_request(interaction:discord.Interaction, category:app_commands.Choice[str], subject:str, details:str):
    if not interaction.guild: return
    await interaction.response.defer(ephemeral=True)
    person=await linked_personnel_for_discord(interaction.guild_id,interaction.user.id)
    if not person:
        await interaction.followup.send('No active Soldier Record is linked to your Discord account. Contact S-1.',ephemeral=True); return
    section=REQUEST_CATEGORY_SECTION[category.value]; db=collector.db
    row=await db.fetchrow("""INSERT INTO personnel_actions(personnel_id,action_type,subject,owning_section,status,priority,initiated_by,due_date,details_json)
        VALUES($1::uuid,$2,$3,$4,'OPEN','ROUTINE',$5,CURRENT_DATE + CASE WHEN $4='S-1' THEN 3 ELSE 5 END,$6::jsonb) RETURNING id""",str(person['id']),category.value,subject.strip(),section,f"{person.get('rank_code') or ''} {person.get('last_name') or ''}".strip(),__import__('json').dumps({'details':details.strip(),'source':'DISCORD /request','discord_user_id':str(interaction.user.id)}))
    ch=await get_report_channel(interaction.guild,f'REQUEST_{category.value}')
    if ch:
        await ch.send(f"**NEW {category.value} REQUEST — {section}**\n**{person.get('rank_code') or ''} {person.get('first_name') or ''} {person.get('last_name') or ''}**\n**Subject:** {subject[:180]}\n{details[:1200]}\nAction: `{row['id']}`")
    await interaction.followup.send(f'Your request has been filed with **{section}**. Action number: `{row["id"]}`',ephemeral=True)

@bot.tree.command(name='request-channel', description='Assign a staff channel for routed member requests.')
@app_commands.choices(category=REQUEST_CHOICES)
async def request_channel(interaction:discord.Interaction, category:app_commands.Choice[str], channel:discord.TextChannel):
    if not await require_manage_guild(interaction): return
    await set_report_channel(interaction.guild_id,f'REQUEST_{category.value}',channel.id)
    await interaction.response.send_message(f'**{category.name} requests** will be posted to {channel.mention}.',ephemeral=True)

@bot.tree.command(name='request-channel-status', description='Show configured request-routing channels.')
async def request_channel_status(interaction:discord.Interaction):
    if not await require_manage_guild(interaction): return
    lines=[]
    for cat in REQUEST_CATEGORY_SECTION:
        ch=await get_report_channel(interaction.guild,f'REQUEST_{cat}')
        lines.append(f'**{cat.title()}** — {ch.mention if ch else "NOT ASSIGNED"}')
    await interaction.response.send_message('\n'.join(lines),ephemeral=True)

# ---------------------------------------------------------------------------
# BATTALION HELP DESK — private Discord tickets + website personnel actions
# ---------------------------------------------------------------------------
HELPDESK_STAFF_ROLES = {
    'PERSONNEL': ('S-1 Personnel', 'Command Staff'),
    'TRAINING': ('S-3 Operations', 'Command Staff'),
    'SUPPLY': ('S-4 Supply', 'Command Staff'),
    'LEADERSHIP': ('Command Staff',),
    'TECHNICAL': ('Command Staff',),
}
HELPDESK_CATEGORY_NAME = 'BATTALION HELP DESK'

async def ensure_helpdesk_table():
    await collector.start(); db=collector.db
    if not db or not getattr(db,'pool',None):
        raise RuntimeError('PostgreSQL is not available.')
    await db.execute("""CREATE TABLE IF NOT EXISTS clerk_helpdesk_tickets (
        ticket_id BIGSERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        channel_id TEXT UNIQUE,
        discord_user_id TEXT NOT NULL,
        category TEXT NOT NULL,
        subject TEXT NOT NULL,
        personnel_action_id TEXT,
        claimed_by TEXT,
        status TEXT NOT NULL DEFAULT 'OPEN',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        closed_at TIMESTAMPTZ,
        closed_by TEXT
    )""")


def _ticket_slug(member: discord.Member, ticket_id: int) -> str:
    base = re.sub(r'[^a-z0-9]+', '-', member.display_name.lower()).strip('-') or 'soldier'
    return f'ticket-{ticket_id:04d}-{base}'[:95]


def _member_has_helpdesk_staff_role(member: discord.Member, category: str | None = None) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild or member.guild_permissions.manage_channels:
        return True
    allowed = set(HELPDESK_STAFF_ROLES.get(category or '', ('Command Staff',)))
    allowed.add('Command Staff')
    return any(role.name in allowed for role in member.roles)


async def _helpdesk_category(guild: discord.Guild) -> discord.CategoryChannel:
    existing = discord.utils.get(guild.categories, name=HELPDESK_CATEGORY_NAME)
    if existing:
        return existing
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    me = guild.me
    if me:
        overwrites[me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
    return await guild.create_category(HELPDESK_CATEGORY_NAME, overwrites=overwrites, reason='Battalion Clerk help desk setup')


async def create_helpdesk_ticket(interaction: discord.Interaction, category: str, subject: str, details: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send('Help Desk tickets can only be opened inside the battalion server.', ephemeral=True)
        return
    await ensure_helpdesk_table(); db=collector.db
    guild=interaction.guild; member=interaction.user

    existing=await db.fetchrow("""SELECT ticket_id,channel_id FROM clerk_helpdesk_tickets
        WHERE guild_id=$1 AND discord_user_id=$2 AND category=$3 AND status='OPEN'
        ORDER BY created_at DESC LIMIT 1""", str(guild.id), str(member.id), category)
    if existing:
        ch=guild.get_channel(int(existing['channel_id'])) if existing.get('channel_id') else None
        if ch:
            await interaction.followup.send(f'You already have an open **{category.title()}** Help Desk ticket: {ch.mention}', ephemeral=True)
            return

    person=await linked_personnel_for_discord(guild.id,member.id)
    action_id=None
    section=REQUEST_CATEGORY_SECTION[category]
    if person:
        action=await db.fetchrow("""INSERT INTO personnel_actions(personnel_id,action_type,subject,owning_section,status,priority,initiated_by,due_date,details_json)
            VALUES($1::uuid,$2,$3,$4,'OPEN','ROUTINE',$5,CURRENT_DATE + CASE WHEN $4='S-1' THEN 3 ELSE 5 END,$6::jsonb) RETURNING id""",
            str(person['id']),category,subject.strip(),section,
            f"{person.get('rank_code') or ''} {person.get('last_name') or ''}".strip(),
            json.dumps({'details':details.strip(),'source':'DISCORD HELPDESK','discord_user_id':str(member.id)}))
        action_id=str(action['id']) if action else None

    row=await db.fetchrow("""INSERT INTO clerk_helpdesk_tickets(guild_id,discord_user_id,category,subject,personnel_action_id)
        VALUES($1,$2,$3,$4,$5) RETURNING ticket_id""",str(guild.id),str(member.id),category,subject.strip(),action_id)
    ticket_id=int(row['ticket_id'])
    parent=await _helpdesk_category(guild)
    overwrites={
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
    }
    if guild.me:
        overwrites[guild.me]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,manage_channels=True,attach_files=True)
    for role_name in HELPDESK_STAFF_ROLES[category]:
        role=discord.utils.get(guild.roles,name=role_name)
        if role:
            overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,attach_files=True)

    channel=await guild.create_text_channel(
        _ticket_slug(member,ticket_id), category=parent, overwrites=overwrites,
        topic=f'1/5 CAV Help Desk | Ticket #{ticket_id} | {category} | Owner {member.id}',
        reason=f'Help Desk ticket #{ticket_id} opened by {member}'
    )
    await db.execute('UPDATE clerk_helpdesk_tickets SET channel_id=$1 WHERE ticket_id=$2',str(channel.id),ticket_id)

    action_line=f'Website Personnel Action: `{action_id}`' if action_id else 'Website Personnel Action: **Not created — no active Soldier Record is linked yet.**'
    embed=discord.Embed(title=f'1/5 CAV HELP DESK — TICKET #{ticket_id}',description=details[:3500])
    embed.add_field(name='Office',value=f'{category.title()} / {section}',inline=True)
    embed.add_field(name='Opened By',value=member.mention,inline=True)
    embed.add_field(name='Subject',value=subject[:1024],inline=False)
    embed.add_field(name='Battalion Record',value=action_line,inline=False)
    embed.set_footer(text='Use Claim to take staff ownership. Use Close Ticket when the matter is resolved.')
    mentions=' '.join(r.mention for r in guild.roles if r.name in HELPDESK_STAFF_ROLES[category])
    await channel.send(content=f'{member.mention} {mentions}'.strip(), embed=embed, view=HelpDeskTicketView())

    route=await get_report_channel(guild,f'REQUEST_{category}')
    if route and route.id != channel.id:
        await route.send(f'**HELP DESK TICKET #{ticket_id} — {category} / {section}**\n{member.mention} • **{subject[:180]}**\nPrivate ticket: {channel.mention}' + (f'\nPersonnel Action: `{action_id}`' if action_id else ''))
    await interaction.followup.send(f'Your private Help Desk ticket has been opened: {channel.mention}',ephemeral=True)


async def close_helpdesk_ticket(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel,discord.TextChannel): return
    await ensure_helpdesk_table(); db=collector.db
    row=await db.fetchrow("SELECT * FROM clerk_helpdesk_tickets WHERE channel_id=$1 AND status='OPEN'",str(interaction.channel.id))
    if not row:
        await interaction.response.send_message('This channel is not an open Battalion Help Desk ticket.',ephemeral=True); return
    member=interaction.user if isinstance(interaction.user,discord.Member) else None
    is_owner=str(interaction.user.id)==str(row['discord_user_id'])
    if not (is_owner or (member and _member_has_helpdesk_staff_role(member,row['category']))):
        await interaction.response.send_message('Only the ticket owner or authorized battalion staff can close this ticket.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True)

    lines=[]
    async for msg in interaction.channel.history(limit=500,oldest_first=True):
        stamp=msg.created_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        body=msg.clean_content or ''
        if msg.attachments:
            body += ('\n' if body else '') + 'Attachments: ' + ', '.join(a.url for a in msg.attachments)
        lines.append(f'[{stamp}] {msg.author} ({msg.author.id}): {body}')
    transcript=('\n'.join(lines) or 'No messages were recorded.').encode('utf-8',errors='replace')

    await db.execute("UPDATE clerk_helpdesk_tickets SET status='CLOSED',closed_at=NOW(),closed_by=$1 WHERE ticket_id=$2",str(interaction.user.id),row['ticket_id'])
    if row.get('personnel_action_id'):
        try:
            await db.execute("UPDATE personnel_actions SET status='CLOSED' WHERE id::text=$1",str(row['personnel_action_id']))
        except Exception as exc:
            log.warning('[HELPDESK ACTION CLOSE FAILED] ticket=%s action=%s error=%s',row['ticket_id'],row['personnel_action_id'],exc)

    archive=await get_report_channel(interaction.guild,'HELPDESK_ARCHIVE')
    if archive:
        f=discord.File(io.BytesIO(transcript),filename=f'helpdesk-ticket-{row["ticket_id"]}.txt')
        await archive.send(f'**CLOSED HELP DESK TICKET #{row["ticket_id"]}**\nCategory: **{str(row["category"]).title()}**\nSubject: **{row["subject"]}**\nClosed by: {interaction.user.mention}' + (f'\nPersonnel Action: `{row["personnel_action_id"]}`' if row.get('personnel_action_id') else ''),file=f)
    await interaction.followup.send('Ticket closed. The transcript has been archived when an archive channel is configured. This channel will be removed in 5 seconds.',ephemeral=True)
    await asyncio.sleep(5)
    try: await interaction.channel.delete(reason=f'Help Desk ticket #{row["ticket_id"]} closed')
    except Exception as exc: log.warning('[HELPDESK CHANNEL DELETE FAILED] ticket=%s error=%s',row['ticket_id'],exc)


class HelpDeskOpenModal(discord.ui.Modal):
    def __init__(self, category:str):
        super().__init__(title=f'{category.title()} Help Desk Request')
        self.category=category
        self.subject=discord.ui.TextInput(label='Subject',max_length=100,placeholder='Briefly describe what you need')
        self.details=discord.ui.TextInput(label='Details',style=discord.TextStyle.paragraph,max_length=1500,placeholder='Give battalion staff the information needed to help you.')
        self.add_item(self.subject); self.add_item(self.details)
    async def on_submit(self,interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True,thinking=True)
        await create_helpdesk_ticket(interaction,self.category,str(self.subject),str(self.details))


class HelpDeskCategoryButton(discord.ui.Button):
    def __init__(self, category:str, label:str, emoji:str):
        super().__init__(label=label,emoji=emoji,style=discord.ButtonStyle.secondary,custom_id=f'helpdesk:open:{category.lower()}')
        self.category=category
    async def callback(self,interaction:discord.Interaction):
        await interaction.response.send_modal(HelpDeskOpenModal(self.category))


class HelpDeskPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for cat,label,emoji in [
            ('PERSONNEL','Personnel','📁'),('TRAINING','Training','🎯'),('SUPPLY','Supply','📦'),
            ('LEADERSHIP','Leadership','⭐'),('TECHNICAL','Technical','🛠️')]:
            self.add_item(HelpDeskCategoryButton(cat,label,emoji))


class HelpDeskTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label='Claim',style=discord.ButtonStyle.primary,emoji='📌',custom_id='helpdesk:claim')
    async def claim(self,interaction:discord.Interaction,button:discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel,discord.TextChannel): return
        await ensure_helpdesk_table(); db=collector.db
        row=await db.fetchrow("SELECT * FROM clerk_helpdesk_tickets WHERE channel_id=$1 AND status='OPEN'",str(interaction.channel.id))
        if not row:
            await interaction.response.send_message('This is not an open Help Desk ticket.',ephemeral=True); return
        if not isinstance(interaction.user,discord.Member) or not _member_has_helpdesk_staff_role(interaction.user,row['category']):
            await interaction.response.send_message('Only authorized battalion staff can claim this ticket.',ephemeral=True); return
        await db.execute('UPDATE clerk_helpdesk_tickets SET claimed_by=$1 WHERE ticket_id=$2',str(interaction.user.id),row['ticket_id'])
        await interaction.response.send_message(f'📌 Ticket claimed by {interaction.user.mention}.')
    @discord.ui.button(label='Close Ticket',style=discord.ButtonStyle.danger,emoji='🔒',custom_id='helpdesk:close')
    async def close(self,interaction:discord.Interaction,button:discord.ui.Button):
        await close_helpdesk_ticket(interaction)


@bot.tree.command(name='helpdesk-panel',description='Post the Battalion Help Desk panel members use to open private tickets.')
@app_commands.describe(channel='Public/member text channel where the Help Desk panel should be posted')
async def helpdesk_panel(interaction:discord.Interaction,channel:discord.TextChannel):
    if not await require_manage_guild(interaction): return
    embed=discord.Embed(title='1ST BATTALION, 5TH CAVALRY — HELP DESK',description='Select the battalion office that best matches your request. Battalion Clerk will open a private channel visible only to you and the appropriate staff section.')
    embed.add_field(name='Personnel — S-1',value='Records, names, assignments, promotions, awards, access.',inline=False)
    embed.add_field(name='Training — S-3',value='Schools, qualifications, training records, operation questions.',inline=False)
    embed.add_field(name='Supply — S-4',value='Weapons, equipment, issue/turn-in, supply discrepancies.',inline=False)
    embed.add_field(name='Leadership / Technical',value='Chain-of-command concerns or website/Discord technical support.',inline=False)
    embed.set_footer(text='Each request creates a private ticket. Linked Soldiers also receive a website Personnel Action automatically.')
    await channel.send(embed=embed,view=HelpDeskPanelView())
    await set_report_channel(interaction.guild_id,'HELPDESK_PANEL',channel.id)
    await interaction.response.send_message(f'Battalion Help Desk panel posted in {channel.mention}.',ephemeral=True)


@bot.tree.command(name='helpdesk-archive',description='Assign the staff channel that receives closed Help Desk transcripts.')
async def helpdesk_archive(interaction:discord.Interaction,channel:discord.TextChannel):
    if not await require_manage_guild(interaction): return
    await set_report_channel(interaction.guild_id,'HELPDESK_ARCHIVE',channel.id)
    await interaction.response.send_message(f'Closed Help Desk transcripts will be archived in {channel.mention}.',ephemeral=True)


@bot.tree.command(name='helpdesk-status',description='Show Battalion Help Desk panel, archive, and open-ticket status.')
async def helpdesk_status(interaction:discord.Interaction):
    if not await require_manage_guild(interaction): return
    await ensure_helpdesk_table(); db=collector.db
    panel=await get_report_channel(interaction.guild,'HELPDESK_PANEL')
    archive=await get_report_channel(interaction.guild,'HELPDESK_ARCHIVE')
    open_count=await db.fetchrow("SELECT COUNT(*) AS n FROM clerk_helpdesk_tickets WHERE guild_id=$1 AND status='OPEN'",str(interaction.guild_id))
    await interaction.response.send_message(f'**Help Desk Panel:** {panel.mention if panel else "NOT ASSIGNED"}\n**Transcript Archive:** {archive.mention if archive else "NOT ASSIGNED"}\n**Open Tickets:** {int(open_count["n"] if open_count else 0)}',ephemeral=True)


@bot.tree.command(name='ticket',description='Open a private Battalion Help Desk ticket without using the panel.')
@app_commands.describe(category='Office that should receive the ticket',subject='Short description',details='What you need help with')
@app_commands.choices(category=REQUEST_CHOICES)
async def ticket_command(interaction:discord.Interaction,category:app_commands.Choice[str],subject:str,details:str):
    if not interaction.guild: return
    await interaction.response.defer(ephemeral=True,thinking=True)
    await create_helpdesk_ticket(interaction,category.value,subject,details)

INACTIVITY_LEVEL_CHOICES=[app_commands.Choice(name='S-1',value='INACTIVITY_S1'),app_commands.Choice(name='Command',value='INACTIVITY_COMMAND')]
@bot.tree.command(name='inactivity-report-channel', description='Assign where inactivity escalation reports are posted.')
@app_commands.choices(level=INACTIVITY_LEVEL_CHOICES)
async def inactivity_report_channel(interaction:discord.Interaction, level:app_commands.Choice[str], channel:discord.TextChannel):
    if not await require_manage_guild(interaction): return
    await set_report_channel(interaction.guild_id,level.value,channel.id)
    await interaction.response.send_message(f'**{level.name} inactivity reports** will be posted to {channel.mention}.',ephemeral=True)

@bot.tree.command(name='inactivity-thresholds', description='Set the shared Soldier/M16 inactivity escalation thresholds.')
async def inactivity_thresholds(interaction:discord.Interaction, warning_days:app_commands.Range[int,1,90], s1_days:app_commands.Range[int,2,120], property_days:app_commands.Range[int,3,150], command_days:app_commands.Range[int,4,180]):
    if not await require_manage_guild(interaction): return
    if not (warning_days < s1_days < property_days < command_days):
        await interaction.response.send_message('Thresholds must be ascending: WATCH < S-1 < PROPERTY < COMMAND.',ephemeral=True); return
    await ensure_clerk_settings_table(); db=collector.db
    await db.execute("""INSERT INTO clerk_guild_settings(guild_id,inactivity_warning_days,inactivity_s1_days,inactivity_property_days,inactivity_command_days,updated_at) VALUES($1,$2,$3,$4,$5,NOW())
        ON CONFLICT(guild_id) DO UPDATE SET inactivity_warning_days=EXCLUDED.inactivity_warning_days,inactivity_s1_days=EXCLUDED.inactivity_s1_days,inactivity_property_days=EXCLUDED.inactivity_property_days,inactivity_command_days=EXCLUDED.inactivity_command_days,updated_at=NOW()""",str(interaction.guild_id),warning_days,s1_days,property_days,command_days)
    await interaction.response.send_message(f'Shared inactivity thresholds: WATCH **{warning_days}d**, S-1 **{s1_days}d**, property/M16 review **{property_days}d**, Command **{command_days}d**.',ephemeral=True)

async def _notice_once(guild_id,personnel_id,notice_type,notice_key):
    db=collector.db
    r=await db.fetchrow("""INSERT INTO clerk_automation_notices(guild_id,personnel_id,notice_type,notice_key) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING RETURNING personnel_id""",str(guild_id),str(personnel_id),notice_type,notice_key)
    return bool(r)


@tasks.loop(minutes=5)
async def live_activity_credit_watch():
    """Credit approved community voice after 10 minutes without requiring a disconnect."""
    await collector.start()
    if not collector.db.pool: return
    now=utc_now()
    for (gid,uid),session in list(voice_sessions.items()):
        try:
            elapsed=max(0,int((now-session['started_at']).total_seconds()))
            if elapsed<300: continue
            channel_id=int(session.get('channel_id') or 0)
            if not await collector.db.fetchrow('SELECT 1 FROM activity_voice_channels WHERE guild_id=$1 AND channel_id=$2',gid,channel_id): continue
            # Discord voice contributes community/readiness attendance only.
            # Issued M16 service is authoritative from verified HLL telemetry.
            # Readiness/activity credit retains the existing ten-minute threshold.
            if elapsed<600: continue
            link=await collector.db.fetchrow("""SELECT p.id::text personnel_id FROM personnel p JOIN website_member_links w ON w.personnel_id=p.id::text WHERE w.guild_id::text=$1 AND w.discord_user_id::text=$2 LIMIT 1""",str(gid),str(uid))
            if not link: continue
            pid=link['personnel_id']; ref=str(channel_id)
            await collector.db.execute('UPDATE personnel SET activity_last_seen_at=NOW(),updated_at=NOW() WHERE id::text=$1',pid)
            existing=await collector.db.fetchrow("""SELECT id,duration_seconds FROM personnel_activity_credit WHERE personnel_id::text=$1 AND source='DISCORD_VOICE' AND source_reference=$2 AND activity_date=CURRENT_DATE ORDER BY created_at DESC LIMIT 1""",pid,ref)
            if existing:
                if elapsed>int(existing['duration_seconds'] or 0): await collector.db.execute('UPDATE personnel_activity_credit SET duration_seconds=$1,credited=TRUE WHERE id=$2',elapsed,existing['id'])
            else:
                await collector.db.execute("""INSERT INTO personnel_activity_credit(personnel_id,source,source_reference,activity_type,activity_date,duration_seconds,credited) VALUES($1::uuid,'DISCORD_VOICE',$2,'COMMUNITY ACTIVITY',CURRENT_DATE,$3,TRUE)""",pid,ref,elapsed)
            if WEBSITE_BASE_URL and CLERK_SYNC_KEY:
                try: await web.request('POST','/internal/clerk/readiness/recheck',json={'personnel_id':pid})
                except Exception as exc: log.warning('[LIVE ACTIVITY READINESS FAILED] member=%s error=%s',uid,exc)
        except Exception as exc:
            log.warning('[LIVE ACTIVITY CREDIT FAILED] guild=%s member=%s error=%s',gid,uid,exc)

@tasks.loop(minutes=60)
async def inactivity_watch():
    await collector.start(); db=collector.db
    # Keep issued-rifle neglect/fouling state current even when nobody opens the website.
    try:
        await web.request('POST','/internal/clerk/weapons/refresh-inactivity',json={})
    except Exception as exc:
        log.warning('[WEAPON INACTIVITY REFRESH FAILED] error=%s',exc)
    for guild in bot.guilds:
        await ensure_clerk_settings_table()
        async with db.pool.acquire() as conn:
            cfg=await conn.fetchrow("SELECT COALESCE(inactivity_warning_days,7) w,COALESCE(inactivity_s1_days,14) s,COALESCE(inactivity_property_days,21) p,COALESCE(inactivity_command_days,30) c FROM clerk_guild_settings WHERE guild_id=$1",str(guild.id))
        w,s1,prop,cmd=(int(cfg['w']),int(cfg['s']),int(cfg['p']),int(cfg['c'])) if cfg else (7,14,21,30)
        rows=await db.fetch("""SELECT p.id,p.rank_code,p.first_name,p.last_name,p.activity_last_seen_at,p.activity_last_duty_at,p.created_at,p.duty_status,p.loa_expected_return_date,w.discord_user_id,
            EXTRACT(DAY FROM NOW()-COALESCE(p.activity_last_seen_at,p.activity_last_duty_at,p.created_at))::int AS inactive_days
            FROM personnel p JOIN website_member_links w ON w.personnel_id=p.id::text AND w.guild_id::text=$1
            WHERE COALESCE(p.lifecycle_state,'') NOT IN ('SEPARATED','ARCHIVED')""",str(guild.id))
        for r in rows:
            # Authorized leave/absence pauses inactivity escalation until the expected return date.
            if str(r.get('duty_status') or '').upper() == 'LEAVE':
                expected=r.get('loa_expected_return_date')
                if expected is None or expected >= __import__('datetime').date.today():
                    continue
            days=int(r['inactive_days'] or 0); pid=r['id']; member=guild.get_member(int(r['discord_user_id']))
            name=f"{r['rank_code'] or ''} {r['first_name'] or ''} {r['last_name'] or ''}".strip()
            if days>=cmd:
                key=f'CMD:{days//7}'
                if await _notice_once(guild.id,pid,'INACTIVITY_COMMAND',key):
                    ch=await get_report_channel(guild,'INACTIVITY_COMMAND')
                    if ch: await ch.send(f'**COMMAND INACTIVITY ESCALATION**\n{name} — **{days} days inactive**. S-1 disposition / leadership review required.')
                    await db.execute("""INSERT INTO personnel_actions(personnel_id,action_type,subject,owning_section,status,priority,initiated_by,details_json,source_key) VALUES($1::uuid,'PERSONNEL',$2,'HQ','OPEN','URGENT','BATTALION CLERK',$3::jsonb,$4) ON CONFLICT(source_key) DO NOTHING""",str(pid),f'Command inactivity review — {name}',__import__('json').dumps({'inactive_days':days}),f'INACTIVE-CMD:{pid}')
            elif days>=prop:
                key='PROPERTY:21'
                if await _notice_once(guild.id,pid,'INACTIVITY_PROPERTY',key):
                    ch=await get_report_channel(guild,'INACTIVITY_S1')
                    if ch: await ch.send(f'**PROPERTY ACCOUNTABILITY REVIEW**\n{name} — **{days} days inactive**. Assigned M16/property requires leadership contact and S-1/S-4 review.')
                    await db.execute("""INSERT INTO personnel_actions(personnel_id,action_type,subject,owning_section,status,priority,initiated_by,details_json,source_key) VALUES($1::uuid,'PERSONNEL',$2,'S-1','OPEN','HIGH','BATTALION CLERK',$3::jsonb,$4) ON CONFLICT(source_key) DO NOTHING""",str(pid),f'Property accountability review — {name}',__import__('json').dumps({'inactive_days':days,'stage':'PROPERTY ACCOUNTABILITY REVIEW'}),f'INACTIVE-PROPERTY:{pid}')
            elif days>=s1:
                key='S1:14'
                if await _notice_once(guild.id,pid,'INACTIVITY_S1',key):
                    ch=await get_report_channel(guild,'INACTIVITY_S1')
                    if ch: await ch.send(f'**S-1 READINESS DEFICIENCY**\n{name} — **{days} days inactive**. Contact the Soldier and determine disposition.')
                    await db.execute("""INSERT INTO personnel_actions(personnel_id,action_type,subject,owning_section,status,priority,initiated_by,details_json,source_key) VALUES($1::uuid,'PERSONNEL',$2,'S-1','OPEN','HIGH','BATTALION CLERK',$3::jsonb,$4) ON CONFLICT(source_key) DO NOTHING""",str(pid),f'Inactivity review — {name}',__import__('json').dumps({'inactive_days':days,'stage':'DEFICIENT'}),f'INACTIVE-S1:{pid}')
            elif days>=w and member:
                if await _notice_once(guild.id,pid,'INACTIVITY_MEMBER',f'WARN:{days//7}'):
                    try: await member.send(f'**1/5 CAV — ACTIVITY NOTICE**\nYour Soldier Record shows **{days} days** since qualifying battalion activity. Join an approved activity voice channel or participate in official battalion duty to return to current status. Simply opening the website does not reset inactivity. Continued inactivity will be referred to S-1.')
                    except discord.Forbidden: pass


@bot.tree.command(name='suspense-channel', description='Assign the staff channel for personnel-action due-date and overdue reminders.')
async def suspense_channel(interaction:discord.Interaction, channel:discord.TextChannel):
    if not await require_manage_guild(interaction): return
    await set_report_channel(interaction.guild_id,'PERSONNEL_SUSPENSE',channel.id)
    await interaction.response.send_message(f'Personnel-action suspense reminders will be posted to {channel.mention}.',ephemeral=True)

@tasks.loop(minutes=60)
async def personnel_suspense_watch():
    """Remind staff at 2 days, 1 day, due today, and each meaningful overdue stage."""
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    for guild in bot.guilds:
        try:
            data=await web.request('GET','/internal/clerk/personnel-actions/suspense',params={'guild_id':guild.id})
            ch=await get_report_channel(guild,'PERSONNEL_SUSPENSE')
            if not ch: continue
            for a in data.get('actions',[]):
                days=int(a.get('days_remaining') or 0)
                aid=str(a.get('id') or '')
                pid=str(a.get('personnel_id') or aid or 'UNASSIGNED')
                if days==2: stage='DUE-2'
                elif days==1: stage='DUE-1'
                elif days==0: stage='DUE-TODAY'
                elif days<0: stage=f'OVERDUE-{min(abs(days),30)}'
                else: continue
                key=f"{aid}:{stage}"
                if not await _notice_once(guild.id,pid,'PERSONNEL_SUSPENSE',key): continue
                soldier=(f"{a.get('rank_code') or ''} {a.get('first_name') or ''} {a.get('last_name') or ''}".strip() or 'UNASSIGNED ACTION')
                if days<0:
                    timing=f"**{abs(days)} day(s) overdue**"
                elif days==0:
                    timing='**DUE TODAY**'
                else:
                    timing=f"due in **{days} day(s)**"
                await ch.send(f"**PERSONNEL ACTION SUSPENSE — {a.get('owning_section') or 'STAFF'}**\n{soldier}\n**{a.get('subject') or a.get('action_type') or 'Personnel Action'}** — {timing}\nPriority: **{a.get('priority') or 'ROUTINE'}** • Status: **{a.get('status') or 'OPEN'}**")
        except Exception as exc:
            log.warning('[PERSONNEL SUSPENSE WATCH FAILED] guild=%s error=%s',guild.id,exc)

@bot.tree.command(name='promotion-report-channel', description='Assign the channel for promotion-eligibility summaries.')
async def promotion_report_channel(interaction:discord.Interaction, channel:discord.TextChannel):
    if not await require_manage_guild(interaction): return
    await set_report_channel(interaction.guild_id,'PROMOTION_ELIGIBILITY',channel.id)
    await interaction.response.send_message(f'Promotion eligibility summaries will be posted to {channel.mention}.',ephemeral=True)

@tasks.loop(minutes=15)
async def promotion_eligibility_watch():
    if not WEBSITE_BASE_URL or not CLERK_SYNC_KEY: return
    for guild in bot.guilds:
        try:
            data=await web.request('GET','/internal/clerk/automation/promotion-eligibility',params={'guild_id':guild.id})
            for item in data.get('eligible',[]):
                pid=item.get('personnel_id'); target=item.get('target_rank'); key=f'{item.get("rank_code")}->{target}'
                if not await _notice_once(guild.id,pid,'PROMOTION_ELIGIBLE',key): continue
                leader_id=item.get('leader_discord_user_id'); leader=guild.get_member(int(leader_id)) if leader_id else None
                text=(f"**PROMOTION ELIGIBILITY — NCO NOTICE**\n{item.get('rank_code','')} {item.get('first_name','')} {item.get('last_name','')} is now **ELIGIBLE FOR CONSIDERATION** for **{target}**.\nThis is not an automatic promotion. Review the Soldier and submit a recommendation if warranted.")
                if leader:
                    try: await leader.send(text)
                    except discord.Forbidden: pass
                ch=await get_report_channel(guild,'PROMOTION_ELIGIBILITY')
                if ch: await ch.send(text)
        except Exception as exc: log.warning('[PROMOTION ELIGIBILITY WATCH FAILED] guild=%s error=%s',guild.id,exc)

@bot.tree.command(name='post-operation-report-channel', description='Assign where automatic post-operation processing summaries are posted.')
async def post_operation_report_channel(interaction:discord.Interaction, channel:discord.TextChannel):
    if not await require_manage_guild(interaction): return
    await set_report_channel(interaction.guild_id,'POST_OPERATION',channel.id)
    await interaction.response.send_message(f'Post-operation processing reports will be posted to {channel.mention}.',ephemeral=True)

@bot.tree.command(name='operation-rounds-default', description='Legacy setting; M16 field service now comes from verified HLL server telemetry.')
async def operation_rounds_default(interaction:discord.Interaction, rounds:app_commands.Range[int,0,1000]):
    if not await require_manage_guild(interaction): return
    await ensure_clerk_settings_table(); db=collector.db
    await db.execute("""INSERT INTO clerk_guild_settings(guild_id,operation_rounds_default,updated_at) VALUES($1,$2,NOW()) ON CONFLICT(guild_id) DO UPDATE SET operation_rounds_default=EXCLUDED.operation_rounds_default,updated_at=NOW()""",str(interaction.guild_id),rounds)
    await interaction.response.send_message('M16 field service is now automatic from **verified HLL server telemetry**. Discord voice does not advance the rifle record. This legacy setting is retained only for older records.',ephemeral=True)

async def operation_rounds_for_guild(guild_id:int):
    await ensure_clerk_settings_table(); db=collector.db
    async with db.pool.acquire() as conn: v=await conn.fetchval("SELECT COALESCE(operation_rounds_default,180) FROM clerk_guild_settings WHERE guild_id=$1",str(guild_id))
    return int(v or 180)

# ---------------------------------------------------------------------------
# HELL LET LOOSE: VIETNAM — RCON / TELEMETRY
# ---------------------------------------------------------------------------
@bot.tree.command(name='hll-link', description='Link your SteamID64 to your 1/5 CAV Soldier Record for automatic server statistics.')
@app_commands.describe(steam_id='Your 17-digit SteamID64')
async def hll_link(interaction:discord.Interaction, steam_id:str):
    if not interaction.guild:
        await interaction.response.send_message('Use this command inside the 1/5 CAV Discord server.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True,thinking=True)
    result=await hllv.link_personnel(interaction.guild.id,interaction.user.id,steam_id,f'DISCORD SELF-LINK:{interaction.user.id}')
    if not result.get('ok'):
        await interaction.followup.send(f"HLL link not filed: **{result.get('error','unknown error')}**",ephemeral=True); return
    await interaction.followup.send(
        f"**HLL: VIETNAM IDENTITY LINK FILED**\n{result.get('soldier')} is now linked to SteamID64 `{result.get('steam_id')}`. "
        "Battalion Clerk will automatically file server time, distance, role time and combat statistics while you are on the unit server.",ephemeral=True)

@bot.tree.command(name='hll-unlink', description='Remove your HLL: Vietnam game identity link from automatic server statistics.')
async def hll_unlink(interaction:discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message('Use this command inside the 1/5 CAV Discord server.',ephemeral=True); return
    ok=await hllv.unlink_personnel(interaction.guild.id,interaction.user.id)
    await interaction.response.send_message('Your HLL game identity link was removed.' if ok else 'No HLL telemetry link was on file.',ephemeral=True)

@bot.tree.command(name='hll-link-soldier', description='Command: manually link a Soldier SteamID64 or console gamer tag.')
@app_commands.describe(
    member='Soldier whose HLL identity should be linked',
    platform='Game platform',
    game_id='SteamID64, Xbox Gamertag, or PSN Online ID exactly as used in-game'
)
@app_commands.choices(platform=[
    app_commands.Choice(name='Steam / PC', value='STEAM'),
    app_commands.Choice(name='Xbox', value='XBOX'),
    app_commands.Choice(name='PlayStation 5', value='PS5'),
])
async def hll_link_soldier(interaction:discord.Interaction, member:discord.Member, platform:app_commands.Choice[str], game_id:str):
    if not await require_manage_guild(interaction): return
    await interaction.response.defer(ephemeral=True,thinking=True)
    result=await hllv.staff_link_identity(
        interaction.guild.id, member.id, platform.value, game_id,
        f'COMMAND MANUAL LINK:{interaction.user.id}'
    )
    if not result.get('ok'):
        await interaction.followup.send(
            f"**HLL IDENTITY LINK FAILED**\nSoldier: {member.mention}\n"
            f"Platform: **{platform.name}**\nReason: **{result.get('error','unknown error')}**",
            ephemeral=True
        ); return
    status=str(result.get('status') or 'VERIFIED').upper()
    identity=result.get('identity') or game_id
    if status == 'PENDING':
        await interaction.followup.send(
            f"**HLL IDENTITY FILED — PENDING VERIFICATION**\n"
            f"Soldier: **{result.get('soldier')}**\n"
            f"Platform: **{platform.name}**\n"
            f"Identity: `{identity}`\n\n"
            "Battalion Clerk has saved the link. The Soldier does **not** need to run a Discord link command. "
            "The first time this exact console account is observed on the 1/5 CAV server, the claim will automatically become **VERIFIED**.",
            ephemeral=True
        ); return
    await interaction.followup.send(
        f"**HLL IDENTITY LINKED — VERIFIED**\n"
        f"Soldier: **{result.get('soldier')}**\n"
        f"Platform: **{platform.name}**\n"
        f"Identity: `{identity}`\n\n"
        "Battalion Clerk will use this identity for automatic HLL service telemetry.",
        ephemeral=True
    )

@bot.tree.command(name='hll-link-member', description='Staff: link a Soldier to a SteamID64 for HLL: Vietnam telemetry.')
@app_commands.describe(member='Soldier to link',steam_id='17-digit SteamID64')
async def hll_link_member(interaction:discord.Interaction, member:discord.Member, steam_id:str):
    if not await require_manage_guild(interaction): return
    await interaction.response.defer(ephemeral=True,thinking=True)
    result=await hllv.link_personnel(interaction.guild.id,member.id,steam_id,f'STAFF:{interaction.user.id}')
    if not result.get('ok'):
        await interaction.followup.send(f"Link failed: **{result.get('error','unknown error')}**",ephemeral=True); return
    await interaction.followup.send(f"Linked **{result.get('soldier')}** to SteamID64 `{result.get('steam_id')}`.",ephemeral=True)

@bot.tree.command(name='hll-link-console', description='Link your Xbox or PS5 account to your 1/5 CAV Soldier Record.')
@app_commands.describe(platform='Your console platform',gamertag='Your Gamertag / PSN Online ID exactly as it appears in-game')
@app_commands.choices(platform=[
    app_commands.Choice(name='Xbox', value='XBOX'),
    app_commands.Choice(name='PlayStation 5', value='PS5'),
])
async def hll_link_console(interaction:discord.Interaction, platform:app_commands.Choice[str], gamertag:str):
    if not interaction.guild:
        await interaction.response.send_message('Use this command inside the 1/5 CAV Discord server.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True,thinking=True)
    result=await hllv.link_console_personnel(interaction.guild.id,interaction.user.id,platform.value,gamertag,f'DISCORD CONSOLE SELF-LINK:{interaction.user.id}')
    if not result.get('ok'):
        await interaction.followup.send(f"Console link failed: **{result.get('error','unknown error')}**",ephemeral=True); return
    await interaction.followup.send(
        f"**HLL: VIETNAM CONSOLE LINK FILED**\nLinked **{result.get('soldier')}** to **{platform.name}** player **{result.get('player_name')}**. "
        "Battalion Clerk will now attach your verified server service record to your Soldier Record.", ephemeral=True)

@bot.tree.command(name='hll-link-console-member', description='Staff: link another Soldier to an Xbox or PS5 player by gamertag.')
@app_commands.describe(member='Soldier to link',platform='Console platform',gamertag='Gamertag / PSN Online ID exactly as it appears in-game')
@app_commands.choices(platform=[
    app_commands.Choice(name='Xbox', value='XBOX'),
    app_commands.Choice(name='PlayStation 5', value='PS5'),
])
async def hll_link_console_member(interaction:discord.Interaction, member:discord.Member, platform:app_commands.Choice[str], gamertag:str):
    if not await require_manage_guild(interaction): return
    await interaction.response.defer(ephemeral=True,thinking=True)
    result=await hllv.link_console_personnel(interaction.guild.id,member.id,platform.value,gamertag,f'STAFF CONSOLE LINK:{interaction.user.id}')
    if not result.get('ok'):
        await interaction.followup.send(f"Console link failed: **{result.get('error','unknown error')}**",ephemeral=True); return
    await interaction.followup.send(
        f"Linked **{result.get('soldier')}** to **{platform.name}** player **{result.get('player_name')}**. "
        "Battalion Clerk will now attach that player's server service record to the Soldier Record.", ephemeral=True)

@bot.tree.command(name='server-message', description='Staff: send a one-time message to everyone on the HLL: Vietnam server.')
@app_commands.describe(message='Message to display in game (180 characters maximum)')
async def server_message(interaction:discord.Interaction, message:str):
    if not await require_manage_guild(interaction): return
    clean=' '.join(str(message or '').split()).strip()
    if not clean:
        await interaction.response.send_message('Enter a message to send to the server.',ephemeral=True); return
    if len(clean)>180:
        await interaction.response.send_message(f'Message is **{len(clean)}** characters. Keep it to **180 or fewer**.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True,thinking=True)
    result=await hllv.send_manual_broadcast(clean,10)
    if not result.get('ok'):
        await interaction.followup.send(f"Server message was not sent: **{result.get('error','unknown error')}**",ephemeral=True); return
    await interaction.followup.send(
        f"**SERVER MESSAGE SENT**\nDisplayed for **{result.get('display_seconds',10)} seconds**, then it will clear automatically.\n\n{result.get('message')}",
        ephemeral=True)

@bot.tree.command(name='server-message-clear', description='Staff: immediately clear the current HLL: Vietnam server broadcast.')
async def server_message_clear(interaction:discord.Interaction):
    if not await require_manage_guild(interaction): return
    await interaction.response.defer(ephemeral=True,thinking=True)
    result=await hllv.clear_manual_broadcast()
    if not result.get('ok'):
        await interaction.followup.send(f"Server message could not be cleared: **{result.get('error','unknown error')}**",ephemeral=True); return
    await interaction.followup.send('**SERVER MESSAGE CLEARED**',ephemeral=True)

@bot.tree.command(name='hll-rcon-status', description='Show Battalion Clerk HLL: Vietnam RCON collector health.')
async def hll_rcon_status(interaction:discord.Interaction):
    if not await require_manage_guild(interaction): return
    await interaction.response.defer(ephemeral=True,thinking=True)
    st=await hllv.status()
    last=st.get('last_success_at')
    last_txt=last.isoformat() if hasattr(last,'isoformat') else str(last or 'NEVER')
    await interaction.followup.send(
        '**HLL: VIETNAM RCON STATUS**\n'
        f"Configured: **{'YES' if st.get('configured') else 'NO'}**\n"
        f"Connection: **{'CURRENT' if st.get('connected') else 'NOT CURRENT'}**\n"
        f"Server: **{st.get('server_name') or '—'}**\n"
        f"Map / Mode: **{st.get('map_name') or '—'} / {st.get('game_mode') or '—'}**\n"
        f"Players: **{st.get('player_count',0)}**\nLast successful sample: `{last_txt}`\n"
        + (f"Last error: `{str(st.get('last_error'))[:700]}`" if st.get('last_error') else 'Last error: **NONE**'),ephemeral=True)

@bot.tree.command(name='hll-research', description='Show your latest HLLV telemetry research sample for role/vehicle/aviation mapping.')
async def hll_research(interaction:discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message('Use this command inside the 1/5 CAV Discord server.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True,thinking=True)
    data=await hllv.research_snapshot(interaction.guild.id,interaction.user.id)
    if not data or not data.get('sample'):
        await interaction.followup.send('No linked research sample is on file yet. Join the unit HLLV server for at least one polling cycle.',ephemeral=True); return
    r=data['sample']; speed=float(r.get('speed_mps') or 0)*3.6; vs=float(r.get('vertical_speed_mps') or 0)
    mapped=(r.get('verified_role_name') or r.get('observed_role_label') or 'UNVERIFIED')
    await interaction.followup.send(
        '**HLLV TELEMETRY RESEARCH SAMPLE**\n'
        f"Role ID: **{r.get('role_id') or '—'}** • Observed label: **{mapped}**\n"
        f"Mapping status: **{'VERIFIED' if r.get('verified') else 'OBSERVATION'}** • Category: **{r.get('role_category') or '—'}** • MOS: **{r.get('mos_code') or '—'}**\n"
        f"Loadout: **{r.get('loadout') or '—'}**\nSpeed: **{speed:.1f} km/h** • Vertical rate: **{vs:.2f} m/s**\n"
        f"Position: `{r.get('x')}, {r.get('y')}, {r.get('z')}`\nObserved: `{r.get('observed_at')}`",ephemeral=True)

@bot.tree.command(name='hll-role-research', description='Staff: summarize observed HLLV role IDs and movement evidence.')
async def hll_role_research(interaction:discord.Interaction):
    if not await require_manage_guild(interaction): return
    await interaction.response.defer(ephemeral=True,thinking=True)
    rows=await hllv.role_research_summary()
    if not rows:
        await interaction.followup.send('No HLLV role observations have been collected yet.',ephemeral=True); return
    lines=[]
    for r in rows[:20]:
        lines.append(f"`{r.get('role_id')}` — {r.get('verified_role_name') or r.get('observed_label') or 'UNMAPPED'} | samples {int(r.get('sample_count') or 0)} | loadouts {int(r.get('loadout_count') or 0)} | max {float(r.get('max_speed_mps') or 0)*3.6:.0f} km/h | vertical {float(r.get('max_vertical_speed_mps') or 0):.1f} m/s | {'VERIFIED' if r.get('verified') else 'OBSERVE'}")
    await interaction.followup.send('**HLLV ROLE / VEHICLE / AVIATION RESEARCH**\n'+'\n'.join(lines),ephemeral=True)

@bot.tree.command(name='hll-stats', description='Show your automatically recorded HLL: Vietnam field-service statistics.')
async def hll_stats(interaction:discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message('Use this command inside the 1/5 CAV Discord server.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True,thinking=True)
    data=await hllv.personnel_stats(interaction.guild.id,interaction.user.id)
    if not data:
        await interaction.followup.send('No Soldier Record is linked to your Discord account.',ephemeral=True); return
    if not data.get('link'):
        await interaction.followup.send('Your Soldier Record exists, but no SteamID64 is linked. Use `/hll-link` first.',ephemeral=True); return
    a=data.get('aggregate') or {}; latest=data.get('latest') or {}
    sec=int(a.get('seconds') or 0); h=sec//3600; m=(sec%3600)//60
    km=float(a.get('distance') or 0)/1000.0
    lead_sec=int(a.get('leadership_total_seconds') or 0); lead_h=lead_sec//3600; lead_m=(lead_sec%3600)//60
    m16=a.get('m16_service') or {}; m16_sec=int(m16.get('seconds') or 0); m16_h=m16_sec//3600; m16_m=(m16_sec%3600)//60
    await interaction.followup.send(
        '**HLL: VIETNAM — FIELD SERVICE STATISTICS**\n'
        f"Matches sampled: **{int(a.get('matches') or 0)}**\nServer time: **{h}h {m}m**\nDistance traveled: **{km:.2f} km**\n"
        f"Infantry kills: **{int(a.get('infantry_kills') or 0)}** • Deaths: **{int(a.get('deaths') or 0)}** • Blue on Blue: **{int(a.get('blue_on_blue') or 0)}**\n"
        f"Vehicle kills: **{int(a.get('vehicle_kills') or 0)}** • Vehicles destroyed: **{int(a.get('vehicles_destroyed') or 0)}**\n"
        f"Leadership experience: **{lead_h}h {lead_m}m** (Squad Leader / Tank Commander / Logistics Officer)\n"
        f"M16/XM16 service: **{m16_h}h {m16_m}m** • **{float(m16.get('distance') or 0)/1000.0:.1f} km** • **{int(m16.get('kills') or 0)} kills** • **{int(m16.get('blue_on_blue') or 0)} Blue on Blue\n"
        f"Verified field experience: **{a.get('field_experience') or 'NEWLY ARRIVED'}**\n"
        f"Total score: **{int(a.get('score_total') or 0)}**\n"
        f"Latest map: **{latest.get('map_name') or '—'} / {latest.get('game_mode') or '—'}**",ephemeral=True)

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

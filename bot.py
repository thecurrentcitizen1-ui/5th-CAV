import os
import logging
import uuid
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


async def ensure_clerk_settings_table():
    """Create the small bot-side settings table without touching website tables."""
    db = getattr(collector, 'db', None)
    if not db or not getattr(db, 'pool', None):
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS clerk_guild_settings (
            guild_id TEXT PRIMARY KEY,
            orders_channel_id TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    if result.get("created"):
        log.info("[201 FILE OPENED] %s (%s) roster=%s rank=%s",
                 member.display_name,member.id,result.get("roster_number"),result.get("rank_code"))
        if deliver_credentials and result.get("roster_number") and result.get("field_code"):
            weapon_line=f"\nM16 Serial No.: **{result.get('weapon_serial')}**" if result.get("weapon_serial") else ""
            try:
                await member.send(
                    "**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n"
                    "**BATTLE ROSTER CARD ISSUED**\n\n"
                    f"Soldier: **{member.display_name}**\n"
                    f"Battle Roster No.: **{result.get('roster_number')}**\n"
                    f"Field Code: **{result.get('field_code')}**"
                    f"{weapon_line}\n\n"
                    "Retain this information. It is used to report for duty and open your 201 File.")
            except discord.Forbidden:
                log.warning("[ROSTER CARD DM BLOCKED] %s (%s)",member.display_name,member.id)
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
        await sync_personnel_identity(after,create_if_missing=False,reason="member_or_role_update")
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
        await sync_personnel_identity(member,create_if_missing=True,reason="official_duty_presence")
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

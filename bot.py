import os
import logging
from datetime import datetime, timezone
from typing import Dict, Tuple

import discord
from discord.ext import commands
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

intents = discord.Intents.none()
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
collector = DataCollector()
collector_started = False

# One active voice session per Discord member.
# key: (guild_id, user_id) -> session metadata
voice_sessions: Dict[Tuple[int, int], dict] = {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


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


@bot.event
async def on_ready():
    global collector_started
    if not collector_started:
        await collector.start()
        collector_started = True

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

        # Sync current Discord identity into the shared database. This does not
        # create website personnel records; it only establishes source identity.
        synced_members = 0
        for member in guild.members:
            await collector.upsert_member(member)
            synced_members += 1

        recovered_count = 0
        for channel in guild.voice_channels:
            for member in channel.members:
                if member.bot:
                    continue
                begin_session(member, channel, now, recovered=True)
                recovered_count += 1
                log.info('[VOICE RECOVER] %s (%s) already in #%s', member.display_name, member.id, channel.name)

        log.info(
            '[GUILD SYNC] guild=%s (%s) members=%s recovered_voice_sessions=%s',
            guild.name, guild.id, synced_members, recovered_count
        )


@bot.event
async def on_member_join(member: discord.Member):
    if GUILD_ID and member.guild.id != GUILD_ID:
        return
    await collector.upsert_member(member)
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
            log.info('[VOICE SESSION] %s #%s %s (member left server)', member.display_name, session['channel_name'], session['duration_hms'])

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
    if before.name == after.name and before.display_name == after.display_name:
        return
    await collector.upsert_member(after)
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

    before_id = str(before.channel.id) if before.channel else None
    after_id = str(after.channel.id) if after.channel else None
    if before_id == after_id:
        return

    now = utc_now()
    await collector.upsert_member(member)

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
        log.info('[VOICE MOVE] %s (%s) #%s -> #%s | prior_session=%s', member.display_name, member.id, before.channel.name, after.channel.name, duration)
        event_type = 'voice_move'

    await collector.record_event(event_type, {
        'guild_id': str(member.guild.id),
        'discord_user_id': str(member.id),
        'username': member.name,
        'display_name': member.display_name,
        'from_channel_id': before_id,
        'from_channel_name': before.channel.name if before.channel else None,
        'to_channel_id': after_id,
        'to_channel_name': after.channel.name if after.channel else None,
        'timestamp': iso(now),
    })


if not TOKEN:
    raise RuntimeError('DISCORD_TOKEN is not set. Add DISCORD_TOKEN in Railway Variables.')

bot.run(TOKEN)

import os
import logging
from datetime import datetime, timezone

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


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@bot.event
async def on_ready():
    log.info('Battalion Clerk online as %s (%s)', bot.user, bot.user.id if bot.user else 'unknown')
    for guild in bot.guilds:
        if GUILD_ID and guild.id != GUILD_ID:
            continue
        await collector.record_event('bot_ready', {
            'guild_id': str(guild.id),
            'guild_name': guild.name,
            'timestamp': iso_now(),
        })


@bot.event
async def on_member_join(member: discord.Member):
    if GUILD_ID and member.guild.id != GUILD_ID:
        return
    await collector.record_event('member_join', {
        'guild_id': str(member.guild.id),
        'discord_user_id': str(member.id),
        'username': member.name,
        'display_name': member.display_name,
        'is_bot': member.bot,
        'joined_at': member.joined_at.isoformat() if member.joined_at else iso_now(),
        'timestamp': iso_now(),
    })


@bot.event
async def on_member_remove(member: discord.Member):
    if GUILD_ID and member.guild.id != GUILD_ID:
        return
    await collector.record_event('member_leave', {
        'guild_id': str(member.guild.id),
        'discord_user_id': str(member.id),
        'username': member.name,
        'display_name': member.display_name,
        'is_bot': member.bot,
        'timestamp': iso_now(),
    })


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

    if before.channel is None and after.channel is not None:
        event_type = 'voice_join'
    elif before.channel is not None and after.channel is None:
        event_type = 'voice_leave'
    else:
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
        'timestamp': iso_now(),
    })


if not TOKEN:
    raise RuntimeError('DISCORD_TOKEN is not set. Copy .env.example to .env and add your token.')

bot.run(TOKEN)

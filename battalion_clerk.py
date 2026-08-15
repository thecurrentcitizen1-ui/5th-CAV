import os
import uuid
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID", "").strip()
GUILD_ID = os.getenv("GUILD_ID", "").strip()
COMMAND_GUILD_ID = TEST_GUILD_ID or GUILD_ID
WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "").strip().rstrip("/")
CLERK_SYNC_KEY = os.getenv("CLERK_SYNC_KEY", "").strip()
BATTALION_TIMEZONE = os.getenv("BATTALION_TIMEZONE", "America/New_York").strip()
FLUSH_SECONDS = int(os.getenv("VOICE_FLUSH_SECONDS", "300"))

DUTY_TYPES = ("TRAINING", "OPERATION", "MEETING")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


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
            raise RuntimeError("WEBSITE_BASE_URL and CLERK_SYNC_KEY must be configured")
        headers = {"X-Battalion-Clerk-Key": CLERK_SYNC_KEY}
        async with self.session.request(method, f"{WEBSITE_BASE_URL}{path}", params=params, json=json, headers=headers) as r:
            try:
                body = await r.json()
            except Exception:
                body = {"ok": False, "error": await r.text()}
            if r.status >= 400:
                raise RuntimeError(body.get("error") or f"Website returned HTTP {r.status}")
            return body


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True


class BattalionClerk(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.web = WebsiteClient()
        self.channel_bindings: dict[int, dict[str, int]] = {}
        # (guild_id, member_id, channel_id) -> chunk_start UTC datetime
        self.voice_presence: dict[tuple[int, int, int], datetime] = {}

    async def setup_hook(self):
        await self.web.start()
        if COMMAND_GUILD_ID:
            guild = discord.Object(id=int(COMMAND_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            source = "TEST_GUILD_ID" if TEST_GUILD_ID else "GUILD_ID"
            print(f"Synced {len(synced)} commands to guild {COMMAND_GUILD_ID} via {source}")
        else:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} global commands")
        if not self.flush_voice_chunks.is_running():
            self.flush_voice_chunks.change_interval(seconds=max(60, FLUSH_SECONDS))
            self.flush_voice_chunks.start()

    async def close(self):
        if self.flush_voice_chunks.is_running():
            self.flush_voice_chunks.cancel()
        await self.flush_all_presence()
        await self.web.close()
        await super().close()

    async def load_bindings(self, guild_id: int):
        data = await self.web.request("GET", "/internal/clerk/channels", params={"guild_id": guild_id})
        self.channel_bindings[guild_id] = {row["event_type"]: int(row["channel_id"]) for row in data.get("channels", [])}
        return data.get("channels", [])

    def event_type_for_channel(self, guild_id: int, channel_id: int) -> Optional[str]:
        for event_type, cid in self.channel_bindings.get(guild_id, {}).items():
            if int(cid) == int(channel_id):
                return event_type
        return None

    async def send_presence_chunk(self, guild_id: int, member_id: int, channel_id: int, started: datetime, ended: datetime):
        event_type = self.event_type_for_channel(guild_id, channel_id)
        if not event_type or ended <= started:
            return None
        guild = self.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        segment_id = str(uuid.uuid4())
        payload = {
            "guild_id": guild_id,
            "member_id": member_id,
            "channel_id": channel_id,
            "channel_name": event_type.title(),
            "joined_at": iso(started),
            "left_at": iso(ended),
            "session_id": segment_id,
        }
        return await self.web.request("POST", "/internal/clerk/attendance", json=payload)

    async def flush_all_presence(self, *, guild_id: Optional[int] = None, channel_id: Optional[int] = None):
        now = utcnow()
        keys = list(self.voice_presence.keys())
        for key in keys:
            gid, uid, cid = key
            if guild_id is not None and gid != guild_id:
                continue
            if channel_id is not None and cid != channel_id:
                continue
            started = self.voice_presence.get(key)
            if not started:
                continue
            try:
                await self.send_presence_chunk(gid, uid, cid, started, now)
                self.voice_presence[key] = now
            except Exception as exc:
                print(f"Voice flush failed for {key}: {exc}")

    @tasks.loop(seconds=300)
    async def flush_voice_chunks(self):
        await self.flush_all_presence()

    @flush_voice_chunks.before_loop
    async def before_flush(self):
        await self.wait_until_ready()


bot = BattalionClerk()


def duty_choice(value: str) -> app_commands.Choice[str]:
    return app_commands.Choice(name=value.title(), value=value)


DUTY_CHOICES = [duty_choice(x) for x in DUTY_TYPES]


async def require_manage_guild(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not interaction.user:
        await interaction.response.send_message("This command must be used inside the battalion server.", ephemeral=True)
        return False
    perms = interaction.user.guild_permissions
    if not (perms.manage_guild or perms.administrator):
        await interaction.response.send_message("Authorization required: Manage Server or Administrator.", ephemeral=True)
        return False
    return True


@bot.tree.command(name="duty-channel", description="Assign the permanent voice channel for Training, Operation, or Meeting duty.")
@app_commands.describe(duty_type="Duty category", channel="Voice channel to monitor")
@app_commands.choices(duty_type=DUTY_CHOICES)
async def duty_channel(interaction: discord.Interaction, duty_type: app_commands.Choice[str], channel: discord.VoiceChannel):
    if not await require_manage_guild(interaction):
        return
    event_type = duty_type.value
    data = await bot.web.request("POST", "/internal/clerk/channels", json={
        "guild_id": interaction.guild_id,
        "event_type": event_type,
        "channel_id": channel.id,
        "channel_name": channel.name,
    })
    await bot.load_bindings(interaction.guild_id)
    # Start timing members already present from this moment forward.
    now = utcnow()
    for member in channel.members:
        if not member.bot:
            bot.voice_presence[(interaction.guild_id, member.id, channel.id)] = now
    await interaction.response.send_message(
        f"**{event_type.title()} duty station assigned:** {channel.mention}\nMinimum credit remains **45 minutes during a scheduled duty period**.",
        ephemeral=True,
    )


@bot.tree.command(name="duty-channel-status", description="Show the permanent battalion duty voice-channel assignments.")
async def duty_channel_status(interaction: discord.Interaction):
    if not await require_manage_guild(interaction):
        return
    rows = await bot.load_bindings(interaction.guild_id)
    by_type = {r["event_type"]: r for r in rows}
    lines = []
    for kind in DUTY_TYPES:
        row = by_type.get(kind)
        channel = interaction.guild.get_channel(int(row["channel_id"])) if row else None
        lines.append(f"**{kind.title()}** — {channel.mention if channel else 'NOT ASSIGNED'}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="schedule", description="Schedule an official Training, Operation, or Meeting duty period.")
@app_commands.describe(
    duty_type="Type of official duty",
    title="Event title",
    date="Local date: YYYY-MM-DD",
    time="Local start time: HH:MM (24-hour)",
    duration_minutes="Scheduled duration in minutes",
    operation_id="Optional website Operation UUID for combat-operation filing",
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
    if interaction.guild_id not in bot.channel_bindings:
        await bot.load_bindings(interaction.guild_id)
    channel_id = bot.channel_bindings.get(interaction.guild_id, {}).get(event_type)
    if not channel_id:
        await interaction.response.send_message(f"No {event_type.title()} voice channel is assigned. Run `/duty-channel` first.", ephemeral=True)
        return
    try:
        tz = ZoneInfo(BATTALION_TIMEZONE)
        local_start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    except Exception:
        await interaction.response.send_message("Use date `YYYY-MM-DD` and time `HH:MM` in 24-hour format.", ephemeral=True)
        return
    from datetime import timedelta
    local_end = local_start + timedelta(minutes=int(duration_minutes))
    channel = interaction.guild.get_channel(channel_id)
    external_id = f"discord:{interaction.guild_id}:{event_type}:{int(local_start.timestamp())}"
    result = await bot.web.request("POST", "/internal/clerk/events", json={
        "external_event_id": external_id,
        "event_type": event_type,
        "title": title.strip(),
        "starts_at": iso(local_start),
        "ends_at": iso(local_end),
        "channel_name": event_type.title(),
        "channel_id": channel_id,
        "operation_id": operation_id or None,
    })
    await interaction.response.send_message(
        f"**HEADQUARTERS — DUTY PERIOD FILED**\n"
        f"**{title}**\nType: **{event_type.title()}**\n"
        f"Duty Station: {channel.mention if channel else f'<#{channel_id}>'}\n"
        f"Start: <t:{int(local_start.timestamp())}:F>\nEnd: <t:{int(local_end.timestamp())}:t>\n"
        f"Credit Requirement: **45 minutes present**\n"
        f"Record No.: `{result.get('event_id')}`"
    )


@bot.tree.command(name="duty-status", description="Show current scheduled duty and each Soldier's credited voice time.")
async def duty_status(interaction: discord.Interaction):
    if not await require_manage_guild(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    # First flush live voice presence so status is current.
    await bot.flush_all_presence(guild_id=interaction.guild_id)
    result = await bot.web.request("GET", "/internal/clerk/events/status", params={"guild_id": interaction.guild_id})
    events = result.get("events", [])
    if not events:
        await interaction.followup.send("NO CURRENT DUTY PERIODS ON FILE.", ephemeral=True)
        return
    parts = []
    for event in events[:4]:
        title = event.get("title") or event.get("event_type")
        parts.append(f"**{event.get('event_type')} — {title}**")
        attendance = event.get("attendance") or []
        if not attendance:
            parts.append("No qualifying presence recorded yet.")
        else:
            for row in attendance[:25]:
                minutes = int(row.get("qualifying_seconds") or 0) // 60
                remain = max(0, 45 - minutes)
                state = "**CREDIT EARNED**" if row.get("credited_at") else f"{remain} MIN REQUIRED"
                parts.append(f"{row.get('rank_code') or ''} {row.get('last_name') or ''} — {minutes} MIN — {state}")
        parts.append("")
    await interaction.followup.send("\n".join(parts)[:1900], ephemeral=True)


@bot.tree.command(name="close-duty", description="Close an official duty period and file final attendance credit.")
@app_commands.describe(event_id="Optional event record UUID. Leave blank to close the nearest active/current event.")
async def close_duty(interaction: discord.Interaction, event_id: Optional[str] = None):
    if not await require_manage_guild(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await bot.flush_all_presence(guild_id=interaction.guild_id)
    status = await bot.web.request("GET", "/internal/clerk/events/status", params={"guild_id": interaction.guild_id})
    events = status.get("events", [])
    selected = None
    if event_id:
        selected = next((e for e in events if str(e.get("id")) == event_id.strip()), None)
        if not selected:
            await interaction.followup.send("That duty record was not found among current scheduled/active events.", ephemeral=True)
            return
    else:
        now = utcnow()
        def distance(e):
            try:
                start = datetime.fromisoformat(str(e["starts_at"]).replace("Z", "+00:00"))
                return abs((start.astimezone(timezone.utc) - now).total_seconds())
            except Exception:
                return 10**15
        if events:
            selected = sorted(events, key=distance)[0]
    if not selected:
        await interaction.followup.send("NO CURRENT DUTY PERIOD ON FILE.", ephemeral=True)
        return
    result = await bot.web.request("POST", f"/internal/clerk/events/{selected['id']}/close", json={})
    summary = result.get("summary") or {}
    await interaction.followup.send(
        f"**DUTY PERIOD CLOSED**\n{selected.get('title')}\n"
        f"Soldiers tracked: **{summary.get('tracked', 0)}**\n"
        f"Soldiers credited (45+ min): **{summary.get('credited', 0)}**",
        ephemeral=True,
    )


@bot.event
async def on_ready():
    print(f"Battalion Clerk logged in as {bot.user} ({bot.user.id if bot.user else 'unknown'})")
    for guild in bot.guilds:
        try:
            rows = await bot.load_bindings(guild.id)
            now = utcnow()
            for row in rows:
                channel = guild.get_channel(int(row["channel_id"]))
                if isinstance(channel, discord.VoiceChannel):
                    for member in channel.members:
                        if not member.bot:
                            bot.voice_presence[(guild.id, member.id, channel.id)] = now
        except Exception as exc:
            print(f"Could not load duty channels for guild {guild.id}: {exc}")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot or not member.guild:
        return
    gid = member.guild.id
    if gid not in bot.channel_bindings:
        try:
            await bot.load_bindings(gid)
        except Exception:
            return
    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None
    if before_id == after_id:
        return
    now = utcnow()
    if before_id and bot.event_type_for_channel(gid, before_id):
        key = (gid, member.id, before_id)
        started = bot.voice_presence.pop(key, None)
        if started:
            try:
                await bot.send_presence_chunk(gid, member.id, before_id, started, now)
            except Exception as exc:
                print(f"Could not file voice interval for {member.id}: {exc}")
    if after_id and bot.event_type_for_channel(gid, after_id):
        bot.voice_presence[(gid, member.id, after_id)] = now


if __name__ == "__main__":
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not WEBSITE_BASE_URL:
        missing.append("WEBSITE_BASE_URL")
    if not CLERK_SYNC_KEY:
        missing.append("CLERK_SYNC_KEY")
    if missing:
        raise SystemExit("Missing required Railway variable(s): " + ", ".join(missing))
    if not COMMAND_GUILD_ID:
        print("WARNING: Neither TEST_GUILD_ID nor GUILD_ID is set; slash commands will be synchronized globally and can take longer to appear.")
    print(f"Battalion timezone: {BATTALION_TIMEZONE}")
    print(f"Voice flush interval: {max(60, FLUSH_SECONDS)} seconds")
    print(f"Website bridge: {WEBSITE_BASE_URL}")
    bot.run(DISCORD_TOKEN)

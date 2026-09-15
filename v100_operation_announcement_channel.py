"""V100: add an explicit announcement channel to /schedule-operation.

Loaded before bot.py. At Bot.run time all commands exist, so this replaces only the
schedule-operation command while preserving Battalion Clerk's authoritative Website
scheduler, RSVP board, audit state and legacy fallback routing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

_INSTALLED = False
_ORIGINAL_RUN = commands.Bot.run


def _install_on_bot(bot: commands.Bot):
    global _INSTALLED
    if _INSTALLED:
        return
    old = bot.tree.get_command("schedule-operation")
    if old is None:
        return
    g = old.callback.__globals__

    async def schedule_operation_with_channel(
        interaction: discord.Interaction,
        title: str,
        date: str,
        time: str,
        duration_minutes: app_commands.Range[int, 45, 720],
        voice_channel: discord.VoiceChannel,
        announcement_channel: discord.TextChannel,
        kind: Optional[app_commands.Choice[str]] = None,
        area_of_operations: Optional[str] = None,
        notes: Optional[str] = None,
    ):
        if not await g["require_operation_scheduler"](interaction):
            return
        me = interaction.guild.me if interaction.guild else None
        perms = announcement_channel.permissions_for(me) if me else None
        if perms and (not perms.send_messages or not perms.embed_links):
            await interaction.response.send_message(
                f"Battalion Clerk cannot post operation notices in {announcement_channel.mention}. Grant Send Messages and Embed Links or choose another channel.",
                ephemeral=True,
            )
            return
        try:
            start_et = g["_parse_training_et"](date, time)
        except Exception as exc:
            await interaction.response.send_message(
                f"Invalid date/time: **{exc}**. Use `YYYY-MM-DD` and `20:00` or `8:00PM`.", ephemeral=True
            )
            return
        if start_et <= datetime.now(ZoneInfo(g["BATTALION_TIMEZONE"])) - timedelta(minutes=2):
            await interaction.response.send_message("Operation step-off must be in the future.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await g["web"].request("POST", "/internal/clerk/operations/schedule", json={
            "title": title[:160],
            "starts_at": start_et.astimezone(timezone.utc).isoformat(),
            "duration_minutes": int(duration_minutes),
            "channel_id": voice_channel.id,
            "channel_name": voice_channel.name,
            "area_of_operations": (area_of_operations or "")[:160],
            "notes": (notes or "")[:2000],
            "requested_by": interaction.user.display_name,
            "operation_type": (kind.value if kind else "OFFICIAL OPERATION"),
            "credit_threshold_minutes": min(45, int(duration_minutes)),
            "reminder_minutes": "1440,120,30",
        })
        if not result.get("ok", True):
            await interaction.followup.send(
                f"Operation could not be scheduled: {result.get('error','unknown error')}", ephemeral=True
            )
            return

        event = result.get("event") or {}
        event["operation_number"] = result.get("operation_number") or event.get("operation_number")
        event["operation_type"] = kind.value if kind else "OFFICIAL OPERATION"
        event["area_of_operations"] = (area_of_operations or "")[:160] or None
        event["notes"] = (notes or "")[:2000] or event.get("notes")
        event_id = str(event.get("id") or event.get("event_id") or result.get("event_id") or "")

        try:
            roster = await g["_scheduled_event_rsvps"]("OPERATION", event_id) if event_id else []
            msg = await announcement_channel.send(
                content="**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**",
                embed=g["_scheduled_event_rsvp_embed"]("OPERATION", event, roster),
                view=g["ScheduledEventRSVPView"](),
            )
            if event_id:
                await g["_save_scheduled_event_message"](
                    guild_id=interaction.guild_id,
                    message_id=msg.id,
                    channel_id=announcement_channel.id,
                    event_kind="OPERATION",
                    event_id=event_id,
                    event=event,
                    audience="BATTALION",
                )
            await g["mark_operation_schedule_notice_sent"](interaction.guild_id, event, announcement_channel.id)
        except Exception as exc:
            g["log"].exception("[V100 COMMAND OPERATION ANNOUNCEMENT FAILED] operation=%s", result.get("operation_id"))
            await interaction.followup.send(
                f"Operation was filed, but the Discord announcement could not be posted in {announcement_channel.mention}: `{str(exc)[:240]}`",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"**OPERATION SCHEDULED — {result.get('operation_number') or 'FILED'}**\n"
            f"**{title}** • <t:{int(start_et.timestamp())}:F>\n"
            f"Operation Voice: {voice_channel.mention}\n"
            f"Announcement Channel: {announcement_channel.mention}\n"
            "Attendance, reminders, HLL telemetry, M16 field-use tracking, and the website Operation record are armed.",
            ephemeral=True,
        )

    schedule_operation_with_channel = app_commands.describe(
        title="Operation / event title",
        date="Date in YYYY-MM-DD",
        time="Eastern step-off time",
        duration_minutes="Planned duration in minutes",
        voice_channel="Discord voice channel used for official operation attendance",
        announcement_channel="Text channel where the operation announcement / RSVP board should be posted",
        kind="Operation, campaign, or special event",
        area_of_operations="Optional area / map / campaign name",
        notes="Optional mission or event notes",
    )(schedule_operation_with_channel)
    schedule_operation_with_channel = app_commands.choices(kind=g["OPERATION_KIND_CHOICES"])(schedule_operation_with_channel)

    replacement = app_commands.Command(
        name="schedule-operation",
        description="Schedule and publish an Operation, campaign, or special battalion event.",
        callback=schedule_operation_with_channel,
    )
    bot.tree.remove_command("schedule-operation")
    bot.tree.add_command(replacement)

    original_post_reminder = g["post_operation_reminder"]

    async def post_operation_reminder_selected(guild: discord.Guild, event: dict, minutes_before: int):
        event_id = str(event.get("id") or event.get("event_id") or "")
        channel = None
        try:
            db = getattr(g["collector"], "db", None)
            if event_id and db and getattr(db, "pool", None):
                async with db.pool.acquire() as conn:
                    value = await conn.fetchval(
                        "SELECT channel_id FROM clerk_operation_schedule_notices WHERE guild_id=$1 AND event_id=$2",
                        str(guild.id), event_id,
                    )
                channel = guild.get_channel(int(value)) if value else None
        except Exception:
            g["log"].exception("[V100 OPERATION REMINDER CHANNEL LOOKUP FAILED] guild=%s event=%s", guild.id, event_id)
        if not isinstance(channel, discord.TextChannel):
            return await original_post_reminder(guild, event, minutes_before)
        start = g["event_timestamp"](event.get("starts_at"))
        if not start:
            return False
        title = event.get("title") or "UNNAMED OPERATION"
        duty_id = event.get("channel_id")
        duty = f"<#{duty_id}>" if duty_id else "AS DIRECTED"
        body = (
            "**HEADQUARTERS — 1ST BATTALION, 5TH CAVALRY REGIMENT**\n"
            f"**OPERATION REMINDER — {g['format_reminder_interval'](minutes_before)} TO STEP-OFF**\n\n"
            f"**{title.upper()}**\n"
            f"Step-Off: <t:{int(start.timestamp())}:F> • <t:{int(start.timestamp())}:R>\n"
            f"Duty Station: {duty}\n"
            "Official Credit Requirement: **45 qualifying minutes**\n\n"
            "**ALL AVAILABLE PERSONNEL REPORT IN ACCORDANCE WITH ASSIGNMENT.**"
        )
        await channel.send(body[:2000])
        return True

    g["post_operation_reminder"] = post_operation_reminder_selected
    g["log"].info("[V100] /schedule-operation announcement-channel selector installed")
    _INSTALLED = True


def _patched_run(self: commands.Bot, token: str, *args, **kwargs):
    _install_on_bot(self)
    return _ORIGINAL_RUN(self, token, *args, **kwargs)


commands.Bot.run = _patched_run

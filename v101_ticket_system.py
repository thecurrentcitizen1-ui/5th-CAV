from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import re
from datetime import datetime, timezone

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("battalion-clerk.tickets")

VERSION = "V101"
GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or os.getenv("GUILD_ID", "0") or 0)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

TICKET_CATEGORY_NAME = "SUPPORT TICKETS"
ARCHIVE_CHANNEL_NAME = "ticket-archive"
MAX_OPEN_TICKETS = 2

TICKET_TYPES = {
    "PERSONNEL": {
        "label": "Personnel / 201 File",
        "description": "201 File, personnel record, status, or service record issue.",
        "roles": ("S-1 Personnel", "NCO", "Command Staff"),
    },
    "ASSIGNMENT": {
        "label": "Assignment / Billet",
        "description": "Company, platoon, squad, fireteam, MOS, or billet issue.",
        "roles": ("S-1 Personnel", "NCO", "Command Staff"),
    },
    "WEBSITE": {
        "label": "Website / Login Problem",
        "description": "Login, website access, page, or account problem.",
        "roles": ("S-1 Personnel", "Command Staff"),
    },
    "DISCORD": {
        "label": "Discord Problem",
        "description": "Roles, channels, permissions, or Discord access.",
        "roles": ("Command Staff",),
    },
    "GAME_LINK": {
        "label": "Game Linking",
        "description": "HLL identity, Steam/Xbox/PS5 linking, or telemetry identity.",
        "roles": ("S-1 Personnel", "Command Staff"),
    },
    "AWARD_PROMOTION": {
        "label": "Award / Promotion",
        "description": "Promotion, award, ribbon, medal, or record question.",
        "roles": ("NCO", "S-1 Personnel", "Command Staff"),
    },
    "REPORT_MEMBER": {
        "label": "Report a Member",
        "description": "Private Command review of a member or conduct concern.",
        "roles": ("Command Staff",),
    },
    "SERVER_ADMIN": {
        "label": "Server / Admin Issue",
        "description": "Server administration, suspected cheating, or server problem.",
        "roles": ("Server Admin", "Command Staff"),
    },
    "OTHER": {
        "label": "Other",
        "description": "Anything that does not fit another support category.",
        "roles": ("NCO", "Command Staff"),
    },
}

STAFF_ROLE_NAMES = {
    "NCO", "S-1 Personnel", "Server Admin", "Command Staff",
    "Battalion Commander", "Battalion Executive Officer", "Battalion Sergeant Major",
}

_pool: asyncpg.Pool | None = None
_install_lock = asyncio.Lock()
_installed_bots: set[int] = set()


def _roles(member: discord.Member) -> set[str]:
    return {r.name for r in member.roles}


def _is_staff(member: discord.Member) -> bool:
    return bool(_roles(member) & STAFF_ROLE_NAMES)


def _is_command(member: discord.Member) -> bool:
    return bool(_roles(member) & {
        "Command Staff", "Battalion Commander", "Battalion Executive Officer", "Battalion Sergeant Major"
    })


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower())
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:32] or "soldier"


async def _db() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    return _pool


async def _ensure_schema():
    pool = await _db()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_support_tickets(
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT UNIQUE,
                opener_user_id BIGINT NOT NULL,
                opener_name TEXT NOT NULL,
                category_key TEXT NOT NULL,
                category_label TEXT NOT NULL,
                subject TEXT NOT NULL,
                opening_details TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                assigned_user_id BIGINT,
                assigned_name TEXT,
                escalated_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                closed_at TIMESTAMPTZ,
                closed_by_user_id BIGINT,
                closed_by_name TEXT,
                close_reason TEXT
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS discord_support_tickets_open_idx ON discord_support_tickets(guild_id,opener_user_id,status)"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_support_ticket_members(
                ticket_id BIGINT NOT NULL REFERENCES discord_support_tickets(id) ON DELETE CASCADE,
                discord_user_id BIGINT NOT NULL,
                added_by_user_id BIGINT,
                added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(ticket_id,discord_user_id)
            )
            """
        )


async def _ticket_for_channel(channel_id: int):
    pool = await _db()
    return await pool.fetchrow(
        "SELECT * FROM discord_support_tickets WHERE channel_id=$1",
        channel_id,
    )


async def _open_count(guild_id: int, user_id: int) -> int:
    pool = await _db()
    return int(await pool.fetchval(
        """
        SELECT COUNT(*)
          FROM discord_support_tickets
         WHERE guild_id=$1 AND opener_user_id=$2
           AND status IN ('OPEN','CLAIMED','ESCALATED')
        """,
        guild_id, user_id,
    ) or 0)


async def _support_category(guild: discord.Guild) -> discord.CategoryChannel:
    category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
    if category:
        return category
    me = guild.me
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    if me:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_messages=True,
        )
    return await guild.create_category(
        TICKET_CATEGORY_NAME,
        overwrites=overwrites,
        reason="Battalion Clerk — support ticket system",
    )


async def _archive_channel(guild: discord.Guild) -> discord.TextChannel:
    channel = discord.utils.get(guild.text_channels, name=ARCHIVE_CHANNEL_NAME)
    if channel:
        return channel
    category = await _support_category(guild)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    me = guild.me
    if me:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_messages=True,
        )
    for name in ("Command Staff", "Battalion Commander", "Battalion Executive Officer", "Battalion Sergeant Major"):
        role = discord.utils.get(guild.roles, name=name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
            )
    return await guild.create_text_channel(
        ARCHIVE_CHANNEL_NAME,
        category=category,
        overwrites=overwrites,
        reason="Battalion Clerk — ticket audit archive",
    )


def _ticket_overwrites(guild: discord.Guild, opener: discord.Member, category_key: str):
    spec = TICKET_TYPES[category_key]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True,
        ),
    }
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_messages=True,
        )
    for role_name in spec["roles"]:
        role = discord.utils.get(guild.roles, name=role_name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_messages=True,
            )
    # Battalion command always retains oversight except that this simply repeats
    # Command Staff access when the ticket itself is Command-routed.
    for role_name in ("Battalion Commander", "Battalion Executive Officer", "Battalion Sergeant Major"):
        role = discord.utils.get(guild.roles, name=role_name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_messages=True,
            )
    return overwrites


async def _create_ticket(interaction: discord.Interaction, category_key: str, subject: str, details: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Tickets can only be opened inside the 5th Cavalry Discord.", ephemeral=True)
        return

    guild = interaction.guild
    opener = interaction.user
    if await _open_count(guild.id, opener.id) >= MAX_OPEN_TICKETS:
        await interaction.response.send_message(
            f"You already have {MAX_OPEN_TICKETS} open tickets. Close or resolve one before opening another.",
            ephemeral=True,
        )
        return

    spec = TICKET_TYPES.get(category_key)
    if not spec:
        await interaction.response.send_message("That ticket category is not available.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    pool = await _db()
    ticket_id = await pool.fetchval(
        """
        INSERT INTO discord_support_tickets(
            guild_id,opener_user_id,opener_name,category_key,category_label,subject,opening_details,status
        ) VALUES($1,$2,$3,$4,$5,$6,$7,'OPEN')
        RETURNING id
        """,
        guild.id, opener.id, str(opener), category_key, spec["label"], subject.strip()[:160], details.strip()[:4000],
    )

    category = await _support_category(guild)
    channel_name = f"ticket-{int(ticket_id):04d}-{_slug(opener.display_name)}"
    try:
        channel = await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=_ticket_overwrites(guild, opener, category_key),
            topic=f"Ticket #{int(ticket_id):04d} • {spec['label']} • Opened by {opener} ({opener.id})",
            reason=f"Battalion Clerk ticket #{int(ticket_id):04d}",
        )
    except Exception:
        await pool.execute(
            "UPDATE discord_support_tickets SET status='FAILED',close_reason='Discord channel creation failed',closed_at=NOW() WHERE id=$1",
            ticket_id,
        )
        raise

    await pool.execute(
        "UPDATE discord_support_tickets SET channel_id=$1 WHERE id=$2",
        channel.id, ticket_id,
    )

    embed = discord.Embed(
        title=f"1/5 CAV SUPPORT TICKET #{int(ticket_id):04d}",
        description=details.strip()[:4000] or "No additional details supplied.",
        color=discord.Color.from_rgb(108, 116, 77),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="TYPE", value=spec["label"], inline=True)
    embed.add_field(name="OPENED BY", value=opener.mention, inline=True)
    embed.add_field(name="STATUS", value="OPEN", inline=True)
    embed.add_field(name="SUBJECT", value=subject.strip()[:1024] or "Support request", inline=False)
    embed.set_footer(text="Battalion Clerk • Private support channel")

    mention_roles = []
    for role_name in spec["roles"]:
        role = discord.utils.get(guild.roles, name=role_name)
        if role:
            mention_roles.append(role.mention)
    content = f"{opener.mention} " + " ".join(mention_roles)
    await channel.send(
        content=content.strip(),
        embed=embed,
        view=TicketControlView(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )
    await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)


class TicketDetailsModal(discord.ui.Modal):
    def __init__(self, category_key: str):
        spec = TICKET_TYPES[category_key]
        super().__init__(title=spec["label"][:45], timeout=300)
        self.category_key = category_key
        self.subject = discord.ui.TextInput(
            label="SUBJECT",
            placeholder="Short description of what you need",
            max_length=160,
            required=True,
        )
        self.details = discord.ui.TextInput(
            label="DETAILS",
            placeholder="Explain the issue and include anything staff should know.",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )
        self.add_item(self.subject)
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction):
        await _create_ticket(interaction, self.category_key, str(self.subject), str(self.details))


class TicketCategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=spec["label"][:100],
                value=key,
                description=spec["description"][:100],
            )
            for key, spec in TICKET_TYPES.items()
        ]
        super().__init__(
            placeholder="Choose the type of support you need…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketDetailsModal(self.values[0]))


class TicketCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(TicketCategorySelect())


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="OPEN TICKET",
        style=discord.ButtonStyle.primary,
        custom_id="battalion-clerk:ticket:open",
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Choose the support category that best matches your issue.",
            view=TicketCategoryView(),
            ephemeral=True,
        )


async def _claim(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
        await interaction.response.send_message("This control is for Battalion staff.", ephemeral=True)
        return
    row = await _ticket_for_channel(interaction.channel_id)
    if not row or row["status"] == "CLOSED":
        await interaction.response.send_message("This is not an open Battalion Clerk ticket.", ephemeral=True)
        return
    pool = await _db()
    await pool.execute(
        """
        UPDATE discord_support_tickets
           SET status='CLAIMED',assigned_user_id=$1,assigned_name=$2
         WHERE id=$3
        """,
        interaction.user.id, str(interaction.user), row["id"],
    )
    await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.")


async def _escalate(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
        await interaction.response.send_message("This control is for Battalion staff.", ephemeral=True)
        return
    row = await _ticket_for_channel(interaction.channel_id)
    if not row or row["status"] == "CLOSED":
        await interaction.response.send_message("This is not an open Battalion Clerk ticket.", ephemeral=True)
        return
    channel = interaction.channel
    if isinstance(channel, discord.TextChannel):
        for role_name in ("Command Staff", "Battalion Commander", "Battalion Executive Officer", "Battalion Sergeant Major"):
            role = discord.utils.get(interaction.guild.roles, name=role_name)
            if role:
                await channel.set_permissions(
                    role,
                    view_channel=True, send_messages=True, read_message_history=True, manage_messages=True,
                    reason="Battalion Clerk — ticket escalated to Command",
                )
    pool = await _db()
    await pool.execute(
        "UPDATE discord_support_tickets SET status='ESCALATED',escalated_at=NOW() WHERE id=$1",
        row["id"],
    )
    command = discord.utils.get(interaction.guild.roles, name="Command Staff")
    await interaction.response.send_message(
        f"Escalated to Battalion Command. {command.mention if command else ''}".strip(),
        allowed_mentions=discord.AllowedMentions(roles=True),
    )


async def _build_transcript(channel: discord.TextChannel, row) -> io.BytesIO:
    lines = [
        f"1/5 CAV SUPPORT TICKET #{int(row['id']):04d}",
        f"Category: {row['category_label']}",
        f"Subject: {row['subject']}",
        f"Opened by: {row['opener_name']} ({row['opener_user_id']})",
        f"Opened: {row['created_at']}",
        "",
        "MESSAGES",
        "--------",
    ]
    try:
        async for msg in channel.history(limit=None, oldest_first=True):
            stamp = msg.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            body = msg.content or ""
            if msg.attachments:
                body += (" " if body else "") + " ".join(a.url for a in msg.attachments)
            if not body and msg.embeds:
                body = "[EMBED]"
            lines.append(f"[{stamp}] {msg.author}: {body}")
    except Exception as exc:
        lines.append(f"[Transcript collection error: {exc}]")
    data = "\n".join(lines).encode("utf-8", errors="replace")
    return io.BytesIO(data)


async def _close_ticket(interaction: discord.Interaction, reason: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
        await interaction.response.send_message("This control is for Battalion staff.", ephemeral=True)
        return
    row = await _ticket_for_channel(interaction.channel_id)
    if not row or row["status"] == "CLOSED":
        await interaction.response.send_message("This ticket is already closed or not registered.", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Ticket channel could not be resolved.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    channel = interaction.channel
    transcript = await _build_transcript(channel, row)
    pool = await _db()
    await pool.execute(
        """
        UPDATE discord_support_tickets
           SET status='CLOSED',closed_at=NOW(),closed_by_user_id=$1,closed_by_name=$2,close_reason=$3
         WHERE id=$4
        """,
        interaction.user.id, str(interaction.user), reason.strip()[:1000], row["id"],
    )

    archive = await _archive_channel(interaction.guild)
    summary = discord.Embed(
        title=f"CLOSED TICKET #{int(row['id']):04d}",
        color=discord.Color.dark_green(),
        timestamp=datetime.now(timezone.utc),
    )
    summary.add_field(name="TYPE", value=row["category_label"], inline=True)
    summary.add_field(name="OPENED BY", value=f"{row['opener_name']} ({row['opener_user_id']})", inline=True)
    summary.add_field(name="CLOSED BY", value=str(interaction.user), inline=True)
    summary.add_field(name="SUBJECT", value=row["subject"][:1024], inline=False)
    summary.add_field(name="CLOSE REASON", value=reason.strip()[:1024] or "Resolved", inline=False)
    await archive.send(
        embed=summary,
        file=discord.File(transcript, filename=f"ticket-{int(row['id']):04d}-transcript.txt"),
    )

    await interaction.followup.send("Ticket closed and archived. This channel will be removed.", ephemeral=True)
    await channel.send("**TICKET CLOSED** • Transcript and audit record filed by Battalion Clerk.")
    await asyncio.sleep(3)
    await channel.delete(reason=f"Battalion Clerk ticket #{int(row['id']):04d} closed by {interaction.user}")


class TicketCloseModal(discord.ui.Modal, title="Close Support Ticket"):
    reason = discord.ui.TextInput(
        label="CLOSING NOTE",
        placeholder="Resolution / reason for closing",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await _close_ticket(interaction, str(self.reason))


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="CLAIM", style=discord.ButtonStyle.secondary, custom_id="battalion-clerk:ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _claim(interaction)

    @discord.ui.button(label="ESCALATE TO COMMAND", style=discord.ButtonStyle.primary, custom_id="battalion-clerk:ticket:escalate")
    async def escalate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _escalate(interaction)

    @discord.ui.button(label="CLOSE", style=discord.ButtonStyle.danger, custom_id="battalion-clerk:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            await interaction.response.send_message("This control is for Battalion staff.", ephemeral=True)
            return
        await interaction.response.send_modal(TicketCloseModal())


async def _install_commands(bot: commands.Bot):
    guild_obj = discord.Object(id=GUILD_ID) if GUILD_ID else None

    @app_commands.command(name="ticket", description="Open a private Battalion Clerk support ticket.")
    async def ticket(interaction: discord.Interaction):
        await interaction.response.send_message(
            "Choose the support category that best matches your issue.",
            view=TicketCategoryView(),
            ephemeral=True,
        )

    @app_commands.command(name="ticket-panel", description="Post the Battalion Clerk support ticket panel.")
    async def ticket_panel(interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_command(interaction.user):
            await interaction.response.send_message("Battalion Command authorization required.", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Run this command in the text channel where the panel should be posted.", ephemeral=True)
            return
        embed = discord.Embed(
            title="1/5 CAV • SUPPORT DESK",
            description=(
                "Need help with your personnel record, assignment, website, Discord, game linking, "
                "awards/promotions, or a server issue? Open a private ticket and Battalion Clerk will "
                "route it to the right staff."
            ),
            color=discord.Color.from_rgb(108, 116, 77),
        )
        embed.add_field(name="PRIVATE", value="Only you and the staff routed to your ticket can see it.", inline=True)
        embed.add_field(name="TRACKED", value="Every ticket receives a number and an audit record.", inline=True)
        embed.add_field(name="LIMIT", value=f"Maximum {MAX_OPEN_TICKETS} open tickets per member.", inline=True)
        await interaction.channel.send(embed=embed, view=TicketPanelView())
        await interaction.response.send_message("Support panel posted.", ephemeral=True)

    @app_commands.command(name="ticket-add", description="Add a member to the current support ticket.")
    @app_commands.describe(member="Member to add to this ticket")
    async def ticket_add(interaction: discord.Interaction, member: discord.Member):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            await interaction.response.send_message("Battalion staff authorization required.", ephemeral=True)
            return
        row = await _ticket_for_channel(interaction.channel_id)
        if not row or row["status"] == "CLOSED" or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use this command inside an open Battalion Clerk ticket.", ephemeral=True)
            return
        await interaction.channel.set_permissions(
            member,
            view_channel=True, send_messages=True, read_message_history=True,
            reason=f"Battalion Clerk ticket #{int(row['id']):04d} participant added",
        )
        pool = await _db()
        await pool.execute(
            """
            INSERT INTO discord_support_ticket_members(ticket_id,discord_user_id,added_by_user_id)
            VALUES($1,$2,$3)
            ON CONFLICT(ticket_id,discord_user_id) DO NOTHING
            """,
            row["id"], member.id, interaction.user.id,
        )
        await interaction.response.send_message(f"{member.mention} added to this ticket.")

    @app_commands.command(name="ticket-close", description="Close the current Battalion Clerk ticket.")
    @app_commands.describe(reason="Resolution or reason for closing")
    async def ticket_close(interaction: discord.Interaction, reason: str):
        await _close_ticket(interaction, reason)

    commands_to_add = [ticket, ticket_panel, ticket_add, ticket_close]
    for command in commands_to_add:
        try:
            if guild_obj:
                bot.tree.add_command(command, guild=guild_obj)
            else:
                bot.tree.add_command(command)
        except app_commands.CommandAlreadyRegistered:
            pass

    bot.add_view(TicketPanelView())
    bot.add_view(TicketControlView())

    if guild_obj:
        synced = await bot.tree.sync(guild=guild_obj)
        log.info("%s synced %s ticket commands to guild %s", VERSION, len(synced), GUILD_ID)
    else:
        synced = await bot.tree.sync()
        log.info("%s synced %s global ticket commands", VERSION, len(synced))


async def install(bot: commands.Bot):
    key = id(bot)
    if key in _installed_bots:
        return
    async with _install_lock:
        if key in _installed_bots:
            return
        await _ensure_schema()
        await _install_commands(bot)
        _installed_bots.add(key)
        log.info("%s installed: persistent Battalion Clerk support ticket system ready", VERSION)


_original_setup_hook = commands.Bot.setup_hook


async def _ticket_setup_hook(self: commands.Bot):
    await _original_setup_hook(self)
    try:
        await install(self)
    except Exception:
        log.exception("%s failed during setup", VERSION)


commands.Bot.setup_hook = _ticket_setup_hook

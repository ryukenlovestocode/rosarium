"""
Moderation cog — kick/ban/timeout, warnings, bulk-delete, channel
lock/slowmode, role management, and a mod-only snipe/editsnipe pair.

Ported from another bot into Rosarium's house style:
  - Uses config.EMBED_COLOR / config.EMBED_COLOR_DARK instead of a grab
    bag of discord.Color values, and config.FOOTER_TEXT for the footer,
    so these embeds read as part of the same bot as general.py/utility.py.
  - Role gating is config-driven instead of hardcoded IDs from the other
    server: set MOD_ROLE_ID and TRIAL_MOD_ROLE_ID in .env. Until those are
    set, only bot owners (config.OWNER_IDS, wired in bot.py) can use
    anything in this cog.
  - Commands are hybrid (slash + prefix) to match the rest of the bot,
    except `clear`/`purge`, which stays prefix-only because its argument
    is a free-form mini-syntax ("bots" / "user @x" / "contains word" / a
    number) that doesn't map cleanly onto a single slash option.

Note on snipe/editsnipe: this cog owns both commands now. They used to
also exist in utility.py (a rolling per-channel cache, open to everyone);
that version has been removed so there's a single snipe implementation.
This one only remembers the single most recent deleted/edited message per
channel, and is gated behind a mod role rather than public — a smaller,
staff-facing tool rather than a public toy. In-memory only, same as AFK
in utility.py: it doesn't need to survive a restart.

Note on warnings: also in-memory. If you want warnings to survive a
restart, swap `mod_warnings` for a storage.JSONStore instance (see
storage.py) — the interface is small enough that nothing else here
would need to change.
"""

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta

import discord
from discord.ext import commands

import config

# ---------- PERMISSION CHECKS ----------


def _member_role_ids(ctx: commands.Context) -> set[int]:
    if isinstance(ctx.author, discord.Member):
        return {r.id for r in ctx.author.roles}
    return set()


def has_any_mod_role():
    """Trial Mod, Mod, or bot owner."""

    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        allowed = {rid for rid in (config.MOD_ROLE_ID, config.TRIAL_MOD_ROLE_ID) if rid}
        if allowed & _member_role_ids(ctx):
            return True
        raise commands.CheckFailure("You don't have the required role to use this command.")

    return commands.check(predicate)


def has_mod_role():
    """Mod or bot owner only — the harsher tier of commands."""

    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        if config.MOD_ROLE_ID and config.MOD_ROLE_ID in _member_role_ids(ctx):
            return True
        raise commands.CheckFailure("This command requires the **Mod** role or higher.")

    return commands.check(predicate)


# ---------- IN-MEMORY STORES ----------
sniped_messages: dict[int, dict] = {}
edited_messages: dict[int, dict] = {}
mod_warnings: dict[int, list[dict]] = defaultdict(list)


# ---------- HELPERS ----------


def _mod_embed(title: str, color: int, fields: list[tuple]) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=datetime.utcnow())
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=config.FOOTER_TEXT)
    return embed


# ---------- COG ----------


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        # Everything in this cog assumes a guild (roles, members, channels).
        if ctx.guild is None:
            raise commands.CheckFailure("This command only works inside a server.")
        return True

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            await ctx.send(str(error), ephemeral=True)
        else:
            raise error

    # ---------- KICK (Mod only) ----------

    @commands.hybrid_command(description="Kick a member from the server.")
    @has_mod_role()
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await member.kick(reason=reason)
        await ctx.send(embed=_mod_embed(
            "👢 Member Kicked",
            config.EMBED_COLOR,
            [("Member", member.mention), ("Reason", reason), ("Moderator", ctx.author.mention)]
        ))

    # ---------- BAN (Mod only) ----------

    @commands.hybrid_command(description="Ban a member from the server.")
    @has_mod_role()
    async def ban(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await member.ban(reason=reason)
        await ctx.send(embed=_mod_embed(
            "🩸 Member Banned",
            config.EMBED_COLOR,
            [("Member", str(member)), ("Reason", reason), ("Moderator", ctx.author.mention)]
        ))

    # ---------- UNBAN (Mod only) ----------

    @commands.hybrid_command(description="Unban a user by ID.")
    @has_mod_role()
    async def unban(self, ctx: commands.Context, user_id: str):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await ctx.guild.unban(user)
            await ctx.send(embed=_mod_embed(
                "Member Unbanned",
                config.EMBED_COLOR_DARK,
                [("User", str(user)), ("Moderator", ctx.author.mention)]
            ))
        except ValueError:
            await ctx.send("That doesn't look like a valid user ID.", ephemeral=True)
        except discord.NotFound:
            await ctx.send("User not found or not banned.", ephemeral=True)

    # ---------- SOFTBAN (Mod only) ----------

    @commands.hybrid_command(description="Ban then immediately unban a member, clearing their recent messages.")
    @has_mod_role()
    async def softban(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await member.ban(reason=f"Softban: {reason}", delete_message_days=1)
        await ctx.guild.unban(member, reason="Softban unban")
        await ctx.send(embed=_mod_embed(
            "Member Softbanned",
            config.EMBED_COLOR,
            [("Member", member.mention), ("Reason", reason), ("Moderator", ctx.author.mention)]
        ))

    # ---------- TIMEOUT (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["mute"], description="Timeout a member for a number of minutes.")
    @has_any_mod_role()
    async def timeout(self, ctx: commands.Context, member: discord.Member, minutes: int, *, reason: str = "No reason provided"):
        if minutes <= 0:
            return await ctx.send("Duration must be greater than 0 minutes.", ephemeral=True)
        if minutes > 40320:
            return await ctx.send("Max timeout is **28 days** (40,320 minutes).", ephemeral=True)

        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        await member.timeout(until, reason=reason)

        h, m = divmod(minutes, 60)
        duration_str = f"{h}h {m}m" if h else f"{m}m"

        await ctx.send(embed=_mod_embed(
            "⛓️ Member Timed Out",
            config.EMBED_COLOR,
            [
                ("Member", member.mention),
                ("Duration", duration_str),
                ("Reason", reason),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- UNTIMEOUT (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["unmute", "untimeout"], description="Remove an active timeout from a member.")
    @has_any_mod_role()
    async def removetimeout(self, ctx: commands.Context, member: discord.Member):
        await member.timeout(None)
        await ctx.send(embed=_mod_embed(
            "Timeout Removed",
            config.EMBED_COLOR_DARK,
            [("Member", member.mention), ("Moderator", ctx.author.mention)]
        ))

    # ---------- WARN (Trial Mod+) ----------

    @commands.hybrid_command(description="Issue a warning to a member.")
    @has_any_mod_role()
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        entry = {
            "reason": reason,
            "moderator": str(ctx.author),
            "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }
        mod_warnings[member.id].append(entry)
        count = len(mod_warnings[member.id])

        await ctx.send(embed=_mod_embed(
            f"⚠️ Warning Issued (#{count})",
            config.EMBED_COLOR,
            [
                ("Member", member.mention),
                ("Reason", reason),
                ("Total Warnings", str(count)),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- WARNINGS (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["warns", "infractions"], description="View a member's warnings.")
    @has_any_mod_role()
    async def warnings(self, ctx: commands.Context, member: discord.Member):
        user_warns = mod_warnings.get(member.id, [])

        if not user_warns:
            return await ctx.send(f"{member.mention} has no warnings.")

        embed = discord.Embed(
            title=f"⚠️ Warnings — {member.display_name}",
            color=config.EMBED_COLOR_DARK,
            timestamp=datetime.utcnow(),
        )
        for i, w in enumerate(user_warns, 1):
            embed.add_field(
                name=f"#{i} — {w['time']}",
                value=f"{w['reason']}\nBy: {w['moderator']}",
                inline=False,
            )
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    # ---------- CLEAR WARNINGS (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["clearwarns"], description="Clear all warnings for a member.")
    @has_any_mod_role()
    async def clearwarnings(self, ctx: commands.Context, member: discord.Member):
        count = len(mod_warnings.pop(member.id, []))
        await ctx.send(embed=_mod_embed(
            "Warnings Cleared",
            config.EMBED_COLOR_DARK,
            [("Member", member.mention), ("Removed", f"{count} warning(s)"), ("Moderator", ctx.author.mention)]
        ))

    # ---------- CLEAR / PURGE (Trial Mod+) ----------
    # Prefix-only: the free-form argument ("bots" / "user @x" / "contains
    # word" / a bare number) doesn't map onto one slash option cleanly.

    @commands.command(
        name="clear",
        aliases=["purge"],
        description="Bulk delete messages. Prefix-only: also supports `bots` / `user @x` / `contains <word>`.",
    )
    @has_any_mod_role()
    async def clear(self, ctx: commands.Context, *, arg: str):
        MAX_SCAN = 100
        MAX_DELETE = 50

        await ctx.message.delete()
        arg_lower = arg.lower().strip()

        if arg_lower in ("bots", "contains bots"):
            deleted = []
            async for msg in ctx.channel.history(limit=MAX_SCAN):
                if msg.author.bot:
                    deleted.append(msg)
                    if len(deleted) >= MAX_DELETE:
                        break
            if not deleted:
                return await ctx.send("No recent bot messages found.", delete_after=3)
            await ctx.channel.delete_messages(deleted)
            return await ctx.send(f"Deleted **{len(deleted)}** bot messages.", delete_after=3)

        if arg_lower.startswith("user "):
            raw = arg[5:].strip()
            member_id = None
            if ctx.message.mentions:
                member_id = ctx.message.mentions[0].id
            else:
                try:
                    member_id = int(raw.strip("<@!>"))
                except ValueError:
                    return await ctx.send("Mention a valid user.")

            deleted = []
            async for msg in ctx.channel.history(limit=MAX_SCAN):
                if msg.author.id == member_id:
                    deleted.append(msg)
                    if len(deleted) >= MAX_DELETE:
                        break
            if not deleted:
                return await ctx.send("No recent messages from that user.", delete_after=3)
            await ctx.channel.delete_messages(deleted)
            return await ctx.send(f"Deleted **{len(deleted)}** messages from that user.", delete_after=3)

        if arg_lower.startswith("contains "):
            keyword = arg[9:].strip().lower()
            if not keyword:
                return await ctx.send("Provide a keyword.")
            deleted = []
            async for msg in ctx.channel.history(limit=MAX_SCAN):
                if keyword in msg.content.lower():
                    deleted.append(msg)
                    if len(deleted) >= MAX_DELETE:
                        break
            if not deleted:
                return await ctx.send(f"No messages found containing **'{keyword}'**.", delete_after=3)
            await ctx.channel.delete_messages(deleted)
            return await ctx.send(f"Deleted **{len(deleted)}** messages containing **'{keyword}'**.", delete_after=3)

        try:
            amount = int(arg)
        except ValueError:
            return await ctx.send(
                "Invalid usage.\n"
                f"`{config.COMMAND_PREFIX}clear <amount>`\n"
                f"`{config.COMMAND_PREFIX}clear bots`\n"
                f"`{config.COMMAND_PREFIX}clear user @member`\n"
                f"`{config.COMMAND_PREFIX}clear contains <keyword>`"
            )

        if amount <= 0 or amount > 100:
            return await ctx.send("Enter a number between 1 and 100.")

        deleted = await ctx.channel.purge(limit=amount)
        await ctx.send(f"Deleted **{len(deleted)}** messages.", delete_after=3)

    # ---------- LOCK / UNLOCK (Trial Mod+) ----------

    @commands.hybrid_command(description="Lock a channel so @everyone can't send messages.")
    @has_any_mod_role()
    async def lock(self, ctx: commands.Context, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(embed=_mod_embed(
            "🔒 Channel Locked",
            config.EMBED_COLOR,
            [("Channel", channel.mention), ("Moderator", ctx.author.mention)]
        ))

    @commands.hybrid_command(description="Unlock a previously locked channel.")
    @has_any_mod_role()
    async def unlock(self, ctx: commands.Context, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = True
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(embed=_mod_embed(
            "🔓 Channel Unlocked",
            config.EMBED_COLOR_DARK,
            [("Channel", channel.mention), ("Moderator", ctx.author.mention)]
        ))

    # ---------- SLOWMODE (Trial Mod+) ----------

    @commands.hybrid_command(description="Set a channel's slowmode delay, in seconds.")
    @has_any_mod_role()
    async def slowmode(self, ctx: commands.Context, seconds: int, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        if not (0 <= seconds <= 21600):
            return await ctx.send("Slowmode must be between **0** and **21600** seconds.", ephemeral=True)
        await channel.edit(slowmode_delay=seconds)
        msg = f"**{seconds}s** delay set." if seconds > 0 else "Slowmode **disabled**."
        await ctx.send(embed=_mod_embed(
            "🐢 Slowmode Updated",
            config.EMBED_COLOR_DARK,
            [("Channel", channel.mention), ("Delay", msg), ("Moderator", ctx.author.mention)]
        ))

    # ---------- NICK (Trial Mod+) ----------

    @commands.hybrid_command(description="Change a member's nickname.")
    @has_any_mod_role()
    async def nick(self, ctx: commands.Context, member: discord.Member, *, nickname: str = None):
        old_nick = member.display_name
        await member.edit(nick=nickname)
        await ctx.send(embed=_mod_embed(
            "Nickname Changed",
            config.EMBED_COLOR_DARK,
            [
                ("Member", member.mention),
                ("Before", old_nick),
                ("After", nickname or "*reset*"),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- USERINFO (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["ui", "whois"], description="View a member's info.")
    @has_any_mod_role()
    async def userinfo(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        roles = [r.mention for r in member.roles[1:]] or ["None"]

        embed = discord.Embed(
            title=member.display_name,
            color=member.color if member.color.value else config.EMBED_COLOR_DARK,
            timestamp=datetime.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, style="R"), inline=False)
        embed.add_field(name="Joined Server", value=discord.utils.format_dt(member.joined_at, style="R") if member.joined_at else "Unknown", inline=False)
        embed.add_field(name=f"Roles ({len(member.roles) - 1})", value=" ".join(roles[:10]) + (" …" if len(roles) > 10 else ""), inline=False)
        warns = len(mod_warnings.get(member.id, []))
        embed.add_field(name="Warnings", value=str(warns), inline=True)
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    # ---------- SERVERINFO (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["si", "server"], description="View server info.")
    @has_any_mod_role()
    async def serverinfo(self, ctx: commands.Context):
        g = ctx.guild
        embed = discord.Embed(title=g.name, color=config.EMBED_COLOR_DARK, timestamp=datetime.utcnow())
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="ID", value=str(g.id), inline=True)
        embed.add_field(name="Owner", value=g.owner.mention if g.owner else "Unknown", inline=True)
        embed.add_field(name="Members", value=str(g.member_count), inline=True)
        embed.add_field(name="Channels", value=str(len(g.channels)), inline=True)
        embed.add_field(name="Roles", value=str(len(g.roles)), inline=True)
        embed.add_field(name="Emojis", value=str(len(g.emojis)), inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, style="R"), inline=False)
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    # ---------- SNIPE (Trial Mod+) ----------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot:
            return
        sniped_messages[message.channel.id] = {
            "author": message.author,
            "content": message.content or "*[No text content]*",
            "time": datetime.utcnow(),
            "avatar": message.author.display_avatar.url,
        }

    @commands.hybrid_command(name="snipe", aliases=["s"], description="Show the last deleted message in this channel.")
    @has_any_mod_role()
    async def snipe(self, ctx: commands.Context):
        data = sniped_messages.get(ctx.channel.id)
        if not data:
            return await ctx.send("Nothing to snipe here. The dead keep their silence.")

        embed = discord.Embed(
            title="Sniped Message",
            description=data["content"],
            color=config.EMBED_COLOR_DARK,
            timestamp=data["time"],
        )
        embed.set_author(name=str(data["author"]), icon_url=data["avatar"])
        embed.set_footer(text=f"{config.FOOTER_TEXT} • deleted message")
        await ctx.send(embed=embed)

    # ---------- EDIT SNIPE (Trial Mod+) ----------

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or before.content == after.content:
            return
        edited_messages[before.channel.id] = {
            "author": before.author,
            "before": before.content or "*[No text]*",
            "after": after.content or "*[No text]*",
            "time": datetime.utcnow(),
            "avatar": before.author.display_avatar.url,
        }

    @commands.hybrid_command(name="editsnipe", aliases=["es"], description="Show the last edited message in this channel.")
    @has_any_mod_role()
    async def editsnipe(self, ctx: commands.Context):
        data = edited_messages.get(ctx.channel.id)
        if not data:
            return await ctx.send("No recently edited messages here.")

        embed = discord.Embed(title="Edit Sniped", color=config.EMBED_COLOR_DARK, timestamp=data["time"])
        embed.set_author(name=str(data["author"]), icon_url=data["avatar"])
        embed.add_field(name="Before", value=data["before"], inline=False)
        embed.add_field(name="After", value=data["after"], inline=False)
        embed.set_footer(text=f"{config.FOOTER_TEXT} • edited message")
        await ctx.send(embed=embed)

    # ---------- MODINFO ----------

    @commands.hybrid_command(description="Show what each staff tier can do.")
    @has_any_mod_role()
    async def modinfo(self, ctx: commands.Context):
        prefix = config.COMMAND_PREFIX
        mod_role = f"<@&{config.MOD_ROLE_ID}>" if config.MOD_ROLE_ID else "*(MOD_ROLE_ID not set)*"
        trial_role = f"<@&{config.TRIAL_MOD_ROLE_ID}>" if config.TRIAL_MOD_ROLE_ID else "*(TRIAL_MOD_ROLE_ID not set)*"

        embed = discord.Embed(
            title="Discipline of the Garden",
            description="A breakdown of what each staff tier can do.",
            color=config.EMBED_COLOR_DARK,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(
            name=f"Mod only — {mod_role}",
            value=(
                f"`{prefix}ban` — Permanently ban a member\n"
                f"`{prefix}unban` — Unban a user by ID\n"
                f"`{prefix}kick` — Kick a member\n"
                f"`{prefix}softban` — Ban + unban to clear messages"
            ),
            inline=False,
        )
        embed.add_field(
            name=f"Trial Mod + Mod — {trial_role} {mod_role}",
            value=(
                f"`{prefix}timeout / {prefix}mute` — Timeout a member\n"
                f"`{prefix}removetimeout / {prefix}unmute` — Remove a timeout\n"
                f"`{prefix}warn` — Issue a warning\n"
                f"`{prefix}warnings / {prefix}warns` — View a member's warnings\n"
                f"`{prefix}clearwarnings / {prefix}clearwarns` — Clear all warnings\n"
                f"`{prefix}clear / {prefix}purge` — Bulk delete messages\n"
                f"`{prefix}lock` — Lock a channel\n"
                f"`{prefix}unlock` — Unlock a channel\n"
                f"`{prefix}slowmode` — Set channel slowmode\n"
                f"`{prefix}nick` — Change a member's nickname\n"
                f"`{prefix}userinfo / {prefix}whois` — View member info\n"
                f"`{prefix}serverinfo / {prefix}server` — View server info\n"
                f"`{prefix}snipe / {prefix}s` — Snipe a deleted message\n"
                f"`{prefix}editsnipe / {prefix}es` — Snipe an edited message\n"
                f"`{prefix}modinfo` — Show this panel\n"
                f"`{prefix}newrole` — Create a new role\n"
                f"`{prefix}role setposition` — Set a role's position\n"
                f"`{prefix}rolename ch` — Rename a role\n"
                f"`{prefix}arole` — Assign a role to a user\n"
                f"`{prefix}remrole` — Remove a role from a user\n"
                f"`{prefix}rolepurge` — Strip all roles from a user\n"
                f"`{prefix}rolelist` — Paginated list of all roles"
            ),
            inline=False,
        )
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    # ══════════════════════════════════════════
    #  ROLE MANAGEMENT COMMANDS (Trial Mod+)
    # ══════════════════════════════════════════

    # ---------- NEWROLE ----------

    @commands.hybrid_command(description="Create a new role.")
    @has_any_mod_role()
    async def newrole(self, ctx: commands.Context, *, name: str):
        if not name:
            return await ctx.send(f"Usage: `{config.COMMAND_PREFIX}newrole <name>`", ephemeral=True)

        role = await ctx.guild.create_role(name=name, reason=f"Created by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Role Created",
            config.EMBED_COLOR_DARK,
            [
                ("Role", role.mention),
                ("Name", role.name),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- ROLE SETPOSITION ----------

    @commands.hybrid_group(name="role", invoke_without_command=True, description="Role management group.")
    @has_any_mod_role()
    async def role_group(self, ctx: commands.Context):
        await ctx.send(
            "Usage:\n"
            f"`{config.COMMAND_PREFIX}role setposition <@role or name> <position>` — "
            "set a role's position in the hierarchy"
        )

    @role_group.command(name="setposition", description="Set a role's position in the hierarchy.")
    @has_any_mod_role()
    async def role_setposition(self, ctx: commands.Context, role: discord.Role, position: int):
        if position < 1:
            return await ctx.send("Position must be 1 or higher.", ephemeral=True)

        max_pos = len(ctx.guild.roles) - 1
        if position > max_pos:
            return await ctx.send(f"Position can't exceed **{max_pos}** (total roles in server).", ephemeral=True)

        old_pos = role.position
        await role.edit(position=position, reason=f"Position set by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Role Position Updated",
            config.EMBED_COLOR_DARK,
            [
                ("Role", role.mention),
                ("Before", str(old_pos)),
                ("After", str(position)),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- RENAME ROLE ----------

    @commands.hybrid_command(name="rolename", description="Rename a role. Usage: ch <role> <new name>")
    @has_any_mod_role()
    async def rolename(self, ctx: commands.Context, subcommand: str, role: discord.Role, *, new_name: str):
        if subcommand.lower() != "ch":
            return await ctx.send(f"Usage: `{config.COMMAND_PREFIX}rolename ch <@role or name> <new name>`", ephemeral=True)
        if not new_name:
            return await ctx.send("Provide a new name.", ephemeral=True)

        old_name = role.name
        await role.edit(name=new_name, reason=f"Renamed by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Role Renamed",
            config.EMBED_COLOR_DARK,
            [
                ("Role", role.mention),
                ("Before", old_name),
                ("After", new_name),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- ASSIGN ROLE ----------

    @commands.hybrid_command(name="arole", description="Assign a role to a member.")
    @has_any_mod_role()
    async def arole(self, ctx: commands.Context, role: discord.Role, member: discord.Member):
        if role in member.roles:
            return await ctx.send(f"{member.mention} already has {role.mention}.", ephemeral=True)

        await member.add_roles(role, reason=f"Assigned by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Role Assigned",
            config.EMBED_COLOR_DARK,
            [
                ("Role", role.mention),
                ("Member", member.mention),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- REMOVE ROLE ----------

    @commands.hybrid_command(name="remrole", description="Remove a role from a member.")
    @has_any_mod_role()
    async def remrole(self, ctx: commands.Context, role: discord.Role, member: discord.Member):
        if role not in member.roles:
            return await ctx.send(f"{member.mention} doesn't have {role.mention}.", ephemeral=True)

        await member.remove_roles(role, reason=f"Removed by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Role Removed",
            config.EMBED_COLOR,
            [
                ("Role", role.mention),
                ("Member", member.mention),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- ROLEPURGE ----------

    @commands.hybrid_command(name="rolepurge", description="Strip all removable roles from a member.")
    @has_any_mod_role()
    async def rolepurge(self, ctx: commands.Context, member: discord.Member):
        removable = [
            r for r in member.roles
            if not r.is_default() and not r.managed
        ]

        if not removable:
            return await ctx.send(f"{member.mention} has no removable roles.", ephemeral=True)

        await member.remove_roles(*removable, reason=f"Role purge by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Roles Purged",
            config.EMBED_COLOR,
            [
                ("Member", member.mention),
                ("Removed", f"**{len(removable)}** role(s)"),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- ROLELIST ----------

    @commands.hybrid_command(name="rolelist", description="Paginated list of all roles in the server.")
    @has_any_mod_role()
    async def rolelist(self, ctx: commands.Context):
        roles = sorted(
            [r for r in ctx.guild.roles if not r.is_default()],
            key=lambda r: r.position,
            reverse=True,
        )

        if not roles:
            return await ctx.send("This server has no roles.", ephemeral=True)

        CHUNK = 15
        chunks = [roles[i:i + CHUNK] for i in range(0, len(roles), CHUNK)]
        total_pages = len(chunks)

        def make_embed(page: int) -> discord.Embed:
            chunk = chunks[page]
            lines = []
            for r in chunk:
                members = len(r.members)
                color_hex = f"#{r.color.value:06X}" if r.color.value else "#000000"
                lines.append(f"`#{r.position:>3}`  {r.mention}  ·  {members} member{'s' if members != 1 else ''}  ·  {color_hex}")

            embed = discord.Embed(
                title=f"Role List — {ctx.guild.name}",
                description="\n".join(lines),
                color=config.EMBED_COLOR_DARK,
                timestamp=datetime.utcnow(),
            )
            embed.set_footer(text=f"{config.FOOTER_TEXT} · Page {page + 1}/{total_pages} · {len(roles)} roles total")
            return embed

        current = 0
        msg = await ctx.send(embed=make_embed(current))

        if total_pages == 1:
            return

        for emoji in ["◀️", "▶️", "❌"]:
            await msg.add_reaction(emoji)

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            return (
                user.id == ctx.author.id
                and reaction.message.id == msg.id
                and str(reaction.emoji) in ["◀️", "▶️", "❌"]
            )

        while True:
            try:
                reaction, user = await ctx.bot.wait_for("reaction_add", timeout=60.0, check=check)
                emoji = str(reaction.emoji)

                if emoji == "▶️":
                    current = (current + 1) % total_pages
                elif emoji == "◀️":
                    current = (current - 1) % total_pages
                elif emoji == "❌":
                    await msg.delete()
                    return

                await msg.edit(embed=make_embed(current))

                try:
                    await msg.remove_reaction(reaction, user)
                except discord.Forbidden:
                    pass

            except asyncio.TimeoutError:
                try:
                    await msg.clear_reactions()
                except discord.Forbidden:
                    pass
                break


# ---------- SETUP ----------


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
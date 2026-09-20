"""
Welcomer cog — posts a decorated embed to a welcome channel whenever
someone joins the server.

Commands:
  welctest — [owner only] preview the welcome embed in the current
             channel using yourself as the joining member, without
             needing to actually leave and rejoin the server to test it

Set WELCOME_CHANNEL_ID in .env or this cog has nowhere to post and stays
silent (rather than crashing). The four channel-mention vars (RULES_,
UPDATES_, GENERAL_, ROLES_CHANNEL_ID) are optional on top of that — set
any of them and that line becomes a real clickable channel mention;
leave one unset and it falls back to plain "#rules"-style text instead.

Design notes:
  - The member count ("you're our 529th member!") and the timestamp
    ("Today at 9:06 AM") are NOT hardcoded — the count reads live from
    guild.member_count and the timestamp comes from Discord's own embed
    timestamp rendering (embed.timestamp), the same mechanism you see
    on every other timestamped embed. Baking in a fixed number/time
    would make this correct exactly once.
  - Colour is a soft pink rather than Rosarium's usual blood
    red/near-black brand pair (config.EMBED_COLOR / EMBED_COLOR_DARK):
    the requested content (roses/wings emoji, cursive script font,
    hearts) reads soft/kawaii rather than gothic, and forcing the brand
    red on top of that would clash. If you'd rather it match the rest
    of the bot, swap WELCOME_COLOR below for config.EMBED_COLOR.
  - WELCOME_GIF_URL is a placeholder — set it below to any direct image/
    GIF link (must be a real https URL Discord can fetch, e.g. a
    Tenor/Giphy direct link or an uploaded image URL). Left blank, the
    embed just skips the image and still looks complete.
    Note: an embed's footer can only hold small text + a tiny icon, not
    a full image/GIF — a full-width image belongs in embed.set_image(),
    which is what's used below.
"""

import discord
from discord.ext import commands

import config

# Deliberately not one of Rosarium's brand colors — see module docstring.
WELCOME_COLOR = 0xFFC0DC

# Paste a direct GIF/image URL here (or leave blank to skip the image).
WELCOME_GIF_URL = "https://static2.klipy.com/ii/c3a19a0b747a76e98651f2b9a3cca5ff/51/ff/9zcdNbN1.gif"


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 11 -> '11th', 529 -> '529th', ..."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _channel_ref(guild: discord.Guild, channel_id: int | None, fallback: str) -> str:
    """A clickable mention if the channel is configured and still exists, else plain text."""
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel is not None:
            return channel.mention
    return fallback


def _build_welcome_embed(member: discord.Member) -> discord.Embed:
    """Builds the welcome embed for a given member. Shared by on_member_join and .welctest."""
    rules = _channel_ref(member.guild, config.RULES_CHANNEL_ID, "#rules")
    notifs = _channel_ref(member.guild, config.UPDATES_CHANNEL_ID, "#notifs")
    chat = _channel_ref(member.guild, config.GENERAL_CHANNEL_ID, "#chat")
    hex_channel = _channel_ref(member.guild, config.ROLES_CHANNEL_ID, "#hex")

    member_count = _ordinal(member.guild.member_count)

    embed = discord.Embed(
        title="Welcome to Rosarium!",
        description=(
            f"welcome, {member.mention}! so glad you're here.\n\n"
            "make sure to check these channels out:\n"
            f"   • {rules}\n"
            f"   • {notifs}\n"
            f"   • {hex_channel}\n"
            f"   • {chat}\n\n"
            "thanks for joining us <3\n\n"
            f"you're our **{member_count}** member!"
        ),
        color=WELCOME_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    if WELCOME_GIF_URL:
        embed.set_image(url=WELCOME_GIF_URL)
    embed.set_footer(text=f"{config.BOT_NAME} • member #{member.guild.member_count}")
    return embed


class Welcomer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if config.WELCOME_CHANNEL_ID is None:
            return

        channel = self.bot.get_channel(config.WELCOME_CHANNEL_ID)
        if channel is None:
            return

        embed = _build_welcome_embed(member)
        await channel.send(content=member.mention, embed=embed)

    @commands.hybrid_command(
        name="welctest",
        description="[Owner only] Preview the welcome message using yourself as the joining member.",
    )
    @commands.is_owner()
    async def welctest(self, ctx: commands.Context):
        embed = _build_welcome_embed(ctx.author)
        await ctx.send(
            content=f"**Test welcome message** (posting here, not the real welcome channel):",
        )
        await ctx.send(content=ctx.author.mention, embed=embed)

    @welctest.error
    async def welctest_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            await ctx.send("This command is owner-only.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcomer(bot))
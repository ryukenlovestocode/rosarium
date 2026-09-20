"""
Fun cog — marriage system, ship calculator, and anonymous confessions.

Persistence:
  Marriages and confession records survive bot restarts via
  storage.JSONStore (data/marriages.json, data/confessions.json) — the
  same mechanism used by the economy and action-counter cogs.

Config:
  config.CONFESS_CHANNEL_ID (.env) — where public confessions post.
  config.OWNER_IDS (.env) — who gets DMed the hidden identity behind
  each confession.

Commands:
  marry <partner>          — propose marriage; partner has 60s to react
                             with ❤️ (accept) or ❌ (decline)
  divorce                  — end your current marriage
  spouse [member]          — check who someone (or yourself) is married to
  ship [user1] [user2]     — compatibility calculator between two members
                             (defaults user2 to yourself if omitted)
  confess <text>           — post an anonymous confession to
                             config.CONFESS_CHANNEL_ID; owners get DMed
                             the real identity, the public post doesn't
                             show it

Note on GIFs (WEDDING_IMAGES / HEART_IMAGES): these are placeholder
Tenor-style links carried over from the original source and have NOT
been verified as working, direct-file links (a common gotcha — see the
.welctest/GIF conversation in this project's history). Test .marry and
.ship once running; if an image shows blank/a gradient, replace it the
same way WELCOME_GIF_URL was fixed in welcomer.py (right-click the
actual GIF image itself and copy its direct link, not the page URL).
"""

import asyncio
import os
import random
from datetime import datetime

import discord
from discord.ext import commands

import config
from storage import JSONStore

MARRIAGES_PATH = os.path.join(os.path.dirname(__file__), "data", "marriages.json")
CONFESSIONS_PATH = os.path.join(os.path.dirname(__file__), "data", "confessions.json")

# NOTE: unverified placeholder links — see module docstring.
WEDDING_IMAGES = [
    "https://media.tenor.com/9p6nKxF4BzEAAAAC/anime-romance.gif",
]

HEART_IMAGES = [
    "https://media.tenor.com/zGm5acSjHCIAAAAC/heart-glow.gif",
    "https://media.tenor.com/7OZz6WmYkXgAAAAC/pixel-heart.gif",
    "https://media.tenor.com/V9q8rY3fZCkAAAAC/heart-aesthetic.gif",
]


def _ship_verdict(percent: int) -> tuple[str, discord.Color]:
    if percent >= 90:
        return "💍 Soulmates detected.", discord.Color.gold()
    elif percent >= 70:
        return "💖 Strong connection.", discord.Color.pink()
    elif percent >= 50:
        return "💫 There's something there.", discord.Color.purple()
    elif percent >= 30:
        return "😬 It's… complicated.", discord.Color.orange()
    else:
        return "💀 This ship has sunk.", discord.Color.dark_gray()


class Fun(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Structure: {"<user_id>": <partner_user_id>} — kept symmetric,
        # both directions written/removed together.
        self.marriages = JSONStore(MARRIAGES_PATH, default={})
        # Structure: {"<confession_number>": <author_user_id>}
        self.confessions = JSONStore(CONFESSIONS_PATH, default={})

    def _next_confession_number(self) -> int:
        existing = self.confessions.all()
        if not existing:
            return 1
        return max(int(n) for n in existing.keys()) + 1

    # ---------- MARRY / DIVORCE / SPOUSE ----------

    @commands.hybrid_command(description="Propose marriage to someone.")
    async def marry(self, ctx: commands.Context, partner: discord.Member = None):
        if not partner:
            return await ctx.send("Marry who? Mention someone.", ephemeral=True)
        if partner.bot:
            return await ctx.send(f"You can't marry a bot. Even {config.BOT_NAME}.")
        if partner.id == ctx.author.id:
            return await ctx.send("You can't marry yourself.")
        if self.marriages.get(ctx.author.id) is not None:
            return await ctx.send("You're already married. Loyalty first.")
        if self.marriages.get(partner.id) is not None:
            return await ctx.send("They're already taken.")

        embed = discord.Embed(
            title="💍 Marriage Proposal",
            description=(
                f"{partner.mention}\n\n"
                f"💖 **{ctx.author.mention} wants to marry you!**\n\n"
                "React with ❤️ to accept or ❌ to decline.\n"
                "*You have 60 seconds.*"
            ),
            color=discord.Color.pink(),
        )
        embed.set_image(url=random.choice(WEDDING_IMAGES))
        embed.set_footer(text=f"{config.BOT_NAME} • Love is a commitment 🌙")

        msg = await ctx.send(embed=embed)
        await msg.add_reaction("❤️")
        await msg.add_reaction("❌")

        def check(reaction, user):
            return (
                user.id == partner.id
                and reaction.message.id == msg.id
                and str(reaction.emoji) in ["❤️", "❌"]
            )

        try:
            reaction, _ = await self.bot.wait_for("reaction_add", timeout=60, check=check)
        except asyncio.TimeoutError:
            return await ctx.send("⌛ Proposal expired. Love waited too long.")

        if str(reaction.emoji) == "❤️":
            self.marriages.set(ctx.author.id, partner.id)
            self.marriages.set(partner.id, ctx.author.id)

            embed = discord.Embed(
                title="💍 Just Married!",
                description=(
                    f"🎊 {ctx.author.mention} and {partner.mention} are now married!\n\n"
                    f"*{config.BOT_NAME} blesses this union.*"
                ),
                color=discord.Color.gold(),
            )
            embed.set_footer(text=f"{config.BOT_NAME} • Congrats 💖")
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"💔 {partner.mention} said no. Pain.")

    @commands.hybrid_command(description="End your marriage.")
    async def divorce(self, ctx: commands.Context):
        partner_id = self.marriages.get(ctx.author.id)
        if partner_id is None:
            return await ctx.send("You're not even married.", ephemeral=True)

        partner = ctx.guild.get_member(partner_id)
        self.marriages.delete(ctx.author.id)
        self.marriages.delete(partner_id)

        embed = discord.Embed(
            title="💔 Divorce Finalized",
            description=(
                f"{ctx.author.mention} and "
                f"{partner.mention if partner else '*someone who left*'} "
                f"are no longer married."
            ),
            color=discord.Color.dark_gray(),
        )
        embed.set_footer(text=f"{config.BOT_NAME} • It happens 🌙")
        await ctx.send(embed=embed)

    @commands.hybrid_command(description="Check who someone is married to.")
    async def spouse(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        partner_id = self.marriages.get(member.id)

        if partner_id is None:
            return await ctx.send(f"{member.mention} is not married.")

        partner = ctx.guild.get_member(partner_id)
        embed = discord.Embed(
            description=(
                f"💍 {member.mention} is married to "
                f"{partner.mention if partner else '*someone who left the server*'}."
            ),
            color=discord.Color.pink(),
        )
        embed.set_footer(text=f"{config.BOT_NAME} • Relationship goals 🌙")
        await ctx.send(embed=embed)

    # ---------- SHIP ----------

    @commands.hybrid_command(description="Ship two people together.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ship(self, ctx: commands.Context, user1: discord.Member = None, user2: discord.Member = None):
        if not user1:
            return await ctx.send("Ship who? Mention at least one user.", ephemeral=True)

        if not user2:
            user2 = ctx.author

        if user1.id == user2.id:
            return await ctx.send("You can't ship someone with themselves.")

        percent = random.randint(1, 100)
        verdict, color = _ship_verdict(percent)

        bar_filled = round(percent / 10)
        bar = "❤️" * bar_filled + "🖤" * (10 - bar_filled)

        embed = discord.Embed(
            title="💞 Ship Calculator",
            description=f"{user1.mention} ❤️ {user2.mention}",
            color=color,
        )
        embed.add_field(name="💯 Compatibility", value=f"{bar}\n**{percent}%**", inline=False)
        embed.add_field(name="🔮 Verdict", value=verdict, inline=False)
        embed.set_image(url=random.choice(HEART_IMAGES))
        embed.set_footer(text=f"{config.BOT_NAME} • Love is risky 🌙")
        await ctx.send(embed=embed)

    # ---------- CONFESS ----------

    @commands.hybrid_command(description="Confess something anonymously.")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def confess(self, ctx: commands.Context, *, confession: str = None):
        if not confession:
            return await ctx.send("You have to actually say something.", ephemeral=True)

        if config.CONFESS_CHANNEL_ID is None:
            return await ctx.send(
                "No confession channel is configured. Set `CONFESS_CHANNEL_ID` in `.env` first.",
                ephemeral=True,
            )

        confession_channel = self.bot.get_channel(config.CONFESS_CHANNEL_ID)
        if not confession_channel:
            return await ctx.send(
                "Confession channel not found — check `CONFESS_CHANNEL_ID` in `.env`.",
                ephemeral=True,
            )

        # Delete the command message immediately to protect identity.
        # Only works for the prefix form (a slash-command invocation
        # leaves no deletable message).
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound, AttributeError):
            pass

        number = self._next_confession_number()
        self.confessions.set(number, ctx.author.id)

        public_embed = discord.Embed(
            title=f"🕯️ Anonymous Confession  ·  #{number}",
            description=f"*❝ {confession} ❞*",
            color=0x2b2d31,
            timestamp=datetime.utcnow(),
        )
        public_embed.set_footer(text=f"{config.BOT_NAME} Confessions • Identity protected 🌙")
        public_embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        await confession_channel.send(embed=public_embed)

        # Silently DM the owners with the hidden identity.
        author = ctx.author
        owner_embed = discord.Embed(
            title=f"🔍 Confession #{number} — Identity Revealed",
            description=f"*❝ {confession} ❞*",
            color=0x5865f2,
            timestamp=datetime.utcnow(),
        )
        owner_embed.add_field(
            name="👤 Sent by", value=f"{author.mention} (`{author}` · `{author.id}`)", inline=False
        )
        owner_embed.set_thumbnail(url=author.display_avatar.url)
        owner_embed.set_footer(text=f"{config.BOT_NAME} • For your eyes only 🌙")

        for owner_id in config.OWNER_IDS:
            try:
                owner = await self.bot.fetch_user(owner_id)
                await owner.send(embed=owner_embed)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass

        # Confirm quietly to the user via DM so no trace in chat.
        try:
            confirm_embed = discord.Embed(
                description="🕯️ Your confession was sent anonymously. No one can trace it back to you.",
                color=0x2b2d31,
            )
            confirm_embed.set_footer(text=f"{config.BOT_NAME} Confessions 🌙")
            await ctx.author.send(embed=confirm_embed)
        except (discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
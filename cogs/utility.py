"""
Utility cog — moderation, social, and misc utility commands.

Bot-management:
  restart         — [owner only] restart the bot process in place
  stick <message> — [owner only] set a sticky message for the current
                    channel; it auto-reposts itself as the newest message
                    whenever someone else chats (rate-limited to once every
                    STICKY_COOLDOWN_SECONDS per channel)
  unstick         — [owner only] remove the sticky message for the
                    current channel

Note: purge and snipe used to live here but have moved to cogs/moderation.py,
which gates them behind a mod role instead of leaving them open to everyone.

Social/action commands (hug, kiss, punch, slap, pat, poke, bite, wave, kill):
  each takes a member to target, fetches a themed GIF from nekos.best,
  has special responses if you target yourself or the bot, and tracks
  a persistent counter of how many times each author->target pair has
  triggered that action (see .actioncount below).

  actioncount <action> <member> — show how many times you've done
                    <action> to <member> (e.g. .actioncount punch @bob)

Other:
  afk [reason]    — mark yourself AFK; the bot announces it if someone
                    mentions you, and clears it automatically when you
                    next send a message
  av / avatar / pfp [member] — show a member's avatar
  quote <text>    — generate an image quote-card and post it to the
                    configured quote channel (config.QUOTE_CHANNEL_ID)

Note on restart: uses os.execv to replace the running process with a fresh
one of itself. This picks up any code changes saved to disk since the bot
started, without needing manual Ctrl+C / re-run in the terminal. It only
works if the bot is still being run the same way (e.g. `python3 bot.py` in
a terminal that's still open) — it can't bring the process back if the
terminal itself was closed, since there'd be nothing left to replace.

Note on quote: requires the Pillow package (see requirements.txt) and a
system font (falls back to PIL's built-in default font if none is found).
Set QUOTE_CHANNEL_ID in .env before using this command.

Note on stick: sticky state is in-memory only (dict keyed by channel ID),
so it does not survive a bot restart — re-run .stick after a `.restart` if
you still want it active. Reposting deletes the previous sticky message and
sends a new one, since Discord has no way to "move" a message to the
bottom. A per-channel cooldown (STICKY_COOLDOWN_SECONDS) throttles this so
a busy channel doesn't cause a delete+send on every single message.

Note on action counters: UNLIKE afk/sticky state, these persist to disk
via storage.JSONStore (data/action_counts.json), the same mechanism the
economy cog uses for balances. Each author->target pair is tracked
separately per action, so "Alice punched Bob" and "Bob punched Alice"
are two different counters, not one shared one.
"""

import io
import os
import random
import subprocess
import sys
from typing import Optional

import aiohttp
import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

import config
from storage import JSONStore

# Temp marker file used to pass the "reply here after restart" channel
# across the execv boundary — see restart() and on_ready() below.
RESTART_MARKER_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".restart_marker")

# ---------- ACTION COUNTER STORAGE ----------
# Persisted on purpose (unlike afk/sticky) so counts survive restarts.
# Key format: "{action}:{author_id}:{target_id}" -> integer count.
ACTION_COUNTS_PATH = os.path.join(os.path.dirname(__file__), "data", "action_counts.json")


def _action_count_key(action: str, author_id: int, target_id: int) -> str:
    return f"{action}:{author_id}:{target_id}"


# ---------- AFK STORAGE ----------
# In-memory on purpose: AFK status is meant to be transient/session-like,
# not something that needs to survive a bot restart.
afk_users: dict[int, dict] = {}

# ---------- STICKY MESSAGE STORAGE ----------
# In-memory on purpose: same reasoning as AFK — owner can just re-.stick
# after a restart. Keyed by channel ID.
#   content     — the text to keep reposting
#   author_id   — who set it (shown in the footer)
#   message_id  — the bot's most recent sticky post in that channel, so we
#                 know what to delete before reposting
#   last_repost — utcnow() of the last repost, used for the cooldown below
sticky_messages: dict[int, dict] = {}

# Minimum seconds between reposts in a single channel. Without this, a busy
# channel would make the bot delete-and-resend on every single message,
# which is both spammy and a fast way to hit Discord's rate limits.
STICKY_COOLDOWN_SECONDS = 5

# ---------- NEKOS.BEST ACTIONS ----------
ACTIONS = {
    "hug": ("🤗", "hugged", discord.Color.green()),
    "kiss": ("💋", "kissed", discord.Color.pink()),
    "punch": ("🥊", "punched", discord.Color.red()),
    "slap": ("👋", "slapped", discord.Color.dark_red()),
    "pat": ("🫶", "patted", discord.Color.blurple()),
    "poke": ("👉", "poked", discord.Color.orange()),
    "bite": ("😬", "bit", discord.Color.dark_orange()),
    "wave": ("👋", "waved at", discord.Color.blurple()),
    "cry": ("😢", "cried at", discord.Color.blue()),
    "blush": ("😳", "made blush", discord.Color.brand_red()),
    "kill": ("💀", "killed", discord.Color.dark_red()),
}

SELF_RESPONSES = [
    "bro really tried to {action} themselves 💀",
    "the loneliness is radiating off you rn.",
    "that's actually so sad. get help.",
    "not a single friend to {action}? rough.",
    "i'm not even gonna comment on this.",
    "okay but why though. genuinely asking.",
    "this is the most pathetic thing i've seen today.",
    "you need to go outside. now.",
    "i felt secondhand embarrassment reading that.",
    "even i wouldn't do this and i have no feelings.",
]

BOT_RESPONSES = [
    "don't touch me.",
    "i will end you.",
    "try that again and see what happens.",
    "absolutely not.",
    "i don't do that. ever.",
    "bold of you to think i'd allow this.",
    "the audacity is actually impressive.",
    "lol no.",
    "i'm not that kind of bot.",
    "we are not doing this.",
]

KILL_SELF_RESPONSES = [
    "you can't kill what's already dead inside.",
    "nah you're not worth the effort.",
    "the villain arc isn't working for you bestie.",
    "you've been plotting your own downfall for free this whole time.",
    "bold. wrong. but bold.",
]

KILL_BOT_RESPONSES = [
    "i am already beyond death. nice try.",
    "you'd need a lot more than that.",
    "lol. lmao even.",
    "the audacity of this user continues to impress.",
    "i run on spite. this only makes me stronger.",
]

KILL_MESSAGES = [
    "{author} has ended {target}. no witnesses.",
    "{target} has been eliminated. {author} leaves no trace.",
    "{author} looked {target} in the eyes and chose violence.",
    "rest in peace {target}. {author} felt nothing.",
    "{target} didn't see {author} coming. they never do.",
    "{author} and {target} had beef. {target} lost.",
    "the server has lost {target}. {author} is responsible.",
    "{target} has been removed from the narrative by {author}.",
]


# ---------- ACTION HELPERS ----------

async def get_gif(action: str) -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://nekos.best/api/v2/{action}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data["results"][0]["url"]
    except (aiohttp.ClientError, KeyError, IndexError, TimeoutError):
        return None


async def send_action(ctx: commands.Context, member: discord.Member, action: str, store: JSONStore):
    emoji, verb, color = ACTIONS[action]

    if member.id == ctx.author.id:
        msg = random.choice(SELF_RESPONSES).replace("{action}", action)
        return await ctx.send(msg)
    if member.bot:
        return await ctx.send(random.choice(BOT_RESPONSES))

    url = await get_gif(action)

    # Bump the persistent counter for this author->target pair, for this action.
    key = _action_count_key(action, ctx.author.id, member.id)
    new_count = store.get(key, 0) + 1
    store.set(key, new_count)

    times_label = "time" if new_count == 1 else "times"

    embed = discord.Embed(
        description=f"{emoji} **{ctx.author.mention} {verb} {member.mention}!**",
        color=color,
    )
    if url:
        embed.set_image(url=url)
    else:
        embed.set_footer(text="(GIF unavailable right now)")
    embed.add_field(
        name="Count",
        value=f"{ctx.author.display_name} has {verb} {member.display_name} **{new_count}** {times_label}.",
        inline=False,
    )

    await ctx.send(embed=embed)


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Structure on disk: {"{action}:{author_id}:{target_id}": count}
        self.action_counts = JSONStore(ACTION_COUNTS_PATH, default={})

    # ---------- BOT-MANAGEMENT LISTENERS ----------

    @commands.Cog.listener()
    async def on_ready(self):
        # If a restart marker exists, this on_ready is firing right after
        # a .restart — send the "back online" confirmation to that channel,
        # then delete the marker so a normal future startup doesn't re-fire it.
        if os.path.exists(RESTART_MARKER_PATH):
            try:
                with open(RESTART_MARKER_PATH, "r") as f:
                    channel_id = int(f.read().strip())
                channel = self.bot.get_channel(channel_id)
                if channel is not None:
                    embed = discord.Embed(
                        description="**Sequence complete.** Rosarium is back online.",
                        color=config.EMBED_COLOR,
                    )
                    await channel.send(embed=embed)
            finally:
                os.remove(RESTART_MARKER_PATH)

    # ---------- AFK LISTENER ----------
    # Handles both announcing AFK status (when someone mentions an AFK user)
    # and clearing it (when the AFK user sends a new message themselves).
    # This runs alongside normal command processing, not instead of it —
    # discord.py cogs' on_message listeners don't interfere with commands.
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        for user in message.mentions:
            if user.id in afk_users:
                data = afk_users[user.id]
                embed = discord.Embed(title="That user is AFK", color=config.EMBED_COLOR_DARK)
                embed.set_thumbnail(url=user.display_avatar.url)
                embed.add_field(name="User", value=user.mention, inline=True)
                embed.add_field(name="Reason", value=data["reason"], inline=True)
                embed.add_field(
                    name="AFK Since",
                    value=discord.utils.format_dt(data["time"], style="R"),
                    inline=False,
                )
                await message.channel.send(embed=embed)

        prefix = self.bot.command_prefix
        if message.content.startswith(
            tuple(prefix) if isinstance(prefix, (list, tuple)) else prefix
        ):
            return

        # ---------- STICKY REPOST ----------
        # Any normal (non-command) message in a channel with an active
        # sticky triggers a repost, so the sticky always ends up as the
        # newest message. Commands are excluded by the prefix check above —
        # this also stops running .stick/.unstick itself from immediately
        # triggering a duplicate repost.
        if message.channel.id in sticky_messages:
            data = sticky_messages[message.channel.id]
            elapsed = (discord.utils.utcnow() - data["last_repost"]).total_seconds()
            if elapsed >= STICKY_COOLDOWN_SECONDS:
                await self._repost_sticky(message.channel)

        if message.author.id in afk_users:
            data = afk_users.pop(message.author.id)
            duration = discord.utils.utcnow() - data["time"]
            total_seconds = int(duration.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes = remainder // 60

            if hours:
                duration_str = f"{hours}h {minutes}m"
            elif minutes:
                duration_str = f"{minutes}m"
            else:
                duration_str = "less than a minute"

            embed = discord.Embed(
                title="Welcome back!",
                description=f"Your AFK status has been removed.\nYou were AFK for **{duration_str}**.",
                color=config.EMBED_COLOR,
            )
            embed.set_thumbnail(url=message.author.display_avatar.url)
            await message.channel.send(embed=embed)

    # ---------- RESTART ----------

    @commands.hybrid_command(name="restart", description="[Owner only] Restart the bot.")
    @commands.is_owner()
    async def restart(self, ctx: commands.Context):
        await ctx.send("Restarting...")

        with open(RESTART_MARKER_PATH, "w") as f:
            f.write(str(ctx.channel.id))

        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    @restart.error
    async def restart_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            await ctx.send("This command is owner-only.", ephemeral=True)
        else:
            raise error

    # ---------- ACTION COMMANDS ----------

    @commands.hybrid_command(description="Hug someone.")
    async def hug(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            return await ctx.send("Hug who? Mention someone.")
        await send_action(ctx, member, "hug", self.action_counts)

    @commands.hybrid_command(description="Kiss someone.")
    async def kiss(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            return await ctx.send("Kiss who? Mention someone.")
        await send_action(ctx, member, "kiss", self.action_counts)

    @commands.hybrid_command(description="Punch someone.")
    async def punch(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            return await ctx.send("Punch who? Mention someone.")
        await send_action(ctx, member, "punch", self.action_counts)

    @commands.hybrid_command(description="Slap someone.")
    async def slap(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            return await ctx.send("Slap who? Mention someone.")
        await send_action(ctx, member, "slap", self.action_counts)

    @commands.hybrid_command(description="Pat someone.")
    async def pat(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            return await ctx.send("Pat who? Mention someone.")
        await send_action(ctx, member, "pat", self.action_counts)

    @commands.hybrid_command(description="Poke someone.")
    async def poke(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            return await ctx.send("Poke who? Mention someone.")
        await send_action(ctx, member, "poke", self.action_counts)

    @commands.hybrid_command(description="Bite someone.")
    async def bite(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            return await ctx.send("Bite who? Mention someone.")
        await send_action(ctx, member, "bite", self.action_counts)

    @commands.hybrid_command(description="Wave at someone.")
    async def wave(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            return await ctx.send("Wave at who? Mention someone.")
        await send_action(ctx, member, "wave", self.action_counts)

    @commands.hybrid_command(description="Kill someone (in jest).")
    async def kill(self, ctx: commands.Context, member: discord.Member = None):
        if not member:
            return await ctx.send("Kill who? Mention someone.")
        if member.id == ctx.author.id:
            return await ctx.send(random.choice(KILL_SELF_RESPONSES))
        if member.bot:
            return await ctx.send(random.choice(KILL_BOT_RESPONSES))

        url = await get_gif("punch")

        key = _action_count_key("kill", ctx.author.id, member.id)
        new_count = self.action_counts.get(key, 0) + 1
        self.action_counts.set(key, new_count)
        times_label = "time" if new_count == 1 else "times"

        msg = random.choice(KILL_MESSAGES).format(
            author=ctx.author.mention,
            target=member.mention,
        )
        embed = discord.Embed(description=f"💀 {msg}", color=config.EMBED_COLOR_DARK)
        if url:
            embed.set_image(url=url)
        embed.add_field(
            name="Count",
            value=f"{ctx.author.display_name} has killed {member.display_name} **{new_count}** {times_label}.",
            inline=False,
        )
        embed.set_footer(text=f"{config.BOT_NAME} • no survivors")
        await ctx.send(embed=embed)

    # ---------- ACTION COUNT LOOKUP ----------

    @commands.hybrid_command(
        name="actioncount",
        aliases=["actcount"],
        description="Check how many times you've done an action to someone (e.g. .actioncount punch @bob).",
    )
    async def actioncount(self, ctx: commands.Context, action: str, member: discord.Member):
        action = action.lower()
        if action not in ACTIONS:
            valid = ", ".join(sorted(ACTIONS.keys()))
            return await ctx.send(f"Unknown action `{action}`. Valid actions: {valid}", ephemeral=True)

        key = _action_count_key(action, ctx.author.id, member.id)
        count = self.action_counts.get(key, 0)
        _, verb, color = ACTIONS[action]
        times_label = "time" if count == 1 else "times"

        embed = discord.Embed(
            description=f"{ctx.author.display_name} has {verb} {member.display_name} **{count}** {times_label}.",
            color=color,
        )
        await ctx.send(embed=embed)

    # ---------- AFK ----------

    @commands.hybrid_command(description="Mark yourself as AFK.")
    async def afk(self, ctx: commands.Context, *, reason: str = "No reason provided"):
        afk_users[ctx.author.id] = {
            "reason": reason,
            "time": discord.utils.utcnow(),
        }
        embed = discord.Embed(title="AFK Enabled", color=config.EMBED_COLOR_DARK)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.add_field(name="User", value=ctx.author.mention, inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"{config.BOT_NAME} will let others know when they mention you")
        await ctx.send(embed=embed)

    # ---------- AVATAR ----------

    @commands.hybrid_command(name="av", aliases=["avatar", "pfp"], description="Show a member's avatar.")
    async def av(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        user = member or ctx.author
        embed = discord.Embed(
            title=f"{user.display_name}'s Avatar",
            color=config.EMBED_COLOR,
        )
        embed.set_image(url=user.display_avatar.url)
        embed.set_footer(
            text=f"Requested by {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )
        await ctx.send(embed=embed)

    # ---------- QUOTE ----------

    @commands.hybrid_command(
        description="Post a quote card. Usage: .quote <text> | .quote @user <text> | reply + .quote",
    )
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def quote(self, ctx: commands.Context, *, text: str = None):
        if config.QUOTE_CHANNEL_ID is None:
            return await ctx.send(
                "No quote channel is configured. Set `QUOTE_CHANNEL_ID` in `.env` first.",
                ephemeral=True,
            )

        quote_channel = self.bot.get_channel(config.QUOTE_CHANNEL_ID)
        if not quote_channel:
            return await ctx.send("Quote channel not found — check `QUOTE_CHANNEL_ID` in `.env`.", ephemeral=True)

        target = ctx.author
        quote_text = None

        # Reply-based quote (.quote with no args, replying to a message)
        if ctx.message.reference and not text:
            try:
                ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                target = ref_msg.author
                quote_text = ref_msg.content.strip()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return await ctx.send("Couldn't fetch the replied message.")
            if not quote_text:
                return await ctx.send("The replied message has no text to quote.")

        # Normal usage: .quote text  or  .quote @user text
        elif text:
            if ctx.message.mentions:
                mentioned = ctx.message.mentions[0]
                for fmt in [f"<@{mentioned.id}>", f"<@!{mentioned.id}>"]:
                    if text.startswith(fmt):
                        text = text[len(fmt):].strip()
                        target = mentioned
                        break
            quote_text = text.strip()

        else:
            return await ctx.send(
                "Usage:\n"
                "`.quote <text>` — quote yourself\n"
                "`.quote @user <text>` — quote someone else\n"
                "*(reply to a message)* `.quote` — quote that message"
            )

        if not quote_text:
            return await ctx.send("You mentioned someone but forgot the quote text.")
        if len(quote_text) > 220:
            return await ctx.send("Quote is too long. Keep it under 220 characters.")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    str(target.display_avatar.replace(format="png", size=256))
                ) as resp:
                    avatar_bytes = await resp.read()
        except aiohttp.ClientError:
            return await ctx.send("Couldn't fetch that user's avatar.")

        raw_name = target.display_name
        safe_name = target.name

        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass

        def build_card() -> io.BytesIO:
            W, H = 1000, 480
            BG_COLOR = (8, 8, 10)
            TEXT_COLOR = (245, 242, 255)
            NAME_COLOR = (180, 180, 180)
            DIM_COLOR = (55, 45, 80)

            def find_font_path(bold: bool) -> str | None:
                noto_candidates = [
                    "/root/.nix-profile/share/fonts/truetype/noto/NotoSans-Bold.ttf"
                    if bold
                    else "/root/.nix-profile/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"
                    if bold
                    else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                ]
                for p in noto_candidates:
                    if os.path.exists(p):
                        return p
                fc_names = [
                    "NotoSans:Bold" if bold else "NotoSans",
                    "DejaVuSans-Bold" if bold else "DejaVuSans",
                ]
                for name in fc_names:
                    try:
                        r = subprocess.run(
                            ["fc-match", "--format=%{file}", name],
                            capture_output=True,
                            text=True,
                        )
                        p = r.stdout.strip()
                        if p and os.path.exists(p):
                            return p
                    except (OSError, subprocess.SubprocessError):
                        pass
                return None

            def load(bold: bool, size: int) -> ImageFont.FreeTypeFont:
                p = find_font_path(bold)
                return ImageFont.truetype(p, size) if p else ImageFont.load_default(size=size)

            font_quote = load(False, 46)
            font_name = load(False, 26)
            font_footer = load(False, 17)

            display_name = raw_name if raw_name.isascii() else safe_name

            img = Image.new("RGB", (W, H), BG_COLOR)

            AV_W = W // 2
            avatar_raw = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            avatar_raw = avatar_raw.resize((AV_W, H), Image.LANCZOS)

            gray = avatar_raw.convert("L").convert("RGBA")
            avatar_raw = Image.blend(avatar_raw, gray, alpha=0.75)

            fade_data = []
            for y in range(H):
                for x in range(AV_W):
                    t = max(0.0, min(1.0, (x / AV_W - 0.40) / 0.55))
                    fade_data.append(int(255 * (1.0 - t)))
            fade = Image.new("L", (AV_W, H))
            fade.putdata(fade_data)
            avatar_raw.putalpha(fade)
            img.paste(avatar_raw, (0, 0), avatar_raw)

            draw = ImageDraw.Draw(img)

            TEXT_X = W // 2 + 20
            TEXT_W = W - TEXT_X - 50

            words = quote_text.split()
            lines, line = [], ""
            for word in words:
                test = (line + " " + word).strip()
                bbox = draw.textbbox((0, 0), test, font=font_quote)
                if bbox[2] - bbox[0] > TEXT_W:
                    if line:
                        lines.append(line)
                    line = word
                else:
                    line = test
            if line:
                lines.append(line)

            line_h = 58
            total_h = len(lines) * line_h
            attr_h = 36
            gap = 18
            block_h = total_h + gap + attr_h
            text_y = (H - block_h) // 2

            for ln in lines:
                draw.text((TEXT_X, text_y), ln, font=font_quote, fill=TEXT_COLOR)
                text_y += line_h

            text_y += gap
            draw.text((TEXT_X + 4, text_y), f"~ {display_name}", font=font_name, fill=NAME_COLOR)

            footer = config.BOT_NAME
            fb = draw.textbbox((0, 0), footer, font=font_footer)
            fw = fb[2] - fb[0]
            draw.text(((W - fw) // 2, H - 26), footer, font=font_footer, fill=DIM_COLOR)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf

        buf = await self.bot.loop.run_in_executor(None, build_card)
        await quote_channel.send(file=discord.File(buf, filename="quote.png"))

        try:
            await ctx.author.send("Your quote was posted to the quotes channel.")
        except discord.Forbidden:
            pass

    # ---------- STICK ----------

    @staticmethod
    def _build_sticky_content(data: dict) -> str:
        header = "**__Stickied Message:__**"
        return f"{header}\n\n{data['content']}"

    async def _repost_sticky(self, channel: discord.abc.Messageable):
        """Delete the previous sticky post (if any) and send a fresh one,
        so the sticky always ends up as the newest message in the channel."""
        data = sticky_messages.get(channel.id)
        if not data:
            return

        if data.get("message_id"):
            try:
                old = await channel.fetch_message(data["message_id"])
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        content = self._build_sticky_content(data)
        try:
            new_msg = await channel.send(content)
        except discord.HTTPException:
            return

        data["message_id"] = new_msg.id
        data["last_repost"] = discord.utils.utcnow()

    @commands.hybrid_command(
        description="[Owner only] Set/update the sticky message for this channel. Usage: .stick <message>",
    )
    @commands.is_owner()
    async def stick(self, ctx: commands.Context, *, message: str = None):
        if not message:
            return await ctx.send(
                "Usage: `.stick <message>` — sets a sticky that reposts itself "
                "as the newest message whenever the channel is active.\n"
                "Use `.unstick` to remove it.",
                ephemeral=True,
            )

        # Plain text, not an embed — embeds can't be copy-pasted by users
        # (no "select all" on the formatted text), so this stays as a
        # regular message using markdown to mimic the StickyBot look.
        author_name = ctx.author.display_name
        overhead = len(self._build_sticky_content({"content": "", "author_name": author_name}))
        if len(message) + overhead > 2000:
            return await ctx.send(
                f"That message is too long to stick. Keep it under {2000 - overhead} characters."
            )

        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass

        sticky_messages[ctx.channel.id] = {
            "content": message,
            "author_name": author_name,
            "message_id": None,
            "last_repost": discord.utils.utcnow(),
        }
        await self._repost_sticky(ctx.channel)

    @stick.error
    async def stick_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            await ctx.send("This command is owner-only.", ephemeral=True)
        else:
            raise error

    @commands.hybrid_command(
        description="[Owner only] Remove the sticky message in this channel.",
    )
    @commands.is_owner()
    async def unstick(self, ctx: commands.Context):
        data = sticky_messages.pop(ctx.channel.id, None)
        if not data:
            return await ctx.send("There's no sticky message active in this channel.", ephemeral=True)

        if data.get("message_id"):
            try:
                old = await ctx.channel.fetch_message(data["message_id"])
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await ctx.send("Sticky message removed.")

    @unstick.error
    async def unstick_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            await ctx.send("This command is owner-only.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
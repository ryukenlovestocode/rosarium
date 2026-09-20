"""
Help cog — the paginated command index for Rosarium.

Rewritten from the Luna-era version:
  - Every page is built from the commands that actually exist in this
    bot's cogs (general, utility, fun, economy, moderation, welcomer).
    Luna's AI / Statistics / Clans pages and her personality commands
    (fortune, cosmic, prophecy, 8ball, roast…) are gone, since nothing
    in this project implements them.
  - The prefix is read from config.COMMAND_PREFIX instead of being
    hardcoded as "$", so the menu can't drift out of sync with .env.
  - Navigation is buttons + a category dropdown rather than reactions.
    No Manage Messages permission needed, no reaction cleanup, and it
    works identically for the slash form (/help) and the prefix form.
  - Colors and copy follow Rosarium's house style (rose/thorn palette,
    config.FOOTER_TEXT, "petals" as the currency).

Maintaining it: everything lives in PAGES below — one entry per
category, each a title, a short blurb, and a list of (section, body)
fields. Add a command by editing the relevant body string. 🔒 marks an
owner-only command; ⛓️ tiers inside Moderation mirror the role checks
in moderation.py (🔴 Mod or owner, 🟡 Trial Mod and above).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import discord
from discord.ext import commands

import config

# How long the buttons stay live before they grey out.
VIEW_TIMEOUT = 150.0

# Rose / thorn palette — deliberately narrow so the pages read as one set.
ROSE = 0x8B1E3F       # deep rose
THORN = 0x2B2D31      # near-black
WINE = 0x6E1423       # dark wine
BLUSH = 0xC97B84      # muted pink
MOSS = 0x4F6D52       # garden green
EMBER = 0xA8452B      # burnt orange
IRON = 0x4A4E57       # cold grey
GOLD = 0xB08968       # antique gold

DIV = "›"


@dataclass
class Page:
    key: str
    label: str
    emoji: str
    title: str
    blurb: str
    color: int
    fields: list[tuple[str, str]] = field(default_factory=list)


def build_pages(bot: commands.Bot) -> list[Page]:
    p = config.COMMAND_PREFIX

    index = Page(
        key="index",
        label="Index",
        emoji="🟢",
        title=f"{config.BOT_NAME} — Command Index",
        blurb=(
            f"*A quiet keeper of this garden of thorns.*\n\n"
            f"> Prefix **`{p}`** · every command also works as a **slash command**\n"
            f"> Use the menu below to jump to a section, or `{p}help <section>`"
        ),
        color=ROSE,
        fields=[
            (
                "🌿  Sections",
                f"🕊️ **General** {DIV} ping, about, this menu\n"
                f"🕯️ **Utility** {DIV} actions, afk, avatars, quote cards\n"
                f"💞 **Fun** {DIV} marriage, ship, anonymous confessions\n"
                f"🌹 **Economy** {DIV} petals, daily, pay, leaderboard\n"
                f"🎲 **Casino** {DIV} coinflip, dice, wheel, fish, slots, blackjack, rob\n"
                f"⛓️ **Moderation** {DIV} staff tools, warnings, roles\n"
                f"🔒 **Owner** {DIV} restart, sticky messages, testing",
            ),
            (
                "⚡  Quick jump",
                f"`{p}help general` · `{p}help utility` · `{p}help fun`\n"
                f"`{p}help economy` · `{p}help casino` · `{p}help mod` · `{p}help owner`",
            ),
            (
                "🔖  Reading the pages",
                f"`<required>` · `[optional]` · 🔒 owner only\n"
                f"🔴 Mod or owner · 🟡 Trial Mod and above",
            ),
        ],
    )

    general = Page(
        key="general",
        label="General",
        emoji="🕊️",
        title="General",
        blurb="> Sanity checks and the menu you're reading.\n> Open to **everyone**.",
        color=IRON,
        fields=[
            (
                "Commands",
                f"`{p}help [section]` {DIV} this menu. Aliases: `{p}h` `{p}commands`\n"
                f"`{p}ping` {DIV} confirm Rosarium is awake, with current latency\n"
                f"`{p}about` {DIV} what this bot is",
            ),
        ],
    )

    utility = Page(
        key="utility",
        label="Utility",
        emoji="🕯️",
        title="Utility",
        blurb="> Actions, AFK, avatars, and quote cards.\n> Open to **everyone**.",
        color=MOSS,
        fields=[
            (
                "🤝  Actions",
                f"`{p}hug` `{p}kiss` `{p}punch` `{p}slap` `{p}pat` `{p}poke` "
                f"`{p}bite` `{p}wave` `{p}kill` — each takes `[@user]`\n"
                f"{DIV} Every action pulls a themed GIF and has its own reply if you "
                f"target yourself or Rosarium.\n"
                f"{DIV} Counts are kept per pair and survive restarts.",
            ),
            (
                "🔢  Action counter",
                f"`{p}actioncount <action> <@user>` {DIV} how many times you've done that "
                f"action to them. Alias: `{p}actcount`\n"
                f"{DIV} Example: `{p}actioncount punch @rose`",
            ),
            (
                "🌙  AFK",
                f"`{p}afk [reason]` {DIV} mark yourself away\n"
                f"┣ Rosarium replies for you when someone mentions you\n"
                f"┗ Clears itself the moment you next speak",
            ),
            (
                "🖼️  Avatar & quotes",
                f"`{p}av [@user]` {DIV} full-size avatar. Aliases: `{p}avatar` `{p}pfp`\n"
                f"`{p}quote <text>` {DIV} quote yourself on a rendered card\n"
                f"┣ `{p}quote @user <text>` — quote someone else\n"
                f"┣ *(reply to a message)* + `{p}quote` — quote that message\n"
                f"┗ *Posts to the quote channel · 15s cooldown*",
            ),
        ],
    )

    fun = Page(
        key="fun",
        label="Fun",
        emoji="💞",
        title="Fun & Social",
        blurb="> Marriage, compatibility, and things said in the dark.\n> Open to **everyone**.",
        color=BLUSH,
        fields=[
            (
                "💍  Marriage",
                f"`{p}marry <@user>` {DIV} propose — they have **60 seconds** to react "
                f"❤️ to accept or ❌ to decline\n"
                f"`{p}divorce` {DIV} end your current marriage\n"
                f"`{p}spouse [@user]` {DIV} check who someone is married to\n"
                f"┗ *One partner at a time. Marriages persist across restarts.*",
            ),
            (
                "💞  Ship",
                f"`{p}ship <@user1> [@user2]` {DIV} compatibility between two members\n"
                f"┣ Omit the second user and it ships them with you\n"
                f"┗ *5s cooldown*",
            ),
            (
                "🕯️  Confessions",
                f"`{p}confess <text>` {DIV} post anonymously to the confessions channel\n"
                f"┣ Your command message is deleted immediately\n"
                f"┣ The public post never shows who sent it\n"
                f"┣ You get a quiet DM confirming it went through\n"
                f"┗ *30s cooldown · use the prefix form so the message can be deleted*",
            ),
        ],
    )

    economy = Page(
        key="economy",
        label="Economy",
        emoji="🌹",
        title="Economy",
        blurb="> Petals: earn them, send them, lose them next page.\n> Open to **everyone**.",
        color=WINE,
        fields=[
            (
                "Commands",
                f"`{p}balance [@user]` {DIV} check a wallet. Aliases: `{p}bal` `{p}wallet` `{p}networth`\n"
                f"`{p}daily` {DIV} claim **5,000–15,000 petals**, once every 24 hours\n"
                f"`{p}pay <@user> <amount>` {DIV} transfer petals. Aliases: `{p}transfer` `{p}give`\n"
                f"`{p}leaderboard` {DIV} the ten richest in the server. Aliases: `{p}lb` `{p}top` `{p}rich`",
            ),
            (
                "🔒  Owner",
                f"`{p}addmoney <amount> [@user]` {DIV} grant petals, for testing",
            ),
        ],
    )

    casino = Page(
        key="casino",
        label="Casino",
        emoji="🎲",
        title="Casino",
        blurb=(
            "> Every game pays out in petals. **Max bet: 250,000.**\n"
            "> Rosarium is not responsible for your decisions."
        ),
        color=EMBER,
        fields=[
            (
                "🪙  Quick bets",
                f"`{p}coinflip <amount> [h/t]` {DIV} straight 50/50, defaults to heads. Alias: `{p}cf` *(8s)*\n"
                f"`{p}dice <amount> <n1> <n2>` {DIV} call both dice before the roll. Alias: `{p}d` *(10s)*\n"
                f"┗ One match **+1×** · both **+2×** · neither **−1×**",
            ),
            (
                "🎡  Luck machines",
                f"`{p}spinwheel <amount>` {DIV} the wheel, from **−4×** to **+4×**. Aliases: `{p}sw` `{p}spin` *(10s)*\n"
                f"`{p}fish <amount>` {DIV} trash **−4×** through legendary catch **+4×** *(10s)*\n"
                f"`{p}slots <amount>` {DIV} three reels. Alias: `{p}slot` *(8s)*\n"
                f"┗ 🌙🌙🌙 pays **10×** · 💎 **5×** · any two matching **0.5×**",
            ),
            (
                "🃏  Blackjack",
                f"`{p}blackjack <amount>` {DIV} play the dealer. Alias: `{p}bj`\n"
                f"┗ React 🟢 hit · 🛑 stand · ⚡ double down",
            ),
            (
                "🗡️  Rob",
                f"`{p}rob <@user>` {DIV} take your chances on someone else's wallet\n"
                f"┗ *Fail and you pay the fine instead · 60s cooldown*",
            ),
        ],
    )

    moderation = Page(
        key="moderation",
        label="Moderation",
        emoji="⛓️",
        title="Moderation",
        blurb=(
            "> Staff only, tiered by role.\n"
            "> 🔴 **Mod or owner** · 🟡 **Trial Mod and above**\n"
            f"> `{p}modinfo` shows the live breakdown for this server."
        ),
        color=THORN,
        fields=[
            (
                "🔴  Removals",
                f"`{p}kick <@user> [reason]` {DIV} remove a member — they can rejoin\n"
                f"`{p}ban <@user> [reason]` {DIV} permanent ban\n"
                f"`{p}unban <user_id>` {DIV} lift a ban by Discord ID\n"
                f"`{p}softban <@user> [reason]` {DIV} ban + instant unban, clears recent messages",
            ),
            (
                "🟡  Timeouts & warnings",
                f"`{p}timeout <@user> <minutes> [reason]` {DIV} mute. Alias: `{p}mute`\n"
                f"`{p}removetimeout <@user>` {DIV} lift it. Aliases: `{p}unmute` `{p}untimeout`\n"
                f"`{p}warn <@user> [reason]` {DIV} issue a warning\n"
                f"`{p}warnings <@user>` {DIV} view history. Aliases: `{p}warns` `{p}infractions`\n"
                f"`{p}clearwarnings <@user>` {DIV} wipe them. Alias: `{p}clearwarns`\n"
                f"┗ *Warnings are in-memory — a restart clears them.*",
            ),
            (
                "🟡  Channels & cleanup",
                f"`{p}clear <amount>` {DIV} bulk delete. Alias: `{p}purge` — **prefix only**\n"
                f"┣ `{p}clear bots` · `{p}clear user @user` · `{p}clear contains <word>`\n"
                f"`{p}lock [#channel]` · `{p}unlock [#channel]` {DIV} close or reopen a channel\n"
                f"`{p}slowmode <seconds> [#channel]` {DIV} `0` disables it\n"
                f"`{p}snipe` {DIV} last deleted message here. Alias: `{p}s`\n"
                f"`{p}editsnipe` {DIV} last edited message here. Alias: `{p}es`",
            ),
            (
                "🟡  Member & server info",
                f"`{p}nick <@user> [nickname]` {DIV} set or reset a nickname\n"
                f"`{p}userinfo [@user]` {DIV} full profile. Aliases: `{p}ui` `{p}whois`\n"
                f"`{p}serverinfo` {DIV} server stats. Aliases: `{p}si` `{p}server`\n"
                f"`{p}modinfo` {DIV} what each staff tier can do",
            ),
            (
                "🟡  Roles",
                f"`{p}newrole <name>` {DIV} create a role\n"
                f"`{p}role setposition <@role> <position>` {DIV} move it in the hierarchy\n"
                f"`{p}rolename ch <@role> <new name>` {DIV} rename it\n"
                f"`{p}arole <@role> <@user>` · `{p}remrole <@role> <@user>` {DIV} give or take\n"
                f"`{p}rolepurge <@user>` {DIV} strip every removable role\n"
                f"`{p}rolelist` {DIV} paginated list of all server roles",
            ),
        ],
    )

    owner = Page(
        key="owner",
        label="Owner",
        emoji="🔒",
        title="Owner Tools",
        blurb="> Restricted to the IDs in `OWNER_IDS`.\n> Everything here is 🔒.",
        color=GOLD,
        fields=[
            (
                "⚙️  Bot management",
                f"`{p}restart` {DIV} restart the process in place, picking up saved code changes\n"
                f"┗ *Only works if the bot is still running from an open terminal.*",
            ),
            (
                "📌  Sticky messages",
                f"`{p}stick <message>` {DIV} pin a message to the bottom of this channel\n"
                f"┣ It reposts itself as the newest message while the channel is active\n"
                f"`{p}unstick` {DIV} remove it\n"
                f"┗ *Sticky state is in-memory — re-run it after a restart.*",
            ),
            (
                "🧪  Testing",
                f"`{p}addmoney <amount> [@user]` {DIV} grant petals\n"
                f"`{p}welctest` {DIV} preview the welcome embed using yourself as the joiner",
            ),
        ],
    )

    return [index, general, utility, fun, economy, casino, moderation, owner]


# Accepted arguments for `.help <section>` → page key.
CATEGORY_ALIASES = {
    "general": "general",
    "utility": "utility",
    "util": "utility",
    "actions": "utility",
    "fun": "fun",
    "social": "fun",
    "economy": "economy",
    "eco": "economy",
    "money": "economy",
    "casino": "casino",
    "gambling": "casino",
    "gamble": "casino",
    "games": "casino",
    "moderation": "moderation",
    "mod": "moderation",
    "staff": "moderation",
    "owner": "owner",
}


def make_embed(bot: commands.Bot, pages: list[Page], index: int) -> discord.Embed:
    page = pages[index]
    embed = discord.Embed(
        title=f"{page.emoji}  {page.title}",
        description=page.blurb,
        color=page.color,
    )
    for name, value in page.fields:
        embed.add_field(name=name, value=value, inline=False)

    if bot.user is not None:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    position = "Index" if index == 0 else f"{index} / {len(pages) - 1}"
    embed.set_footer(text=f"{config.FOOTER_TEXT} · {position}")
    return embed


# ---------- NAVIGATION ----------


class CategorySelect(discord.ui.Select):
    def __init__(self, pages: list[Page]):
        super().__init__(
            placeholder="Jump to a section…",
            options=[
                discord.SelectOption(
                    label=page.label,
                    value=str(i),
                    emoji=page.emoji,
                    description=page.title if i else "Everything at a glance",
                )
                for i, page in enumerate(pages)
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: HelpView = self.view  # type: ignore[assignment]
        view.index = int(self.values[0])
        await view.refresh(interaction)


class HelpView(discord.ui.View):
    def __init__(self, bot: commands.Bot, author_id: int, pages: list[Page], start: int = 0):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.bot = bot
        self.author_id = author_id
        self.pages = pages
        self.index = start
        self.message: discord.Message | None = None
        self.add_item(CategorySelect(pages))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't yours — run the command yourself.", ephemeral=True
            )
            return False
        return True

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=make_embed(self.bot, self.pages, self.index), view=self
        )

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index - 1) % len(self.pages)
        await self.refresh(interaction)

    @discord.ui.button(emoji="🟢", label="Index", style=discord.ButtonStyle.primary, row=1)
    async def home(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        await self.refresh(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index + 1) % len(self.pages)
        await self.refresh(interaction)

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.danger, row=1)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        try:
            await interaction.response.defer()
            if self.message:
                await self.message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def on_timeout(self):
        # Grey the controls out rather than deleting — the page stays readable.
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


# ---------- COG ----------


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="help",
        aliases=["h", "commands"],
        description="Browse every Rosarium command, by section.",
    )
    async def help_command(self, ctx: commands.Context, *, section: str = None):
        pages = build_pages(self.bot)

        start = 0
        if section:
            key = CATEGORY_ALIASES.get(section.lower().strip())
            if key:
                start = next(i for i, page in enumerate(pages) if page.key == key)

        view = HelpView(self.bot, ctx.author.id, pages, start)
        view.message = await ctx.send(embed=make_embed(self.bot, pages, start), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
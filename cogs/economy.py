"""
Economy cog — persistent currency + casino games, ported from Luna's
gambling.py and adapted for Rosarium.

Data persistence:
  Balances live in data/economy.json via storage.JSONStore (atomic writes,
  survives restarts). This replaces Luna's moonlight.database module —
  same idea (get/set balance, get/set last daily claim, top balances),
  just backed by a local JSON file instead of a separate DB layer.

Ownership:
  Luna's original addmoney command checked a single hardcoded Discord
  user ID. Here it uses @commands.is_owner(), which checks against
  config.OWNER_IDS (set in .env) — no ID hardcoded in this file, and it
  automatically covers everyone listed as an owner, not just one person.

Aesthetic pass:
  Reskinned for Rosarium's rose/thorn identity instead of Luna's — the
  slot reel that used to be a 🌙 is now a 🌹 jackpot, coinflip calls
  "petal" and "thorn" instead of heads/tails, and the wheel/fish outcome
  labels read like a garden rather than a generic casino floor.
  config.EMBED_COLOR / EMBED_COLOR_DARK still drive every neutral embed
  (balance, pay, daily, loading states) so this cog stays visually
  consistent with the rest of the bot; the four *_COLOR constants below
  exist only because config doesn't have enough granularity to
  distinguish a win from a jackpot from a push, and are used for casino
  outcomes only. Every embed goes through the local _embed() helper so
  that isn't five repeated lines in every command.

  Also fixed along the way:
    - `pay` used to reuse the casino games' "Max bet is..." error text,
      which read oddly for a plain transfer. It has its own message now.
    - `leaderboard` said "the richest members of Rosarium" regardless of
      which server it ran in. It names the actual server now.
    - `addmoney` was missing the footer every other embed here has.
    - The six cooldown-gated commands (coinflip, dice, spinwheel, fish,
      slots, rob) now release your cooldown if your input was invalid
      (bad bet, bad side, self-rob, etc.) instead of quietly burning it
      on a typo. Payouts and odds are untouched — only the copy, colors,
      and this one UX papercut changed.

Commands:
  balance [user]           — check your (or someone else's) balance
  pay <user> <amount>      — send currency to another member
  daily                    — claim a once-per-day reward (randomized amount)
  leaderboard              — top 10 balances in the server
  addmoney <amount> [user] — [owner only] grant currency, for testing
  coinflip <amount> [petal/thorn] — 50/50 coinflip, defaults to petal (h/t still work)
  dice <amount> <n1> <n2>  — guess two numbers, roll two dice
  spinwheel <amount>       — wheel of fortune with big win/loss multipliers
  fish <amount>            — fish for a payout multiplier
  slots <amount>           — 3-reel slot machine
  rob <user>               — attempt to steal currency, risk of a fine
  blackjack <amount>       — reaction-button blackjack (🟢 hit, 🛑 stand, ⚡ double down)
"""

import asyncio
import os
import random
import datetime
from typing import Final

import discord
from discord.ext import commands
from discord.ext.commands import BucketType

import config
from storage import JSONStore

# ---------- CONSTANTS ----------

MAX_BET: Final = 250_000
CASINO_MAX_BET: Final = 100_000  # lower ceiling for wheel / fish / slots
DAILY_MIN: Final = 5_000
DAILY_MAX: Final = 15_000
DAILY_COOLDOWN: Final = datetime.timedelta(hours=24)

CURRENCY: Final = "petals"

# ---------- OUTCOME PALETTE ----------
# config.py only exposes one brand color and one darker variant, which
# isn't enough granularity for casino results to read at a glance. These
# four are local to this cog and used ONLY for win/loss/jackpot/push
# outcomes — every other embed here still uses config.EMBED_COLOR /
# config.EMBED_COLOR_DARK, same as the rest of the bot.
WIN_COLOR: Final = 0xD4A24E       # a win — warm gold
JACKPOT_COLOR: Final = 0xE8B023   # the best possible outcome in a given game
LOSS_COLOR: Final = 0x5C1A2E      # a loss — deep wine
PUSH_COLOR: Final = 0x4A4E57      # a tie / partial win / "nothing happened"

ROB_MIN_VICTIM_BALANCE: Final = 500    # victim needs at least this much to be worth robbing
ROB_MIN_ROBBER_BALANCE: Final = 1_000  # you need at least this much to attempt it
ROB_SUCCESS_RATE: Final = 0.20         # 20% chance of a clean theft
ROB_MAX_STOLEN: Final = 5_000          # hard cap on a single theft
ROB_FINE_MIN: Final = 500
ROB_FINE_MAX: Final = 2_000

CARD_VALUES: Final[dict[str, int]] = {
    "A": 11,
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    "7": 7, "8": 8, "9": 9, "10": 10,
    "J": 10, "Q": 10, "K": 10,
}
CARDS: Final = list(CARD_VALUES.keys())

# Wheel outcomes: (label, multiplier) — multipliers unchanged from before,
# only the flavor text was reskinned.
WHEEL_OUTCOMES: Final = [
    ("Thorns bite deep.", -4),
    ("A withering spin.", -2),
    ("A weak turn.", -1),
    ("A lucky bloom.", 1),
    ("A brilliant bloom!", 2),
    ("FULL BLOOM — JACKPOT!", 4),
]

# Fish outcomes: (label, multiplier) — same deal, multipliers unchanged.
FISH_OUTCOMES: Final = [
    ("A waterlogged thorn branch. Nothing but trouble.", -4),
    ("Just an old boot, tangled in reeds.", -2),
    ("A small silver fish.", 1),
    ("A fine catch!", 2),
    ("A glimmering prize fish!", 3),
    ("A LEGENDARY catch!", 4),
]

# Slots symbols: (symbol, weight, multiplier) — 🌙 (Luna's jackpot symbol)
# is now 🌹; 🔔 is now 🕯️ to match the confession-candle motif already
# used in fun.py. Weights and multipliers are untouched.
SLOTS_REELS: Final = [
    ("🍋", 30, 1.5),
    ("🍒", 25, 2),
    ("🕯️", 20, 2.5),
    ("⭐", 15, 3),
    ("💎", 7, 5),
    ("🌹", 3, 10),
]

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "economy.json")

# ---------- ACTIVE GAME STORE ----------
# In-memory on purpose: an active blackjack hand is transient session state,
# not something that needs to survive a restart. Only final balances (via
# self.store) get persisted.
blackjack_games: dict[int, dict] = {}


# ---------- HELPERS ----------

def hand_value(hand: list[str]) -> int:
    value = sum(CARD_VALUES[c] for c in hand)
    aces = hand.count("A")
    while value > 21 and aces:
        value -= 10
        aces -= 1
    return value


def validate_bet(amount: int, balance: int, max_bet: int = MAX_BET) -> str | None:
    """Returns an error string or None if valid."""
    if amount <= 0:
        return "Enter a positive amount."
    if amount > max_bet:
        return f"Max bet is **{max_bet:,} {CURRENCY}**."
    if amount > balance:
        return f"You don't have enough {CURRENCY}."
    return None


def _petal_bar(balance: int, max_display: int = 250_000) -> str:
    """Visual balance bar for embeds. Renamed from balance_bar and
    reskinned from 🟣⬛ to 🌹🖤 — same 10-segment shape, rose palette."""
    filled = min(10, round((balance / max_display) * 10))
    return "🌹" * filled + "🖤" * (10 - filled)


def spin_slots() -> tuple[list[str], float]:
    """
    Spins 3 slot reels using weighted random selection.
    Returns (symbols, multiplier). Multiplier 0 = loss.
    """
    symbols = [s for s, _, _ in SLOTS_REELS]
    weights = [w for _, w, _ in SLOTS_REELS]
    mult_map = {s: m for s, _, m in SLOTS_REELS}

    result = random.choices(symbols, weights=weights, k=3)

    if result[0] == result[1] == result[2]:
        return result, mult_map[result[0]]
    elif result[0] == result[1] or result[1] == result[2]:
        return result, 0.5  # partial match
    else:
        return result, 0.0  # loss


def _embed(
    title: str,
    color: int,
    *,
    description: str | None = None,
    fields: list[tuple[str, str, bool]] | None = None,
    thumbnail: str | None = None,
    footer_extra: str | None = None,
) -> discord.Embed:
    """Shared embed builder for this cog — keeps title/fields/thumbnail/
    footer construction in one place instead of repeating the same five
    lines in every command below. footer_extra appends context (e.g.
    blackjack's control hints) after the standard brand footer."""
    embed = discord.Embed(title=title, color=color)
    if description:
        embed.description = description
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    embed.set_footer(
        text=f"{config.FOOTER_TEXT} · {footer_extra}" if footer_extra else config.FOOTER_TEXT
    )
    return embed


# ---------- COG ----------

class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Structure on disk:
        # {
        #   "<user_id>": {"balance": 100, "last_daily": "2026-09-15T00:00:00+00:00"}
        # }
        self.store = JSONStore(DATA_PATH, default={})

    # ---------- Internal storage helpers (replace moonlight.database) ----------

    def _get_account(self, user_id: int) -> dict:
        account = self.store.get(user_id)
        if account is None:
            account = {"balance": 0, "last_daily": None}
            self.store.set(user_id, account)
        return account

    def get_balance(self, user_id: int) -> int:
        return self._get_account(user_id)["balance"]

    def set_balance(self, user_id: int, new_balance: int):
        account = self._get_account(user_id)
        account["balance"] = max(0, new_balance)  # never go negative
        self.store.set(user_id, account)

    def get_last_daily(self, user_id: int) -> datetime.datetime | None:
        last = self._get_account(user_id)["last_daily"]
        return datetime.datetime.fromisoformat(last) if last else None

    def set_daily_claimed_now(self, user_id: int):
        account = self._get_account(user_id)
        account["last_daily"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.store.set(user_id, account)

    def get_top_balances(self, limit: int = 10) -> list[tuple[int, int]]:
        all_accounts = self.store.all()
        ranked = sorted(
            all_accounts.items(), key=lambda item: item[1].get("balance", 0), reverse=True
        )
        return [(int(uid), acc.get("balance", 0)) for uid, acc in ranked[:limit]]

    # ---------- BALANCE ----------

    @commands.hybrid_command(aliases=["bal", "networth", "wallet"], description="Check your currency balance.")
    async def balance(self, ctx: commands.Context, member: discord.Member = None):
        user = member or ctx.author
        bal = self.get_balance(user.id)

        embed = _embed(
            "🌹 Wallet",
            config.EMBED_COLOR,
            fields=[
                ("User", user.mention, True),
                ("Server", ctx.guild.name, True),
                ("Balance", f"**{bal:,} {CURRENCY}**\n{_petal_bar(bal)}", False),
            ],
            thumbnail=user.display_avatar.url,
        )
        await ctx.send(embed=embed)

    # ---------- PAY ----------

    @commands.hybrid_command(aliases=["transfer", "give"], description="Send currency to another member.")
    async def pay(self, ctx: commands.Context, member: discord.Member, amount: int):
        if member.bot:
            return await ctx.send("You can't send currency to bots.")
        if member.id == ctx.author.id:
            return await ctx.send("You can't pay yourself.")
        if amount <= 0:
            return await ctx.send("Enter a positive amount.")

        sender_bal = self.get_balance(ctx.author.id)
        if amount > sender_bal:
            return await ctx.send(f"You don't have enough {CURRENCY} to send that much.")

        self.set_balance(ctx.author.id, sender_bal - amount)
        self.set_balance(member.id, self.get_balance(member.id) + amount)

        embed = _embed(
            "🌹 Transfer",
            config.EMBED_COLOR,
            fields=[
                ("From", ctx.author.mention, True),
                ("To", member.mention, True),
                ("Amount", f"**{amount:,} {CURRENCY}**", False),
            ],
            thumbnail=ctx.author.display_avatar.url,
        )
        await ctx.send(embed=embed)

    # ---------- DAILY ----------

    @commands.hybrid_command(description="Claim your daily currency reward.")
    async def daily(self, ctx: commands.Context):
        user_id = ctx.author.id
        now = datetime.datetime.now(datetime.timezone.utc)

        last = self.get_last_daily(user_id)
        if last is not None:
            elapsed = now - last
            remaining = DAILY_COOLDOWN - elapsed
            if remaining.total_seconds() > 0:
                h, rem = divmod(int(remaining.total_seconds()), 3600)
                m, _ = divmod(rem, 60)
                embed = _embed(
                    "Still Blooming",
                    config.EMBED_COLOR_DARK,
                    description=f"Return in **{h}h {m}m** for your next bloom.",
                )
                return await ctx.send(embed=embed)

        reward = random.randint(DAILY_MIN, DAILY_MAX)
        new_bal = self.get_balance(user_id) + reward
        self.set_balance(user_id, new_bal)
        self.set_daily_claimed_now(user_id)

        embed = _embed(
            "🌹 Daily Bloom",
            config.EMBED_COLOR,
            description=f"**+{reward:,} {CURRENCY}** bloomed into your wallet.",
            fields=[("New Balance", f"**{new_bal:,} {CURRENCY}**\n{_petal_bar(new_bal)}", False)],
            thumbnail=ctx.author.display_avatar.url,
        )
        await ctx.send(embed=embed)

    # ---------- ADD MONEY (OWNER) ----------

    @commands.hybrid_command(description="[Owner only] Add currency to a user's balance, for testing.")
    @commands.is_owner()
    async def addmoney(self, ctx: commands.Context, amount: int = 0, member: discord.Member = None):
        target = member or ctx.author
        if amount <= 0:
            return await ctx.send("Amount must be positive.", ephemeral=True)

        new_bal = self.get_balance(target.id) + amount
        self.set_balance(target.id, new_bal)

        embed = _embed(
            "🔒 Admin Grant",
            config.EMBED_COLOR,
            description=f"**+{amount:,} {CURRENCY}** → {target.mention}",
            fields=[("New Balance", f"**{new_bal:,} {CURRENCY}**\n{_petal_bar(new_bal)}", False)],
        )
        await ctx.send(embed=embed)

    @addmoney.error
    async def addmoney_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            await ctx.send("This command is owner-only.", ephemeral=True)
        else:
            raise error

    # ---------- LEADERBOARD ----------

    @commands.hybrid_command(aliases=["lb", "top", "rich"], description="Top balances in this server.")
    async def leaderboard(self, ctx: commands.Context):
        top = self.get_top_balances(10)
        if not top:
            return await ctx.send("No data yet.")

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        rank = 0
        for uid, bal in top:
            member = ctx.guild.get_member(uid)
            if member is None:
                continue  # skip users no longer in this server
            medal = medals[rank] if rank < 3 else f"`#{rank + 1}`"
            lines.append(f"{medal} {member.display_name} — **{bal:,} {CURRENCY}**")
            rank += 1

        if not lines:
            return await ctx.send("No one on the leaderboard is currently in this server.")

        embed = _embed(
            "🌹 Leaderboard",
            config.EMBED_COLOR_DARK,
            description=f"The wealthiest members of **{ctx.guild.name}**, by petal count.\n\n"
                        + "\n".join(lines),
        )
        await ctx.send(embed=embed)

    # ---------- COINFLIP ----------

    @commands.hybrid_command(aliases=["cf"], description=f"Bet some {CURRENCY} on a petal-or-thorn coinflip.")
    @commands.cooldown(1, 8, BucketType.user)
    async def coinflip(self, ctx: commands.Context, amount: int, side: str = "h"):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)
        side_input = side.lower().strip()

        if side_input in ("h", "heads", "petal", "p"):
            choice = "h"
        elif side_input in ("t", "tails", "thorn", "th"):
            choice = "t"
        else:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(
                f"Call `{ctx.clean_prefix}coinflip <amount> petal` or `thorn` — `h`/`t` still work too."
            )

        err = validate_bet(amount, balance)
        if err:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(err)

        bet_label = "🌹 Petal" if choice == "h" else "🥀 Thorn"

        msg = await ctx.send(embed=_embed(
            "Flipping...",
            config.EMBED_COLOR,
            description=f"You call **{bet_label}**.",
            thumbnail=ctx.author.display_avatar.url,
        ))
        await asyncio.sleep(1.8)

        result = random.choice(("h", "t"))
        landed_label = "🌹 Petal" if result == "h" else "🥀 Thorn"
        won = choice == result

        new_bal = balance + amount if won else balance - amount
        self.set_balance(user_id, new_bal)

        result_embed = _embed(
            f"{landed_label}!",
            WIN_COLOR if won else LOSS_COLOR,
            description=f"You called **{bet_label}**.\n{'You **WON**!' if won else 'You **LOST**...'}",
            fields=[
                ("Outcome", f"{'+' if won else '-'}{amount:,} {CURRENCY}", True),
                ("New Balance", f"**{new_bal:,}**", True),
            ],
            thumbnail=ctx.author.display_avatar.url,
        )
        await msg.edit(embed=result_embed)

    # ---------- DICE ----------

    @commands.hybrid_command(aliases=["d"], description="Bet on two dice numbers.")
    @commands.cooldown(1, 10, BucketType.user)
    async def dice(self, ctx: commands.Context, amount: int, n1: int, n2: int):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)

        err = validate_bet(amount, balance)
        if err:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(err)
        if n1 == n2:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send("The two guesses must be different.")
        if not (1 <= n1 <= 6 and 1 <= n2 <= 6):
            ctx.command.reset_cooldown(ctx)
            return await ctx.send("Dice numbers must be between **1 and 6**.")

        msg = await ctx.send(embed=_embed(
            "Rolling...",
            config.EMBED_COLOR,
            description="The dice tumble through fallen petals.",
            thumbnail=ctx.author.display_avatar.url,
        ))
        await asyncio.sleep(1.8)

        guessed = {n1, n2}
        rolled = random.sample(range(1, 7), 2)
        matches = len(guessed & set(rolled))

        if matches == 2:
            delta = amount * 2
            new_bal = balance + delta
            title, color = "Full Bloom!", JACKPOT_COLOR
            outcome = f"Both numbers matched!\n**+{delta:,} {CURRENCY}**"
        elif matches == 1:
            delta = amount
            new_bal = balance + delta
            title, color = "A Petal Caught", WIN_COLOR
            outcome = f"One number matched!\n**+{delta:,} {CURRENCY}**"
        else:
            new_bal = balance - amount
            title, color = "Thorned", LOSS_COLOR
            outcome = f"No matches.\n**-{amount:,} {CURRENCY}**"

        self.set_balance(user_id, new_bal)

        embed = _embed(
            title,
            color,
            fields=[
                ("Rolled", f"**{rolled[0]} & {rolled[1]}**", True),
                ("Guessed", f"**{n1} & {n2}**", True),
                ("Outcome", outcome, False),
                ("New Balance", f"`{new_bal:,} {CURRENCY}`", False),
            ],
            thumbnail=ctx.author.display_avatar.url,
        )
        await msg.edit(embed=embed)

    # ---------- SPIN WHEEL ----------

    @commands.hybrid_command(name="spinwheel", aliases=["sw", "spin"], description="Spin the wheel of fortune.")
    @commands.cooldown(1, 10, BucketType.user)
    async def spinwheel(self, ctx: commands.Context, amount: int):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)

        err = validate_bet(amount, balance, max_bet=CASINO_MAX_BET)
        if err:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(err)

        msg = await ctx.send(embed=_embed(
            "Spinning the Wheel...",
            config.EMBED_COLOR,
            description="The wheel turns among the thorns.",
            thumbnail=ctx.author.display_avatar.url,
        ))
        await asyncio.sleep(2)

        label, multiplier = random.choice(WHEEL_OUTCOMES)
        won = multiplier > 0
        delta = amount * abs(multiplier)
        new_bal = balance + delta if won else balance - delta
        self.set_balance(user_id, new_bal)

        best = max(m for _, m in WHEEL_OUTCOMES)
        color = LOSS_COLOR if not won else (JACKPOT_COLOR if multiplier >= best else WIN_COLOR)

        embed = _embed(
            "The Wheel Stops",
            color,
            description=label,
            fields=[
                ("Bet", f"`{amount:,} {CURRENCY}`", True),
                ("Outcome", f"{'+' if won else '-'}{delta:,} {CURRENCY}", True),
                ("New Balance", f"`{new_bal:,} {CURRENCY}`", False),
            ],
            thumbnail=ctx.author.display_avatar.url,
        )
        await msg.edit(embed=embed)

    # ---------- FISH ----------

    @commands.hybrid_command(description="Cast a line for a payout multiplier.")
    @commands.cooldown(1, 10, BucketType.user)
    async def fish(self, ctx: commands.Context, amount: int):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)

        err = validate_bet(amount, balance, max_bet=CASINO_MAX_BET)
        if err:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(err)

        msg = await ctx.send(embed=_embed(
            "Fishing...",
            config.EMBED_COLOR,
            description="A line drops into still water.",
            thumbnail=ctx.author.display_avatar.url,
            footer_extra="Will you catch treasure or trash?",
        ))
        await asyncio.sleep(2)

        label, multiplier = random.choice(FISH_OUTCOMES)
        won = multiplier > 0
        delta = amount * abs(multiplier)
        new_bal = balance + delta if won else balance - delta
        self.set_balance(user_id, new_bal)

        best = max(m for _, m in FISH_OUTCOMES)
        color = LOSS_COLOR if not won else (JACKPOT_COLOR if multiplier >= best else WIN_COLOR)

        embed = _embed(
            "Reeling It In",
            color,
            description=label,
            fields=[
                ("Bet", f"`{amount:,} {CURRENCY}`", True),
                ("Outcome", f"{'+' if won else '-'}{delta:,} {CURRENCY}", True),
                ("New Balance", f"`{new_bal:,} {CURRENCY}`", False),
            ],
            thumbnail=ctx.author.display_avatar.url,
        )
        await msg.edit(embed=embed)

    # ---------- SLOTS ----------

    @commands.hybrid_command(aliases=["slot"], description="Pull the slot machine.")
    @commands.cooldown(1, 8, BucketType.user)
    async def slots(self, ctx: commands.Context, amount: int):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)

        err = validate_bet(amount, balance, max_bet=CASINO_MAX_BET)
        if err:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(err)

        msg = await ctx.send(embed=_embed(
            "Spinning Slots...",
            config.EMBED_COLOR,
            description="🕯️ ❓ ❓ ❓ 🕯️",
            thumbnail=ctx.author.display_avatar.url,
        ))
        await asyncio.sleep(2)

        reels, multiplier = spin_slots()
        display = f"🕯️ {' | '.join(reels)} 🕯️"

        won = multiplier > 0
        if won:
            delta = int(amount * multiplier)
            new_bal = balance + delta
            if multiplier >= 5:
                title, color = "🌹 FULL BLOOM — JACKPOT!", JACKPOT_COLOR
            elif multiplier >= 3:
                title, color = "A Brilliant Pull!", WIN_COLOR
            elif multiplier == 0.5:
                title, color = "A Partial Bloom", PUSH_COLOR
            else:
                title, color = "A Small Bloom", WIN_COLOR
            outcome = f"**+{delta:,} {CURRENCY}**"
        else:
            new_bal = balance - amount
            title, color = "Withered", LOSS_COLOR
            outcome = f"**-{amount:,} {CURRENCY}**"

        self.set_balance(user_id, new_bal)

        embed = _embed(
            title,
            color,
            fields=[
                ("Reels", f"**{display}**", False),
                ("Outcome", outcome, True),
                ("New Balance", f"`{new_bal:,} {CURRENCY}`", True),
            ],
            thumbnail=ctx.author.display_avatar.url,
        )
        await msg.edit(embed=embed)

    # ---------- ROB ----------

    @commands.hybrid_command(description="Attempt to rob another user. High risk, high reward.")
    @commands.cooldown(1, 60, BucketType.user)
    async def rob(self, ctx: commands.Context, target: discord.Member):
        if target.bot:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send("You can't rob a bot.")
        if target.id == ctx.author.id:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send("You can't rob yourself.")

        robber_bal = self.get_balance(ctx.author.id)
        victim_bal = self.get_balance(target.id)

        if victim_bal < ROB_MIN_VICTIM_BALANCE:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f"{target.mention} is too broke to rob.")
        if robber_bal < ROB_MIN_ROBBER_BALANCE:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f"You need at least **{ROB_MIN_ROBBER_BALANCE:,}** to attempt a robbery.")

        success = random.random() < ROB_SUCCESS_RATE
        stolen = random.randint(100, min(ROB_MAX_STOLEN, victim_bal // 4))  # up to 25% of their balance
        fine = random.randint(ROB_FINE_MIN, ROB_FINE_MAX)

        if success:
            new_robber_bal = robber_bal + stolen
            self.set_balance(ctx.author.id, new_robber_bal)
            self.set_balance(target.id, victim_bal - stolen)
            embed = _embed(
                "A Clean Theft",
                WIN_COLOR,
                description=f"You slipped away with **{stolen:,} {CURRENCY}** from {target.mention}.",
                fields=[("New Balance", f"`{new_robber_bal:,}`", True)],
                thumbnail=ctx.author.display_avatar.url,
            )
        else:
            new_robber_bal = max(0, robber_bal - fine)
            self.set_balance(ctx.author.id, new_robber_bal)
            embed = _embed(
                "Caught Among the Thorns",
                LOSS_COLOR,
                description=f"You got caught trying to rob {target.mention} and paid a **{fine:,} {CURRENCY}** fine.",
                fields=[("New Balance", f"`{new_robber_bal:,}`", True)],
                thumbnail=ctx.author.display_avatar.url,
            )

        await ctx.send(embed=embed)

    # ---------- BLACKJACK ----------

    @commands.hybrid_command(aliases=["bj"], description="Play blackjack against the dealer.")
    async def blackjack(self, ctx: commands.Context, amount: int):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)

        err = validate_bet(amount, balance)
        if err:
            return await ctx.send(err)
        if user_id in blackjack_games:
            return await ctx.send("Finish your current blackjack game first.")

        player = random.sample(CARDS, 2)
        dealer = random.sample(CARDS, 2)
        pval = hand_value(player)

        embed = _embed(
            "🃏 Blackjack",
            config.EMBED_COLOR,
            fields=[
                ("Your Hand", f"`{' '.join(player)}` → **{pval}**", False),
                ("Dealer", f"`{dealer[0]}` ❓", False),
                ("Bet", f"`{amount:,} {CURRENCY}`", False),
            ],
            thumbnail=ctx.author.display_avatar.url,
            footer_extra="🟢 Hit  🛑 Stand  ⚡ Double Down",
        )

        msg = await ctx.send(embed=embed)
        await msg.add_reaction("🟢")
        await msg.add_reaction("🛑")
        await msg.add_reaction("⚡")

        blackjack_games[user_id] = {
            "amount": amount,
            "player": player,
            "dealer": dealer,
            "message_id": msg.id,
            "doubled": False,
        }

        if pval == 21:
            await self._resolve_blackjack(user_id, msg)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        if user.bot:
            return

        game = blackjack_games.get(user.id)
        if not game or reaction.message.id != game["message_id"]:
            return

        try:
            await reaction.remove(user)
        except (discord.Forbidden, discord.HTTPException):
            pass

        player = game["player"]
        bet = game["amount"]
        emoji = str(reaction.emoji)

        if emoji == "⚡" and not game["doubled"]:
            balance = self.get_balance(user.id)
            if balance < bet:
                return  # silently fail if can't afford doubling
            game["amount"] = bet * 2
            game["doubled"] = True
            player.append(random.choice(CARDS))
            await self._resolve_blackjack(user.id, reaction.message)
            return

        if emoji == "🟢":
            player.append(random.choice(CARDS))
            value = hand_value(player)

            if value >= 21:
                await self._resolve_blackjack(user.id, reaction.message)
                return

            embed = reaction.message.embeds[0]
            embed.set_field_at(
                0,
                name="Your Hand",
                value=f"`{' '.join(player)}` → **{value}**",
                inline=False,
            )
            await reaction.message.edit(embed=embed)
            return

        if emoji == "🛑":
            await self._resolve_blackjack(user.id, reaction.message)

    async def _resolve_blackjack(self, user_id: int, message: discord.Message) -> None:
        """Dealer plays out and resolves the blackjack game."""
        game = blackjack_games.pop(user_id, None)
        if not game:
            return

        player = game["player"]
        dealer = game["dealer"]
        bet = game["amount"]

        while hand_value(dealer) < 17:
            dealer.append(random.choice(CARDS))

        p = hand_value(player)
        d = hand_value(dealer)
        balance = self.get_balance(user_id)

        natural_bj = p == 21 and len(player) == 2

        if p > 21:
            new_bal = balance - bet
            title, color = "Bust — Thorned", LOSS_COLOR
            outcome = f"**-{bet:,} {CURRENCY}**"
        elif natural_bj and d != 21:
            payout = int(bet * 1.5)
            new_bal = balance + payout
            title, color = "🌹 Natural Blackjack!", JACKPOT_COLOR
            outcome = f"**+{payout:,} {CURRENCY}** (1.5x)"
        elif d > 21 or p > d:
            new_bal = balance + bet
            title, color = "You Win!", WIN_COLOR
            outcome = f"**+{bet:,} {CURRENCY}**"
        elif p == d:
            new_bal = balance
            title, color = "Push — Even Trade", PUSH_COLOR
            outcome = "Bet returned."
        else:
            new_bal = balance - bet
            title, color = "Dealer Wins", LOSS_COLOR
            outcome = f"**-{bet:,} {CURRENCY}**"

        self.set_balance(user_id, new_bal)

        user_obj = self.bot.get_user(user_id)

        embed = _embed(
            title,
            color,
            fields=[
                ("Your Hand", f"`{' '.join(player)}` → **{p}**", True),
                ("Dealer Hand", f"`{' '.join(dealer)}` → **{d}**", True),
                ("Outcome", outcome, False),
                ("New Balance", f"`{new_bal:,} {CURRENCY}`", False),
            ],
            thumbnail=user_obj.display_avatar.url if user_obj else None,
        )
        await message.edit(embed=embed)


# ---------- SETUP ----------

async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))

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

Commands:
  balance [user]        — check your (or someone else's) balance
  pay <user> <amount>    — send currency to another member
  daily                  — claim a once-per-day reward (randomized amount)
  leaderboard            — top 10 balances in the server
  addmoney <amount> [user] — [owner only] grant currency, for testing
  coinflip <amount> [h/t] — 50/50 coinflip, defaults to heads
  dice <amount> <n1> <n2> — guess two numbers, roll two dice
  spinwheel <amount>     — wheel of fortune with big win/loss multipliers
  fish <amount>          — fish for a payout multiplier
  slots <amount>         — 3-reel slot machine
  rob <user>             — attempt to steal currency, risk of a fine
  blackjack <amount>     — reaction-button blackjack (🟢 hit, 🛑 stand, ⚡ double down)
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
DAILY_MIN: Final = 5_000
DAILY_MAX: Final = 15_000
DAILY_COOLDOWN: Final = datetime.timedelta(hours=24)

CURRENCY: Final = "petals"

CARD_VALUES: Final[dict[str, int]] = {
    "A": 11,
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
    "7": 7, "8": 8, "9": 9, "10": 10,
    "J": 10, "Q": 10, "K": 10,
}
CARDS: Final = list(CARD_VALUES.keys())

# Wheel outcomes: (label, multiplier)
WHEEL_OUTCOMES: Final = [
    ("Total disaster!", -4),
    ("Bad spin", -2),
    ("Weak spin", -1),
    ("Lucky spin!", 1),
    ("Great spin!", 2),
    ("JACKPOT!", 4),
]

# Fish outcomes: (label, multiplier)
FISH_OUTCOMES: Final = [
    ("You fished up literal trash. x4 loss", -4),
    ("A soggy boot. x2 loss", -2),
    ("Small fish! x1 profit", 1),
    ("Nice catch! x2 profit", 2),
    ("BIG FISH! x3 profit", 3),
    ("LEGENDARY CATCH! x4 profit", 4),
]

# Slots symbols: (symbol, weight, multiplier)
SLOTS_REELS: Final = [
    ("🍋", 30, 1.5),
    ("🍒", 25, 2),
    ("🔔", 20, 2.5),
    ("⭐", 15, 3),
    ("💎", 7, 5),
    ("🌙", 3, 10),
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


def balance_bar(balance: int, max_display: int = 250_000) -> str:
    """Visual balance bar for embeds."""
    filled = min(10, round((balance / max_display) * 10))
    return "🟣" * filled + "⬛" * (10 - filled)


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

        embed = discord.Embed(
            title="Wallet",
            color=config.EMBED_COLOR,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="User", value=user.mention, inline=True)
        embed.add_field(name="Server", value=ctx.guild.name, inline=True)
        embed.add_field(
            name="Balance",
            value=f"**{bal:,} {CURRENCY}**\n{balance_bar(bal)}",
            inline=False,
        )
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    # ---------- PAY ----------

    @commands.hybrid_command(aliases=["transfer", "give"], description="Send currency to another member.")
    async def pay(self, ctx: commands.Context, member: discord.Member, amount: int):
        if member.bot:
            return await ctx.send("You can't send currency to bots.")
        if member.id == ctx.author.id:
            return await ctx.send("You can't pay yourself.")

        sender_bal = self.get_balance(ctx.author.id)
        err = validate_bet(amount, sender_bal, max_bet=sender_bal)
        if err:
            return await ctx.send(err)

        self.set_balance(ctx.author.id, sender_bal - amount)
        self.set_balance(member.id, self.get_balance(member.id) + amount)

        embed = discord.Embed(
            title="Transfer",
            color=config.EMBED_COLOR,
        )
        embed.add_field(name="From", value=ctx.author.mention, inline=True)
        embed.add_field(name="To", value=member.mention, inline=True)
        embed.add_field(name="Amount", value=f"**{amount:,} {CURRENCY}**", inline=False)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.set_footer(text=config.FOOTER_TEXT)
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
                embed = discord.Embed(
                    description=f"Come back in **{h}h {m}m**.",
                    color=config.EMBED_COLOR_DARK,
                )
                return await ctx.send(embed=embed)

        reward = random.randint(DAILY_MIN, DAILY_MAX)
        new_bal = self.get_balance(user_id) + reward
        self.set_balance(user_id, new_bal)
        self.set_daily_claimed_now(user_id)

        embed = discord.Embed(
            title="Daily Reward",
            description=f"**+{reward:,} {CURRENCY}** added to your wallet.",
            color=config.EMBED_COLOR,
        )
        embed.add_field(
            name="New Balance",
            value=f"**{new_bal:,} {CURRENCY}**\n{balance_bar(new_bal)}",
            inline=False,
        )
        embed.set_footer(text=config.FOOTER_TEXT)
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

        embed = discord.Embed(
            title="Admin Grant",
            description=f"**+{amount:,} {CURRENCY}** → {target.mention}",
            color=config.EMBED_COLOR,
        )
        embed.add_field(name="New Balance", value=f"**{new_bal:,}**", inline=False)
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

        embed = discord.Embed(
            title="Leaderboard",
            description="The richest members of Rosarium.",
            color=config.EMBED_COLOR_DARK,
        )
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

        embed.description += "\n\n" + "\n".join(lines)
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    # ---------- COINFLIP ----------

    @commands.hybrid_command(aliases=["cf"], description=f"Bet some {CURRENCY} on a 50/50 coinflip.")
    @commands.cooldown(1, 8, BucketType.user)
    async def coinflip(self, ctx: commands.Context, amount: int, side: str = "h"):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)
        side = side.lower()

        if side in ("h", "heads"):
            choice = "h"
        elif side in ("t", "tails"):
            choice = "t"
        else:
            return await ctx.send("Use `.coinflip <amount> h` or `.coinflip <amount> t`.")

        err = validate_bet(amount, balance)
        if err:
            return await ctx.send(err)

        bet_label = "Heads" if choice == "h" else "Tails"

        embed = discord.Embed(
            title="Flipping...",
            description=f"You bet on **{bet_label}**",
            color=config.EMBED_COLOR,
        )
        msg = await ctx.send(embed=embed)
        await asyncio.sleep(1.8)

        result = random.choice(("h", "t"))
        landed = "Heads" if result == "h" else "Tails"
        won = choice == result

        new_bal = balance + amount if won else balance - amount
        self.set_balance(user_id, new_bal)

        result_embed = discord.Embed(
            title=f"{landed}!",
            description=f"You bet on **{bet_label}**\n{'You **WON**!' if won else 'You **LOST**...'}",
            color=config.EMBED_COLOR if won else config.EMBED_COLOR_DARK,
        )
        result_embed.add_field(
            name="Change",
            value=f"{'+' if won else '-'}{amount:,} {CURRENCY}",
            inline=True,
        )
        result_embed.add_field(
            name="Balance",
            value=f"**{new_bal:,}**",
            inline=True,
        )
        result_embed.set_footer(text=config.FOOTER_TEXT)
        await msg.edit(embed=result_embed)

    # ---------- DICE ----------

    @commands.hybrid_command(aliases=["d"], description="Bet on two dice numbers.")
    @commands.cooldown(1, 10, BucketType.user)
    async def dice(self, ctx: commands.Context, amount: int, n1: int, n2: int):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)

        err = validate_bet(amount, balance)
        if err:
            return await ctx.send(err)
        if n1 == n2:
            return await ctx.send("The two guesses must be different.")
        if not (1 <= n1 <= 6 and 1 <= n2 <= 6):
            return await ctx.send("Dice numbers must be between **1 and 6**.")

        loading = discord.Embed(
            title="Rolling...",
            description="The dice tumble across the table.",
            color=config.EMBED_COLOR,
        )
        msg = await ctx.send(embed=loading)
        await asyncio.sleep(1.8)

        guessed = {n1, n2}
        rolled = random.sample(range(1, 7), 2)
        matches = len(guessed & set(rolled))

        if matches == 2:
            delta = amount * 2
            new_bal = balance + delta
            title, color = "JACKPOT!", config.EMBED_COLOR
            result = f"Both numbers matched!\n**+{delta:,} {CURRENCY}**"
        elif matches == 1:
            delta = amount
            new_bal = balance + delta
            title, color = "You Won!", config.EMBED_COLOR
            result = f"One number matched!\n**+{delta:,} {CURRENCY}**"
        else:
            new_bal = balance - amount
            title, color = "You Lost", config.EMBED_COLOR_DARK
            result = f"No matches.\n**-{amount:,} {CURRENCY}**"

        self.set_balance(user_id, new_bal)

        embed = discord.Embed(title=title, color=color)
        embed.add_field(name="Rolled", value=f"**{rolled[0]} & {rolled[1]}**", inline=True)
        embed.add_field(name="Guessed", value=f"**{n1} & {n2}**", inline=True)
        embed.add_field(name="Result", value=result, inline=False)
        embed.add_field(name="New Balance", value=f"`{new_bal:,} {CURRENCY}`", inline=False)
        embed.set_footer(text=config.FOOTER_TEXT)
        await msg.edit(embed=embed)

    # ---------- SPIN WHEEL ----------

    @commands.hybrid_command(name="spinwheel", aliases=["sw", "spin"], description="Spin the wheel of fortune.")
    @commands.cooldown(1, 10, BucketType.user)
    async def spinwheel(self, ctx: commands.Context, amount: int):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)

        err = validate_bet(amount, balance, max_bet=100_000)
        if err:
            return await ctx.send(err)

        loading = discord.Embed(
            title="Spinning the Wheel...",
            description="The wheel spins.",
            color=config.EMBED_COLOR,
        )
        msg = await ctx.send(embed=loading)
        await asyncio.sleep(2)

        label, multiplier = random.choice(WHEEL_OUTCOMES)
        won = multiplier > 0
        delta = amount * abs(multiplier)
        new_bal = balance + delta if won else balance - delta
        self.set_balance(user_id, new_bal)

        embed = discord.Embed(
            title="Spin Result",
            description=label,
            color=config.EMBED_COLOR if won else config.EMBED_COLOR_DARK,
        )
        embed.add_field(name="Bet", value=f"`{amount:,} {CURRENCY}`", inline=True)
        embed.add_field(
            name="Outcome",
            value=f"{'+' if won else '-'}{delta:,} {CURRENCY}",
            inline=True,
        )
        embed.add_field(name="New Balance", value=f"`{new_bal:,} {CURRENCY}`", inline=False)
        embed.set_footer(text=config.FOOTER_TEXT)
        await msg.edit(embed=embed)

    # ---------- FISH ----------

    @commands.hybrid_command(description="Cast a line for a payout multiplier.")
    @commands.cooldown(1, 10, BucketType.user)
    async def fish(self, ctx: commands.Context, amount: int):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)

        err = validate_bet(amount, balance, max_bet=100_000)
        if err:
            return await ctx.send(err)

        loading = discord.Embed(
            title="Fishing...",
            description="Casting your line into the water.",
            color=config.EMBED_COLOR,
        )
        loading.set_footer(text="Will you catch treasure or trash?")
        msg = await ctx.send(embed=loading)
        await asyncio.sleep(2)

        label, multiplier = random.choice(FISH_OUTCOMES)
        won = multiplier > 0
        delta = amount * abs(multiplier)
        new_bal = balance + delta if won else balance - delta
        self.set_balance(user_id, new_bal)

        embed = discord.Embed(
            title="Fishing Result",
            description=label,
            color=config.EMBED_COLOR if won else config.EMBED_COLOR_DARK,
        )
        embed.add_field(name="Bet", value=f"`{amount:,} {CURRENCY}`", inline=True)
        embed.add_field(
            name="Outcome",
            value=f"{'+' if won else '-'}{delta:,} {CURRENCY}",
            inline=True,
        )
        embed.add_field(name="New Balance", value=f"`{new_bal:,} {CURRENCY}`", inline=False)
        embed.set_footer(text=config.FOOTER_TEXT)
        await msg.edit(embed=embed)

    # ---------- SLOTS ----------

    @commands.hybrid_command(aliases=["slot"], description="Pull the slot machine.")
    @commands.cooldown(1, 8, BucketType.user)
    async def slots(self, ctx: commands.Context, amount: int):
        user_id = ctx.author.id
        balance = self.get_balance(user_id)

        err = validate_bet(amount, balance, max_bet=100_000)
        if err:
            return await ctx.send(err)

        loading = discord.Embed(
            title="Spinning Slots...",
            description="| ❓ ❓ ❓ |",
            color=config.EMBED_COLOR,
        )
        msg = await ctx.send(embed=loading)
        await asyncio.sleep(2)

        reels, multiplier = spin_slots()
        display = " | ".join(reels)

        won = multiplier > 0
        if won:
            delta = int(amount * multiplier)
            new_bal = balance + delta
            if multiplier >= 5:
                title, color = "MOONSHOT JACKPOT!", config.EMBED_COLOR
            elif multiplier >= 3:
                title, color = "Big Win!", config.EMBED_COLOR
            elif multiplier == 0.5:
                title, color = "Partial Match", config.EMBED_COLOR_DARK
            else:
                title, color = "You Won!", config.EMBED_COLOR
            outcome = f"**+{delta:,} {CURRENCY}**"
        else:
            new_bal = balance - amount
            title, color = "No Match", config.EMBED_COLOR_DARK
            outcome = f"**-{amount:,} {CURRENCY}**"

        self.set_balance(user_id, new_bal)

        embed = discord.Embed(title=title, color=color)
        embed.add_field(name="Reels", value=f"**{display}**", inline=False)
        embed.add_field(name="Outcome", value=outcome, inline=True)
        embed.add_field(name="New Balance", value=f"`{new_bal:,} {CURRENCY}`", inline=True)
        embed.set_footer(text=config.FOOTER_TEXT)
        await msg.edit(embed=embed)

    # ---------- ROB ----------

    @commands.hybrid_command(description="Attempt to rob another user. High risk, high reward.")
    @commands.cooldown(1, 60, BucketType.user)
    async def rob(self, ctx: commands.Context, target: discord.Member):
        if target.bot:
            return await ctx.send("You can't rob a bot.")
        if target.id == ctx.author.id:
            return await ctx.send("You can't rob yourself.")

        robber_bal = self.get_balance(ctx.author.id)
        victim_bal = self.get_balance(target.id)

        if victim_bal < 500:
            return await ctx.send(f"{target.mention} is too broke to rob.")
        if robber_bal < 1000:
            return await ctx.send("You need at least **1,000** to attempt a robbery.")

        success = random.random() < 0.2  # 20% success rate
        stolen = random.randint(100, min(5000, victim_bal // 4))
        fine = random.randint(500, 2000)

        if success:
            self.set_balance(ctx.author.id, robber_bal + stolen)
            self.set_balance(target.id, victim_bal - stolen)
            embed = discord.Embed(
                title="Robbery Successful!",
                description=f"You slipped away with **{stolen:,} {CURRENCY}** from {target.mention}.",
                color=config.EMBED_COLOR,
            )
            embed.add_field(name="Your Balance", value=f"`{robber_bal + stolen:,}`", inline=True)
        else:
            new_robber_bal = max(0, robber_bal - fine)
            self.set_balance(ctx.author.id, new_robber_bal)
            embed = discord.Embed(
                title="Caught!",
                description=f"You got caught trying to rob {target.mention} and paid a **{fine:,} {CURRENCY}** fine.",
                color=config.EMBED_COLOR_DARK,
            )
            embed.add_field(name="Your Balance", value=f"`{new_robber_bal:,}`", inline=True)

        embed.set_footer(text=config.FOOTER_TEXT)
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

        embed = discord.Embed(title="Blackjack", color=config.EMBED_COLOR)
        embed.add_field(
            name="Your Hand",
            value=f"`{' '.join(player)}` → **{pval}**",
            inline=False,
        )
        embed.add_field(
            name="Dealer",
            value=f"`{dealer[0]}` ❓",
            inline=False,
        )
        embed.add_field(
            name="Bet",
            value=f"`{amount:,} {CURRENCY}`",
            inline=False,
        )
        embed.set_footer(text="🟢 Hit  |  🛑 Stand  |  ⚡ Double Down")

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
            title, color = "Bust!", config.EMBED_COLOR_DARK
            result = f"**-{bet:,} {CURRENCY}**"
        elif natural_bj and d != 21:
            payout = int(bet * 1.5)
            new_bal = balance + payout
            title, color = "Blackjack! Natural 21!", config.EMBED_COLOR
            result = f"**+{payout:,} {CURRENCY}** (1.5x)"
        elif d > 21 or p > d:
            new_bal = balance + bet
            title, color = "You Win!", config.EMBED_COLOR
            result = f"**+{bet:,} {CURRENCY}**"
        elif p == d:
            new_bal = balance
            title, color = "Push — Tie", config.EMBED_COLOR_DARK
            result = "Bet returned."
        else:
            new_bal = balance - bet
            title, color = "Dealer Wins", config.EMBED_COLOR_DARK
            result = f"**-{bet:,} {CURRENCY}**"

        self.set_balance(user_id, new_bal)

        embed = discord.Embed(title=title, color=color)
        embed.add_field(
            name="Your Hand",
            value=f"`{' '.join(player)}` → **{p}**",
            inline=True,
        )
        embed.add_field(
            name="Dealer Hand",
            value=f"`{' '.join(dealer)}` → **{d}**",
            inline=True,
        )
        embed.add_field(name="Result", value=result, inline=False)
        embed.add_field(name="New Balance", value=f"`{new_bal:,} {CURRENCY}`", inline=False)
        embed.set_footer(text=config.FOOTER_TEXT)
        await message.edit(embed=embed)


# ---------- SETUP ----------

async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
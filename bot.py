"""
Rosarium — main entrypoint.

Design:
- Cog-based from the start. Drop new features into cogs/ and they
  auto-load on startup (see load_extensions below).
- Host-agnostic: reads config from environment variables (.env locally,
  or whatever env-var mechanism your host provides — Railway, a VPS
  with systemd, Docker, Replit, etc.). Nothing here assumes Railway.
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands

import config

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rosarium")


# --- Intents ---
# Start minimal, enable more as cogs need them.
# message_content is needed if you add any prefix-command or
# message-scanning cogs later; harmless to leave on.
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # needed for welcome/onboarding cogs later


class Rosarium(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=config.COMMAND_PREFIX,
            intents=intents,
            help_command=None,  # replaced by cogs/help.py
            # Wires config.OWNER_IDS into discord.py's built-in owner check,
            # so @commands.is_owner() respects the IDs in your .env rather
            # than only the Discord application's registered owner.
            owner_ids=set(config.OWNER_IDS),
        )

    async def setup_hook(self):
        """Called once, before the bot logs in and connects to the gateway."""
        await self.load_extensions()
        # Sync slash commands globally. During active dev, consider
        # syncing to a single guild instead (instant) — see note below.
        synced = await self.tree.sync()
        log.info(f"Synced {len(synced)} slash command(s).")

    async def load_extensions(self):
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        for filename in os.listdir(cogs_dir):
            if filename.endswith(".py") and not filename.startswith("_"):
                extension = f"cogs.{filename[:-3]}"
                try:
                    await self.load_extension(extension)
                    log.info(f"Loaded cog: {extension}")
                except Exception:
                    log.exception(f"Failed to load cog: {extension}")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=config.STATUS_MESSAGE,
            )
        )


# --- Global check: server-only ---
# Runs before every command in every cog. Returning False silently
# blocks the command; the bot won't respond in DMs at all.
def setup(bot: Rosarium):
    @bot.check
    async def guild_only(ctx: commands.Context) -> bool:
        return ctx.guild is not None


async def main():
    if not config.DISCORD_TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is not set. Create a .env file (see .env.example) "
            "or set the environment variable on your host."
        )

    bot = Rosarium()
    setup(bot)
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
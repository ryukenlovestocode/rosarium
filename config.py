"""
Central config for the Rosarium bot.
Keep branding/theme constants here so cogs can import them
instead of hardcoding colors/names everywhere.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Secrets / environment ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_IDS = [int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()]

# --- Branding ---
BOT_NAME = "Rosarium"
EMBED_COLOR = 0x8B0000       # deep blood red, primary accent
EMBED_COLOR_DARK = 0x1A1A1A  # near-black, secondary accent
FOOTER_TEXT = "Rosarium"

# --- Behavior ---
COMMAND_PREFIX = "."  # only used if you ever add prefix commands alongside slash commands
STATUS_MESSAGE = "over the garden of thorns"  # shown as "Watching ..."

# Channel ID where .quote posts generated quote cards. Set QUOTE_CHANNEL_ID
# in .env once you know the channel — leave blank/unset until then; the
# quote command will give a clear error rather than crash if it's not set.
QUOTE_CHANNEL_ID = int(os.getenv("QUOTE_CHANNEL_ID", "0")) or None

# --- Moderation role tiers ---
# Used by cogs/moderation.py to gate mod-only commands. Leave unset (0/blank)
# until you know the role IDs for your server — commands gated on a role that
# isn't configured simply won't be usable by anyone except bot owners
# (see OWNER_IDS above), rather than crashing.
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0")) or None
TRIAL_MOD_ROLE_ID = int(os.getenv("TRIAL_MOD_ROLE_ID", "0")) or None

# --- Welcomer ---
# Used by cogs/welcomer.py. WELCOME_CHANNEL_ID is where the join embed gets
# posted — required for the cog to do anything. The four *_CHANNEL_ID vars
# below are only used to turn "#rules"-style mentions in that embed into
# real clickable channel links; leave any of them unset and the cog falls
# back to plain "#rules" text for that one instead of crashing.
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0")) or None
RULES_CHANNEL_ID = int(os.getenv("RULES_CHANNEL_ID", "0")) or None
UPDATES_CHANNEL_ID = int(os.getenv("UPDATES_CHANNEL_ID", "0")) or None
GENERAL_CHANNEL_ID = int(os.getenv("GENERAL_CHANNEL_ID", "0")) or None
ROLES_CHANNEL_ID = int(os.getenv("ROLES_CHANNEL_ID", "0")) or None
ROLES_CHANNEL_ID = int(os.getenv("ROLES_CHANNEL_ID", "0")) or None
CONFESS_CHANNEL_ID = int(os.getenv("CONFESS_CHANNEL_ID", "0")) or None

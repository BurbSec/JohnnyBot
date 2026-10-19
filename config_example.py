"""Configuration settings for the Discord bot.

Copy this file to config.py and edit the values for your deployment:
    cp config_example.py config.py

config.py is gitignored, so your customizations won't conflict with git pull.
"""
# pylint: disable=cyclic-import
import os
import logging
from logging.handlers import RotatingFileHandler

# You probably want to change these:
MODERATORS_CHANNEL_NAME = 'moderators_only'
PROTECTED_CHANNELS = {'🫠・code_of_conduct', '🧚・hey_listen', '👯・local_events'}
# Moderator-gated commands and behaviors check the Discord "Manage
# Messages" / "Administrator" permission directly rather than a role
# name — no role to create or name to match, so this works on any
# server with zero setup beyond the bot's own role having permissions.
# These are for the "Voice Chaperone" function
VOICE_CHAPERONE_ENABLED = True  # Set to False to disable voice chaperone functionality
ADULT_ROLE_NAMES = {'Dads', 'GrownUps'}
CHILD_ROLE_NAMES = {'Kids', 'Bambinos', 'Girls'}

# Raid detection: a burst of joins in a short window pauses invites
# and DMs (Discord's own "incident" actions, self-expiring) and alerts
# moderators. See CLAUDE.md's "Raid Protection" section for details.
RAID_PROTECTION_ENABLED = True
RAID_JOIN_THRESHOLD = 6        # joins within the window that trigger it
RAID_JOIN_WINDOW_SECONDS = 30  # sliding window the threshold measures
RAID_LOCKDOWN_MINUTES = 60     # how long invites/DMs stay paused
RAID_NEW_ACCOUNT_HOURS = 24    # flag accounts younger than this in review/kick
# Raid accounts are generated in bulk and reuse profile pictures, so
# grouping recent joiners by avatar separates a real raid from an
# ordinary surge of arrivals. Costs one CDN fetch per new avatar.
RAID_AVATAR_CLUSTERING_ENABLED = True
RAID_AVATAR_SCAN_LIMIT = 50    # skip the scan past this many members
RAID_AVATAR_CACHE_TTL = 300    # seconds to reuse a hashed avatar

# Anti-nuke: watches the audit log for a compromised mod/admin account (or
# a rogue integration) going on a destructive spree, or quietly granting a
# role dangerous permissions. See CLAUDE.md's "Anti-Nuke Protection"
# section for details.
ANTI_NUKE_ENABLED = True
ANTI_NUKE_THRESHOLD = 3            # destructive actions by one actor...
ANTI_NUKE_WINDOW_SECONDS = 60      # ...within this many seconds
# 'strip_roles' immediately zeros the actor's permissions (reversible via
# /assign_role) before a human has to react; response time matters more
# here than avoiding a false-positive story. 'alert' skips the automated
# response and only notifies moderators.
ANTI_NUKE_ACTION = 'strip_roles'   # 'strip_roles' | 'alert'

# Message spam protection: catches the two things a raid cashes out as —
# the same link blasted across several channels at once, and mass pings.
# Link detection is behavioral rather than a domain blocklist, so it
# catches brand-new scam domains with no list to maintain. See CLAUDE.md's
# "Message Spam Protection" section.
SPAM_PROTECTION_ENABLED = True
SPAM_CROSSPOST_WINDOW_SECONDS = 30  # same link in 2+ channels this fast
SPAM_MENTION_THRESHOLD = 3          # distinct mentions in one message
SPAM_TIMEOUT_MINUTES = 10           # how long an offender is timed out
SPAM_NEW_ACCOUNT_DAYS = 2           # link-spammers younger: kick, not timeout

# /media_dump zips a channel's images/video/audio for a date range and
# DMs the requester a link to it, served by a small built-in web server.
# The server binds MEDIA_DUMP_HOST only while a link is live and shuts
# down (deleting the archives) MEDIA_DUMP_LINK_MINUTES after the newest
# link. With no BASE_URL the link is built from the bot's public IP (looked
# up via api.ipify.org, falling back to the local address) and the port
# below, so that port must be reachable from moderators' browsers. Plain
# HTTP: the unguessable link itself is the only credential.
MEDIA_DUMP_BASE_URL = ''           # optional override, e.g. 'https://bot.example.com'
MEDIA_DUMP_HOST = '0.0.0.0'        # interface the download server binds
MEDIA_DUMP_PORT = 48731            # uncommon on purpose
MEDIA_DUMP_LINK_MINUTES = 15       # link + server lifetime
MEDIA_DUMP_MAX_FILE_MB = 100       # skip single files larger than this
MEDIA_DUMP_MAX_TOTAL_MB = 1024     # stop adding once the archive hits this

# Update checking configuration
UPDATE_CHECKING_ENABLED = True  # Set to False to disable automatic update checking
UPDATE_CHECK_REPO_URL = "https://github.com/BurbSec/JohnnyBot"
# When True, updates that passed CI and don't modify config_example.py
# are pulled automatically and the bot restarts itself. Requires the
# bot to run from a git checkout. Anything else (CI not green, config
# changes, pull/install failure) falls back to a moderator notification.
AUTO_UPDATE_ENABLED = False
# Announce to the moderators channel when the running version changes,
# on the first start after an update. Confirms an update actually landed
# (the pre-restart notice is sent before anything is proven) and covers
# manual git pull + restart, which otherwise announces nothing. The body
# is the annotated tag's message, so whatever you write when tagging a
# release is what moderators read.
VERSION_ANNOUNCE_ENABLED = True

# Timezone for scheduled jobs and event announcements
BOT_TIMEZONE = 'America/Chicago'

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN environment variable is not set")

# File paths
LOG_FILE = os.path.join(os.path.dirname(__file__), 'johnnybot.log')
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB
REMINDERS_FILE = os.path.join(os.path.dirname(__file__), 'reminders.json')
TEMP_DIR = os.path.join(os.path.dirname(__file__), 'temp')

os.makedirs(TEMP_DIR, exist_ok=True)

logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=2)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

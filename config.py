"""Configuration loader for the Discord bot.

Reads user settings from config.txt (copy config_example.txt to get started).
This file is tracked in git — only config.txt is customized per deployment.
"""
# pylint: disable=cyclic-import
import os
import sys
import logging
from configparser import ConfigParser
from logging.handlers import RotatingFileHandler

_BASE_DIR = os.path.dirname(__file__)
_CONFIG_FILE = os.path.join(_BASE_DIR, 'config.txt')

if not os.path.exists(_CONFIG_FILE):
    print(f"ERROR: {_CONFIG_FILE} not found.\n"
          f"Copy config_example.txt to config.txt and edit it:\n"
          f"  cp config_example.txt config.txt", file=sys.stderr)
    sys.exit(1)

_cfg = ConfigParser()
_cfg.read(_CONFIG_FILE, encoding='utf-8')

# ── User settings ────────────────────────────────────────────────

# General
BOT_TIMEZONE = _cfg.get('general', 'BOT_TIMEZONE', fallback='America/Chicago')
HOST_IP = _cfg.get('general', 'HOST_IP', fallback='0.0.0.0')

# Channels
MODERATORS_CHANNEL_NAME = _cfg.get('channels', 'MODERATORS_CHANNEL_NAME', fallback='moderators_only')
PROTECTED_CHANNELS = {
    s.strip() for s in _cfg.get('channels', 'PROTECTED_CHANNELS', fallback='').split(',') if s.strip()
}

# Roles
MODERATOR_ROLE_NAME = _cfg.get('roles', 'MODERATOR_ROLE_NAME', fallback='Moderators')
ADULT_ROLE_NAMES = {
    s.strip() for s in _cfg.get('roles', 'ADULT_ROLE_NAMES', fallback='').split(',') if s.strip()
}
CHILD_ROLE_NAMES = {
    s.strip() for s in _cfg.get('roles', 'CHILD_ROLE_NAMES', fallback='').split(',') if s.strip()
}

# Features
VOICE_CHAPERONE_ENABLED = _cfg.getboolean('features', 'VOICE_CHAPERONE_ENABLED', fallback=True)
UPDATE_CHECKING_ENABLED = _cfg.getboolean('features', 'UPDATE_CHECKING_ENABLED', fallback=True)
UPDATE_CHECK_REPO_URL = _cfg.get('features', 'UPDATE_CHECK_REPO_URL', fallback='')

# Token (always from environment)
TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN environment variable is not set")

# ── File paths ───────────────────────────────────────────────────

LOG_FILE = os.path.join(_BASE_DIR, 'johnnybot.log')
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB
REMINDERS_FILE = os.path.join(_BASE_DIR, 'reminders.json')
TEMP_DIR = os.path.join(_BASE_DIR, 'temp')

os.makedirs(TEMP_DIR, exist_ok=True)

# ── Logging ──────────────────────────────────────────────────────

logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=2)
_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
logger.addHandler(_handler)

# Route APScheduler logs to the same file so job errors are visible
for _name in ('apscheduler', 'apscheduler.scheduler', 'apscheduler.executors.default'):
    _aplogger = logging.getLogger(_name)
    _aplogger.setLevel(logging.INFO)
    _aplogger.addHandler(_handler)

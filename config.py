"""Configuration settings for the Discord bot.

This module contains all configuration variables, constants, and setup for logging.
"""
# pylint: disable=cyclic-import
import os
import logging
from logging.handlers import RotatingFileHandler

# Bot configuration
TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN environment variable is not set")

# Role names
MODERATOR_ROLE_NAME = 'Moderators'

# File paths
LOG_FILE = os.path.join(os.path.dirname(__file__), 'johnnybot.log')
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB
REMINDERS_FILE = os.path.join(os.path.dirname(__file__), 'reminders.json')
TEMP_DIR = os.path.join(os.path.dirname(__file__), 'temp')

# Create temp directory if it doesn't exist
os.makedirs(TEMP_DIR, exist_ok=True)

# Channel names
MODERATORS_CHANNEL_NAME = 'moderators_only'
PROTECTED_CHANNELS = ['🫠・code_of_conduct', '🧚・hey_listen', '👯・local_events']

# Configure logging
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=2)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

"""Configuration settings for the Discord bot.

This module contains all configuration variables, constants, and setup for logging.
"""
# pylint: disable=cyclic-import
import os
import logging
from logging.handlers import RotatingFileHandler

# You probably want to change these:
MODERATORS_CHANNEL_NAME = 'moderators_only'
PROTECTED_CHANNELS = ['🫠・code_of_conduct', '🧚・hey_listen', '👯・local_events']
MODERATOR_ROLE_NAME = 'Moderators'
# These are for the "Voice Chaperone" function
ADULT_ROLE_NAMES = ['Dads', 'GrownUps']
CHILD_ROLE_NAMES = ['Kids', 'Bambinos', 'Girls']

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

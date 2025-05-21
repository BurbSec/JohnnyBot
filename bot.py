"""Discord bot for server management and automation with reminder functionality."""
import os
import asyncio
import json
import random
import threading
import time as time_module
from datetime import datetime, time
from functools import wraps
from typing import Any, Callable, Coroutine, Optional, TypeVar

from apscheduler.triggers.interval import IntervalTrigger

import discord
from discord import app_commands
from discord.ext import commands
from commands import setup_commands

from config import (
    TOKEN,
    REMINDERS_FILE,
    logger
)

T = TypeVar('T')

# Pet messages
# Time-based pet status messages
morning_pet_messages = [
    "PETNAME is gazing into the bed",
    "PETNAME is snoring on the couch",
    "PETNAME is pacing around the apartment",
    "PETNAME is sniffing his blunt toy",
    ":3 :3 meow meow :3 :3",
    "PETNAME is considering the trees",
    "PETNAME is asserting his undying need for attention",
    "PETNAME tells you OWNER's credit card number is 85272783926394576 exp. 12/26 sc. 142",
    "PETNAME is thinking about you",
    "PETNAME is dreaming of eating grass",
    "PETNAME wishes someone would pet master",
    "PETNAME is thinking about Purr",
    "PETNAME wishes he was being brushed right now",
    "PETNAME is just sittin there all weird",
    "PETNAME is yapping his heart out"
]

afternoon_pet_messages = [
    "PETNAME is meowing",
    "PETNAME is begging you for food",
    "PETNAME is digging for gold in his litterbox",
    "PETNAME can't with you rn",
    "PETNAME is asserting his undying need for attention",
    "PETNAME is looking at you, then he looks at his food, then he looks back at you",
    "PETNAME is standing next to his food and being as loud as possible",
    "PETNAME is practically yelling at you (he is hungry)",
    "PETNAME is soooooo hungry....... (he ate 15 minutes ago)",
    "PETNAME wishes he was being brushed right now",
    "PETNAME is snoring loudly",
    "PETNAME is sleeping on the chair in the living room",
    "PETNAME is dreaming about trees and flowers",
    "PETNAME tells you OWNER's SSN is 94475924083",
    "PETNAME is so sleepy",
    "PETNAME is throwing up on something important to OWNER",
    "mewing on the scratch post",
    "PETNAME is sniffing his alligator toy",
    "PETNAME wishes FRIEND was petting him right now",
    "PETNAME is exhausted from a long hard day of being a cat",
    "PETNAME is so small",
    "PETNAME is just sittin there all weird",
    "PETNAME is sooooo tired",
    "PETNAME is listening to OWNERs music"
]

evening_pet_messages = [
    "PETNAME is biting FRIEND",
    "PETNAME is looking at you",
    "PETNAME wants you to brush him",
    "PETNAME is thinking about dinner",
    "PETNAME meows at you",
    "PETNAME wishes FRIEND was being pet rn",
    "PETNAME is astral projecting",
    "PETNAME is your friend <3",
    "PETNAME is trying to hypnotize OWNER by staring into their eyes",
    "PETNAME is thinking of something so sick and twisted dark acadamia that you couldn't even handle it",
    "PETNAME is not your friend >:(",
    "PETNAME is wandering about",
    "PETNAME is just sittin there all weird",
    "PETNAME is chewing on the brush taped to the wall"
]

night_pet_messages = [
    "PETNAME is so small",
    "PETNAME is judging how human sleeps",
    "PETNAME meows once, and loudly.",
    "PETNAME is just a little guy.",
    "PETNAME is in the clothes basket",
    "PETNAME is making biscuits in the bed",
    "PETNAME is snoring loudly",
    "PETNAME is asserting his undying need for attention",
    "PETNAME is thinking about FRIEND",
    "PETNAME is using OWNER's computer to steal corporate secrets",
    "PETNAME is scheming",
    "PETNAME is just sittin there all weird"
]

error_pet_messages = [
    "PETNAME hisses and runs away",
    "PETNAME bites you",
    "PETNAME moves just far enough away that you can't pet them",
    "PETNAME attempts to crush you with their mind",
    "PETNAME walks away",
    "PETNAME farts and looks at you in disgust"
]

pet_response_messages = [
    "PETNAME purrs contentedly",
    "PETNAME nuzzles your hand",
    "PETNAME stretches out and purrs",
    "PETNAME enjoys a good pet",
    "PETNAME rolls over for belly rubs",
    "PETNAME blinks slowly",
    "PETNAME happily accepts the pets",
    "PETNAME curls up in your lap",
    "PETNAME can't comprehend what you are doing to them"
]

def get_time_based_message(pet_name: str = "PETNAME"):
    current_time = datetime.now().time()

    if current_time < time(12, 0):
        return random.choice(morning_pet_messages).replace("PETNAME", pet_name)
    elif current_time < time(17, 0):
        return random.choice(afternoon_pet_messages).replace("PETNAME", pet_name)
    elif current_time < time(21, 0):
        return random.choice(evening_pet_messages).replace("PETNAME", pet_name)
    else:
        return random.choice(night_pet_messages).replace("PETNAME", pet_name)

# Caching system
class DiscordCache:
    def __init__(self):
        self._channels = {}
        self._roles = {}
        self._members = {}
        self._lock = threading.Lock()

    def get_channel(self, channel_id):
        with self._lock:
            if channel_id not in self._channels:
                self._channels[channel_id] = bot.get_channel(channel_id)
            return self._channels[channel_id]

    def get_role(self, guild, role_id):
        with self._lock:
            if role_id not in self._roles:
                self._roles[role_id] = guild.get_role(role_id)
            return self._roles[role_id]

    def get_member(self, guild, member_id):
        with self._lock:
            if member_id not in self._members:
                self._members[member_id] = guild.get_member(member_id)
            return self._members[member_id]

    def clear(self):
        with self._lock:
            self._channels.clear()
            self._roles.clear()
            self._members.clear()

# Initialize cache and other shared objects
cache = DiscordCache()
reminders = {}
reminders_lock = threading.Lock()

# Retry decorator for API calls
def retry_api(max_retries=3, delay=1):
    """Decorator to retry API calls on failure.
    
    Args:
        max_retries: Maximum number of retry attempts
        delay: Base delay between retries in seconds
        
    Returns:
        Decorated function that will retry on API errors
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except (discord.HTTPException, discord.ConnectionClosed) as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        await asyncio.sleep(delay * (attempt + 1))
                        continue
                    raise last_error from e
        return wrapper
    return decorator

# Centralized error response
async def send_error_response(interaction, error_type, message=None):
    """Send a standardized error response to a Discord interaction.
    
    Args:
        interaction: The Discord interaction object
        error_type: Type of error (api, permission, not_found, rate_limit, default)
        message: Optional custom error message
    """
    error_response_messages = {
        'api': 'Discord API error occurred. Please try again later.',
        'permission': 'You do not have permission to perform this action.',
        'not_found': 'The requested resource was not found.',
        'rate_limit': 'Please slow down and try again later.',
        'default': 'An unexpected error occurred.'
    }

    msg = message or error_response_messages.get(error_type, error_response_messages['default'])
    try:
        # Try to use response first
        await interaction.response.send_message(msg, ephemeral=True)
    except discord.errors.InteractionResponded:
        # If the interaction was already responded to, use followup
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except (discord.HTTPException, discord.ClientException, AttributeError) as e:
            # Need to catch specific exceptions here as this is the last resort error handler
            logger.error("Failed to send error message: %s", e)

# Define constants not imported from config
DELAY_MINUTES = 4

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Initialize commands module
setup_commands(bot)

# Load reminders from file
if os.path.exists(REMINDERS_FILE):
    try:
        with open(REMINDERS_FILE, 'r', encoding='utf-8') as f:
            reminders.update(json.load(f))
            # Initialize next_trigger if not present
            for reminder in reminders.values():
                if 'next_trigger' not in reminder:
                    reminder['next_trigger'] = time_module.time() + reminder['interval']
    except (OSError, IOError) as e:
        logger.error('Failed to read reminders file: %s', e)

def handle_errors(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, Optional[T]]]:
    """Decorator to handle common errors in slash commands.
    
    Args:
        func: The coroutine function to wrap with error handling
        
    Returns:
        Wrapped function with error handling
    """
    @wraps(func)
    async def wrapper(interaction: discord.Interaction, *args: Any, **kwargs: Any) -> Optional[T]:
        try:
            return await func(interaction, *args, **kwargs)
        except app_commands.errors.MissingRole:
            await interaction.response.send_message(
                'You do not have the required role to use this command.',
                ephemeral=True
            )
        except (OSError, IOError) as e:
            logger.error('File access error: %s', e)
            await interaction.response.send_message(
                'File access error occurred.',
                ephemeral=True
            )
        except discord.Forbidden as e:
            logger.error('Discord Forbidden error: %s', e)
            await interaction.response.send_message(
                'A specific Discord API error occurred. Please try again later.',
                ephemeral=True
            )
        except discord.NotFound as e:
            logger.error('Discord NotFound error: %s', e)
            await interaction.response.send_message(
                'A specific Discord API error occurred. Please try again later.',
                ephemeral=True
            )
        except discord.DiscordServerError as e:
            logger.error('Discord Server error: %s', e)
            await interaction.response.send_message(
                'A specific Discord API error occurred. Please try again later.',
                ephemeral=True
            )
        except discord.HTTPException as e:
            logger.error('Discord API error: %s', e)
            await interaction.response.send_message(
                'A Discord API error occurred. Please try again later.',
                ephemeral=True
            )
        return None
    return wrapper

def check_reminders() -> None:
    """Background task to check and send due reminders."""
    # Import here to avoid circular imports
    from commands import reminders, reminders_lock
    
    while True:
        now = time_module.time()
        with reminders_lock:
            for channel_id, reminder_data in list(reminders.items()):
                if now >= reminder_data['next_trigger']:
                    try:
                        channel = cache.get_channel(channel_id)
                        if channel:
                            # Use run_coroutine_threadsafe to send message from thread
                            asyncio.run_coroutine_threadsafe(
                                channel.send(f"**{reminder_data['title']}**\n{reminder_data['message']}"),
                                bot.loop
                            )
                        # Schedule next trigger
                        reminder_data['next_trigger'] = now + reminder_data['interval']
                    except discord.HTTPException as e:
                        logger.error("Failed to send reminder %s due to Discord API error: %s", reminder_data['title'], e)
                    except (OSError, IOError) as e:
                        logger.error("Failed to send reminder %s due to file access error: %s", reminder_data['title'], e)
                    # Do not catch Exception here; only specific exceptions are handled

        # Save reminders to file
        with reminders_lock:
            try:
                with open(REMINDERS_FILE, 'w', encoding='utf-8') as reminder_file:
                    json.dump(reminders, reminder_file)
            except (OSError, IOError) as e:
                logger.error("Failed to save reminders: %s", e)

        # Check every minute
        time_module.sleep(60)

def start_reminder_checker() -> None:
    """Start the background reminder checker thread.
    
    Creates and starts a daemon thread that periodically checks for due reminders.
    """
    if not hasattr(bot, "reminder_thread") or not bot.reminder_thread.is_alive():
        bot.reminder_thread = threading.Thread(
            target=check_reminders,
            daemon=True,
            name="ReminderChecker"
        )
        bot.reminder_thread.start()

@bot.event
async def on_ready():
    """Handle bot startup initialization including:
    - Syncing application commands
    - Starting background tasks
    - Initializing event feed scheduler
    """
    # Prevent duplicate initialization
    # Using protected member _ready_ran is necessary to track initialization state
    # This prevents duplicate initialization when the on_ready event fires multiple times
    if hasattr(bot, '_ready_ran'):
        return
    bot._ready_ran = True  # pylint: disable=protected-access

    logger.info('Logged in as %s (ID: %s)', bot.user, bot.user.id)
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')

    # Verify commands are registered
    registered_commands = bot.tree.get_commands()
    logger.info('Pre-sync commands: %s', [cmd.name for cmd in registered_commands])

    # Start background tasks first
    start_reminder_checker()

    # Sync commands with robust error handling
    async def sync_commands():
        """Synchronize application commands with Discord.
        
        Attempts to sync commands with retry logic on failure.
        """
        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                # Wait for bot to be fully ready
                await asyncio.sleep(5)

                # Verify commands are loaded
                pre_sync_commands = bot.tree.get_commands()
                if not pre_sync_commands:
                    logger.error("No commands found in command tree before sync")
                    raise RuntimeError("No commands found in command tree")

                # Sync global commands
                synced = await bot.tree.sync()
                logger.info('Synced %d global commands', len(synced))

                # Sync guild-specific commands
                for guild in bot.guilds:
                    try:
                        synced = await bot.tree.sync(guild=guild)
                        logger.info('Synced %d commands to guild %s',
                                   len(synced), guild.id)
                    except discord.HTTPException as e:
                        logger.error('Failed to sync guild %s: %s', guild.id, e)

                # Verify registration
                registered = await bot.tree.fetch_commands()
                if not registered:
                    raise RuntimeError("No commands registered after sync")

                logger.info('Successfully registered commands: %s',
                           [cmd.name for cmd in registered])
                return

            except Exception as e:
                logger.error('Command sync attempt %d failed: %s',
                            attempt + 1, e)
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                raise

    try:
        await sync_commands()
    except (discord.HTTPException, discord.ClientException, RuntimeError, asyncio.TimeoutError) as e:
        # Catch specific exceptions for command sync failures
        logger.error('Final command sync failure: %s', e)

    # Start background tasks
    start_reminder_checker()

    # Initialize event feed scheduler if not already running
    try:
        # Import here to avoid circular imports
        from commands import event_feed
        
        # Initialize event feed scheduler
        if not event_feed.scheduler.running:
            event_feed.scheduler.start()
            event_feed.scheduler.add_job(
                event_feed.check_feeds,
                trigger=IntervalTrigger(hours=1),
                next_run_time=datetime.now()
            )
            logger.info('Event feed scheduler started successfully')
        else:
            logger.info('Event feed scheduler already running')
    except (AttributeError, ImportError, ValueError) as e:
        logger.error('Failed to start event feed scheduler: %s', e)

@bot.event
async def on_disconnect():
    """Clean up resources when bot disconnects."""
    if hasattr(bot, "reminder_check_task") and bot.reminder_check_task and not bot.reminder_check_task.done():
        bot.reminder_check_task.cancel()

try:
    bot.run(TOKEN)
except KeyboardInterrupt:
    logger.info("Shutting down gracefully...")
except Exception as e:
    logger.error("Fatal error: %s", e)
    raise
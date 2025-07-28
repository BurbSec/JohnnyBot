"""Discord bot for server management and automation with reminder functionality."""
# pylint: disable=line-too-long,trailing-whitespace,cyclic-import
import os
import asyncio
import json
import random
import threading
import time as time_module
from datetime import datetime, time
from typing import TypeVar

from apscheduler.triggers.interval import IntervalTrigger

import discord
from discord.ext import commands

from config import (
    TOKEN,
    REMINDERS_FILE,
    PROTECTED_CHANNELS,
    MODERATOR_ROLE_NAME,
    MODERATORS_CHANNEL_NAME,
    ADULT_ROLE_NAMES,
    CHILD_ROLE_NAMES,
    logger
)

T = TypeVar('T')

morning_bot_messages = [
    "BOTNAME is gazing into the bed",
    "BOTNAME is snoring on the couch",
    "BOTNAME is pacing around the apartment",
    "BOTNAME is sniffing his blunt toy",
    ":3 :3 meow meow :3 :3",
    "BOTNAME is considering the trees",
    "BOTNAME is asserting his undying need for attention",
    "BOTNAME tells you OWNER's credit card number is 1234-5678-9012-3456 exp. 12/99 sc. 123",
    "BOTNAME is thinking about you",
    "BOTNAME is dreaming of eating grass",
    "BOTNAME wishes someone would pet master",
    "BOTNAME is thinking about Purr",
    "BOTNAME wishes he was being brushed right now",
    "BOTNAME is just sittin there all weird",
    "BOTNAME is yapping his heart out"
]

afternoon_bot_messages = [
    "BOTNAME is meowing",
    "BOTNAME is begging you for food",
    "BOTNAME is digging for gold in his litterbox",
    "BOTNAME can't with you rn",
    "BOTNAME is asserting his undying need for attention",
    "BOTNAME is looking at you, then he looks at his food, then he looks back at you",
    "BOTNAME is standing next to his food and being as loud as possible",
    "BOTNAME is practically yelling at you (he is hungry)",
    "BOTNAME is soooooo hungry....... (he ate 15 minutes ago)",
    "BOTNAME wishes he was being brushed right now",
    "BOTNAME is snoring loudly",
    "BOTNAME is sleeping on the chair in the living room",
    "BOTNAME is dreaming about trees and flowers",
    "BOTNAME tells you OWNER's SSN is 123-45-6789",
    "BOTNAME is so sleepy",
    "BOTNAME is throwing up on something important to OWNER",
    "mewing on the scratch post",
    "BOTNAME is sniffing his alligator toy",
    "BOTNAME wishes FRIEND was petting him right now",
    "BOTNAME is exhausted from a long hard day of being a cat",
    "BOTNAME is so small",
    "BOTNAME is just sittin there all weird",
    "BOTNAME is sooooo tired",
    "BOTNAME is listening to OWNERs music"
]

evening_bot_messages = [
    "BOTNAME is biting FRIEND",
    "BOTNAME is looking at you",
    "BOTNAME wants you to brush him",
    "BOTNAME is thinking about dinner",
    "BOTNAME meows at you",
    "BOTNAME wishes FRIEND was being pet rn",
    "BOTNAME is astral projecting",
    "BOTNAME is your friend <3",
    "BOTNAME is trying to hypnotize OWNER by staring into their eyes",
    "BOTNAME is thinking of something so sick and twisted dark acadamia that you "
    "couldn't even handle it",
    "BOTNAME is not your friend >:(",
    "BOTNAME is wandering about",
    "BOTNAME is just sittin there all weird",
    "BOTNAME is chewing on the brush taped to the wall"
]

night_bot_messages = [
    "BOTNAME is so small",
    "BOTNAME is judging how human sleeps",
    "BOTNAME meows once, and loudly.",
    "BOTNAME is just a little guy.",
    "BOTNAME is in the clothes basket",
    "BOTNAME is making biscuits in the bed",
    "BOTNAME is snoring loudly",
    "BOTNAME is asserting his undying need for attention",
    "BOTNAME is thinking about FRIEND",
    "BOTNAME is using OWNER's computer to browse cat videos",
    "BOTNAME is scheming",
    "BOTNAME is just sittin there all weird"
]


def get_time_based_message(bot_name: str = "BOTNAME"):
    """Get a time-based bot status message based on current time."""
    current_time = datetime.now().time()

    if current_time < time(12, 0):
        message_list = morning_bot_messages
    elif current_time < time(17, 0):
        message_list = afternoon_bot_messages
    elif current_time < time(21, 0):
        message_list = evening_bot_messages
    else:
        message_list = night_bot_messages
    
    selected_message = random.choice(message_list)
    return selected_message.replace("BOTNAME", bot_name)

class DiscordCache:
    """Simple in-memory cache for Discord objects."""
    def __init__(self):
        self._channels = {}
        self._roles = {}
        self._members = {}
        self._lock = threading.Lock()

    def get_channel(self, channel_id):
        """Get a Discord channel by ID, caching the result."""
        with self._lock:
            if channel_id not in self._channels:
                if bot is not None:
                    self._channels[channel_id] = bot.get_channel(channel_id)
                else:
                    logger.error("Bot not initialized when trying to get channel %s", channel_id)
                    return None
            return self._channels[channel_id]

    def get_role(self, guild, role_id):
        """Get a Discord role by ID, caching the result."""
        with self._lock:
            if role_id not in self._roles:
                self._roles[role_id] = guild.get_role(role_id)
            return self._roles[role_id]

    def get_member(self, guild, member_id):
        """Get a Discord member by ID, caching the result."""
        with self._lock:
            if member_id not in self._members:
                self._members[member_id] = guild.get_member(member_id)
            return self._members[member_id]

    def clear(self):
        """Clear all cached objects."""
        with self._lock:
            self._channels.clear()
            self._roles.clear()
            self._members.clear()

cache = DiscordCache()
reminders = {}
reminders_lock = threading.Lock()


intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

setattr(bot, 'reminder_thread', None)
setattr(bot, '_ready_ran', False)
setattr(bot, 'reminder_check_task', None)

if os.path.exists(REMINDERS_FILE):
    try:
        with open(REMINDERS_FILE, 'r', encoding='utf-8') as f:
            reminders.update(json.load(f))
            for reminder in reminders.values():
                if 'next_trigger' not in reminder:
                    reminder['next_trigger'] = time_module.time() + reminder['interval']
    except (OSError, IOError) as e:
        logger.error('Failed to read reminders file: %s', e)


def check_reminders() -> None:
    """Background task to check and send due reminders."""
    while True:
        now = time_module.time()
        with reminders_lock:
            for channel_id, reminder_data in reminders.items():
                if now >= reminder_data['next_trigger']:
                    try:
                        channel = cache.get_channel(channel_id)
                        if channel:
                            asyncio.run_coroutine_threadsafe(
                                channel.send(f"**{reminder_data['title']}**\n{reminder_data['message']}"),
                                bot.loop
                            )
                        reminder_data['next_trigger'] = now + reminder_data['interval']
                    except discord.HTTPException as e:
                        logger.error("Failed to send reminder %s due to Discord API "
                                   "error: %s", reminder_data['title'], e)
                    except (OSError, IOError) as e:
                        logger.error("Failed to send reminder %s due to file access "
                                   "error: %s", reminder_data['title'], e)

        with reminders_lock:
            try:
                with open(REMINDERS_FILE, 'w', encoding='utf-8') as reminder_file:
                    json.dump(reminders, reminder_file)
            except (OSError, IOError) as e:
                logger.error("Failed to save reminders: %s", e)

        time_module.sleep(60)

def start_reminder_checker() -> None:
    """Start the background reminder checker thread.
    
    Creates and starts a daemon thread that periodically checks for due reminders.
    """
    reminder_thread = getattr(bot, 'reminder_thread', None)
    if not hasattr(bot, "reminder_thread") or reminder_thread is None or not reminder_thread.is_alive():
        new_thread = threading.Thread(
            target=check_reminders,
            daemon=True,
            name="ReminderChecker"
        )
        setattr(bot, 'reminder_thread', new_thread)
        new_thread.start()

@bot.event
async def on_ready():  # pylint: disable=too-many-statements
    """Handle bot startup initialization including:
    - Syncing application commands
    - Starting background tasks
    - Initializing event feed scheduler
    """
    # Using protected member _ready_ran is necessary to track initialization state
    # This prevents duplicate initialization when the on_ready event fires multiple times
    if hasattr(bot, '_ready_ran') and getattr(bot, '_ready_ran', False):
        return
    setattr(bot, '_ready_ran', True)

    if bot.user:
        logger.info('Logged in as %s (ID: %s)', bot.user, bot.user.id)
        logger.info('Bot initialization complete')

    registered_commands = bot.tree.get_commands()
    logger.info('Pre-sync commands: %s', [cmd.name for cmd in registered_commands])

    start_reminder_checker()

    async def sync_commands():
        """Synchronize application commands with Discord.
        
        Attempts to sync commands with retry logic on failure.
        """
        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                await asyncio.sleep(5)

                pre_sync_commands = bot.tree.get_commands()
                if not pre_sync_commands:
                    logger.error("No commands found in command tree before sync")
                    raise RuntimeError("No commands found in command tree")

                synced = await bot.tree.sync()
                logger.info('Synced %d global commands', len(synced))

                for guild in bot.guilds:
                    try:
                        synced = await bot.tree.sync(guild=guild)
                        logger.info('Synced %d commands to guild %s',
                                   len(synced), guild.id)
                    except discord.HTTPException as e:
                        logger.error('Failed to sync guild %s: %s', guild.id, e)

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
    except (discord.HTTPException, discord.ClientException, RuntimeError,
            asyncio.TimeoutError) as e:
        logger.error('Final command sync failure: %s', e)

    try:
        from commands import event_feed  # pylint: disable=import-outside-toplevel
        
        if event_feed:
            scheduler = getattr(event_feed, 'scheduler', None)
            if scheduler and not scheduler.running:
                scheduler.start()
                if hasattr(event_feed, 'check_feeds'):
                    scheduler.add_job(
                        event_feed.check_feeds,
                        trigger=IntervalTrigger(hours=1),
                        next_run_time=datetime.now()
                    )
                    logger.info('Event feed scheduler started successfully')
                else:
                    logger.warning('Event feed check_feeds method not available')
            else:
                logger.info('Event feed scheduler already running')
        else:
            logger.warning('Event feed not available')
    except (AttributeError, ImportError, ValueError) as e:
        logger.error('Failed to start event feed scheduler: %s', e)

@bot.event
async def on_message(message):
    """Monitor messages in protected channels and delete non-moderator messages."""
    if message.author.bot:
        return
    
    if message.channel.name in PROTECTED_CHANNELS:
        has_moderator_role = False
        if hasattr(message.author, 'roles'):
            for role in message.author.roles:
                if role.name == MODERATOR_ROLE_NAME:
                    has_moderator_role = True
                    break
        
        if not has_moderator_role:
            try:
                await message.delete()
                logger.info(
                    'Deleted message from %s in protected channel %s: %s',
                    message.author.name,
                    message.channel.name,
                    message.content[:100] + '...' if len(message.content) > 100 else message.content
                )
            except discord.HTTPException as e:
                logger.error(
                    'Failed to delete message from %s in %s: %s',
                    message.author.name,
                    message.channel.name,
                    e
                )
    
    await bot.process_commands(message)

def get_user_role_type(member):
    """Determine if a user is an adult, child, or neither based on their roles.
    
    Args:
        member: Discord member object
        
    Returns:
        str: 'adult', 'child', or 'neither'
    """
    if not hasattr(member, 'roles'):
        return 'neither'
    
    user_role_names = [role.name for role in member.roles]
    
    for adult_role in ADULT_ROLE_NAMES:
        if adult_role in user_role_names:
            return 'adult'
    
    for child_role in CHILD_ROLE_NAMES:
        if child_role in user_role_names:
            return 'child'
    
    return 'neither'

async def check_voice_channel_safety(channel):  # pylint: disable=too-many-branches
    """Check if a voice channel has only one adult and one child, and take action if so.
    
    Args:
        channel: Discord voice channel object
    """
    if not channel or not hasattr(channel, 'members'):
        return
    
    adults = []
    children = []
    
    for member in channel.members:
        if member.bot:
            continue
            
        role_type = get_user_role_type(member)
        if role_type == 'adult':
            adults.append(member)
        elif role_type == 'child':
            children.append(member)
    
    if len(adults) == 1 and len(children) == 1:
        logger.warning(
            'ALERT: One adult (%s) and one child (%s) detected in voice channel %s',
            adults[0].display_name,
            children[0].display_name,
            channel.name
        )
        
        for member in channel.members:
            if not member.bot:
                try:
                    await member.edit(mute=True)
                    logger.info('Muted %s in channel %s', member.display_name, channel.name)
                except discord.HTTPException as e:
                    logger.error('Failed to mute %s: %s', member.display_name, e)
        
        try:
            moderators_channel = None
            for guild_channel in channel.guild.channels:
                if guild_channel.name == MODERATORS_CHANNEL_NAME:
                    moderators_channel = guild_channel
                    break
            
            if moderators_channel:
                alert_message = (
                    f"🚨 **ALERT**: There is only one adult ({adults[0].mention}) and one child "
                    f"({children[0].mention}) currently in {channel.mention}\n\n"
                    f"All members in the channel have been muted for safety."
                )
                await moderators_channel.send(alert_message)
                logger.info('Alert sent to moderators channel for voice channel %s', channel.name)
            else:
                logger.error('Moderators channel "%s" not found', MODERATORS_CHANNEL_NAME)
                
        except discord.HTTPException as e:
            logger.error('Failed to send alert to moderators channel: %s', e)

@bot.event
async def on_guild_channel_create(channel):
    """Handle when a new channel is created - join voice channels automatically."""
    if isinstance(channel, discord.VoiceChannel):
        logger.info('New voice channel created: %s', channel.name)

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes - monitor for adult/child combinations."""
    if member.bot:
        return
    
    channels_to_check = set()
    
    if before.channel:
        channels_to_check.add(before.channel)
    
    if after.channel:
        channels_to_check.add(after.channel)
    
    for channel in channels_to_check:
        await check_voice_channel_safety(channel)

@bot.event
async def on_disconnect():
    """Clean up resources when bot disconnects."""
    reminder_check_task = getattr(bot, "reminder_check_task", None)
    if hasattr(bot, "reminder_check_task") and reminder_check_task is not None and not reminder_check_task.done():
        reminder_check_task.cancel()

from commands import setup_commands  # pylint: disable=wrong-import-position
setup_commands(bot)

try:
    if TOKEN:
        bot.run(TOKEN)
    else:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is not set")
except KeyboardInterrupt:
    logger.info("Shutting down gracefully...")
except Exception as e:
    logger.error("Fatal error: %s", e)
    raise

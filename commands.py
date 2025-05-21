"""Discord bot command module for server management and automation."""
import os
import random
import time as time_module
import threading
import asyncio
import json
from datetime import datetime, timedelta
from typing import TypeVar
import discord
from discord import app_commands
import requests
from icalendar import Calendar
from config import (
    MODERATOR_ROLE_NAME,
    LOG_FILE,
    REMINDERS_FILE,
    logger
)

class DiscordCache:
    """Simple in-memory cache for Discord data."""
    def __init__(self):
        self._cache = {}

    def get(self, key):
        return self._cache.get(key)

    def set(self, key, value):
        self._cache[key] = value

    def clear(self):
        self._cache.clear()

class EventFeed:
    """Handles event feed subscriptions and notifications."""
    def __init__(self, bot):
        self.bot = bot
        self.feeds = {}  # {guild_id: {url: last_checked}}
        self.running = True

    async def check_feeds(self):
        """Check all subscribed feeds for new events."""
        while self.running:
            for _, feeds in self.feeds.items():
                for url, _ in feeds.items():
                    try:
                        response = requests.get(url, timeout=30)
                        # Process calendar events...
                        Calendar.from_ical(response.text)
                    except (requests.RequestException, ValueError, AttributeError) as e:
                        logger.error("Error checking feed %s: %s", url, e)
            await asyncio.sleep(3600)  # Check hourly

# Pet response messages
pet_response_messages = [
    "PETNAME purrs happily!",
    "PETNAME rubs against your leg!",
    "PETNAME gives you a slow blink of affection!",
    "PETNAME meows appreciatively!",
    "PETNAME headbutts your hand for more pets!"
]

def get_time_based_message(pet_name: str) -> str:
    """Returns a time-based greeting message."""
    current_hour = datetime.now().hour
    if 5 <= current_hour < 12:
        return f"Good morning! {pet_name} is awake and ready for the day!"
    elif 12 <= current_hour < 17:
        return f"Good afternoon! {pet_name} is enjoying the day!"
    elif 17 <= current_hour < 22:
        return f"Good evening! {pet_name} is winding down."
    else:
        return f"{pet_name} is sleeping... shhh!"



# These will be set when commands are registered
bot_instance = None  # Renamed to avoid redefining name from outer scope
tree = None
cache = None
reminders = {}
reminders_lock = None
reminder_threads = {}
event_feed = None

def register_commands():
    """Register all commands with the command tree."""
    # Only register if tree is initialized
    if tree is None:
        return

    # Add all commands
    tree.add_command(create_set_reminder_command())

    # Create list_reminders command
    @tree.command(name='list_reminders', description='Lists all current reminders')
    async def list_reminders(interaction: discord.Interaction):
        """Lists all current reminders."""
        try:
            if not reminders:
                await interaction.response.send_message('There are no reminders set.', ephemeral=True)
                return

            reminder_list = '\n'.join([
                f"**{reminder['title']}**: {reminder['message']} "
                f"(every {reminder['interval']} seconds)"
                for reminder in reminders.values()
            ])
            await interaction.response.send_message(f'Current reminders:\n{reminder_list}', ephemeral=True)
        except (discord.HTTPException, OSError, IOError) as e:
            logger.error('Error listing reminders: %s', e)
            await interaction.response.send_message('Failed to list reminders due to an error.', ephemeral=True)

    # Register delete_all_reminders command
    @tree.command(name='delete_all_reminders', description='Deletes all active reminders')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _delete_all_reminders(interaction: discord.Interaction) -> None:
        await delete_all_reminders(interaction)

    # Register delete_reminder command
    @tree.command(name='delete_reminder', description='Deletes a reminder by title')
    @app_commands.describe(title='Title of the reminder to delete')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _delete_reminder(interaction: discord.Interaction, title: str) -> None:
        await delete_reminder(interaction, title)

    # Register purge_last_messages command
    @tree.command(name='purge_last_messages', description='Purges a specified number of messages from a channel')
    @app_commands.describe(channel='Channel to purge messages from', limit='Number of messages to delete')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _purge_last_messages(interaction: discord.Interaction, channel: discord.TextChannel, limit: int):
        await purge_last_messages(interaction, channel, limit)

    # Add error handler for purge_last_messages
    _purge_last_messages.error(purge_last_messages_error)

    # Register purge_string command
    @tree.command(name='purge_string', description='Purges all messages containing a specific string from a channel')
    @app_commands.describe(channel='Channel to purge messages from', search_string='String to search for in messages')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _purge_string(interaction: discord.Interaction, channel: discord.TextChannel, search_string: str):
        await purge_string(interaction, channel, search_string)

    # Add error handler for purge_string
    _purge_string.error(purge_string_error)

    # Register purge_webhooks command
    @tree.command(name='purge_webhooks', description='Purges all messages sent by webhooks or apps from a channel')
    @app_commands.describe(channel='Channel to purge messages from')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _purge_webhooks(interaction: discord.Interaction, channel: discord.TextChannel):
        await purge_webhooks(interaction, channel)

    # Add error handler for purge_webhooks
    _purge_webhooks.error(purge_webhooks_error)

    # Register kick command
    @tree.command(name='kick', description='Kicks a member from the server')
    @app_commands.describe(member='Member to kick', reason='Reason for kick')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _kick(interaction: discord.Interaction, member: discord.Member, reason: str = None):
        await kick_member(interaction, member, reason)

    # Add error handler for kick
    _kick.error(kick_error)

    # Register botsay command
    @tree.command(name='botsay', description='Makes the bot send a message to a specified channel')
    @app_commands.describe(channel='Channel to send the message to', message='Message to send')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _botsay(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
        await botsay_message(interaction, channel, message)

    # Add error handler for botsay
    _botsay.error(botsay_error)

    # Register timeout command
    @tree.command(name='timeout', description='Timeouts a member for a specified duration')
    @app_commands.describe(member='Member to timeout', duration='Timeout duration in seconds', reason='Reason for timeout')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _timeout(interaction: discord.Interaction, member: discord.Member, duration: int, reason: str = None):
        await timeout_member(interaction, member, duration, reason)

    # Add error handler for timeout
    _timeout.error(timeout_error)

    # Register log_tail command
    @tree.command(name='log_tail', description='DM the last specified number of lines of the bot log to the user')
    @app_commands.describe(lines='Number of lines to retrieve from the log')
    async def _log_tail(interaction: discord.Interaction, lines: int):
        await log_tail_command(interaction, lines)
    
    # Add error handler for log_tail
    _log_tail.error(log_tail_error)

    # Register add_event_feed_url command
    @tree.command(name='add_event_feed_url', description='Adds a calendar feed URL to check for events')
    @app_commands.describe(
        calendar_url='URL of the calendar feed',
        channel_name='Channel to post notifications (default: bot-trap)'
    )
    async def _add_event_feed_url(interaction: discord.Interaction, calendar_url: str, channel_name: str = "bot-trap"):
        await add_event_feed_url_command(interaction, calendar_url, channel_name)

    # Add error handler for add_event_feed_url
    _add_event_feed_url.error(add_event_feed_url_error)

    # Register add_event_feed command
    @tree.command(name='add_event_feed', description='Adds a calendar feed to check for events')
    @app_commands.describe(calendar_url='URL of the calendar feed')
    async def _add_event_feed(interaction: discord.Interaction, calendar_url: str):
        await add_event_feed_command(interaction, calendar_url)

    # Add error handler for add_event_feed
    _add_event_feed.error(add_event_feed_error)

    # Register list_event_feeds command
    @tree.command(name='list_event_feeds', description='Lists all registered calendar feeds')
    async def _list_event_feeds(interaction: discord.Interaction):
        await list_event_feeds_command(interaction)

    # Register remove_event_feed command
    @tree.command(name='remove_event_feed', description='Removes a calendar feed')
    @app_commands.describe(feed_url='URL of the calendar feed to remove')
    async def _remove_event_feed(interaction: discord.Interaction, feed_url: str):
        await remove_event_feed_command(interaction, feed_url)

    # Register cat command
    @tree.command(name='cat', description='Check on JohnnyBot')
    async def _cat(interaction: discord.Interaction):
        await cat_command(interaction)

    # Register pet_cat command
    @tree.command(name='pet_cat', description='Pet JohnnyBot')
    async def _pet_cat(interaction: discord.Interaction):
        await pet_cat_command(interaction)

    # Register cat_pick_fav command
    @tree.command(name='cat_pick_fav', description='See who JohnnyBot prefers today')
    @app_commands.describe(
        user1="First potential favorite",
        user2="Second potential favorite"
    )
    async def _cat_pick_fav(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
        await cat_pick_fav_command(interaction, user1, user2)

def setup_commands(bot_param):
    """Initialize command module with bot instance and register commands."""
    # Using globals is necessary here to initialize module-level variables
    # pylint: disable=global-statement
    global bot_instance, tree, cache, reminders, reminders_lock, reminder_threads, event_feed
    bot_instance = bot_param
    tree = bot_instance.tree
    cache = DiscordCache()
    reminders = {}
    reminders_lock = threading.Lock()
    reminder_threads = {}
    event_feed = EventFeed(bot_instance)

    # Initialize scheduler for EventFeed
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    # This attribute is defined outside __init__ because it depends on an import
    # that should happen at function level to avoid circular imports
    # pylint: disable=attribute-defined-outside-init
    event_feed.scheduler = AsyncIOScheduler()

    # Register all commands
    register_commands()

T = TypeVar('T')

class InvalidReminderInterval(Exception):
    """Exception raised when an invalid reminder interval is provided."""

def validate_reminder_interval(interval: int) -> None:
    """Validate that reminder interval is reasonable."""
    if interval < 60:
        raise InvalidReminderInterval("Interval must be at least 60 seconds")

def create_set_reminder_command():
    """Factory function to create the set_reminder command."""
    cmd = app_commands.Command(
        name='set_reminder',
        description='Sets a reminder message to be sent to a channel at regular intervals',
        callback=set_reminder_callback
    )
    cmd.add_check(app_commands.checks.has_role(MODERATOR_ROLE_NAME))

    # Add error handler
    async def on_error(interaction: discord.Interaction, error):
        """Handles errors for the set_reminder command."""
        if isinstance(error, app_commands.errors.MissingRole):
            await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
        elif isinstance(error, InvalidReminderInterval):
            await interaction.response.send_message(f'Invalid interval: {error}', ephemeral=True)
        elif isinstance(error, discord.HTTPException):
            logger.error('Discord API error: %s', error)
            await interaction.response.send_message('Discord API error occurred.', ephemeral=True)
        else:
            await interaction.response.send_message(f'Error: {error}', ephemeral=True)
    cmd.on_error = on_error

    return cmd

async def set_reminder_callback(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, interval: int):
    """Callback for the set_reminder command."""
    validate_reminder_interval(interval)
    with reminders_lock:
        reminders[channel.id] = {
            'channel_id': channel.id,
            'title': title,
            'message': message,
            'interval': interval,
            'next_trigger': time_module.time() + interval
        }
        with open(REMINDERS_FILE, 'w', encoding='utf-8') as reminder_file:
            json.dump(reminders, reminder_file)

    await interaction.response.send_message(f'Reminder set in {channel.mention} every {interval} seconds.', ephemeral=True)


# Command functions defined at module level but registered in register_commands()
async def delete_all_reminders(interaction: discord.Interaction) -> None:
    """Delete all active reminders."""
    with reminders_lock:
        reminders.clear()
        for stop_event in reminder_threads.values():
            stop_event.set()
        reminder_threads.clear()
        try:
            with open(REMINDERS_FILE, 'w', encoding='utf-8') as reminder_file:
                json.dump(reminders, reminder_file)
        except (OSError, IOError) as e:
            logger.error('Failed to write reminders file: %s', e)
            await interaction.response.send_message('Failed to delete reminders due to file access error.', ephemeral=True)
            return
    await interaction.response.send_message('All reminders have been deleted.', ephemeral=True)

async def delete_reminder(interaction: discord.Interaction, title: str) -> None:
    """Deletes a reminder by title."""
    try:
        with reminders_lock:
            for channel_id, reminder_data in list(reminders.items()):
                if reminder_data['title'] == title:
                    del reminders[channel_id]
                    if channel_id in reminder_threads:
                        reminder_threads[channel_id].set()
                        del reminder_threads[channel_id]
                    try:
                        with open(REMINDERS_FILE, 'w', encoding='utf-8') as reminder_file:
                            json.dump(reminders, reminder_file)
                    except (OSError, IOError) as e:
                        logger.error('Failed to write reminders file: %s', e)
                        await interaction.response.send_message('Failed to delete reminder due to file access error.', ephemeral=True)
                        return
                    await interaction.response.send_message(f'Reminder titled "{title}" has been deleted.', ephemeral=True)
                    return
        await interaction.response.send_message(f'No reminder found with the title "{title}".', ephemeral=True)
    except (discord.HTTPException, OSError, IOError) as e:
        logger.error('Error deleting reminder: %s', e)
        await interaction.response.send_message('Failed to delete reminder due to an error.', ephemeral=True)

async def purge_last_messages(interaction: discord.Interaction, channel: discord.TextChannel, limit: int):
    """Purges a specified number of messages from a channel."""
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await channel.purge(limit=limit)
        await interaction.followup.send(f'Deleted {len(deleted)} message(s)', ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send('You do not have permission to perform this action.', ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error: %s', e)
        await interaction.followup.send('Discord API error occurred. Please try again later.', ephemeral=True)

async def purge_last_messages_error(interaction: discord.Interaction, error):
    """Handles errors for the purge_last_messages command.
    
    Args:
        interaction: The Discord interaction object
        error: The error that occurred
    """
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message('Discord API error occurred.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

async def purge_string(interaction: discord.Interaction, channel: discord.TextChannel, search_string: str):
    """Purges all messages containing a specific string from a channel."""
    try:
        def check_message(message):
            return search_string in message.content

        deleted = await channel.purge(check=check_message)
        await interaction.response.send_message(f'Deleted {len(deleted)} message(s) containing "{search_string}".', ephemeral=True)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

async def purge_string_error(interaction: discord.Interaction, error):
    """Handles errors for the purge_string command.
    
    Args:
        interaction: The Discord interaction object
        error: The error that occurred
    """
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

async def purge_webhooks(interaction: discord.Interaction, channel: discord.TextChannel):
    """Purges all messages sent by webhooks or apps from a channel."""
    try:
        def check_message(message):
            return message.webhook_id is not None or message.author.bot

        deleted = await channel.purge(check=check_message)
        await interaction.response.send_message(f'Deleted {len(deleted)} message(s) sent by webhooks or apps.', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

async def purge_webhooks_error(interaction: discord.Interaction, error):
    """Handles errors for the purge_webhooks command.
    
    Args:
        interaction: The Discord interaction object
        error: The error that occurred
    """
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

async def kick_member(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    """Kicks a member from the server."""
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f'{member.mention} has been kicked. Reason: {reason}', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

async def kick_error(interaction: discord.Interaction, error):
    """Handles errors for the kick command.
    
    Args:
        interaction: The Discord interaction object
        error: The error that occurred
    """
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message('Discord API error occurred.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

async def botsay_message(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    """Makes the bot send a message to a specified channel."""
    try:
        await channel.send(message)
        await interaction.response.send_message(f'Message sent to {channel.mention}', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

async def botsay_error(interaction: discord.Interaction, error):
    """Handles errors for the botsay command.
    
    Args:
        interaction: The Discord interaction object
        error: The error that occurred
    """
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message('Discord API error occurred.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

async def timeout_member(interaction: discord.Interaction, member: discord.Member, duration: int, reason: str = None):
    """Timeouts a member for a specified duration."""
    try:
        until = discord.utils.utcnow() + timedelta(seconds=duration)
        await member.timeout(until, reason=reason)
        await interaction.response.send_message(f'{member.mention} has been timed out for {duration} seconds.', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

async def timeout_error(interaction: discord.Interaction, error):
    """Handles errors for the timeout command.
    
    Args:
        interaction: The Discord interaction object
        error: The error that occurred
    """
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

async def log_tail_command(interaction: discord.Interaction, lines: int):
    """DM the last specified number of lines of the bot log to the user."""
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as log_file:
            last_lines = ''.join(log_file.readlines()[-lines:])
        if last_lines:
            await interaction.user.send(f'```{last_lines}```')
            await interaction.response.send_message('Log lines sent to your DMs.', ephemeral=True)
        else:
            await interaction.response.send_message('Log file is empty.', ephemeral=True)
    except (OSError, IOError) as e:
        logger.error('Failed to read log file: %s', e)
        await interaction.response.send_message('Failed to retrieve log file.', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

async def log_tail_error(interaction: discord.Interaction, error):
    """Handles errors for the log_tail command.
    
    Args:
        interaction: The Discord interaction object
        error: The error that occurred
    """
    if isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message('Discord API error occurred.', ephemeral=True)
    else:
        await interaction.response.send_message(f"Error: {str(error)}", ephemeral=True)

async def add_event_feed_url_command(interaction: discord.Interaction, calendar_url: str, channel_name: str = "bot-trap"):
    """Adds a calendar feed URL to check for events."""
    try:
        if not calendar_url.startswith(('http://', 'https://')):
            await interaction.response.send_message("Invalid URL format", ephemeral=True)
            return
        if interaction.guild.id not in event_feed.feeds:
            event_feed.feeds[interaction.guild.id] = {}
        event_feed.feeds[interaction.guild.id][calendar_url] = {
            'last_checked': None,
            'channel': channel_name
        }
        await event_feed.check_feeds()
        await interaction.response.send_message(
            f"Added calendar feed! I'll check for new events hourly and post in "
            f"#{channel_name}",
            ephemeral=True
        )
    except (discord.Forbidden, discord.HTTPException, ValueError, AttributeError) as e:
        await interaction.response.send_message(
            f"Error adding feed: {str(e)}",
            ephemeral=True
        )

async def add_event_feed_url_error(interaction: discord.Interaction, error):
    """Handles errors for the add_event_feed_url command.
    
    Args:
        interaction: The Discord interaction object
        error: The error that occurred
    """
    if isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message('Discord API error occurred.', ephemeral=True)
    else:
        await interaction.response.send_message(f"Error: {str(error)}", ephemeral=True)

async def add_event_feed_command(interaction: discord.Interaction, calendar_url: str):
    """Adds a calendar feed to check for events."""
    try:
        if not calendar_url.startswith(('http://', 'https://')):
            await interaction.response.send_message("Invalid URL format", ephemeral=True)
            return
        if interaction.guild.id not in event_feed.feeds:
            event_feed.feeds[interaction.guild.id] = {}
        event_feed.feeds[interaction.guild.id][calendar_url] = None
        await event_feed.check_feeds()
        await interaction.response.send_message(
            "Added calendar feed! I'll check for new events hourly and post in #bot-trap",
            ephemeral=True
        )
    except (discord.Forbidden, discord.HTTPException, ValueError, AttributeError) as e:
        await interaction.response.send_message(
            f"Error adding feed: {str(e)}",
            ephemeral=True
        )

async def add_event_feed_error(interaction: discord.Interaction, error):
    """Handles errors for the add_event_feed command.
    
    Args:
        interaction: The Discord interaction object
        error: The error that occurred
    """
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message('Discord API error occurred.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

async def list_event_feeds_command(interaction: discord.Interaction):
    """Lists all registered calendar feeds."""
    if interaction.guild.id not in event_feed.feeds or not event_feed.feeds[interaction.guild.id]:
        await interaction.response.send_message('No calendar feeds registered', ephemeral=True)
        return
    feed_list = '\n'.join(event_feed.feeds[interaction.guild.id].keys())
    await interaction.response.send_message(f'Registered calendar feeds:\n{feed_list}', ephemeral=True)

async def remove_event_feed_command(interaction: discord.Interaction, feed_url: str):
    """Removes a calendar feed."""
    if interaction.guild.id not in event_feed.feeds or feed_url not in event_feed.feeds[interaction.guild.id]:
        await interaction.response.send_message('Calendar feed not found', ephemeral=True)
        return
    del event_feed.feeds[interaction.guild.id][feed_url]
    await interaction.response.send_message(f'Removed calendar feed: {feed_url}', ephemeral=True)

async def cat_command(interaction: discord.Interaction):
    """Check on JohnnyBot."""
    try:
        pet_name = os.getenv("PET_NAME", "JohnnyBot")
        message = get_time_based_message(pet_name)
        logger.info('[%s] - cat command: %s', interaction.user, message)
        await interaction.response.send_message(message)
    except discord.errors.NotFound:
        # Handle case where interaction has timed out
        logger.warning("Interaction timed out for cat command from %s", interaction.user)

async def pet_cat_command(interaction: discord.Interaction):
    """Pet JohnnyBot."""
    try:
        pet_name = os.getenv("PET_NAME", "JohnnyBot")
        message = random.choice(pet_response_messages).replace("PETNAME", pet_name)
        logger.info('[%s] - pet cat command: %s', interaction.user, message)
        await interaction.response.send_message(message)
    except discord.errors.NotFound:
        # Handle case where interaction has timed out
        logger.warning("Interaction timed out for pet_cat command from %s", interaction.user)

async def cat_pick_fav_command(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
    """See who JohnnyBot prefers today."""
    try:
        pet_name = os.getenv("PET_NAME", "JohnnyBot")
        chosen_user = random.choice([user1, user2])
        message = f"{pet_name} is giving attention to {chosen_user.mention}!"
        logger.info('[%s] - cat pick fav command: %s', interaction.user, message)
        await interaction.response.send_message(message)
    except discord.errors.NotFound:
        # Handle case where interaction has timed out
        logger.warning("Interaction timed out for cat_pick_fav command from %s", interaction.user)
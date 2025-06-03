"""Discord bot command module for server management and automation."""
import os
import random
import time as time_module
import threading
import asyncio
import json
import zipfile
import socket
import shutil
from datetime import datetime, timedelta
from typing import TypeVar
from pathlib import Path
import discord
from discord import app_commands
import requests
from icalendar import Calendar
from flask import Flask, send_file
from waitress import serve
from config import (
    MODERATOR_ROLE_NAME,
    LOG_FILE,
    REMINDERS_FILE,
    TEMP_DIR,
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
message_dump_servers = {}  # Store active message dump servers

class MessageDumpServer:
    """Manages a temporary web server for hosting message dump files."""
    def __init__(self, file_path, zip_path, duration=1800):  # 30 minutes default
        self.file_path = file_path
        self.zip_path = zip_path
        self.duration = duration
        self.app = Flask(__name__)
        self.server_thread = None
        self.shutdown_timer = None
        self.port = self._find_free_port()
        self.ip = self._get_public_ip()
        
        # Set up Flask route
        @self.app.route('/')
        def download_file():
            return send_file(self.zip_path, as_attachment=True)
    
    def _find_free_port(self):
        """Find a free port to use for the server."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            return s.getsockname()[1]
    
    def _get_public_ip(self):
        """Get the public IP address of the server."""
        try:
            # This is a simple way to get the public IP, but it requires internet access
            response = requests.get('https://api.ipify.org', timeout=5)
            return response.text
        except requests.RequestException:
            # Fallback to local IP if public IP can't be determined
            hostname = socket.gethostname()
            return socket.gethostbyname(hostname)
    
    def start(self):
        """Start the web server in a separate thread."""
        def run_server():
            serve(self.app, host='0.0.0.0', port=self.port)
        
        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()
        
        # Set up shutdown timer
        self.shutdown_timer = threading.Timer(self.duration, self.cleanup)
        self.shutdown_timer.start()
        
        return f"http://{self.ip}:{self.port}"
    
    def cleanup(self):
        """Clean up resources when the server is shut down."""
        # Remove the files
        try:
            if os.path.exists(self.file_path):
                os.remove(self.file_path)
            if os.path.exists(self.zip_path):
                os.remove(self.zip_path)
            
            # Remove parent directory if it's empty
            parent_dir = os.path.dirname(self.file_path)
            if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                os.rmdir(parent_dir)
                
            logger.info(f"Cleaned up message dump files: {self.file_path}, {self.zip_path}")
        except (OSError, IOError) as e:
            logger.error(f"Error cleaning up message dump files: {e}")
        
        # Remove from active servers
        for key, server in list(message_dump_servers.items()):
            if server is self:
                del message_dump_servers[key]
                break

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
        
    # Register message_dump command
    @tree.command(name='message_dump', description='Dump a user\'s messages from a channel into a downloadable file')
    @app_commands.describe(
        user="User whose messages to dump",
        channel="Channel to dump messages from",
        start_date="Start date in YYYY-MM-DD format (e.g., 2025-01-01)",
        limit="Maximum number of messages to fetch (default: 1000)"
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _message_dump(interaction: discord.Interaction, user: discord.User, channel: discord.TextChannel,
                           start_date: str, limit: int = 1000):
        await message_dump_command(interaction, user, channel, start_date, limit)
    
    # Add error handler for message_dump
    _message_dump.error(message_dump_error)

def setup_commands(bot_param):
    """Initialize command module with bot instance and register commands."""
    # Using globals is necessary here to initialize module-level variables
    # pylint: disable=global-statement
    global bot_instance, tree, cache, reminders, reminders_lock, reminder_threads, event_feed, message_dump_servers
    bot_instance = bot_param
    tree = bot_instance.tree
    cache = DiscordCache()
    reminders = {}
    reminders_lock = threading.Lock()
    reminder_threads = {}
    event_feed = EventFeed(bot_instance)
    message_dump_servers = {}

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

def cleanup_orphaned_dumps():
    """Clean up orphaned message dump files and folders older than 30 minutes.
    
    Returns:
        int: Number of directories cleaned up
    """
    cleaned_count = 0
    try:
        # Get the current time
        now = datetime.now()
        
        # Check if the temp directory exists
        if not os.path.exists(TEMP_DIR):
            return cleaned_count
            
        # Iterate through all subdirectories in the temp directory
        for item in os.listdir(TEMP_DIR):
            if item.startswith("message_dump_"):
                item_path = os.path.join(TEMP_DIR, item)
                
                # Check if it's a directory
                if os.path.isdir(item_path):
                    # Get the creation time of the directory
                    creation_time = datetime.fromtimestamp(os.path.getctime(item_path))
                    
                    # Check if it's older than 30 minutes
                    if (now - creation_time).total_seconds() > 1800:  # 30 minutes in seconds
                        # Delete the directory and all its contents
                        shutil.rmtree(item_path, ignore_errors=True)
                        logger.info(f"Cleaned up orphaned message dump directory: {item_path}")
                        cleaned_count += 1
        
        return cleaned_count
    except Exception as e:
        logger.error(f"Error cleaning up orphaned message dumps: {e}")
        return cleaned_count

async def message_dump_command(interaction: discord.Interaction, user: discord.User, channel: discord.TextChannel,
                              start_date: str, limit: int = 1000):
    """Dump a user's messages from a channel into a downloadable file starting from a specific date."""
    try:
        # Clean up any orphaned dump files/folders
        logger.info("Checking for orphaned message dump files/folders...")
        cleaned_count = cleanup_orphaned_dumps()
        if cleaned_count > 0:
            logger.info(f"Cleaned up {cleaned_count} orphaned message dump directories")
        # Defer the response since this might take a while
        await interaction.response.defer(ephemeral=True)
        
        # Notify user if any orphaned dumps were cleaned up
        if cleaned_count > 0:
            await interaction.followup.send(
                f"Cleaned up {cleaned_count} orphaned message dump files that were older than 30 minutes.",
                ephemeral=True
            )
        
        # Parse the start date
        try:
            start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
            # Set time to beginning of the day (midnight)
            start_datetime = start_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            await interaction.followup.send(
                "Invalid date format. Please use YYYY-MM-DD format (e.g., 2025-01-01).",
                ephemeral=True
            )
            return
            
        # Create a unique directory for this dump
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dump_dir = os.path.join(TEMP_DIR, f"message_dump_{interaction.user.id}_{timestamp}")
        os.makedirs(dump_dir, exist_ok=True)
        
        # Create file paths
        file_path = os.path.join(dump_dir, f"{user.name}_messages.txt")
        zip_path = os.path.join(dump_dir, f"{user.name}_messages.zip")
        
        # Fetch messages with proper pagination
        messages = []
        message_count = 0
        oldest_message = None
        
        # Log the user ID we're looking for
        logger.info(f"Looking for messages from user ID: {user.id}, name: {user.name}")
        
        # Check if the channel exists and is accessible
        try:
            # Try to fetch one message to verify channel access
            async for _ in channel.history(limit=1):
                break
            else:
                # No messages in the channel
                await interaction.followup.send(f"The channel {channel.mention} appears to be empty.", ephemeral=True)
                shutil.rmtree(dump_dir, ignore_errors=True)
                return
        except discord.Forbidden:
            await interaction.followup.send(f"I don't have permission to read messages in {channel.mention}.", ephemeral=True)
            shutil.rmtree(dump_dir, ignore_errors=True)
            return
        except Exception as e:
            logger.error(f"Error accessing channel: {e}")
            await interaction.followup.send(f"Error accessing channel {channel.mention}: {str(e)}", ephemeral=True)
            shutil.rmtree(dump_dir, ignore_errors=True)
            return
            
        # Status update for the user
        await interaction.followup.send(
            f"Fetching messages from {user.mention} (ID: {user.id}) in {channel.mention} starting from {start_date}. "
            f"This may take a while...",
            ephemeral=True
        )
        
        # Simplified approach to message fetching
        messages = []
        total_processed = 0
        last_message_id = None
        
        # Log the start of message fetching
        logger.info(f"Starting message fetch for user {user.id} in channel {channel.id}")
        
        # Rate limit handling variables
        retry_count = 0
        max_retries = 5
        base_delay = 1.0
        
        # Continue fetching until we reach the limit or run out of messages
        while total_processed < limit:
            try:
                # Determine how many messages to fetch in this batch
                batch_size = min(100, limit - total_processed)
                
                # Set up the fetch parameters
                fetch_kwargs = {'limit': batch_size, 'after': start_datetime}
                if last_message_id:
                    fetch_kwargs['before'] = discord.Object(id=last_message_id)
                
                # Log the current fetch attempt
                logger.info(f"Fetching batch with params: {fetch_kwargs}, processed so far: {total_processed}")
            
                # Fetch the batch
                current_batch = []
                messages_in_this_batch = 0
                
                async for msg in channel.history(**fetch_kwargs):
                    messages_in_this_batch += 1
                    # Keep track of the last message ID for pagination
                    if last_message_id is None or msg.id < last_message_id:
                        last_message_id = msg.id
                    
                    # Count this message
                    total_processed += 1
                    
                    # Log message details for debugging
                    logger.info(f"Message {msg.id} from author ID: {msg.author.id}, target user ID: {user.id}, match: {msg.author.id == user.id}")
                    
                    # Check if this message is from our target user
                    if msg.author.id == user.id:
                        # Format the message
                        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        content = msg.content if msg.content else "[No text content]"
                        
                        # Handle attachments
                        attachments = ""
                        if msg.attachments:
                            attachments = "\nAttachments: " + ", ".join([a.url for a in msg.attachments])
                        
                        # Handle embeds
                        embeds = ""
                        if msg.embeds:
                            embeds = "\nEmbeds: " + str(len(msg.embeds)) + " embed(s)"
                        
                        # Add to our collection
                        formatted_message = f"[{timestamp}] {content}{attachments}{embeds}\n\n"
                        current_batch.append(formatted_message)
                
                # Reset retry count on successful fetch
                retry_count = 0
                
            except discord.HTTPException as e:
                if "rate limited" in str(e).lower():
                    retry_delay = base_delay * (2 ** retry_count)
                    retry_count += 1
                    logger.warning(f"Rate limited. Retrying in {retry_delay} seconds. Retry {retry_count}/{max_retries}")
                    
                    if retry_count <= max_retries:
                        await interaction.followup.send(
                            f"Hit Discord rate limit. Waiting {retry_delay} seconds before continuing...",
                            ephemeral=True
                        )
                        await asyncio.sleep(retry_delay)
                        # Skip the rest of this iteration and retry
                        continue
                    else:
                        logger.error(f"Max retries ({max_retries}) reached for rate limiting")
                        await interaction.followup.send(
                            "Hit Discord rate limit too many times. Try again later or with a smaller limit.",
                            ephemeral=True
                        )
                        # Break out of the loop entirely
                        break
                else:
                    # Re-raise other HTTP exceptions
                    raise
            
            # Log the results of this batch
            batch_count = len(current_batch)
            logger.info(f"Batch complete: processed {batch_count} messages from target user")
            
            # Add the batch to our collection
            messages.extend(current_batch)
            
            # Send a progress update every 500 messages or at the end of a batch
            if total_processed % 500 == 0 or batch_count < batch_size:
                await interaction.followup.send(
                    f"Progress update: Processed {total_processed} messages, found {len(messages)} from {user.mention}...",
                    ephemeral=True
                )
            
            # Log the batch results
            logger.info(f"Batch complete: got {messages_in_this_batch} messages in batch, of which {batch_count} were from target user")
            
            # If we got fewer messages than requested, we've reached the end
            if messages_in_this_batch == 0 or messages_in_this_batch < batch_size:
                logger.info(f"End of channel history reached. Total processed: {total_processed}, found: {len(messages)}")
                break
            
            # Add a delay to avoid rate limiting
            # Use exponential backoff - start with a small delay and increase if we hit rate limits
            try:
                await asyncio.sleep(1.0)  # Increased from 0.5 to 1.0 second
            except asyncio.CancelledError:
                # Handle cancellation gracefully
                logger.info("Message fetching was cancelled")
                break
        
        # Send a completion message based on whether we reached the limit or ran out of messages
        if total_processed >= limit:
            await interaction.followup.send(
                f"Reached the message limit of {limit}. Found {len(messages)} messages from {user.mention}.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Completed! Processed all {total_processed} messages in the channel and found {len(messages)} from {user.mention}.",
                ephemeral=True
            )
        
        # If no messages found
        if not messages:
            await interaction.followup.send(f"No messages found from {user.mention} in {channel.mention}.", ephemeral=True)
            # Clean up the directory
            shutil.rmtree(dump_dir, ignore_errors=True)
            return
        
        # Write messages to file
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(f"Messages from {user.name} (ID: {user.id}) in #{channel.name}\n")
            f.write(f"Dump created at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Start date: {start_date}\n")
            f.write(f"Messages found: {len(messages)}\n")
            f.write(f"Total messages processed: {total_processed}\n\n")
            f.write("="*50 + "\n\n")
            f.writelines(messages)
        
        # Compress the file
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(file_path, os.path.basename(file_path))
        
        # Start the web server
        server = MessageDumpServer(file_path, zip_path)
        download_url = server.start()
        
        # Store the server in the active servers dictionary
        server_key = f"{interaction.user.id}_{timestamp}"
        message_dump_servers[server_key] = server
        
        # Send DM to the user with the download link
        expiry_time = (datetime.now() + timedelta(seconds=1800)).strftime("%Y-%m-%d %H:%M:%S")
        dm_message = (
            f"Here's the message dump you requested from {channel.mention}:\n\n"
            f"**Download Link:** {download_url}\n"
            f"**User:** {user.mention}\n"
            f"**Start Date:** {start_date}\n"
            f"**Messages found:** {len(messages)}\n"
            f"**Messages processed:** {total_processed}\n"
            f"**Link expires:** {expiry_time} (30 minutes from now)\n\n"
            f"The file will be automatically deleted after the link expires."
        )
        
        await interaction.user.send(dm_message)
        
        # Send a confirmation in the channel where the command was used
        await interaction.followup.send(
            f"Message dump for {user.mention} from {channel.mention} has been created. "
            f"Check your DMs for the download link. The link will expire in 30 minutes.",
            ephemeral=True
        )
        
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to access that channel or send you DMs.", ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error: %s', e)
        await interaction.followup.send("A Discord API error occurred.", ephemeral=True)
    except (OSError, IOError) as e:
        logger.error('File operation error: %s', e)
        await interaction.followup.send("An error occurred while creating the message dump file.", ephemeral=True)
    except Exception as e:
        logger.error('Unexpected error in message_dump_command: %s', e)
        await interaction.followup.send("An unexpected error occurred.", ephemeral=True)

async def message_dump_error(interaction: discord.Interaction, error):
    """Handles errors for the message_dump command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    elif isinstance(error, app_commands.errors.MissingRequiredArgument):
        await interaction.response.send_message(
            'Missing required argument. Make sure to provide user, channel, and start_date.',
            ephemeral=True
        )
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message('Discord API error occurred.', ephemeral=True)
    else:
        logger.error('Error in message_dump command: %s', error)
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)
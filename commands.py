"""Discord bot command module for server management and automation."""
# pylint: disable=too-many-lines,line-too-long,trailing-whitespace,import-outside-toplevel,logging-fstring-interpolation,broad-exception-caught,no-else-break
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
from typing import Optional, Dict, Any
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


class EventFeed:  # pylint: disable=too-few-public-methods
    """Handles event feed subscriptions and notifications."""
    def __init__(self, bot):
        self.bot = bot
        self.feeds: Dict[int, Dict[str, Any]] = {}  # {guild_id: {url: feed_data}}
        self.running = True
        self.scheduler: Optional[Any] = None  # Will be set in setup_commands

    async def check_feeds_job(self):
        """Scheduled job to check all subscribed feeds for new events."""
        for guild_id, feeds in self.feeds.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
                
            for url, feed_data in feeds.items():
                try:
                    await self._check_single_feed(guild, url, feed_data)
                except Exception as e:
                    logger.error("Error checking feed %s: %s", url, e)

    async def _check_single_feed(self, guild, url: str, feed_data: Dict[str, Any]):
        """Check a single calendar feed for new events."""
        try:
            calendar = await self._fetch_calendar(url)
            channel = self._get_notification_channel(guild, feed_data.get('channel', 'bot-trap'))
            if not channel:
                return
            
            new_events = self._parse_calendar_events(calendar, feed_data)
            await self._process_new_events(guild, channel, new_events, feed_data)
            
        except (requests.RequestException, ValueError, AttributeError) as e:
            logger.error("Error checking feed %s: %s", url, e)

    async def _fetch_calendar(self, url: str):
        """Fetch and parse calendar from URL."""
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return Calendar.from_ical(response.text)

    def _get_notification_channel(self, guild, channel_name: str):
        """Get the Discord channel for notifications."""
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if not channel:
            logger.error("Channel #%s not found in guild %s", channel_name, guild.name)
        return channel

    def _parse_calendar_events(self, calendar, feed_data: Dict[str, Any]) -> list:
        """Parse calendar events and return new ones."""
        posted_events = feed_data.get('posted_events', set())
        last_checked = feed_data.get('last_checked', datetime.now() - timedelta(days=1))
        current_time = datetime.now()
        new_events = []

        for component in calendar.walk():
            if component.name == "VEVENT":
                event = self._process_calendar_event(component, posted_events, last_checked, current_time)
                if event:
                    new_events.append(event)
        
        return new_events

    def _process_calendar_event(self, component, posted_events: set, last_checked: datetime, current_time: datetime):
        """Process a single calendar event component."""
        event_uid = str(component.get('uid', ''))
        if event_uid in posted_events:
            return None

        # Extract basic event details
        event_details = self._extract_event_details(component)
        if not event_details:
            return None

        start_date, end_date = event_details['start_date'], event_details['end_date']
        
        # Only process future events or events that started recently
        if start_date < current_time - timedelta(hours=1):
            return None
        
        # Check if this is a new event since last check
        if start_date > last_checked or event_uid not in posted_events:
            return {
                'uid': event_uid,
                'summary': event_details['summary'],
                'description': event_details['description'],
                'location': event_details['location'],
                'start_date': start_date,
                'end_date': end_date
            }
        return None

    def _extract_event_details(self, component):
        """Extract event details from calendar component."""
        # Extract basic info
        summary = str(component.get('summary', 'No Title'))
        description = str(component.get('description', ''))
        location = str(component.get('location', ''))
        
        # Handle start datetime
        dtstart = component.get('dtstart')
        if not dtstart:
            return None  # Skip events without start date
        
        start_date = dtstart.dt
        if not hasattr(start_date, 'date'):
            start_date = datetime.combine(start_date, datetime.min.time())
        
        # Handle end datetime
        end_date = self._calculate_end_date(component.get('dtend'), start_date)
        
        return {
            'summary': summary,
            'description': description,
            'location': location,
            'start_date': start_date,
            'end_date': end_date
        }

    def _calculate_end_date(self, dtend, start_date):
        """Calculate event end date."""
        if dtend:
            end_date = dtend.dt
            if not hasattr(end_date, 'date'):
                end_date = datetime.combine(end_date, datetime.min.time())
            return end_date
        
        # If no end date, assume 1 hour duration for timed events
        if isinstance(start_date, datetime):
            return start_date + timedelta(hours=1)
        # For all-day events, end is the same day
        return start_date

    async def _process_new_events(self, guild, channel, new_events: list, feed_data: Dict[str, Any]):
        """Process and post new events."""
        posted_events = feed_data.get('posted_events', set())
        
        for event in new_events:
            await self._post_event_to_discord(channel, event)
            await self._create_discord_event(guild, event)
            posted_events.add(event['uid'])
        
        # Update feed data
        feed_data['last_checked'] = datetime.now()
        feed_data['posted_events'] = posted_events

    async def _post_event_to_discord(self, channel, event: Dict[str, Any]):
        """Post an event to a Discord channel."""
        try:
            # Format the event date
            start_date = event['start_date']
            end_date = event.get('end_date')
            
            if isinstance(start_date, datetime):
                date_str = start_date.strftime("%Y-%m-%d")
                time_str = start_date.strftime("%H:%M")
                if end_date and isinstance(end_date, datetime) and end_date.date() == start_date.date():
                    time_str += f" - {end_date.strftime('%H:%M')}"
            else:
                date_str = str(start_date)
                time_str = "All Day"
            
            # Build the message
            embed = discord.Embed(
                title=f"📅 {event['summary']}",
                color=0x00ff00,
                description="🎉 **Also added to Discord Events!**"
            )
            
            embed.add_field(name="📅 Date", value=date_str, inline=True)
            embed.add_field(name="🕐 Time", value=time_str, inline=True)
            
            if event['location']:
                embed.add_field(name="📍 Location", value=event['location'], inline=False)
            
            if event['description']:
                # Truncate description if too long
                desc = event['description'][:1000] + "..." if len(event['description']) > 1000 else event['description']
                embed.add_field(name="📝 Description", value=desc, inline=False)
            
            await channel.send(embed=embed)
            logger.info("Posted event '%s' to #%s", event['summary'], channel.name)
            
        except discord.HTTPException as e:
            logger.error("Error posting event to Discord: %s", e)

    async def _create_discord_event(self, guild, event: Dict[str, Any]):
        """Create a Discord Event in the guild's Events section."""
        try:
            # Prepare event data
            name = event['summary'][:100]  # Discord has a 100 character limit for event names
            description = event.get('description', '')[:1000]  # 1000 character limit for description
            start_time = event['start_date']
            end_time = event.get('end_date')
            location = event.get('location', '')
            
            # Convert to timezone-aware datetime if needed
            if isinstance(start_time, datetime) and start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=discord.utils.utcnow().tzinfo)
            
            if end_time and isinstance(end_time, datetime) and end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=discord.utils.utcnow().tzinfo)
            
            # Handle all-day events (date objects)
            if not isinstance(start_time, datetime):
                # Convert date to datetime at start of day
                start_time = datetime.combine(start_time, datetime.min.time())
                start_time = start_time.replace(tzinfo=discord.utils.utcnow().tzinfo)
                
            if end_time and not isinstance(end_time, datetime):
                # Convert date to datetime at end of day
                end_time = datetime.combine(end_time, datetime.max.time().replace(microsecond=0))
                end_time = end_time.replace(tzinfo=discord.utils.utcnow().tzinfo)
            
            # If no end time, set it to 1 hour after start for timed events
            if not end_time:
                end_time = start_time + timedelta(hours=1)
            
            # Create the Discord event as an external event
            # External events require a location
            event_location = location[:100] if location else "See event details"
            
            # Create the Discord event
            discord_event = await guild.create_scheduled_event(
                name=name,
                description=description,
                start_time=start_time,
                end_time=end_time,
                entity_type=discord.EntityType.external,
                location=event_location,
                privacy_level=discord.PrivacyLevel.guild_only
            )
            
            logger.info("Created Discord Event '%s' (ID: %s) in guild %s",
                       name, discord_event.id, guild.name)
            
        except discord.HTTPException as e:
            logger.error("Error creating Discord Event '%s': %s", event['summary'], e)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Unexpected error creating Discord Event '%s': %s", event['summary'], e)

    async def check_feeds(self):
        """Legacy method for backward compatibility."""
        await self.check_feeds_job()

# Pet response messages



# These will be set when commands are registered
bot_instance: Optional[Any] = None  # Renamed to avoid redefining name from outer scope
tree: Optional[Any] = None
reminders: Dict[int, Dict[str, Any]] = {}
reminders_lock: Optional[threading.Lock] = None
reminder_threads: Dict[int, Any] = {}
event_feed: Optional[EventFeed] = None
message_dump_servers: Dict[str, Any] = {}  # Store active message dump servers

class MessageDumpServer:  # pylint: disable=too-many-instance-attributes
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
                
            logger.info("Cleaned up message dump files: %s, %s", self.file_path, self.zip_path)
        except (OSError, IOError) as e:
            logger.error("Error cleaning up message dump files: %s", e)
        
        # Remove from active servers
        for key, server in message_dump_servers.items():
            if server is self:
                del message_dump_servers[key]
                break

def register_commands():  # pylint: disable=too-many-locals
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
                await interaction.response.send_message('There are no reminders set.',
                                                       ephemeral=True)
                return

            reminder_list = '\n'.join(
                f"**{reminder['title']}**: {reminder['message']} "
                f"(every {reminder['interval']} seconds)"
                for reminder in reminders.values()
            )
            await interaction.response.send_message(f'Current reminders:\n{reminder_list}',
                                                   ephemeral=True)
        except (discord.HTTPException, OSError, IOError) as e:
            logger.error('Error listing reminders: %s', e)
            await interaction.response.send_message('Failed to list reminders due to an error.',
                                                   ephemeral=True)

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
    @tree.command(name='purge_last_messages',
                  description='Purges a specified number of messages from a channel')
    @app_commands.describe(channel='Channel to purge messages from',
                          limit='Number of messages to delete')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _purge_last_messages(interaction: discord.Interaction,
                                  channel: discord.TextChannel, limit: int):
        await purge_last_messages(interaction, channel, limit)

    # Add error handler for purge_last_messages
    _purge_last_messages.on_error = purge_last_messages_error

    # Register purge_string command
    @tree.command(name='purge_string',
                  description='Purges all messages containing a specific string from a channel')
    @app_commands.describe(channel='Channel to purge messages from',
                          search_string='String to search for in messages')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _purge_string(interaction: discord.Interaction,
                           channel: discord.TextChannel, search_string: str):
        await purge_string(interaction, channel, search_string)

    # Add error handler for purge_string
    _purge_string.on_error = purge_string_error

    # Register purge_webhooks command
    @tree.command(name='purge_webhooks',
                  description='Purges all messages sent by webhooks or apps from a channel')
    @app_commands.describe(channel='Channel to purge messages from')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _purge_webhooks(interaction: discord.Interaction, channel: discord.TextChannel):
        await purge_webhooks(interaction, channel)

    # Add error handler for purge_webhooks
    _purge_webhooks.on_error = purge_webhooks_error

    # Register kick command
    @tree.command(name='kick', description='Kicks a member from the server')
    @app_commands.describe(member='Member to kick', reason='Reason for kick')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _kick(interaction: discord.Interaction, member: discord.Member,
                   reason: Optional[str] = None):
        await kick_member(interaction, member, reason)

    # Add error handler for kick
    _kick.on_error = kick_error

    # Register botsay command
    @tree.command(name='botsay', description='Makes the bot send a message to a specified channel')
    @app_commands.describe(channel='Channel to send the message to', message='Message to send')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _botsay(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
        await botsay_message(interaction, channel, message)

    # Add error handler for botsay
    _botsay.on_error = botsay_error

    # Register timeout command
    @tree.command(name='timeout', description='Timeouts a member for a specified duration')
    @app_commands.describe(member='Member to timeout', duration='Timeout duration in seconds',
                          reason='Reason for timeout')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _timeout(interaction: discord.Interaction, member: discord.Member,
                      duration: int, reason: Optional[str] = None):
        await timeout_member(interaction, member, duration, reason)

    # Add error handler for timeout
    _timeout.on_error = timeout_error

    # Register log_tail command
    @tree.command(name='log_tail',
                  description='DM the last specified number of lines of the bot log to the user')
    @app_commands.describe(lines='Number of lines to retrieve from the log')
    async def _log_tail(interaction: discord.Interaction, lines: int):
        await log_tail_command(interaction, lines)
    
    # Add error handler for log_tail
    _log_tail.on_error = log_tail_error

    # Register add_event_feed command
    @tree.command(name='add_event_feed',
                  description='Adds a calendar feed URL to check for events')
    @app_commands.describe(
        calendar_url='URL of the calendar feed',
        channel_name='Channel to post notifications (default: bot-trap)'
    )
    async def _add_event_feed(interaction: discord.Interaction, calendar_url: str,
                                 channel_name: str = "bot-trap"):
        await add_event_feed_command(interaction, calendar_url, channel_name)

    # Add error handler for add_event_feed
    _add_event_feed.on_error = add_event_feed_error


    # Register list_event_feeds command
    @tree.command(name='list_event_feeds', description='Lists all registered calendar feeds')
    async def _list_event_feeds(interaction: discord.Interaction):
        await list_event_feeds_command(interaction)

    # Register remove_event_feed command
    @tree.command(name='remove_event_feed', description='Removes a calendar feed')
    @app_commands.describe(feed_url='URL of the calendar feed to remove')
    async def _remove_event_feed(interaction: discord.Interaction, feed_url: str):
        await remove_event_feed_command(interaction, feed_url)

    # Register bot_mood command
    @tree.command(name='bot_mood', description='Check on JohnnyBot\'s current mood')
    async def _bot_mood(interaction: discord.Interaction):
        await bot_command(interaction)

    # Register pet_bot command
    @tree.command(name='pet_bot', description='Pet JohnnyBot')
    async def _pet_bot(interaction: discord.Interaction):
        await pet_bot_command(interaction)

    # Register bot_pick_fav command
    @tree.command(name='bot_pick_fav',
                  description='See who JohnnyBot prefers today')
    @app_commands.describe(
        user1="First potential favorite",
        user2="Second potential favorite"
    )
    async def _bot_pick_fav(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
        await bot_pick_fav_command(interaction, user1, user2)
        
    # Register message_dump command
    @tree.command(name='message_dump',
                  description='Dump a user\'s messages from a channel into a downloadable file')
    @app_commands.describe(
        user="User whose messages to dump",
        channel="Channel to dump messages from",
        start_date="Start date in YYYY-MM-DD format (e.g., 2025-01-01)",
        limit="Maximum number of messages to fetch (default: 1000)"
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _message_dump(interaction: discord.Interaction, user: discord.User,
                           channel: discord.TextChannel, start_date: str, limit: int = 1000):
        await message_dump_command(interaction, user, channel, start_date, limit)
    
    # Add error handler for message_dump
    _message_dump.on_error = message_dump_error

    # Register clone_category_permissions command
    @tree.command(name='clone_category_permissions',
                  description='Clone permissions from source category to destination category')
    @app_commands.describe(
        source_category='Source category to copy permissions from',
        destination_category='Destination category to copy permissions to'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clone_category_permissions(interaction: discord.Interaction,
                                        source_category: discord.CategoryChannel,
                                        destination_category: discord.CategoryChannel):
        await clone_category_permissions(interaction, source_category, destination_category)

    # Add error handler for clone_category_permissions
    _clone_category_permissions.on_error = clone_category_permissions_error

    # Register clone_channel_permissions command
    @tree.command(name='clone_channel_permissions',
                  description='Clone permissions from source channel to destination channel')
    @app_commands.describe(
        source_channel='Source channel to copy permissions from',
        destination_channel='Destination channel to copy permissions to'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clone_channel_permissions(interaction: discord.Interaction,
                                       source_channel: discord.abc.GuildChannel,
                                       destination_channel: discord.abc.GuildChannel):
        await clone_channel_permissions(interaction, source_channel, destination_channel)

    # Add error handler for clone_channel_permissions
    _clone_channel_permissions.on_error = clone_channel_permissions_error

    # Register clone_role_permissions command
    @tree.command(name='clone_role_permissions',
                  description='Clone permissions from source role to destination role')
    @app_commands.describe(
        source_role='Source role to copy permissions from',
        destination_role='Destination role to copy permissions to'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clone_role_permissions(interaction: discord.Interaction,
                                    source_role: discord.Role,
                                    destination_role: discord.Role):
        await clone_role_permissions(interaction, source_role, destination_role)

    # Add error handler for clone_role_permissions
    _clone_role_permissions.on_error = clone_role_permissions_error

    # Register clear_category_permissions command
    @tree.command(name='clear_category_permissions',
                  description='Clear all permission overwrites from a category')
    @app_commands.describe(
        category='Category to clear all permission overwrites from'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clear_category_permissions(interaction: discord.Interaction,
                                        category: discord.CategoryChannel):
        await clear_category_permissions(interaction, category)

    # Add error handler for clear_category_permissions
    _clear_category_permissions.on_error = clear_category_permissions_error

    # Register clear_channel_permissions command
    @tree.command(name='clear_channel_permissions',
                  description='Clear all permission overwrites from a channel')
    @app_commands.describe(
        channel='Channel to clear all permission overwrites from'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clear_channel_permissions(interaction: discord.Interaction,
                                       channel: discord.abc.GuildChannel):
        await clear_channel_permissions(interaction, channel)

    # Add error handler for clear_channel_permissions
    _clear_channel_permissions.on_error = clear_channel_permissions_error

    # Register clear_role_permissions command
    @tree.command(name='clear_role_permissions',
                  description='Clear all permissions from a role (reset to default)')
    @app_commands.describe(
        role='Role to clear all permissions from'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clear_role_permissions(interaction: discord.Interaction,
                                    role: discord.Role):
        await clear_role_permissions(interaction, role)

    # Add error handler for clear_role_permissions
    _clear_role_permissions.on_error = clear_role_permissions_error

def setup_commands(bot_param):
    """Initialize command module with bot instance and register commands."""
    # Using globals is necessary here to initialize module-level variables
    # pylint: disable=global-statement
    global bot_instance, tree, reminders, reminders_lock, reminder_threads, event_feed, message_dump_servers  # pylint: disable=line-too-long
    bot_instance = bot_param
    if bot_instance:
        tree = bot_instance.tree
    # Import cache from bot.py to maintain functionality
    from bot import cache  # pylint: disable=import-outside-toplevel,unused-import
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
    if event_feed:
        event_feed.scheduler = AsyncIOScheduler()

    # Register all commands
    register_commands()


class InvalidReminderInterval(Exception):
    """Exception raised when an invalid reminder interval is provided."""

def validate_reminder_interval(interval: int) -> None:
    """Validate that reminder interval is reasonable."""
    if interval < 60:
        raise InvalidReminderInterval("Interval must be at least 60 seconds")

def create_set_reminder_command():
    """Factory function to create the set_reminder command."""
    # Create the command with proper parameter definitions
    @app_commands.command(name='set_reminder', description='Sets a reminder message to be sent to a channel at regular intervals')
    @app_commands.describe(
        channel='Channel to send reminders to',
        title='Title of the reminder',
        message='Message content of the reminder',
        interval='Interval in seconds between reminders (minimum 60)'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def set_reminder_command(interaction: discord.Interaction,
                                  channel: discord.TextChannel, title: str,
                                  message: str, interval: int):
        """Sets a reminder message to be sent to a channel at regular intervals."""
        await set_reminder_callback(interaction, channel, title, message, interval)

    # Add error handler
    async def on_error(interaction: discord.Interaction, error):
        """Handles errors for the set_reminder command."""
        if isinstance(error, app_commands.errors.MissingRole):
            await interaction.response.send_message(
                'You do not have the required role to use this command.', ephemeral=True)
        elif isinstance(error, InvalidReminderInterval):
            await interaction.response.send_message(f'Invalid interval: {error}', ephemeral=True)
        elif isinstance(error, discord.HTTPException):
            logger.error('Discord API error: %s', error)
            await interaction.response.send_message('Discord API error occurred.', ephemeral=True)
        else:
            await interaction.response.send_message(f'Error: {error}', ephemeral=True)
    
    set_reminder_command.on_error = on_error
    return set_reminder_command

async def set_reminder_callback(interaction: discord.Interaction,
                               channel: discord.TextChannel, title: str,
                               message: str, interval: int):
    """Callback for the set_reminder command."""
    validate_reminder_interval(interval)
    if reminders_lock:
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

    await interaction.response.send_message(
        f'Reminder set in {channel.mention} every {interval} seconds.', ephemeral=True)


# Command functions defined at module level but registered in register_commands()
async def delete_all_reminders(interaction: discord.Interaction) -> None:
    """Delete all active reminders."""
    if reminders_lock:
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
        if reminders_lock:
            with reminders_lock:
                for channel_id, reminder_data in reminders.items():
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

async def kick_member(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
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

async def timeout_member(interaction: discord.Interaction, member: discord.Member, duration: int, reason: Optional[str] = None):
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

async def add_event_feed_command(interaction: discord.Interaction, calendar_url: str, channel_name: str = "bot-trap"):
    """Adds a calendar feed URL to check for events."""
    try:
        if not calendar_url.startswith(('http://', 'https://')):
            await interaction.response.send_message("Invalid URL format", ephemeral=True)
            return
        
        # Test the URL to make sure it's a valid calendar feed
        try:
            response = requests.get(calendar_url, timeout=10)
            response.raise_for_status()
            Calendar.from_ical(response.text)
        except (requests.RequestException, ValueError) as e:
            await interaction.response.send_message(
                f"Error accessing calendar feed: {str(e)}", ephemeral=True)
            return
        
        if event_feed and interaction.guild and interaction.guild.id not in event_feed.feeds:
            event_feed.feeds[interaction.guild.id] = {}
        if event_feed and interaction.guild:
            event_feed.feeds[interaction.guild.id][calendar_url] = {
                'last_checked': datetime.now(),
                'channel': channel_name,
                'posted_events': set()  # Track posted events to avoid duplicates
            }
            
            # Start the scheduler if it's not already running
            if event_feed.scheduler:
                if not event_feed.scheduler.running:
                    event_feed.scheduler.start()
                
                # Add the feed checking job if it doesn't exist
                try:
                    event_feed.scheduler.get_job('event_feed_checker')
                except (AttributeError, KeyError):
                    # Job doesn't exist, add it
                    event_feed.scheduler.add_job(
                        event_feed.check_feeds_job,
                        'interval',
                        hours=1,
                        id='event_feed_checker'
                    )
        
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

async def add_event_feed_error(interaction: discord.Interaction, error):
    """Handles errors for the add_event_feed command.
    
    Args:
        interaction: The Discord interaction object
        error: The error that occurred
    """
    if isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message('Discord API error occurred.', ephemeral=True)
    else:
        await interaction.response.send_message(f"Error: {str(error)}", ephemeral=True)

async def list_event_feeds_command(interaction: discord.Interaction):
    """Lists all registered calendar feeds."""
    if not event_feed or not interaction.guild or interaction.guild.id not in event_feed.feeds or not event_feed.feeds[interaction.guild.id]:
        await interaction.response.send_message('No calendar feeds registered', ephemeral=True)
        return
    feed_list = '\n'.join(event_feed.feeds[interaction.guild.id])
    await interaction.response.send_message(f'Registered calendar feeds:\n{feed_list}', ephemeral=True)

async def remove_event_feed_command(interaction: discord.Interaction, feed_url: str):
    """Removes a calendar feed."""
    if not event_feed or not interaction.guild or interaction.guild.id not in event_feed.feeds or feed_url not in event_feed.feeds[interaction.guild.id]:
        await interaction.response.send_message('Calendar feed not found', ephemeral=True)
        return
    del event_feed.feeds[interaction.guild.id][feed_url]
    await interaction.response.send_message(f'Removed calendar feed: {feed_url}', ephemeral=True)

async def bot_command(interaction: discord.Interaction):
    """Check on JohnnyBot."""
    try:
        bot_name = interaction.client.user.display_name if interaction.client.user else "JohnnyBot"
        # Import the function from bot.py
        from bot import get_time_based_message  # pylint: disable=import-outside-toplevel
        message = get_time_based_message(bot_name)
        logger.info('[%s] - bot command: %s', interaction.user, message)
        await interaction.response.send_message(message)
    except discord.errors.NotFound:
        # Handle case where interaction has timed out
        logger.warning("Interaction timed out for bot command from %s", interaction.user)

async def pet_bot_command(interaction: discord.Interaction):
    """Pet JohnnyBot."""
    try:
        bot_name = interaction.client.user.display_name if interaction.client.user else "JohnnyBot"
        # Define bot response messages inline and select efficiently
        bot_responses = [
            "BOTNAME purrs happily!",
            "BOTNAME rubs against your leg!",
            "BOTNAME gives you a slow blink of affection!",
            "BOTNAME meows appreciatively!",
            "BOTNAME headbutts your hand for more pets!"
        ]
        selected_response = random.choice(bot_responses)
        message = selected_response.replace("BOTNAME", bot_name)
        logger.info('[%s] - pet bot command: %s', interaction.user, message)
        await interaction.response.send_message(message)
    except discord.errors.NotFound:
        # Handle case where interaction has timed out
        logger.warning("Interaction timed out for pet_bot command from %s", interaction.user)

async def bot_pick_fav_command(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
    """See who JohnnyBot prefers today."""
    try:
        bot_name = interaction.client.user.display_name if interaction.client.user else "JohnnyBot"
        # More efficient user selection and message formatting
        users = [user1, user2]
        chosen_user = random.choice(users)
        message = f"{bot_name} is giving attention to {chosen_user.mention}!"
        logger.info('[%s] - bot pick fav command: %s', interaction.user, message)
        await interaction.response.send_message(message)
    except discord.errors.NotFound:
        # Handle case where interaction has timed out
        logger.warning("Interaction timed out for bot_pick_fav command from %s", interaction.user)

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
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Error cleaning up orphaned message dumps: %s", e)
        return cleaned_count

async def message_dump_command(interaction: discord.Interaction, user: discord.User, channel: discord.TextChannel,  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
                              start_date: str, limit: int = 1000):
    """Dump a user's messages from a channel into a downloadable file starting from a specific date."""
    try:
        # Clean up any orphaned dump files/folders
        logger.info("Checking for orphaned message dump files/folders...")
        cleaned_count = cleanup_orphaned_dumps()
        if cleaned_count > 0:
            logger.info("Cleaned up %s orphaned message dump directories", cleaned_count)
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
        # message_count = 0  # Unused variable
        # oldest_message = None  # Unused variable
        
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
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Error accessing channel: %s", e)
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
        logger.info("Starting message fetch for user %s in channel %s", user.id, channel.id)
        
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
                logger.info("Fetching batch with params: %s, processed so far: %s", fetch_kwargs, total_processed)
            
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
                    logger.info("Message %s from author ID: %s, target user ID: %s, match: %s",
                               msg.id, msg.author.id, user.id, msg.author.id == user.id)
                    
                    # Check if this message is from our target user
                    if msg.author.id == user.id:
                        # Format the message more efficiently
                        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        content = msg.content or "[No text content]"
                        
                        # Build message parts list for efficient joining
                        message_parts = [f"[{timestamp}] {content}"]
                        
                        # Handle attachments
                        if msg.attachments:
                            attachment_urls = [a.url for a in msg.attachments]
                            message_parts.append(f"\nAttachments: {', '.join(attachment_urls)}")
                        
                        # Handle embeds
                        if msg.embeds:
                            message_parts.append(f"\nEmbeds: {len(msg.embeds)} embed(s)")
                        
                        # Join all parts efficiently
                        formatted_message = ''.join(message_parts) + "\n\n"
                        current_batch.append(formatted_message)
                
                # Reset retry count on successful fetch
                retry_count = 0
                
            except discord.HTTPException as e:
                if "rate limited" in str(e).lower():
                    retry_delay = base_delay * (2 ** retry_count)
                    retry_count += 1
                    logger.warning("Rate limited. Retrying in %s seconds. Retry %s/%s",
                                  retry_delay, retry_count, max_retries)
                    
                    if retry_count <= max_retries:
                        await interaction.followup.send(
                            f"Hit Discord rate limit. Waiting {retry_delay} seconds before continuing...",
                            ephemeral=True
                        )
                        await asyncio.sleep(retry_delay)
                        # Skip the rest of this iteration and retry
                        continue
                    
                    logger.error("Max retries (%s) reached for rate limiting", max_retries)
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
            logger.info("Batch complete: processed %s messages from target user", batch_count)
            
            # Add the batch to our collection
            messages.extend(current_batch)
            
            # Send a progress update every 500 messages or at the end of a batch
            if total_processed % 500 == 0 or batch_count < batch_size:
                await interaction.followup.send(
                    f"Progress update: Processed {total_processed} messages, found {len(messages)} from {user.mention}...",
                    ephemeral=True
                )
            
            # Log the batch results
            logger.info("Batch complete: got %s messages in batch, of which %s were from target user",
                       messages_in_this_batch, batch_count)
            
            # If we got fewer messages than requested, we've reached the end
            if messages_in_this_batch == 0 or messages_in_this_batch < batch_size:
                logger.info("End of channel history reached. Total processed: %s, found: %s",
                           total_processed, len(messages))
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
        # Write file more efficiently using a single write operation
        header_parts = [
            f"Messages from {user.name} (ID: {user.id}) in #{channel.name}\n",
            f"Dump created at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            f"Start date: {start_date}\n",
            f"Messages found: {len(messages)}\n",
            f"Total messages processed: {total_processed}\n\n",
            "="*50 + "\n\n"
        ]
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(''.join(header_parts))
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
        # Build DM message more efficiently
        dm_parts = [
            f"Here's the message dump you requested from {channel.mention}:\n\n",
            f"**Download Link:** {download_url}\n",
            f"**User:** {user.mention}\n",
            f"**Start Date:** {start_date}\n",
            f"**Messages found:** {len(messages)}\n",
            f"**Messages processed:** {total_processed}\n",
            f"**Link expires:** {expiry_time} (30 minutes from now)\n\n",
            "The file will be automatically deleted after the link expires."
        ]
        dm_message = ''.join(dm_parts)
        
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
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Unexpected error in message_dump_command: %s', e)
        await interaction.followup.send("An unexpected error occurred.", ephemeral=True)

async def message_dump_error(interaction: discord.Interaction, error):
    """Handles errors for the message_dump command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    elif hasattr(app_commands, 'MissingRequiredArgument') and isinstance(error, getattr(app_commands, 'MissingRequiredArgument')):
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

async def clone_category_permissions(interaction: discord.Interaction,  # pylint: disable=too-many-branches
                                   source_category: discord.CategoryChannel,
                                   destination_category: discord.CategoryChannel):
    """Clone permissions from source category to destination category."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Verify both categories are in the same guild
        if source_category.guild.id != destination_category.guild.id:
            await interaction.followup.send(
                'Source and destination categories must be in the same server.',
                ephemeral=True
            )
            return
        
        # Clear existing permissions on destination category
        await interaction.followup.send(
            f'Clearing existing permissions on {destination_category.name}...',
            ephemeral=True
        )
        
        # Get the bot's highest role for hierarchy checking
        bot_member = destination_category.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        # Get all current overwrites and clear them (except Administrator, managed, and hierarchy-protected roles)
        for target in list(destination_category.overwrites.keys()):
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        logger.info('Skipped clearing Administrator role %s on category %s', target, destination_category.name)
                        continue
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        logger.info('Skipped clearing managed role %s on category %s', target, destination_category.name)
                        continue
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                        logger.info('Skipped clearing role %s (hierarchy: role position %d >= bot position %d)',
                                   target.name, target.position, bot_top_role.position)
                        continue
                    # Additional check for privileged roles that might cause issues
                    if isinstance(target, discord.Role):
                        if target.permissions.manage_roles or target.permissions.manage_guild or target.permissions.manage_channels:
                            if bot_top_role and target >= bot_top_role:
                                logger.info('Skipped clearing privileged role %s (has management permissions and position %d >= bot position %d)',
                                           target.name, target.position, bot_top_role.position)
                                continue
                    await destination_category.set_permissions(target, overwrite=None)
                    logger.info('Cleared permissions for %s on category %s', target, destination_category.name)
            except discord.Forbidden as e:
                # Log as hierarchy issue if it's a role
                if isinstance(target, discord.Role):
                    logger.error('Failed to clear permissions for role %s (likely hierarchy issue or Discord\'s limitation on bots not being allowed go manage Moderator-style roles. You must manage these manually): %s', target.name, e)
                else:
                    logger.error('Failed to clear permissions for %s: %s', target, e)
                # Continue without spamming user with individual errors
                continue
            except discord.HTTPException as e:
                logger.error('Failed to clear permissions for %s: %s', target, e)
        
        # Copy permissions from source to destination
        await interaction.followup.send(
            f'Copying permissions from {source_category.name} to {destination_category.name}...',
            ephemeral=True
        )
        
        copied_count = 0
        skipped_admin_count = 0
        skipped_managed_count = 0
        skipped_hierarchy_count = 0
        failed_hierarchy_roles = []  # Track specific roles that failed due to hierarchy
        
        # Get the bot's highest role for hierarchy checking
        bot_member = destination_category.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        # Log bot's role information for debugging
        if bot_top_role:
            logger.info('Bot\'s highest role: %s (position: %d)', bot_top_role.name, bot_top_role.position)
        else:
            logger.warning('Could not determine bot\'s highest role')
        
        for target, overwrite in source_category.overwrites.items():
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        skipped_admin_count += 1
                        logger.info('Skipped copying Administrator role %s from %s to %s',
                                   target, source_category.name, destination_category.name)
                        continue
                    
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        skipped_managed_count += 1
                        logger.info('Skipped copying managed role %s from %s to %s',
                                   target, source_category.name, destination_category.name)
                        continue
                    
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role:
                        # Log role comparison for debugging
                        logger.info('Checking role %s (position: %d) vs bot role %s (position: %d)',
                                   target.name, target.position, bot_top_role.name, bot_top_role.position)
                        if target >= bot_top_role:
                            skipped_hierarchy_count += 1
                            failed_hierarchy_roles.append(target.name)
                            logger.info('Skipped copying role %s (hierarchy: role position %d >= bot position %d)',
                                       target.name, target.position, bot_top_role.position)
                            continue
                    
                    # Test if we can actually modify this role by checking Discord's restrictions
                    if isinstance(target, discord.Role):
                        # Check if the bot can manage this role
                        if not bot_member.guild_permissions.manage_roles:
                            logger.info('Skipped copying role %s (bot lacks manage_roles permission)', target.name)
                            continue
                        
                        # Check for Discord's restricted permissions that bots cannot manage
                        # Discord prevents bots from managing roles with these dangerous permissions
                        dangerous_perms = [
                            target.permissions.ban_members,
                            target.permissions.kick_members,
                            target.permissions.manage_roles,
                            target.permissions.manage_guild,
                            target.permissions.manage_channels,
                            target.permissions.manage_messages,
                            target.permissions.moderate_members,
                            target.permissions.administrator
                        ]
                        
                        if any(dangerous_perms):
                            skipped_hierarchy_count += 1
                            failed_hierarchy_roles.append(target.name)
                            logger.info('Skipped copying role %s (Discord restricts bots from managing roles with moderation permissions like ban_members, kick_members, etc.)',
                                       target.name)
                            continue
                    
                    await destination_category.set_permissions(target, overwrite=overwrite)
                    copied_count += 1
                    logger.info('Copied permissions for %s from %s to %s',
                               target, source_category.name, destination_category.name)
            except discord.Forbidden as e:
                # Log the specific role that failed and provide detailed feedback
                if isinstance(target, discord.Role):
                    skipped_hierarchy_count += 1
                    failed_hierarchy_roles.append(target.name)
                    
                    # Check if this is likely due to Discord's moderation permission restrictions
                    dangerous_perms = [
                        target.permissions.ban_members,
                        target.permissions.kick_members,
                        target.permissions.manage_roles,
                        target.permissions.manage_guild,
                        target.permissions.manage_channels,
                        target.permissions.manage_messages,
                        target.permissions.moderate_members,
                        target.permissions.administrator
                    ]
                    
                    if any(dangerous_perms):
                        logger.error('Failed to copy permissions for role %s - Discord restricts bots from managing roles with moderation permissions (ban_members, kick_members, etc.): %s', target.name, e)
                        await interaction.followup.send(
                            f'⚠️ **Discord Restriction**: Cannot copy permissions for role **{target.name}** because Discord prevents bots from managing roles with moderation permissions like ban_members, kick_members, manage_roles, etc. You\'ll need to copy these permissions manually.',
                            ephemeral=True
                        )
                    else:
                        logger.error('Failed to copy permissions for role %s (likely hierarchy issue): %s', target.name, e)
                else:
                    logger.error('Failed to copy permissions for %s: %s', target, e)
                continue
            except discord.HTTPException as e:
                logger.error('Failed to copy permissions for %s: %s', target, e)
                await interaction.followup.send(
                    f'Warning: Failed to copy permissions for {target}: {e}',
                    ephemeral=True
                )
        
        success_msg = (
            f'Successfully cloned permissions from **{source_category.name}** to **{destination_category.name}**.\n'
            f'Copied {copied_count} permission overrides.'
        )
        
        notes = []
        if skipped_admin_count > 0:
            notes.append(f'Skipped {skipped_admin_count} Administrator role(s) for security reasons')
        if skipped_managed_count > 0:
            notes.append(f'Skipped {skipped_managed_count} managed role(s) (bot roles, booster roles, etc.)')
        if skipped_hierarchy_count > 0:
            notes.append(f'Skipped {skipped_hierarchy_count} role(s) due to Discord\'s restrictions on bots managing roles with moderation permissions (ban_members, kick_members, etc.)')
        
        if notes:
            success_msg += f'\n\n⚠️ **Note:** {", ".join(notes)}.'
        
        await interaction.followup.send(success_msg, ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage permissions on one or both categories.\n\n'
            '**Possible causes:**\n'
            '• I lack "Manage Channels" permission\n'
            '• I lack "Manage Roles" permission\n'
            '• My role is not high enough in the hierarchy to modify permissions for some roles/members\n'
            '• Discord restricts bots from managing roles with moderation permissions (ban_members, kick_members, manage_roles, etc.)\n\n'
            '**Solutions:**\n'
            '• Ensure I have "Manage Channels" and "Manage Roles" permissions\n'
            '• Move my role higher than the roles you want to copy permissions for in Server Settings > Roles\n'
            '• For roles with moderation permissions, you\'ll need to copy their permissions manually as Discord prevents bots from managing these roles for security reasons',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clone_category_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while cloning permissions.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clone_category_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while cloning permissions.',
            ephemeral=True
        )

async def clone_category_permissions_error(interaction: discord.Interaction, error):
    """Handles errors for the clone_category_permissions command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message(
            'You do not have the required role to use this command.',
            ephemeral=True
        )
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message(
            'Discord API error occurred.',
            ephemeral=True
        )
    else:
        logger.error('Error in clone_category_permissions command: %s', error)
        await interaction.response.send_message(
            f'Error: {error}',
            ephemeral=True
        )

async def clone_channel_permissions(interaction: discord.Interaction,  # pylint: disable=too-many-branches
                                  source_channel: discord.abc.GuildChannel,
                                  destination_channel: discord.abc.GuildChannel):
    """Clone permissions from source channel to destination channel."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Verify both channels are in the same guild
        if source_channel.guild.id != destination_channel.guild.id:
            await interaction.followup.send(
                'Source and destination channels must be in the same server.',
                ephemeral=True
            )
            return
        
        # Clear existing permissions on destination channel
        await interaction.followup.send(
            f'Clearing existing permissions on {destination_channel.name}...',
            ephemeral=True
        )
        
        # Get the bot's highest role for hierarchy checking
        bot_member = destination_channel.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        # Get all current overwrites and clear them (except Administrator, managed, and hierarchy-protected roles)
        for target in list(destination_channel.overwrites.keys()):
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        logger.info('Skipped clearing Administrator role %s on channel %s', target, destination_channel.name)
                        continue
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        logger.info('Skipped clearing managed role %s on channel %s', target, destination_channel.name)
                        continue
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                        logger.info('Skipped clearing role %s (hierarchy: role position %d >= bot position %d)',
                                   target.name, target.position, bot_top_role.position)
                        continue
                    await destination_channel.set_permissions(target, overwrite=None)
                    logger.info('Cleared permissions for %s on channel %s', target, destination_channel.name)
            except discord.Forbidden as e:
                logger.error('Failed to clear permissions for %s: %s', target, e)
                # Continue without spamming user with individual errors
                continue
            except discord.HTTPException as e:
                logger.error('Failed to clear permissions for %s: %s', target, e)
        
        # Copy permissions from source to destination
        await interaction.followup.send(
            f'Copying permissions from {source_channel.name} to {destination_channel.name}...',
            ephemeral=True
        )
        
        copied_count = 0
        skipped_admin_count = 0
        skipped_managed_count = 0
        skipped_hierarchy_count = 0
        
        # Get the bot's highest role for hierarchy checking
        bot_member = destination_channel.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        for target, overwrite in source_channel.overwrites.items():
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        skipped_admin_count += 1
                        logger.info('Skipped copying Administrator role %s from %s to %s',
                                   target, source_channel.name, destination_channel.name)
                        continue
                    
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        skipped_managed_count += 1
                        logger.info('Skipped copying managed role %s from %s to %s',
                                   target, source_channel.name, destination_channel.name)
                        continue
                    
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                        skipped_hierarchy_count += 1
                        logger.info('Skipped copying role %s (hierarchy: role position %d >= bot position %d)',
                                   target.name, target.position, bot_top_role.position)
                        continue
                    
                    # Check for Discord's restricted permissions that bots cannot manage
                    if isinstance(target, discord.Role):
                        dangerous_perms = [
                            target.permissions.ban_members,
                            target.permissions.kick_members,
                            target.permissions.manage_roles,
                            target.permissions.manage_guild,
                            target.permissions.manage_channels,
                            target.permissions.manage_messages,
                            target.permissions.moderate_members,
                            target.permissions.administrator
                        ]
                        
                        if any(dangerous_perms):
                            skipped_hierarchy_count += 1
                            logger.info('Skipped copying role %s (Discord restricts bots from managing roles with moderation permissions)',
                                       target.name)
                            continue
                    
                    await destination_channel.set_permissions(target, overwrite=overwrite)
                    copied_count += 1
                    logger.info('Copied permissions for %s from %s to %s',
                               target, source_channel.name, destination_channel.name)
            except discord.Forbidden as e:
                # Log the specific role that failed and provide detailed feedback
                if isinstance(target, discord.Role):
                    skipped_hierarchy_count += 1
                    
                    # Check if this is likely due to Discord's moderation permission restrictions
                    dangerous_perms = [
                        target.permissions.ban_members,
                        target.permissions.kick_members,
                        target.permissions.manage_roles,
                        target.permissions.manage_guild,
                        target.permissions.manage_channels,
                        target.permissions.manage_messages,
                        target.permissions.moderate_members,
                        target.permissions.administrator
                    ]
                    
                    if any(dangerous_perms):
                        logger.error('Failed to copy permissions for role %s - Discord restricts bots from managing roles with moderation permissions: %s', target.name, e)
                        await interaction.followup.send(
                            f'⚠️ **Discord Restriction**: Cannot copy permissions for role **{target.name}** because Discord prevents bots from managing roles with moderation permissions like ban_members, kick_members, manage_roles, etc. You\'ll need to copy these permissions manually.',
                            ephemeral=True
                        )
                    else:
                        logger.error('Failed to copy permissions for role %s (likely hierarchy issue): %s', target.name, e)
                else:
                    logger.error('Failed to copy permissions for %s: %s', target, e)
                continue
            except discord.HTTPException as e:
                logger.error('Failed to copy permissions for %s: %s', target, e)
                await interaction.followup.send(
                    f'Warning: Failed to copy permissions for {target}: {e}',
                    ephemeral=True
                )
        
        success_msg = (
            f'Successfully cloned permissions from **{source_channel.name}** to **{destination_channel.name}**.\n'
            f'Copied {copied_count} permission overrides.'
        )
        
        notes = []
        if skipped_admin_count > 0:
            notes.append(f'Skipped {skipped_admin_count} Administrator role(s) for security reasons')
        if skipped_managed_count > 0:
            notes.append(f'Skipped {skipped_managed_count} managed role(s) (bot roles, booster roles, etc.)')
        if skipped_hierarchy_count > 0:
            notes.append(f'Skipped {skipped_hierarchy_count} role(s) due to hierarchy or Discord\'s restrictions on bots managing roles with moderation permissions')
        
        if notes:
            success_msg += f'\n\n⚠️ **Note:** {", ".join(notes)}.'
        
        await interaction.followup.send(success_msg, ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage permissions on one or both channels.\n\n'
            '**Possible causes:**\n'
            '• I lack "Manage Channels" permission\n'
            '• I lack "Manage Roles" permission\n'
            '• My role is not high enough in the hierarchy to modify permissions for some roles/members\n'
            '• Discord restricts bots from managing roles with moderation permissions (ban_members, kick_members, etc.)\n'
            '• The channels are in categories I cannot access\n\n'
            '**Solutions:**\n'
            '• Ensure I have "Manage Channels" and "Manage Roles" permissions\n'
            '• Move my role higher than the roles you want to copy permissions for in Server Settings > Roles\n'
            '• Check that I can view and access both channels\n'
            '• For roles with moderation permissions, you\'ll need to copy their permissions manually',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clone_channel_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while cloning permissions.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clone_channel_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while cloning permissions.',
            ephemeral=True
        )

async def clone_channel_permissions_error(interaction: discord.Interaction, error):
    """Handles errors for the clone_channel_permissions command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message(
            'You do not have the required role to use this command.',
            ephemeral=True
        )
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message(
            'Discord API error occurred.',
            ephemeral=True
        )
    else:
        logger.error('Error in clone_channel_permissions command: %s', error)
        await interaction.response.send_message(
            f'Error: {error}',
            ephemeral=True
        )

async def clone_role_permissions(interaction: discord.Interaction,  # pylint: disable=too-many-return-statements,too-many-branches,too-many-statements
                               source_role: discord.Role,
                               destination_role: discord.Role):
    """Clone permissions from source role to destination role."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Verify both roles are in the same guild
        if source_role.guild.id != destination_role.guild.id:
            await interaction.followup.send(
                'Source and destination roles must be in the same server.',
                ephemeral=True
            )
            return
        
        # Check if we're trying to clone to/from @everyone role
        if source_role.is_default() or destination_role.is_default():
            await interaction.followup.send(
                'Cannot clone permissions to or from the @everyone role.',
                ephemeral=True
            )
            return
        
        # Check if we're trying to clone to/from a role higher than the bot's highest role
        if interaction.guild:
            bot_member = interaction.guild.me
            if bot_member:
                if source_role >= bot_member.top_role:
                    await interaction.followup.send(
                        f'Cannot clone permissions from **{source_role.name}** - it is higher than or equal to my highest role (**{bot_member.top_role.name}**).\n'
                        f'Please move my role higher in the server settings, or choose a different source role.',
                        ephemeral=True
                    )
                    return
                if destination_role >= bot_member.top_role:
                    await interaction.followup.send(
                        f'Cannot clone permissions to **{destination_role.name}** - it is higher than or equal to my highest role (**{bot_member.top_role.name}**).\n'
                        f'Please move my role higher in the server settings, or choose a different destination role.',
                        ephemeral=True
                    )
                    return
        
        # Check if we're trying to clone to/from a role higher than the user's highest role
        # interaction.user might be a User, we need to get the Member object
        if interaction.guild and hasattr(interaction, 'user'):
            member = interaction.guild.get_member(interaction.user.id)
            if member:
                if source_role >= member.top_role:
                    await interaction.followup.send(
                        f'Cannot clone permissions from **{source_role.name}** - it is higher than or equal to your highest role (**{member.top_role.name}**).',
                        ephemeral=True
                    )
                    return
                if destination_role >= member.top_role:
                    await interaction.followup.send(
                        f'Cannot clone permissions to **{destination_role.name}** - it is higher than or equal to your highest role (**{member.top_role.name}**).',
                        ephemeral=True
                    )
                    return
        
        # Check if the bot has manage_roles permission
        if interaction.guild and interaction.guild.me:
            bot_permissions = interaction.guild.me.guild_permissions
            if not bot_permissions.manage_roles:
                await interaction.followup.send(
                    'I do not have the "Manage Roles" permission required to clone role permissions.\n'
                    'Please grant me this permission in the server settings.',
                    ephemeral=True
                )
                return
        
        await interaction.followup.send(
            f'Cloning permissions from **{source_role.name}** to **{destination_role.name}**...',
            ephemeral=True
        )
        
        # Copy the permissions from source role to destination role (excluding Administrator)
        try:
            # Create a copy of source permissions but exclude Administrator permission
            new_permissions = discord.Permissions(source_role.permissions.value)
            new_permissions.administrator = False
            
            await destination_role.edit(
                permissions=new_permissions,
                reason=f'Permissions cloned from {source_role.name} by {interaction.user} (Administrator permission excluded)'
            )
            
            # Check if Administrator permission was excluded
            admin_excluded = source_role.permissions.administrator and not new_permissions.administrator
            success_msg = (
                f'Successfully cloned permissions from **{source_role.name}** to **{destination_role.name}**.\n'
                f'The destination role now has the same server-wide permissions as the source role.'
            )
            
            if admin_excluded:
                success_msg += '\n\n⚠️ **Note:** Administrator permission was excluded for security reasons.'
            
            await interaction.followup.send(success_msg, ephemeral=True)
            
            logger.info('Cloned permissions from role %s to role %s by user %s',
                       source_role.name, destination_role.name, interaction.user)
            
        except discord.Forbidden as e:
            error_msg = (
                f'Failed to clone permissions: Missing permissions.\n\n'
                f'**Possible causes:**\n'
                f'• My role is not high enough in the hierarchy to modify **{destination_role.name}**\n'
                f'• I lack the "Manage Roles" permission\n'
                f'• The destination role has special permissions I cannot modify\n\n'
                f'**Solutions:**\n'
                f'• Move my role above **{destination_role.name}** in Server Settings > Roles\n'
                f'• Ensure I have "Manage Roles" permission\n'
                f'• Try cloning to a role lower in the hierarchy'
            )
            logger.error('Failed to clone role permissions due to insufficient permissions: %s', e)
            await interaction.followup.send(error_msg, ephemeral=True)
        except discord.HTTPException as e:
            logger.error('Failed to clone role permissions: %s', e)
            await interaction.followup.send(
                f'Failed to clone permissions due to a Discord API error: {e}',
                ephemeral=True
            )
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage one or both of these roles.',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clone_role_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while cloning permissions.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clone_role_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while cloning permissions.',
            ephemeral=True
        )

async def clone_role_permissions_error(interaction: discord.Interaction, error):
    """Handles errors for the clone_role_permissions command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message(
            'You do not have the required role to use this command.',
            ephemeral=True
        )
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message(
            'Discord API error occurred.',
            ephemeral=True
        )
    else:
        logger.error('Error in clone_role_permissions command: %s', error)
        await interaction.response.send_message(
            f'Error: {error}',
            ephemeral=True
        )


async def clear_category_permissions(interaction: discord.Interaction,
                                   category: discord.CategoryChannel):
    """Clear all permission overwrites from a category."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Get the bot's highest role for hierarchy checking
        bot_member = category.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        cleared_count = 0
        skipped_admin_count = 0
        skipped_managed_count = 0
        skipped_hierarchy_count = 0
        failed_roles = []
        
        await interaction.followup.send(
            f'Clearing all permission overwrites from **{category.name}**...',
            ephemeral=True
        )
        
        # Get all current overwrites and clear them
        for target in list(category.overwrites.keys()):
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        skipped_admin_count += 1
                        logger.info('Skipped clearing Administrator role %s on category %s', target, category.name)
                        continue
                    
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        skipped_managed_count += 1
                        logger.info('Skipped clearing managed role %s on category %s', target, category.name)
                        continue
                    
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                        skipped_hierarchy_count += 1
                        logger.info('Skipped clearing role %s (hierarchy: role position %d >= bot position %d)',
                                   target.name, target.position, bot_top_role.position)
                        continue
                    
                    # Check for Discord's restricted permissions that bots cannot manage
                    if isinstance(target, discord.Role):
                        dangerous_perms = [
                            target.permissions.ban_members,
                            target.permissions.kick_members,
                            target.permissions.manage_roles,
                            target.permissions.manage_guild,
                            target.permissions.manage_channels,
                            target.permissions.manage_messages,
                            target.permissions.moderate_members,
                            target.permissions.administrator
                        ]
                        
                        if any(dangerous_perms):
                            skipped_hierarchy_count += 1
                            failed_roles.append(target.name)
                            logger.info('Skipped clearing role %s (Discord restricts bots from managing roles with moderation permissions)',
                                       target.name)
                            continue
                    
                    await category.set_permissions(target, overwrite=None)
                    cleared_count += 1
                    logger.info('Cleared permissions for %s on category %s', target, category.name)
                    
            except discord.Forbidden as e:
                # Log the specific role that failed and provide detailed feedback
                if isinstance(target, discord.Role):
                    skipped_hierarchy_count += 1
                    failed_roles.append(target.name)
                    
                    # Check if this is likely due to Discord's moderation permission restrictions
                    dangerous_perms = [
                        target.permissions.ban_members,
                        target.permissions.kick_members,
                        target.permissions.manage_roles,
                        target.permissions.manage_guild,
                        target.permissions.manage_channels,
                        target.permissions.manage_messages,
                        target.permissions.moderate_members,
                        target.permissions.administrator
                    ]
                    
                    if any(dangerous_perms):
                        logger.error('Failed to clear permissions for role %s - Discord restricts bots from managing roles with moderation permissions: %s', target.name, e)
                        await interaction.followup.send(
                            f'⚠️ **Discord Restriction**: Cannot clear permissions for role **{target.name}** because Discord prevents bots from managing roles with moderation permissions. You\'ll need to clear these permissions manually.',
                            ephemeral=True
                        )
                    else:
                        logger.error('Failed to clear permissions for role %s (likely hierarchy issue): %s', target.name, e)
                else:
                    logger.error('Failed to clear permissions for %s: %s', target, e)
                continue
            except discord.HTTPException as e:
                logger.error('Failed to clear permissions for %s: %s', target, e)
                await interaction.followup.send(
                    f'Warning: Failed to clear permissions for {target}: {e}',
                    ephemeral=True
                )
        
        success_msg = (
            f'Successfully cleared permissions from **{category.name}**.\n'
            f'Cleared {cleared_count} permission overwrites.'
        )
        
        notes = []
        if skipped_admin_count > 0:
            notes.append(f'Skipped {skipped_admin_count} Administrator role(s) for security reasons')
        if skipped_managed_count > 0:
            notes.append(f'Skipped {skipped_managed_count} managed role(s) (bot roles, booster roles, etc.)')
        if skipped_hierarchy_count > 0:
            notes.append(f'Skipped {skipped_hierarchy_count} role(s) due to Discord\'s restrictions on bots managing roles with moderation permissions')
        
        if notes:
            success_msg += f'\n\n⚠️ **Note:** {", ".join(notes)}.'
        
        await interaction.followup.send(success_msg, ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage permissions on this category.\n\n'
            '**Possible causes:**\n'
            '• I lack "Manage Channels" permission\n'
            '• I lack "Manage Roles" permission\n'
            '• My role is not high enough in the hierarchy to modify permissions for some roles/members\n'
            '• Discord restricts bots from managing roles with moderation permissions\n\n'
            '**Solutions:**\n'
            '• Ensure I have "Manage Channels" and "Manage Roles" permissions\n'
            '• Move my role higher than the roles you want to clear permissions for\n'
            '• For roles with moderation permissions, you\'ll need to clear their permissions manually',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clear_category_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while clearing permissions.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clear_category_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while clearing permissions.',
            ephemeral=True
        )

async def clear_category_permissions_error(interaction: discord.Interaction, error):
    """Handles errors for the clear_category_permissions command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message(
            'You do not have the required role to use this command.',
            ephemeral=True
        )
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message(
            'Discord API error occurred.',
            ephemeral=True
        )
    else:
        logger.error('Error in clear_category_permissions command: %s', error)
        await interaction.response.send_message(
            f'Error: {error}',
            ephemeral=True
        )

async def clear_channel_permissions(interaction: discord.Interaction,
                                  channel: discord.abc.GuildChannel):
    """Clear all permission overwrites from a channel."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Get the bot's highest role for hierarchy checking
        bot_member = channel.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        cleared_count = 0
        skipped_admin_count = 0
        skipped_managed_count = 0
        skipped_hierarchy_count = 0
        
        await interaction.followup.send(
            f'Clearing all permission overwrites from **{channel.name}**...',
            ephemeral=True
        )
        
        # Get all current overwrites and clear them
        for target in list(channel.overwrites.keys()):
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        skipped_admin_count += 1
                        logger.info('Skipped clearing Administrator role %s on channel %s', target, channel.name)
                        continue
                    
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        skipped_managed_count += 1
                        logger.info('Skipped clearing managed role %s on channel %s', target, channel.name)
                        continue
                    
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                        skipped_hierarchy_count += 1
                        logger.info('Skipped clearing role %s (hierarchy: role position %d >= bot position %d)',
                                   target.name, target.position, bot_top_role.position)
                        continue
                    
                    # Check for Discord's restricted permissions that bots cannot manage
                    if isinstance(target, discord.Role):
                        dangerous_perms = [
                            target.permissions.ban_members,
                            target.permissions.kick_members,
                            target.permissions.manage_roles,
                            target.permissions.manage_guild,
                            target.permissions.manage_channels,
                            target.permissions.manage_messages,
                            target.permissions.moderate_members,
                            target.permissions.administrator
                        ]
                        
                        if any(dangerous_perms):
                            skipped_hierarchy_count += 1
                            logger.info('Skipped clearing role %s (Discord restricts bots from managing roles with moderation permissions)',
                                       target.name)
                            continue
                    
                    await channel.set_permissions(target, overwrite=None)
                    cleared_count += 1
                    logger.info('Cleared permissions for %s on channel %s', target, channel.name)
                    
            except discord.Forbidden as e:
                # Log the specific role that failed and provide detailed feedback
                if isinstance(target, discord.Role):
                    skipped_hierarchy_count += 1
                    
                    # Check if this is likely due to Discord's moderation permission restrictions
                    dangerous_perms = [
                        target.permissions.ban_members,
                        target.permissions.kick_members,
                        target.permissions.manage_roles,
                        target.permissions.manage_guild,
                        target.permissions.manage_channels,
                        target.permissions.manage_messages,
                        target.permissions.moderate_members,
                        target.permissions.administrator
                    ]
                    
                    if any(dangerous_perms):
                        logger.error('Failed to clear permissions for role %s - Discord restricts bots from managing roles with moderation permissions: %s', target.name, e)
                        await interaction.followup.send(
                            f'⚠️ **Discord Restriction**: Cannot clear permissions for role **{target.name}** because Discord prevents bots from managing roles with moderation permissions. You\'ll need to clear these permissions manually.',
                            ephemeral=True
                        )
                    else:
                        logger.error('Failed to clear permissions for role %s (likely hierarchy issue): %s', target.name, e)
                else:
                    logger.error('Failed to clear permissions for %s: %s', target, e)
                continue
            except discord.HTTPException as e:
                logger.error('Failed to clear permissions for %s: %s', target, e)
                await interaction.followup.send(
                    f'Warning: Failed to clear permissions for {target}: {e}',
                    ephemeral=True
                )
        
        success_msg = (
            f'Successfully cleared permissions from **{channel.name}**.\n'
            f'Cleared {cleared_count} permission overwrites.'
        )
        
        notes = []
        if skipped_admin_count > 0:
            notes.append(f'Skipped {skipped_admin_count} Administrator role(s) for security reasons')
        if skipped_managed_count > 0:
            notes.append(f'Skipped {skipped_managed_count} managed role(s) (bot roles, booster roles, etc.)')
        if skipped_hierarchy_count > 0:
            notes.append(f'Skipped {skipped_hierarchy_count} role(s) due to hierarchy or Discord\'s restrictions on bots managing roles with moderation permissions')
        
        if notes:
            success_msg += f'\n\n⚠️ **Note:** {", ".join(notes)}.'
        
        await interaction.followup.send(success_msg, ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage permissions on this channel.\n\n'
            '**Possible causes:**\n'
            '• I lack "Manage Channels" permission\n'
            '• I lack "Manage Roles" permission\n'
            '• My role is not high enough in the hierarchy to modify permissions for some roles/members\n'
            '• Discord restricts bots from managing roles with moderation permissions\n\n'
            '**Solutions:**\n'
            '• Ensure I have "Manage Channels" and "Manage Roles" permissions\n'
            '• Move my role higher than the roles you want to clear permissions for\n'
            '• For roles with moderation permissions, you\'ll need to clear their permissions manually',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clear_channel_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while clearing permissions.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clear_channel_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while clearing permissions.',
            ephemeral=True
        )

async def clear_channel_permissions_error(interaction: discord.Interaction, error):
    """Handles errors for the clear_channel_permissions command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message(
            'You do not have the required role to use this command.',
            ephemeral=True
        )
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message(
            'Discord API error occurred.',
            ephemeral=True
        )
    else:
        logger.error('Error in clear_channel_permissions command: %s', error)
        await interaction.response.send_message(
            f'Error: {error}',
            ephemeral=True
        )

async def clear_role_permissions(interaction: discord.Interaction,
                               role: discord.Role):
    """Clear all permissions from a role (reset to default)."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Check if we're trying to clear the @everyone role
        if role.is_default():
            await interaction.followup.send(
                'Cannot clear permissions from the @everyone role.',
                ephemeral=True
            )
            return
        
        # Check if we're trying to clear a role higher than the bot's highest role
        if interaction.guild:
            bot_member = interaction.guild.me
            if bot_member:
                if role >= bot_member.top_role:
                    await interaction.followup.send(
                        f'Cannot clear permissions from **{role.name}** - it is higher than or equal to my highest role (**{bot_member.top_role.name}**).\n'
                        f'Please move my role higher in the server settings, or choose a different role.',
                        ephemeral=True
                    )
                    return
        
        # Check if we're trying to clear a role higher than the user's highest role
        if interaction.guild and hasattr(interaction, 'user'):
            member = interaction.guild.get_member(interaction.user.id)
            if member:
                if role >= member.top_role:
                    await interaction.followup.send(
                        f'Cannot clear permissions from **{role.name}** - it is higher than or equal to your highest role (**{member.top_role.name}**).',
                        ephemeral=True
                    )
                    return
        
        # Check if the bot has manage_roles permission
        if interaction.guild and interaction.guild.me:
            bot_permissions = interaction.guild.me.guild_permissions
            if not bot_permissions.manage_roles:
                await interaction.followup.send(
                    'I do not have the "Manage Roles" permission required to clear role permissions.\n'
                    'Please grant me this permission in the server settings.',
                    ephemeral=True
                )
                return
        
        # Check for Discord's restricted permissions
        dangerous_perms = [
            role.permissions.ban_members,
            role.permissions.kick_members,
            role.permissions.manage_roles,
            role.permissions.manage_guild,
            role.permissions.manage_channels,
            role.permissions.manage_messages,
            role.permissions.moderate_members,
            role.permissions.administrator
        ]
        
        if any(dangerous_perms):
            await interaction.followup.send(
                f'⚠️ **Discord Restriction**: Cannot clear permissions from role **{role.name}** because Discord prevents bots from managing roles with moderation permissions like ban_members, kick_members, manage_roles, etc. You\'ll need to clear these permissions manually.',
                ephemeral=True
            )
            return
        
        await interaction.followup.send(
            f'Clearing all permissions from **{role.name}**...',
            ephemeral=True
        )
        
        # Reset the role to default permissions (no permissions)
        try:
            default_permissions = discord.Permissions.none()
            
            await role.edit(
                permissions=default_permissions,
                reason=f'Permissions cleared by {interaction.user}'
            )
            
            success_msg = (
                f'Successfully cleared all permissions from **{role.name}**.\n'
                f'The role now has no special permissions (default state).'
            )
            
            await interaction.followup.send(success_msg, ephemeral=True)
            
            logger.info('Cleared permissions from role %s by user %s',
                       role.name, interaction.user)
            
        except discord.Forbidden as e:
            error_msg = (
                f'Failed to clear permissions: Missing permissions.\n\n'
                f'**Possible causes:**\n'
                f'• My role is not high enough in the hierarchy to modify **{role.name}**\n'
                f'• I lack the "Manage Roles" permission\n'
                f'• The role has special permissions I cannot modify\n'
                f'• Discord restricts bots from managing roles with moderation permissions\n\n'
                f'**Solutions:**\n'
                f'• Move my role above **{role.name}** in Server Settings > Roles\n'
                f'• Ensure I have "Manage Roles" permission\n'
                f'• For roles with moderation permissions, you\'ll need to clear manually'
            )
            logger.error('Failed to clear role permissions due to insufficient permissions: %s', e)
            await interaction.followup.send(error_msg, ephemeral=True)
        except discord.HTTPException as e:
            logger.error('Failed to clear role permissions: %s', e)
            await interaction.followup.send(
                f'Failed to clear permissions due to a Discord API error: {e}',
                ephemeral=True
            )
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage this role.',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clear_role_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while clearing permissions.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clear_role_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while clearing permissions.',
            ephemeral=True
        )

async def clear_role_permissions_error(interaction: discord.Interaction, error):
    """Handles errors for the clear_role_permissions command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message(
            'You do not have the required role to use this command.',
            ephemeral=True
        )
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        await interaction.response.send_message(
            'Discord API error occurred.',
            ephemeral=True
        )
    else:
        logger.error('Error in clear_role_permissions command: %s', error)
        await interaction.response.send_message(
            f'Error: {error}',
            ephemeral=True
        )

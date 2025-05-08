import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from icalendar import Calendar
import requests
from datetime import datetime, timedelta
import logging
import threading
import os
import json
from datetime import timedelta
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands
from functools import wraps
from typing import Any, Callable, Coroutine, Optional, TypeVar, Union

T = TypeVar('T')

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

# Initialize cache
cache = DiscordCache()

# Retry decorator for API calls
def retry_api(max_retries=3, delay=1):
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
                    raise last_error
        return wrapper
    return decorator

# Centralized error response
async def send_error_response(interaction, error_type, message=None):
    error_messages = {
        'api': 'Discord API error occurred. Please try again later.',
        'permission': 'You do not have permission to perform this action.',
        'not_found': 'The requested resource was not found.',
        'rate_limit': 'Please slow down and try again later.',
        'default': 'An unexpected error occurred.'
    }
    
    msg = message or error_messages.get(error_type, error_messages['default'])
    await interaction.response.send_message(msg, ephemeral=True)

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN environment variable is not set")
BAD_BOT_ROLE_NAME = 'bad bots'
MODERATOR_ROLE_NAME = 'Moderators'
AUTOMATA_ROLE_NAME = 'automata'
DELAY_MINUTES = 4
LOG_FILE = os.path.join(os.path.dirname(__file__), 'johnnybot.log')
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB
MODERATORS_CHANNEL_NAME = 'moderators_only'
PROTECTED_CHANNELS = ['🫠・code_of_conduct', '🧚・hey_listen', '👯・local_events']
REMINDERS_FILE = os.path.join(os.path.dirname(__file__), 'reminders.json')

# Configure logging
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=2)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

reminders = {}  # Format: {channel_id: {title, message, interval, next_trigger}}
reminders_lock = threading.Lock()
reminder_check_task = None

# Load reminders from file
if os.path.exists(REMINDERS_FILE):
    try:
        with open(REMINDERS_FILE, 'r', encoding='utf-8') as f:
            reminders = json.load(f)
            # Initialize next_trigger if not present
            for reminder in reminders.values():
                if 'next_trigger' not in reminder:
                    reminder['next_trigger'] = time.time() + reminder['interval']
    except (OSError, IOError) as e:
        logger.error('Failed to read reminders file: %s', e)

def handle_errors(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, Optional[T]]]:
    """Decorator to handle common errors in slash commands."""
    @wraps(func)
    async def wrapper(interaction: discord.Interaction, *args: Any, **kwargs: Any) -> Optional[T]:
        try:
            return await func(interaction, *args, **kwargs)
        except app_commands.errors.MissingRole:
            await interaction.response.send_message(
                'You do not have the required role to use this command.',
                ephemeral=True
            )
        except discord.HTTPException as e:
            logger.error('Discord API error: %s', e)
            await interaction.response.send_message(
                'Discord API error occurred.',
                ephemeral=True
            )
        except (OSError, IOError) as e:
            logger.error('File access error: %s', e)
            await interaction.response.send_message(
                'File access error occurred.',
                ephemeral=True
            )
        except Exception as e:
            logger.error('Unexpected error: %s', e)
            await interaction.response.send_message(
                'An unexpected error occurred.',
                ephemeral=True
            )
        return None
    return wrapper

async def check_reminders() -> None:
    """Background task to check and send due reminders."""
    while True:
        now = time.time()
        with reminders_lock:
            for channel_id, reminder in list(reminders.items()):
                if now >= reminder['next_trigger']:
                    try:
                        channel = cache.get_channel(channel_id)
                        if channel:
                            await channel.send(f"**{reminder['title']}**\n{reminder['message']}")
                        # Schedule next trigger
                        reminder['next_trigger'] = now + reminder['interval']
                    except Exception as e:
                        logger.error(f"Failed to send reminder {reminder['title']}: {e}")
        
        # Save reminders to file
        with reminders_lock:
            with open(REMINDERS_FILE, 'w', encoding='utf-8') as f:
                json.dump(reminders, f)
        
        # Check every minute
        await asyncio.sleep(60)

async def start_reminder_checker() -> None:
    """Start the background reminder checker task."""
    global reminder_check_task
    if reminder_check_task is None or reminder_check_task.done():
        reminder_check_task = asyncio.create_task(check_reminders())

@bot.event
async def on_ready():
    try:
        await tree.sync()  # Global sync
        logger.info('Commands globally synced successfully')
    except (discord.HTTPException, discord.Forbidden) as e:
        logger.error('Failed to sync commands globally: %s', e)

    for guild in bot.guilds:
        try:
            await tree.sync(guild=guild)
            logger.info('Successfully synced commands to guild: %s', guild.id)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound) as e:
            logger.error('Failed to sync commands to guild %s: %s', guild.id, e)
    logger.info('All commands synced to joined guilds')
    logger.info('Logged in as %s (ID: %s)', bot.user, bot.user.id)
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    
    # Start reminder checker
    await start_reminder_checker()

@bot.event
async def on_disconnect():
    """Clean up resources when bot disconnects."""
    if reminder_check_task and not reminder_check_task.done():
        reminder_check_task.cancel()

# Removed send_reminder function as it's no longer needed

class InvalidReminderInterval(Exception):
    """Exception raised when an invalid reminder interval is provided."""
    pass

def validate_reminder_interval(interval: int) -> None:
    """Validate that reminder interval is reasonable."""
    if interval < 60:
        raise InvalidReminderInterval("Interval must be at least 60 seconds")
    # No upper limit since we now handle long intervals via persistent storage

@tree.command(name='set_reminder', description='Sets a reminder message to be sent to a channel at regular intervals')
@app_commands.describe(channel='Channel to send the reminder to', title='Title of the reminder', message='Reminder message', interval='Interval in seconds')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def set_reminder(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, interval: int):
    """Sets a reminder message to be sent to a channel at regular intervals."""
    @retry_api()
    async def _set_reminder():
        validate_reminder_interval(interval)
        with reminders_lock:
            reminders[channel.id] = {
                'channel_id': channel.id,
                'title': title,
                'message': message,
                'interval': interval,
                'next_trigger': time.time() + interval
            }
            with open(REMINDERS_FILE, 'w', encoding='utf-8') as reminder_file:
                json.dump(reminders, reminder_file)
        
        # Ensure reminder checker is running
        await start_reminder_checker()
        
        await interaction.response.send_message(f'Reminder set in {channel.mention} every {interval} seconds.', ephemeral=True)

    try:
        await _set_reminder()
    except InvalidReminderInterval as e:
        await send_error_response(interaction, 'validation', f'Invalid interval: {e}')
    except discord.HTTPException:
        await send_error_response(interaction, 'api')
    except (OSError, IOError):
        await send_error_response(interaction, 'io', 'Failed to set reminder due to file access error.')
    except Exception as e:
        logger.error('An error occurred: %s', e)
        await send_error_response(interaction, 'default')

@set_reminder.error
async def set_reminder_error(interaction: discord.Interaction, error):
    """Handles errors for the set_reminder command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

@tree.command(name='list_reminders', description='Lists all current reminders')
async def list_reminders(interaction: discord.Interaction):
    """Lists all current reminders."""
    try:
        if not reminders:
            await interaction.response.send_message('There are no reminders set.', ephemeral=True)
            return

        reminder_list = '\n'.join([f"**{reminder['title']}**: {reminder['message']} (every {reminder['interval']} seconds)" for reminder in reminders.values()])
        await interaction.response.send_message(f'Current reminders:\n{reminder_list}', ephemeral=True)
    except Exception as e:
        logger.error('An error occurred: %s', e)
        await interaction.response.send_message('An unexpected error occurred.', ephemeral=True)

@tree.command(name='delete_all_reminders', description='Deletes all active reminders')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
@handle_errors
async def delete_all_reminders(interaction: discord.Interaction) -> None:
    """Delete all active reminders."""
    with reminders_lock:
        reminders.clear()
        with open(REMINDERS_FILE, 'w', encoding='utf-8') as reminder_file:
            json.dump(reminders, reminder_file)
    
    await interaction.response.send_message('All reminders have been deleted.', ephemeral=True)

@tree.command(name='delete_reminder', description='Deletes a reminder by title')
@app_commands.describe(title='Title of the reminder to delete')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
@handle_errors
async def delete_reminder(interaction: discord.Interaction, title: str) -> None:
    try:
        with reminders_lock:
            for channel_id, reminder in list(reminders.items()):  # Use list() to avoid runtime dictionary modification
                if reminder['title'] == title:
                    # Remove the reminder from the dictionary
                    del reminders[channel_id]
                    
                    # Save the updated reminders to the file
                    with open(REMINDERS_FILE, 'w', encoding='utf-8') as reminder_file:
                        json.dump(reminders, reminder_file)
                    
                    await interaction.response.send_message(f'Reminder titled "{title}" has been deleted.', ephemeral=True)
                    return

        # If no reminder with the given title is found
        await interaction.response.send_message(f'No reminder found with the title "{title}".', ephemeral=True)
    except (OSError, IOError) as e:
        logger.error('Failed to write reminders file: %s', e)
        await interaction.response.send_message('Failed to delete reminder due to file access error.', ephemeral=True)
    except Exception as e:
        logger.error('An error occurred: %s', e)
        await interaction.response.send_message('An unexpected error occurred.', ephemeral=True)

# Removed list_reminder_threads command as it's no longer needed

# Purge channel messages
@tree.command(name='purge_last_messages', description='Purges a specified number of messages from a channel')
@app_commands.describe(channel='Channel to purge messages from', limit='Number of messages to delete')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def purge_last_messages(interaction: discord.Interaction, channel: discord.TextChannel, limit: int):
    """Purges a specified number of messages from a channel."""
    @retry_api()
    async def _purge_messages():
        deleted = await channel.purge(limit=limit)
        await interaction.response.send_message(f'Deleted {len(deleted)} message(s)', ephemeral=True)

    try:
        await _purge_messages()
    except discord.Forbidden:
        await send_error_response(interaction, 'permission')
    except discord.HTTPException:
        await send_error_response(interaction, 'api')
    except Exception as e:
        logger.error('An error occurred: %s', e)
        await send_error_response(interaction, 'default')

@purge_last_messages.error
async def purge_last_messages_error(interaction: discord.Interaction, error):
    """Handles errors for the purge_last_messages command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

@tree.command(name='purge_string', description='Purges all messages containing a specific string from a channel')
@app_commands.describe(channel='Channel to purge messages from', search_string='String to search for in messages')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def purge_string(interaction: discord.Interaction, channel: discord.TextChannel, search_string: str):
    """Purges all messages containing a specific string from a channel."""
    try:
        def check_message(message):
            return search_string in message.content

        deleted = await channel.purge(check=check_message)
        await interaction.response.send_message(f'Deleted {len(deleted)} message(s) containing "{search_string}".', ephemeral=True)
    except Exception as e:
        logger.error('An error occurred: %s', e)
        await interaction.response.send_message('An unexpected error occurred.', ephemeral=True)

@purge_string.error
async def purge_string_error(interaction: discord.Interaction, error):
    """Handles errors for the purge_string command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

@tree.command(name='purge_webhooks', description='Purges all messages sent by webhooks or apps from a channel')
@app_commands.describe(channel='Channel to purge messages from')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def purge_webhooks(interaction: discord.Interaction, channel: discord.TextChannel):
    """Purges all messages sent by webhooks or apps from a channel."""
    try:
        def check_message(message):
            return message.webhook_id is not None or message.author.bot

        deleted = await channel.purge(check=check_message)
        await interaction.response.send_message(f'Deleted {len(deleted)} message(s) sent by webhooks or apps.', ephemeral=True)
    except Exception as e:
        logger.error('An error occurred: %s', e)
        await interaction.response.send_message('An unexpected error occurred.', ephemeral=True)

@purge_webhooks.error
async def purge_webhooks_error(interaction: discord.Interaction, error):
    """Handles errors for the purge_webhooks command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

# Kick a member
@tree.command(name='kick', description='Kicks a member from the server')
@app_commands.describe(member='Member to kick', reason='Reason for kick')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f'{member.mention} has been kicked. Reason: {reason}', ephemeral=True)
    except Exception as e:
        logger.error('An error occurred: %s', e)
        await interaction.response.send_message('An unexpected error occurred.', ephemeral=True)

@kick.error
async def kick_error(interaction: discord.Interaction, error):
    """Handles errors for the kick command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

# Make the bot say something in chat
@tree.command(name='botsay', description='Makes the bot send a message to a specified channel')
@app_commands.describe(channel='Channel to send the message to', message='Message to send')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def botsay(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    try:
        await channel.send(message)
        await interaction.response.send_message(f'Message sent to {channel.mention}', ephemeral=True)
    except Exception as e:
        logger.error('An error occurred: %s', e)
        await interaction.response.send_message('An unexpected error occurred.', ephemeral=True)

@botsay.error
async def botsay_error(interaction: discord.Interaction, error):
    """Handles errors for the botsay command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

# Put a member in time out
@tree.command(name='timeout', description='Timeouts a member for a specified duration')
@app_commands.describe(member='Member to timeout', duration='Timeout duration in seconds', reason='Reason for timeout')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def timeout(interaction: discord.Interaction, member: discord.Member, duration: int, reason: str = None):
    try:
        until = discord.utils.utcnow() + timedelta(seconds=duration)
        await member.timeout(until, reason=reason)
        await interaction.response.send_message(f'{member.mention} has been timed out for {duration} seconds.', ephemeral=True)
    except Exception as e:
        logger.error('An error occurred: %s', e)
        await interaction.response.send_message('An unexpected error occurred.', ephemeral=True)

@timeout.error
async def timeout_error(interaction: discord.Interaction, error):
    """Handles errors for the timeout command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

@tree.command(name='log_tail', description='DM the last specified number of lines of the bot log to the user')
@app_commands.describe(lines='Number of lines to retrieve from the log')
async def log_tail(interaction: discord.Interaction, lines: int):
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
    except Exception as e:
        logger.error('An error occurred: %s', e)
        await interaction.response.send_message('An unexpected error occurred.', ephemeral=True)

@log_tail.error
async def log_tail_error(interaction: discord.Interaction, error):
    await interaction.response.send_message(f"Error: {str(error)}", ephemeral=True)

@tree.command(name='add_event_feed_url', description='Adds a calendar feed URL to check for events')
async def add_event_feed_url(interaction: discord.Interaction, calendar_url: str):
    """Adds a calendar feed to check for new events"""
    try:
        # Validate URL
        if not calendar_url.startswith(('http://', 'https://')):
            await interaction.response.send_message("Invalid URL format", ephemeral=True)
            return
            
        # Add feed to tracking
        if interaction.guild.id not in event_feed.feeds:
            event_feed.feeds[interaction.guild.id] = {}
            
        event_feed.feeds[interaction.guild.id][calendar_url] = None
        
        # Do initial check
        await event_feed.check_feeds()
        
        await interaction.response.send_message(
            f"Added calendar feed! I'll check for new events hourly and post in #bot-trap",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"Error adding feed: {str(e)}",
            ephemeral=True
        )

@add_event_feed_url.error
async def add_event_feed_url_error(interaction: discord.Interaction, error):
    await interaction.response.send_message(f"Error: {str(error)}", ephemeral=True)

@tree.command(name='add_event_feed', description='Adds a calendar feed to check for events')
async def add_event_feed(interaction: discord.Interaction, calendar_url: str):
    """Adds a calendar feed to check for new events"""
    try:
        # Validate URL
        if not calendar_url.startswith(('http://', 'https://')):
            await interaction.response.send_message("Invalid URL format", ephemeral=True)
            return
            
        # Add feed to tracking
        if interaction.guild.id not in event_feed.feeds:
            event_feed.feeds[interaction.guild.id] = {}
            
        event_feed.feeds[interaction.guild.id][calendar_url] = None
        
        # Do initial check
        await event_feed.check_feeds()
        
        await interaction.response.send_message(
            f"Added calendar feed! I'll check for new events hourly and post in #bot-trap",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"Error adding feed: {str(e)}",
            ephemeral=True
        )

@add_event_feed.error
async def add_event_feed_error(interaction: discord.Interaction, error):
    """Handles errors for the log_tail command."""
    if isinstance(error, app_commands.errors.MissingRole):
        await interaction.response.send_message('You do not have the required role to use this command.', ephemeral=True)
    else:
        await interaction.response.send_message(f'Error: {error}', ephemeral=True)

def validate_input(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
    """Decorator to validate command input."""
    @wraps(func)
    async def wrapper(interaction: discord.Interaction, *args: Any, **kwargs: Any) -> T:
        # Basic input sanitization
        for arg in args:
            if isinstance(arg, str) and len(arg) > 2000:
                raise ValueError("Input too long (max 2000 characters)")
        return await func(interaction, *args, **kwargs)
    return wrapper

class EventFeed:
    def __init__(self, bot):
        self.bot = bot
        self.feeds = {}  # {guild_id: {feed_url: last_checked}}
        self.scheduler = AsyncIOScheduler()
        self.scheduler.start()
        
    async def check_feeds(self):
        """Check all registered calendar feeds for new events"""
        for guild_id, feeds in self.feeds.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
                
            for feed_url, last_checked in feeds.items():
                try:
                    # Fetch and parse calendar
                    response = requests.get(feed_url)
                    calendar = Calendar.from_ical(response.text)
                    
                    # Process events in next 30 days
                    now = datetime.utcnow()
                    end_date = now + timedelta(days=30)
                    
                    for component in calendar.walk():
                        if component.name == "VEVENT":
                            start = component.get('dtstart').dt
                            if isinstance(start, datetime) and now <= start <= end_date:
                                # Check if event is new or updated
                                if last_checked is None or start > last_checked:
                                    await self.create_discord_event(guild, component)
                                    
                    # Update last checked time
                    self.feeds[guild_id][feed_url] = now
                    
                except Exception as e:
                    print(f"Error processing feed {feed_url}: {e}")
    
    async def create_discord_event(self, guild, ical_event):
        """Create Discord event from iCal event"""
        try:
            # Get bot-trap channel
            channel = discord.utils.get(guild.text_channels, name="bot-trap")
            if not channel:
                return
                
            # Create Discord event
            event = await guild.create_scheduled_event(
                name=ical_event.get('summary'),
                description=ical_event.get('description'),
                start_time=ical_event.get('dtstart').dt,
                end_time=ical_event.get('dtend').dt,
                location=ical_event.get('location')
            )
            
            # Post initial notification
            await channel.send(
                f"Upcoming event! {event.url}\n"
                f"**{event.name}**\n"
                f"{event.description or ''}"
            )
            
            # Schedule reminders
            await self.schedule_reminders(event)
            
        except Exception as e:
            print(f"Error creating event: {e}")
    
    async def schedule_reminders(self, event):
        """Schedule 24h and 10h reminders for an event"""
        # 24h reminder
        remind_time = event.start_time - timedelta(hours=24)
        if remind_time > datetime.utcnow():
            self.scheduler.add_job(
                self.send_reminder,
                trigger='date',
                run_date=remind_time,
                args=[event, "Starts in 24 hours!"]
            )
        
        # 10h reminder
        remind_time = event.start_time - timedelta(hours=10)
        if remind_time > datetime.utcnow():
            self.scheduler.add_job(
                self.send_reminder,
                trigger='date',
                run_date=remind_time,
                args=[event, "Starts in 10 hours!"]
            )
    
    async def send_reminder(self, event, message):
        """Send reminder message to bot-trap channel"""
        try:
            channel = discord.utils.get(event.guild.text_channels, name="bot-trap")
            if channel:
                await channel.send(f"{message} {event.url}")
        except Exception as e:
            print(f"Error sending reminder: {e}")

# Initialize event feed system
event_feed = EventFeed(bot)

# Schedule hourly feed checks
@bot.event
async def on_ready():
    event_feed.scheduler.add_job(
        event_feed.check_feeds,
        trigger=IntervalTrigger(hours=1),
        next_run_time=datetime.now()
    )

try:
    bot.run(TOKEN)
except KeyboardInterrupt:
    logger.info("Shutting down gracefully...")
    # Cleanup handled by background task cancellation
except Exception as e:
    logger.error("Fatal error: %s", e)
    raise
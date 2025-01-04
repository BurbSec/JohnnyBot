import logging
import time
import threading
import os
import json
from datetime import timedelta
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
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
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

reminders = {}
reminder_threads = {}

# Load reminders from file
if os.path.exists(REMINDERS_FILE):
    try:
        with open(REMINDERS_FILE, 'r', encoding='utf-8') as f:
            reminders = json.load(f)
    except (OSError, IOError) as e:
        logger.error('Failed to read reminders file: %s', e)

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

    # Restart reminder loops for existing reminders
    for reminder in reminders.values():
        stop_event = threading.Event()
        reminder_thread = threading.Thread(target=send_reminder, args=(reminder['channel_id'], reminder['title'], reminder['message'], reminder['interval'], stop_event))
        reminder_thread.daemon = True
        reminder_thread.start()
        reminder_threads[reminder['channel_id']] = stop_event

def send_reminder(channel_id, title, message, interval, stop_event):
    """Sends a reminder message to a channel at regular intervals."""
    channel = bot.get_channel(channel_id)
    if channel:
        bot.loop.create_task(channel.send(f'**{title}**\n{message}'))
    while not stop_event.is_set():
        stop_event.wait(interval)
        if stop_event.is_set():
            break
        if channel:
            bot.loop.create_task(channel.send(f'**{title}**\n{message}'))

@tree.command(name='set_reminder', description='Sets a reminder message to be sent to a channel at regular intervals')
@app_commands.describe(channel='Channel to send the reminder to', title='Title of the reminder', message='Reminder message', interval='Interval in seconds')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def set_reminder(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, interval: int):
    """Sets a reminder message to be sent to a channel at regular intervals."""
    reminders[channel.id] = {
        'channel_id': channel.id,
        'title': title,
        'message': message,
        'interval': interval
    }
    try:
        with open(REMINDERS_FILE, 'w', encoding='utf-8') as reminder_file:
            json.dump(reminders, reminder_file)
    except (OSError, IOError) as e:
        logger.error('Failed to write reminders file: %s', e)
        await interaction.response.send_message('Failed to set reminder due to file access error.', ephemeral=True)
        return
    
    # Start the reminder loop using threading.Timer
    stop_event = threading.Event()
    reminder_thread = threading.Thread(target=send_reminder, args=(channel.id, title, message, interval, stop_event))
    reminder_thread.daemon = True
    reminder_thread.start()
    reminder_threads[channel.id] = stop_event
    
    await interaction.response.send_message(f'Reminder set in {channel.mention} every {interval} seconds.', ephemeral=True)

@set_reminder.error
async def set_reminder_error(interaction: discord.Interaction, error):
    """Handles errors for the set_reminder command."""
    await interaction.response.send_message(f'Error: {error}', ephemeral=True)

@tree.command(name='list_reminders', description='Lists all current reminders')
async def list_reminders(interaction: discord.Interaction):
    """Lists all current reminders."""
    if not reminders:
        await interaction.response.send_message('There are no reminders set.', ephemeral=True)
        return

    reminder_list = '\n'.join([f"**{reminder['title']}**: {reminder['message']} (every {reminder['interval']} seconds)" for reminder in reminders.values()])
    await interaction.response.send_message(f'Current reminders:\n{reminder_list}', ephemeral=True)

@tree.command(name='delete_reminder', description='Deletes a reminder by title')
@app_commands.describe(title='Title of the reminder to delete')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def delete_reminder(interaction: discord.Interaction, title: str):
    for channel_id, reminder in reminders.items():
        if reminder['title'] == title:
            del reminders[channel_id]
            if channel_id in reminder_threads:
                reminder_threads[channel_id].set()  # Stop the reminder thread
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

# Purge channel messages
@tree.command(name='purge', description='Purges a specified number of messages from a channel')
@app_commands.describe(channel='Channel to purge messages from', limit='Number of messages to delete')
@app_commands.describe(limit='Number of messages to delete')
async def purge(interaction: discord.Interaction, channel: discord.TextChannel, limit: int):
    deleted = await channel.purge(limit=limit)
    await interaction.response.send_message(f'Deleted {len(deleted)} message(s)', ephemeral=True)

# Kick a member
@tree.command(name='kick', description='Kicks a member from the server')
@app_commands.describe(member='Member to kick', reason='Reason for kick')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    await member.kick(reason=reason)
    await interaction.response.send_message(f'{member.mention} has been kicked. Reason: {reason}', ephemeral=True)

# Make the bot say something in chat
@tree.command(name='botsay', description='Makes the bot send a message to a specified channel')
@app_commands.describe(channel='Channel to send the message to', message='Message to send')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def botsay(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    await channel.send(message)
    await interaction.response.send_message(f'Message sent to {channel.mention}', ephemeral=True)

# Put a member in time out
@tree.command(name='timeout', description='Timeouts a member for a specified duration')
@app_commands.describe(member='Member to timeout', duration='Timeout duration in seconds', reason='Reason for timeout')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def timeout(interaction: discord.Interaction, member: discord.Member, duration: int, reason: str = None):
    until = discord.utils.utcnow() + timedelta(seconds=duration)
    await member.timeout(until, reason=reason)
    await interaction.response.send_message(f'{member.mention} has been timed out for {duration} seconds.', ephemeral=True)

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

bot.run(TOKEN)
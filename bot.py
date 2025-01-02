import logging
import os
import json
from datetime import timedelta
from logging.handlers import RotatingFileHandler

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
BAD_BOT_ROLE_NAME = 'bad bots'
MODERATOR_ROLE_NAME = 'Moderators'
AUTOMATA_ROLE_NAME = 'automata'
DELAY_MINUTES = 4
LOG_FILE = os.path.join(os.path.dirname(__file__), 'johnnybot.log')
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB
MODERATORS_CHANNEL_NAME = 'moderators_only'
PROTECTED_CHANNELS = ['🫠・code_of_conduct', '🧚・hey_listen', '👯・local_events']
REMINDERS_FILE = 'reminders.json'

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

reminders = {}

# Load reminders from file
if os.path.exists(REMINDERS_FILE):
    with open(REMINDERS_FILE, 'r', encoding='utf-8') as f:
        reminders = json.load(f)

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
    reminder_loop.start()
    logger.info('Logged in as %s (ID: %s)', bot.user, bot.user.id)
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')

@tasks.loop(seconds=60)
async def reminder_loop():
    for reminder in reminders.values():
        channel = bot.get_channel(reminder['channel_id'])
        if channel:
            await channel.send(f'**{reminder['title']}**\n{reminder['message']}')

@tree.command(name='set_reminder', description='Sets a reminder message to be sent to a channel at regular intervals')
@app_commands.describe(channel='Channel to send the reminder to', title='Title of the reminder', message='Reminder message', interval='Interval in seconds')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def set_reminder(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, interval: int):
    reminders[channel.id] = {
        'channel_id': channel.id,
        'title': title,
        'message': message,
        'interval': interval
    }
    with open(REMINDERS_FILE, 'w', encoding='utf-8') as reminder_file:
        json.dump(reminders, reminder_file)
    await interaction.response.send_message(f'Reminder set in {channel.mention} every {interval} seconds.', ephemeral=True)

@tree.command(name='purge', description='Purges a specified number of messages from a channel')
@app_commands.describe(channel='Channel to purge messages from', limit='Number of messages to delete')
@app_commands.describe(limit='Number of messages to delete')
async def purge(interaction: discord.Interaction, channel: discord.TextChannel, limit: int):
    deleted = await channel.purge(limit=limit)
    await interaction.response.send_message(f'Deleted {len(deleted)} message(s)', ephemeral=True)

@tree.command(name='mute', description='Mutes a member by adding a specific role')
@app_commands.describe(member='Member to mute', reason='Reason for mute')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def mute(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    automata_role = discord.utils.get(interaction.guild.roles, name=AUTOMATA_ROLE_NAME)
    if automata_role:
        await member.add_roles(automata_role, reason=reason)
        await interaction.response.send_message(f'{member.mention} has been muted.', ephemeral=True)
    else:
        await interaction.response.send_message('Mute role not found.', ephemeral=True)

@tree.command(name='kick', description='Kicks a member from the server')
@app_commands.describe(member='Member to kick', reason='Reason for kick')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    await member.kick(reason=reason)
    await interaction.response.send_message(f'{member.mention} has been kicked. Reason: {reason}', ephemeral=True)

@tree.command(name='botsay', description='Makes the bot send a message to a specified channel')
@app_commands.describe(channel='Channel to send the message to', message='Message to send')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def botsay(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    await channel.send(message)
    await interaction.response.send_message(f'Message sent to {channel.mention}', ephemeral=True)

@tree.command(name='timeout', description='Timeouts a member for a specified duration')
@app_commands.describe(member='Member to timeout', duration='Timeout duration in seconds', reason='Reason for timeout')
@app_commands.checks.has_role(MODERATOR_ROLE_NAME)
async def timeout(interaction: discord.Interaction, member: discord.Member, duration: int, reason: str = None):
    until = discord.utils.utcnow() + timedelta(seconds=duration)
    await member.timeout(until, reason=reason)
    await interaction.response.send_message(f'{member.mention} has been timed out for {duration} seconds.', ephemeral=True)

@set_reminder.error
async def set_reminder_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingRole):
        await interaction.response.send_message('You do not have permission to set reminders.', ephemeral=True)



bot.run(TOKEN)

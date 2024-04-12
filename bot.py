#!/usr/bin/env python
import os
import logging
from logging.handlers import RotatingFileHandler
import discord
from discord.ext import commands

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
BAD_BOT_ROLE_NAME = 'bad bots'
MODERATOR_ROLE_NAME = 'Moderators'
DELAY_MINUTES = 4
LOG_FILE = 'johnnybot.log'
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB
MODERATORS_CHANNEL_NAME = 'moderators_only'  # Name of the moderators channel
PROTECTED_CHANNELS = ['🫠・code_of_conduct', '🧚・hey_listen', '👯・local_events',
                      '🧩・ctf_announcements', '🖥・virtual_events'] #Users can't post here

if not TOKEN:
    print('DISCORD_BOT_TOKEN environment variable not set. Exiting...')
    exit(1)

intents = discord.Intents.default()
intents.members = True
intents.messages = True
intents.message_content = True

# Set up logging
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)

script_dir = os.path.dirname(os.path.abspath(__file__))
log_file_path = os.path.join(script_dir, LOG_FILE)

handler = RotatingFileHandler(log_file_path, maxBytes=LOG_MAX_SIZE, backupCount=5)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

bot = commands.Bot(command_prefix='!', intents=intents)

async def get_roles_and_channel(guild):
    bad_bots_role = discord.utils.get(guild.roles, name=BAD_BOT_ROLE_NAME)
    moderator_role = discord.utils.get(guild.roles, name=MODERATOR_ROLE_NAME)
    moderators_channel = discord.utils.get(guild.text_channels, name=MODERATORS_CHANNEL_NAME)
    return bad_bots_role, moderator_role, moderators_channel

async def log_and_send_message(guild, message, *args, level='info'):
    if level == 'info':
        logger.info(message, *args)
    elif level == 'error':
        logger.error(message, *args)
    elif level == 'debug':
        logger.debug(message, *args)
    if moderators_channel := discord.utils.get(guild.text_channels, name=MODERATORS_CHANNEL_NAME):
        await moderators_channel.send(message % args)

async def kick_and_delete_messages(member):
    guild = member.guild
    delete_messages = [msg async for msg in member.history(limit=None)]
    try:
        await member.kick(reason=f'No role assigned after {DELAY_MINUTES} minutes')
        await log_and_send_message(guild, 'Kicked %s from %s', member.name, guild.name)
    except discord.errors.HTTPException as e:
        error_response = e.response
        await log_and_send_message(guild, 'Error kicking %s from %s: %s (Status code: %d)',
                                   member.name, guild.name, error_response.text,
                                   error_response.status, level='error')
    if delete_messages:
        for channel in guild.text_channels:
            delete_messages_channel = [msg for msg in delete_messages if msg.channel == channel]
            if delete_messages_channel:
                try:
                    await channel.delete_messages(delete_messages_channel)
                    logger.info('Deleted %d messages from %s for %s',
                                len(delete_messages_channel), channel.name, member.name)
                except discord.errors.HTTPException as e:
                    error_response = e.response
                    logger.error('Error deleting messages in %s: %s (Status code: %d)',
                                 channel.name, error_response.text, error_response.status)

@bot.event
async def on_member_join(member):
    guild = member.guild
    bad_bots_role, _, _ = await get_roles_and_channel(guild)
    if bad_bots_role:
        await member.add_roles(bad_bots_role, reason='New member joined')
        await asyncio.sleep(DELAY_MINUTES * 60)
        if member.roles == [guild.default_role, bad_bots_role]:
            await kick_and_delete_messages(member)

@bot.event
async def on_member_update(before, after):
    guild = after.guild
    bad_bots_role, _, _ = await get_roles_and_channel(guild)
    if bad_bots_role in after.roles and len(after.roles) > 2:
        await after.remove_roles(bad_bots_role, reason='User has additional roles')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.name in PROTECTED_CHANNELS:
        _, moderator_role, _ = await get_roles_and_channel(message.guild)
        if moderator_role not in message.author.roles:
            await message.delete()
            logger.info('Deleted message from %s in protected channel %s: %s',
                        message.author.name, message.channel.name, message.content)

@bot.event
async def on_ready():
    logger.info('Logged in as %s (ID: %s)', bot.user.name, bot.user.id)

    # Register the slash command
    try:
        await bot.tree.sync()
        logger.info('Slash command registered successfully')
    except discord.errors.HTTPException as e:
        error_response = e.response
        logger.error('Failed to register slash command: %s (Status code: %d)',
                     error_response.text, error_response.status)

@bot.tree.command(name='botsay', description='Make the bot say something in a channel')
@commands.has_role(MODERATOR_ROLE_NAME)
async def botsay(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    try:
        await channel.send(message)
        await interaction.response.send_message(f'Message sent to {channel.mention}', ephemeral=True)
    except discord.errors.HTTPException as e:
        error_response = e.response
        await interaction.response.send_message(
            f'Error sending message: {error_response.text} (Status code: {error_response.status})',
            ephemeral=True
        )
        logger.error('Error sending message: %s (Status code: %d)',
                     error_response.text, error_response.status)

@botsay.error
async def botsay_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingRole):
        await interaction.response.send_message(
            f'You need the {MODERATOR_ROLE_NAME} role to use this command.', ephemeral=True)
    else:
        logger.error('Error occurred: %s', str(error))
        await interaction.response.send_message(
            'An error occurred while processing the command.', ephemeral=True)

if __name__ == '__main__':
    bot.run(TOKEN)
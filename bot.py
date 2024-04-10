#!/usr/bin/env python

import os
import logging
from logging.handlers import RotatingFileHandler
import discord
from discord.ext import commands, tasks

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
BAD_BOT_ROLE_NAME = 'bad bots'
MODERATOR_ROLE_NAME = 'Moderators'
DELAY_MINUTES = 1
LOG_FILE = 'johnnybot.log'
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB
MODERATORS_CHANNEL_NAME = 'moderators_only'  # Name of the moderators channel

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

async def process_member(member, bad_bots_role, moderator_role):
    if bad_bots_role in member.roles and moderator_role not in member.roles:
        if len(member.roles) > 2:
            await member.remove_roles(bad_bots_role, reason='User has been assigned additional roles')
            await log_and_send_message(member.guild, 'Removed %s role from %s in %s',
                                       BAD_BOT_ROLE_NAME, member.name, member.guild.name)
            return True
        elif len(member.roles) == 1:
            joined_at = member.joined_at
            delay = DELAY_MINUTES * 60
            if (discord.utils.utcnow() - joined_at).total_seconds() > delay:
                await member.add_roles(bad_bots_role,
                                       reason=f'No role assigned after {DELAY_MINUTES} minutes')
                await log_and_send_message(member.guild, 'Assigned %s role to %s in %s',
                                           BAD_BOT_ROLE_NAME, member.name, member.guild.name)
                return True
    return False

async def ban_and_delete_messages(message):
    for guild in bot.guilds:
        if message.author in guild.members:
            delete_messages = [msg async for msg in message.author.history(limit=None)]
            try:
                await message.author.ban(reason='Banned for DM spam (DMing JohnnyBot)')
                await log_and_send_message(guild, 'Banned %s from %s and deleted all messages',
                                           message.author.name, guild.name)
            except discord.errors.HTTPException as e:
                error_response = e.response
                await log_and_send_message(guild,
                                           'Error banning %s from %s: %s (Status code: %d)',
                                           message.author.name, guild.name, error_response.text,
                                           error_response.status, level='error')
            if delete_messages:
                for channel in guild.text_channels:
                    delete_messages_channel = [msg for msg in delete_messages
                                               if msg.channel == channel]
                    if delete_messages_channel:
                        try:
                            await channel.delete_messages(delete_messages_channel)
                            logger.info('Deleted %d messages from %s for %s',
                                        len(delete_messages_channel), channel.name,
                                        message.author.name)
                        except discord.errors.HTTPException as e:
                            error_response = e.response
                            logger.error('Error deleting messages in %s: %s (Status code: %d)',
                                         channel.name, error_response.text, error_response.status)
            break

@bot.event
async def on_ready():
    logger.info('Logged in as %s (ID: %s)', bot.user.name, bot.user.id)
    update_bad_bots.start()

@tasks.loop(minutes=1)
async def update_bad_bots():
    roles_modified = False
    try:
        for guild in bot.guilds:
            bad_bots_role, moderator_role, moderators_channel = await get_roles_and_channel(guild)
            if bad_bots_role and moderator_role and moderators_channel:
                for member in guild.members:
                    if not member.bot:
                        roles_modified |= await process_member(member, bad_bots_role, moderator_role)
    except discord.errors.HTTPException as e:
        error_response = e.response
        logger.error('Unable to complete task "update_bad_bots": %s (Status code: %d)',
                     error_response.text, error_response.status)
    else:
        if not roles_modified:
            logger.debug('Task "update_bad_bots" completed without modifying roles')

@update_bad_bots.before_loop
async def before_update_bad_bots():
    await bot.wait_until_ready()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    bad_bots_role = discord.utils.get(message.author.guild.roles, name=BAD_BOT_ROLE_NAME)
    if ((bad_bots_role in message.author.roles or len(message.author.roles) == 1)
            and isinstance(message.channel, discord.DMChannel)):
        await ban_and_delete_messages(message)
    elif bad_bots_role in message.author.roles:
        await message.delete()
        logger.info('Deleted message from %s in %s: %s', message.author.name,
                    message.guild.name, message.content)

@bot.tree.command(name='post', description='Post a message in a channel')
@commands.has_role(MODERATOR_ROLE_NAME)
async def post_message(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    await channel.send(message)
    await interaction.response.send_message(f'Message sent to {channel.mention}', ephemeral=True)

@post_message.error
async def post_message_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingRole):
        await interaction.response.send_message(
            f'You need the {MODERATOR_ROLE_NAME} role to use this command.', ephemeral=True)
    else:
        logger.error('Error occurred: %s', str(error))

if __name__ == '__main__':
    bot.run(TOKEN)

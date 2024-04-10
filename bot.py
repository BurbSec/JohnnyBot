#!/usr/bin/env python

import os
import logging
from logging.handlers import RotatingFileHandler
import discord
from discord.ext import commands, tasks

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
BAD_BOT_ROLE_NAME = 'bad bots'
MODERATOR_ROLE_NAME = 'moderators'
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

@bot.event
async def on_ready():
    logger.info('Logged in as %s (ID: %s)', bot.user.name, bot.user.id)
    update_bad_bots.start()

@tasks.loop(minutes=1)
async def update_bad_bots():
    roles_modified = False
    try:
        for guild in bot.guilds:
            bad_bots_role = discord.utils.get(guild.roles, name=BAD_BOT_ROLE_NAME)
            moderators_channel = discord.utils.get(guild.text_channels, name=MODERATORS_CHANNEL_NAME)
            if bad_bots_role and moderators_channel:
                for member in guild.members:
                    if not member.bot and bad_bots_role in member.roles:
                        if len(member.roles) > 2:
                            await member.remove_roles(bad_bots_role, reason='User has been assigned additional roles')
                            log_message = 'Removed %s role from %s in %s' % (BAD_BOT_ROLE_NAME, member.name, guild.name)
                            logger.info(log_message)
                            await moderators_channel.send(log_message)
                            roles_modified = True
                        elif len(member.roles) == 1:
                            joined_at = member.joined_at
                            delay = DELAY_MINUTES * 60
                            if (discord.utils.utcnow() - joined_at).total_seconds() > delay:
                                await member.add_roles(bad_bots_role, reason=f'No role assigned after {DELAY_MINUTES} minutes')
                                log_message = 'Assigned %s role to %s in %s' % (BAD_BOT_ROLE_NAME, member.name, guild.name)
                                logger.info(log_message)
                                await moderators_channel.send(log_message)
                                roles_modified = True
    except Exception as e:
        log_message = 'Unable to complete task "update_bad_bots": %s' % str(e)
        logger.error(log_message)
        if moderators_channel:
            await moderators_channel.send(log_message)
    else:
        if not roles_modified:
            log_message = 'Task "update_bad_bots" completed without modifying roles'
            logger.debug(log_message)
            if moderators_channel:
                await moderators_channel.send(log_message)

@update_bad_bots.before_loop
async def before_update_bad_bots():
    await bot.wait_until_ready()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    bad_bots_role = discord.utils.get(message.author.guild.roles, name=BAD_BOT_ROLE_NAME)
    if bad_bots_role in message.author.roles:
        if len(message.author.roles) <= 2 and isinstance(message.channel, discord.DMChannel):
            for guild in bot.guilds:
                if message.author in guild.members:
                    delete_messages = [msg async for msg in message.author.history(limit=None)]
                    await message.author.ban(reason='Banned for DM spam (DMing JohnnyBot)')  # Updated ban reason
                    log_message = 'Banned %s from %s and deleted all messages' % (message.author.name, guild.name)
                    logger.info(log_message)
                    if moderators_channel := discord.utils.get(guild.text_channels, name=MODERATORS_CHANNEL_NAME):
                        await moderators_channel.send(log_message)
                    if delete_messages:
                        for channel in guild.text_channels:
                            delete_messages_channel = [msg for msg in delete_messages if msg.channel == channel]
                            if delete_messages_channel:
                                await channel.delete_messages(delete_messages_channel)
                                logger.info(f'Deleted {len(delete_messages_channel)} messages from {channel.name} for {message.author.name}')
                    break
        else:
            await message.delete()
            logger.info('Deleted message from %s in %s: %s', message.author.name, message.guild.name, message.content)

@bot.tree.command(name='post', description='Post a message in a channel')
@commands.has_role(MODERATOR_ROLE_NAME)
async def post_message(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    await channel.send(message)
    await interaction.response.send_message(f'Message sent to {channel.mention}', ephemeral=True)

@post_message.error
async def post_message_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingRole):
        await interaction.response.send_message(f'You need the {MODERATOR_ROLE_NAME} role to use this command.', ephemeral=True)
    else:
        logger.error('Error occurred: %s', str(error))

if __name__ == '__main__':
    bot.run(TOKEN)
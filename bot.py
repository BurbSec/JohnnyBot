#!/usr/bin/env python

import os
import logging
from logging.handlers import RotatingFileHandler
import discord
from discord.ext import commands, tasks
TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
BAD_BOT_ROLE_NAME = 'bad bots'  # Changed ROLE_NAME to BAD_BOT_ROLE_NAME
MODERATOR_ROLE_NAME = 'moderators'
DELAY_MINUTES = 1
LOG_FILE = '/var/log/johnnybot.log'
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB

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
handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=5)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    update_bad_bots.start()

@tasks.loop(minutes=1)
async def update_bad_bots():
    roles_modified = False
    try:
        for guild in bot.guilds:
            bad_bots_role = discord.utils.get(guild.roles, name=BAD_BOT_ROLE_NAME)
            if bad_bots_role:
                for member in guild.members:
                    if not member.bot and bad_bots_role in member.roles:
                        if len(member.roles) > 2:
                            await member.remove_roles(bad_bots_role, reason='User has been assigned additional roles')
                            logger.info(f'Removed {BAD_BOT_ROLE_NAME} role from {member.name} in {guild.name}')
                            roles_modified = True
                        elif len(member.roles) == 1:
                            joined_at = member.joined_at
                            delay = DELAY_MINUTES * 60
                            if (discord.utils.utcnow() - joined_at).total_seconds() > delay:
                                await member.add_roles(bad_bots_role, reason=f'No role assigned after {DELAY_MINUTES} minutes')
                                logger.info(f'Assigned {BAD_BOT_ROLE_NAME} role to {member.name} in {guild.name}')
                                roles_modified = True
    except Exception as e:
        logger.error(f'Unable to complete task "update_bad_bots": {e}')
    else:
        if not roles_modified:
            logger.debug(f'Task "update_bad_bots" completed without modifying roles')

@update_bad_bots.before_loop
async def before_update_bad_bots():
    await bot.wait_until_ready()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    bad_bots_role = discord.utils.get(message.author.guild.roles, name=BAD_BOT_ROLE_NAME)
    if bad_bots_role in message.author.roles:
        await message.delete()
        logger.info(f'Deleted message from {message.author.name} in {message.guild.name}: {message.content}')

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
        logger.error(f'Error occurred: {error}')

if __name__ == '__main__':
    bot.run(TOKEN)
import discord
from discord.ext import commands
import os
import logging
from logging.handlers import RotatingFileHandler
import asyncio

TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
BAD_BOT_ROLE_NAME = 'bad bots'
MODERATOR_ROLE_NAME = 'Moderators'
DELAY_MINUTES = 4
LOG_FILE = 'johnnybot.log'
LOG_MAX_SIZE = 5 * 1024 * 1024  # 5MB
MODERATORS_CHANNEL_NAME = 'moderators_only'  # Name of the moderators channel
PROTECTED_CHANNELS = ['🫠・code_of_conduct', '🧚・hey_listen', '👯・local_events',
                      '🧩・ctf_announcements', '🖥・virtual_events']  # Users can't post here
LOGGING_CHANNEL_NAME = '🍻・general_lobbycon'  # Name of the channel to log kicks

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
    moderators_channel = discord.utils.get(guild.text_channels, name=MODERATORS_CHANNEL_NAME)
    if moderators_channel:
        await moderators_channel.send(message % args)

async def kick_and_delete_messages(member):
    guild = member.guild
    try:
        delete_messages = [msg async for msg in member.history(limit=None)]
        await member.kick(reason=f'No role assigned after {DELAY_MINUTES} minutes')
        await log_and_send_message(guild, 'Kicked %s from %s', member.name, guild.name)

        # Send a message to the logging channel
        logging_channel = discord.utils.get(guild.text_channels, name=LOGGING_CHANNEL_NAME)
        if logging_channel:
            await logging_channel.send(f'(¯`*•.¸,¤°´.｡.:* {member.name} is a bot and has been derezzed *:.｡.`°¤,¸.•*´¯)')
        else:
            logger.warning('Channel "%s" not found in guild %s', LOGGING_CHANNEL_NAME, guild.name)

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
                        await log_and_send_message(guild, 'Error deleting messages for %s in %s: %s (Status code: %d)',
                                                   member.name, channel.name, error_response.text,
                                                   error_response.status, level='error')
    except discord.errors.HTTPException as e:
        error_response = e.response
        await log_and_send_message(guild, 'Error kicking %s from %s: %s (Status code: %d)',
                                   member.name, guild.name, error_response.text,
                                   error_response.status, level='error')

@bot.event
async def on_member_join(member):
    guild = member.guild
    bad_bots_role, _, _ = await get_roles_and_channel(guild)
    if bad_bots_role:
        await member.add_roles(bad_bots_role, reason='New member joined')
        await log_and_send_message(guild, 'Assigned %s role to %s in %s',
                                   BAD_BOT_ROLE_NAME, member.name, guild.name)
        await asyncio.sleep(DELAY_MINUTES * 60)
        if member.roles == [guild.default_role, bad_bots_role]:
            await kick_and_delete_messages(member)

@bot.event
async def on_member_update(after):
    guild = after.guild
    bad_bots_role, _, _ = await get_roles_and_channel(guild)
    if bad_bots_role in after.roles and len(after.roles) > 2:
        await after.remove_roles(bad_bots_role, reason='User has additional roles')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    guild = message.guild
    if isinstance(message.channel, discord.DMChannel):
        bad_bots_role, _, _ = await get_roles_and_channel(guild)
        if (bad_bots_role in message.author.roles or len(message.author.roles) == 1) and message.author in guild.members:
            await kick_and_delete_messages(message.author)
    else:
        bad_bots_role, moderator_role, _ = await get_roles_and_channel(guild)
        if bad_bots_role in message.author.roles:
            await message.delete()
            logger.info('Deleted message from %s in %s: %s', message.author.name,
                        message.guild.name, message.content)
        elif message.channel.name in PROTECTED_CHANNELS:
            if moderator_role not in message.author.roles:
                try:
                    await message.delete()
                    logger.info('Deleted message from %s in protected channel %s: %s',
                                message.author.name, message.channel.name, message.content)
                except discord.errors.HTTPException as e:
                    error_response = e.response
                    logger.error('Error deleting message from %s in protected channel %s: %s (Status code: %d)',
                                 message.author.name, message.channel.name, error_response.text, error_response.status)
                    await log_and_send_message(guild, 'Error deleting message from %s in protected channel %s: %s (Status code: %d)',
                                               message.author.name, message.channel.name, error_response.text,
                                               error_response.status, level='error')
            
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

@bot.tree.command(name='kick', description='Kick a member from the server')
@commands.has_role(MODERATOR_ROLE_NAME)
async def kick_command(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f'Kicked {member.mention} from the server.', ephemeral=True)
        
        logging_channel = discord.utils.get(interaction.guild.text_channels, name=LOGGING_CHANNEL_NAME)
        if logging_channel:
            await logging_channel.send(f'(¯`*•.¸,¤°´.｡.:* {member.name} has been kicked from the server. Reason: {reason} *:.｡.`°¤,¸.•*´¯)')
        else:
            logger.warning('Channel "%s" not found in guild %s', LOGGING_CHANNEL_NAME, interaction.guild.name)
    except discord.errors.HTTPException as e:
        error_response = e.response
        await interaction.response.send_message(
            f'Error kicking {member.mention}: {error_response.text} (Status code: {error_response.status})',
            ephemeral=True
        )
        logger.error('Error kicking %s: %s (Status code: %d)', member.name, error_response.text, error_response.status)
        await log_and_send_message(interaction.guild, 'Error kicking %s: %s (Status code: %d)', member.name,
                                   error_response.text, error_response.status, level='error')

@bot.tree.command(name='ban', description='Ban a member from the server')
@commands.has_role(MODERATOR_ROLE_NAME)
async def ban_command(interaction: discord.Interaction, member: discord.Member, reason: str = None):
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f'Banned {member.mention} from the server.', ephemeral=True)
        
        logging_channel = discord.utils.get(interaction.guild.text_channels, name=LOGGING_CHANNEL_NAME)
        if logging_channel:
            await logging_channel.send(f'(¯`*•.¸,¤°´.｡.:* {member.name} has been banned from the server. Reason: {reason} *:.｡.`°¤,¸.•*´¯)')
        else:
            logger.warning('Channel "%s" not found in guild %s', LOGGING_CHANNEL_NAME, interaction.guild.name)
    except discord.errors.HTTPException as e:
        error_response = e.response
        await interaction.response.send_message(
            f'Error banning {member.mention}: {error_response.text} (Status code: {error_response.status})',
            ephemeral=True
        )
        logger.error('Error banning %s: %s (Status code: %d)', member.name, error_response.text, error_response.status)
        await log_and_send_message(interaction.guild, 'Error banning %s: %s (Status code: %d)', member.name,
                                   error_response.text, error_response.status, level='error')

@bot.tree.command(name='timeout', description='Timeout a member in the server')
@commands.has_role(MODERATOR_ROLE_NAME)
async def timeout_command(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = None):
    try:
        timeout_duration = parse_duration(duration)
        await member.timeout(timeout_duration, reason=reason)
        await interaction.response.send_message(f'Timed out {member.mention} for {duration}.', ephemeral=True)
        
        logging_channel = discord.utils.get(interaction.guild.text_channels, name=LOGGING_CHANNEL_NAME)
        if logging_channel:
            await logging_channel.send(f'(¯`*•.¸,¤°´.｡.:* {member.name} has been timed out for {duration}. Reason: {reason} *:.｡.`°¤,¸.•*´¯)')
        else:
            logger.warning('Channel "%s" not found in guild %s', LOGGING_CHANNEL_NAME, interaction.guild.name)
    except discord.errors.HTTPException as e:
        error_response = e.response
        await interaction.response.send_message(
            f'Error timing out {member.mention}: {error_response.text} (Status code: {error_response.status})',
            ephemeral=True
        )
        logger.error('Error timing out %s: %s (Status code: %d)', member.name, error_response.text, error_response.status)
        await log_and_send_message(interaction.guild, 'Error timing out %s: %s (Status code: %d)', member.name,
                                   error_response.text, error_response.status, level='error')

def parse_duration(duration: str) -> int:
    units = {
        's': 1,
        'm': 60,
        'h': 3600,
        'd': 86400
    }
    try:
        amount = int(duration[:-1])
        unit = duration[-1].lower()
        return amount * units[unit]
    except (ValueError, KeyError) as exc:
        raise ValueError('Invalid duration format. Use a number followed by a unit (s, m, h, d).') from exc

if __name__ == '__main__':
    bot.run(TOKEN)
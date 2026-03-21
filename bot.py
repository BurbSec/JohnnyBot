"""Discord bot for server management and automation with reminder functionality."""
# pylint: disable=line-too-long,trailing-whitespace,cyclic-import
import os
import asyncio
from datetime import datetime, timedelta

from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

import aiohttp
import discord
from discord.ext import commands

from config import (
    TOKEN,
    PROTECTED_CHANNELS,
    MODERATOR_ROLE_NAME,
    MODERATORS_CHANNEL_NAME,
    ADULT_ROLE_NAMES,
    CHILD_ROLE_NAMES,

    UPDATE_CHECKING_ENABLED,
    UPDATE_CHECK_REPO_URL,
    BOT_TIMEZONE,
    logger
)

_last_notified_commit = None

def _parse_repo_from_url(url):
    """Extract 'owner/repo' from a GitHub URL."""
    url = url.rstrip('/')
    parts = url.split('github.com/')
    if len(parts) == 2:
        return parts[1].removesuffix('.git')
    return None

async def check_for_updates():
    """Check for updates from the GitHub repository by comparing commit hashes."""
    global _last_notified_commit
    if not UPDATE_CHECKING_ENABLED:
        return

    # Get the current local commit hash (async to avoid blocking event loop)
    try:
        proc = await asyncio.create_subprocess_exec(
            'git', 'rev-parse', 'HEAD',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.dirname(__file__)
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("Failed to get local git commit hash: %s", stderr.decode())
            return
        local_commit = stdout.decode().strip()
    except OSError as e:
        logger.error("Error getting local git commit: %s", e)
        return

    # Build API URL from repo URL
    repo_path = _parse_repo_from_url(UPDATE_CHECK_REPO_URL)
    if not repo_path:
        logger.error("Could not parse repo from URL: %s", UPDATE_CHECK_REPO_URL)
        return

    # Get the latest commit hash and check if config.py changed
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Fetch latest commit
            api_url = f"https://api.github.com/repos/{repo_path}/commits/main"
            async with session.get(api_url) as response:
                if response.status != 200:
                    logger.error("Failed to fetch remote commit info: HTTP %s", response.status)
                    return
                data = await response.json()
                remote_commit = data['sha']

            # Skip if up to date or already notified for this commit
            if local_commit == remote_commit:
                logger.info("Bot is up to date: %s", local_commit[:8])
                return
            if remote_commit == _last_notified_commit:
                logger.info("Already notified for commit %s, skipping", remote_commit[:8])
                return

            # Check if config.py was changed between local and remote
            compare_url = f"https://api.github.com/repos/{repo_path}/compare/{local_commit}...{remote_commit}"
            async with session.get(compare_url) as response:
                config_changed = False
                if response.status == 200:
                    compare_data = await response.json()
                    changed_files = [f['filename'] for f in compare_data.get('files', [])]
                    config_changed = 'config.py' in changed_files
                else:
                    logger.warning("Failed to fetch commit comparison: HTTP %s", response.status)
    except (aiohttp.ClientError, KeyError, ValueError) as e:
        logger.error("Error fetching remote git info: %s", e)
        return

    logger.info("Update available: local=%s, remote=%s", local_commit[:8], remote_commit[:8])
    _last_notified_commit = remote_commit
    await send_update_notification(local_commit, remote_commit, config_changed)

async def send_update_notification(local_commit, remote_commit, config_changed=False):
    """Send update notification to the moderators channel."""
    try:
        moderators_channel = next(
            (ch for g in bot.guilds for ch in g.text_channels
             if ch.name == MODERATORS_CHANNEL_NAME), None)

        if not moderators_channel:
            logger.error("Moderators channel '%s' not found", MODERATORS_CHANNEL_NAME)
            return

        if config_changed:
            message = (
                "⚠️ **Breaking Changes in New Version**\n\n"
                f"`config.py` has been modified in the latest update.\n"
                f"Current version: `{local_commit[:8]}`\n"
                f"Latest version: `{remote_commit[:8]}`\n\n"
                f"**Please update manually** — review the config changes before pulling.\n\n"
                f"Repository: {UPDATE_CHECK_REPO_URL}"
            )
        else:
            message = (
                "🤖 **Bot Update Available!**\n\n"
                f"A new version of JohnnyBot is available on GitHub.\n"
                f"Current version: `{local_commit[:8]}`\n"
                f"Latest version: `{remote_commit[:8]}`\n\n"
                f"**To update:**\n"
                f"1. Run `git pull` from the bot directory on the server\n"
                f"2. Restart the bot service\n\n"
                f"Repository: {UPDATE_CHECK_REPO_URL}"
            )

        await moderators_channel.send(message)
        logger.info("Update notification sent to %s", moderators_channel.name)

    except (discord.HTTPException, discord.Forbidden) as e:
        logger.error("Error sending update notification: %s", e)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)

setattr(bot, '_ready_ran', False)

@bot.event
async def on_ready():  # pylint: disable=too-many-statements
    """Handle bot startup initialization including:
    - Syncing application commands
    - Starting background tasks
    - Initializing event feed scheduler
    """
    # Using protected member _ready_ran is necessary to track initialization state
    # This prevents duplicate initialization when the on_ready event fires multiple times
    if getattr(bot, '_ready_ran', False):
        return
    setattr(bot, '_ready_ran', True)

    if bot.user:
        logger.info('Logged in as %s (ID: %s)', bot.user, bot.user.id)
        logger.info('Bot initialization complete')

    registered_commands = bot.tree.get_commands()
    logger.info('Pre-sync commands: %s', [cmd.name for cmd in registered_commands])

    async def sync_commands():
        """Synchronize application commands with Discord.
        
        Attempts to sync commands with retry logic on failure.
        """
        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                await asyncio.sleep(5)

                pre_sync_commands = bot.tree.get_commands()
                if not pre_sync_commands:
                    logger.error("No commands found in command tree before sync")
                    raise RuntimeError("No commands found in command tree")

                synced = await bot.tree.sync()
                logger.info('Synced %d global commands', len(synced))

                registered = await bot.tree.fetch_commands()
                if not registered:
                    raise RuntimeError("No commands registered after sync")

                logger.info('Successfully registered commands: %s',
                           [cmd.name for cmd in registered])
                return

            except Exception as e:
                logger.error('Command sync attempt %d failed: %s',
                            attempt + 1, e)
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                raise

    try:
        await sync_commands()
    except (discord.HTTPException, discord.ClientException, RuntimeError,
            asyncio.TimeoutError) as e:
        logger.error('Final command sync failure: %s', e)

    try:
        from commands import event_feed, register_all_reminder_jobs  # pylint: disable=import-outside-toplevel

        if event_feed:
            sched = getattr(event_feed, 'scheduler', None)
            if sched and not sched.running:
                sched.start()

                # Feed check: weekly Monday 10am Central
                if hasattr(event_feed, 'check_feeds_job'):
                    sched.add_job(
                        event_feed.check_feeds_job,
                        trigger=CronTrigger(
                            day_of_week='mon', hour=10,
                            minute=0,
                            timezone=BOT_TIMEZONE),
                        id='weekly_feed_check',
                        replace_existing=True
                    )
                    logger.info(
                        'Feed check scheduled: Monday 10am CT')

                # Announce: Mon + Thu 10am Central
                if hasattr(event_feed, 'announce_weekly_events'):
                    sched.add_job(
                        event_feed.announce_weekly_events,
                        trigger=CronTrigger(
                            day_of_week='mon,thu', hour=10,
                            minute=0,
                            timezone=BOT_TIMEZONE),
                        id='weekly_announce',
                        replace_existing=True
                    )
                    logger.info(
                        'Announce scheduled: Mon+Thu 10am CT')

                # Daily update checking
                if UPDATE_CHECKING_ENABLED:
                    sched.add_job(
                        check_for_updates,
                        trigger=IntervalTrigger(hours=24),
                        next_run_time=(
                            datetime.now() + timedelta(minutes=5))
                    )
                    logger.info(
                        'Update checking scheduler started')

                # Register all persisted reminders as scheduler jobs
                register_all_reminder_jobs()
            else:
                logger.info('Event feed scheduler already running')
        else:
            logger.warning('Event feed not available')
    except (AttributeError, ImportError, ValueError) as e:
        logger.error('Failed to start event feed scheduler: %s', e)

@bot.event
async def on_message(message):
    """Monitor messages in protected channels and delete non-moderator messages. Also check for autoreply rules."""
    if message.author.bot:
        return
    
    # Check for autoreply rules first
    try:
        from commands import check_message_for_autoreplies  # pylint: disable=import-outside-toplevel
        await check_message_for_autoreplies(message)
    except (ImportError, AttributeError):
        # Commands module may not be fully initialized yet
        pass
    except Exception as e:
        logger.error('Error checking autoreply rules: %s', e)
    
    if message.channel.name in PROTECTED_CHANNELS:
        has_moderator_role = any(
            role.name == MODERATOR_ROLE_NAME
            for role in getattr(message.author, 'roles', []))
        if not has_moderator_role:
            try:
                await message.delete()
                logger.info(
                    'Deleted message from %s in protected channel %s: %s',
                    message.author.name,
                    message.channel.name,
                    message.content[:100] + '...' if len(message.content) > 100 else message.content
                )
            except discord.HTTPException as e:
                logger.error(
                    'Failed to delete message from %s in %s: %s',
                    message.author.name,
                    message.channel.name,
                    e
                )
    
    await bot.process_commands(message)

def get_user_role_type(member):
    """Determine if a user is an adult, child, or neither based on their roles."""
    role_names = {role.name for role in getattr(member, 'roles', [])}
    if role_names & ADULT_ROLE_NAMES:
        return 'adult'
    if role_names & CHILD_ROLE_NAMES:
        return 'child'
    return 'neither'

async def check_voice_channel_safety(channel):  # pylint: disable=too-many-branches
    """Check if a voice channel has only one adult and one child, and take action if so.
    
    Args:
        channel: Discord voice channel object
    """
    if not channel or not hasattr(channel, 'members'):
        return
    
    adults = []
    children = []
    
    for member in channel.members:
        if member.bot:
            continue
            
        role_type = get_user_role_type(member)
        if role_type == 'adult':
            adults.append(member)
        elif role_type == 'child':
            children.append(member)
    
    if len(adults) == 1 and len(children) == 1:
        logger.warning(
            'ALERT: One adult (%s) and one child (%s) detected in voice channel %s',
            adults[0].display_name,
            children[0].display_name,
            channel.name
        )
        
        for member in channel.members:
            if not member.bot:
                try:
                    await member.edit(mute=True)
                    logger.info('Muted %s in channel %s', member.display_name, channel.name)
                except discord.HTTPException as e:
                    logger.error('Failed to mute %s: %s', member.display_name, e)
        
        try:
            moderators_channel = discord.utils.get(
                channel.guild.text_channels,
                name=MODERATORS_CHANNEL_NAME)
            
            if moderators_channel:
                alert_message = (
                    f"🚨 **ALERT**: There is only one adult ({adults[0].mention}) and one child "
                    f"({children[0].mention}) currently in {channel.mention}\n\n"
                    f"All members in the channel have been muted for safety."
                )
                await moderators_channel.send(alert_message)
                logger.info('Alert sent to moderators channel for voice channel %s', channel.name)
            else:
                logger.error('Moderators channel "%s" not found', MODERATORS_CHANNEL_NAME)
                
        except discord.HTTPException as e:
            logger.error('Failed to send alert to moderators channel: %s', e)

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes - monitor for adult/child combinations."""
    if member.bot:
        return
    
    # Check if voice chaperone functionality is enabled (check config module directly for runtime changes)
    import config
    if not config.VOICE_CHAPERONE_ENABLED:
        return
    
    channels_to_check = set()
    
    if before.channel:
        channels_to_check.add(before.channel)
    
    if after.channel:
        channels_to_check.add(after.channel)
    
    for channel in channels_to_check:
        await check_voice_channel_safety(channel)

from commands import setup_commands  # pylint: disable=wrong-import-position
setup_commands(bot)

try:
    if TOKEN:
        bot.run(TOKEN)
    else:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is not set")
except KeyboardInterrupt:
    logger.info("Shutting down gracefully...")
except Exception as e:
    logger.error("Fatal error: %s", e)
    raise

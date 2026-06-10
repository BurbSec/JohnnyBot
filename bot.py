"""Discord bot for server management and automation with reminder functionality."""
# pylint: disable=line-too-long,trailing-whitespace,cyclic-import
import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

import aiohttp
import discord
from discord.ext import commands

import config
from config import (
    TOKEN,
    PROTECTED_CHANNELS,
    MODERATOR_ROLE_NAME,
    MODERATORS_CHANNEL_NAME,
    ADULT_ROLE_NAMES,
    CHILD_ROLE_NAMES,

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

def _get_moderators_channel():
    """Find the moderators channel across all guilds, or None."""
    return next(
        (ch for g in bot.guilds for ch in g.text_channels
         if ch.name == MODERATORS_CHANNEL_NAME), None)

async def _run_cmd(*args):
    """Run a command in the bot directory without blocking the event loop.

    Returns (returncode, stdout, stderr) as decoded strings.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=os.path.dirname(os.path.abspath(__file__))
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

async def _ci_passed(session, repo_path, sha):
    """Return True if all completed check runs for `sha` succeeded.

    Requires at least one check run so an untested commit is never
    auto-deployed.
    """
    url = f"https://api.github.com/repos/{repo_path}/commits/{sha}/check-runs"
    try:
        async with session.get(url) as response:
            if response.status != 200:
                logger.warning("Failed to fetch check runs: HTTP %s", response.status)
                return False
            runs = (await response.json()).get('check_runs', [])
    except (aiohttp.ClientError, KeyError, ValueError) as e:
        logger.error("Error fetching check runs: %s", e)
        return False
    if not runs:
        logger.info("No check runs found for %s; skipping auto-update", sha[:8])
        return False
    return all(
        run['status'] == 'completed'
        and run['conclusion'] in ('success', 'neutral', 'skipped')
        for run in runs
    )

async def _auto_update_and_restart(local_commit, remote_commit, deps_changed):
    """Pull the latest code, reinstall deps if needed, and re-exec the bot.

    On success this never returns (the process is replaced). Returns an
    error string on failure so the caller can notify moderators.
    """
    rc, _, err = await _run_cmd('git', 'pull', '--ff-only')
    if rc != 0:
        return f"`git pull --ff-only` failed: {err or 'unknown error'}"

    if deps_changed:
        rc, _, err = await _run_cmd(
            sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt')
        if rc != 0:
            # Code is already pulled; restarting with missing deps could
            # crash-loop, so bail out and ask for a manual fix
            return f"`pip install -r requirements.txt` failed: {err or 'unknown error'}"

    channel = _get_moderators_channel()
    if channel:
        try:
            await channel.send(
                "🤖 **Auto-updating JohnnyBot**\n\n"
                f"`{local_commit[:8]}` → `{remote_commit[:8]}` — "
                "restarting now. Back in a moment!"
            )
        except (discord.HTTPException, discord.Forbidden) as e:
            logger.error("Error sending auto-update notice: %s", e)

    logger.info("Auto-update complete (%s -> %s); restarting",
                local_commit[:8], remote_commit[:8])
    logging.shutdown()
    bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.py')
    os.execv(sys.executable, [sys.executable, bot_path])
    return None  # unreachable; satisfies linters

async def check_for_updates():
    """Check for updates from the GitHub repository by comparing commit hashes.

    With AUTO_UPDATE_ENABLED, updates whose CI passed and which don't
    touch config_example.py are pulled and the bot restarts itself;
    everything else falls back to a moderator notification.
    """
    global _last_notified_commit
    # Read via the config module so /update_checking toggles take
    # effect at runtime (a `from config import` binding would not)
    if not config.UPDATE_CHECKING_ENABLED:
        return

    # Get the current local commit hash (async to avoid blocking event loop)
    try:
        rc, local_commit, err = await _run_cmd('git', 'rev-parse', 'HEAD')
        if rc != 0:
            logger.error("Failed to get local git commit hash: %s", err)
            return
    except OSError as e:
        logger.error("Error getting local git commit: %s", e)
        return

    # Build API URL from repo URL
    repo_path = _parse_repo_from_url(UPDATE_CHECK_REPO_URL)
    if not repo_path:
        logger.error("Could not parse repo from URL: %s", UPDATE_CHECK_REPO_URL)
        return

    auto_update = bool(getattr(config, 'AUTO_UPDATE_ENABLED', False))

    # Get the latest commit hash and check what changed
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

            # Check what changed between local and remote
            compare_url = f"https://api.github.com/repos/{repo_path}/compare/{local_commit}...{remote_commit}"
            changed_files = []
            async with session.get(compare_url) as response:
                if response.status == 200:
                    compare_data = await response.json()
                    changed_files = [f['filename'] for f in compare_data.get('files', [])]
                else:
                    logger.warning("Failed to fetch commit comparison: HTTP %s", response.status)
            config_changed = 'config_example.py' in changed_files

            # Auto-update only commits that passed CI and don't
            # require config changes
            ci_ok = False
            if auto_update and not config_changed:
                ci_ok = await _ci_passed(session, repo_path, remote_commit)
    except (aiohttp.ClientError, KeyError, ValueError) as e:
        logger.error("Error fetching remote git info: %s", e)
        return

    logger.info("Update available: local=%s, remote=%s", local_commit[:8], remote_commit[:8])

    if auto_update and not config_changed and ci_ok:
        error = await _auto_update_and_restart(
            local_commit, remote_commit, 'requirements.txt' in changed_files)
        # Only reached on failure — os.execv never returns
        logger.error("Auto-update failed: %s", error)
        _last_notified_commit = remote_commit
        channel = _get_moderators_channel()
        if channel:
            try:
                await channel.send(
                    "❌ **Auto-update failed**\n\n"
                    f"{error}\n\n"
                    "**Please update manually** from the bot directory "
                    "on the server.\n\n"
                    f"Repository: {UPDATE_CHECK_REPO_URL}"
                )
            except (discord.HTTPException, discord.Forbidden) as e:
                logger.error("Error sending auto-update failure notice: %s", e)
        return

    if auto_update and not config_changed and not ci_ok:
        logger.info("Auto-update skipped: CI not green for %s", remote_commit[:8])

    _last_notified_commit = remote_commit
    await send_update_notification(local_commit, remote_commit, config_changed)

async def send_update_notification(local_commit, remote_commit, config_changed=False):
    """Send update notification to the moderators channel."""
    try:
        moderators_channel = _get_moderators_channel()

        if not moderators_channel:
            logger.error("Moderators channel '%s' not found", MODERATORS_CHANNEL_NAME)
            return

        if config_changed:
            message = (
                "⚠️ **Breaking Changes in New Version**\n\n"
                f"`config_example.py` has been modified in the latest update.\n"
                f"Current version: `{local_commit[:8]}`\n"
                f"Latest version: `{remote_commit[:8]}`\n\n"
                f"**Please update manually** — review the config changes and "
                f"update your local `config.py` before pulling.\n\n"
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

_ready_ran = False

@bot.event
async def on_ready():  # pylint: disable=too-many-statements
    """Handle bot startup initialization including:
    - Syncing application commands
    - Starting background tasks
    - Initializing event feed scheduler
    """
    global _ready_ran
    if _ready_ran:
        return
    _ready_ran = True

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
                # Route APScheduler logs to the bot log file so job errors
                # are visible. Attach handlers only to the parent logger;
                # child loggers propagate up, so attaching to both would
                # emit each message twice.
                _aplogger = logging.getLogger('apscheduler')
                _aplogger.setLevel(logging.INFO)
                for _h in logger.handlers:
                    if _h not in _aplogger.handlers:
                        _aplogger.addHandler(_h)

                sched.start()

                # Feed check: Monday 9am Central (pulls next 30 days,
                # creates Discord Events) — runs one hour before the
                # weekly announce so new events are visible when it fires
                if hasattr(event_feed, 'check_feeds_job'):
                    sched.add_job(
                        event_feed.check_feeds_job,
                        trigger=CronTrigger(
                            day_of_week='mon', hour=9,
                            minute=0,
                            timezone=BOT_TIMEZONE),
                        id='weekly_feed_check',
                        replace_existing=True
                    )
                    logger.info(
                        'Feed check scheduled: Monday 9am CT')

                # Weekly preview: Monday 10am Central
                if hasattr(event_feed, 'announce_weekly_events'):
                    sched.add_job(
                        event_feed.announce_weekly_events,
                        trigger=CronTrigger(
                            day_of_week='mon', hour=10,
                            minute=0,
                            timezone=BOT_TIMEZONE),
                        id='weekly_announce',
                        replace_existing=True
                    )
                    logger.info(
                        'Weekly announce scheduled: Monday 10am CT')

                # Day-of reminder: daily 10am Central
                if hasattr(event_feed, 'announce_todays_events'):
                    sched.add_job(
                        event_feed.announce_todays_events,
                        trigger=CronTrigger(
                            hour=10,
                            minute=0,
                            timezone=BOT_TIMEZONE),
                        id='daily_event_reminder',
                        replace_existing=True
                    )
                    logger.info(
                        'Day-of reminder scheduled: daily 10am CT')

                # Daily update checking. Always scheduled so the
                # /update_checking runtime toggle works; the job
                # itself no-ops when checking is disabled.
                sched.add_job(
                    check_for_updates,
                    trigger=IntervalTrigger(hours=24),
                    next_run_time=(
                        datetime.now() + timedelta(minutes=5))
                )
                logger.info(
                    'Update checking scheduled (enabled=%s)',
                    config.UPDATE_CHECKING_ENABLED)

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

    try:
        await _check_autoreplies(message)
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

    # Read VOICE_CHAPERONE_ENABLED from the config module each call so
    # runtime toggles via /voice_chaperone take effect immediately.
    if not config.VOICE_CHAPERONE_ENABLED:
        return
    
    channels_to_check = set()
    
    if before.channel:
        channels_to_check.add(before.channel)
    
    if after.channel:
        channels_to_check.add(after.channel)
    
    for channel in channels_to_check:
        await check_voice_channel_safety(channel)

from commands import (  # pylint: disable=wrong-import-position
    setup_commands,
    check_message_for_autoreplies as _check_autoreplies,
)
setup_commands(bot)

def main():
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


if __name__ == '__main__':
    main()

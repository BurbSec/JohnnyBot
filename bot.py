"""Discord bot for server management and automation with reminder functionality."""
# pylint: disable=line-too-long,trailing-whitespace,cyclic-import
import os
import re
import sys
import asyncio
import hashlib
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

import aiohttp
import discord
from discord.ext import commands

import json

import config
from config import (
    TOKEN,
    PROTECTED_CHANNELS,
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

def _short_sha(sha):
    return sha[:8] if sha else '?'


async def _local_tag_for_commit(sha):
    """Return a git tag pointing at `sha` in the local checkout, or None.

    Update detection stays commit-SHA-based (see check_for_updates) —
    this only resolves a friendlier label to display, so a missing or
    unparsable tag never blocks an update, it just falls back to the
    short SHA.
    """
    try:
        rc, out, _ = await _run_cmd('git', 'tag', '--points-at', sha)
    except OSError:
        return None
    if rc == 0 and out:
        return out.splitlines()[0]
    return None


async def _remote_tag_for_commit(session, repo_path, sha):
    """Return a git tag pointing at `sha` on the remote, or None."""
    url = f"https://api.github.com/repos/{repo_path}/tags"
    try:
        async with session.get(url) as response:
            if response.status != 200:
                return None
            tags = await response.json()
    except (aiohttp.ClientError, KeyError, ValueError, TypeError):
        return None
    for t in tags:
        if t.get('commit', {}).get('sha') == sha:
            return t.get('name')
    return None


async def _version_label(session, repo_path, sha, *, local):
    """A tag name if `sha` is tagged, else its short SHA."""
    tag = (await _local_tag_for_commit(sha) if local
           else await _remote_tag_for_commit(session, repo_path, sha))
    return tag or _short_sha(sha)


async def _auto_update_and_restart(local_commit, remote_commit, deps_changed,
                                   local_version=None, remote_version=None):
    """Pull the latest code, reinstall deps if needed, and re-exec the bot.

    On success this never returns (the process is replaced). Returns an
    error string on failure so the caller can notify moderators.
    """
    local_version = local_version or _short_sha(local_commit)
    remote_version = remote_version or _short_sha(remote_commit)

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
                f"`{local_version}` → `{remote_version}` — "
                "restarting now. Back in a moment!"
            )
        except (discord.HTTPException, discord.Forbidden) as e:
            logger.error("Error sending auto-update notice: %s", e)

    logger.info("Auto-update complete (%s -> %s); restarting",
                local_version, remote_version)
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

            # Tag names are cosmetic only — detection above stays
            # commit-SHA-based regardless of whether either side is
            # tagged, so an untagged commit (or a fork with no tags at
            # all) still updates correctly, just displayed by short SHA.
            local_version = await _version_label(session, repo_path, local_commit, local=True)
            remote_version = await _version_label(session, repo_path, remote_commit, local=False)
    except (aiohttp.ClientError, KeyError, ValueError) as e:
        logger.error("Error fetching remote git info: %s", e)
        return

    logger.info("Update available: local=%s, remote=%s", local_version, remote_version)

    if auto_update and not config_changed and ci_ok:
        error = await _auto_update_and_restart(
            local_commit, remote_commit, 'requirements.txt' in changed_files,
            local_version, remote_version)
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
        logger.info("Auto-update skipped: CI not green for %s", remote_version)

    _last_notified_commit = remote_commit
    await send_update_notification(local_version, remote_version, config_changed)

async def send_update_notification(local_version, remote_version, config_changed=False):
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
                f"Current version: `{local_version}`\n"
                f"Latest version: `{remote_version}`\n\n"
                f"**Please update manually** — review the config changes and "
                f"update your local `config.py` before pulling.\n\n"
                f"Repository: {UPDATE_CHECK_REPO_URL}"
            )
        else:
            message = (
                "🤖 **Bot Update Available!**\n\n"
                f"A new version of JohnnyBot is available on GitHub.\n"
                f"Current version: `{local_version}`\n"
                f"Latest version: `{remote_version}`\n\n"
                f"**To update:**\n"
                f"1. Run `git pull` from the bot directory on the server\n"
                f"2. Restart the bot service\n\n"
                f"Repository: {UPDATE_CHECK_REPO_URL}"
            )

        await moderators_channel.send(message)
        logger.info("Update notification sent to %s", moderators_channel.name)

    except (discord.HTTPException, discord.Forbidden) as e:
        logger.error("Error sending update notification: %s", e)


# Version-change announcements
# ---------------------------------------------------------------------------
# Both existing notices fire *before* anything changes — "Update
# Available", and "restarting now" sent immediately before os.execv. So
# nothing ever confirms an update actually landed: if the new version
# fails to boot, the last thing moderators saw was "Back in a moment!",
# and silence looks identical to success. A manual `git pull` + restart
# announces nothing at all. This is the missing half, posted on the first
# start after the running commit changes.

# Kept out of config.py for the same reason as CHAPERONE_MUTES_FILE:
# deployed configs are not tracked by git and would not have the constant
# after a pull.
VERSION_STATE_FILE = getattr(
    config, 'VERSION_STATE_FILE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 'version_state.json'))

# Well under Discord's 2000-character cap, leaving room for the header.
_RELEASE_NOTES_BUDGET = 1500


def _load_version_state():
    """The commit/label recorded on the last start, or None."""
    if not os.path.exists(VERSION_STATE_FILE):
        return None
    try:
        with open(VERSION_STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
        return state if isinstance(state, dict) else None
    except (OSError, IOError, ValueError, TypeError) as e:
        logger.error('Failed to read version state: %s', e)
        return None


def _save_version_state(sha, label):
    """Record the running version. Never let this abort startup."""
    try:
        _atomic_json_write(VERSION_STATE_FILE, {
            'commit': sha,
            'label': label,
            'recorded_at': datetime.now(timezone.utc).isoformat(),
        })
    except (OSError, IOError, TypeError, ValueError) as e:
        logger.error('Failed to save version state: %s', e)


async def _current_version():
    """(sha, label) for the running checkout, or None if git is absent.

    Update checking already assumes a git checkout, so a tarball or
    container install without one simply opts out rather than erroring.
    """
    try:
        rc, sha, err = await _run_cmd('git', 'rev-parse', 'HEAD')
    except OSError as e:
        logger.info('Version announce: git unavailable (%s)', e)
        return None
    if rc != 0 or not sha:
        logger.info('Version announce: could not resolve HEAD (%s)', err)
        return None
    label = await _local_tag_for_commit(sha) or _short_sha(sha)
    return sha, label


async def _is_ancestor(old_sha, new_sha):
    """True if old_sha is an ancestor of new_sha — i.e. a forward move.

    Distinguishes an upgrade from a rollback or a diverged checkout, so a
    surprise `git checkout` of an older tag reads as what it is instead
    of being announced as an update.
    """
    try:
        rc, _, _ = await _run_cmd(
            'git', 'merge-base', '--is-ancestor', old_sha, new_sha)
    except OSError:
        return True
    return rc == 0


# Signed tags carry the signature inside the annotation, so `%(contents)`
# returns it too. This repo signs with SSH, others use PGP/GPG — match
# any of them rather than one, or a wall of base64 gets posted to the
# moderators channel.
_SIGNATURE_RE = re.compile(r'^-----BEGIN [A-Z0-9 ]*SIGNATURE-----',
                           re.MULTILINE)


def _strip_signature(notes):
    """Drop a trailing signature block from a tag annotation."""
    match = _SIGNATURE_RE.search(notes)
    return notes[:match.start()].strip() if match else notes


async def _tag_release_notes(label):
    """The annotated tag's message for `label`, or ''.

    Deliberately the tag annotation rather than `git log old..new`: a
    commit log is noise in a moderators channel — every chore, CI tweak
    and doc fix — while the annotation is written by whoever cut the
    release and therefore contains the highlights by construction.

    Returns '' for a lightweight tag (no message), an untagged commit, or
    any git failure. A missing body must never suppress the announcement.
    """
    if not label or not label.startswith('v'):
        return ''
    try:
        rc, out, _ = await _run_cmd(
            'git', 'for-each-ref', '--format=%(contents)',
            f'refs/tags/{label}')
    except OSError:
        return ''
    if rc != 0 or not out.strip():
        return ''
    notes = _strip_signature(out.strip())
    if len(notes) > _RELEASE_NOTES_BUDGET:
        notes = notes[:_RELEASE_NOTES_BUDGET].rstrip() + '\n…'
    return notes


async def announce_version_change():
    """Post to every guild's moderators channel when the running version
    differs from the one recorded on the last start.

    Posts to each guild rather than via _get_moderators_channel(), which
    returns the first match across all guilds — in a multi-guild
    deployment only one server would ever learn the bot changed. The
    existing update notices still use that single-channel lookup; that
    inconsistency is known and left alone here.
    """
    if not getattr(config, 'VERSION_ANNOUNCE_ENABLED', True):
        return

    current = await _current_version()
    if current is None:
        return
    sha, label = current

    previous = _load_version_state()
    if previous is None:
        # First start after this feature ships, or a fresh install.
        # Recording silently avoids a bogus "updated" notice on every
        # existing deployment the first time it runs.
        await asyncio.to_thread(_save_version_state, sha, label)
        logger.info('Version announce: recorded baseline %s', label)
        return

    if previous.get('commit') == sha:
        return  # ordinary restart, reconnect, or crash-loop

    old_label = previous.get('label') or _short_sha(previous.get('commit'))
    forward = await _is_ancestor(previous.get('commit', ''), sha)
    header = (f"🤖 **JohnnyBot updated** — `{old_label}` → `{label}`"
              if forward else
              f"🔄 **JohnnyBot version changed** — `{old_label}` → "
              f"`{label}` (rollback or diverged checkout)")

    notes = await _tag_release_notes(label)
    message = f"{header}\n\n{notes}" if notes else header

    sent = 0
    for guild in bot.guilds:
        channel = discord.utils.get(
            guild.text_channels, name=MODERATORS_CHANNEL_NAME)
        if channel is None:
            # Matches the "not found" logging every other alert path in
            # this file does (raid/anti-nuke/spam) — a silent `continue`
            # here left this exact bug invisible: the info line below
            # fired unconditionally, so a channel-name mismatch looked
            # identical to a successful send in the log.
            logger.error('Moderators channel "%s" not found in %s',
                         MODERATORS_CHANNEL_NAME, guild.name)
            continue
        try:
            await channel.send(message)
            sent += 1
        except (discord.HTTPException, discord.Forbidden) as e:
            logger.error('Failed to announce version change in %s: %s',
                         guild.name, e)

    logger.info('Version announce: %s -> %s (delivered to %d guild(s))',
               old_label, label, sent)
    await asyncio.to_thread(_save_version_state, sha, label)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True
# Non-privileged; enables on_audit_log_entry_create for anti-nuke
# protection. Already on via Intents.default(), set explicitly for
# clarity alongside the other intents here.
intents.moderation = True

bot = commands.Bot(command_prefix='!', intents=intents)

async def _sync_commands():
    """Synchronize application commands with Discord, with retries."""
    max_retries = 3
    retry_delay = 5

    for attempt in range(max_retries):
        try:
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

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error('Command sync attempt %d failed: %s',
                        attempt + 1, e)
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay * (attempt + 1))
                continue
            raise


@bot.event
async def setup_hook():
    """One-time async startup, run before the gateway connects.

    Command sync belongs here rather than in on_ready: on_ready fires
    again on every reconnect, which is what the old _ready_ran flag and
    the leading asyncio.sleep(5) were working around.

    Only the sync moves. Anything needing bot.guilds — the chaperone
    sweep — or anything whose jobs call bot.get_channel — the scheduler
    registrations — must stay in on_ready, because guild state is not
    populated yet at this point.
    """
    logger.info('Pre-sync commands: %s',
                [cmd.name for cmd in bot.tree.get_commands()])
    try:
        await _sync_commands()
    except (discord.HTTPException, discord.ClientException, RuntimeError,
            asyncio.TimeoutError) as e:
        logger.error('Final command sync failure: %s', e)


_ready_ran = False

@bot.event
async def on_ready():  # pylint: disable=too-many-statements
    """Post-connect startup: chaperone sweep and scheduler registration.

    Still guarded by _ready_ran because on_ready re-fires on reconnect
    and these steps are not idempotent-by-accident. Command sync now
    happens once in setup_hook().
    """
    global _ready_ran
    if _ready_ran:
        return
    _ready_ran = True

    if bot.user:
        logger.info('Logged in as %s (ID: %s)', bot.user, bot.user.id)
        logger.info('Bot initialization complete')

    # Lift any chaperone mutes left outstanding by the last shutdown
    _load_chaperone_mutes()
    try:
        await sweep_chaperone_mutes()
    except (discord.HTTPException, AttributeError) as e:
        logger.error('Chaperone startup sweep failed: %s', e)

    try:
        from commands import event_feed, register_all_reminder_jobs, register_all_auto_backup_jobs  # pylint: disable=import-outside-toplevel,line-too-long

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

                # Day-of reminder: 10am Central, Tue-Sun. Mondays are
                # skipped because the weekly preview fires at the same
                # minute and already covers that day's events — running
                # both posted every Monday event twice.
                if hasattr(event_feed, 'announce_todays_events'):
                    sched.add_job(
                        event_feed.announce_todays_events,
                        trigger=CronTrigger(
                            day_of_week='tue-sun',
                            hour=10,
                            minute=0,
                            timezone=BOT_TIMEZONE),
                        id='daily_event_reminder',
                        replace_existing=True
                    )
                    logger.info(
                        'Day-of reminder scheduled: Tue-Sun 10am CT')

                # Daily update checking; the job no-ops when
                # UPDATE_CHECKING_ENABLED is off in config.py
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

                # Register all persisted auto-backup jobs
                register_all_auto_backup_jobs()
            else:
                logger.info('Event feed scheduler already running')
        else:
            logger.warning('Event feed not available')
    except (AttributeError, ImportError, ValueError) as e:
        logger.error('Failed to start event feed scheduler: %s', e)

    # Last, so a git or network problem here can never delay the
    # chaperone sweep or the scheduler above.
    try:
        await announce_version_change()
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Version announce failed: %s', e)

async def handle_unsolicited_dm(message):
    """Kick anyone who DMs the bot.

    Exempt: moderators, and anyone the bot itself DMed recently — the
    /message_dump archive and /log_tail output arrive by DM, so a reply
    to one of those is solicited and must not be punished.
    """
    author = message.author

    if _was_recently_dmed(author.id):
        logger.info(
            'Ignoring DM from %s (replying to a DM we sent)', author)
        return

    # A user can share more than one guild with the bot; moderator
    # status anywhere exempts them everywhere. Fall back to an API
    # fetch when the member cache misses — kicking is irreversible for
    # the member, so never decide it on a stale cache.
    # Anyone on the internet can DM the bot, and this used to spend one
    # fetch_member API call per guild on every such DM — an easy way for
    # an outsider to burn the global rate limit.
    #
    # A cache miss is only trustworthy when the guild is chunked, i.e.
    # its member cache is known complete. Before chunking finishes (cold
    # start, or a large guild) a miss means nothing, and treating it as
    # "not a member" would silently exempt a real member from a kick —
    # so those guilds still get an API confirmation. Kicking is
    # irreversible; it must never run off a cache that may be stale.
    candidates = []
    for guild in bot.guilds:
        member = guild.get_member(author.id)
        if member is not None:
            candidates.append((guild, member))
        elif not guild.chunked:
            candidates.append((guild, None))  # must confirm via the API

    if not candidates:
        logger.info(
            'DM from %s who is not a member of any chunked guild; '
            'nothing to kick', author)
        return

    memberships = []
    for guild, member in candidates:
        if member is None:
            try:
                member = await guild.fetch_member(author.id)
            except discord.NotFound:
                continue
            except discord.HTTPException as e:
                logger.error(
                    'Could not fetch %s in %s, skipping kick there: %s',
                    author, guild.name, e)
                continue
        memberships.append(member)
    # Matches the mod_only command gate (manage_messages permission)
    # rather than a role literally named MODERATOR_ROLE_NAME, so DM
    # exemption tracks the same authorization as command access.
    if any(getattr(getattr(m, 'guild_permissions', None), 'manage_messages', False)
           for m in memberships):
        logger.info('Ignoring DM from moderator %s', author)
        return

    if not memberships:
        logger.info(
            'DM from %s who shares no guild with the bot; nothing to kick',
            author)
        return

    logger.warning('Kicking %s for DMing the bot', author)
    for member in memberships:
        try:
            await member.kick(reason='Sent an unsolicited DM to the bot')
            logger.info('Kicked %s from %s for DMing the bot',
                        member, member.guild.name)
        except discord.Forbidden:
            logger.error(
                'Missing permission to kick %s from %s',
                member, member.guild.name)
        except discord.HTTPException as e:
            logger.error('Failed to kick %s from %s: %s',
                         member, member.guild.name, e)
            continue

        moderators_channel = discord.utils.get(
            member.guild.text_channels, name=MODERATORS_CHANNEL_NAME)
        if not moderators_channel:
            continue
        try:
            await moderators_channel.send(
                f"👢 Kicked **{member}** (ID: {member.id}) for DMing the bot.\n"
                f"> {message.content[:200] or '[no text content]'}"
            )
        except (discord.HTTPException, discord.Forbidden) as e:
            logger.error('Failed to report DM kick: %s', e)

@bot.event
async def on_message(message):
    """Monitor messages in protected channels and delete non-moderator messages. Also check for autoreply rules."""
    if message.author.bot:
        return

    # DMs have no guild and DMChannel has no .name, so handle them
    # before anything that assumes a guild channel.
    if message.guild is None:
        try:
            await handle_unsolicited_dm(message)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error('Error handling DM from %s: %s', message.author, e)
        return

    # Runs before the autoreply and protected-channel checks below: when
    # this deletes the message, there is nothing left to reply to.
    try:
        if await check_message_spam(message):
            return
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Error checking message for spam: %s', e)

    try:
        await _check_autoreplies(message)
    except (ImportError, AttributeError):
        # Commands module may not be fully initialized yet
        pass
    except Exception as e:
        logger.error('Error checking autoreply rules: %s', e)

    if getattr(message.channel, 'name', None) in PROTECTED_CHANNELS:
        # Matches the mod_only command gate (manage_messages permission)
        # rather than a role literally named MODERATOR_ROLE_NAME, so
        # protected-channel enforcement tracks the same authorization
        # as command access.
        is_moderator = getattr(
            getattr(message.author, 'guild_permissions', None),
            'manage_messages', False)
        if not is_moderator:
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

# Members the chaperone itself muted, so we only ever lift our own
# mutes and never one a moderator applied by hand. Persisted, because
# a restart while someone is muted would otherwise strand them: the
# unmute path skips anyone missing from this set.
# Keyed by (guild_id, user_id), not a bare user id: the same person can
# be in voice in two guilds the bot serves, and a single-guild key meant
# one guild's mute suppressed the other's mute *and* its unmute.
_chaperone_muted = set()
# Voice channels currently in the flagged 1-adult/1-child state.
# Muting a member re-fires on_voice_state_update, so without this the
# same incident alerts the moderators two or three times. Not
# persisted — rebuilt by the startup sweep from live channel state.
_chaperone_flagged = set()

# Kept out of config.py: deployed configs are not tracked by git and
# would not have the constant after a pull.
CHAPERONE_MUTES_FILE = getattr(
    config, 'CHAPERONE_MUTES_FILE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 'chaperone_mutes.json'))

def _load_chaperone_mutes():
    """Restore the set of members this feature muted before a restart."""
    if not os.path.exists(CHAPERONE_MUTES_FILE):
        return
    try:
        with open(CHAPERONE_MUTES_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        legacy = 0
        for entry in raw:
            # New format is [guild_id, user_id]; the old one was a bare
            # user id. A legacy entry can't name its guild, so it is
            # dropped — the startup sweep re-flags anyone still in an
            # unsafe channel, and the worst case is a stale mute a
            # moderator clears once.
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                _chaperone_muted.add((int(entry[0]), int(entry[1])))
            else:
                legacy += 1
        if legacy:
            logger.warning(
                'Dropped %d chaperone mute(s) in the pre-guild-key '
                'format; the startup sweep will re-evaluate', legacy)
        logger.info('Loaded %d outstanding chaperone mutes',
                    len(_chaperone_muted))
    except (OSError, IOError, ValueError, TypeError) as e:
        logger.error('Failed to load chaperone mutes: %s', e)

def _save_chaperone_mutes():
    """Persist outstanding chaperone mutes so a restart can lift them."""
    # Never let a persistence problem abort the mute/unmute it is
    # recording — the safety action matters more than the bookkeeping.
    try:
        _atomic_json_write(
            CHAPERONE_MUTES_FILE,
            [[int(gid), int(uid)] for gid, uid in _chaperone_muted])
    except (OSError, IOError, TypeError, ValueError) as e:
        logger.error('Failed to save chaperone mutes: %s', e)

async def sweep_chaperone_mutes():
    """Re-evaluate every populated voice channel at startup.

    Rebuilds the flagged-channel set from live state and lifts mutes
    left over from before the restart.
    """
    if not config.VOICE_CHAPERONE_ENABLED:
        return
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            if channel.members:
                await check_voice_channel_safety(channel)
    if _chaperone_muted:
        logger.info(
            '%d chaperone mutes still outstanding after startup sweep '
            '(members not currently connected to voice)',
            len(_chaperone_muted))

def _count_adults_children(channel):
    """Return (adults, children) among the non-bot members of a channel."""
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
    return adults, children

async def _unmute_member(member, reason):
    """Lift a chaperone mute. Discord rejects this unless the member is
    connected to voice, so leave them flagged and retry on rejoin."""
    key = (member.guild.id, member.id)
    if key not in _chaperone_muted:
        return
    if not member.voice or not member.voice.channel:
        return
    try:
        await member.edit(mute=False)
        _chaperone_muted.discard(key)
        _save_chaperone_mutes()
        logger.info('Unmuted %s (%s)', member.display_name, reason)
    except discord.HTTPException as e:
        logger.error('Failed to unmute %s: %s', member.display_name, e)

async def check_voice_channel_safety(channel):  # pylint: disable=too-many-branches
    """Check if a voice channel has only one adult and one child, and take action if so.

    When the channel returns to a safe state, any mute this feature
    applied is lifted again — otherwise members stay server-muted
    indefinitely and a moderator has to clear each one by hand.

    Args:
        channel: Discord voice channel object
    """
    if not channel or not hasattr(channel, 'members'):
        return

    adults, children = _count_adults_children(channel)
    unsafe = len(adults) == 1 and len(children) == 1

    if not unsafe:
        # Safe again (or empty) — release our mutes and re-arm alerting
        was_flagged = channel.id in _chaperone_flagged
        _chaperone_flagged.discard(channel.id)
        for member in channel.members:
            await _unmute_member(
                member, f'{channel.name} no longer 1 adult + 1 child')
        if was_flagged:
            logger.info(
                'Voice channel %s cleared; chaperone mutes lifted',
                channel.name)
        return

    already_flagged = channel.id in _chaperone_flagged
    _chaperone_flagged.add(channel.id)

    for member in channel.members:
        if member.bot or (channel.guild.id, member.id) in _chaperone_muted:
            continue
        try:
            await member.edit(mute=True)
            _chaperone_muted.add((channel.guild.id, member.id))
            _save_chaperone_mutes()
            logger.info('Muted %s in channel %s', member.display_name, channel.name)
        except discord.HTTPException as e:
            logger.error('Failed to mute %s: %s', member.display_name, e)

    if already_flagged:
        # Same incident, still open — don't re-alert
        return

    logger.warning(
        'ALERT: One adult (%s) and one child (%s) detected in voice channel %s',
        adults[0].display_name,
        children[0].display_name,
        channel.name
    )

    try:
        moderators_channel = discord.utils.get(
            channel.guild.text_channels,
            name=MODERATORS_CHANNEL_NAME)

        if moderators_channel:
            alert_message = (
                f"🚨 **ALERT**: There is only one adult ({adults[0].mention}) and one child "
                f"({children[0].mention}) currently in {channel.mention}\n\n"
                f"All members in the channel have been muted for safety. "
                f"The mute lifts automatically once the channel is no longer "
                f"one adult and one child."
            )
            await moderators_channel.send(alert_message)
            logger.info('Alert sent to moderators channel for voice channel %s', channel.name)
        else:
            logger.error('Moderators channel "%s" not found', MODERATORS_CHANNEL_NAME)

    except discord.HTTPException as e:
        logger.error('Failed to send alert to moderators channel: %s', e)

# Message spam protection
# ---------------------------------------------------------------------------
# The two ways a raid cashes out once accounts are inside: the same scam
# link blasted across several channels at once, and mass pings to drive
# attention to it. Link detection is deliberately behavioural rather than
# a domain blocklist — scam domains burn within days, so a list is stale
# before it ships, while the cross-posting *pattern* is the same whatever
# domain is in the message.
#
# Keyed by (guild_id, user_id), pruned by window, key dropped when its
# deque empties — bounded without a cleanup job, same shape as
# _raid_join_times and _nuke_actions.
_link_posts = {}

# A key is only pruned when that same user posts another link, so a
# one-off poster's entry would otherwise linger for the process's
# lifetime. Sweeping once the dict crosses this size keeps that bounded
# without a scheduled job, and costs nothing on a quiet server.
_LINK_POSTS_SWEEP_AT = 1000

_URL_RE = re.compile(r'https?://\S+')

def _sweep_link_posts(now, window):
    """Drop tracking entries for users whose last link fell out of the
    window."""
    stale = [k for k, posts in _link_posts.items()
             if not posts or now - posts[-1][0] > window]
    for key in stale:
        del _link_posts[key]

def _normalize_for_spam(lowered):
    """Hash already-lowercased content with whitespace collapsed, so
    'FREE  nitro' and 'free nitro' land on the same key. The digest is
    what gets tracked, so message bodies are never retained in memory."""
    return hashlib.sha256(
        ' '.join(lowered.split()).encode('utf-8')).hexdigest()

def _spam_exempt(message):
    """Moderators are exempt, matching every other gate in this codebase."""
    return bool(getattr(
        getattr(message.author, 'guild_permissions', None),
        'manage_messages', False))

def _check_link_crosspost(message):
    """Track a link-bearing message; return the other copies if this one
    completes a cross-channel spam run, else None.

    on_message runs for every message in every channel, so this bails on a
    substring test before doing any regex or hashing, and only
    link-bearing messages are tracked at all. The case fold happens first
    because matching is case-insensitive — varying capitalization between
    posts is otherwise a one-keystroke evasion — and `check_message_for_autoreplies`
    already establishes one `.lower()` per message as an acceptable cost.
    """
    lowered = message.content.lower()
    if 'http' not in lowered or not _URL_RE.search(lowered):
        return None

    window = getattr(config, 'SPAM_CROSSPOST_WINDOW_SECONDS', 30)
    now = datetime.now(timezone.utc).timestamp()
    digest = _normalize_for_spam(lowered)
    key = (message.guild.id, message.author.id)

    if len(_link_posts) > _LINK_POSTS_SWEEP_AT:
        _sweep_link_posts(now, window)

    posts = _link_posts.setdefault(key, deque())
    while posts and now - posts[0][0] > window:
        posts.popleft()

    # Same text in the *same* channel is ordinary repetition; the
    # cross-channel spread is what marks a spam run.
    matches = [p for p in posts
               if p[1] == digest and p[2] != message.channel.id]
    posts.append((now, digest, message.channel.id, message.id))

    return matches or None

def _check_mention_spam(message):
    """True if one message pings too many distinct targets.

    Discord de-duplicates the mentions payload, so `@bob @bob @bob` counts
    once — the threshold is 3 distinct targets, which is the intent.
    """
    threshold = getattr(config, 'SPAM_MENTION_THRESHOLD', 3)
    total = len(getattr(message, 'mentions', ()))
    total += len(getattr(message, 'role_mentions', ()))
    return total >= threshold

async def _delete_spam_copies(message, copies):
    """Remove the offending message and any earlier tracked copies.

    Deleting only the message that tripped the detector would leave the
    first post standing in whatever channel it landed in.
    """
    deleted = 0
    try:
        await message.delete()
        deleted += 1
    except discord.HTTPException as e:
        logger.error('Spam protection: failed to delete message: %s', e)

    for _ts, _digest, channel_id, message_id in copies or ():
        channel = message.guild.get_channel(channel_id)
        if channel is None:
            continue
        try:
            await channel.get_partial_message(message_id).delete()
            deleted += 1
        except discord.HTTPException as e:
            logger.error('Spam protection: failed to delete copy in %s: %s',
                         channel_id, e)
    return deleted

async def _apply_spam_response(message, reason_text, copies=None,
                               kick_new_accounts=False):
    """Delete the offending message(s), punish the author, alert mods.

    With kick_new_accounts, an account younger than SPAM_NEW_ACCOUNT_DAYS
    is kicked rather than timed out — a throwaway made for the spam run
    gets removed, while an established member gets a reversible timeout.
    """
    member = message.author
    guild = message.guild

    deleted = await _delete_spam_copies(message, copies)

    minutes = getattr(config, 'SPAM_TIMEOUT_MINUTES', 10)
    new_account_days = getattr(config, 'SPAM_NEW_ACCOUNT_DAYS', 2)
    created_at = getattr(member, 'created_at', None)
    is_new = bool(
        kick_new_accounts and created_at is not None
        and created_at > datetime.now(timezone.utc)
        - timedelta(days=new_account_days))

    if is_new:
        try:
            await member.kick(reason=f'Spam protection: {reason_text}')
            action_note = (f'👢 Kicked **{member}** — account is less than '
                          f'{new_account_days} day(s) old.')
        except discord.Forbidden:
            action_note = ('⚠️ Could not kick them — missing permission or '
                          'rank. Act manually.')
        except discord.HTTPException as e:
            logger.error('Spam protection: kick failed for %s: %s', member, e)
            action_note = '⚠️ Failed to kick them — Discord API error.'
    else:
        try:
            until = discord.utils.utcnow() + timedelta(minutes=minutes)
            await member.timeout(until, reason=f'Spam protection: {reason_text}')
            action_note = f'🔇 Timed out **{member}** for {minutes} minute(s).'
        except discord.Forbidden:
            action_note = ('⚠️ Could not time them out — missing permission '
                          'or rank. Act manually.')
        except discord.HTTPException as e:
            logger.error('Spam protection: timeout failed for %s: %s',
                         member, e)
            action_note = '⚠️ Failed to time them out — Discord API error.'

    logger.warning('ALERT: spam protection triggered in %s by %s: %s',
                   guild.name, member, reason_text)

    moderators_channel = discord.utils.get(
        guild.text_channels, name=MODERATORS_CHANNEL_NAME)
    if not moderators_channel:
        logger.error('Moderators channel "%s" not found in %s',
                     MODERATORS_CHANNEL_NAME, guild.name)
        return
    excerpt = message.content[:200] or '[no text content]'
    try:
        await moderators_channel.send(
            f"🚨 **SPAM PROTECTION TRIGGERED**\n\n"
            f"{reason_text}\n{action_note}\n"
            f"🗑️ Deleted {deleted} message(s).\n"
            f"> {excerpt}")
    except (discord.HTTPException, discord.Forbidden) as e:
        logger.error('Failed to send spam alert to moderators: %s', e)

async def check_message_spam(message):
    """Run both spam detectors. True if the message was removed.

    Returning True tells on_message to stop — there is no sense
    autoreplying to a message that no longer exists.
    """
    if not getattr(config, 'SPAM_PROTECTION_ENABLED', True):
        return False
    if _spam_exempt(message):
        return False

    # Track the link first, unconditionally. Spam runs routinely pair a
    # link with mass pings, and responding to the mentions before this
    # ran would leave every such post untracked — so the cross-channel
    # copies would never accumulate, and if the timeout failed the run
    # would continue undetected as a cross-post.
    copies = _check_link_crosspost(message)

    if copies:
        # A confirmed cross-channel run outranks the mention count: it's
        # the stronger signal and carries the stronger response.
        channels = len({c[2] for c in copies}) + 1
        await _apply_spam_response(
            message,
            f'**{message.author}** posted the same link in **{channels}** '
            f'channels within '
            f'{getattr(config, "SPAM_CROSSPOST_WINDOW_SECONDS", 30)}s.',
            copies=copies, kick_new_accounts=True)
        return True

    if _check_mention_spam(message):
        total = len(getattr(message, 'mentions', ()))
        total += len(getattr(message, 'role_mentions', ()))
        await _apply_spam_response(
            message,
            f'**{message.author}** pinged **{total}** targets in one message '
            f'in {message.channel.mention}.',
            kick_new_accounts=True)
        return True

    return False

# Raid protection
# ---------------------------------------------------------------------------
# Timestamps of recent joins per guild, used only to detect a burst.
# Not persisted: a restart simply resets the window, same tradeoff as
# _chaperone_flagged. Config is read with getattr() everywhere here
# because config.py is gitignored and a deployed instance may predate
# these settings (see check_for_updates' config_changed handling).
_raid_join_times = {}

def _raid_lockdown_active(guild):
    """True if this guild currently has an unexpired invites pause.

    Reads Discord's own incident state (`invites_paused_until`) rather
    than tracking our own flag, so this is correct across restarts and
    also reflects a lockdown a moderator applied by hand in the UI.
    """
    until = getattr(guild, 'invites_paused_until', None)
    return until is not None and until > datetime.now(timezone.utc)

# Avatar clustering
# ---------------------------------------------------------------------------
# Raid accounts are bought or generated in bulk and reuse a small set of
# profile pictures, so grouping joiners by avatar separates a real raid
# ("12 of these 14 share one image") from an ordinary surge of arrivals.
#
# Hashes the image BYTES rather than Discord's avatar key. The key looked
# like a free shortcut, but whether two accounts uploading the identical
# image receive the same key is undocumented and could not be verified —
# building on that assumption risks a feature that silently finds
# nothing. The key is still used as a download cache key, so if it does
# turn out to be content-derived the work collapses for free.
_avatar_hash_cache = {}  # avatar key -> (timestamp, sha256 of the bytes)

# "Has an avatar we couldn't download", which is not the same thing as
# "has no avatar". Reporting a failed fetch as a bare default-avatar
# account would tell moderators something false about that account.
_AVATAR_UNREADABLE = object()

async def _hash_member_avatar(member):
    """SHA-256 of a member's avatar image.

    Returns None for a member with no custom avatar, or
    _AVATAR_UNREADABLE when they have one but it could not be fetched.
    """
    asset = getattr(member, 'avatar', None)
    if asset is None:
        return None

    key = getattr(asset, 'key', None)
    ttl = getattr(config, 'RAID_AVATAR_CACHE_TTL', 300)
    now = datetime.now(timezone.utc).timestamp()
    if key is not None:
        cached = _avatar_hash_cache.get(key)
        if cached and now - cached[0] <= ttl:
            return cached[1]

    try:
        raw = await asset.read()
    except (discord.HTTPException, discord.DiscordException) as e:
        # One unreadable avatar must never fail the whole batch.
        logger.warning('Avatar clustering: could not read avatar for %s: %s',
                       member, e)
        return _AVATAR_UNREADABLE

    digest = hashlib.sha256(raw).hexdigest()
    if key is not None:
        _avatar_hash_cache[key] = (now, digest)
    return digest

async def cluster_members_by_avatar(members):
    """Group members by identical avatar image.

    Returns a dict:
      clusters   - list of member lists sharing an image, largest first,
                   only groups of 2+
      no_avatar  - members with no custom avatar, counted but never
                   clustered: they share a Discord default derived from
                   their user id, so grouping them would be meaningless
                   (though a lot of them at once is its own raid signal).
                   Members whose avatar merely failed to download are not
                   in here — that would misreport them as picture-less.
      unreadable - count of members whose avatar could not be fetched,
                   reported separately so the summary never implies they
                   were checked
      scanned    - how many members were looked at
      skipped    - True when the batch exceeded RAID_AVATAR_SCAN_LIMIT
                   and no scan was run at all
    """
    empty = {'clusters': [], 'no_avatar': [], 'unreadable': 0,
             'scanned': 0, 'skipped': False}
    if not getattr(config, 'RAID_AVATAR_CLUSTERING_ENABLED', True):
        return empty

    limit = getattr(config, 'RAID_AVATAR_SCAN_LIMIT', 50)
    if len(members) > limit:
        return {**empty, 'skipped': True}

    # Bounded concurrency, matching the scraper pattern in commands.py.
    # Bytes are hashed and dropped inside each task, so at most this many
    # avatars are ever resident at once.
    sem = asyncio.Semaphore(5)

    async def _hash(member):
        async with sem:
            return member, await _hash_member_avatar(member)

    results = await asyncio.gather(
        *[_hash(m) for m in members], return_exceptions=True)

    by_digest = {}
    no_avatar = []
    unreadable = 0
    scanned = 0
    for result in results:
        if isinstance(result, BaseException):
            logger.warning('Avatar clustering: hashing failed: %s', result)
            continue
        member, digest = result
        scanned += 1
        if digest is _AVATAR_UNREADABLE:
            unreadable += 1
        elif digest is None:
            no_avatar.append(member)
        else:
            by_digest.setdefault(digest, []).append(member)

    if unreadable:
        logger.info('Avatar clustering: %d avatar(s) could not be checked',
                    unreadable)

    clusters = sorted(
        (group for group in by_digest.values() if len(group) > 1),
        key=len, reverse=True)
    return {'clusters': clusters, 'no_avatar': no_avatar,
            'unreadable': unreadable, 'scanned': scanned, 'skipped': False}

# Discord hard-rejects any message over 2000 characters, and this report
# is appended to text that already carries its own content. Budgeting it
# well under the cap keeps a big raid from turning the whole alert into
# an HTTPException — which would cost moderators the alert at exactly
# the wrong moment. Same reasoning as DASHBOARD_CHUNK_LIMIT.
_CLUSTER_REPORT_BUDGET = 1200
_CLUSTER_MAX_GROUPS = 5

def format_avatar_clusters(result):
    """Render a cluster_members_by_avatar() result as report text, or ''
    if there is nothing worth saying."""
    if result['skipped']:
        limit = getattr(config, 'RAID_AVATAR_SCAN_LIMIT', 50)
        return (f'\n\n🖼️ Avatar scan skipped — more than {limit} members to '
                f'check.')
    scanned = result['scanned']
    if not scanned:
        return ''

    clusters = result['clusters']
    no_avatar = result['no_avatar']
    unreadable = result['unreadable']
    # Only these were actually compared, so no line may imply anything
    # about the ones that failed to download or never had a picture.
    checked = scanned - unreadable
    with_avatar = checked - len(no_avatar)

    parts = []
    if clusters:
        matched = sum(len(group) for group in clusters)
        parts.append(
            f'🖼️ **{matched} of {with_avatar}** share only '
            f'**{len(clusters)}** distinct avatar(s) — bulk-created '
            f'accounts reuse profile pictures:')
        for index, group in enumerate(clusters[:_CLUSTER_MAX_GROUPS], start=1):
            names = ', '.join(str(m) for m in group[:10])
            if len(group) > 10:
                names += f' ... and {len(group) - 10} more'
            parts.append(f'• Group {index} ({len(group)}): {names}')
        if len(clusters) > _CLUSTER_MAX_GROUPS:
            parts.append(
                f'• ... and {len(clusters) - _CLUSTER_MAX_GROUPS} more group(s)')
    elif with_avatar:
        parts.append(f'🖼️ All {with_avatar} avatars are distinct.')
    if no_avatar:
        parts.append(
            f'👤 **{len(no_avatar)} of {checked}** have no avatar set.')
    if unreadable:
        parts.append(
            f'❓ {unreadable} avatar(s) could not be checked.')
    if not parts:
        return ''

    body = '\n'.join(parts)
    if len(body) > _CLUSTER_REPORT_BUDGET:
        body = body[:_CLUSTER_REPORT_BUDGET] + '\n… report truncated.'
    return '\n\n' + body

async def _avatar_cluster_report(members):
    """Cluster and format in one call, never raising.

    Used on the raid-alert path, where a clustering problem must not cost
    the moderators their alert.
    """
    try:
        return format_avatar_clusters(
            await cluster_members_by_avatar(members))
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Avatar clustering failed: %s', e)
        return ''

async def _apply_raid_lockdown(guild, join_count, window_seconds):
    """Pause invites and DMs (Discord's self-expiring incident actions)
    and alert moderators. No-ops if a lockdown is already active so a
    continuing burst doesn't re-alert on every join."""
    if _raid_lockdown_active(guild):
        return

    minutes = getattr(config, 'RAID_LOCKDOWN_MINUTES', 60)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)

    edited = True
    try:
        await guild.edit(
            invites_disabled_until=until,
            dms_disabled_until=until,
            reason=(f'Raid protection: {join_count} joins in '
                    f'{window_seconds}s'))
    except discord.Forbidden:
        edited = False
        logger.error(
            'Raid protection triggered in %s but the bot lacks '
            'Manage Server to pause invites/DMs', guild.name)
    except discord.HTTPException as e:
        edited = False
        logger.error('Raid protection: failed to edit guild %s: %s',
                     guild.name, e)

    logger.warning(
        'ALERT: raid protection triggered in %s (%d joins in %ds)',
        guild.name, join_count, window_seconds)

    moderators_channel = discord.utils.get(
        guild.text_channels, name=MODERATORS_CHANNEL_NAME)
    if not moderators_channel:
        logger.error('Moderators channel "%s" not found in %s',
                     MODERATORS_CHANNEL_NAME, guild.name)
        return

    if edited:
        body = (
            f"🚨 **RAID PROTECTION TRIGGERED**\n\n"
            f"**{join_count}** members joined within **{window_seconds}s**. "
            f"Invites and DMs between members have been paused for "
            f"**{minutes} minute(s)** (auto-lifts, or run "
            f"`/raid lockdown enabled:False` to lift early).\n\n"
            f"Use `/raid recent_joins` to review who joined, or "
            f"`/raid kick_recent` to remove suspicious new accounts.")
    else:
        body = (
            f"🚨 **RAID DETECTED** ({join_count} joins in "
            f"{window_seconds}s) — **could not pause invites/DMs**, "
            f"the bot needs the **Manage Server** permission. "
            f"Review with `/raid recent_joins` and act manually.")

    try:
        await moderators_channel.send(body)
    except (discord.HTTPException, discord.Forbidden) as e:
        logger.error('Failed to send raid alert to moderators: %s', e)

    # Runs only now, after both the lockdown and the alert: this
    # downloads avatars, and neither the protective action nor the
    # moderators' notification should ever wait on network calls. Sent as
    # its own message so a long cluster list can't push the alert itself
    # over Discord's 2000-character limit.
    window_start = datetime.now(timezone.utc) - timedelta(
        seconds=window_seconds)
    recent = [m for m in guild.members
              if m.joined_at and m.joined_at >= window_start]
    report = await _avatar_cluster_report(recent)
    if report.strip():
        try:
            await moderators_channel.send(report.strip())
        except (discord.HTTPException, discord.Forbidden) as e:
            logger.error('Failed to send avatar cluster report: %s', e)

@bot.event
async def on_member_join(member):
    """Track join bursts and trigger raid protection past the threshold.

    Unlike the voice chaperone and DM-kick gates, bot accounts are not
    exempted here — bulk-joining bot accounts are a common raid vector.
    """
    if not getattr(config, 'RAID_PROTECTION_ENABLED', True):
        return

    guild = member.guild
    window = getattr(config, 'RAID_JOIN_WINDOW_SECONDS', 30)
    threshold = getattr(config, 'RAID_JOIN_THRESHOLD', 6)

    now = datetime.now(timezone.utc).timestamp()
    times = _raid_join_times.setdefault(guild.id, deque())
    times.append(now)
    while times and now - times[0] > window:
        times.popleft()

    if len(times) >= threshold:
        await _apply_raid_lockdown(guild, len(times), window)

# Anti-nuke protection
# ---------------------------------------------------------------------------
# Defends against a compromised moderator/admin account (or a rogue
# integration) acting straight against the Discord API — raid protection
# above only sees join events, not this. Driven by the audit-log gateway
# event rather than polling, so it fires in near-real-time per entry.
# State is in-memory only, same tradeoff as _raid_join_times: a restart
# resets the window, which is acceptable for a burst detector.
_nuke_actions = {}  # (guild_id, actor_id) -> deque[float] of action times

# Destructive actions counted toward the burst threshold. Permission
# escalation (role_update / member_role_update granting a dangerous
# permission) is checked separately below, on every occurrence, since one
# occurrence of that is already the attack.
_NUKE_BURST_ACTIONS = {
    discord.AuditLogAction.channel_delete,
    discord.AuditLogAction.role_delete,
    discord.AuditLogAction.kick,
    discord.AuditLogAction.ban,
    discord.AuditLogAction.webhook_create,
}

# A prune can remove hundreds of members in ONE audit-log entry, so it
# never accumulates toward a burst threshold — it fires on its own.
_NUKE_SINGLE_ACTIONS = {discord.AuditLogAction.member_prune}

_NUKE_DANGEROUS_PERMS = (
    'administrator', 'manage_guild', 'manage_roles',
    'manage_channels', 'ban_members', 'kick_members',
)

def _check_permission_escalation(entry, actor_perms=None):
    """Detect a dangerous permission being granted.

    Returns (description, privileged_roles_added) — the roles list is
    populated for member_role_update so the caller can undo the grant —
    or None when nothing escalated.

    Covers the two ways a role ends up with real power: editing the
    role's own permission bits (role_update), or assigning a member a
    role that already carries one (member_role_update).

    `actor_perms` suppresses the *routine administration* case: granting
    a permission the actor already holds is not an escalation, because
    you cannot escalate to something you already have. A real admin
    setting up a Moderator role with kick_members is normal work and must
    not lock them out of their own server. The classic attack — a
    manage_roles holder granting administrator to themselves or an alt —
    still fires, because the actor lacked administrator. An actor who
    already has administrator and turns destructive is caught by the
    burst path instead.
    """
    def _is_escalation(perm_names):
        if actor_perms is None:
            return list(perm_names)
        return [p for p in perm_names if not getattr(actor_perms, p, False)]

    if entry.action is discord.AuditLogAction.role_update:
        after_perms = getattr(entry.after, 'permissions', None)
        if after_perms is None:
            return None
        before_perms = getattr(entry.before, 'permissions', None)
        before_set = {p for p, v in before_perms if v} if before_perms else set()
        granted = _is_escalation(
            p for p, v in after_perms
            if v and p in _NUKE_DANGEROUS_PERMS and p not in before_set)
        if granted:
            return (f"role **{entry.target}** was granted: "
                   f"{', '.join(sorted(granted))}", [])
        return None

    if entry.action is discord.AuditLogAction.member_role_update:
        for role in getattr(entry.after, 'roles', []):
            perms = getattr(role, 'permissions', None)
            if perms is None:
                continue
            granted = _is_escalation(
                p for p in _NUKE_DANGEROUS_PERMS if getattr(perms, p, False))
            if granted:
                return (f"**{entry.target}** was assigned role "
                       f"**{role}**, which carries: {', '.join(granted)}",
                       [role])
        return None

    return None

def _resolve_role(guild, target):
    """A live Role for an audit-log target, or None.

    Audit-log targets fall back to a bare `discord.Object` when the role
    isn't cached, and Object carries no behaviour — look it up by id.
    """
    if isinstance(target, discord.Role):
        return target
    role_id = getattr(target, 'id', None)
    return guild.get_role(role_id) if role_id is not None else None

async def _revert_escalation(entry, privileged_roles):
    """Undo the permission grant itself, not just neutralize the actor.

    Stripping the actor's roles leaves the payoff standing — the alt keeps
    the administrator role, or the edited role keeps the permission and
    everyone holding it keeps the power. Returns a note for the alert.
    """
    try:
        if entry.action is discord.AuditLogAction.role_update:
            before_perms = getattr(entry.before, 'permissions', None)
            if before_perms is None:
                return ''
            # entry.target is a discord.Object, not a Role, whenever the
            # role isn't cached — which is exactly the realistic attack
            # (a role the attacker created seconds earlier). Object has
            # no .edit(), so resolve it against the guild first.
            role = _resolve_role(entry.guild, entry.target)
            if role is None:
                return ('\n⚠️ Could not revert the permission change — the '
                        'role is no longer resolvable. Revert it manually.')
            await role.edit(
                permissions=before_perms,
                reason='Anti-nuke: reverting permission escalation')
            return f"\n↩️ Reverted **{role}**'s permissions."

        if entry.action is discord.AuditLogAction.member_role_update:
            for role in privileged_roles:
                await entry.target.remove_roles(
                    role, reason='Anti-nuke: reverting permission escalation')
            names = ', '.join(str(r) for r in privileged_roles)
            return f"\n↩️ Removed **{names}** from **{entry.target}**."
    except discord.Forbidden:
        return ('\n⚠️ Could not revert the permission change — I lack the '
                'permission or rank to do so. Revert it manually.')
    except discord.HTTPException as e:
        logger.error('Anti-nuke: failed to revert escalation: %s', e)
        return '\n⚠️ Failed to revert the permission change — API error.'
    except AttributeError as e:
        # Belt-and-braces: an un-resolvable audit-log target must never
        # take the event handler down mid-incident.
        logger.error('Anti-nuke: un-revertable escalation target: %s', e)
        return '\n⚠️ Could not revert the permission change automatically.'
    return ''

async def _apply_anti_nuke_response(guild, actor_id, reason_text, extra_note=''):
    """Strip the actor's roles (unless configured to alert-only or the
    actor is the guild owner) and alert moderators.

    The strip happens before the alert is sent — response time matters
    more here than narration order.

    Returns True if the actor was actually neutralized. Callers use this
    to decide whether to reset their burst counter: when the strip failed
    (the bot is ranked below them — precisely when the attacker is most
    dangerous) the actor still has full permissions, and resetting would
    mean their continued destruction goes quiet until a whole fresh
    window's worth of actions accumulates again.
    """
    stripped_note = ''
    neutralized = False
    action = getattr(config, 'ANTI_NUKE_ACTION', 'strip_roles')

    if action == 'strip_roles' and actor_id != guild.owner_id:
        member = guild.get_member(actor_id)
        if member is None:
            try:
                member = await guild.fetch_member(actor_id)
            except discord.NotFound:
                member = None
            except discord.HTTPException as e:
                logger.error('Anti-nuke: failed to fetch actor %s: %s',
                             actor_id, e)
                member = None
        if member is not None:
            try:
                await member.edit(roles=[],
                                  reason='Anti-nuke: destructive activity detected')
                stripped_note = f'\n🔒 Stripped all roles from {member.mention}.'
                neutralized = True
                logger.warning('Anti-nuke: stripped roles from %s in %s',
                               member, guild.name)
            except discord.Forbidden:
                stripped_note = (
                    '\n⚠️ Could not strip roles — I\'m ranked at or below '
                    'them in the role hierarchy. Act manually.')
            except discord.HTTPException as e:
                logger.error('Anti-nuke: failed to strip roles for %s: %s',
                             member, e)
                stripped_note = '\n⚠️ Failed to strip roles — Discord API error.'
    elif action == 'strip_roles' and actor_id == guild.owner_id:
        stripped_note = '\nℹ️ Actor is the guild owner — no action taken.'

    logger.warning('ALERT: anti-nuke triggered in %s: %s',
                   guild.name, reason_text)

    moderators_channel = discord.utils.get(
        guild.text_channels, name=MODERATORS_CHANNEL_NAME)
    if not moderators_channel:
        logger.error('Moderators channel "%s" not found in %s',
                     MODERATORS_CHANNEL_NAME, guild.name)
        return neutralized
    try:
        await moderators_channel.send(
            f"🚨 **ANTI-NUKE TRIGGERED**\n\n"
            f"{reason_text}{extra_note}{stripped_note}")
    except (discord.HTTPException, discord.Forbidden) as e:
        logger.error('Failed to send anti-nuke alert to moderators: %s', e)
    return neutralized

@bot.event
async def on_audit_log_entry_create(entry):
    """Watch the audit log for a destructive spree or a permission grant.

    entry.user_id is the actor Discord's own token attributed the action
    to. Every destructive command JohnnyBot itself runs (kick, purge,
    server_restore, /raid kick_recent, ...) shows up here with the BOT's
    user as actor, not the invoking moderator — so the bot's own actions
    must never be counted, or its own moderation commands would trip
    anti-nuke on themselves.
    """
    if not getattr(config, 'ANTI_NUKE_ENABLED', True):
        return
    if entry.user_id is None or entry.user_id == bot.user.id:
        return

    guild = entry.guild
    actor = guild.get_member(entry.user_id)

    escalation = _check_permission_escalation(
        entry, getattr(actor, 'guild_permissions', None))
    if escalation:
        description, privileged_roles = escalation
        revert_note = await _revert_escalation(entry, privileged_roles)
        await _apply_anti_nuke_response(
            guild, entry.user_id,
            f"**{entry.user}** — permission escalation: {description}",
            revert_note)
        return

    if entry.action not in _NUKE_BURST_ACTIONS | _NUKE_SINGLE_ACTIONS:
        return

    threshold = getattr(config, 'ANTI_NUKE_THRESHOLD', 3)
    window = getattr(config, 'ANTI_NUKE_WINDOW_SECONDS', 60)

    now = datetime.now(timezone.utc).timestamp()
    key = (guild.id, entry.user_id)
    times = _nuke_actions.setdefault(key, deque())
    already_responded = bool(times) and entry.action in _NUKE_SINGLE_ACTIONS
    times.append(now)
    while times and now - times[0] > window:
        times.popleft()

    if entry.action in _NUKE_SINGLE_ACTIONS:
        # Fires on its own rather than accumulating: one prune entry can
        # remove hundreds of members. The shared deque still dedups a
        # rapid series of them into a single response.
        if already_responded:
            return
        removed = getattr(getattr(entry, 'extra', None), 'members_removed', None)
        neutralized = await _apply_anti_nuke_response(
            guild, entry.user_id,
            f"**{entry.user}** ran a member prune"
            + (f", removing **{removed}** member(s)." if removed else "."))
    elif len(times) >= threshold:
        neutralized = await _apply_anti_nuke_response(
            guild, entry.user_id,
            f"**{entry.user}** performed **{len(times)}** destructive "
            f"action(s) in {window}s (latest: `{entry.action.name}`).")
    else:
        return

    # Only reset the counter once the actor is actually depowered. If the
    # strip failed they still hold their permissions, and clearing here
    # would silence their continued destruction until a whole fresh
    # window's worth of actions piled up again.
    if neutralized:
        times.clear()

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes - monitor for adult/child combinations."""
    if member.bot:
        return

    # Read VOICE_CHAPERONE_ENABLED from the config module each call so
    # runtime toggles via /voice_chaperone take effect immediately.
    if not config.VOICE_CHAPERONE_ENABLED:
        # Turning the feature off shouldn't strand anyone muted
        await _unmute_member(member, 'voice chaperone disabled')
        return

    channels_to_check = set()

    if before.channel:
        channels_to_check.add(before.channel)

    if after.channel:
        channels_to_check.add(after.channel)

    for channel in channels_to_check:
        await check_voice_channel_safety(channel)

    # Someone who walked out of a flagged channel (or rejoined voice
    # still carrying our mute) gets released here — check_voice_channel_safety
    # only sees the members currently in the channel it inspects.
    if after.channel and after.channel.id not in _chaperone_flagged:
        await _unmute_member(member, 'left flagged voice channel')

from commands import (  # pylint: disable=wrong-import-position
    setup_commands,
    check_message_for_autoreplies as _check_autoreplies,
    was_recently_dmed as _was_recently_dmed,
    _atomic_json_write,
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

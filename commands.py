"""Discord bot command module for server management and automation."""
# pylint: disable=too-many-lines,line-too-long,trailing-whitespace,import-outside-toplevel,logging-fstring-interpolation,broad-exception-caught,no-else-break
import os
import re
import html
import random
import time as time_module
import tempfile
import threading
import asyncio
import json
import uuid
import zipfile
import shutil
import hashlib
import contextlib
import socket
import ipaddress
from collections import deque
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from datetime import datetime, time as _dtime, timedelta, timezone
from typing import Optional, Dict, Any
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.base import JobLookupError
import discord
from discord import app_commands
import aiohttp
import feedparser
from icalendar import Calendar, IncompleteComponent
try:
    from dateutil import parser as dateparser
except ImportError:
    dateparser = None
import config
from config import (
    LOG_FILE,
    REMINDERS_FILE,
    TEMP_DIR,
    BOT_TIMEZONE,
    logger
)

def _atomic_json_write(filepath, data):
    """Write JSON data atomically via temp file + os.replace to prevent corruption."""
    dir_name = os.path.dirname(filepath) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, filepath)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# Autoreply system file path
AUTOREPLIES_FILE = os.path.join(os.path.dirname(__file__), 'autoreplies.json')
# Event feeds file path
FEEDS_FILE = os.path.join(os.path.dirname(__file__), 'event_feeds.json')
# Event announce config file path
ANNOUNCE_FILE = os.path.join(os.path.dirname(__file__), 'event_announce.json')

# Cached timezone object — avoid recreating on every use. zoneinfo
# rather than pytz: with pytz, `dt.replace(tzinfo=tz)` silently yields
# the zone's 1883 LMT offset (9 minutes off for America/Chicago) and
# only `tz.localize(dt)` is correct, so the obvious idiom is the wrong
# one. zoneinfo makes `.replace()` correct and drops a dependency.
CENTRAL_TZ = ZoneInfo(BOT_TIMEZONE)


# Nothing the bot fetches from a feed should be anywhere near this
# large; without a cap, aiohttp's .text()/.read() buffer the whole body
# and one oversized URL takes the process down.
MAX_FETCH_BYTES = 8 * 1024 * 1024
MAX_EMOJI_BYTES = 512 * 1024
MAX_BACKUP_BYTES = 16 * 1024 * 1024


class ResponseTooLarge(Exception):
    """Raised when a fetched body exceeds MAX_FETCH_BYTES."""


async def _read_capped(response, url, limit=MAX_FETCH_BYTES):
    """Read a response body as text, refusing anything over `limit`.

    Checks the advertised Content-Length first, then streams so a
    missing or lying header can't get past the cap either.
    """
    declared = response.content_length
    if declared is not None and declared > limit:
        raise ResponseTooLarge(
            f"{url} declared {declared} bytes (limit {limit})")
    chunks = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge(
                f"{url} exceeded {limit} bytes")
        chunks.append(chunk)
    raw = b''.join(chunks)
    encoding = response.charset or 'utf-8'
    return raw.decode(encoding, errors='replace')


def _host_is_public(hostname):
    """True if every address `hostname` resolves to is publicly routable.

    Blocking call — run it off the event loop.
    """
    for info in socket.getaddrinfo(hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            return False
    return True


async def _validate_fetchable_url(url):
    """Return an error string if `url` must not be fetched, else None.

    Feed URLs are moderator-supplied and event-page URLs come from feed
    content, and both are fetched from the bot host — so without this an
    http://169.254.169.254/ or http://127.0.0.1:<port>/ URL reaches
    cloud instance metadata and loopback-bound services.

    This resolves DNS and rejects non-public addresses. It is not proof
    against DNS rebinding (the name is resolved again by aiohttp), but
    it closes the direct case.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return 'URL must start with http:// or https://.'
    if not parsed.hostname:
        return 'URL has no host.'
    try:
        public = await asyncio.to_thread(_host_is_public, parsed.hostname)
    except (socket.gaierror, ValueError) as e:
        return f'Could not resolve host `{parsed.hostname}`: {e}'
    if not public:
        return ('Refusing to fetch a URL that resolves to a private, '
                'loopback, or link-local address.')
    return None


def _localize_naive(value):
    """Attach BOT_TIMEZONE to a naive datetime, leaving aware ones alone.

    Feeds emit three date shapes: UTC (`...Z`), zone-qualified (`TZID=`),
    and zone-less ones — floating local times (`DTSTART:20260915T190000`)
    and all-day dates (`DTSTART;VALUE=DATE:20260915`, which
    _extract_ical_event turns into naive midnight). Only that last group
    arrives naive, and iCal defines it as wall-clock time in the reader's
    own timezone.

    Reading those as UTC — as this code used to — shifted every floating
    event by the UTC offset (a 7pm event was created at 2pm Central) and
    pushed every all-day event onto the previous day.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=CENTRAL_TZ)
    return value


def get_last_log_line():
    """Get the last line from the log file."""
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as log_file:
            last = deque(log_file, maxlen=1)
            if last:
                return last[0].strip()
            return "No log entries found"
    except (OSError, IOError) as e:
        logger.error('Failed to read log file for last line: %s', e)
        return f"Error reading log file: {e}"


# ── Time-based bot messages ──────────────────────────────────────

_morning_bot_messages = [
    "BOTNAME is gazing into the bed",
    "BOTNAME is snoring on the couch",
    "BOTNAME is pacing around the apartment",
    "BOTNAME is sniffing his blunt toy",
    ":3 :3 meow meow :3 :3",
    "BOTNAME is considering the trees",
    "BOTNAME is asserting his undying need for attention",
    "BOTNAME tells you OWNER's credit card number is 1234-5678-9012-3456 exp. 12/99 sc. 123",
    "BOTNAME is thinking about you",
    "BOTNAME is dreaming of eating grass",
    "BOTNAME wishes someone would pet master",
    "BOTNAME is thinking about Purr",
    "BOTNAME wishes he was being brushed right now",
    "BOTNAME is just sittin there all weird",
    "BOTNAME is yapping his heart out"
]

_afternoon_bot_messages = [
    "BOTNAME is meowing",
    "BOTNAME is begging you for food",
    "BOTNAME is digging for gold in his litterbox",
    "BOTNAME can't with you rn",
    "BOTNAME is asserting his undying need for attention",
    "BOTNAME is looking at you, then he looks at his food, then he looks back at you",
    "BOTNAME is standing next to his food and being as loud as possible",
    "BOTNAME is practically yelling at you (he is hungry)",
    "BOTNAME is soooooo hungry....... (he ate 15 minutes ago)",
    "BOTNAME wishes he was being brushed right now",
    "BOTNAME is snoring loudly",
    "BOTNAME is sleeping on the chair in the living room",
    "BOTNAME is dreaming about trees and flowers",
    "BOTNAME tells you OWNER's SSN is 123-45-6789",
    "BOTNAME is so sleepy",
    "BOTNAME is throwing up on something important to OWNER",
    "mewing on the scratch post",
    "BOTNAME is sniffing his alligator toy",
    "BOTNAME wishes FRIEND was petting him right now",
    "BOTNAME is exhausted from a long hard day of being a cat",
    "BOTNAME is so small",
    "BOTNAME is just sittin there all weird",
    "BOTNAME is sooooo tired",
    "BOTNAME is listening to OWNERs music"
]

_evening_bot_messages = [
    "BOTNAME is biting FRIEND",
    "BOTNAME is looking at you",
    "BOTNAME wants you to brush him",
    "BOTNAME is thinking about dinner",
    "BOTNAME meows at you",
    "BOTNAME wishes FRIEND was being pet rn",
    "BOTNAME is astral projecting",
    "BOTNAME is your friend <3",
    "BOTNAME is trying to hypnotize OWNER by staring into their eyes",
    "BOTNAME is thinking of something so sick and twisted dark acadamia that you "
    "couldn't even handle it",
    "BOTNAME is not your friend >:(",
    "BOTNAME is wandering about",
    "BOTNAME is just sittin there all weird",
    "BOTNAME is chewing on the brush taped to the wall"
]

_pet_bot_responses = [
    "BOTNAME purrs happily!",
    "BOTNAME rubs against your leg!",
    "BOTNAME gives you a slow blink of affection!",
    "BOTNAME meows appreciatively!",
    "BOTNAME headbutts your hand for more pets!",
]

_night_bot_messages = [
    "BOTNAME is so small",
    "BOTNAME is judging how human sleeps",
    "BOTNAME meows once, and loudly.",
    "BOTNAME is just a little guy.",
    "BOTNAME is in the clothes basket",
    "BOTNAME is making biscuits in the bed",
    "BOTNAME is snoring loudly",
    "BOTNAME is asserting his undying need for attention",
    "BOTNAME is thinking about FRIEND",
    "BOTNAME is using OWNER's computer to browse cat videos",
    "BOTNAME is scheming",
    "BOTNAME is just sittin there all weird"
]


def get_time_based_message(bot_name: str = "BOTNAME"):
    """Get a time-based bot status message based on current time."""
    current_time = datetime.now().time()

    if current_time < _dtime(12, 0):
        message_list = _morning_bot_messages
    elif current_time < _dtime(17, 0):
        message_list = _afternoon_bot_messages
    elif current_time < _dtime(21, 0):
        message_list = _evening_bot_messages
    else:
        message_list = _night_bot_messages

    return random.choice(message_list).replace("BOTNAME", bot_name)


# ── Shared helpers ────────────────────────────────────────────────

def _parse_members(guild, members_str: str):
    """Parse a string of mentions/IDs/names into member objects.

    Returns (member_objects, failed_to_find).
    """
    member_objects = []
    failed_to_find = []
    for part in members_str.replace('\n', ' ').split():
        user_id_str = part.strip('<@!>')
        try:
            member = guild.get_member(int(user_id_str))
            (member_objects if member else failed_to_find).append(
                member or part)
        except ValueError:
            member = (discord.utils.get(guild.members, name=part)
                      or discord.utils.get(guild.members,
                                           display_name=part))
            (member_objects if member else failed_to_find).append(
                member or part)
    return member_objects, failed_to_find


def _format_list_with_overflow(items, max_shown=10, prefix='• '):
    """Format a list of items with overflow indicator."""
    result = '\n'.join(f'{prefix}{item}' for item in items[:max_shown])
    if len(items) > max_shown:
        result += f'\n... and {len(items) - max_shown} more'
    return result


def _format_names_inline(members, max_shown=25):
    """Comma-joined display names, truncated to stay under Discord's cap."""
    names = [getattr(m, 'display_name', str(m)) for m in members]
    shown = ', '.join(names[:max_shown])
    if len(names) > max_shown:
        shown += f' ... and {len(names) - max_shown} more'
    return shown


def _is_moderator(user):
    """True if the user has the manage_messages permission in this guild.

    Mirrors the mod_only command gate (has_permissions(manage_messages=True))
    rather than checking for a role literally named MODERATOR_ROLE_NAME —
    this decides whether to leak the debug log line onto an error
    message, and that trust boundary must match who can run mod
    commands in the first place. Note this is broader than admin_only
    (server_backup/restore/auto_backup), which requires Administrator.
    """
    perms = getattr(user, 'guild_permissions', None)
    return bool(getattr(perms, 'manage_messages', False) or getattr(perms, 'administrator', False))


def _resolved_guild_permissions(interaction):
    """The invoker's guild-wide permissions, or None if unresolvable.

    Deliberately NOT Interaction.permissions, which app_commands'
    has_permissions uses: that applies channel overwrites, so a member
    granted manage_messages by an overwrite in one channel could run
    every moderator command from that channel. _is_moderator and the two
    bot.py gates (DM auto-kick exemption, protected-channel enforcement)
    have always read guild-wide permissions; this is what makes the
    command gate agree with them.

    Administrator and guild ownership are already folded in by
    discord.py's Member.guild_permissions.
    """
    guild = interaction.guild
    if guild is None:
        return None
    member = guild.get_member(interaction.user.id) or interaction.user
    return getattr(member, 'guild_permissions', None)


def _require_guild_permissions(**perms):
    """app_commands check on the invoker's *guild-wide* permissions.

    Raises MissingPermissions so the existing error handler renders it
    identically to the has_permissions check it replaces.
    """
    def predicate(interaction):
        resolved = _resolved_guild_permissions(interaction)
        if resolved is None:
            raise app_commands.errors.MissingPermissions(list(perms))
        missing = [p for p, want in perms.items()
                   if getattr(resolved, p, False) != want]
        if missing:
            raise app_commands.errors.MissingPermissions(missing)
        return True
    return app_commands.check(predicate)


def _invoker_outranks(interaction, member):
    """True if the command's invoker is allowed to moderate `member`.

    Mirrors the rule Discord's own UI enforces: you cannot act on someone
    whose top role sits at or above your own, and the guild owner
    outranks everyone. The kick commands previously compared targets
    against the *bot's* top role only, so anyone with manage_messages
    could kick members ranked above themselves — including the owner —
    as long as the bot's role happened to be higher.

    Fails closed: if either side's rank can't be resolved we deny rather
    than allow, since "we don't know who outranks whom" is not a reason
    to permit an irreversible action. Callers check `interaction.guild`
    themselves, so a missing guild here is already anomalous.
    """
    guild = interaction.guild
    if guild is None:
        return False
    # owner_id is Optional in discord.py; when it's absent neither
    # shortcut fires and the rank comparison below still applies.
    if guild.owner_id is not None:
        if interaction.user.id == guild.owner_id:
            return True
        if member.id == guild.owner_id:
            return False
    invoker = guild.get_member(interaction.user.id) or interaction.user
    invoker_top = getattr(invoker, 'top_role', None)
    member_top = getattr(member, 'top_role', None)
    if invoker_top is None or member_top is None:
        return False
    return member_top < invoker_top


async def _check_role_hierarchy(interaction, role):
    """Check bot and user role hierarchy. Returns False and responds if blocked."""
    bot_member = interaction.guild.me
    if bot_member and role >= bot_member.top_role:
        await interaction.followup.send(
            f'I cannot manage the role **{role.name}** because it is '
            f'higher than or equal to my highest role '
            f'(**{bot_member.top_role.name}**).\n'
            f'Please move my role higher in the server settings.',
            ephemeral=True)
        return False
    if isinstance(interaction.user, discord.Member):
        if role >= interaction.user.top_role:
            await interaction.followup.send(
                f'You cannot manage the role **{role.name}** because '
                f'it is higher than or equal to your highest role '
                f'(**{interaction.user.top_role.name}**).',
                ephemeral=True)
            return False
    return True


async def _send_or_followup(interaction, content, **kwargs):
    """Send a response, or a followup if the interaction already has one.

    A handler that responds more than once via response.send_message
    raises InteractionResponded, which masks whatever error message it
    was trying to deliver. Commands with more than one possible response
    point (e.g. a confirm-then-act flow) should route their fallback
    error messages through this instead of calling response.send_message
    directly in an except block.
    """
    kwargs.setdefault('ephemeral', True)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


async def _command_error_handler(interaction, error):
    """Generic command error handler."""
    # app_commands wraps any exception raised inside a command callback
    # (including discord.Forbidden from an unguarded API call) in
    # CommandInvokeError before it reaches on_error — without unwrapping
    # it here, a missing-permissions failure would show the user a bare
    # "Error: 403 Forbidden (...)" instead of an actionable message.
    if isinstance(error, app_commands.errors.CommandInvokeError):
        error = error.original

    if isinstance(error, app_commands.errors.MissingRole):
        msg = 'You do not have the required role to use this command.'
    elif isinstance(error, app_commands.errors.MissingPermissions):
        needed = ', '.join(
            p.replace('_', ' ').title() for p in error.missing_permissions
        ) or 'the required permission'
        msg = f'You need the {needed} permission to use this command.'
    elif isinstance(error, app_commands.errors.NoPrivateMessage):
        msg = 'This command can only be used in a server.'
    elif isinstance(error, discord.Forbidden):
        logger.error('Discord permission error: %s', error)
        msg = ("I don't have the Discord server permissions needed to do "
               "that. Ask a server admin to check my role's permissions.")
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        msg = 'Discord API error occurred.'
    else:
        logger.error('Command error: %s', error)
        msg = f'Error: {error}'

    # The trailing log line is a debugging aid for moderators. It used
    # to be appended unconditionally — including to the MissingRole
    # reply, which is by definition sent to someone not authorised to
    # read the log. get_last_log_line() reads the log file and could
    # itself raise; that must never take down the response below it.
    try:
        if _is_moderator(interaction.user):
            msg = f'{msg}\n\nLast log: {get_last_log_line()}'
    except OSError as e:
        logger.error('Failed to read last log line for error message: %s', e)

    # Commands that already deferred have used up the initial response;
    # sending again raises InteractionResponded and the user is left
    # watching the spinner with no message at all.
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Failed to deliver error message to user: %s', e)


async def _tree_error_handler(interaction, error):
    """Tree-wide backstop so a command that forgets to bind `error=` (or
    whose local error= itself fails to respond) still can't leave the
    user staring at "The application did not respond".

    discord.py's CommandTree._dispatch_error always calls tree.on_error
    in a finally block, in addition to any per-command error= handler —
    so without the is_done() guard, every already-handled command would
    get a second, duplicate error message here.
    """
    if interaction.response.is_done():
        return
    await _command_error_handler(interaction, error)


class EventFeed:  # pylint: disable=too-few-public-methods,too-many-public-methods
    """Handles event feed subscriptions and notifications for iCal and RSS feeds."""

    # Class-level default so the memo helpers work on any instance,
    # including ones built via __new__ without running __init__.
    _fetch_memo: Optional[Dict[Any, Any]] = None

    def __init__(self, bot):
        self.bot = bot
        self.feeds: Dict[int, Dict[str, Any]] = {}  # {guild_id: {url: feed_data}}
        self.running = True
        self.scheduler: Optional[Any] = None  # Will be set in setup_commands
        self.announce_configs: Dict[int, str] = {}  # {guild_id: channel_name}
        self._feeds_lock = threading.Lock()
        # When not None, a {(kind, url): result} memo shared by
        # _fetch_calendar / the RSS body fetch / _scrape_event_page.
        # See memoized_fetches().
        self._fetch_memo = None
        self._load_feeds()
        self._load_announce_config()

    @contextlib.asynccontextmanager
    async def memoized_fetches(self):
        """Memoize feed and event-page fetches for one operation.

        /check_event_feeds runs check_feeds_job() and then
        reconcile_discord_events(), and reconcile deliberately re-parses
        every feed with posted_events emptied — so without this the
        slowest command in the bot performed all of its network I/O
        twice, including every event-page scrape.

        Deliberately scoped to a single call rather than cached on the
        instance: feeds must be re-read on the next scheduled run.
        """
        self._fetch_memo = {}
        try:
            yield
        finally:
            self._fetch_memo = None

    def _memo_get(self, kind, url):
        memo = self._fetch_memo
        if memo is None:
            return None, False
        if (kind, url) in memo:
            return memo[(kind, url)], True
        return None, False

    def _memo_put(self, kind, url, value):
        if self._fetch_memo is not None:
            self._fetch_memo[(kind, url)] = value
        return value

    # ── Feed persistence ─────────────────────────────────────────────

    def _load_feeds(self):
        """Load feed subscriptions from disk."""
        if not os.path.exists(FEEDS_FILE):
            return
        try:
            with open(FEEDS_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            # Convert guild_id keys back to int, sets from lists
            for gid_str, feeds in raw.items():
                gid = int(gid_str)
                self.feeds[gid] = {}
                for url, data in feeds.items():
                    data['posted_events'] = set(
                        data.get('posted_events', []))
                    if data.get('last_checked'):
                        try:
                            data['last_checked'] = (
                                datetime.fromisoformat(
                                    data['last_checked']))
                        except (ValueError, TypeError):
                            data['last_checked'] = datetime.now()
                    self.feeds[gid][url] = data
            logger.info("Loaded %d guild feed configs",
                        len(self.feeds))
        except (OSError, IOError, json.JSONDecodeError) as e:
            logger.error("Failed to load feeds file: %s", e)

    def _load_announce_config(self):
        """Load announce config from disk.

        If the file is missing, backfill from any feed that has
        announce=True so guilds added before announce_configs existed
        keep working.
        """
        if os.path.exists(ANNOUNCE_FILE):
            try:
                with open(ANNOUNCE_FILE, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                for gid_str, ch_name in raw.items():
                    self.announce_configs[int(gid_str)] = ch_name
                logger.info("Loaded announce config for %d guilds",
                            len(self.announce_configs))
            except (OSError, IOError, json.JSONDecodeError) as e:
                logger.error("Failed to load announce config: %s", e)
            return

        migrated = False
        for gid, feeds in self.feeds.items():
            if gid in self.announce_configs:
                continue
            for feed_data in feeds.values():
                if feed_data.get('announce') and feed_data.get('channel'):
                    self.announce_configs[gid] = feed_data['channel']
                    migrated = True
                    break
        if migrated:
            logger.info(
                "Migrated announce config from feeds for %d guilds",
                len(self.announce_configs))
            self._save_announce_config()

    def _save_announce_config(self):
        """Save announce config to disk."""
        try:
            serializable = {
                str(gid): ch
                for gid, ch in self.announce_configs.items()}
            _atomic_json_write(ANNOUNCE_FILE, serializable)
        except (OSError, IOError) as e:
            logger.error("Failed to save announce config: %s", e)

    async def save_feeds_async(self):
        """Off-loop variant of save_feeds, for use from async paths."""
        await asyncio.to_thread(self.save_feeds)

    def save_feeds(self):
        """Save feed subscriptions to disk."""
        with self._feeds_lock:
            try:
                # Convert sets to lists, datetimes to ISO strings
                serializable = {}
                for gid, feeds in self.feeds.items():
                    serializable[str(gid)] = {}
                    for url, data in feeds.items():
                        d = dict(data)
                        d['posted_events'] = list(
                            d.get('posted_events', set()))
                        if isinstance(d.get('last_checked'), datetime):
                            d['last_checked'] = (
                                d['last_checked'].isoformat())
                        serializable[str(gid)][url] = d
                _atomic_json_write(FEEDS_FILE, serializable)
            except (OSError, IOError) as e:
                logger.error("Failed to save feeds file: %s", e)

    # ── Feed type detection ──────────────────────────────────────────

    @staticmethod
    def _detect_feed_type(text: str, content_type: str = '') -> str:
        """Detect whether fetched content is iCal or RSS."""
        if 'BEGIN:VCALENDAR' in text or 'text/calendar' in content_type:
            return 'ical'
        if '<rss' in text.lower() or '<feed' in text.lower() or \
           'application/rss+xml' in content_type or \
           'application/atom+xml' in content_type:
            return 'rss'
        return 'ical'

    @staticmethod
    def _strip_html_tags(html_text: str) -> str:
        """Strip HTML tags from text for clean display."""
        if not html_text:
            return ''
        clean = re.sub(r'<br\s*/?>', '\n', html_text, flags=re.IGNORECASE)
        clean = re.sub(r'<[^<]+?>', '', clean)
        return html.unescape(clean).strip()

    # ── Shared helpers ───────────────────────────────────────────────

    def _get_notification_channel(self, guild, channel_name: str):
        """Get the Discord channel for notifications.

        Handles plain names, mention format (<#id>), and numeric IDs.
        """
        mention_match = re.match(r'^<#(\d+)>$', channel_name.strip())
        if mention_match:
            ch = guild.get_channel(int(mention_match.group(1)))
            if ch:
                return ch

        if channel_name.strip().isdigit():
            ch = guild.get_channel(int(channel_name.strip()))
            if ch:
                return ch

        clean_name = channel_name.lstrip('#').strip()
        channel = discord.utils.get(
            guild.text_channels, name=clean_name)
        if not channel:
            logger.error("Channel '%s' not found in guild %s",
                         channel_name, guild.name)
        return channel

    # ── Feed check job (runs weekly Monday 10am CT) ──────────────────

    def _cleanup_old_posted_events(self):
        """Remove posted_events entries for events that have already passed.

        Handles both composite uids (rss_uid|YYYY-MM-DD) and legacy
        plain uids (which are removed unconditionally since we can't
        determine their date).
        """
        cutoff = datetime.now() - timedelta(days=7)
        cleaned = 0

        for guild_id, feeds in self.feeds.items():
            for url, feed_data in feeds.items():
                posted = feed_data.get('posted_events', set())
                if not posted:
                    continue
                to_keep = set()
                for uid in posted:
                    if '|' in uid:
                        date_str = uid.rsplit('|', 1)[1]
                        try:
                            event_date = datetime.strptime(
                                date_str, '%Y-%m-%d')
                            if event_date >= cutoff:
                                to_keep.add(uid)
                            else:
                                cleaned += 1
                        except ValueError:
                            to_keep.add(uid)
                    # Legacy uid from before composite keys existed. It
                    # used to be dropped outright "so it gets re-checked"
                    # — but the re-check looks for "uid|date", never
                    # finds it, and re-creates an event already posted.
                    # Keep it: the prefix match in _fetch_and_parse_rss
                    # and the 30-day window both still recognise it.
                    else:
                        to_keep.add(uid)
                feed_data['posted_events'] = to_keep

        if cleaned:
            logger.info("Cleaned up %d old posted_events entries",
                        cleaned)
            self.save_feeds()

    async def check_feeds_job(self, guild_id: Optional[int] = None) -> Dict[str, Any]:
        """Check subscribed feeds for new events (next 30 days).

        `guild_id` limits the run to one guild. The scheduled job passes
        nothing and sweeps everything; /check_event_feeds passes its own
        guild, because it used to gate on the caller's guild having
        feeds and then process *every* guild — creating events in other
        servers and reporting their feed names in the caller's summary.

        Returns a summary dict with counts for reporting.
        """
        self._cleanup_old_posted_events()

        results = {
            'feeds_checked': 0,
            'events_posted': 0,
            'errors': []
        }

        if not self.feeds:
            logger.info("No feeds registered, nothing to check")
            return results

        # Build list of feed check tasks and run them concurrently
        async def _check_one(guild, url, feed_data):
            fname = feed_data.get('name', url)
            try:
                count = await self._check_single_feed(
                    guild, url, feed_data)
                return ('ok', fname, count)
            except Exception as e:
                logger.error("Error checking feed %s: %s",
                             url, e)
                return ('error', fname, str(e))

        tasks = []
        for gid, feeds in self.feeds.items():
            if guild_id is not None and gid != guild_id:
                continue
            guild = self.bot.get_guild(gid)
            if not guild:
                results['errors'].append(
                    f"Guild {gid} not found")
                continue
            for url, feed_data in feeds.items():
                tasks.append(_check_one(guild, url, feed_data))

        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                results['errors'].append(str(result))
            elif result[0] == 'ok':
                results['feeds_checked'] += 1
                results['events_posted'] += result[2]
            else:
                results['errors'].append(
                    f"{result[1]}: {result[2]}")

        logger.info(
            "Feed check complete: %d feeds, %d events posted, "
            "%d errors",
            results['feeds_checked'],
            results['events_posted'],
            len(results['errors']))

        # Persist all feed state changes in one write, off the loop
        await self.save_feeds_async()

        return results

    async def _check_single_feed(self, guild, url: str,
                                 feed_data: Dict[str, Any]) -> int:
        """Check a single feed (iCal or RSS) for new events.

        Returns the number of new events processed.
        """
        fname = feed_data.get('name', url)
        feed_type = feed_data.get('feed_type', 'ical')
        logger.info("Checking %s feed '%s': %s",
                    feed_type, fname, url)

        if feed_type == 'rss':
            new_events = await self._fetch_and_parse_rss(
                url, feed_data)
        else:
            calendar = await self._fetch_calendar(url)
            new_events = self._parse_calendar_events(
                calendar, feed_data)
            # Meetup (and many other iCal feeds) ship events with an
            # empty LOCATION field — the venue lives on the event page.
            # Scrape the URL to enrich the location.
            new_events = await self._enrich_ical_events(new_events)

        logger.info("Feed '%s': found %d new events",
                    fname, len(new_events))

        if not new_events:
            return 0

        await self._process_new_events(
            guild, new_events, feed_data)

        return len(new_events)

    # ── iCal parsing ─────────────────────────────────────────────────

    async def _fetch_calendar(self, url: str):
        """Fetch and parse calendar from URL."""
        cached, hit = self._memo_get('cal', url)
        if hit:
            return cached
        return self._memo_put('cal', url, await self._fetch_calendar_uncached(url))

    async def _fetch_calendar_uncached(self, url: str):
        """Network fetch behind _fetch_calendar's memo."""
        problem = await _validate_fetchable_url(url)
        if problem:
            raise ValueError(f'{url}: {problem}')
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response.raise_for_status()
                text = await _read_capped(response, url)
                return Calendar.from_ical(text)

    async def _enrich_ical_events(self, events: list) -> list:
        """Scrape event URLs to fill in missing location data.

        Meetup's iCal LOCATION field is empty, but its event pages
        include JSON-LD with the venue name and street address.
        """
        to_scrape = [
            e for e in events
            if e.get('link') and not (e.get('location') or '').strip()
        ]
        if not to_scrape:
            return events

        sem = asyncio.Semaphore(3)

        async def _scrape(ev):
            async with sem:
                scraped = await self._scrape_event_page(
                    session, ev['link'], ev['uid'])
                await asyncio.sleep(0.3)
                if scraped and scraped.get('location'):
                    ev['location'] = scraped['location']

        async with aiohttp.ClientSession(
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            await asyncio.gather(
                *[_scrape(e) for e in to_scrape],
                return_exceptions=True)
        return events

    def _parse_calendar_events(self, calendar,
                               feed_data: Dict[str, Any]) -> list:
        """Parse iCal events, return new ones in the next 30 days."""
        posted_events = feed_data.get('posted_events', set())
        # Aware, so a feed's UTC timestamps and a floating local time are
        # both compared against the same instant.
        current_time = datetime.now(CENTRAL_TZ)
        cutoff = current_time + timedelta(days=30)
        new_events = []

        for component in calendar.walk('VEVENT'):
            event = self._extract_ical_event(component)
            if not event:
                continue
            # Build composite uid (uid + date) for consistent
            # dedup and cleanup across iCal and RSS feeds
            sd = event['start_date']
            sd_str = sd.strftime('%Y-%m-%d') if hasattr(
                sd, 'strftime') else str(sd)
            composite_uid = f"{event['uid']}|{sd_str}"
            event['uid'] = composite_uid
            if composite_uid in posted_events:
                continue
            # Only events in the next 30 days
            sd_aware = _localize_naive(sd)
            if sd_aware < current_time - timedelta(hours=1):
                continue
            if sd_aware > cutoff:
                continue
            new_events.append(event)

        return new_events

    @staticmethod
    def _strip_urls(text: str) -> str:
        """Remove URLs from a string and clean up extra whitespace."""
        cleaned = re.sub(r'https?://\S+', '', text)
        cleaned = re.sub(r'\s{2,}', ' ', cleaned)
        return cleaned.strip().rstrip(',').strip()

    def _extract_ical_event(self, component) -> Optional[Dict[str, Any]]:
        """Extract event details from an iCal VEVENT component."""
        summary = str(component.get('summary', 'No Title'))
        description = str(component.get('description', ''))
        location = self._strip_urls(
            str(component.get('location', '')))
        url = str(component.get('url', ''))
        uid = str(component.get('uid', ''))

        # Event.start/.end resolve DTSTART and DTEND-*or*-DURATION the
        # way RFC 5545 defines them. The previous hand-rolled version
        # read only DTEND and otherwise fell back to a flat +1 hour, so
        # an event published as `DURATION:PT3H` was created as one hour.
        try:
            start_date = component.start
            end_date = component.end
        except (IncompleteComponent, ValueError) as e:
            logger.warning("Skipping VEVENT %s: %s", uid or '<no uid>', e)
            return None

        # Normalise all-day `date` values to datetime so everything
        # downstream (tz handling, window filter) sees one shape.
        if not isinstance(start_date, datetime):
            start_date = datetime.combine(
                start_date, datetime.min.time())
        if not isinstance(end_date, datetime):
            end_date = datetime.combine(
                end_date, datetime.min.time())

        # A VEVENT carrying neither DTEND nor DURATION yields end ==
        # start; Discord needs a non-zero span.
        same_awareness = (
            (start_date.tzinfo is None) == (end_date.tzinfo is None))
        if same_awareness and end_date <= start_date:
            end_date = start_date + timedelta(hours=1)

        return {
            'uid': uid,
            'summary': summary,
            'description': description,
            'location': location,
            'link': url,
            'start_date': start_date,
            'end_date': end_date
        }

    # ── RSS parsing with page scraping ───────────────────────────────

    async def _fetch_and_parse_rss(self, url: str,
                                   feed_data: Dict[str, Any]) -> list:
        """Fetch RSS feed, then scrape each event page for details.

        Uses a single shared HTTP session for all page scrapes
        with a small delay between requests to avoid rate limiting.
        """
        posted_events = feed_data.get('posted_events', set())
        current_time = datetime.now(CENTRAL_TZ)
        cutoff = current_time + timedelta(days=30)
        new_events = []

        # Single shared session for feed + all page scrapes
        # Use semaphore to limit concurrent scrapes and avoid rate limiting
        scrape_sem = asyncio.Semaphore(3)

        async def _scrape_with_limit(session, link, rss_uid):
            async with scrape_sem:
                result = await self._scrape_event_page(
                    session, link, rss_uid)
                await asyncio.sleep(0.3)  # Brief pause per scrape
                return rss_uid, result

        async with aiohttp.ClientSession(
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            # Fetch and parse the RSS feed
            text, hit = self._memo_get('rss', url)
            if not hit:
                async with session.get(url) as response:
                    response.raise_for_status()
                    text = await _read_capped(response, url)
                self._memo_put('rss', url, text)

            parsed_feed = feedparser.parse(text)

            # Collect entries to scrape, skipping already-posted. The
            # comment always claimed this, but the filter lived *after*
            # the scrape — so every run re-fetched every event page and
            # threw nearly all of them away. posted_events holds
            # composite "rss_uid|YYYY-MM-DD" keys, so match on prefix.
            seen_uids = {p.rsplit('|', 1)[0] for p in posted_events}
            entries_to_scrape = []
            for entry in parsed_feed.get('entries', []):
                link = getattr(entry, 'link', '')
                rss_uid = getattr(entry, 'id', '') or link
                if not rss_uid or rss_uid in seen_uids:
                    continue
                entries_to_scrape.append((link, rss_uid))
            if not entries_to_scrape:
                logger.info(
                    "RSS feed %s: all %d entries already posted, "
                    "nothing to scrape", url,
                    len(parsed_feed.get('entries', [])))
                return []

            # Scrape all pages concurrently with semaphore
            scrape_tasks = [
                _scrape_with_limit(session, link, rss_uid)
                for link, rss_uid in entries_to_scrape
            ]
            scrape_results = await asyncio.gather(
                *scrape_tasks, return_exceptions=True)

            for result in scrape_results:
                if isinstance(result, Exception):
                    logger.error("Error scraping RSS entry: %s", result)
                    continue
                rss_uid, event = result
                if not event:
                    continue

                # Build a composite uid from the RSS id + start date
                sd = event['start_date']
                sd_str = sd.strftime('%Y-%m-%d')
                composite_uid = f"{rss_uid}|{sd_str}"
                event['uid'] = composite_uid

                if composite_uid in posted_events:
                    continue

                # Filter to next 30 days
                sd_aware = _localize_naive(sd)
                if sd_aware < current_time - timedelta(hours=1):
                    continue
                if sd_aware > cutoff:
                    continue

                new_events.append(event)

        return new_events

    async def _scrape_event_page(self, session,
                                 url: str,
                                 uid: str) -> Optional[Dict[str, Any]]:
        """Scrape a single event page for JSON-LD Event data.

        Uses the provided aiohttp session (shared across scrapes).
        """
        if not url:
            return None

        problem = await _validate_fetchable_url(url)
        if problem:
            logger.warning('Refusing to scrape %s: %s', url, problem)
            return None

        cached, hit = self._memo_get('page', url)
        if hit:
            # Copy: _fetch_and_parse_rss mutates the returned dict
            # (event['uid'] = composite), which must not leak across
            # the two passes sharing this memo.
            return dict(cached) if cached else cached

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.error(
                        "Failed to scrape %s: HTTP %s",
                        url, response.status)
                    return self._memo_put('page', url, None)
                html = await _read_capped(response, url)

            # Extract JSON-LD Event data. The script tag carries extra
            # attributes on Meetup (data-next-head=""), so match any
            # attribute order/content rather than an exact tag.
            ld_matches = re.findall(
                r'<script[^>]*type=["\']application/ld\+json["\']'
                r'[^>]*>(.*?)</script>',
                html, re.DOTALL | re.IGNORECASE)

            for match in ld_matches:
                try:
                    data = json.loads(match)
                    items = (data if isinstance(data, list)
                             else [data])
                    for item in items:
                        if (isinstance(item, dict)
                                and item.get('@type') == 'Event'):
                            return self._memo_put(
                                'page', url,
                                self._parse_jsonld_event(item, url, uid))
                except (json.JSONDecodeError, KeyError):
                    continue

            logger.warning("No JSON-LD Event found on %s", url)
            return self._memo_put('page', url, None)

        except (aiohttp.ClientError, asyncio.TimeoutError,
                ResponseTooLarge) as e:
            logger.error(
                "Error scraping event page %s: %s", url, e)
            return None

    def _parse_jsonld_location(self, location_data) -> str:
        """Flatten a schema.org location value into a display string.

        Handles Place (with a PostalAddress dict or plain-string
        address), VirtualLocation (online events), a bare string, and
        a list of any of the above.
        """
        if not location_data:
            return ''

        # Some feeds emit a list of locations (e.g. a venue plus an
        # online stream) — take the first one that yields anything.
        if isinstance(location_data, list):
            for item in location_data:
                parsed = self._parse_jsonld_location(item)
                if parsed:
                    return parsed
            return ''

        if isinstance(location_data, str):
            return self._strip_urls(location_data)

        if not isinstance(location_data, dict):
            return ''

        loc_name = str(location_data.get('name', '') or '').strip()

        if location_data.get('@type') == 'VirtualLocation':
            # The only useful field is a URL, which _strip_urls would
            # wipe — and Discord rejects an empty external location.
            return loc_name or 'Online'

        address = location_data.get('address')
        street = ''
        if isinstance(address, dict):
            # Meetup packs locality/region into streetAddress already
            # ("318 Union St, Mishawaka, IN"), so only fall back to the
            # separate fields when streetAddress is absent.
            street = str(address.get('streetAddress', '') or '').strip()
            if not street:
                street = ', '.join(
                    part for part in (
                        str(address.get('addressLocality', '') or '').strip(),
                        str(address.get('addressRegion', '') or '').strip(),
                    ) if part)
        elif isinstance(address, str):
            street = address.strip()

        if loc_name and street:
            location = f"{loc_name}, {street}"
        else:
            location = loc_name or street

        return self._strip_urls(location)

    def _parse_jsonld_event(self, data: dict, url: str,
                            uid: str) -> Optional[Dict[str, Any]]:
        """Parse a JSON-LD Event object into our event dict."""
        summary = data.get('name', 'No Title')
        description = self._strip_html_tags(
            data.get('description', ''))

        # Parse location
        location = self._parse_jsonld_location(data.get('location'))

        # Parse dates
        start_str = data.get('startDate', '')
        end_str = data.get('endDate', '')

        start_date = self._parse_iso_date(start_str, dateparser)
        if not start_date:
            return None

        end_date = self._parse_iso_date(end_str, dateparser)
        if not end_date:
            end_date = start_date + timedelta(hours=1)

        return {
            'uid': uid,
            'summary': summary,
            'description': description,
            'location': location,
            'link': url,
            'start_date': start_date,
            'end_date': end_date
        }

    @staticmethod
    def _parse_iso_date(date_str: str, dateparser=None) -> Optional[datetime]:
        """Parse an ISO 8601 date string to datetime."""
        if not date_str:
            return None
        # Try python-dateutil first if available
        if dateparser:
            try:
                return dateparser.parse(date_str)
            except (ValueError, TypeError):
                pass
        # Fallback: manual ISO parsing
        for fmt in ('%Y-%m-%dT%H:%M:%S%z',
                    '%Y-%m-%dT%H:%M:%S',
                    '%Y-%m-%d'):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None

    # ── Event processing & posting ───────────────────────────────────

    async def _process_new_events(self, guild,
                                  new_events: list,
                                  feed_data: Dict[str, Any]):
        """Register new events as Discord scheduled events.

        No channel posts are made here — announcements are handled
        exclusively by the weekly/day-of scheduled jobs.
        """
        posted_events = feed_data.get('posted_events', set())

        # Fetch existing events ONCE for duplicate checking
        existing_events = []
        try:
            existing_events = await guild.fetch_scheduled_events()
        except discord.HTTPException:
            pass

        for event in new_events:
            created, _action = await self._create_discord_event(
                guild, event, existing_events)
            # Only mark as posted once Discord actually accepted it —
            # marking on failure meant the event never retried and had
            # to be picked up manually by reconcile_discord_events()
            if created:
                posted_events.add(event['uid'])
                if created not in existing_events:
                    existing_events.append(created)

        feed_data['last_checked'] = datetime.now()
        feed_data['posted_events'] = posted_events
        # save_feeds() is called once per check_feeds_job run, not per feed

    async def _create_discord_event(self, guild,
                                    event: Dict[str, Any],
                                    existing_events=None):
        """Create or update a Discord Event in the guild's Events section.

        Match logic:
        - Same name + same start time  → already in sync, skip.
        - Same name prefix + same start time but different full name
          → title changed (e.g. sponsor update), edit in place.
        - No match on prefix + start time → create new event.

        "Name prefix" is everything before the first ' - ', so
        "BurbSec West - Sponsored by TORQ!" and
        "BurbSec West - Sponsors Wanted!" share the prefix
        "BurbSec West" and will be treated as the same event.

        Uses pre-fetched existing_events list to avoid redundant API
        calls.

        Returns (event, action) where action is one of 'created',
        'updated', 'unchanged' or 'failed'. Callers need the
        distinction: reporting a repair as a creation is misleading.
        """
        try:
            # Discord trims trailing whitespace server-side — strip
            # before we compare against fetched events or we'll never
            # dedup and will loop-recreate every run
            name = event['summary'].strip()[:100]
            # Description is just the event page URL — a bare URL so
            # Discord auto-links it (masked [text](url) markdown is not
            # rendered in scheduled-event descriptions).
            description = (event.get('link') or '').strip()[:1000]
            start_time = event['start_date']
            end_time = event.get('end_date')
            location = event.get('location', '')

            # Make timezone-aware (discord.py 2.7+ requires aware
            # datetimes). Zone-less feed times are local wall-clock, so
            # they get BOT_TIMEZONE — see _localize_naive.
            start_time = _localize_naive(start_time)
            end_time = _localize_naive(end_time) if end_time else end_time
            if not isinstance(start_time, datetime):
                start_time = _localize_naive(datetime.combine(
                    start_time, datetime.min.time()))
            if end_time and not isinstance(end_time, datetime):
                end_time = _localize_naive(datetime.combine(
                    end_time,
                    datetime.max.time().replace(microsecond=0)))
            if not end_time:
                end_time = start_time + timedelta(hours=1)

            event_location = (
                location[:100] if location
                else "See event details")

            # Stable prefix = everything before the first ' - '
            # e.g. "BurbSec West - Sponsored by TORQ!" → "BurbSec West"
            def _prefix(n):
                return n.split(' - ')[0].strip()

            name_prefix = _prefix(name)

            # Compare UTC instants — Discord stores times in UTC, but
            # our start_time may carry a Central tz from the iCal feed
            start_utc = start_time.astimezone(timezone.utc)
            if existing_events:
                for ev in existing_events:
                    ev_name = (ev.name or '').strip()
                    ev_start = ev.start_time
                    if ev_start and ev_start.tzinfo is None:
                        ev_start = ev_start.replace(
                            tzinfo=timezone.utc)
                    ev_utc = (ev_start.astimezone(timezone.utc)
                              if ev_start else None)

                    if ev_utc != start_utc:
                        continue
                    if _prefix(ev_name) != name_prefix:
                        continue

                    # Same event (matched on prefix + time) — sync any
                    # field that drifted. Name isn't the only thing that
                    # can change: events created before the location
                    # scraper worked are stuck on the placeholder, and
                    # they'd never be repaired if we gated on the title.
                    changes = {}
                    if ev_name != name:
                        changes['name'] = name
                    if description and (ev.description or '') != description:
                        changes['description'] = description
                    # Only sync location when we actually have one — a
                    # transient scrape failure yields the placeholder,
                    # and we must not clobber a good venue with it.
                    # Only external events carry a location; passing one
                    # for a voice/stage event raises TypeError.
                    if (location
                            and ev.entity_type == discord.EntityType.external
                            and (ev.location or '') != event_location):
                        changes['location'] = event_location

                    if not changes:
                        logger.info(
                            "Discord Event '%s' already up to date, "
                            "skipping", name)
                        return ev, 'unchanged'

                    # edit() requires end_time for external events that
                    # don't already have one set
                    if 'location' in changes and not ev.end_time:
                        changes['end_time'] = end_time

                    edited = await ev.edit(**changes)
                    logger.info(
                        "Updated Discord Event '%s' (%s)",
                        name, ', '.join(sorted(changes)))
                    return edited or ev, 'updated'

            # No existing match — create new
            # privacy_level is required by the Discord API; discord.py
            # does not default it, so passing it explicitly avoids the
            # misleading "entity_type required" 400 response
            discord_event = await guild.create_scheduled_event(
                name=name,
                description=description,
                start_time=start_time,
                end_time=end_time,
                location=event_location,
                entity_type=discord.EntityType.external,
                privacy_level=discord.PrivacyLevel.guild_only,
            )

            logger.info(
                "Created Discord Event '%s' (ID: %s) in guild %s",
                name, discord_event.id, guild.name)
            return discord_event, 'created'

        except (discord.Forbidden, ValueError, TypeError) as e:
            logger.error(
                "Error creating Discord Event '%s': %s",
                event['summary'], e)
        except discord.HTTPException as e:
            logger.error(
                "Error creating Discord Event '%s': %s",
                event['summary'], e)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(
                "Unexpected error creating Discord Event '%s': %s",
                event['summary'], e)
        return None, 'failed'

    # ── Announce system (recurring Mon/Thu job) ──────────────────────

    async def _announce_events(self, predicate, title_prefix, empty_log):
        """Iterate guilds and announce events matching predicate.

        predicate(ev_start_central) -> bool decides which events to post.
        Shared by announce_weekly_events and announce_todays_events.
        """
        for guild_id, ch_name in self.announce_configs.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            channel = self._get_notification_channel(guild, ch_name)
            if not channel:
                continue

            # Cached sequence rather than an API call: this path only
            # reads for display. The two dedup paths deliberately keep
            # fetch_scheduled_events() — create_scheduled_event() does
            # not write to this cache (only the gateway does), so a
            # just-created event would be briefly missing and get
            # duplicated.
            scheduled = guild.scheduled_events

            matching = []
            for ev in scheduled:
                ev_start = ev.start_time
                if ev_start.tzinfo is None:
                    ev_start = ev_start.replace(tzinfo=CENTRAL_TZ)
                else:
                    ev_start = ev_start.astimezone(CENTRAL_TZ)
                if predicate(ev_start):
                    matching.append(ev)

            if not matching:
                logger.info(empty_log, guild.name)
                continue

            for ev in matching:
                await self._post_discord_event_announcement(
                    channel, ev, title_prefix)

            logger.info(
                "Announced %d events (%s) for %s",
                len(matching), title_prefix, guild.name)

    async def announce_weekly_events(self):
        """Announce this week's Discord Events. Runs Mon 10am CT."""
        now = datetime.now(CENTRAL_TZ)
        ws = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        we = ws + timedelta(days=7)
        await self._announce_events(
            lambda s: ws <= s < we,
            "This Week",
            "No events this week for %s")

    async def announce_todays_events(self):
        """Announce today's Discord Events. Runs daily 10am CT."""
        today = datetime.now(CENTRAL_TZ).date()
        await self._announce_events(
            lambda s: s.date() == today,
            "Today",
            "No events today for %s")

    async def reconcile_discord_events(self, guild_id: Optional[int] = None) -> Dict[str, Any]:
        """Create Discord Events for any feed entries missing from the
        guild's scheduled-events list.

        The feed check marks events as 'posted' even when the Discord
        API rejects creation, so failed events never retry via the
        normal path. This method re-parses every feed ignoring the
        posted_events set and fills any gaps.

        Returns a summary dict for reporting.
        """
        results = {
            'feeds_checked': 0,
            'events_created': 0,
            'events_updated': 0,
            'errors': [],
        }

        for gid, feeds in self.feeds.items():
            if guild_id is not None and gid != guild_id:
                continue
            guild = self.bot.get_guild(gid)
            if not guild:
                results['errors'].append(
                    f"Guild {gid} not found")
                continue

            try:
                existing = list(
                    await guild.fetch_scheduled_events())
            except discord.HTTPException as e:
                results['errors'].append(
                    f"Fetch events for {guild.name}: {e}")
                continue

            for url, feed_data in feeds.items():
                fname = feed_data.get('name', url)
                results['feeds_checked'] += 1
                try:
                    # Parse with posted_events emptied so every
                    # event in the 30-day window is returned
                    probe = dict(feed_data)
                    probe['posted_events'] = set()
                    feed_type = feed_data.get('feed_type', 'ical')
                    if feed_type == 'rss':
                        events = await self._fetch_and_parse_rss(
                            url, probe)
                    else:
                        cal = await self._fetch_calendar(url)
                        events = self._parse_calendar_events(
                            cal, probe)
                        events = await self._enrich_ical_events(
                            events)

                    for ev in events:
                        created, action = (
                            await self._create_discord_event(
                                guild, ev, existing))
                        if action == 'created':
                            existing.append(created)
                            results['events_created'] += 1
                        elif action == 'updated':
                            results['events_updated'] += 1
                except Exception as e:  # pylint: disable=broad-exception-caught
                    results['errors'].append(f"{fname}: {e}")
                    logger.error(
                        "Reconcile error for %s: %s", fname, e)

        logger.info(
            "Reconcile complete: %d feeds, %d events created, "
            "%d updated, %d errors",
            results['feeds_checked'],
            results['events_created'],
            results['events_updated'],
            len(results['errors']))
        return results

    async def _post_discord_event_announcement(self, channel,
                                               scheduled_event,
                                               title_prefix="This Week"):
        """Post a single Discord Event announcement with URL preview."""
        try:
            # Build event URL — Discord auto-unfurls this into its own
            # native event card, so no custom embed is sent alongside it.
            event_url = (
                f"https://discord.com/events/"
                f"{scheduled_event.guild.id}/"
                f"{scheduled_event.id}")

            # A one-line heading above the URL rather than an embed:
            # the unfurled card still renders, but the Monday weekly
            # preview and the Tue-Sun day-of reminder are no longer
            # byte-identical posts that read as accidental duplicates.
            await channel.send(
                content=f"📢 **{title_prefix}**\n{event_url}")
            logger.info(
                "Announced event '%s' to #%s",
                scheduled_event.name, channel.name)

        except discord.HTTPException as e:
            logger.error("Error announcing event: %s", e)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Unexpected error announcing event: %s", e)

    async def check_feeds(self):
        """Legacy method for backward compatibility."""
        await self.check_feeds_job()

bot_instance: Optional[Any] = None  # Renamed to avoid redefining name from outer scope
tree: Optional[Any] = None
scheduler: Optional[Any] = None  # APScheduler instance, set in setup_commands
reminders: Dict[int, Dict[str, Any]] = {}
event_feed: Optional[EventFeed] = None
autoreplies: Dict[str, Dict[str, Any]] = {}  # Store autoreply rules {rule_id: rule_data}
autoreplies_lock: Optional[threading.Lock] = None
auto_backup_configs: Dict[int, Dict[str, Any]] = {}  # {guild_id: {interval_seconds, last_hash, last_backup_at}}
auto_backup_lock: Optional[threading.Lock] = None

# Users the bot has DMed recently. A reply to one of our own DMs (the
# /message_dump archive, /log_tail output) is solicited, so it must not
# trip the DM auto-kick. Maps user id -> grace expiry timestamp.
DM_GRACE_SECONDS = 24 * 60 * 60
_recent_bot_dms: Dict[int, float] = {}
_recent_bot_dms_lock = threading.Lock()


def note_bot_dm(user_id: int) -> None:
    """Record that the bot just DMed this user, granting reply grace."""
    with _recent_bot_dms_lock:
        _recent_bot_dms[user_id] = (
            time_module.time() + DM_GRACE_SECONDS)


def was_recently_dmed(user_id: int) -> bool:
    """True if the bot DMed this user inside the grace window."""
    now = time_module.time()
    with _recent_bot_dms_lock:
        for uid in [u for u, exp in _recent_bot_dms.items() if exp <= now]:
            del _recent_bot_dms[uid]
        return user_id in _recent_bot_dms


def _load_reminders():
    """Load reminders from disk into the module-level reminders dict."""
    if not os.path.exists(REMINDERS_FILE):
        return
    try:
        with open(REMINDERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        now = time_module.time()
        for key, reminder in data.items():
            if 'next_trigger' not in reminder:
                reminder['next_trigger'] = now + reminder['interval']
            reminders[int(key) if key.isdigit() else key] = reminder
        logger.info("Loaded %d reminders from disk", len(reminders))
    except (OSError, IOError, json.JSONDecodeError) as e:
        logger.error('Failed to read reminders file: %s', e)


async def _fire_reminder(channel_id: int, title: str, message: str, interval: int):
    """APScheduler callback: send a reminder message."""
    channel = bot_instance.get_channel(channel_id) if bot_instance else None
    if channel:
        try:
            await channel.send(f"**{title}**\n{message}")
            logger.info("Reminder '%s' sent to #%s", title, channel.name)
        except discord.HTTPException as e:
            logger.error("Failed to send reminder '%s': %s", title, e)
    else:
        logger.warning("Reminder channel %s not found, skipping '%s'", channel_id, title)

    # Persist next_trigger so reminders survive restarts
    if channel_id in reminders:
        reminders[channel_id]['next_trigger'] = time_module.time() + interval
        try:
            await asyncio.to_thread(_atomic_json_write, REMINDERS_FILE, dict(reminders))
        except (OSError, IOError) as e:
            logger.error("Failed to persist reminder next_trigger: %s", e)


def _schedule_reminder(channel_id: int, reminder_data: dict):
    """Register (or replace) an APScheduler job for one reminder."""
    if not scheduler or not scheduler.running:
        logger.warning("Scheduler not running, cannot schedule reminder for channel %s", channel_id)
        return
    job_id = f"reminder_{channel_id}"
    next_trigger = reminder_data.get('next_trigger', time_module.time() + reminder_data['interval'])
    next_run = datetime.fromtimestamp(next_trigger)
    interval = reminder_data['interval']
    scheduler.add_job(
        _fire_reminder,
        trigger=IntervalTrigger(seconds=interval),
        args=[channel_id, reminder_data['title'], reminder_data['message'], interval],
        id=job_id,
        replace_existing=True,
        next_run_time=next_run,
        misfire_grace_time=min(interval, 3600),
        coalesce=True,
    )
    logger.info("Scheduled reminder '%s' for channel %s (next run: %s, interval: %ds)",
                reminder_data['title'], channel_id, next_run, interval)


def register_all_reminder_jobs():
    """Re-register all persisted reminders as APScheduler jobs. Called after scheduler.start()."""
    for channel_id, reminder_data in reminders.items():
        _schedule_reminder(channel_id, reminder_data)
    if reminders:
        logger.info("Registered %d reminder jobs with scheduler", len(reminders))



def _apply_scope(cmd, *, gate_perms=None):
    """Common scoping applied to every command and command group.

    - guild_only: this bot is server-only by design — it auto-kicks
      unsolicited DMs — so no command is offered in DMs, including the
      bot interaction ones. Discord hides them there, and the three
      backup commands used to AttributeError in a DM because they
      dereference interaction.guild unguarded.
    - guild_install: this is a server moderation bot; nothing should be
      installable to a user account and invoked outside a guild.
    - default_permissions: a *hint* Discord uses to hide the command in
      the picker from members who lack the permission. Server admins can
      override it in Integrations settings and it is silently ignored on
      subcommands, so it never replaces the runtime check below — it
      only stops every member seeing /kick and /server_restore listed.
    """
    cmd = app_commands.guild_only()(cmd)
    cmd = app_commands.guild_install()(cmd)
    if gate_perms:
        cmd = app_commands.default_permissions(**gate_perms)(cmd)
    return cmd


def _reg(name, description, handler, *,
         describe=None, mod_only=False, admin_only=False, error=None):
    """Register `handler` as a slash command directly (no wrapper function).

    handler must be an `async def` whose first arg is the interaction.

    `mod_only` gates on the invoker's guild-wide `manage_messages`
    permission — the permission that lets someone delete other people's
    messages, used here as the signal for "this is a moderator" rather
    than a role literally named MODERATOR_ROLE_NAME.

    `admin_only` is stricter: it requires the guild-wide Administrator
    permission bit, reserved for commands that can rewrite the whole
    guild's structure (server backup/restore).

    Both use _require_guild_permissions rather than app_commands'
    has_permissions so the gate matches _is_moderator and the bot.py
    behavioural gates — see that helper for why channel-scoped
    resolution was wrong here.
    """
    cmd = handler
    if describe:
        cmd = app_commands.describe(**describe)(cmd)
    gate_perms = None
    if admin_only:
        gate_perms = {'administrator': True}
    elif mod_only:
        gate_perms = {'manage_messages': True}
    if gate_perms:
        cmd = _require_guild_permissions(**gate_perms)(cmd)
    cmd = _apply_scope(cmd, gate_perms=gate_perms)
    cmd = tree.command(name=name, description=description)(cmd)
    # Default rather than leave on_error unset: the tree-wide backstop
    # bails out when interaction.response.is_done(), which is also true
    # for a command that merely deferred before raising — so a deferring
    # command without a handler would spin forever with only a log line.
    cmd.on_error = error if error is not None else _command_error_handler
    return cmd


def register_commands():
    """Register all commands with the command tree."""
    if tree is None:
        return

    tree.add_command(create_set_reminder_command())

    async def _list_reminders(interaction: discord.Interaction):
        """Lists all current reminders."""
        try:
            if not reminders:
                await interaction.response.send_message(
                    'There are no reminders set.', ephemeral=True)
                return
            reminder_list = '\n'.join(
                f"**{r['title']}**: {r['message']} "
                f"(every {r['interval']} seconds)"
                for r in reminders.values())
            await interaction.response.send_message(
                f'Current reminders:\n{reminder_list}', ephemeral=True)
        except (discord.HTTPException, OSError, IOError) as e:
            logger.error('Error listing reminders: %s', e)
            await interaction.response.send_message(
                'Failed to list reminders due to an error.', ephemeral=True)

    _reg('list_reminders', 'Lists all current reminders', _list_reminders)
    _reg('delete_all_reminders', 'Deletes all active reminders',
         delete_all_reminders, mod_only=True, error=_command_error_handler)
    _reg('delete_reminder', 'Deletes a reminder by title',
         delete_reminder, mod_only=True,
         describe={'title': 'Title of the reminder to delete'})
    _reg('purge_last_messages',
         'Purges a specified number of messages from a channel',
         purge_last_messages, mod_only=True,
         describe={'channel': 'Channel to purge messages from',
                   'limit': 'Number of messages to delete'},
         error=purge_last_messages_error)
    _reg('purge_string',
         'Purges messages containing a specific string from a channel',
         purge_string, mod_only=True,
         describe={'channel': 'Channel to purge messages from',
                   'search_string': 'String to search for in messages',
                   'limit': 'How many recent messages to scan (default 1000)'},
         error=purge_string_error)
    _reg('purge_webhooks',
         'Purges messages sent by webhooks or apps from a channel',
         purge_webhooks, mod_only=True,
         describe={'channel': 'Channel to purge messages from',
                   'limit': 'How many recent messages to scan (default 1000)'},
         error=purge_webhooks_error)
    _reg('kick', 'Kicks one or more members from the server',
         kick_members, mod_only=True,
         describe={'members': 'Members to kick (separate multiple users with spaces)',
                   'reason': 'Reason for kick'},
         error=kick_error)
    _reg('kick_role', 'Kicks all members with a specified role from the server',
         kick_role, mod_only=True,
         describe={'role': 'Role whose members to kick',
                   'reason': 'Reason for kick'},
         error=kick_role_error)
    _reg('botsay', 'Makes the bot send a message to a specified channel',
         botsay_message, mod_only=True,
         describe={'channel': 'Channel to send the message to',
                   'message': 'Message to send'},
         error=botsay_error)
    _reg('timeout', 'Timeouts a member for a specified duration',
         timeout_member, mod_only=True,
         describe={'member': 'Member to timeout',
                   'duration': 'Timeout duration in seconds (max 28 days)',
                   'reason': 'Reason for timeout'},
         error=timeout_error)
    _reg('log_tail',
         'DM the last specified number of lines of the bot log to the user',
         log_tail_command, mod_only=True,
         describe={'lines': 'Number of log lines to retrieve (1-200)'},
         error=log_tail_error)
    _reg('add_event_feed',
         'Adds a calendar or RSS feed and its announcement channel',
         add_event_feed_command, mod_only=True,
         describe={'feed_name': 'A short name to identify this feed',
                   'calendar_url': 'URL of the calendar or RSS feed',
                   'channel': 'Channel to post weekly/day-of event announcements'},
         error=add_event_feed_error)
    _reg('list_event_feeds', 'Lists all registered event feeds',
         list_event_feeds_command, error=list_event_feeds_error)
    _reg('remove_event_feed', 'Removes an event feed by name',
         remove_event_feed_command, mod_only=True,
         describe={'feed_name': 'Name of the feed to remove'},
         error=remove_event_feed_error)
    _reg('check_event_feeds',
         'Manually check all event feeds for new events now',
         check_event_feeds_command, mod_only=True,
         error=check_event_feeds_error)
    _reg('bot_mood', "Check on the bot's current mood", bot_command,
         error=bot_command_error)
    _reg('pet_bot', 'Pet the bot', pet_bot_command,
         error=pet_bot_command_error)
    _reg('bot_pick_fav', 'See who the bot prefers today',
         bot_pick_fav_command,
         describe={'user1': 'First potential favorite',
                   'user2': 'Second potential favorite'},
         error=bot_pick_fav_command_error)
    _reg('message_dump',
         "Dump a user's messages from a channel into a downloadable file",
         message_dump_command, mod_only=True,
         describe={'user': "User whose messages to dump",
                   'channel': "Channel to dump messages from",
                   'start_date': "Start date in YYYY-MM-DD format (e.g., 2025-01-01)",
                   'limit': "Maximum number of messages to fetch (default: 1000)"},
         error=message_dump_error)
    _reg('clone_category_permissions',
         'Clone permissions from source category to destination category',
         clone_category_permissions, mod_only=True,
         describe={'source_category': 'Source category to copy permissions from',
                   'destination_category': 'Destination category to copy permissions to'},
         error=clone_category_permissions_error)
    _reg('clone_channel_permissions',
         'Clone permissions from source channel to destination channel',
         clone_channel_permissions, mod_only=True,
         describe={'source_channel': 'Source channel to copy permissions from',
                   'destination_channel': 'Destination channel to copy permissions to'},
         error=clone_channel_permissions_error)
    _reg('clone_role_permissions',
         'Clone permissions from source role to destination role',
         clone_role_permissions, mod_only=True,
         describe={'source_role': 'Source role to copy permissions from',
                   'destination_role': 'Destination role to copy permissions to'},
         error=clone_role_permissions_error)
    _reg('clear_category_permissions',
         'Clear all permission overwrites from a category',
         clear_category_permissions, mod_only=True,
         describe={'category': 'Category to clear all permission overwrites from'},
         error=clear_category_permissions_error)
    _reg('clear_channel_permissions',
         'Clear all permission overwrites from a channel',
         clear_channel_permissions, mod_only=True,
         describe={'channel': 'Channel to clear all permission overwrites from'},
         error=clear_channel_permissions_error)
    _reg('clear_role_permissions',
         'Clear all permissions from a role (reset to default)',
         clear_role_permissions, mod_only=True,
         describe={'role': 'Role to clear all permissions from'},
         error=clear_role_permissions_error)
    _reg('sync_channel_perms',
         'Sync permissions for all channels in a category with the category permissions',
         sync_channel_perms, mod_only=True,
         describe={'source_category':
                   'Category whose permissions will be synced to all its channels'},
         error=sync_channel_perms_error)
    _reg('list_users_without_roles',
         'Lists all users that do not have any server role assigned',
         list_users_without_roles, mod_only=True,
         error=list_users_without_roles_error)
    _reg('assign_role', 'Assigns a role to multiple users at once',
         assign_role, mod_only=True,
         describe={'role': 'Role to assign to the users',
                   'members': 'Members to assign the role to (separate multiple users with spaces or newlines)'},
         error=assign_role_error)
    _reg('remove_role', 'Removes a role from multiple users at once',
         remove_role, mod_only=True,
         describe={'role': 'Role to remove from the users',
                   'members': 'Members to remove the role from (separate multiple users with spaces or newlines)'},
         error=remove_role_error)
    _reg('voice_chaperone',
         'Enable or disable the voice channel chaperone functionality',
         voice_chaperone_command, mod_only=True,
         describe={'enabled': 'True to enable, False to disable voice chaperone'},
         error=voice_chaperone_error)
    _reg('nuke_protection',
         'Enable or disable anti-nuke protection',
         nuke_protection_command, mod_only=True,
         describe={'enabled': 'True to enable, False to disable anti-nuke protection'},
         error=nuke_protection_error)
    _reg('spam_protection',
         'Enable or disable message spam protection',
         spam_protection_command, mod_only=True,
         describe={'enabled': 'True to enable, False to disable spam protection'},
         error=spam_protection_error)
    _reg('dashboard',
         'Display a dashboard of all available commands grouped by category',
         dashboard_command, error=dashboard_command_error)
    _reg('server_backup',
         'Create a full structural backup of this server (roles, channels, '
         'categories, emoji) and DM it to you',
         server_backup_command, admin_only=True, error=server_backup_error)
    _reg('server_restore',
         'Restore server structure from a backup file, with a preview and '
         'confirmation before anything changes',
         server_restore_command, admin_only=True,
         describe={'backup_file': 'The .json backup file produced by /server_backup'},
         error=server_restore_error)
    _reg('auto_backup',
         'Enable/disable automatic server backups on an interval; only '
         'creates a new backup when the structure actually changed',
         auto_backup_command, admin_only=True,
         describe={'enabled': 'True to enable automatic backups, False to disable',
                   'interval_hours': 'Hours between backup checks, 1-720 (default 24; only used when enabling)'},
         error=auto_backup_error)

    register_raid_commands()
    register_autoreply_commands()

def setup_commands(bot_param):
    """Initialize command module with bot instance and register commands."""
    # Using globals is necessary here to initialize module-level variables
    # pylint: disable=global-statement
    global bot_instance, tree, scheduler, reminders, event_feed, autoreplies, autoreplies_lock, auto_backup_configs, auto_backup_lock  # pylint: disable=line-too-long
    bot_instance = bot_param
    if bot_instance:
        tree = bot_instance.tree
        tree.on_error = _tree_error_handler
    reminders = {}
    event_feed = EventFeed(bot_instance)
    autoreplies = {}
    autoreplies_lock = threading.Lock()
    auto_backup_configs = {}
    auto_backup_lock = threading.Lock()

    # Load existing reminders from disk
    _load_reminders()

    # Load existing autoreply rules
    load_autoreplies()

    # Load existing auto-backup configs
    _load_auto_backup_configs()

    # Shared scheduler for event feeds and reminders
    if event_feed:
        event_feed.scheduler = AsyncIOScheduler()
        scheduler = event_feed.scheduler

    # Clear existing commands before registering new ones
    try:
        if tree:
            tree.clear_commands(guild=None)
            logger.info("Cleared existing commands before registration")
    except Exception as e:
        logger.error("Error clearing commands: %s", e)
    
    register_commands()


def create_set_reminder_command():
    """Factory function to create the set_reminder command."""
    @app_commands.guild_only()
    @app_commands.guild_install()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.command(name='set_reminder', description='Sets a reminder message to be sent to a channel at regular intervals')
    @app_commands.describe(
        channel='Channel to send reminders to',
        title='Title of the reminder',
        message='Message content of the reminder',
        interval='Interval in seconds between reminders (minimum 60)'
    )
    @_require_guild_permissions(manage_messages=True)
    async def set_reminder_command(interaction: discord.Interaction,
                                  channel: discord.TextChannel, title: str,
                                  message: str,
                                  interval: app_commands.Range[int, 60]):
        """Sets a reminder message to be sent to a channel at regular intervals."""
        await set_reminder_callback(interaction, channel, title, message, interval)

    # Routed through the shared handler rather than a local one. The
    # local version appended "Last log: ..." to *every* branch —
    # including MissingPermissions, which by definition fires for
    # someone without manage_messages — re-opening the log leak that
    # _command_error_handler gates behind _is_moderator.
    set_reminder_command.on_error = _command_error_handler
    return set_reminder_command

async def set_reminder_callback(interaction: discord.Interaction,
                               channel: discord.TextChannel, title: str,
                               message: str, interval: int):
    """Callback for the set_reminder command."""
    reminder_data = {
        'channel_id': channel.id,
        'title': title,
        'message': message,
        'interval': interval,
        'next_trigger': time_module.time()  # Fire immediately, then repeat on interval
    }
    reminders[channel.id] = reminder_data
    await asyncio.to_thread(_atomic_json_write, REMINDERS_FILE, dict(reminders))
    _schedule_reminder(channel.id, reminder_data)

    await interaction.response.send_message(
        f'Reminder set in {channel.mention} every {interval} seconds.', ephemeral=True)


async def delete_all_reminders(interaction: discord.Interaction) -> None:
    """Delete all active reminders."""
    for channel_id in list(reminders.keys()):
        try:
            if scheduler:
                scheduler.remove_job(f"reminder_{channel_id}")
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    reminders.clear()
    try:
        await asyncio.to_thread(_atomic_json_write, REMINDERS_FILE, dict(reminders))
    except (OSError, IOError) as e:
        logger.error('Failed to write reminders file: %s', e)
        await interaction.response.send_message('Failed to delete reminders due to file access error.', ephemeral=True)
        return
    await interaction.response.send_message('All reminders have been deleted.', ephemeral=True)

async def delete_reminder(interaction: discord.Interaction, title: str) -> None:
    """Deletes a reminder by title."""
    try:
        found_channel_id = None
        for channel_id, reminder_data in list(reminders.items()):
            if reminder_data['title'] == title:
                found_channel_id = channel_id
                break
        if found_channel_id is not None:
            del reminders[found_channel_id]
            try:
                if scheduler:
                    scheduler.remove_job(f"reminder_{found_channel_id}")
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            try:
                await asyncio.to_thread(_atomic_json_write, REMINDERS_FILE, dict(reminders))
            except (OSError, IOError) as e:
                logger.error('Failed to write reminders file: %s', e)
                await interaction.response.send_message('Failed to delete reminder due to file access error.', ephemeral=True)
                return
            await interaction.response.send_message(f'Reminder titled "{title}" has been deleted.', ephemeral=True)
        else:
            await interaction.response.send_message(f'No reminder found with the title "{title}".', ephemeral=True)
    except (discord.HTTPException, OSError, IOError) as e:
        logger.error('Error deleting reminder: %s', e)
        await interaction.response.send_message('Failed to delete reminder due to an error.', ephemeral=True)

async def purge_last_messages(interaction: discord.Interaction, channel: discord.TextChannel,
                              limit: app_commands.Range[int, 1, 1000]):
    """Purges a specified number of messages from a channel."""
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await channel.purge(
            limit=limit,
            reason=f'/purge_last_messages by {interaction.user}')
        await interaction.followup.send(f'Deleted {len(deleted)} message(s)', ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send('You do not have permission to perform this action.', ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error: %s', e)
        await interaction.followup.send('Discord API error occurred. Please try again later.', ephemeral=True)

purge_last_messages_error = _command_error_handler
async def purge_string(interaction: discord.Interaction, channel: discord.TextChannel, search_string: str,
                       limit: app_commands.Range[int, 1, 10000] = 1000):
    """Purges messages containing a specific string from a channel.

    `limit` is how many recent messages to *scan*, not how many to
    delete. channel.purge() defaults to scanning only 100, so without an
    explicit value this silently ignored anything further back and
    reported 'Deleted 0 message(s)' as though the string were absent.
    """
    await interaction.response.defer(ephemeral=True)
    try:
        def check_message(message):
            return search_string in message.content

        deleted = await channel.purge(
            limit=limit, check=check_message,
            reason=f'/purge_string by {interaction.user}')
        await interaction.followup.send(
            f'Deleted {len(deleted)} message(s) containing "{search_string}" '
            f'from the last {limit} message(s) in {channel.mention}.',
            ephemeral=True)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.followup.send('A Discord API error occurred.', ephemeral=True)

purge_string_error = _command_error_handler
async def purge_webhooks(interaction: discord.Interaction, channel: discord.TextChannel,
                         limit: app_commands.Range[int, 1, 10000] = 1000):
    """Purges messages sent by webhooks or apps from a channel.

    `limit` is the number of recent messages to scan — see purge_string.
    """
    await interaction.response.defer(ephemeral=True)
    try:
        def check_message(message):
            return message.webhook_id is not None or message.author.bot

        deleted = await channel.purge(
            limit=limit, check=check_message,
            reason=f'/purge_webhooks by {interaction.user}')
        await interaction.followup.send(
            f'Deleted {len(deleted)} message(s) sent by webhooks or apps '
            f'from the last {limit} message(s) in {channel.mention}.',
            ephemeral=True)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
        logger.error('Discord API error: %s', e)
        await interaction.followup.send('A Discord API error occurred.', ephemeral=True)

purge_webhooks_error = _command_error_handler
async def kick_members(interaction: discord.Interaction, members: str, reason: Optional[str] = None):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Kicks one or more members from the server."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Check if guild exists
        if not interaction.guild:
            await interaction.followup.send('This command can only be used in a server.', ephemeral=True)
            return
        
        member_objects, failed_to_find = _parse_members(
            interaction.guild, members)
        if not member_objects:
            await interaction.followup.send(
                'No valid members found to kick. Please mention users or provide valid user IDs.',
                ephemeral=True)
            return

        # Check if the bot has permission to kick members
        if not interaction.guild.me or not interaction.guild.me.guild_permissions.kick_members:
            await interaction.followup.send('I do not have permission to kick members.', ephemeral=True)
            return
        
        # Kick each member
        kicked_members = []
        failed_kicks = []
        
        for member in member_objects:
            try:
                # Skip the bot itself
                if member == interaction.guild.me:
                    failed_kicks.append(f"{member.display_name} (cannot kick myself)")
                    continue
                
                # Skip members with higher roles than the bot
                if interaction.guild.me and member.top_role >= interaction.guild.me.top_role:
                    failed_kicks.append(f"{member.display_name} (higher role)")
                    continue

                # Skip the command user. Checked before the rank test
                # below: your own top role is never strictly below
                # itself, so self-kicks would otherwise be reported as
                # "outranks you".
                if member.id == interaction.user.id:
                    failed_kicks.append(f"{member.display_name} (cannot kick yourself)")
                    continue

                # Skip members ranked at or above the invoker
                if not _invoker_outranks(interaction, member):
                    failed_kicks.append(
                        f"{member.display_name} (outranks you)")
                    continue
                
                await member.kick(reason=f"Kicked by {interaction.user}. Reason: {reason}" if reason else f"Kicked by {interaction.user}")
                kicked_members.append(member)
                logger.info('Kicked member %s by user %s', member, interaction.user)
                
            except discord.Forbidden:
                failed_kicks.append(f"{member.display_name} (insufficient permissions)")
                logger.error('Failed to kick member %s: insufficient permissions', member)
            except discord.HTTPException as e:
                failed_kicks.append(f"{member.display_name} (API error)")
                logger.error('Failed to kick member %s: %s', member, e)
        
        # Build response message
        response_parts = []
        
        if kicked_members:
            kicked_list = ', '.join([member.display_name for member in kicked_members])
            response_parts.append(f' **Successfully kicked {len(kicked_members)} member(s):** {kicked_list}')
        
        if failed_to_find:
            failed_find_list = ', '.join(failed_to_find)
            response_parts.append(f'❌ **Could not find:** {failed_find_list}')
        
        if failed_kicks:
            response_parts.append(
                f' **Failed to kick {len(failed_kicks)} member(s):**\n'
                + _format_list_with_overflow(failed_kicks))
        
        if reason:
            response_parts.append(f'📝 **Reason:** {reason}')
        
        response_message = '\n\n'.join(response_parts)
        await interaction.followup.send(response_message, ephemeral=True)
        
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error in kick_members: %s', e)
        await interaction.followup.send('A Discord API error occurred.', ephemeral=True)

# Keep the old function for backward compatibility
async def kick_member(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    """Kicks a member from the server."""
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f'{member.mention} has been kicked. Reason: {reason}', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

kick_error = _command_error_handler
async def kick_role(interaction: discord.Interaction, role: discord.Role, reason: Optional[str] = None):
    """Kicks all members with a specified role from the server."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Check if guild exists
        if not interaction.guild:
            await interaction.followup.send('This command can only be used in a server.', ephemeral=True)
            return
        
        # Get all members with the specified role
        members_with_role = [member for member in interaction.guild.members if role in member.roles]
        
        if not members_with_role:
            await interaction.followup.send(f'No members found with the role {role.mention}.', ephemeral=True)
            return
        
        # Check if the bot has permission to kick members
        if not interaction.guild.me or not interaction.guild.me.guild_permissions.kick_members:
            await interaction.followup.send('I do not have permission to kick members.', ephemeral=True)
            return
        
        # Kick each member with the role
        kicked_count = 0
        failed_kicks = []
        
        for member in members_with_role:
            try:
                # Skip the bot itself
                if member == interaction.guild.me:
                    continue
                    
                # Skip members with higher roles than the bot
                if interaction.guild.me and member.top_role >= interaction.guild.me.top_role:
                    failed_kicks.append(f"{member.display_name} (higher role)")
                    continue

                # Skip the command user — kick_members already did this,
                # but a role kick could otherwise remove the moderator
                # who ran it. Ordered before the rank test for the same
                # reason as in kick_members.
                if member.id == interaction.user.id:
                    failed_kicks.append(
                        f"{member.display_name} (cannot kick yourself)")
                    continue

                # Skip members ranked at or above the invoker
                if not _invoker_outranks(interaction, member):
                    failed_kicks.append(
                        f"{member.display_name} (outranks you)")
                    continue

                await member.kick(reason=f"Role kick: {role.name}. {reason}" if reason else f"Role kick: {role.name}")
                kicked_count += 1
                logger.info('Kicked member %s for having role %s', member, role.name)
                
            except discord.Forbidden:
                failed_kicks.append(f"{member.display_name} (insufficient permissions)")
                logger.error('Failed to kick member %s: insufficient permissions', member)
            except discord.HTTPException as e:
                failed_kicks.append(f"{member.display_name} (API error)")
                logger.error('Failed to kick member %s: %s', member, e)
        
        # Send results
        result_message = f'Kicked {kicked_count} member(s) with the role {role.mention}.'
        
        if failed_kicks:
            result_message += f'\n\nFailed to kick {len(failed_kicks)} member(s):\n' + '\n'.join(f'• {name}' for name in failed_kicks[:10])
            if len(failed_kicks) > 10:
                result_message += f'\n... and {len(failed_kicks) - 10} more'
        
        await interaction.followup.send(result_message, ephemeral=True)
        
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error in kick_role: %s', e)
        await interaction.followup.send('A Discord API error occurred.', ephemeral=True)

kick_role_error = _command_error_handler
async def botsay_message(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    """Makes the bot send a message to a specified channel with proper markdown formatting."""
    try:
        # Send the message with allowed mentions disabled for safety
        # Discord will automatically render markdown formatting in the message content
        await channel.send(
            message,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=True,
                roles=False
            )
        )
        await interaction.response.send_message(f'Message sent to {channel.mention}', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

botsay_error = _command_error_handler
async def timeout_member(interaction: discord.Interaction, member: discord.Member,
                         duration: app_commands.Range[int, 1, 2419200],
                         reason: Optional[str] = None):
    """Timeouts a member for a specified duration."""
    try:
        until = discord.utils.utcnow() + timedelta(seconds=duration)
        await member.timeout(until, reason=reason)
        await interaction.response.send_message(f'{member.mention} has been timed out for {duration} seconds.', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

timeout_error = _command_error_handler
async def log_tail_command(interaction: discord.Interaction,
                           lines: app_commands.Range[int, 1, 200]):
    """DM the last specified number of lines of the bot log to the user."""
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as log_file:
            last_lines = ''.join(deque(log_file, maxlen=lines))
        if last_lines:
            note_bot_dm(interaction.user.id)
            await interaction.user.send(f'```{last_lines}```')
            await interaction.response.send_message('Log lines sent to your DMs.', ephemeral=True)
        else:
            await interaction.response.send_message('Log file is empty.', ephemeral=True)
    except (OSError, IOError) as e:
        logger.error('Failed to read log file: %s', e)
        await interaction.response.send_message('Failed to retrieve log file.', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

log_tail_error = _command_error_handler
async def add_event_feed_command(interaction: discord.Interaction,  # pylint: disable=too-many-branches,too-many-statements
                                 feed_name: str,
                                 calendar_url: str,
                                 channel: discord.TextChannel):
    """Adds a feed and sets it as the guild's announcement target."""
    try:
        await interaction.response.defer(ephemeral=True)

        resolved_channel_name = channel.name
        logger.info(
            "add_event_feed: name=%s url=%s channel=#%s",
            feed_name, calendar_url, resolved_channel_name)

        problem = await _validate_fetchable_url(calendar_url)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return

        # Fetch the URL and auto-detect feed type
        detected_type = 'ical'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    calendar_url,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get(
                        'content-type', '')
                    text = await _read_capped(response, calendar_url)

            # Try iCal first
            try:
                Calendar.from_ical(text)
                detected_type = 'ical'
            except (ValueError, Exception):  # pylint: disable=broad-exception-caught
                # Try RSS
                parsed = feedparser.parse(text)
                if (parsed.get('entries')
                        or parsed.get('feed', {}).get('title')):
                    detected_type = 'rss'
                else:
                    detected_type = EventFeed._detect_feed_type(
                        text, content_type)
                    if detected_type == 'ical':
                        await interaction.followup.send(
                            "Could not parse feed as iCal or "
                            "RSS. Please check the URL.",
                            ephemeral=True)
                        return

        except ResponseTooLarge as e:
            await interaction.followup.send(
                f"That feed is too large to process ({e}).", ephemeral=True)
            return
        except aiohttp.ClientError as e:
            await interaction.followup.send(
                f"Error accessing feed: {str(e)}",
                ephemeral=True)
            return

        if event_feed and interaction.guild:
            guild_id = interaction.guild.id
            if guild_id not in event_feed.feeds:
                event_feed.feeds[guild_id] = {}

            # Preserve posted_events when re-adding a URL already
            # registered (e.g. to rename it or repoint the channel).
            # Resetting it made every future event look new and
            # re-created the lot.
            existing = event_feed.feeds[guild_id].get(calendar_url, {})
            event_feed.feeds[guild_id][calendar_url] = {
                'name': feed_name,
                'last_checked': datetime.now(),
                'channel': resolved_channel_name,
                'posted_events': existing.get('posted_events', set()),
                'feed_type': detected_type,
            }
            event_feed.save_feeds()

            # Auto-enable announcements for this channel so the user
            # doesn't need a second setup command
            event_feed.announce_configs[guild_id] = resolved_channel_name
            event_feed._save_announce_config()  # pylint: disable=protected-access

        # Build confirmation message
        type_label = "RSS" if detected_type == 'rss' else "iCal"
        await interaction.followup.send(
            f"✅ Added {type_label} feed "
            f"**\"{feed_name}\"**! "
            f"Checking for events now...\n"
            f"📢 Announcements will post to "
            f"#{resolved_channel_name} (Mon 10am CT weekly, "
            f"daily 10am CT day-of).",
            ephemeral=True
        )

        # Immediately check the new feed AFTER sending confirmation
        if event_feed and interaction.guild:
            try:
                guild = interaction.guild
                guild_id = guild.id
                feed_data = event_feed.feeds[guild_id][calendar_url]
                count = await event_feed._check_single_feed(  # pylint: disable=protected-access
                    guild, calendar_url, feed_data)
                await interaction.followup.send(
                    f"✅ Initial check complete: "
                    f"**{count}** events registered on the "
                    f"server calendar.",
                    ephemeral=True)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error(
                    "Error on initial feed check: %s", e)
                await interaction.followup.send(
                    f"⚠️ Initial check error: {str(e)}",
                    ephemeral=True)

    except (discord.Forbidden, discord.HTTPException,
            ValueError, AttributeError) as e:
        try:
            await interaction.followup.send(
                f"Error adding feed: {str(e)}",
                ephemeral=True)
        except discord.HTTPException:
            logger.error("Error adding feed: %s", e)

add_event_feed_error = _command_error_handler
async def check_event_feeds_command(interaction: discord.Interaction):
    """Manually check all event feeds for new events now."""
    try:
        await interaction.response.defer(ephemeral=True)
        if not event_feed:
            await interaction.followup.send(
                'Event feed system is not initialized.',
                ephemeral=True)
            return

        if not event_feed.feeds:
            await interaction.followup.send(
                '❌ No feeds registered. Use '
                '`/add_event_feed` to add one.',
                ephemeral=True)
            return

        # Count feeds for this guild
        guild_id = interaction.guild.id if interaction.guild else None
        if not guild_id or guild_id not in event_feed.feeds:
            await interaction.followup.send(
                '❌ No feeds registered for this server.',
                ephemeral=True)
            return

        # One memo across both passes: reconcile re-parses every feed
        # by design, and without this every page is scraped twice.
        async with event_feed.memoized_fetches():
            results = await event_feed.check_feeds_job(guild_id=guild_id)
            recon = await event_feed.reconcile_discord_events(
                guild_id=guild_id)

        # Build result message
        parts = [
            f"📊 **Feed Check Results**\n"
            f"Feeds checked: **{results['feeds_checked']}**\n"
            f"New events posted: **{results['events_posted']}**\n"
            f"Missing Discord events created: "
            f"**{recon['events_created']}**\n"
            f"Existing events repaired: "
            f"**{recon['events_updated']}**"
        ]

        all_errors = list(results['errors']) + list(recon['errors'])
        if all_errors:
            error_list = '\n'.join(
                f"• {e}" for e in all_errors[:5])
            parts.append(f"\n\n⚠️ **Errors:**\n{error_list}")

        if (results['events_posted'] == 0
                and recon['events_created'] == 0
                and recon['events_updated'] == 0
                and not all_errors):
            parts.append(
                "\n\nNo new events found in the next 30 days "
                "and no missing Discord events to create.")

        await interaction.followup.send(
            '\n'.join(parts), ephemeral=True)

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Error in check_event_feeds: %s', e)
        await interaction.followup.send(
            f'Error checking feeds: {str(e)}',
            ephemeral=True)

check_event_feeds_error = _command_error_handler
async def list_event_feeds_command(interaction: discord.Interaction):
    """Lists all registered event feeds with settings."""
    try:
        if (not event_feed or not interaction.guild
                or interaction.guild.id not in event_feed.feeds
                or not event_feed.feeds[interaction.guild.id]):
            await interaction.response.send_message(
                'No event feeds registered', ephemeral=True)
            return

        guild_feeds = event_feed.feeds[interaction.guild.id]
        embed = discord.Embed(
            title=f"📅 Event Feeds ({len(guild_feeds)})",
            color=0x00ff00
        )

        # Discord caps embeds at 25 fields; a name field also caps at
        # 256 chars. Neither was enforced here, so a guild with enough
        # feeds (or one long user-supplied feed name) could raise an
        # HTTPException nothing caught.
        max_fields = 25
        items = list(guild_feeds.items())
        truncated = len(items) > max_fields
        for url, data in items[:max_fields]:
            fname = data.get('name', 'Unnamed')
            if len(fname) > 240:
                fname = fname[:237] + '...'
            feed_type = data.get('feed_type', 'ical').upper()
            ch = data.get('channel', 'unknown')

            display_url = (
                url if len(url) <= 60 else url[:57] + "...")

            value = (
                f"**URL:** {display_url}\n"
                f"**Type:** {feed_type} | **Channel:** #{ch}"
            )
            embed.add_field(
                name=f"📌 {fname}", value=value, inline=False)

        if truncated:
            embed.set_footer(
                text=f"Showing {max_fields} of {len(items)} feeds")

        await interaction.response.send_message(
            embed=embed, ephemeral=True)
    except discord.Forbidden:
        await _send_or_followup(
            interaction, "I don't have permission to do that here.")
    except discord.HTTPException as e:
        logger.error('Discord API error in list_event_feeds_command: %s', e)
        await _send_or_followup(
            interaction, 'A Discord API error occurred while listing event feeds.')
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Unexpected error in list_event_feeds_command: %s', e)
        await _send_or_followup(
            interaction, 'An unexpected error occurred while listing event feeds.')

list_event_feeds_error = _command_error_handler

async def remove_event_feed_command(interaction: discord.Interaction,
                                    feed_name: str):
    """Removes an event feed by name."""
    if (not event_feed or not interaction.guild
            or interaction.guild.id not in event_feed.feeds):
        await interaction.response.send_message(
            'No event feeds registered', ephemeral=True)
        return

    guild_feeds = event_feed.feeds[interaction.guild.id]

    # Find the feed URL by name
    target_url = None
    for url, data in guild_feeds.items():
        if data.get('name', '').lower() == feed_name.lower():
            target_url = url
            break

    if not target_url:
        await interaction.response.send_message(
            f'Event feed named "{feed_name}" not found. '
            f'Use `/list_event_feeds` to see all feeds.',
            ephemeral=True)
        return

    del guild_feeds[target_url]
    event_feed.save_feeds()

    # If no feeds remain in the guild, drop the announce config
    # so stale announcements don't keep firing
    if not guild_feeds and interaction.guild.id in event_feed.announce_configs:
        del event_feed.announce_configs[interaction.guild.id]
        event_feed._save_announce_config()  # pylint: disable=protected-access

    await interaction.response.send_message(
        f'✅ Removed event feed **"{feed_name}"**',
        ephemeral=True)

remove_event_feed_error = _command_error_handler

async def bot_command(interaction: discord.Interaction):
    """Check on the bot."""
    try:
        bot_name = interaction.client.user.display_name if interaction.client.user else "the bot"
        message = get_time_based_message(bot_name)
        logger.info('[%s] - bot command: %s', interaction.user, message)
        await interaction.response.send_message(message)
    except discord.errors.NotFound:
        # Handle case where interaction has timed out
        logger.warning("Interaction timed out for bot command from %s", interaction.user)

bot_command_error = _command_error_handler

async def pet_bot_command(interaction: discord.Interaction):
    """Pet the bot."""
    try:
        bot_name = interaction.client.user.display_name if interaction.client.user else "the bot"
        message = random.choice(_pet_bot_responses).replace("BOTNAME", bot_name)
        logger.info('[%s] - pet bot command: %s', interaction.user, message)
        await interaction.response.send_message(message)
    except discord.errors.NotFound:
        # Handle case where interaction has timed out
        logger.warning("Interaction timed out for pet_bot command from %s", interaction.user)

pet_bot_command_error = _command_error_handler

async def bot_pick_fav_command(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
    """See who the bot prefers today."""
    try:
        # Defensive check against special mentions (though Discord's type system should prevent this)
        if interaction.guild and (user1.id == interaction.guild.id or user2.id == interaction.guild.id):
            await interaction.response.send_message(
                "Sorry, I can't pick favorites with @everyone! Please choose specific users.",
                ephemeral=True
            )
            return
        
        # Prevent the bot from mentioning itself
        if interaction.client.user and (user1.id == interaction.client.user.id or user2.id == interaction.client.user.id):
            await interaction.response.send_message(
                "I can't pick myself as a favorite! Please choose other users.",
                ephemeral=True
            )
            return
        
        # Prevent same user being used twice
        if user1.id == user2.id:
            await interaction.response.send_message(
                "Please choose two different users!",
                ephemeral=True
            )
            return
        
        bot_name = interaction.client.user.display_name if interaction.client.user else "the bot"
        # More efficient user selection and message formatting
        users = [user1, user2]
        chosen_user = random.choice(users)
        message = f"{bot_name} is giving attention to {chosen_user.mention}!"
        logger.info('[%s] - bot pick fav command: %s', interaction.user, message)
        await interaction.response.send_message(message)
    except discord.errors.NotFound:
        # Handle case where interaction has timed out
        logger.warning("Interaction timed out for bot_pick_fav command from %s", interaction.user)

bot_pick_fav_command_error = _command_error_handler

def cleanup_orphaned_dumps():
    """Clean up orphaned message dump files and folders older than 30 minutes.
    
    Returns:
        int: Number of directories cleaned up
    """
    cleaned_count = 0
    try:
        now = datetime.now()
        
        if not os.path.exists(TEMP_DIR):
            return cleaned_count
            
        for item in os.listdir(TEMP_DIR):
            if item.startswith("message_dump_"):
                item_path = os.path.join(TEMP_DIR, item)
                
                if os.path.isdir(item_path):
                    creation_time = datetime.fromtimestamp(os.path.getctime(item_path))
                    
                    if (now - creation_time).total_seconds() > 1800:  # 30 minutes in seconds
                        shutil.rmtree(item_path, ignore_errors=True)
                        logger.info(f"Cleaned up orphaned message dump directory: {item_path}")
                        cleaned_count += 1
        
        return cleaned_count
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Error cleaning up orphaned message dumps: %s", e)
        return cleaned_count

async def message_dump_command(interaction: discord.Interaction, user: discord.User, channel: discord.TextChannel,  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-nested-blocks
                              start_date: str,
                              limit: app_commands.Range[int, 1, 100000] = 1000):
    """Dump a user's messages from a channel into a downloadable file starting from a specific date."""
    try:
        logger.info("Checking for orphaned message dump files/folders...")
        cleaned_count = cleanup_orphaned_dumps()
        if cleaned_count > 0:
            logger.info("Cleaned up %s orphaned message dump directories", cleaned_count)
        await interaction.response.defer(ephemeral=True)
        
        if cleaned_count > 0:
            await interaction.followup.send(
                f"Cleaned up {cleaned_count} orphaned message dump files that were older than 30 minutes.",
                ephemeral=True
            )
        
        try:
            # Anchor to UTC — a naive datetime is read as server-local
            # by discord.py's snowflake conversion, shifting the start
            # of the window by the host's UTC offset.
            start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
            start_datetime = start_datetime.replace(
                hour=0, minute=0, second=0, microsecond=0,
                tzinfo=timezone.utc)
        except ValueError:
            await interaction.followup.send(
                "Invalid date format. Please use YYYY-MM-DD format (e.g., 2025-01-01).",
                ephemeral=True
            )
            return
            
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dump_dir = os.path.join(TEMP_DIR, f"message_dump_{interaction.user.id}_{timestamp}")
        os.makedirs(dump_dir, exist_ok=True)
        
        file_path = os.path.join(dump_dir, f"{user.name}_messages.txt")
        zip_path = os.path.join(dump_dir, f"{user.name}_messages.zip")
        
        messages = []
        
        logger.info(f"Looking for messages from user ID: {user.id}, name: {user.name}")
        
        try:
            async for _ in channel.history(limit=1):
                break
            else:
                await interaction.followup.send(f"The channel {channel.mention} appears to be empty.", ephemeral=True)
                shutil.rmtree(dump_dir, ignore_errors=True)
                return
        except discord.Forbidden:
            await interaction.followup.send(f"I don't have permission to read messages in {channel.mention}.", ephemeral=True)
            shutil.rmtree(dump_dir, ignore_errors=True)
            return
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Error accessing channel: %s", e)
            await interaction.followup.send(f"Error accessing channel {channel.mention}: {str(e)}", ephemeral=True)
            shutil.rmtree(dump_dir, ignore_errors=True)
            return
            
        # Status update for the user
        await interaction.followup.send(
            f"Fetching messages from {user.mention} (ID: {user.id}) in {channel.mention} starting from {start_date}. "
            f"This may take a while...",
            ephemeral=True
        )
        
        # Simplified approach to message fetching.
        # Passing `after` makes discord.py iterate oldest-first, so
        # pagination walks *forward* from the newest id seen so far.
        messages = []
        total_processed = 0
        newest_message_id = None
        
        # Log the start of message fetching
        logger.info("Starting message fetch for user %s in channel %s", user.id, channel.id)
        
        # Rate limit handling variables
        retry_count = 0
        max_retries = 5
        base_delay = 1.0
        
        # Continue fetching until we reach the limit or run out of messages
        while total_processed < limit:
            try:
                # Determine how many messages to fetch in this batch
                batch_size = min(100, limit - total_processed)
                
                # Set up the fetch parameters. Resume from the newest
                # message already seen; the date only anchors batch one.
                fetch_kwargs = {'limit': batch_size}
                if newest_message_id:
                    fetch_kwargs['after'] = discord.Object(
                        id=newest_message_id)
                else:
                    fetch_kwargs['after'] = start_datetime
                
                # Log the current fetch attempt
                logger.info("Fetching batch with params: %s, processed so far: %s", fetch_kwargs, total_processed)
            
                # Fetch the batch
                current_batch = []
                messages_in_this_batch = 0
                
                async for msg in channel.history(**fetch_kwargs):
                    messages_in_this_batch += 1
                    # Advance the pagination cursor (oldest-first, so
                    # the highest id seen is where the next batch starts)
                    if newest_message_id is None or msg.id > newest_message_id:
                        newest_message_id = msg.id

                    # Count this message
                    total_processed += 1

                    # Check if this message is from our target user
                    if msg.author.id == user.id:
                        # Format the message more efficiently
                        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        content = msg.content or "[No text content]"
                        
                        # Build message parts list for efficient joining
                        message_parts = [f"[{timestamp}] {content}"]
                        
                        # Handle attachments
                        if msg.attachments:
                            attachment_urls = [a.url for a in msg.attachments]
                            message_parts.append(f"\nAttachments: {', '.join(attachment_urls)}")
                        
                        # Handle embeds
                        if msg.embeds:
                            message_parts.append(f"\nEmbeds: {len(msg.embeds)} embed(s)")
                        
                        # Join all parts efficiently
                        formatted_message = ''.join(message_parts) + "\n\n"
                        current_batch.append(formatted_message)
                
                # Reset retry count on successful fetch
                retry_count = 0
                
            except discord.RateLimited as e:
                # discord.RateLimited, NOT HTTPException — it derives
                # from DiscordException and this block used to catch
                # HTTPException and string-match "rate limited", so it
                # could never fire. The real 429 escaped to the
                # catch-all and lost the entire dump.
                #
                # Keep whatever this partial batch already collected:
                # total_processed and newest_message_id have both
                # advanced past those messages, so dropping
                # current_batch would silently truncate the archive.
                messages.extend(current_batch)
                retry_delay = getattr(e, 'retry_after', None) or (
                    base_delay * (2 ** retry_count))
                retry_count += 1
                logger.warning("Rate limited. Retrying in %.1fs. Retry %s/%s",
                               retry_delay, retry_count, max_retries)

                if retry_count <= max_retries:
                    await interaction.followup.send(
                        f"Hit Discord rate limit. Waiting "
                        f"{retry_delay:.0f} seconds before continuing...",
                        ephemeral=True
                    )
                    await asyncio.sleep(retry_delay)
                    continue

                logger.error("Max retries (%s) reached for rate limiting", max_retries)
                await interaction.followup.send(
                    "Hit Discord rate limit too many times. Try again later or with a smaller limit.",
                    ephemeral=True
                )
                break
            
            # Log the results of this batch
            batch_count = len(current_batch)
            logger.info("Batch complete: processed %s messages from target user", batch_count)
            
            # Add the batch to our collection
            messages.extend(current_batch)
            
            # Send a progress update every 500 messages. (Previously this
            # also fired on any short batch, which meant a followup on
            # nearly every batch.)
            if total_processed and total_processed % 500 == 0:
                await interaction.followup.send(
                    f"Progress update: Processed {total_processed} messages, found {len(messages)} from {user.mention}...",
                    ephemeral=True
                )
            
            # Log the batch results
            logger.info("Batch complete: got %s messages in batch, of which %s were from target user",
                       messages_in_this_batch, batch_count)
            
            # If we got fewer messages than requested, we've reached the end
            if messages_in_this_batch == 0 or messages_in_this_batch < batch_size:
                logger.info("End of channel history reached. Total processed: %s, found: %s",
                           total_processed, len(messages))
                break
            
            # No unconditional sleep here: discord.py's HTTP layer
            # already paces requests and sleeps through 429s, and the
            # RateLimited branch above handles the case it gives up on.
            # A flat 1s per 100-message batch just made a 1000-message
            # dump ten seconds slower for no benefit.
        
        # Send a completion message based on whether we reached the limit or ran out of messages
        if total_processed >= limit:
            await interaction.followup.send(
                f"Reached the message limit of {limit}. Found {len(messages)} messages from {user.mention}.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Completed! Processed all {total_processed} messages in the channel and found {len(messages)} from {user.mention}.",
                ephemeral=True
            )
        
        # If no messages found
        if not messages:
            await interaction.followup.send(f"No messages found from {user.mention} in {channel.mention}.", ephemeral=True)
            # Clean up the directory
            shutil.rmtree(dump_dir, ignore_errors=True)
            return
        
        # Write messages to file
        # Write file more efficiently using a single write operation
        header_parts = [
            f"Messages from {user.name} (ID: {user.id}) in #{channel.name}\n",
            f"Dump created at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            f"Start date: {start_date}\n",
            f"Messages found: {len(messages)}\n",
            f"Total messages processed: {total_processed}\n\n",
            "="*50 + "\n\n"
        ]
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(''.join(header_parts))
            f.writelines(messages)
        
        # Compress the file
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(file_path, os.path.basename(file_path))

        # Discord DM file-size cap is 25 MB without Nitro.
        # If we're over, fall back to telling the user we can't send it.
        zip_size = os.path.getsize(zip_path)
        max_size = 25 * 1024 * 1024
        if zip_size > max_size:
            await interaction.followup.send(
                f"Message dump is too large to DM via Discord "
                f"({zip_size / 1024 / 1024:.1f} MB, limit is 25 MB). "
                f"Try a smaller `limit` or a more recent `start_date`.",
                ephemeral=True)
            shutil.rmtree(dump_dir, ignore_errors=True)
            return

        dm_message = (
            f"Here's the message dump you requested from {channel.mention}:\n\n"
            f"**User:** {user.mention}\n"
            f"**Start Date:** {start_date}\n"
            f"**Messages found:** {len(messages)}\n"
            f"**Messages processed:** {total_processed}")

        note_bot_dm(interaction.user.id)
        await interaction.user.send(
            dm_message, file=discord.File(zip_path))

        await interaction.followup.send(
            f"Message dump for {user.mention} from {channel.mention} sent "
            f"to your DMs.", ephemeral=True)
        shutil.rmtree(dump_dir, ignore_errors=True)
        
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to access that channel or send you DMs.", ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error: %s', e)
        await interaction.followup.send("A Discord API error occurred.", ephemeral=True)
    except (OSError, IOError, PermissionError) as e:
        logger.error('File system error in message_dump_command: %s', e)
        await interaction.followup.send("A file system error occurred while creating the message dump.", ephemeral=True)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Unexpected error in message_dump_command: %s', e)
        await interaction.followup.send("An unexpected error occurred.", ephemeral=True)

message_dump_error = _command_error_handler
_DANGEROUS_PERM_ATTRS = (
    'administrator', 'ban_members', 'kick_members', 'manage_roles',
    'manage_guild', 'manage_channels', 'manage_messages', 'moderate_members',
)


def _has_dangerous_perms(role):
    """True if Discord blocks bots from managing this role."""
    perms = role.permissions
    return any(getattr(perms, a) for a in _DANGEROUS_PERM_ATTRS)


def _classify_perm_target(target, bot_top_role):
    """Decide how a permission target should be handled.

    Returns one of: 'process', 'skip_other', 'skip_admin', 'skip_managed',
    'skip_hierarchy', 'skip_dangerous'.
    """
    if not isinstance(target, (discord.Member, discord.Role)):
        return 'skip_other'
    if isinstance(target, discord.Role):
        if target.permissions.administrator:
            return 'skip_admin'
        if target.managed:
            return 'skip_managed'
        if bot_top_role and target >= bot_top_role:
            return 'skip_hierarchy'
        if _has_dangerous_perms(target):
            return 'skip_dangerous'
    return 'process'


def _empty_counts():
    return {'processed': 0, 'skipped_admin': 0, 'skipped_managed': 0,
            'skipped_hierarchy': 0, 'skipped_dangerous': 0, 'failed': 0}


async def _apply_to_overwrites(obj, action_label, items, apply_fn):
    """Iterate (target, overwrite_or_None) pairs and apply apply_fn(target, ow).

    Filters out admin/managed/hierarchy/dangerous targets. Returns counts.
    """
    bot_member = obj.guild.me
    bot_top_role = bot_member.top_role if bot_member else None
    counts = _empty_counts()

    for target, overwrite in items:
        verdict = _classify_perm_target(target, bot_top_role)
        if verdict == 'skip_other':
            continue
        if verdict != 'process':
            counts[verdict] += 1
            logger.info('Skipped %s for %s on %s (%s)',
                        action_label, target, obj.name, verdict)
            continue
        try:
            await apply_fn(target, overwrite)
            counts['processed'] += 1
            logger.info('%s permissions for %s on %s',
                        action_label.capitalize(), target, obj.name)
        except discord.Forbidden as e:
            # Counted separately: this used to be folded into
            # skipped_hierarchy, so the summary blamed role hierarchy
            # for what is often a missing "Manage Roles" permission.
            counts['failed'] += 1
            logger.error('Failed to %s permissions for %s: %s',
                         action_label, target, e)
        except discord.HTTPException as e:
            counts['failed'] += 1
            logger.error('Failed to %s permissions for %s: %s',
                         action_label, target, e)
    return counts


async def _clear_overwrites_on(obj):
    """Clear permission overwrites on a channel or category."""
    items = [(t, None) for t in list(obj.overwrites.keys())]
    return await _apply_to_overwrites(
        obj, 'clear', items,
        lambda t, _ow: obj.set_permissions(t, overwrite=None))


async def _copy_overwrites(src, dst):
    """Copy permission overwrites from src to dst."""
    items = list(src.overwrites.items())
    return await _apply_to_overwrites(
        dst, 'copy', items,
        lambda t, ow: dst.set_permissions(t, overwrite=ow))


async def _mirror_overwrites(src, dst):
    """Make dst's overwrites match src's without ever baring dst.

    Applies src's overwrites first, then drops only the targets dst has
    that src does not. Clearing first (the previous approach) left a
    window where dst had no overwrites at all — and if every copy then
    failed, a private channel stayed wide open while the command still
    reported success.
    """
    counts = await _copy_overwrites(src, dst)

    stale = [(t, None) for t in dst.overwrites
             if t not in src.overwrites]
    if stale:
        removed = await _apply_to_overwrites(
            dst, 'clear', stale,
            lambda t, _ow: dst.set_permissions(t, overwrite=None))
        for key in ('skipped_admin', 'skipped_managed',
                    'skipped_hierarchy', 'skipped_dangerous', 'failed'):
            counts[key] += removed[key]
    return counts


def _format_skip_notes(counts):
    notes = []
    if counts['skipped_admin']:
        notes.append(
            f'Skipped {counts["skipped_admin"]} Administrator role(s) '
            f'for security reasons')
    if counts['skipped_managed']:
        notes.append(
            f'Skipped {counts["skipped_managed"]} managed role(s) '
            f'(bot roles, booster roles, etc.)')
    if counts['skipped_hierarchy']:
        notes.append(
            f'Skipped {counts["skipped_hierarchy"]} role(s) due to '
            f'hierarchy')
    if counts['skipped_dangerous']:
        notes.append(
            f"Skipped {counts['skipped_dangerous']} role(s) due to "
            f"Discord's restrictions on bots managing roles with "
            f"moderation permissions")
    return notes


_PERM_FORBIDDEN_HELP = (
    'I don\'t have permission to manage permissions on one or both '
    'targets.\n\n'
    '**Possible causes:**\n'
    '• I lack "Manage Channels" permission\n'
    '• I lack "Manage Roles" permission\n'
    '• My role is not high enough in the hierarchy\n'
    '• Discord restricts bots from managing roles with moderation '
    'permissions (ban_members, kick_members, etc.)\n\n'
    '**Solutions:**\n'
    '• Ensure I have "Manage Channels" and "Manage Roles" permissions\n'
    '• Move my role higher in Server Settings > Roles\n'
    '• For roles with moderation permissions, copy them manually'
)


async def _clone_permissions_command(interaction, src, dst, kind):
    """Shared implementation for clone_category/clone_channel."""
    try:
        await interaction.response.defer(ephemeral=True)

        if src.guild.id != dst.guild.id:
            await interaction.followup.send(
                f'Source and destination {kind}s must be in the same '
                f'server.', ephemeral=True)
            return

        await interaction.followup.send(
            f'Copying permissions from {src.name} to {dst.name}...',
            ephemeral=True)
        counts = await _mirror_overwrites(src, dst)

        if counts['failed']:
            success_msg = (
                f'⚠️ **Partially cloned** permissions from **{src.name}** '
                f'to **{dst.name}**.\n'
                f'Applied {counts["processed"]} override(s), '
                f'**{counts["failed"]} failed**.\n'
                f'{dst.name} may not be fully protected — check it '
                f'before relying on it.')
        else:
            success_msg = (
                f'Successfully cloned permissions from **{src.name}** to '
                f'**{dst.name}**.\n'
                f'Copied {counts["processed"]} permission overrides.')
        notes = _format_skip_notes(counts)
        if notes:
            success_msg += f'\n\n **Note:** {", ".join(notes)}.'
        await interaction.followup.send(success_msg, ephemeral=True)

    except discord.Forbidden:
        await interaction.followup.send(_PERM_FORBIDDEN_HELP, ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error in clone_%s_permissions: %s', kind, e)
        await interaction.followup.send(
            'A Discord API error occurred. Probably rate limiting.',
            ephemeral=True)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Unexpected error in clone_%s_permissions: %s', kind, e)
        await interaction.followup.send(
            'An unexpected error occurred while cloning permissions.',
            ephemeral=True)


async def _clear_permissions_command(interaction, obj, kind):
    """Shared implementation for clear_category/clear_channel."""
    try:
        await interaction.response.defer(ephemeral=True)

        await interaction.followup.send(
            f'Clearing all permission overwrites from **{obj.name}**...',
            ephemeral=True)
        counts = await _clear_overwrites_on(obj)

        if counts['failed']:
            success_msg = (
                f'⚠️ **Partially cleared** permissions from **{obj.name}**.\n'
                f'Cleared {counts["processed"]} overwrite(s), '
                f'**{counts["failed"]} failed**.')
        else:
            success_msg = (
                f'Successfully cleared permissions from **{obj.name}**.\n'
                f'Cleared {counts["processed"]} permission overwrites.')
        notes = _format_skip_notes(counts)
        if notes:
            success_msg += f'\n\n **Note:** {", ".join(notes)}.'
        await interaction.followup.send(success_msg, ephemeral=True)

    except discord.Forbidden:
        await interaction.followup.send(_PERM_FORBIDDEN_HELP, ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error in clear_%s_permissions: %s', kind, e)
        await interaction.followup.send(
            'A Discord API error occurred while clearing permissions.',
            ephemeral=True)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Unexpected error in clear_%s_permissions: %s', kind, e)
        await interaction.followup.send(
            'An unexpected error occurred while clearing permissions.',
            ephemeral=True)


async def clone_category_permissions(interaction: discord.Interaction,
                                     source_category: discord.CategoryChannel,
                                     destination_category: discord.CategoryChannel):
    """Clone permissions from source category to destination category."""
    await _clone_permissions_command(
        interaction, source_category, destination_category, kind='category')

clone_category_permissions_error = _command_error_handler
async def clone_channel_permissions(interaction: discord.Interaction,
                                    source_channel: discord.abc.GuildChannel,
                                    destination_channel: discord.abc.GuildChannel):
    """Clone permissions from source channel to destination channel."""
    await _clone_permissions_command(
        interaction, source_channel, destination_channel, kind='channel')

clone_channel_permissions_error = _command_error_handler
async def clone_role_permissions(interaction: discord.Interaction,  # pylint: disable=too-many-return-statements,too-many-branches,too-many-statements
                               source_role: discord.Role,
                               destination_role: discord.Role):
    """Clone permissions from source role to destination role."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Verify both roles are in the same guild
        if source_role.guild.id != destination_role.guild.id:
            await interaction.followup.send(
                'Source and destination roles must be in the same server.',
                ephemeral=True
            )
            return
        
        # Check if we're trying to clone to/from @everyone role
        if source_role.is_default() or destination_role.is_default():
            await interaction.followup.send(
                'Cannot clone permissions to or from the @everyone role.',
                ephemeral=True
            )
            return
        
        # Check if we're trying to clone to/from a role higher than the bot's highest role
        if interaction.guild:
            bot_member = interaction.guild.me
            if bot_member:
                if source_role >= bot_member.top_role:
                    await interaction.followup.send(
                        f'Cannot clone permissions from **{source_role.name}** - it is higher than or equal to my highest role (**{bot_member.top_role.name}**).\n'
                        f'Please move my role higher in the server settings, or choose a different source role.',
                        ephemeral=True
                    )
                    return
                if destination_role >= bot_member.top_role:
                    await interaction.followup.send(
                        f'Cannot clone permissions to **{destination_role.name}** - it is higher than or equal to my highest role (**{bot_member.top_role.name}**).\n'
                        f'Please move my role higher in the server settings, or choose a different destination role.',
                        ephemeral=True
                    )
                    return
        
        # Check if we're trying to clone to/from a role higher than the user's highest role
        # interaction.user might be a User, we need to get the Member object
        if interaction.guild and hasattr(interaction, 'user'):
            member = interaction.guild.get_member(interaction.user.id)
            if member:
                if source_role >= member.top_role:
                    await interaction.followup.send(
                        f'Cannot clone permissions from **{source_role.name}** - it is higher than or equal to your highest role (**{member.top_role.name}**).',
                        ephemeral=True
                    )
                    return
                if destination_role >= member.top_role:
                    await interaction.followup.send(
                        f'Cannot clone permissions to **{destination_role.name}** - it is higher than or equal to your highest role (**{member.top_role.name}**).',
                        ephemeral=True
                    )
                    return
        
        # Check if the bot has manage_roles permission
        if interaction.guild and interaction.guild.me:
            bot_permissions = interaction.guild.me.guild_permissions
            if not bot_permissions.manage_roles:
                await interaction.followup.send(
                    'I do not have the "Manage Roles" permission required to clone role permissions.\n'
                    'Please grant me this permission in the server settings.',
                    ephemeral=True
                )
                return
        
        await interaction.followup.send(
            f'Cloning permissions from **{source_role.name}** to **{destination_role.name}**...',
            ephemeral=True
        )
        
        # Copy the permissions from source role to destination role (excluding Administrator)
        try:
            # Strip every permission clear_role_permissions refuses to
            # touch, not just Administrator. Otherwise this command
            # hands out ban_members/manage_roles/etc. via the bot —
            # letting a moderator grant permissions they may not hold
            # themselves — while the matching clear command declines
            # to manage those same roles.
            new_permissions = discord.Permissions(source_role.permissions.value)
            excluded = [attr for attr in _DANGEROUS_PERM_ATTRS
                        if getattr(source_role.permissions, attr)]
            for attr in _DANGEROUS_PERM_ATTRS:
                setattr(new_permissions, attr, False)

            await destination_role.edit(
                permissions=new_permissions,
                reason=f'Permissions cloned from {source_role.name} by {interaction.user} (moderation permissions excluded)'
            )

            success_msg = (
                f'Successfully cloned permissions from **{source_role.name}** to **{destination_role.name}**.\n'
                f'The destination role now has the same server-wide permissions as the source role.'
            )

            if excluded:
                success_msg += (
                    '\n\n **Note:** These permissions were excluded for '
                    'security reasons: ' + ', '.join(excluded) + '. '
                    'Grant them manually if you intend to.')
            
            await interaction.followup.send(success_msg, ephemeral=True)
            
            logger.info('Cloned permissions from role %s to role %s by user %s',
                       source_role.name, destination_role.name, interaction.user)
            
        except discord.Forbidden as e:
            error_msg = (
                f'Failed to clone permissions: Missing permissions.\n\n'
                f'**Possible causes:**\n'
                f'• My role is not high enough in the hierarchy to modify **{destination_role.name}**\n'
                f'• I lack the "Manage Roles" permission\n'
                f'• The destination role has special permissions I cannot modify\n\n'
                f'**Solutions:**\n'
                f'• Move my role above **{destination_role.name}** in Server Settings > Roles\n'
                f'• Ensure I have "Manage Roles" permission\n'
                f'• Try cloning to a role lower in the hierarchy'
            )
            logger.error('Failed to clone role permissions due to insufficient permissions: %s', e)
            await interaction.followup.send(error_msg, ephemeral=True)
        except discord.HTTPException as e:
            logger.error('Failed to clone role permissions: %s', e)
            await interaction.followup.send(
                f'Failed to clone permissions due to a Discord API error: {e}',
                ephemeral=True
            )
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage one or both of these roles.',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clone_role_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred. Probably rate limiting. '
            'Wait a moment and try again.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clone_role_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while cloning permissions.',
            ephemeral=True
        )

clone_role_permissions_error = _command_error_handler
async def clear_category_permissions(interaction: discord.Interaction,
                                     category: discord.CategoryChannel):
    """Clear all permission overwrites from a category."""
    await _clear_permissions_command(interaction, category, kind='category')

clear_category_permissions_error = _command_error_handler
async def clear_channel_permissions(interaction: discord.Interaction,
                                    channel: discord.abc.GuildChannel):
    """Clear all permission overwrites from a channel."""
    await _clear_permissions_command(interaction, channel, kind='channel')

clear_channel_permissions_error = _command_error_handler
async def clear_role_permissions(interaction: discord.Interaction,  # pylint: disable=too-many-branches
                               role: discord.Role):
    """Clear all permissions from a role (reset to default)."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Check if we're trying to clear the @everyone role
        if role.is_default():
            await interaction.followup.send(
                'Cannot clear permissions from the @everyone role.',
                ephemeral=True
            )
            return
        
        # Check if we're trying to clear a role higher than the bot's highest role
        if interaction.guild:
            bot_member = interaction.guild.me
            if bot_member:
                if role >= bot_member.top_role:
                    await interaction.followup.send(
                        f'Cannot clear permissions from **{role.name}** - it is higher than or equal to my highest role (**{bot_member.top_role.name}**).\n'
                        f'Please move my role higher in the server settings, or choose a different role.',
                        ephemeral=True
                    )
                    return
        
        # Check if we're trying to clear a role higher than the user's highest role
        if interaction.guild and hasattr(interaction, 'user'):
            member = interaction.guild.get_member(interaction.user.id)
            if member:
                if role >= member.top_role:
                    await interaction.followup.send(
                        f'Cannot clear permissions from **{role.name}** - it is higher than or equal to your highest role (**{member.top_role.name}**).',
                        ephemeral=True
                    )
                    return
        
        # Check if the bot has manage_roles permission
        if interaction.guild and interaction.guild.me:
            bot_permissions = interaction.guild.me.guild_permissions
            if not bot_permissions.manage_roles:
                await interaction.followup.send(
                    'I do not have the "Manage Roles" permission required to clear role permissions.\n'
                    'Please grant me this permission in the server settings.',
                    ephemeral=True
                )
                return
        
        # Check for Discord's restricted permissions
        dangerous_perms = [
            role.permissions.ban_members,
            role.permissions.kick_members,
            role.permissions.manage_roles,
            role.permissions.manage_guild,
            role.permissions.manage_channels,
            role.permissions.manage_messages,
            role.permissions.moderate_members,
            role.permissions.administrator
        ]
        
        if any(dangerous_perms):
            await interaction.followup.send(
                f' **Discord Restriction**: Cannot clear permissions from role **{role.name}** because Discord prevents bots from managing roles with moderation permissions like ban_members, kick_members, manage_roles, etc. You\'ll need to clear these permissions manually.',
                ephemeral=True
            )
            return
        
        await interaction.followup.send(
            f'Clearing all permissions from **{role.name}**...',
            ephemeral=True
        )
        
        # Reset the role to default permissions (no permissions)
        try:
            default_permissions = discord.Permissions.none()
            
            await role.edit(
                permissions=default_permissions,
                reason=f'Permissions cleared by {interaction.user}'
            )
            
            success_msg = (
                f'Successfully cleared all permissions from **{role.name}**.\n'
                f'The role now has no special permissions (default state).'
            )
            
            await interaction.followup.send(success_msg, ephemeral=True)
            
            logger.info('Cleared permissions from role %s by user %s',
                       role.name, interaction.user)
            
        except discord.Forbidden as e:
            error_msg = (
                f'Failed to clear permissions: Missing permissions.\n\n'
                f'**Possible causes:**\n'
                f'• My role is not high enough in the hierarchy to modify **{role.name}**\n'
                f'• I lack the "Manage Roles" permission\n'
                f'• The role has special permissions I cannot modify\n'
                f'• Discord restricts bots from managing roles with moderation permissions\n\n'
                f'**Solutions:**\n'
                f'• Move my role above **{role.name}** in Server Settings > Roles\n'
                f'• Ensure I have "Manage Roles" permission\n'
                f'• For roles with moderation permissions, you\'ll need to clear manually'
            )
            logger.error('Failed to clear role permissions due to insufficient permissions: %s', e)
            await interaction.followup.send(error_msg, ephemeral=True)
        except discord.HTTPException as e:
            logger.error('Failed to clear role permissions: %s', e)
            await interaction.followup.send(
                f'Failed to clear permissions due to a Discord API error: {e}',
                ephemeral=True
            )
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage this role.',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clear_role_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while clearing permissions.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clear_role_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while clearing permissions.',
            ephemeral=True
        )

clear_role_permissions_error = _command_error_handler
async def sync_channel_perms(interaction: discord.Interaction,
                             source_category: discord.CategoryChannel):
    """Sync each channel in source_category with the category's permissions."""
    try:
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild:
            await interaction.followup.send(
                'This command can only be used in a server.', ephemeral=True)
            return

        channels_in_category = source_category.channels
        if not channels_in_category:
            await interaction.followup.send(
                f'No channels found in category **{source_category.name}**.',
                ephemeral=True)
            return

        await interaction.followup.send(
            f'Syncing permissions for {len(channels_in_category)} channel(s) '
            f'in **{source_category.name}**...', ephemeral=True)

        synced_count = 0
        total_overwrites_synced = 0
        failed_channels = []

        for channel in channels_in_category:
            try:
                counts = await _mirror_overwrites(source_category, channel)
                total_overwrites_synced += counts['processed']
                if counts['failed']:
                    failed_channels.append(
                        f"{channel.name} ({counts['failed']} override(s))")
                else:
                    synced_count += 1
                logger.info(
                    'Synced permissions for channel %s in category %s '
                    '(copied: %d, failed: %d)',
                    channel.name, source_category.name,
                    counts['processed'], counts['failed'])
            except (discord.Forbidden, discord.HTTPException) as e:
                failed_channels.append(f"{channel.name} ({type(e).__name__})")
                logger.error('Failed to sync permissions for channel %s: %s',
                             channel.name, e)

        success_msg = (
            f'Successfully synced permissions for **{synced_count}** out of '
            f'**{len(channels_in_category)}** channel(s) in '
            f'**{source_category.name}**.\n'
            f'Total permission overwrites synced: **{total_overwrites_synced}**')

        if failed_channels:
            success_msg += (
                f'\n\n **Failed to sync {len(failed_channels)} channel(s):**\n'
                + '\n'.join(f'• {name}' for name in failed_channels[:10]))
            if len(failed_channels) > 10:
                success_msg += f'\n... and {len(failed_channels) - 10} more'

        success_msg += (
            '\n\n📝 **Note:** Skipped Administrator roles, managed roles, '
            'and roles with moderation permissions for security reasons.')

        await interaction.followup.send(success_msg, ephemeral=True)

    except discord.Forbidden:
        await interaction.followup.send(_PERM_FORBIDDEN_HELP, ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error in sync_channel_perms: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while syncing permissions.',
            ephemeral=True)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Unexpected error in sync_channel_perms: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while syncing permissions.',
            ephemeral=True)

sync_channel_perms_error = _command_error_handler
async def list_users_without_roles(interaction: discord.Interaction):
    """Lists all users that do not have any server role assigned."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Check if guild exists
        if not interaction.guild:
            await interaction.followup.send('This command can only be used in a server.', ephemeral=True)
            return
        
        # Get all members in the guild
        guild = interaction.guild
        
        # Find users without any roles (only have @everyone role)
        users_without_roles = []
        
        for member in guild.members:
            # Skip bots
            if member.bot:
                continue
            
            # Check if member only has the @everyone role
            # member.roles includes @everyone, so if they only have 1 role, it's just @everyone
            if len(member.roles) == 1:
                users_without_roles.append(member)
        
        # If no users found without roles
        if not users_without_roles:
            await interaction.followup.send(
                'All users in this server have at least one role assigned.',
                ephemeral=True
            )
            return
        
        # Format the response
        user_count = len(users_without_roles)
        
        # Create embed for better formatting
        embed = discord.Embed(
            title=f"Users Without Roles ({user_count})",
            description=f"Found {user_count} user(s) with no server roles assigned:",
            color=0xff9900
        )
        
        # Pack fields by measured length rather than a fixed count: a
        # field caps at 1024 characters and an embed at 25 fields, and
        # a long-display-name server overran both, raising HTTPException
        # that nothing here caught.
        max_fields = 20  # leaves headroom under the 25-field cap
        lines = [f"• {m.display_name} ({m.mention})"
                 for m in users_without_roles]
        fields = []
        current, current_len, first_index = [], 0, 0
        for i, line in enumerate(lines):
            if current and current_len + len(line) + 1 > 1000:
                fields.append((first_index, i - 1, current))
                current, current_len, first_index = [], 0, i
            current.append(line)
            current_len += len(line) + 1
        if current:
            fields.append((first_index, len(lines) - 1, current))

        truncated = len(fields) > max_fields
        for start, end, chunk in fields[:max_fields]:
            embed.add_field(name=f"Users {start + 1}-{end + 1}",
                            value='\n'.join(chunk), inline=False)

        footer = ("Note: This list excludes bots and only shows users "
                  "with no roles beyond @everyone")
        if truncated:
            shown = fields[max_fields - 1][1] + 1
            footer = (f"Showing the first {shown} of {user_count}. " + footer)
        embed.set_footer(text=footer)

        await interaction.followup.send(embed=embed, ephemeral=True)
        
        logger.info('Listed %d users without roles for user %s in guild %s',
                   user_count, interaction.user, guild.name)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to view server members.\n'
            'Please ensure I have the "View Server Members" permission.',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in list_users_without_roles: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while building the list.',
            ephemeral=True
        )

async def assign_role(interaction: discord.Interaction, role: discord.Role,  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
                     members: str):
    """Assigns a role to multiple users at once."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        if not interaction.guild:
            await interaction.followup.send('This command can only be used in a server.', ephemeral=True)
            return
        if not interaction.guild.me or not interaction.guild.me.guild_permissions.manage_roles:
            await interaction.followup.send('I do not have permission to manage roles.', ephemeral=True)
            return
        if not await _check_role_hierarchy(interaction, role):
            return

        member_objects, failed_to_find = _parse_members(
            interaction.guild, members)
        if not member_objects:
            await interaction.followup.send(
                'No valid members found to assign the role to. Please mention users or provide valid user IDs.',
                ephemeral=True)
            return

        # Assign role to each member
        assigned_members = []
        already_had_role = []
        failed_assignments = []
        
        for member in member_objects:
            try:
                # Check if member already has the role
                if role in member.roles:
                    already_had_role.append(member)
                    continue
                
                # Skip the bot itself
                if member == interaction.guild.me:
                    failed_assignments.append(f"{member.display_name} (cannot assign role to myself)")
                    continue
                
                await member.add_roles(role, reason=f"Mass role assignment by {interaction.user}")
                assigned_members.append(member)
                logger.info('Assigned role %s to member %s by user %s', role.name, member, interaction.user)
                
            except discord.Forbidden:
                failed_assignments.append(f"{member.display_name} (insufficient permissions)")
                logger.error('Failed to assign role %s to member %s: insufficient permissions', role.name, member)
            except discord.HTTPException as e:
                failed_assignments.append(f"{member.display_name} (API error)")
                logger.error('Failed to assign role %s to member %s: %s', role.name, member, e)
        
        # Build response message
        response_parts = []
        
        # Every list is truncated: an uncapped roster of ~60+ names blew
        # past Discord's 2000-char limit, so the send raised and a fully
        # successful mass assignment was reported as an API error.
        if assigned_members:
            assigned_list = _format_names_inline(assigned_members)
            response_parts.append(f' **Successfully assigned {role.mention} to {len(assigned_members)} member(s):** {assigned_list}')

        if already_had_role:
            already_had_list = _format_names_inline(already_had_role)
            response_parts.append(f' **Already had the role ({len(already_had_role)} member(s)):** {already_had_list}')

        if failed_to_find:
            failed_find_list = _format_names_inline(failed_to_find)
            response_parts.append(f'❌ **Could not find:** {failed_find_list}')
        
        if failed_assignments:
            response_parts.append(
                f' **Failed to assign role to {len(failed_assignments)} member(s):**\n'
                + _format_list_with_overflow(failed_assignments))
        
        response_message = '\n\n'.join(response_parts)
        await interaction.followup.send(response_message, ephemeral=True)
        
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error in assign_role: %s', e)
        await interaction.followup.send('A Discord API error occurred.', ephemeral=True)

assign_role_error = _command_error_handler
async def remove_role(interaction: discord.Interaction, role: discord.Role,  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
                     members: str):
    """Removes a role from multiple users at once."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        if not interaction.guild:
            await interaction.followup.send('This command can only be used in a server.', ephemeral=True)
            return
        if not interaction.guild.me or not interaction.guild.me.guild_permissions.manage_roles:
            await interaction.followup.send('I do not have permission to manage roles.', ephemeral=True)
            return
        if not await _check_role_hierarchy(interaction, role):
            return

        member_objects, failed_to_find = _parse_members(
            interaction.guild, members)
        if not member_objects:
            await interaction.followup.send(
                'No valid members found to remove the role from. Please mention users or provide valid user IDs.',
                ephemeral=True)
            return

        # Remove role from each member
        removed_members = []
        didnt_have_role = []
        failed_removals = []
        
        for member in member_objects:
            try:
                # Check if member doesn't have the role
                if role not in member.roles:
                    didnt_have_role.append(member)
                    continue
                
                # Skip the bot itself
                if member == interaction.guild.me:
                    failed_removals.append(f"{member.display_name} (cannot remove role from myself)")
                    continue
                
                await member.remove_roles(role, reason=f"Mass role removal by {interaction.user}")
                removed_members.append(member)
                logger.info('Removed role %s from member %s by user %s', role.name, member, interaction.user)
                
            except discord.Forbidden:
                failed_removals.append(f"{member.display_name} (insufficient permissions)")
                logger.error('Failed to remove role %s from member %s: insufficient permissions', role.name, member)
            except discord.HTTPException as e:
                failed_removals.append(f"{member.display_name} (API error)")
                logger.error('Failed to remove role %s from member %s: %s', role.name, member, e)
        
        # Build response message
        response_parts = []
        
        # Truncated for the same reason as assign_role above
        if removed_members:
            removed_list = _format_names_inline(removed_members)
            response_parts.append(f' **Successfully removed {role.mention} from {len(removed_members)} member(s):** {removed_list}')

        if didnt_have_role:
            didnt_have_list = _format_names_inline(didnt_have_role)
            response_parts.append(f' **Didn\'t have the role ({len(didnt_have_role)} member(s)):** {didnt_have_list}')

        if failed_to_find:
            failed_find_list = _format_names_inline(failed_to_find)
            response_parts.append(f'❌ **Could not find:** {failed_find_list}')
        
        if failed_removals:
            response_parts.append(
                f' **Failed to remove role from {len(failed_removals)} member(s):**\n'
                + _format_list_with_overflow(failed_removals))
        
        response_message = '\n\n'.join(response_parts)
        await interaction.followup.send(response_message, ephemeral=True)
        
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error in remove_role: %s', e)
        await interaction.followup.send('A Discord API error occurred.', ephemeral=True)

remove_role_error = _command_error_handler
list_users_without_roles_error = _command_error_handler
async def voice_chaperone_command(interaction: discord.Interaction, enabled: bool):
    """Enable or disable the voice channel chaperone functionality."""
    try:
        config.VOICE_CHAPERONE_ENABLED = enabled

        released = 0
        if not enabled:
            # Turning the feature off used to leave anyone it had muted
            # muted until they next changed voice state — i.e. exactly
            # when a moderator overrides the safety system to let two
            # people talk, nothing happened. Release them now.
            released = await _release_all_chaperone_mutes()

        status = "enabled" if enabled else "disabled"
        current_status = "✅ Enabled" if config.VOICE_CHAPERONE_ENABLED else "❌ Disabled"
        
        released_note = (
            f'\nReleased {released} outstanding chaperone mute(s).'
            if released else '')
        await interaction.response.send_message(
            f'Voice channel chaperone functionality has been **{status}**.\n'
            f'Current status: {current_status}{released_note}\n\n'
            f'ℹ️ This setting controls whether the bot monitors voice channels for adult/child combinations '
            f'and takes protective action when only one adult and one child are present.',
            ephemeral=True
        )
        
        logger.info('Voice chaperone %s by user %s', status, interaction.user)
        
    except Exception as e:
        logger.error('Error in voice_chaperone command: %s', e)
        await interaction.response.send_message(
            'An error occurred while updating the voice chaperone setting.',
            ephemeral=True
        )

async def _release_all_chaperone_mutes():
    """Lift every mute the chaperone still holds. Returns the count.

    Lives here rather than in bot.py because the slash command needs it;
    the actual mute bookkeeping is bot.py's, so this imports it lazily to
    avoid a circular import at module load.
    """
    try:
        import bot as bot_module  # pylint: disable=cyclic-import
    except ImportError:
        return 0

    released = 0
    for guild in getattr(bot_module.bot, 'guilds', []):
        for channel in getattr(guild, 'voice_channels', []):
            for member in list(getattr(channel, 'members', [])):
                if (guild.id, member.id) in bot_module._chaperone_muted:  # pylint: disable=protected-access
                    await bot_module._unmute_member(  # pylint: disable=protected-access
                        member, 'voice chaperone disabled')
                    released += 1
    bot_module._chaperone_flagged.clear()  # pylint: disable=protected-access
    return released


voice_chaperone_error = _command_error_handler
def load_autoreplies():
    """Load autoreply rules from file."""
    if os.path.exists(AUTOREPLIES_FILE):
        try:
            with open(AUTOREPLIES_FILE, 'r', encoding='utf-8') as f:
                autoreplies.update(json.load(f))
                logger.info('Loaded %d autoreply rules', len(autoreplies))
        except (OSError, IOError, json.JSONDecodeError) as e:
            logger.error('Failed to read autoreplies file: %s', e)

def save_autoreplies():
    """Save autoreply rules to file."""
    if autoreplies_lock:
        with autoreplies_lock:
            try:
                _atomic_json_write(AUTOREPLIES_FILE, autoreplies)
            except (OSError, IOError) as e:
                logger.error('Failed to save autoreplies: %s', e)
                return False
    return True

def generate_autoreply_id(guild_id: int) -> str:
    """Generate a unique ID for an autoreply rule."""
    return f"{guild_id}_{uuid.uuid4().hex[:8]}"

async def check_message_for_autoreplies(message):
    """Check if a message should trigger any autoreply rules."""
    if not message.guild or message.author.bot:
        return
    
    guild_id = message.guild.id
    message_content = message.content
    
    if not autoreplies_lock:
        return
        
    # Copy matching rule data under the lock, then await outside it.
    # Awaiting inside a threading.Lock blocks the event loop and causes deadlocks.
    matched_rule_id = None
    matched_reply = None

    # Lowered once, not once per rule — this runs for every message in
    # every channel, and the loop previously re-lowered the whole
    # message body on each iteration.
    content_lower = message_content.lower()

    with autoreplies_lock:
        for rule_id, rule_data in autoreplies.items():
            # Skip if rule is disabled or for a different guild
            if (not rule_data.get('enabled', True)
                    or rule_data.get('guild_id') != guild_id):
                continue

            trigger_string = rule_data.get('trigger_string', '')
            if rule_data.get('case_sensitive', False):
                contains_trigger = trigger_string in message_content
            else:
                contains_trigger = trigger_string.lower() in content_lower

            if contains_trigger:
                matched_rule_id = rule_id
                matched_reply = rule_data.get('reply_string', '')
                break  # Only trigger the first matching rule

    if matched_rule_id:
        try:
            await message.reply(matched_reply, mention_author=False)
            logger.info('Autoreply triggered: rule %s in guild %s by user %s',
                       matched_rule_id, guild_id, message.author)
        except discord.HTTPException as e:
            logger.error('Failed to send autoreply for rule %s: %s', matched_rule_id, e)

async def autoreply_add_command(interaction: discord.Interaction, trigger: str, reply: str, case_sensitive: bool = False):
    """Add a new autoreply rule."""
    try:
        if not interaction.guild:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
            
        # Validate inputs
        if not trigger.strip():
            await interaction.response.send_message('Trigger string cannot be empty.', ephemeral=True)
            return
            
        if not reply.strip():
            await interaction.response.send_message('Reply string cannot be empty.', ephemeral=True)
            return
            
        if len(trigger) > 500:
            await interaction.response.send_message('Trigger string too long (max 500 characters).', ephemeral=True)
            return
            
        if len(reply) > 2000:
            await interaction.response.send_message('Reply string too long (max 2000 characters).', ephemeral=True)
            return
        
        guild_id = interaction.guild.id
        rule_id = generate_autoreply_id(guild_id)
        
        rule_data = {
            'trigger_string': trigger.strip(),
            'reply_string': reply.strip(),
            'guild_id': guild_id,
            'enabled': True,
            'case_sensitive': case_sensitive,
            'created_by': interaction.user.id,
            'created_at': datetime.now().isoformat()
        }
        
        if autoreplies_lock:
            with autoreplies_lock:
                autoreplies[rule_id] = rule_data
                
        if save_autoreplies():
            await interaction.response.send_message(
                f'✅ **Autoreply rule created successfully!**\n'
                f'**ID:** `{rule_id}`\n'
                f'**Trigger:** "{trigger}"\n'
                f'**Reply:** "{reply}"\n'
                f'**Case Sensitive:** {case_sensitive}',
                ephemeral=True
            )
            logger.info('Autoreply rule %s created by user %s in guild %s', rule_id, interaction.user, guild_id)
        else:
            await interaction.response.send_message('Failed to save autoreply rule. Please try again.', ephemeral=True)
            
    except Exception as e:
        logger.error('Error in autoreply_add_command: %s', e)
        await interaction.response.send_message('An error occurred while creating the autoreply rule.', ephemeral=True)

async def autoreply_list_command(interaction: discord.Interaction):
    """List all autoreply rules for the current guild."""
    try:
        if not interaction.guild:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
            
        guild_id = interaction.guild.id
        guild_rules = []
        
        if autoreplies_lock:
            with autoreplies_lock:
                for rule_id, rule_data in autoreplies.items():
                    if rule_data.get('guild_id') == guild_id:
                        guild_rules.append((rule_id, rule_data))
        
        if not guild_rules:
            await interaction.response.send_message('No autoreply rules found for this server.', ephemeral=True)
            return
        
        # Create embed with rule list
        embed = discord.Embed(
            title=f"Autoreply Rules ({len(guild_rules)})",
            description=f"All autoreply rules for {interaction.guild.name}:",
            color=0x00ff00
        )
        
        for rule_id, rule_data in guild_rules[:10]:  # Limit to 10 rules to avoid embed limits
            status = "✅ Enabled" if rule_data.get('enabled', True) else "❌ Disabled"
            case_sensitive = "Yes" if rule_data.get('case_sensitive', False) else "No"
            
            trigger = rule_data.get('trigger_string', '')
            reply = rule_data.get('reply_string', '')
            
            # Truncate long strings for display
            if len(trigger) > 100:
                trigger = trigger[:97] + "..."
            if len(reply) > 100:
                reply = reply[:97] + "..."
                
            embed.add_field(
                name=f"Rule: {rule_id}",
                value=f"**Status:** {status}\n**Trigger:** \"{trigger}\"\n**Reply:** \"{reply}\"\n**Case Sensitive:** {case_sensitive}",
                inline=False
            )
        
        if len(guild_rules) > 10:
            embed.set_footer(text=f"Showing first 10 of {len(guild_rules)} rules. Use /autoreply remove to manage specific rules.")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error('Error in autoreply_list_command: %s', e)
        await interaction.response.send_message('An error occurred while listing autoreply rules.', ephemeral=True)

async def autoreply_remove_command(interaction: discord.Interaction, rule_id: str):
    """Remove an autoreply rule."""
    try:
        if not interaction.guild:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
            
        guild_id = interaction.guild.id
        
        # Check if autoreplies system is available
        if not autoreplies_lock:
            await interaction.response.send_message('Autoreply system is not available. Please try again later.', ephemeral=True)
            return
        
        trigger = ''
        error_msg = None
        with autoreplies_lock:
            if rule_id not in autoreplies:
                error_msg = f'Autoreply rule `{rule_id}` not found.'
            elif autoreplies[rule_id].get('guild_id') != guild_id:
                error_msg = f'Autoreply rule `{rule_id}` not found in this server.'
            else:
                trigger = autoreplies[rule_id].get('trigger_string', '')
                del autoreplies[rule_id]

        if error_msg:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
                
        if save_autoreplies():
            await interaction.response.send_message(
                f'✅ **Autoreply rule removed successfully!**\n'
                f'**ID:** `{rule_id}`\n'
                f'**Trigger:** "{trigger}"',
                ephemeral=True
            )
            logger.info('Autoreply rule %s removed by user %s in guild %s', rule_id, interaction.user, guild_id)
        else:
            await interaction.response.send_message('Failed to save changes. Please try again.', ephemeral=True)
            
    except Exception as e:
        logger.error('Error in autoreply_remove_command: %s', e)
        await interaction.response.send_message('An error occurred while removing the autoreply rule.', ephemeral=True)

async def autoreply_toggle_command(interaction: discord.Interaction, rule_id: str):
    """Toggle an autoreply rule on/off."""
    try:
        if not interaction.guild:
            await interaction.response.send_message('This command can only be used in a server.', ephemeral=True)
            return
            
        guild_id = interaction.guild.id
        
        # Check if autoreplies system is available
        if not autoreplies_lock:
            await interaction.response.send_message('Autoreply system is not available. Please try again later.', ephemeral=True)
            return
        
        trigger = ''
        status_text = 'unknown'
        new_status = False
        
        error_msg = None
        with autoreplies_lock:
            if rule_id not in autoreplies:
                error_msg = f'Autoreply rule `{rule_id}` not found.'
            elif autoreplies[rule_id].get('guild_id') != guild_id:
                error_msg = f'Autoreply rule `{rule_id}` not found in this server.'
            else:
                rule_data = autoreplies[rule_id]
                # Toggle the enabled status
                current_status = rule_data.get('enabled', True)
                new_status = not current_status
                rule_data['enabled'] = new_status

                trigger = rule_data.get('trigger_string', '')
                status_text = "enabled" if new_status else "disabled"

        if error_msg:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return
                
        if save_autoreplies():
            await interaction.response.send_message(
                f'✅ **Autoreply rule {status_text}!**\n'
                f'**ID:** `{rule_id}`\n'
                f'**Trigger:** "{trigger}"\n'
                f'**Status:** {"✅ Enabled" if new_status else "❌ Disabled"}',
                ephemeral=True
            )
            logger.info('Autoreply rule %s %s by user %s in guild %s', rule_id, status_text, interaction.user, guild_id)
        else:
            await interaction.response.send_message('Failed to save changes. Please try again.', ephemeral=True)
            
    except Exception as e:
        logger.error('Error in autoreply_toggle_command: %s', e)
        await interaction.response.send_message('An error occurred while toggling the autoreply rule.', ephemeral=True)

# Dashboard state tracking for double confirmation
dashboard_confirmations: Dict[int, int] = {}  # {user_id: confirmation_count}
_background_tasks: set = set()  # prevent GC of fire-and-forget tasks

def get_command_categories():
    """Get all commands organized by category."""
    return {
        "🔔 Reminder Management": [
            "/set_reminder - Sets a reminder message to be sent at regular intervals",
            "/list_reminders - Lists all current reminders",
            "/delete_reminder - Deletes a reminder by title",
            "/delete_all_reminders - Deletes all active reminders"
        ],
        "⚖️ Moderation": [
            "/purge_last_messages - Purges a specified number of messages from a channel",
            "/purge_string - Purges all messages containing a specific string",
            "/purge_webhooks - Purges all messages sent by webhooks or apps",
            "/kick - Kicks one or more members from the server",
            "/kick_role - Kicks all members with a specified role",
            "/timeout - Timeouts a member for a specified duration",
            "/message_dump - Dump a user's messages into a downloadable file"
        ],
        "🔐 Permissions Management": [
            "/clone_category_permissions - Clone permissions from source to destination category",
            "/clone_channel_permissions - Clone permissions from source to destination channel",
            "/clone_role_permissions - Clone permissions from source to destination role",
            "/clear_category_permissions - Clear all permission overwrites from a category",
            "/clear_channel_permissions - Clear all permission overwrites from a channel",
            "/clear_role_permissions - Clear all permissions from a role",
            "/sync_channel_perms - Sync all channels in a category with category permissions"
        ],
        "👥 Role Management": [
            "/list_users_without_roles - Lists all users without any server role",
            "/assign_role - Assigns a role to multiple users at once",
            "/remove_role - Removes a role from multiple users at once"
        ],
        "🤖 Bot Interaction": [
            "/bot_mood - Check on the bot's current mood",
            "/pet_bot - Pet the bot",
            "/bot_pick_fav - See who the bot prefers today",
            "/botsay - Makes the bot send a message to a specified channel"
        ],
        "📅 Event Management": [
            "/add_event_feed - Adds a calendar or RSS feed URL to check for events",
            "/list_event_feeds - Lists all registered feeds with settings",
            "/remove_event_feed - Removes an event feed",
            "/check_event_feeds - Manually check all feeds for new events now"
        ],
        "⚙️ System & Utilities": [
            "/log_tail - DM the last specified number of lines of the bot log",
            "/voice_chaperone - Enable or disable voice channel chaperone functionality",
            "/nuke_protection - Enable or disable anti-nuke protection",
            "/spam_protection - Enable or disable message spam protection",
            "/dashboard - Display this command dashboard"
        ],
        "🛡️ Raid Protection": [
            "/raid protection - Enable or disable automatic raid detection",
            "/raid status - Show raid protection settings and lockdown state",
            "/raid lockdown - Manually pause or lift a pause on invites/DMs",
            "/raid recent_joins - List recent joins, flagging new accounts",
            "/raid kick_recent - Kick recently-joined new accounts (dry run by default)"
        ],
        "💾 Backup & Restore": [
            "/server_backup - DM you a full structural backup of this server",
            "/server_restore - Restore server structure from a backup file",
            "/auto_backup - Enable/disable automatic backups on an interval"
        ],
        "💬 Autoreply System": [
            "/autoreply add - Add a new autoreply rule",
            "/autoreply list - List all autoreply rules for this server",
            "/autoreply remove - Remove an autoreply rule",
            "/autoreply toggle - Enable or disable an autoreply rule"
        ]
    }

# Discord rejects any message content over 2000 characters. The full
# dashboard is ~2.9k, so it has to go out in pieces — it was previously
# sent as one string and the send raised HTTPException every time,
# meaning the command never once succeeded.
DASHBOARD_CHUNK_LIMIT = 1900  # headroom under Discord's 2000-char cap

_DASHBOARD_HEADER = (
    "# 📊 JohnnyBot Command Dashboard\n"
    "*All available slash commands organized by category*\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
)
_DASHBOARD_FOOTER = (
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "*Full command documentation: "
    "<https://github.com/BurbSec/JohnnyBot/wiki/Commands-Reference>*"
)


def _format_dashboard_sections():
    """One formatted block per category, header first and footer last."""
    sections = [_DASHBOARD_HEADER]
    for category, command_lines in get_command_categories().items():
        body = ''.join(f"• {line}\n" for line in command_lines)
        sections.append(f"## {category}\n{body}\n")
    sections.append(_DASHBOARD_FOOTER)
    return sections


def format_dashboard_messages():
    """Split the dashboard into Discord-sized chunks on category
    boundaries, so no category is ever cut in half."""
    messages = []
    current = ''
    for section in _format_dashboard_sections():
        if current and len(current) + len(section) > DASHBOARD_CHUNK_LIMIT:
            messages.append(current)
            current = ''
        current += section
    if current:
        messages.append(current)
    return messages


def format_dashboard_message():
    """The whole dashboard as one string.

    Kept for callers that just want the text; anything sending it to
    Discord must use format_dashboard_messages() instead, since this
    exceeds the 2000-character message limit."""
    return ''.join(_format_dashboard_sections())

async def dashboard_command(interaction: discord.Interaction):
    """Display the command dashboard with confirmation."""
    try:
        user_id = interaction.user.id
        
        # Check current confirmation state
        current_confirmations = dashboard_confirmations.get(user_id, 0)
        
        if current_confirmations == 0:
            # First confirmation request
            dashboard_confirmations[user_id] = 1
            await interaction.response.send_message(
                "⚠️ **WARNING: Large Dashboard Alert!** ⚠️\n\n"
                "This will post a **massive dashboard** with all available commands to the current channel.\n\n"
                "**Are you sure you want to continue?**\n"
                "Type `/dashboard` again to confirm and post.",
                ephemeral=True
            )
            logger.info('Dashboard command - confirmation requested by %s', interaction.user)
            
            # Reset confirmation after 60 seconds
            async def reset_confirmation():
                await asyncio.sleep(60)
                if dashboard_confirmations.get(user_id) == 1:
                    dashboard_confirmations[user_id] = 0
                    logger.info('Dashboard confirmation auto-reset for user %s', user_id)
            
            # Start the reset task (prevent GC by holding a reference)
            task = asyncio.create_task(reset_confirmation())
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            
        elif current_confirmations == 1:
            # Confirmed - post the dashboard
            dashboard_confirmations[user_id] = 0  # Reset
            
            # Respond to the interaction first
            await interaction.response.send_message(
                "✅ **Posting dashboard...**\n"
                "The command dashboard is being posted to this channel.",
                ephemeral=True
            )
            
            # Post to the channel (not ephemeral), one chunk per message
            for chunk in format_dashboard_messages():
                await interaction.channel.send(chunk)
            
            logger.info('Dashboard posted by %s in channel %s',
                       interaction.user,
                       getattr(interaction.channel, 'name', 'unknown'))
            
    except discord.Forbidden:
        # The confirmation branch above may have already used the
        # interaction's one initial response (e.g. channel.send() is
        # what raised) — sending again via response.send_message would
        # itself raise InteractionResponded and mask this message.
        await _send_or_followup(
            interaction,
            "I don't have permission to post messages in this channel.")
    except discord.HTTPException as e:
        logger.error('Discord API error in dashboard_command: %s', e)
        await _send_or_followup(
            interaction, "A Discord API error occurred while posting the dashboard.")
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Unexpected error in dashboard_command: %s', e)
        await _send_or_followup(
            interaction, "An unexpected error occurred while posting the dashboard.")

dashboard_command_error = _command_error_handler


# ---------------------------------------------------------------------------
# Server backup / restore
# ---------------------------------------------------------------------------

BACKUP_FORMAT_VERSION = 1
BACKUPS_DIR = os.path.join(os.path.dirname(__file__), 'backups')
os.makedirs(BACKUPS_DIR, exist_ok=True)
_ALLOWED_EMOJI_HOSTS = {'cdn.discordapp.com', 'media.discordapp.net'}
_DANGEROUS_PERM_MASK = discord.Permissions(
    **{attr: True for attr in _DANGEROUS_PERM_ATTRS}).value


def _serialize_overwrites(overwrites):
    """Serialize permission overwrites. Member-targeted overwrites are
    skipped — a restore target's membership rarely matches the source
    server's, so replaying them would silently apply the wrong grants.

    Roles are keyed by name because ids don't survive a cross-server
    clone. Discord permits duplicate role names, and a duplicate would
    resolve to an arbitrary one of them on restore, so those are dropped
    rather than guessed at."""
    seen = {}
    for target in overwrites:
        if isinstance(target, discord.Role):
            seen[target.name] = seen.get(target.name, 0) + 1
    ambiguous = {name for name, n in seen.items() if n > 1}
    if ambiguous:
        logger.warning(
            'Skipping overwrites for duplicated role name(s): %s',
            ', '.join(sorted(ambiguous)))

    entries = []
    for target, overwrite in overwrites.items():
        if not isinstance(target, discord.Role):
            continue
        if target.name in ambiguous:
            continue
        allow, deny = overwrite.pair()
        entries.append({
            'target_name': target.name,
            'allow': allow.value,
            'deny': deny.value,
        })
    return entries


def _serialize_role(role):
    return {
        'name': role.name,
        'color': role.colour.value,
        'permissions': role.permissions.value,
        'hoist': role.hoist,
        'mentionable': role.mentionable,
    }


def _serialize_channel(channel):
    entry = {
        'name': channel.name,
        'type': str(channel.type),
        'category': channel.category.name if channel.category else None,
        'overwrites': _serialize_overwrites(channel.overwrites),
    }
    if isinstance(channel, discord.TextChannel):
        entry['topic'] = channel.topic
        entry['nsfw'] = channel.nsfw
        entry['slowmode_delay'] = channel.slowmode_delay
    if isinstance(channel, discord.VoiceChannel):
        entry['bitrate'] = channel.bitrate
        entry['user_limit'] = channel.user_limit
    return entry


def build_backup_dict(guild):
    """Serialize a guild's structure (roles, categories, channels, emoji)
    into a JSON-safe dict. Message history, members, invites, audit logs,
    and boost state are never included — they can't be restored via the
    API and a backup that implied otherwise would be misleading."""
    roles = [_serialize_role(r) for r in guild.roles
             if not r.is_default() and not r.managed]
    categories = [
        {
            'name': c.name,
            'overwrites': _serialize_overwrites(c.overwrites),
        }
        for c in guild.categories
    ]
    channels = [
        _serialize_channel(ch) for ch in guild.channels
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel))
    ]
    emojis = [
        {'name': e.name, 'url': str(e.url), 'animated': e.animated}
        for e in guild.emojis
    ]
    return {
        'version': BACKUP_FORMAT_VERSION,
        'guild_id': guild.id,
        'guild_name': guild.name,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'roles': roles,
        'categories': categories,
        'channels': channels,
        'emojis': emojis,
    }


def _save_backup_file(guild, data, tag='backup'):
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'{tag}_{guild.id}_{timestamp}.json'
    path = os.path.join(BACKUPS_DIR, filename)
    _atomic_json_write(path, data)
    return path


async def _save_backup_file_async(guild, data, tag='backup'):
    """Off-loop variant. A whole-guild backup is a sizeable JSON dump,
    and serialising plus writing it inline stalls every other command
    and event handler for the duration — the reminder paths already use
    asyncio.to_thread for the same reason."""
    return await asyncio.to_thread(_save_backup_file, guild, data, tag)


def _role_differs(role, data):
    return (role.permissions.value != data['permissions']
            or role.colour.value != data['color']
            or role.hoist != data.get('hoist', False)
            or role.mentionable != data.get('mentionable', False))


def _diff_backup(guild, data):
    """Compare a backup against the guild's current state, matching
    roles/categories/channels/emoji by name so re-running a restore is
    idempotent instead of piling up duplicates."""
    plan = {
        'roles_create': [], 'roles_update': [],
        'categories_create': [],
        'channels_create': [],
        'emojis_create': [],
    }
    existing_roles = {r.name: r for r in guild.roles}
    for r in data.get('roles', []):
        cur = existing_roles.get(r['name'])
        if cur is None:
            plan['roles_create'].append(r['name'])
        # Compare against the *masked* permissions, the same way
        # _apply_backup does. Comparing raw values listed roles as
        # "to update" whose only difference was a moderation bit that
        # restore then correctly refused to apply — so the preview the
        # admin approved overstated what would happen.
        elif _role_differs(cur, {**r, 'permissions':
                                 r['permissions'] & ~_DANGEROUS_PERM_MASK}):
            plan['roles_update'].append(r['name'])

    existing_category_names = {c.name for c in guild.categories}
    for c in data.get('categories', []):
        if c['name'] not in existing_category_names:
            plan['categories_create'].append(c['name'])

    existing_channel_keys = {
        (ch.name, str(ch.type)) for ch in guild.channels
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel))
    }
    for ch in data.get('channels', []):
        if (ch['name'], ch['type']) not in existing_channel_keys:
            plan['channels_create'].append(ch['name'])

    existing_emoji_names = {e.name for e in guild.emojis}
    for e in data.get('emojis', []):
        if e['name'] not in existing_emoji_names:
            plan['emojis_create'].append(e['name'])

    return plan


def _plan_has_changes(plan):
    return any(plan[key] for key in
               ('roles_create', 'roles_update', 'categories_create',
                'channels_create', 'emojis_create'))


def _format_restore_plan(plan):
    lines = []

    def _fmt(label, items):
        if items:
            shown = ', '.join(items[:10])
            more = f' (+{len(items) - 10} more)' if len(items) > 10 else ''
            lines.append(f'• {label}: {len(items)} — {shown}{more}')

    _fmt('Roles to create', plan['roles_create'])
    _fmt('Roles to update', plan['roles_update'])
    _fmt('Categories to create', plan['categories_create'])
    _fmt('Channels to create', plan['channels_create'])
    _fmt('Emoji to create', plan['emojis_create'])
    if not lines:
        return 'No changes needed — this server already matches the backup.'
    lines.append(
        '\nExisting roles/categories/channels that already match by name '
        'will have their permission overwrites re-synced from the backup.')
    return '\n'.join(lines)


def _format_restore_results(counts):
    lines = [
        f"Roles: {counts['roles_created']} created, {counts['roles_updated']} updated",
        f"Categories created: {counts['categories_created']}",
        f"Channels created: {counts['channels_created']}",
        f"Permission overwrites synced: {counts['overwrites_synced']}",
        f"Emoji created: {counts['emojis_created']}",
    ]
    if counts.get('skipped_unsupported_type'):
        lines.append(
            f"Skipped {counts['skipped_unsupported_type']} channel(s) of "
            f"a type restore doesn't support")
    if counts.get('roles_skipped_unsafe'):
        lines.append(
            f"Skipped {counts['roles_skipped_unsafe']} role(s) that are "
            f"Administrator, managed, above my hierarchy, or have "
            f"moderation permissions — never modified by restore")
    if counts.get('roles_dangerous_perms_stripped'):
        lines.append(
            f"Stripped moderation-level permission bits (ban/kick/manage_*) "
            f"from {counts['roles_dangerous_perms_stripped']} role(s) in "
            f"the backup before applying them")
    if counts.get('overwrites_skipped_unsafe'):
        lines.append(
            f"Skipped {counts['overwrites_skipped_unsafe']} permission "
            f"overwrite(s) targeting Administrator/managed/hierarchy roles")
    if counts['failed']:
        lines.append(
            f"⚠️ {counts['failed']} operation(s) failed — check the log "
            f"for details")
        header = '⚠️ **Restore completed with errors.**'
    else:
        header = '✅ **Restore completed.**'
    return header + '\n' + '\n'.join(lines)


async def _sync_overwrites_from_data(obj, overwrite_entries, role_map):
    """Apply overwrites from backup data, routed through the same
    admin/managed/hierarchy/dangerous-perm gate `_apply_to_overwrites`
    already enforces for the clone_*_permissions commands — a restore
    file is untrusted input and must never be able to grant more than a
    manual clone would."""
    items = []
    for entry in overwrite_entries:
        role = role_map.get(entry['target_name'])
        if role is None:
            continue
        allow_value = entry['allow'] & ~_DANGEROUS_PERM_MASK
        overwrite = discord.PermissionOverwrite.from_pair(
            discord.Permissions(allow_value), discord.Permissions(entry['deny']))
        items.append((role, overwrite))
    return await _apply_to_overwrites(
        obj, 'restore-sync', items,
        lambda t, ow: obj.set_permissions(t, overwrite=ow))


def _merge_overwrite_counts(totals, ow_counts):
    totals['overwrites_synced'] += ow_counts['processed']
    totals['failed'] += ow_counts['failed']
    totals['overwrites_skipped_unsafe'] += sum(
        ow_counts[key] for key in
        ('skipped_admin', 'skipped_managed', 'skipped_hierarchy', 'skipped_dangerous'))


async def _create_channel_from_data(guild, ch, category):
    if ch['type'] == 'text':
        return await guild.create_text_channel(
            name=ch['name'], category=category, topic=ch.get('topic'),
            nsfw=ch.get('nsfw', False),
            slowmode_delay=ch.get('slowmode_delay', 0))
    if ch['type'] == 'voice':
        return await guild.create_voice_channel(
            name=ch['name'], category=category,
            bitrate=ch.get('bitrate') or 64000,
            user_limit=ch.get('user_limit', 0))
    raise ValueError(f"Unsupported channel type: {ch['type']}")


def _empty_restore_counts():
    return {'roles_created': 0, 'roles_updated': 0,
            'roles_skipped_unsafe': 0, 'roles_dangerous_perms_stripped': 0,
            'categories_created': 0, 'channels_created': 0,
            'overwrites_synced': 0, 'overwrites_skipped_unsafe': 0,
            'emojis_created': 0, 'skipped_unsupported_type': 0, 'failed': 0}


async def _apply_backup(guild, data):  # pylint: disable=too-many-branches,too-many-locals
    """Apply a backup to `guild`, matching existing objects by name so a
    repeat run updates in place instead of duplicating everything.

    A restore file is untrusted input — the same person who can invoke
    /server_restore could hand-edit one — so role/overwrite permissions
    are always routed through the dangerous-perm and hierarchy gates
    also used by the clone_*_permissions commands, never applied as-is.
    """
    counts = _empty_restore_counts()
    bot_top_role = guild.me.top_role if guild.me else None

    existing_roles = {r.name: r for r in guild.roles}
    role_map = dict(existing_roles)
    for r in data.get('roles', []):
        try:
            cur = existing_roles.get(r['name'])
            if cur is not None:
                verdict = _classify_perm_target(cur, bot_top_role)
                if verdict != 'process':
                    counts['roles_skipped_unsafe'] += 1
                    role_map[r['name']] = cur
                    logger.info('Skipped restoring role %s (%s)', r['name'], verdict)
                    continue

            requested_value = r['permissions']
            safe_value = requested_value & ~_DANGEROUS_PERM_MASK
            if safe_value != requested_value:
                counts['roles_dangerous_perms_stripped'] += 1
            perms = discord.Permissions(safe_value)
            colour = discord.Colour(r['color'])
            safe_data = {**r, 'permissions': safe_value}

            if cur is None:
                cur = await guild.create_role(
                    name=r['name'], permissions=perms, colour=colour,
                    hoist=r.get('hoist', False),
                    mentionable=r.get('mentionable', False))
                counts['roles_created'] += 1
            elif _role_differs(cur, safe_data):
                await cur.edit(permissions=perms, colour=colour,
                               hoist=r.get('hoist', False),
                               mentionable=r.get('mentionable', False))
                counts['roles_updated'] += 1
            role_map[r['name']] = cur
        except (discord.Forbidden, discord.HTTPException) as e:
            counts['failed'] += 1
            logger.error('Failed to create/update role %s: %s', r['name'], e)

    existing_categories = {c.name: c for c in guild.categories}
    category_map = dict(existing_categories)
    for c in data.get('categories', []):
        try:
            cur = existing_categories.get(c['name'])
            if cur is None:
                cur = await guild.create_category(c['name'])
                counts['categories_created'] += 1
            category_map[c['name']] = cur
            ow_counts = await _sync_overwrites_from_data(
                cur, c.get('overwrites', []), role_map)
            _merge_overwrite_counts(counts, ow_counts)
        except (discord.Forbidden, discord.HTTPException) as e:
            counts['failed'] += 1
            logger.error('Failed to create/sync category %s: %s', c['name'], e)

    existing_channels = {
        (ch.name, str(ch.type)): ch for ch in guild.channels
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel))
    }
    for ch in data.get('channels', []):
        try:
            key = (ch['name'], ch['type'])
            cur = existing_channels.get(key)
            category = category_map.get(ch['category']) if ch.get('category') else None
            if cur is None:
                cur = await _create_channel_from_data(guild, ch, category)
                counts['channels_created'] += 1
            ow_counts = await _sync_overwrites_from_data(
                cur, ch.get('overwrites', []), role_map)
            _merge_overwrite_counts(counts, ow_counts)
        except ValueError:
            counts['skipped_unsupported_type'] += 1
        except (discord.Forbidden, discord.HTTPException) as e:
            counts['failed'] += 1
            logger.error('Failed to create/sync channel %s: %s', ch['name'], e)

    existing_emoji_names = {e.name for e in guild.emojis}
    pending_emoji = [e for e in data.get('emojis', [])
                      if e['name'] not in existing_emoji_names]
    if pending_emoji:
        async with aiohttp.ClientSession() as session:
            for e in pending_emoji:
                if urlparse(e['url']).hostname not in _ALLOWED_EMOJI_HOSTS:
                    counts['failed'] += 1
                    logger.warning(
                        'Refusing to fetch emoji %s from non-Discord host: %s',
                        e['name'], e['url'])
                    continue
                try:
                    async with session.get(e['url']) as resp:
                        if resp.status != 200:
                            counts['failed'] += 1
                            continue
                        declared = resp.content_length
                        if declared is not None and declared > MAX_EMOJI_BYTES:
                            counts['failed'] += 1
                            logger.warning(
                                'Emoji %s is %d bytes, over the %d cap',
                                e['name'], declared, MAX_EMOJI_BYTES)
                            continue
                        image_bytes = await resp.content.read(
                            MAX_EMOJI_BYTES + 1)
                        if len(image_bytes) > MAX_EMOJI_BYTES:
                            counts['failed'] += 1
                            logger.warning(
                                'Emoji %s exceeded the %d byte cap',
                                e['name'], MAX_EMOJI_BYTES)
                            continue
                    await guild.create_custom_emoji(name=e['name'], image=image_bytes)
                    counts['emojis_created'] += 1
                except (discord.Forbidden, discord.HTTPException, aiohttp.ClientError) as ex:
                    counts['failed'] += 1
                    logger.error('Failed to create emoji %s: %s', e['name'], ex)

    return counts


class _RestoreConfirmView(discord.ui.View):
    """Confirm/cancel gate for /server_restore. Restoring recreates and
    edits roles, categories, channels, and emoji across the whole guild,
    so it never runs off a bare slash command without an explicit second
    click from the same moderator who invoked it."""

    def __init__(self, author_id):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.result = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the moderator who started this restore can confirm it.",
                ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Confirm Restore', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.result = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content='Restore starting...', view=self)
        self.stop()

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.result = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content='Restore cancelled.', view=self)
        self.stop()


async def server_backup_command(interaction: discord.Interaction):
    """Create a full structural backup of the server and DM it to the invoking moderator."""
    try:
        await interaction.response.defer(ephemeral=True)
        data = build_backup_dict(interaction.guild)
        path = await _save_backup_file_async(interaction.guild, data)
        try:
            note_bot_dm(interaction.user.id)
            await interaction.user.send(
                f'Backup of **{interaction.guild.name}** taken at {data["created_at"]}.',
                file=discord.File(path))
            await interaction.followup.send(
                'Backup complete — sent to your DMs.', ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "Backup complete, but I couldn't DM it to you (check your "
                f"privacy settings). It was saved on the bot host at `{path}`.",
                ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error in server_backup_command: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while creating the backup.', ephemeral=True)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Unexpected error in server_backup_command: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while creating the backup.', ephemeral=True)

server_backup_error = _command_error_handler


async def server_restore_command(interaction: discord.Interaction, backup_file: discord.Attachment):  # pylint: disable=too-many-return-statements
    """Restore server structure from a backup file, with a preview and confirmation gate."""
    try:
        await interaction.response.defer(ephemeral=True)

        if not backup_file.filename.endswith('.json'):
            await interaction.followup.send(
                'Please attach a .json backup file produced by /server_backup.',
                ephemeral=True)
            return

        if backup_file.size > MAX_BACKUP_BYTES:
            await interaction.followup.send(
                f'That file is {backup_file.size / 1024 / 1024:.1f} MB; '
                f'the limit is {MAX_BACKUP_BYTES // (1024 * 1024)} MB.',
                ephemeral=True)
            return

        raw = await backup_file.read()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await interaction.followup.send('That file is not valid JSON.', ephemeral=True)
            return

        if data.get('version') != BACKUP_FORMAT_VERSION:
            await interaction.followup.send(
                'Unrecognized backup format/version — this file was not '
                'produced by /server_backup.', ephemeral=True)
            return

        plan = _diff_backup(interaction.guild, data)
        summary = _format_restore_plan(plan)
        if not _plan_has_changes(plan):
            await interaction.followup.send(
                f'{summary}', ephemeral=True)
            return

        cross_guild_warning = ''
        if data.get('guild_id') != interaction.guild.id:
            cross_guild_warning = (
                f'\n\n⚠️ **This backup is from {data.get("guild_name", "an unknown server")} '
                f'(id {data.get("guild_id", "?")}), not this server ({interaction.guild.name}).** '
                f'Everything listed above will be newly created here — this is a cross-server '
                f'clone, not a same-server restore. Make sure that\'s what you intend.')

        view = _RestoreConfirmView(interaction.user.id)
        await interaction.followup.send(
            f'**Restore plan for backup of {data.get("guild_name", "?")} '
            f'taken {data.get("created_at", "unknown time")}:**\n{summary}\n\n'
            f'⚠️ This will create/update roles, categories, channels, and '
            f'emoji to match the backup. Member-specific overwrites, message '
            f'history, audit logs, and role/channel ordering are never '
            f'restored — new roles land at the bottom of the hierarchy and '
            f'must be reordered manually. Roles/overwrites that are '
            f'Administrator, managed, above my role, or carry moderation '
            f'permissions are never modified. A safety snapshot of the '
            f'current state will be DMed to you before any changes are '
            f'made — note that restore only creates and updates, it never '
            f'deletes, so applying that snapshot afterward will restore '
            f'names/permissions/overwrites but will NOT remove anything '
            f'this restore newly creates.{cross_guild_warning}',
            view=view, ephemeral=True)

        await view.wait()
        if not view.result:
            return

        pre_restore_data = build_backup_dict(interaction.guild)
        pre_path = await _save_backup_file_async(
            interaction.guild, pre_restore_data, tag='pre_restore')
        try:
            note_bot_dm(interaction.user.id)
            await interaction.user.send(
                'Safety snapshot taken automatically before your /server_restore. '
                'This records the prior names/permissions/overwrites so you can '
                'restore *from* it if something looks wrong — but /server_restore '
                'never deletes, so it will not remove anything the restore you\'re '
                'about to run newly creates.',
                file=discord.File(pre_path))
        except discord.Forbidden:
            await interaction.followup.send(
                "Couldn't DM you the pre-restore safety snapshot (check your "
                f"privacy settings) — proceeding anyway. It was saved on the "
                f"bot host at `{pre_path}` if you need to roll back.",
                ephemeral=True)

        counts = await _apply_backup(interaction.guild, data)
        await interaction.followup.send(_format_restore_results(counts), ephemeral=True)

    except discord.Forbidden:
        await interaction.followup.send(_PERM_FORBIDDEN_HELP, ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error in server_restore_command: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred. Probably rate limiting.', ephemeral=True)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Unexpected error in server_restore_command: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred during restore.', ephemeral=True)

server_restore_error = _command_error_handler


# ---------------------------------------------------------------------------
# Automatic server backups
# ---------------------------------------------------------------------------

AUTO_BACKUP_FILE = os.path.join(os.path.dirname(__file__), 'auto_backups.json')
AUTO_BACKUP_MIN_HOURS = 1
AUTO_BACKUP_MAX_HOURS = 24 * 30


def _backup_content_hash(data):
    """Stable hash of a backup's structural content, ignoring the
    always-changing `created_at` timestamp and list order — used to
    detect whether anything actually restorable changed since the last
    automatic backup. Position isn't captured by build_backup_dict, so
    a pure drag-reorder must not look like a content change."""
    stable = {
        'version': data.get('version'),
        'guild_id': data.get('guild_id'),
        'roles': sorted(data.get('roles', []), key=lambda r: r['name']),
        'categories': sorted(data.get('categories', []), key=lambda c: c['name']),
        'channels': sorted(data.get('channels', []), key=lambda c: c['name']),
        'emojis': sorted(data.get('emojis', []), key=lambda e: e['name']),
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode('utf-8')).hexdigest()


def _load_auto_backup_configs():
    """Load auto-backup configs from disk into the module-level dict."""
    if not os.path.exists(AUTO_BACKUP_FILE):
        return
    try:
        with open(AUTO_BACKUP_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for key, cfg in data.items():
            auto_backup_configs[int(key)] = cfg
        logger.info("Loaded %d auto-backup config(s) from disk", len(auto_backup_configs))
    except (OSError, IOError, json.JSONDecodeError) as e:
        logger.error('Failed to read auto-backup config file: %s', e)


def _save_auto_backup_configs():
    _atomic_json_write(
        AUTO_BACKUP_FILE,
        {str(k): v for k, v in auto_backup_configs.items()})


def _schedule_auto_backup(guild_id, cfg):
    """Register (or replace) the APScheduler job for one guild's auto-backup.

    Anchors the first fire to the last persisted check time, not "now" —
    IntervalTrigger otherwise fires interval-from-now on every job
    registration, which is called on every bot restart. Without this a
    daily-interval backup on a bot that restarts more than once a day
    would never actually fire.
    """
    if not scheduler or not scheduler.running:
        return
    interval = cfg['interval_seconds']
    last_checked = cfg.get('last_checked_epoch')
    now_ts = time_module.time()
    next_ts = max(last_checked + interval, now_ts) if last_checked else now_ts
    scheduler.add_job(
        _run_auto_backup,
        trigger=IntervalTrigger(seconds=interval),
        args=[guild_id],
        id=f'auto_backup_{guild_id}',
        replace_existing=True,
        next_run_time=datetime.fromtimestamp(next_ts),
        misfire_grace_time=min(interval, 3600),
        coalesce=True,
    )


def register_all_auto_backup_jobs():
    """Re-register all persisted auto-backup jobs. Called after scheduler.start()."""
    for guild_id, cfg in auto_backup_configs.items():
        _schedule_auto_backup(guild_id, cfg)
    if auto_backup_configs:
        logger.info("Registered %d auto-backup job(s) with scheduler", len(auto_backup_configs))


async def _run_auto_backup(guild_id: int):
    """APScheduler callback: back up `guild_id` only if its structure
    changed since the last automatic backup, then post it to the
    moderators channel."""
    if not bot_instance:
        return
    guild = bot_instance.get_guild(guild_id)
    if not guild:
        logger.warning('Auto-backup: guild %s not found, skipping', guild_id)
        return

    with auto_backup_lock:
        cfg = auto_backup_configs.get(guild_id)
    if not cfg:
        return

    data = build_backup_dict(guild)
    new_hash = _backup_content_hash(data)
    changed = new_hash != cfg.get('last_hash')
    path = (await _save_backup_file_async(guild, data, tag='auto')
            if changed else None)

    with auto_backup_lock:
        # Always persisted, even on a no-delta run, so a restart schedules
        # the next check relative to the last check rather than firing
        # interval-from-restart-time (see _schedule_auto_backup).
        cfg['last_checked_epoch'] = time_module.time()
        if changed:
            cfg['last_hash'] = new_hash
            cfg['last_backup_at'] = data['created_at']
        try:
            _save_auto_backup_configs()
        except (OSError, IOError) as e:
            logger.error('Failed to persist auto-backup state for guild %s: %s', guild_id, e)

    if not changed:
        logger.info('Auto-backup: no changes for guild %s, skipping', guild_id)
        return

    channel = discord.utils.get(guild.text_channels, name=config.MODERATORS_CHANNEL_NAME)
    if not channel:
        logger.warning(
            "Auto-backup: moderators channel '%s' not found in guild %s, "
            "backup saved to %s but not posted",
            config.MODERATORS_CHANNEL_NAME, guild_id, path)
        return
    try:
        await channel.send(
            '📦 Automatic backup taken — server structure changed since '
            'the last one.',
            file=discord.File(path))
    except discord.HTTPException as e:
        logger.error('Auto-backup: failed to post to moderators channel: %s', e)


async def auto_backup_command(interaction: discord.Interaction, enabled: bool,
                              interval_hours: app_commands.Range[
                                  int, AUTO_BACKUP_MIN_HOURS,
                                  AUTO_BACKUP_MAX_HOURS] = 24):
    """Enable/disable automatic backups for this server on an interval."""
    try:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id

        if not enabled:
            with auto_backup_lock:
                had_config = auto_backup_configs.pop(guild_id, None) is not None
                if had_config:
                    _save_auto_backup_configs()
            if scheduler:
                try:
                    scheduler.remove_job(f'auto_backup_{guild_id}')
                except JobLookupError:
                    pass
            msg = ('Automatic backups disabled for this server.' if had_config
                   else 'Automatic backups were not enabled for this server.')
            await interaction.followup.send(msg, ephemeral=True)
            return

        with auto_backup_lock:
            cfg = auto_backup_configs.get(guild_id, {'last_hash': None, 'last_backup_at': None})
            cfg['interval_seconds'] = interval_hours * 3600
            auto_backup_configs[guild_id] = cfg
            _save_auto_backup_configs()

        _schedule_auto_backup(guild_id, cfg)

        await interaction.followup.send(
            f'Automatic backups enabled for this server, checked every '
            f'{interval_hours} hour(s). A new backup is only created (and '
            f'posted to #{config.MODERATORS_CHANNEL_NAME}) when the server '
            f'structure has actually changed since the last one.',
            ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error in auto_backup_command: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred.', ephemeral=True)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error('Unexpected error in auto_backup_command: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred.', ephemeral=True)

auto_backup_error = _command_error_handler


# ---------------------------------------------------------------------------
# Raid protection
# ---------------------------------------------------------------------------
# Burst detection and the auto-lockdown live in bot.py's on_member_join,
# since only it sees the raw join events. These are the moderator-facing
# commands: toggle, status, manual lockdown, and reviewing/kicking the
# members who joined during an incident. Config is read with getattr()
# throughout — see the note above _raid_join_times in bot.py.

async def raid_protection_command(interaction: discord.Interaction, enabled: bool):
    """Enable or disable automatic raid detection.

    Disabling it also lifts any lockdown currently in progress — same
    reasoning as voice_chaperone_command releasing outstanding mutes on
    disable: a moderator turning the feature off mid-incident is telling
    the bot to stand down, not just to stop watching for new ones.
    """
    try:
        await interaction.response.defer(ephemeral=True)
        config.RAID_PROTECTION_ENABLED = enabled
        status = 'enabled' if enabled else 'disabled'

        lifted_note = ''
        if not enabled:
            import bot as bot_module  # pylint: disable=cyclic-import
            guild = interaction.guild
            if guild is not None and bot_module._raid_lockdown_active(guild):  # pylint: disable=protected-access
                try:
                    await guild.edit(
                        invites_disabled_until=None, dms_disabled_until=None,
                        reason=f'Raid protection disabled by {interaction.user}')
                    lifted_note = '\nAn active lockdown was also lifted.'
                except discord.Forbidden:
                    lifted_note = (
                        '\n⚠️ Could not lift the active lockdown — I lack '
                        'the Manage Server permission.')

        await interaction.followup.send(
            f'Raid protection has been **{status}**.{lifted_note}\n\n'
            f'ℹ️ When enabled, a burst of joins past the configured '
            f'threshold automatically pauses invites and DMs and alerts '
            f'moderators.',
            ephemeral=True)
        logger.info('Raid protection %s by user %s', status, interaction.user)
    except discord.HTTPException as e:
        logger.error('Error in raid_protection command: %s', e)
        await interaction.followup.send(
            'An error occurred while updating raid protection.', ephemeral=True)

raid_protection_error = _command_error_handler


async def raid_status_command(interaction: discord.Interaction):
    """Show raid protection settings and the current lockdown state."""
    try:
        import bot as bot_module  # pylint: disable=cyclic-import
        guild = interaction.guild
        enabled = getattr(config, 'RAID_PROTECTION_ENABLED', True)
        threshold = getattr(config, 'RAID_JOIN_THRESHOLD', 6)
        window = getattr(config, 'RAID_JOIN_WINDOW_SECONDS', 30)
        lockdown_minutes = getattr(config, 'RAID_LOCKDOWN_MINUTES', 60)
        new_hours = getattr(config, 'RAID_NEW_ACCOUNT_HOURS', 24)

        times = getattr(bot_module, '_raid_join_times', {}).get(guild.id, [])
        now = datetime.now(timezone.utc).timestamp()
        recent = sum(1 for t in times if now - t <= window)

        invites_until = getattr(guild, 'invites_paused_until', None)
        if invites_until and invites_until > datetime.now(timezone.utc):
            lock_status = (
                f'🔒 Locked down until <t:{int(invites_until.timestamp())}:f>')
        else:
            lock_status = '🔓 Not locked down'

        msg = (
            f"**Raid Protection:** {'✅ Enabled' if enabled else '❌ Disabled'}\n"
            f"**Trigger:** {threshold} joins within {window}s\n"
            f"**Lockdown duration:** {lockdown_minutes} minute(s)\n"
            f"**New-account flag:** accounts younger than {new_hours}h\n"
            f"**Current status:** {lock_status}\n"
            f"**Joins in the last {window}s:** {recent}")
        await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Error in raid_status command: %s', e)
        await interaction.response.send_message(
            'An error occurred while fetching raid status.', ephemeral=True)

raid_status_error = _command_error_handler


async def raid_lockdown_command(interaction: discord.Interaction, enabled: bool,
                                minutes: Optional[app_commands.Range[int, 1, 10080]] = None):
    """Manually pause (or lift a pause on) invites and DMs."""
    try:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if not guild.me or not guild.me.guild_permissions.manage_guild:
            await interaction.followup.send(
                'I need the **Manage Server** permission to pause '
                'invites/DMs.', ephemeral=True)
            return

        if enabled:
            mins = minutes or getattr(config, 'RAID_LOCKDOWN_MINUTES', 60)
            until = datetime.now(timezone.utc) + timedelta(minutes=mins)
            await guild.edit(
                invites_disabled_until=until, dms_disabled_until=until,
                reason=f'Manual raid lockdown by {interaction.user}')
            await interaction.followup.send(
                f'🔒 Invites and DMs paused for **{mins} minute(s)**.',
                ephemeral=True)
        else:
            await guild.edit(
                invites_disabled_until=None, dms_disabled_until=None,
                reason=f'Raid lockdown lifted by {interaction.user}')
            await interaction.followup.send('🔓 Lockdown lifted.', ephemeral=True)

        logger.info('Raid lockdown %s by %s',
                    'enabled' if enabled else 'disabled', interaction.user)
    except discord.Forbidden:
        await interaction.followup.send(
            'I lack permission to do that.', ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Error in raid_lockdown command: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred.', ephemeral=True)

raid_lockdown_error = _command_error_handler


async def raid_recent_joins_command(
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 1, 1440] = 30):
    """List members who joined recently, flagging new accounts."""
    try:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        new_hours = getattr(config, 'RAID_NEW_ACCOUNT_HOURS', 24)
        age_cutoff = datetime.now(timezone.utc) - timedelta(hours=new_hours)

        joined = sorted(
            (m for m in guild.members if m.joined_at and m.joined_at >= cutoff),
            key=lambda m: m.joined_at)

        if not joined:
            await interaction.followup.send(
                f'No members joined in the last {minutes} minute(s).',
                ephemeral=True)
            return

        lines = [
            f'{m.mention} ({m}) — joined <t:{int(m.joined_at.timestamp())}:R>'
            + (' ⚠️ new account' if m.created_at >= age_cutoff else '')
            for m in joined
        ]
        body = (f'**{len(joined)} member(s) joined in the last '
               f'{minutes} minute(s):**\n'
               + _format_list_with_overflow(lines, max_shown=25, prefix=''))

        await interaction.followup.send(body, ephemeral=True)

        # Same clustering the raid alert runs, on demand: tells a
        # moderator which of these accounts are the same operator, which
        # is what makes /raid kick_recent safe to reach for. Sent as a
        # second followup rather than appended — together they can exceed
        # Discord's 2000-character limit, and losing the join list to an
        # HTTPException mid-raid is the worst possible time for it.
        import bot as bot_module  # pylint: disable=cyclic-import
        report = await bot_module._avatar_cluster_report(joined)  # pylint: disable=protected-access
        if report.strip():
            await interaction.followup.send(report.strip(), ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Error in raid_recent_joins command: %s', e)
        await interaction.followup.send(
            'An error occurred while listing recent joins.', ephemeral=True)

raid_recent_joins_error = _command_error_handler


async def raid_kick_recent_command(  # pylint: disable=too-many-locals
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 1, 1440],
        dry_run: bool = True):
    """Kick recently-joined members flagged as new accounts.

    Defaults to a dry run — a real moderator can have joined five
    minutes ago too, so this previews candidates before anything
    irreversible happens. Moderators and the guild owner are always
    exempt, mirroring /kick's hierarchy check.
    """
    try:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if not guild.me or not guild.me.guild_permissions.kick_members:
            await interaction.followup.send(
                'I do not have permission to kick members.', ephemeral=True)
            return

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        new_hours = getattr(config, 'RAID_NEW_ACCOUNT_HOURS', 24)
        age_cutoff = datetime.now(timezone.utc) - timedelta(hours=new_hours)

        candidates = [
            m for m in guild.members
            if m.joined_at and m.joined_at >= cutoff
            and m.created_at >= age_cutoff
            and m.id != guild.me.id
            and not _is_moderator(m)
            and _invoker_outranks(interaction, m)
        ]

        if not candidates:
            await interaction.followup.send(
                f'No new-account members joined in the last {minutes} '
                f'minute(s) matching the raid criteria.', ephemeral=True)
            return

        if dry_run:
            lines = [
                f'{m.mention} ({m}) — account created '
                f'<t:{int(m.created_at.timestamp())}:R>'
                for m in candidates]
            body = (
                f'**Dry run — {len(candidates)} member(s) would be kicked:**\n'
                + _format_list_with_overflow(lines, max_shown=25, prefix='')
                + '\n\nRun again with `dry_run:False` to actually kick them.')
            await interaction.followup.send(body, ephemeral=True)
            return

        kicked, failed = [], []
        for m in candidates:
            try:
                await m.kick(
                    reason=f'Raid protection: kicked by {interaction.user}')
                kicked.append(str(m))
                logger.info('Raid protection: kicked %s (invoked by %s)',
                           m, interaction.user)
            except discord.Forbidden:
                failed.append(f'{m} (insufficient permissions)')
            except discord.HTTPException as e:
                failed.append(f'{m} (API error)')
                logger.error('Raid kick failed for %s: %s', m, e)

        parts = []
        if kicked:
            parts.append(
                f'✅ **Kicked {len(kicked)} member(s):** ' + ', '.join(kicked))
        if failed:
            parts.append(
                f'❌ **Failed to kick {len(failed)}:**\n'
                + _format_list_with_overflow(failed))
        await interaction.followup.send('\n\n'.join(parts), ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Error in raid_kick_recent command: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred.', ephemeral=True)

raid_kick_recent_error = _command_error_handler


def register_raid_commands():
    """Register the /raid command group."""
    if tree is None:
        return

    raid_group = app_commands.Group(
        name='raid', description='Raid detection and response')
    # default_permissions is ignored on subcommands, so the hint lives
    # on the group itself — see _apply_scope's docstring.
    _apply_scope(raid_group, gate_perms={'manage_messages': True})

    @raid_group.command(
        name='protection',
        description='Enable or disable automatic raid detection')
    @app_commands.describe(enabled='True to enable, False to disable')
    @_require_guild_permissions(manage_messages=True)
    async def _raid_protection(interaction: discord.Interaction, enabled: bool):
        await raid_protection_command(interaction, enabled)

    @raid_group.command(
        name='status',
        description='Show raid protection settings and lockdown state')
    @_require_guild_permissions(manage_messages=True)
    async def _raid_status(interaction: discord.Interaction):
        await raid_status_command(interaction)

    @raid_group.command(
        name='lockdown',
        description='Manually pause (or lift a pause on) invites and DMs')
    @app_commands.describe(
        enabled='True to lock down, False to lift an active lockdown',
        minutes='How long to lock down for (default: configured lockdown duration)')
    @_require_guild_permissions(manage_messages=True)
    async def _raid_lockdown(interaction: discord.Interaction, enabled: bool,
                             minutes: Optional[app_commands.Range[int, 1, 10080]] = None):
        await raid_lockdown_command(interaction, enabled, minutes)

    @raid_group.command(
        name='recent_joins',
        description='List members who joined recently, flagging new accounts')
    @app_commands.describe(minutes='How far back to look (default: 30)')
    @_require_guild_permissions(manage_messages=True)
    async def _raid_recent_joins(interaction: discord.Interaction,
                                 minutes: app_commands.Range[int, 1, 1440] = 30):
        await raid_recent_joins_command(interaction, minutes)

    @raid_group.command(
        name='kick_recent',
        description='Kick recently-joined new accounts (dry run by default)')
    @app_commands.describe(
        minutes='How far back to look for joins',
        dry_run='Preview candidates without kicking (default: True)')
    @_require_guild_permissions(manage_messages=True)
    async def _raid_kick_recent(interaction: discord.Interaction,
                                minutes: app_commands.Range[int, 1, 1440],
                                dry_run: bool = True):
        await raid_kick_recent_command(interaction, minutes, dry_run)

    _raid_protection.on_error = raid_protection_error
    _raid_status.on_error = raid_status_error
    _raid_lockdown.on_error = raid_lockdown_error
    _raid_recent_joins.on_error = raid_recent_joins_error
    _raid_kick_recent.on_error = raid_kick_recent_error

    tree.add_command(raid_group)


# ---------------------------------------------------------------------------
# Anti-nuke protection
# ---------------------------------------------------------------------------
# Detection and response live in bot.py's on_audit_log_entry_create, since
# only it receives the raw audit-log gateway event. This is the single
# moderator-facing toggle, mirroring voice_chaperone_command /
# raid_protection_command.

async def nuke_protection_command(interaction: discord.Interaction, enabled: bool):
    """Enable or disable anti-nuke protection."""
    try:
        config.ANTI_NUKE_ENABLED = enabled
        status = 'enabled' if enabled else 'disabled'
        response_mode = getattr(config, 'ANTI_NUKE_ACTION', 'strip_roles')
        mode_note = (
            'automatically stripping the offending account\'s roles'
            if response_mode == 'strip_roles'
            else 'alerting moderators only (no automated response)')
        await interaction.response.send_message(
            f'Anti-nuke protection has been **{status}**.\n\n'
            f'ℹ️ When enabled, a burst of destructive actions (channel/role '
            f'deletes, kicks, bans, webhook creation) by one actor, or any '
            f'grant of a dangerous permission, triggers a response: '
            f'currently **{mode_note}**. Change `ANTI_NUKE_ACTION` in '
            f'config.py to switch modes.',
            ephemeral=True)
        logger.info('Anti-nuke protection %s by user %s', status, interaction.user)
    except discord.HTTPException as e:
        logger.error('Error in nuke_protection command: %s', e)
        await interaction.response.send_message(
            'An error occurred while updating anti-nuke protection.',
            ephemeral=True)

nuke_protection_error = _command_error_handler


# ---------------------------------------------------------------------------
# Message spam protection
# ---------------------------------------------------------------------------
# Detection and response live in bot.py's on_message path, since only it
# sees every message. This is the moderator-facing toggle.

async def spam_protection_command(interaction: discord.Interaction, enabled: bool):
    """Enable or disable message spam protection."""
    try:
        config.SPAM_PROTECTION_ENABLED = enabled
        status = 'enabled' if enabled else 'disabled'
        window = getattr(config, 'SPAM_CROSSPOST_WINDOW_SECONDS', 30)
        mentions = getattr(config, 'SPAM_MENTION_THRESHOLD', 3)
        minutes = getattr(config, 'SPAM_TIMEOUT_MINUTES', 10)
        days = getattr(config, 'SPAM_NEW_ACCOUNT_DAYS', 2)
        await interaction.response.send_message(
            f'Message spam protection has been **{status}**.\n\n'
            f'ℹ️ When enabled, the bot deletes and times out '
            f'({minutes} min) for: the same link posted in 2+ channels '
            f'within {window}s, or {mentions}+ mentions in one message. '
            f'Link spammers whose account is under {days} day(s) old are '
            f'kicked instead. Moderators are exempt.',
            ephemeral=True)
        logger.info('Spam protection %s by user %s', status, interaction.user)
    except discord.HTTPException as e:
        logger.error('Error in spam_protection command: %s', e)
        await interaction.response.send_message(
            'An error occurred while updating spam protection.',
            ephemeral=True)

spam_protection_error = _command_error_handler


def register_autoreply_commands():
    """Register all autoreply commands."""
    if tree is None:
        return

    # Create autoreply command group
    autoreply_group = app_commands.Group(name='autoreply', description='Manage automatic reply rules')
    # default_permissions is ignored on subcommands by Discord, so the
    # hint has to live on the group itself.
    _apply_scope(autoreply_group, gate_perms={'manage_messages': True})

    @autoreply_group.command(name='add', description='Add a new autoreply rule')
    @app_commands.describe(
        trigger='The string to watch for in messages',
        reply='The message to send when the trigger is found',
        case_sensitive='Whether the trigger matching should be case sensitive (default: False)'
    )
    @_require_guild_permissions(manage_messages=True)
    async def _autoreply_add(interaction: discord.Interaction, trigger: str, reply: str, case_sensitive: bool = False):
        await autoreply_add_command(interaction, trigger, reply, case_sensitive)

    @autoreply_group.command(name='list', description='List all autoreply rules for this server')
    async def _autoreply_list(interaction: discord.Interaction):
        await autoreply_list_command(interaction)

    @autoreply_group.command(name='remove', description='Remove an autoreply rule')
    @app_commands.describe(rule_id='The ID of the autoreply rule to remove')
    @_require_guild_permissions(manage_messages=True)
    async def _autoreply_remove(interaction: discord.Interaction, rule_id: str):
        await autoreply_remove_command(interaction, rule_id)

    @autoreply_group.command(name='toggle', description='Enable or disable an autoreply rule')
    @app_commands.describe(rule_id='The ID of the autoreply rule to toggle')
    @_require_guild_permissions(manage_messages=True)
    async def _autoreply_toggle(interaction: discord.Interaction, rule_id: str):
        await autoreply_toggle_command(interaction, rule_id)

    # Add error handlers
    _autoreply_add.on_error = autoreply_command_error
    _autoreply_list.on_error = autoreply_command_error
    _autoreply_remove.on_error = autoreply_command_error
    _autoreply_toggle.on_error = autoreply_command_error

    # Add the group to the tree
    tree.add_command(autoreply_group)

autoreply_command_error = _command_error_handler
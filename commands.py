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
import zipfile
import socket
import shutil
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from apscheduler.triggers.interval import IntervalTrigger
import discord
from discord import app_commands
import aiohttp
import feedparser
import pytz
from icalendar import Calendar
from flask import Flask, send_file
from waitress import serve
try:
    from dateutil import parser as dateparser
except ImportError:
    dateparser = None
from config import (
    MODERATOR_ROLE_NAME,
    LOG_FILE,
    REMINDERS_FILE,
    TEMP_DIR,
    HOST_IP,
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

# Cached timezone object — avoid recreating on every use
CENTRAL_TZ = pytz.timezone(BOT_TIMEZONE)


def get_last_log_line():
    """Get the last line from the log file."""
    try:
        from collections import deque
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
    from datetime import time as _time
    current_time = datetime.now().time()

    if current_time < _time(12, 0):
        message_list = _morning_bot_messages
    elif current_time < _time(17, 0):
        message_list = _afternoon_bot_messages
    elif current_time < _time(21, 0):
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


async def _command_error_handler(interaction, error):
    """Generic command error handler."""
    last_log = get_last_log_line()
    if isinstance(error, app_commands.errors.MissingRole):
        msg = 'You do not have the required role to use this command.'
    elif isinstance(error, discord.HTTPException):
        logger.error('Discord API error: %s', error)
        msg = 'Discord API error occurred.'
    else:
        logger.error('Command error: %s', error)
        msg = f'Error: {error}'
    await interaction.response.send_message(
        f'{msg}\n\nLast log: {last_log}', ephemeral=True)


class EventFeed:  # pylint: disable=too-few-public-methods,too-many-public-methods
    """Handles event feed subscriptions and notifications for iCal and RSS feeds."""
    def __init__(self, bot):
        self.bot = bot
        self.feeds: Dict[int, Dict[str, Any]] = {}  # {guild_id: {url: feed_data}}
        self.running = True
        self.scheduler: Optional[Any] = None  # Will be set in setup_commands
        self.announce_configs: Dict[int, str] = {}  # {guild_id: channel_name}
        self._feeds_lock = threading.Lock()
        self._load_feeds()
        self._load_announce_config()

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
        """Load announce config from disk."""
        if not os.path.exists(ANNOUNCE_FILE):
            return
        try:
            with open(ANNOUNCE_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            for gid_str, ch_name in raw.items():
                self.announce_configs[int(gid_str)] = ch_name
            logger.info("Loaded announce config for %d guilds",
                        len(self.announce_configs))
        except (OSError, IOError, json.JSONDecodeError) as e:
            logger.error("Failed to load announce config: %s", e)

    def _save_announce_config(self):
        """Save announce config to disk."""
        try:
            serializable = {
                str(gid): ch
                for gid, ch in self.announce_configs.items()}
            _atomic_json_write(ANNOUNCE_FILE, serializable)
        except (OSError, IOError) as e:
            logger.error("Failed to save announce config: %s", e)

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
                    # Drop legacy uids without dates so they
                    # get re-checked with composite uid logic
                    else:
                        cleaned += 1
                feed_data['posted_events'] = to_keep

        if cleaned:
            logger.info("Cleaned up %d old posted_events entries",
                        cleaned)
            self.save_feeds()

    async def check_feeds_job(self) -> Dict[str, Any]:
        """Check all subscribed feeds for new events (next 30 days).

        Returns a summary dict with counts for reporting.
        """
        self._cleanup_old_posted_events()

        results = {
            'feeds_checked': 0,
            'events_found': 0,
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
        for guild_id, feeds in self.feeds.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                results['errors'].append(
                    f"Guild {guild_id} not found")
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

        # Persist all feed state changes in one write
        self.save_feeds()

        return results

    async def _check_single_feed(self, guild, url: str,
                                 feed_data: Dict[str, Any]) -> int:
        """Check a single feed (iCal or RSS) for new events.

        Returns the number of new events posted.
        """
        fname = feed_data.get('name', url)
        channel_name = feed_data.get('channel', '')

        channel = self._get_notification_channel(
            guild, channel_name)
        if not channel:
            raise ValueError(
                f"Channel '{channel_name}' not found")

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

        display_options = {
            'show_description': feed_data.get(
                'show_description', True),
            'show_location': feed_data.get(
                'show_location', True),
            'show_link': feed_data.get('show_link', True),
        }

        await self._process_new_events(
            guild, channel, new_events, feed_data,
            display_options)

        return len(new_events)

    # ── iCal parsing ─────────────────────────────────────────────────

    async def _fetch_calendar(self, url: str):
        """Fetch and parse calendar from URL."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response.raise_for_status()
                text = await response.text()
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
        current_time = datetime.now()
        cutoff = current_time + timedelta(days=30)
        new_events = []

        for component in calendar.walk():
            if component.name != "VEVENT":
                continue
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
            if hasattr(sd, 'tzinfo') and sd.tzinfo:
                sd_naive = sd.replace(tzinfo=None)
            else:
                sd_naive = sd
            if sd_naive < current_time - timedelta(hours=1):
                continue
            if sd_naive > cutoff:
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

        dtstart = component.get('dtstart')
        if not dtstart:
            return None

        start_date = dtstart.dt
        if not hasattr(start_date, 'date'):
            start_date = datetime.combine(
                start_date, datetime.min.time())

        dtend = component.get('dtend')
        if dtend:
            end_date = dtend.dt
            if not hasattr(end_date, 'date'):
                end_date = datetime.combine(
                    end_date, datetime.min.time())
        elif isinstance(start_date, datetime):
            end_date = start_date + timedelta(hours=1)
        else:
            end_date = start_date

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
        current_time = datetime.now()
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
            async with session.get(url) as response:
                response.raise_for_status()
                text = await response.text()

            parsed_feed = feedparser.parse(text)

            # Collect entries to scrape, skipping already-posted
            entries_to_scrape = []
            for entry in parsed_feed.get('entries', []):
                link = getattr(entry, 'link', '')
                rss_uid = getattr(entry, 'id', '') or link
                if not rss_uid:
                    continue
                entries_to_scrape.append((link, rss_uid))

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
                if hasattr(sd, 'tzinfo') and sd.tzinfo:
                    sd_naive = sd.replace(tzinfo=None)
                else:
                    sd_naive = sd
                if sd_naive < current_time - timedelta(hours=1):
                    continue
                if sd_naive > cutoff:
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

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.error(
                        "Failed to scrape %s: HTTP %s",
                        url, response.status)
                    return None
                html = await response.text()

            # Extract JSON-LD Event data
            ld_matches = re.findall(
                r'<script type="application/ld\+json">'
                r'(.*?)</script>',
                html, re.DOTALL)

            for match in ld_matches:
                try:
                    data = json.loads(match)
                    items = (data if isinstance(data, list)
                             else [data])
                    for item in items:
                        if (isinstance(item, dict)
                                and item.get('@type') == 'Event'):
                            return self._parse_jsonld_event(
                                item, url, uid)
                except (json.JSONDecodeError, KeyError):
                    continue

            logger.warning("No JSON-LD Event found on %s", url)
            return None

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(
                "Error scraping event page %s: %s", url, e)
            return None

    def _parse_jsonld_event(self, data: dict, url: str,
                            uid: str) -> Optional[Dict[str, Any]]:
        """Parse a JSON-LD Event object into our event dict."""
        summary = data.get('name', 'No Title')
        description = self._strip_html_tags(
            data.get('description', ''))

        # Parse location
        location_data = data.get('location', {})
        location = ''
        if isinstance(location_data, dict):
            loc_name = location_data.get('name', '')
            address = location_data.get('address', {})
            if isinstance(address, dict):
                street = address.get('streetAddress', '')
                if loc_name and street:
                    location = f"{loc_name}, {street}"
                elif loc_name:
                    location = loc_name
                elif street:
                    location = street
            elif loc_name:
                location = loc_name
        elif isinstance(location_data, str):
            location = location_data

        location = self._strip_urls(location)

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

    async def _process_new_events(self, guild, channel,
                                  new_events: list,
                                  feed_data: Dict[str, Any],
                                  display_options=None):
        """Process and post new events, create Discord Events.

        Announcements are handled by the scheduled Mon/Thu jobs —
        this method only persists and publishes events.
        """
        posted_events = feed_data.get('posted_events', set())

        # Fetch existing events ONCE for duplicate checking
        existing_events = []
        try:
            existing_events = await guild.fetch_scheduled_events()
        except discord.HTTPException:
            pass

        for event in new_events:
            await self._post_event_to_discord(
                channel, event, display_options)
            await self._create_discord_event(
                guild, event, existing_events)
            posted_events.add(event['uid'])

        feed_data['last_checked'] = datetime.now()
        feed_data['posted_events'] = posted_events
        # save_feeds() is called once per check_feeds_job run, not per feed

    async def _post_event_to_discord(self, channel,
                                     event: Dict[str, Any],
                                     display_options=None):
        """Post an event to a Discord channel."""
        try:
            if display_options is None:
                display_options = {
                    'show_description': True,
                    'show_location': True,
                    'show_link': True,
                }

            start_date = event['start_date']
            end_date = event.get('end_date')

            if isinstance(start_date, datetime):
                date_str = start_date.strftime("%A, %B %d, %Y")
                time_str = start_date.strftime("%I:%M %p")
                if (end_date and isinstance(end_date, datetime)
                        and end_date.date() == start_date.date()):
                    time_str += (
                        f" - {end_date.strftime('%I:%M %p')}")
            else:
                date_str = str(start_date)
                time_str = "All Day"

            embed = discord.Embed(
                title=f"📅 {event['summary']}",
                color=0x00ff00,
                description="🎉 **Added to Discord Events!**"
            )
            embed.add_field(
                name="📅 Date", value=date_str, inline=True)
            embed.add_field(
                name="⏰ Time", value=time_str, inline=True)

            if (display_options.get('show_location', True)
                    and event.get('location')):
                embed.add_field(
                    name="📍 Location",
                    value=event['location'], inline=False)

            if (display_options.get('show_description', True)
                    and event.get('description')):
                desc = event['description']
                if len(desc) > 1000:
                    desc = desc[:1000] + "..."
                embed.add_field(
                    name="📝 Description",
                    value=desc, inline=False)

            if (display_options.get('show_link', True)
                    and event.get('link')):
                embed.add_field(
                    name="🔗 Event Link",
                    value=event['link'], inline=False)

            await channel.send(embed=embed)
            logger.info("Posted event '%s' to #%s",
                        event['summary'], channel.name)

        except discord.HTTPException as e:
            logger.error("Error posting event to Discord: %s", e)

    async def _create_discord_event(self, guild,
                                    event: Dict[str, Any],
                                    existing_events=None):
        """Create a Discord Event in the guild's Events section.

        Skips creation if an event with the same name and start
        time already exists. Uses pre-fetched existing_events list
        to avoid redundant API calls.
        """
        try:
            # Discord trims trailing whitespace server-side — strip
            # before we compare against fetched events or we'll never
            # dedup and will loop-recreate every run
            name = event['summary'].strip()[:100]
            description = event.get('description', '')[:1000]
            start_time = event['start_date']
            end_time = event.get('end_date')
            location = event.get('location', '')

            # Make timezone-aware (discord.py 2.7+ requires aware datetimes)
            if isinstance(start_time, datetime) and \
               start_time.tzinfo is None:
                start_time = start_time.replace(
                    tzinfo=timezone.utc)
            if end_time and isinstance(end_time, datetime) and \
               end_time.tzinfo is None:
                end_time = end_time.replace(
                    tzinfo=timezone.utc)
            if not isinstance(start_time, datetime):
                start_time = datetime.combine(
                    start_time, datetime.min.time())
                start_time = start_time.replace(
                    tzinfo=timezone.utc)
            if end_time and not isinstance(end_time, datetime):
                end_time = datetime.combine(
                    end_time,
                    datetime.max.time().replace(microsecond=0))
                end_time = end_time.replace(
                    tzinfo=timezone.utc)
            if not end_time:
                end_time = start_time + timedelta(hours=1)

            # Check if event already exists (using pre-fetched list).
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
                    if ev_name == name and ev_utc == start_utc:
                        logger.info(
                            "Discord Event '%s' already exists, "
                            "skipping", name)
                        return

            event_location = (
                location[:100] if location
                else "See event details")

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
            return discord_event

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
        return None

    # ── Announce system (recurring Mon/Thu job) ──────────────────────

    async def announce_weekly_events(self):
        """Announce this week's Discord Events to configured channels.

        Runs Monday at 10am Central — one preview per week.
        Uses independent announce_configs (not feed-dependent).
        """
        central_tz = CENTRAL_TZ
        now = datetime.now(central_tz)

        ws_naive = (
            now - timedelta(days=now.weekday())
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        ws_naive = ws_naive.replace(tzinfo=None)
        ws = central_tz.localize(ws_naive)
        we = ws + timedelta(days=7)

        for guild_id, ch_name in self.announce_configs.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            channel = self._get_notification_channel(
                guild, ch_name)
            if not channel:
                continue

            try:
                scheduled = (
                    await guild.fetch_scheduled_events())
            except discord.HTTPException as e:
                logger.error(
                    "Announce: error fetching events for %s: %s",
                    guild.name, e)
                continue

            week_events = []
            for ev in scheduled:
                ev_start = ev.start_time
                if ev_start.tzinfo is None:
                    ev_start = central_tz.localize(ev_start)
                else:
                    ev_start = ev_start.astimezone(central_tz)
                if ws <= ev_start < we:
                    week_events.append(ev)

            if not week_events:
                logger.info(
                    "No events this week for %s", guild.name)
                continue

            for ev in week_events:
                await self._post_discord_event_announcement(
                    channel, ev, title_prefix="This Week")

            logger.info(
                "Announced %d events for %s",
                len(week_events), guild.name)

    async def announce_todays_events(self):
        """Announce Discord Events starting today at 8am Central.

        Runs daily at 8am Central — day-of reminder for each event.
        Uses independent announce_configs (not feed-dependent).
        """
        central_tz = CENTRAL_TZ
        now = datetime.now(central_tz)
        today = now.date()

        for guild_id, ch_name in self.announce_configs.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue

            channel = self._get_notification_channel(
                guild, ch_name)
            if not channel:
                continue

            try:
                scheduled = (
                    await guild.fetch_scheduled_events())
            except discord.HTTPException as e:
                logger.error(
                    "Announce: error fetching events for %s: %s",
                    guild.name, e)
                continue

            todays_events = []
            for ev in scheduled:
                ev_start = ev.start_time
                if ev_start.tzinfo is None:
                    ev_start = central_tz.localize(ev_start)
                else:
                    ev_start = ev_start.astimezone(central_tz)
                if ev_start.date() == today:
                    todays_events.append(ev)

            if not todays_events:
                logger.info(
                    "No events today for %s", guild.name)
                continue

            for ev in todays_events:
                await self._post_discord_event_announcement(
                    channel, ev, title_prefix="Today")

            logger.info(
                "Announced %d today's events for %s",
                len(todays_events), guild.name)

    async def reconcile_discord_events(self) -> Dict[str, Any]:
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
            'errors': [],
        }

        for guild_id, feeds in self.feeds.items():
            guild = self.bot.get_guild(guild_id)
            if not guild:
                results['errors'].append(
                    f"Guild {guild_id} not found")
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
                        created = await self._create_discord_event(
                            guild, ev, existing)
                        if created:
                            existing.append(created)
                            results['events_created'] += 1
                except Exception as e:  # pylint: disable=broad-exception-caught
                    results['errors'].append(f"{fname}: {e}")
                    logger.error(
                        "Reconcile error for %s: %s", fname, e)

        logger.info(
            "Reconcile complete: %d feeds, %d events created, "
            "%d errors",
            results['feeds_checked'],
            results['events_created'],
            len(results['errors']))
        return results

    async def _post_discord_event_announcement(self, channel,
                                               scheduled_event,
                                               title_prefix: str = "This Week"):
        """Post a single Discord Event announcement with URL preview."""
        try:
            start = scheduled_event.start_time
            central_tz = CENTRAL_TZ
            if start.tzinfo:
                start = start.astimezone(central_tz)

            date_str = start.strftime("%A, %B %d, %Y")
            time_str = start.strftime("%I:%M %p %Z")

            embed = discord.Embed(
                title=f"📢 {title_prefix}: {scheduled_event.name}",
                color=0xFF6600,
                description="Don't miss this event!"
            )
            embed.add_field(
                name="📅 Date", value=date_str, inline=True)
            embed.add_field(
                name="⏰ Time", value=time_str, inline=True)

            loc = getattr(scheduled_event, 'location', '') or ''
            if loc and loc != "See event details":
                embed.add_field(
                    name="📍 Location",
                    value=loc, inline=False)

            # Build event URL for preview
            event_url = (
                f"https://discord.com/events/"
                f"{scheduled_event.guild.id}/"
                f"{scheduled_event.id}")

            # Send URL as content (generates preview) + embed
            await channel.send(
                content=event_url, embed=embed)
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
message_dump_servers: Dict[str, Any] = {}  # Store active message dump servers
autoreplies: Dict[str, Dict[str, Any]] = {}  # Store autoreply rules {rule_id: rule_data}
autoreplies_lock: Optional[threading.Lock] = None


def _load_reminders():
    """Load reminders from disk into the module-level reminders dict."""
    if not os.path.exists(REMINDERS_FILE):
        return
    try:
        with open(REMINDERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        import time as _time
        for key, reminder in data.items():
            if 'next_trigger' not in reminder:
                reminder['next_trigger'] = _time.time() + reminder['interval']
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


class MessageDumpServer:  # pylint: disable=too-many-instance-attributes
    """Manages a temporary web server for hosting message dump files."""
    def __init__(self, file_path, zip_path, duration=1800):  # 30 minutes default
        self.file_path = file_path
        self.zip_path = zip_path
        self.duration = duration
        self.app = Flask(__name__)
        self.server_thread = None
        self.shutdown_timer = None
        self.port = self._find_free_port()
        self.host_ip = self._get_host_ip()
        
        # Set up Flask route
        @self.app.route('/')
        def download_file():
            return send_file(self.zip_path, as_attachment=True)
    
    def _find_free_port(self):
        """Find a free port to use for the server."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            return s.getsockname()[1]
    
    def _get_host_ip(self):
        """Get the IP address to bind the server to based on config."""
        if HOST_IP != "0.0.0.0":
            # Use the specific IP from config
            return HOST_IP
        
        # If HOST_IP is 0.0.0.0, find the interface with route to internet
        return self._get_internet_facing_ip()
    
    def _get_internet_facing_ip(self):
        """Get the IP of the interface that has a route to the internet."""
        try:
            # Create a socket and connect to a remote address to determine which interface is used
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                # Connect to Google's DNS server (doesn't actually send data)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                return local_ip
        except Exception:  # pylint: disable=broad-exception-caught
            # Fallback to localhost if we can't determine the internet-facing interface
            logger.warning("Could not determine internet-facing interface, falling back to localhost")
            return "127.0.0.1"
    
    def start(self):
        """Start the web server in a separate thread."""
        self._waitress_server = None

        def run_server():
            # Use create_server + run so we can close it gracefully
            from waitress import create_server
            self._waitress_server = create_server(self.app, host=self.host_ip, port=self.port)
            self._waitress_server.run()

        self.server_thread = threading.Thread(target=run_server, daemon=True)
        self.server_thread.start()

        # Set up shutdown timer
        self.shutdown_timer = threading.Timer(self.duration, self.cleanup)
        self.shutdown_timer.start()

        return f"http://{self.host_ip}:{self.port}"

    def cleanup(self):
        """Clean up resources when the server is shut down."""
        # Stop the waitress server gracefully
        try:
            if self._waitress_server is not None:
                self._waitress_server.close()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Could not gracefully stop waitress server on port %s", self.port)

        try:
            if os.path.exists(self.file_path):
                os.remove(self.file_path)
            if os.path.exists(self.zip_path):
                os.remove(self.zip_path)

            parent_dir = os.path.dirname(self.file_path)
            if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                os.rmdir(parent_dir)

            logger.info("Cleaned up message dump files: %s, %s", self.file_path, self.zip_path)
        except (OSError, IOError) as e:
            logger.error("Error cleaning up message dump files: %s", e)

        # Use list() to avoid modifying dict during iteration
        for key, server in list(message_dump_servers.items()):
            if server is self:
                del message_dump_servers[key]
                break

def register_commands():  # pylint: disable=too-many-locals,too-many-statements
    """Register all commands with the command tree."""
    if tree is None:
        return

    tree.add_command(create_set_reminder_command())

    @tree.command(name='list_reminders', description='Lists all current reminders')
    async def list_reminders(interaction: discord.Interaction):
        """Lists all current reminders."""
        try:
            if not reminders:
                await interaction.response.send_message('There are no reminders set.',
                                                       ephemeral=True)
                return
            reminder_list = '\n'.join(
                f"**{reminder['title']}**: {reminder['message']} "
                f"(every {reminder['interval']} seconds)"
                for reminder in reminders.values()
            )
            await interaction.response.send_message(f'Current reminders:\n{reminder_list}',
                                                   ephemeral=True)
        except (discord.HTTPException, OSError, IOError) as e:
            logger.error('Error listing reminders: %s', e)
            await interaction.response.send_message('Failed to list reminders due to an error.',
                                                   ephemeral=True)

    @tree.command(name='delete_all_reminders', description='Deletes all active reminders')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _delete_all_reminders(interaction: discord.Interaction) -> None:
        await delete_all_reminders(interaction)

    @tree.command(name='delete_reminder', description='Deletes a reminder by title')
    @app_commands.describe(title='Title of the reminder to delete')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _delete_reminder(interaction: discord.Interaction, title: str) -> None:
        await delete_reminder(interaction, title)

    @tree.command(name='purge_last_messages',
                  description='Purges a specified number of messages from a channel')
    @app_commands.describe(channel='Channel to purge messages from',
                          limit='Number of messages to delete')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _purge_last_messages(interaction: discord.Interaction,
                                  channel: discord.TextChannel, limit: int):
        await purge_last_messages(interaction, channel, limit)

    _purge_last_messages.on_error = purge_last_messages_error

    @tree.command(name='purge_string',
                  description='Purges all messages containing a specific string from a channel')
    @app_commands.describe(channel='Channel to purge messages from',
                          search_string='String to search for in messages')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _purge_string(interaction: discord.Interaction,
                           channel: discord.TextChannel, search_string: str):
        await purge_string(interaction, channel, search_string)

    _purge_string.on_error = purge_string_error

    @tree.command(name='purge_webhooks',
                  description='Purges all messages sent by webhooks or apps from a channel')
    @app_commands.describe(channel='Channel to purge messages from')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _purge_webhooks(interaction: discord.Interaction, channel: discord.TextChannel):
        await purge_webhooks(interaction, channel)

    _purge_webhooks.on_error = purge_webhooks_error

    @tree.command(name='kick', description='Kicks one or more members from the server')
    @app_commands.describe(
        members='Members to kick (separate multiple users with spaces)',
        reason='Reason for kick'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _kick(interaction: discord.Interaction, members: str,
                   reason: Optional[str] = None):
        await kick_members(interaction, members, reason)

    _kick.on_error = kick_error

    @tree.command(name='kick_role', description='Kicks all members with a specified role from the server')
    @app_commands.describe(role='Role whose members to kick', reason='Reason for kick')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _kick_role(interaction: discord.Interaction, role: discord.Role,
                        reason: Optional[str] = None):
        await kick_role(interaction, role, reason)

    _kick_role.on_error = kick_role_error

    @tree.command(name='botsay', description='Makes the bot send a message to a specified channel')
    @app_commands.describe(channel='Channel to send the message to', message='Message to send')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _botsay(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
        await botsay_message(interaction, channel, message)

    _botsay.on_error = botsay_error

    @tree.command(name='timeout', description='Timeouts a member for a specified duration')
    @app_commands.describe(member='Member to timeout', duration='Timeout duration in seconds',
                          reason='Reason for timeout')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _timeout(interaction: discord.Interaction, member: discord.Member,
                      duration: int, reason: Optional[str] = None):
        await timeout_member(interaction, member, duration, reason)

    _timeout.on_error = timeout_error

    @tree.command(name='log_tail',
                  description='DM the last specified number of lines of the bot log to the user')
    @app_commands.describe(lines='Number of lines to retrieve from the log')
    async def _log_tail(interaction: discord.Interaction, lines: int):
        await log_tail_command(interaction, lines)
    
    _log_tail.on_error = log_tail_error

    @tree.command(name='add_event_feed',
                  description='Adds a calendar or RSS feed URL to check for events')
    @app_commands.describe(
        feed_name='A short name to identify this feed',
        calendar_url='URL of the calendar or RSS feed',
        channel='Channel to post event notifications',
        show_description='Show event description (default: Yes)',
        show_location='Show event location (default: Yes)',
        show_link='Show event URL link (default: Yes)'
    )
    async def _add_event_feed(interaction: discord.Interaction,
                              feed_name: str,
                              calendar_url: str,
                              channel: discord.TextChannel,
                              show_description: bool = True,
                              show_location: bool = True,
                              show_link: bool = True):
        await add_event_feed_command(
            interaction, feed_name, calendar_url, channel,
            show_description, show_location, show_link)

    _add_event_feed.on_error = add_event_feed_error

    @tree.command(name='list_event_feeds',
                  description='Lists all registered event feeds')
    async def _list_event_feeds(interaction: discord.Interaction):
        await list_event_feeds_command(interaction)

    @tree.command(name='remove_event_feed',
                  description='Removes an event feed by name')
    @app_commands.describe(
        feed_name='Name of the feed to remove')
    async def _remove_event_feed(interaction: discord.Interaction,
                                 feed_name: str):
        await remove_event_feed_command(interaction, feed_name)

    @tree.command(name='check_event_feeds',
                  description='Manually check all event feeds for new events now')
    async def _check_event_feeds(interaction: discord.Interaction):
        await check_event_feeds_command(interaction)

    _check_event_feeds.on_error = check_event_feeds_error

    @tree.command(name='event_announce',
                  description='Enable weekly event announcements (Mon/Thu 10am CT)')
    @app_commands.describe(
        channel='Channel to post event announcements in')
    async def _event_announce(interaction: discord.Interaction,
                              channel: discord.TextChannel):
        await event_announce_command(interaction, channel)

    @tree.command(name='disable_event_announce',
                  description='Disable weekly event announcements')
    async def _disable_event_announce(
            interaction: discord.Interaction):
        await disable_event_announce_command(interaction)

    @tree.command(name='bot_mood', description='Check on the bot\'s current mood')
    async def _bot_mood(interaction: discord.Interaction):
        await bot_command(interaction)

    @tree.command(name='pet_bot', description='Pet the bot')
    async def _pet_bot(interaction: discord.Interaction):
        await pet_bot_command(interaction)

    @tree.command(name='bot_pick_fav',
                  description='See who the bot prefers today')
    @app_commands.describe(
        user1="First potential favorite",
        user2="Second potential favorite"
    )
    async def _bot_pick_fav(interaction: discord.Interaction, user1: discord.User, user2: discord.User):
        await bot_pick_fav_command(interaction, user1, user2)
        
    @tree.command(name='message_dump',
                  description='Dump a user\'s messages from a channel into a downloadable file')
    @app_commands.describe(
        user="User whose messages to dump",
        channel="Channel to dump messages from",
        start_date="Start date in YYYY-MM-DD format (e.g., 2025-01-01)",
        limit="Maximum number of messages to fetch (default: 1000)"
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _message_dump(interaction: discord.Interaction, user: discord.User,
                           channel: discord.TextChannel, start_date: str, limit: int = 1000):
        await message_dump_command(interaction, user, channel, start_date, limit)
    
    _message_dump.on_error = message_dump_error

    @tree.command(name='clone_category_permissions',
                  description='Clone permissions from source category to destination category')
    @app_commands.describe(
        source_category='Source category to copy permissions from',
        destination_category='Destination category to copy permissions to'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clone_category_permissions(interaction: discord.Interaction,
                                        source_category: discord.CategoryChannel,
                                        destination_category: discord.CategoryChannel):
        await clone_category_permissions(interaction, source_category, destination_category)

    _clone_category_permissions.on_error = clone_category_permissions_error

    @tree.command(name='clone_channel_permissions',
                  description='Clone permissions from source channel to destination channel')
    @app_commands.describe(
        source_channel='Source channel to copy permissions from',
        destination_channel='Destination channel to copy permissions to'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clone_channel_permissions(interaction: discord.Interaction,
                                       source_channel: discord.abc.GuildChannel,
                                       destination_channel: discord.abc.GuildChannel):
        await clone_channel_permissions(interaction, source_channel, destination_channel)

    _clone_channel_permissions.on_error = clone_channel_permissions_error

    @tree.command(name='clone_role_permissions',
                  description='Clone permissions from source role to destination role')
    @app_commands.describe(
        source_role='Source role to copy permissions from',
        destination_role='Destination role to copy permissions to'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clone_role_permissions(interaction: discord.Interaction,
                                    source_role: discord.Role,
                                    destination_role: discord.Role):
        await clone_role_permissions(interaction, source_role, destination_role)

    # Add error handler for clone_role_permissions
    _clone_role_permissions.on_error = clone_role_permissions_error

    # Register clear_category_permissions command
    @tree.command(name='clear_category_permissions',
                  description='Clear all permission overwrites from a category')
    @app_commands.describe(
        category='Category to clear all permission overwrites from'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clear_category_permissions(interaction: discord.Interaction,
                                        category: discord.CategoryChannel):
        await clear_category_permissions(interaction, category)

    # Add error handler for clear_category_permissions
    _clear_category_permissions.on_error = clear_category_permissions_error

    # Register clear_channel_permissions command
    @tree.command(name='clear_channel_permissions',
                  description='Clear all permission overwrites from a channel')
    @app_commands.describe(
        channel='Channel to clear all permission overwrites from'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clear_channel_permissions(interaction: discord.Interaction,
                                       channel: discord.abc.GuildChannel):
        await clear_channel_permissions(interaction, channel)

    # Add error handler for clear_channel_permissions
    _clear_channel_permissions.on_error = clear_channel_permissions_error

    # Register clear_role_permissions command
    @tree.command(name='clear_role_permissions',
                  description='Clear all permissions from a role (reset to default)')
    @app_commands.describe(
        role='Role to clear all permissions from'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _clear_role_permissions(interaction: discord.Interaction,
                                    role: discord.Role):
        await clear_role_permissions(interaction, role)

    # Add error handler for clear_role_permissions
    _clear_role_permissions.on_error = clear_role_permissions_error

    # Register sync_channel_perms command
    @tree.command(name='sync_channel_perms',
                  description='Sync permissions for all channels in a category with the category permissions')
    @app_commands.describe(
        source_category='Category whose permissions will be synced to all its channels'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _sync_channel_perms(interaction: discord.Interaction,
                                source_category: discord.CategoryChannel):
        await sync_channel_perms(interaction, source_category)

    # Add error handler for sync_channel_perms
    _sync_channel_perms.on_error = sync_channel_perms_error

    # Register list_users_without_roles command
    @tree.command(name='list_users_without_roles',
                  description='Lists all users that do not have any server role assigned')
    async def _list_users_without_roles(interaction: discord.Interaction):
        await list_users_without_roles(interaction)

    # Add error handler for list_users_without_roles
    _list_users_without_roles.on_error = list_users_without_roles_error

    # Register assign_role command
    @tree.command(name='assign_role',
                  description='Assigns a role to multiple users at once')
    @app_commands.describe(
        role='Role to assign to the users',
        members='Members to assign the role to (separate multiple users with spaces or newlines)'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _assign_role(interaction: discord.Interaction, role: discord.Role,
                          members: str):
        await assign_role(interaction, role, members)

    # Add error handler for assign_role
    _assign_role.on_error = assign_role_error

    # Register remove_role command
    @tree.command(name='remove_role',
                  description='Removes a role from multiple users at once')
    @app_commands.describe(
        role='Role to remove from the users',
        members='Members to remove the role from (separate multiple users with spaces or newlines)'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _remove_role(interaction: discord.Interaction, role: discord.Role,
                          members: str):
        await remove_role(interaction, role, members)

    # Add error handler for remove_role
    _remove_role.on_error = remove_role_error

    # Register voice_chaperone command
    @tree.command(name='voice_chaperone',
                  description='Enable or disable the voice channel chaperone functionality')
    @app_commands.describe(enabled='True to enable, False to disable voice chaperone')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _voice_chaperone(interaction: discord.Interaction, enabled: bool):
        await voice_chaperone_command(interaction, enabled)

    # Add error handler for voice_chaperone
    _voice_chaperone.on_error = voice_chaperone_error

    # Register update checking command
    register_update_checking_command()
    
    # Register dashboard command
    @tree.command(name='dashboard', description='Display a dashboard of all available commands grouped by category')
    async def _dashboard(interaction: discord.Interaction):
        await dashboard_command(interaction)
    
    _dashboard.on_error = dashboard_command_error
    
    # Register autoreply commands
    register_autoreply_commands()


def setup_commands(bot_param):
    """Initialize command module with bot instance and register commands."""
    # Using globals is necessary here to initialize module-level variables
    # pylint: disable=global-statement
    global bot_instance, tree, scheduler, reminders, event_feed, message_dump_servers, autoreplies, autoreplies_lock  # pylint: disable=line-too-long
    bot_instance = bot_param
    if bot_instance:
        tree = bot_instance.tree
    reminders = {}
    event_feed = EventFeed(bot_instance)
    message_dump_servers = {}
    autoreplies = {}
    autoreplies_lock = threading.Lock()

    # Load existing reminders from disk
    _load_reminders()

    # Load existing autoreply rules
    load_autoreplies()

    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # pylint: disable=import-outside-toplevel
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


class InvalidReminderInterval(Exception):
    """Exception raised when an invalid reminder interval is provided."""

def validate_reminder_interval(interval: int) -> None:
    """Validate that reminder interval is reasonable."""
    if interval < 60:
        raise InvalidReminderInterval("Interval must be at least 60 seconds")

def create_set_reminder_command():
    """Factory function to create the set_reminder command."""
    @app_commands.command(name='set_reminder', description='Sets a reminder message to be sent to a channel at regular intervals')
    @app_commands.describe(
        channel='Channel to send reminders to',
        title='Title of the reminder',
        message='Message content of the reminder',
        interval='Interval in seconds between reminders (minimum 60)'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def set_reminder_command(interaction: discord.Interaction,
                                  channel: discord.TextChannel, title: str,
                                  message: str, interval: int):
        """Sets a reminder message to be sent to a channel at regular intervals."""
        await set_reminder_callback(interaction, channel, title, message, interval)

    async def on_error(interaction: discord.Interaction, error):
        """Handles errors for the set_reminder command."""
        last_log = get_last_log_line()
        if isinstance(error, app_commands.errors.MissingRole):
            await interaction.response.send_message(
                f'You do not have the required role to use this command.\n\nLast log: {last_log}', ephemeral=True)
        elif isinstance(error, InvalidReminderInterval):
            await interaction.response.send_message(f'Invalid interval: {error}\n\nLast log: {last_log}', ephemeral=True)
        elif isinstance(error, discord.HTTPException):
            logger.error('Discord API error: %s', error)
            await interaction.response.send_message(f'Discord API error occurred.\n\nLast log: {last_log}', ephemeral=True)
        else:
            await interaction.response.send_message(f'Error: {error}\n\nLast log: {last_log}', ephemeral=True)
    
    set_reminder_command.on_error = on_error
    return set_reminder_command

async def set_reminder_callback(interaction: discord.Interaction,
                               channel: discord.TextChannel, title: str,
                               message: str, interval: int):
    """Callback for the set_reminder command."""
    validate_reminder_interval(interval)
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

async def purge_last_messages(interaction: discord.Interaction, channel: discord.TextChannel, limit: int):
    """Purges a specified number of messages from a channel."""
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await channel.purge(limit=limit)
        await interaction.followup.send(f'Deleted {len(deleted)} message(s)', ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send('You do not have permission to perform this action.', ephemeral=True)
    except discord.HTTPException as e:
        logger.error('Discord API error: %s', e)
        await interaction.followup.send('Discord API error occurred. Please try again later.', ephemeral=True)

purge_last_messages_error = _command_error_handler
async def purge_string(interaction: discord.Interaction, channel: discord.TextChannel, search_string: str):
    """Purges all messages containing a specific string from a channel."""
    await interaction.response.defer(ephemeral=True)
    try:
        def check_message(message):
            return search_string in message.content

        deleted = await channel.purge(check=check_message)
        await interaction.followup.send(f'Deleted {len(deleted)} message(s) containing "{search_string}".', ephemeral=True)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.followup.send('A Discord API error occurred.', ephemeral=True)

purge_string_error = _command_error_handler
async def purge_webhooks(interaction: discord.Interaction, channel: discord.TextChannel):
    """Purges all messages sent by webhooks or apps from a channel."""
    await interaction.response.defer(ephemeral=True)
    try:
        def check_message(message):
            return message.webhook_id is not None or message.author.bot

        deleted = await channel.purge(check=check_message)
        await interaction.followup.send(f'Deleted {len(deleted)} message(s) sent by webhooks or apps.', ephemeral=True)
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
                
                # Skip the command user
                if member.id == interaction.user.id:
                    failed_kicks.append(f"{member.display_name} (cannot kick yourself)")
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
async def timeout_member(interaction: discord.Interaction, member: discord.Member, duration: int, reason: Optional[str] = None):
    """Timeouts a member for a specified duration."""
    try:
        until = discord.utils.utcnow() + timedelta(seconds=duration)
        await member.timeout(until, reason=reason)
        await interaction.response.send_message(f'{member.mention} has been timed out for {duration} seconds.', ephemeral=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error('Discord API error: %s', e)
        await interaction.response.send_message('A Discord API error occurred.', ephemeral=True)

timeout_error = _command_error_handler
async def log_tail_command(interaction: discord.Interaction, lines: int):
    """DM the last specified number of lines of the bot log to the user."""
    try:
        from collections import deque
        with open(LOG_FILE, 'r', encoding='utf-8') as log_file:
            last_lines = ''.join(deque(log_file, maxlen=lines))
        if last_lines:
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
async def add_event_feed_command(interaction: discord.Interaction,  # pylint: disable=too-many-arguments,too-many-branches,too-many-statements
                                 feed_name: str,
                                 calendar_url: str,
                                 channel: discord.TextChannel,
                                 show_description: bool = True,
                                 show_location: bool = True,
                                 show_link: bool = True):
    """Adds a calendar or RSS feed URL to check for events."""
    try:
        await interaction.response.defer(ephemeral=True)

        resolved_channel_name = channel.name
        logger.info(
            "add_event_feed: name=%s url=%s channel=#%s",
            feed_name, calendar_url, resolved_channel_name)

        if not calendar_url.startswith(('http://', 'https://')):
            await interaction.followup.send(
                "Invalid URL format", ephemeral=True)
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
                    text = await response.text()

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

        except aiohttp.ClientError as e:
            await interaction.followup.send(
                f"Error accessing feed: {str(e)}",
                ephemeral=True)
            return

        if event_feed and interaction.guild:
            guild_id = interaction.guild.id
            if guild_id not in event_feed.feeds:
                event_feed.feeds[guild_id] = {}

            event_feed.feeds[guild_id][calendar_url] = {
                'name': feed_name,
                'last_checked': datetime.now(),
                'channel': resolved_channel_name,
                'posted_events': set(),
                'feed_type': detected_type,
                'show_description': show_description,
                'show_location': show_location,
                'show_link': show_link,
            }
            event_feed.save_feeds()

        # Build confirmation message
        type_label = "RSS" if detected_type == 'rss' else "iCal"
        opts = []
        opts.append("Description ✅" if show_description
                     else "Description ❌")
        opts.append("Location ✅" if show_location
                     else "Location ❌")
        opts.append("Link ✅" if show_link else "Link ❌")

        await interaction.followup.send(
            f"✅ Added {type_label} feed "
            f"**\"{feed_name}\"**! "
            f"Checking for events now...\n"
            f"**Channel:** #{resolved_channel_name}\n"
            f"**Show:** {' | '.join(opts)}",
            ephemeral=True
        )

        # Immediately check the new feed AFTER sending confirmation
        if event_feed and interaction.guild:
            try:
                guild = interaction.guild
                guild_id = guild.id
                feed_data = event_feed.feeds[guild_id][calendar_url]
                count = await event_feed._check_single_feed(
                    guild, calendar_url, feed_data)
                await interaction.followup.send(
                    f"✅ Initial check complete: "
                    f"**{count}** new events found and posted.",
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

        results = await event_feed.check_feeds_job()
        recon = await event_feed.reconcile_discord_events()

        # Build result message
        parts = [
            f"📊 **Feed Check Results**\n"
            f"Feeds checked: **{results['feeds_checked']}**\n"
            f"New events posted: **{results['events_posted']}**\n"
            f"Missing Discord events created: "
            f"**{recon['events_created']}**"
        ]

        all_errors = list(results['errors']) + list(recon['errors'])
        if all_errors:
            error_list = '\n'.join(
                f"• {e}" for e in all_errors[:5])
            parts.append(f"\n\n⚠️ **Errors:**\n{error_list}")

        if (results['events_posted'] == 0
                and recon['events_created'] == 0
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

    for url, data in guild_feeds.items():
        fname = data.get('name', 'Unnamed')
        feed_type = data.get('feed_type', 'ical').upper()
        ch = data.get('channel', 'unknown')

        show_parts = []
        show_parts.append(
            "Desc ✅" if data.get('show_description', True)
            else "Desc ❌")
        show_parts.append(
            "Loc ✅" if data.get('show_location', True)
            else "Loc ❌")
        show_parts.append(
            "Link ✅" if data.get('show_link', True)
            else "Link ❌")

        # Truncate URL for display
        display_url = (
            url if len(url) <= 60 else url[:57] + "...")

        value = (
            f"**URL:** {display_url}\n"
            f"**Type:** {feed_type} | **Channel:** #{ch}\n"
            f"**Show:** {' | '.join(show_parts)}"
        )
        embed.add_field(
            name=f"📌 {fname}", value=value, inline=False)

    await interaction.response.send_message(
        embed=embed, ephemeral=True)

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
    await interaction.response.send_message(
        f'✅ Removed event feed **"{feed_name}"**',
        ephemeral=True)

async def event_announce_command(
        interaction: discord.Interaction,
        channel: discord.TextChannel):
    """Enable weekly event announcements for this server."""
    if not event_feed or not interaction.guild:
        await interaction.response.send_message(
            'Event feed system not initialized.',
            ephemeral=True)
        return

    guild_id = interaction.guild.id
    event_feed.announce_configs[guild_id] = channel.name
    event_feed._save_announce_config()

    await interaction.response.send_message(
        f"✅ **Event announcements enabled!**\n"
        f"📢 This week's events will be posted to "
        f"#{channel.name} every **Mon & Thu at 10am CT**.\n"
        f"Use `/disable_event_announce` to turn off.",
        ephemeral=True)

    logger.info(
        "Event announcements enabled for guild %s → #%s",
        interaction.guild.name, channel.name)

async def disable_event_announce_command(
        interaction: discord.Interaction):
    """Disable weekly event announcements for this server."""
    if not event_feed or not interaction.guild:
        await interaction.response.send_message(
            'Event feed system not initialized.',
            ephemeral=True)
        return

    guild_id = interaction.guild.id
    if guild_id in event_feed.announce_configs:
        del event_feed.announce_configs[guild_id]
        event_feed._save_announce_config()
        await interaction.response.send_message(
            "✅ Event announcements **disabled**.",
            ephemeral=True)
    else:
        await interaction.response.send_message(
            "Event announcements were not enabled.",
            ephemeral=True)

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

async def pet_bot_command(interaction: discord.Interaction):
    """Pet the bot."""
    try:
        bot_name = interaction.client.user.display_name if interaction.client.user else "the bot"
        # Define bot response messages inline and select efficiently
        bot_responses = [
            "BOTNAME purrs happily!",
            "BOTNAME rubs against your leg!",
            "BOTNAME gives you a slow blink of affection!",
            "BOTNAME meows appreciatively!",
            "BOTNAME headbutts your hand for more pets!"
        ]
        selected_response = random.choice(bot_responses)
        message = selected_response.replace("BOTNAME", bot_name)
        logger.info('[%s] - pet bot command: %s', interaction.user, message)
        await interaction.response.send_message(message)
    except discord.errors.NotFound:
        # Handle case where interaction has timed out
        logger.warning("Interaction timed out for pet_bot command from %s", interaction.user)

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
                              start_date: str, limit: int = 1000):
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
            start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
            start_datetime = start_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
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
        
        # Simplified approach to message fetching
        messages = []
        total_processed = 0
        last_message_id = None
        
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
                
                # Set up the fetch parameters
                fetch_kwargs = {'limit': batch_size, 'after': start_datetime}
                if last_message_id:
                    fetch_kwargs['before'] = discord.Object(id=last_message_id)
                
                # Log the current fetch attempt
                logger.info("Fetching batch with params: %s, processed so far: %s", fetch_kwargs, total_processed)
            
                # Fetch the batch
                current_batch = []
                messages_in_this_batch = 0
                
                async for msg in channel.history(**fetch_kwargs):
                    messages_in_this_batch += 1
                    # Keep track of the last message ID for pagination
                    if last_message_id is None or msg.id < last_message_id:
                        last_message_id = msg.id
                    
                    # Count this message
                    total_processed += 1
                    
                    # Log message details for debugging
                    logger.info("Message %s from author ID: %s, target user ID: %s, match: %s",
                               msg.id, msg.author.id, user.id, msg.author.id == user.id)
                    
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
                
            except discord.HTTPException as e:
                if "rate limited" in str(e).lower():
                    retry_delay = base_delay * (2 ** retry_count)
                    retry_count += 1
                    logger.warning("Rate limited. Retrying in %s seconds. Retry %s/%s",
                                  retry_delay, retry_count, max_retries)
                    
                    if retry_count <= max_retries:
                        await interaction.followup.send(
                            f"Hit Discord rate limit. Waiting {retry_delay} seconds before continuing...",
                            ephemeral=True
                        )
                        await asyncio.sleep(retry_delay)
                        # Skip the rest of this iteration and retry
                        continue
                    
                    logger.error("Max retries (%s) reached for rate limiting", max_retries)
                    await interaction.followup.send(
                        "Hit Discord rate limit too many times. Try again later or with a smaller limit.",
                        ephemeral=True
                    )
                    # Break out of the loop entirely
                    break
                else:
                    # Re-raise other HTTP exceptions
                    raise
            
            # Log the results of this batch
            batch_count = len(current_batch)
            logger.info("Batch complete: processed %s messages from target user", batch_count)
            
            # Add the batch to our collection
            messages.extend(current_batch)
            
            # Send a progress update every 500 messages or at the end of a batch
            if total_processed % 500 == 0 or batch_count < batch_size:
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
            
            # Add a delay to avoid rate limiting
            # Use exponential backoff - start with a small delay and increase if we hit rate limits
            try:
                await asyncio.sleep(1.0)  # Increased from 0.5 to 1.0 second
            except asyncio.CancelledError:
                # Handle cancellation gracefully
                logger.info("Message fetching was cancelled")
                break
        
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
        
        # Start the web server
        server = MessageDumpServer(file_path, zip_path)
        download_url = server.start()
        
        # Store the server in the active servers dictionary
        server_key = f"{interaction.user.id}_{timestamp}"
        message_dump_servers[server_key] = server
        
        # Send DM to the user with the download link
        expiry_time = (datetime.now() + timedelta(seconds=1800)).strftime("%Y-%m-%d %H:%M:%S")
        # Build DM message more efficiently
        dm_parts = [
            f"Here's the message dump you requested from {channel.mention}:\n\n",
            f"**Download Link:** {download_url}\n",
            f"**User:** {user.mention}\n",
            f"**Start Date:** {start_date}\n",
            f"**Messages found:** {len(messages)}\n",
            f"**Messages processed:** {total_processed}\n",
            f"**Link expires:** {expiry_time} (30 minutes from now)\n\n",
            "The file will be automatically deleted after the link expires."
        ]
        dm_message = ''.join(dm_parts)
        
        await interaction.user.send(dm_message)
        
        # Send a confirmation in the channel where the command was used
        await interaction.followup.send(
            f"Message dump for {user.mention} from {channel.mention} has been created. "
            f"Check your DMs for the download link. The link will expire in 30 minutes.",
            ephemeral=True
        )
        
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
async def clone_category_permissions(interaction: discord.Interaction,  # pylint: disable=too-many-branches,too-many-locals,too-many-statements,too-many-nested-blocks
                                   source_category: discord.CategoryChannel,
                                   destination_category: discord.CategoryChannel):
    """Clone permissions from source category to destination category."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Verify both categories are in the same guild
        if source_category.guild.id != destination_category.guild.id:
            await interaction.followup.send(
                'Source and destination categories must be in the same server.',
                ephemeral=True
            )
            return
        
        # Clear existing permissions on destination category
        await interaction.followup.send(
            f'Clearing existing permissions on {destination_category.name}...',
            ephemeral=True
        )
        
        # Get the bot's highest role for hierarchy checking
        bot_member = destination_category.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        # Get all current overwrites and clear them (except Administrator, managed, and hierarchy-protected roles)
        for target in list(destination_category.overwrites.keys()):
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        logger.info('Skipped clearing Administrator role %s on category %s', target, destination_category.name)
                        continue
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        logger.info('Skipped clearing managed role %s on category %s', target, destination_category.name)
                        continue
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                        logger.info('Skipped clearing role %s (hierarchy: role position %d >= bot position %d)',
                                   target.name, target.position, bot_top_role.position)
                        continue
                    # Additional check for privileged roles that might cause issues
                    if isinstance(target, discord.Role):
                        if target.permissions.manage_roles or target.permissions.manage_guild or target.permissions.manage_channels:
                            if bot_top_role and target >= bot_top_role:
                                logger.info('Skipped clearing privileged role %s (has management permissions and position %d >= bot position %d)',
                                           target.name, target.position, bot_top_role.position)
                                continue
                    await destination_category.set_permissions(target, overwrite=None)
                    logger.info('Cleared permissions for %s on category %s', target, destination_category.name)
            except discord.Forbidden as e:
                # Log as hierarchy issue if it's a role
                if isinstance(target, discord.Role):
                    logger.error('Failed to clear permissions for role %s (likely hierarchy issue or Discord\'s limitation on bots not being allowed go manage Moderator-style roles. You must manage these manually): %s', target.name, e)
                else:
                    logger.error('Failed to clear permissions for %s: %s', target, e)
                # Continue without spamming user with individual errors
                continue
            except discord.HTTPException as e:
                logger.error('Failed to clear permissions for %s: %s', target, e)
        
        # Copy permissions from source to destination
        await interaction.followup.send(
            f'Copying permissions from {source_category.name} to {destination_category.name}...',
            ephemeral=True
        )
        
        copied_count = 0
        skipped_admin_count = 0
        skipped_managed_count = 0
        skipped_hierarchy_count = 0
        failed_hierarchy_roles = []  # Track specific roles that failed due to hierarchy
        
        # Get the bot's highest role for hierarchy checking
        bot_member = destination_category.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        # Log bot's role information for debugging
        if bot_top_role:
            logger.info('Bot\'s highest role: %s (position: %d)', bot_top_role.name, bot_top_role.position)
        else:
            logger.warning('Could not determine bot\'s highest role')
        
        for target, overwrite in source_category.overwrites.items():
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        skipped_admin_count += 1
                        logger.info('Skipped copying Administrator role %s from %s to %s',
                                   target, source_category.name, destination_category.name)
                        continue
                    
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        skipped_managed_count += 1
                        logger.info('Skipped copying managed role %s from %s to %s',
                                   target, source_category.name, destination_category.name)
                        continue
                    
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role:
                        # Log role comparison for debugging
                        logger.info('Checking role %s (position: %d) vs bot role %s (position: %d)',
                                   target.name, target.position, bot_top_role.name, bot_top_role.position)
                        if target >= bot_top_role:
                            skipped_hierarchy_count += 1
                            failed_hierarchy_roles.append(target.name)
                            logger.info('Skipped copying role %s (hierarchy: role position %d >= bot position %d)',
                                       target.name, target.position, bot_top_role.position)
                            continue
                    
                    # Test if we can actually modify this role by checking Discord's restrictions
                    if isinstance(target, discord.Role):
                        # Check if the bot can manage this role
                        if not bot_member.guild_permissions.manage_roles:
                            logger.info('Skipped copying role %s (bot lacks manage_roles permission)', target.name)
                            continue
                        
                        # Check for Discord's restricted permissions that bots cannot manage
                        # Discord prevents bots from managing roles with these dangerous permissions
                        dangerous_perms = [
                            target.permissions.ban_members,
                            target.permissions.kick_members,
                            target.permissions.manage_roles,
                            target.permissions.manage_guild,
                            target.permissions.manage_channels,
                            target.permissions.manage_messages,
                            target.permissions.moderate_members,
                            target.permissions.administrator
                        ]
                        
                        if any(dangerous_perms):
                            skipped_hierarchy_count += 1
                            failed_hierarchy_roles.append(target.name)
                            logger.info('Skipped copying role %s (Discord restricts bots from managing roles with moderation permissions like ban_members, kick_members, etc.)',
                                       target.name)
                            continue
                    
                    await destination_category.set_permissions(target, overwrite=overwrite)
                    copied_count += 1
                    logger.info('Copied permissions for %s from %s to %s',
                               target, source_category.name, destination_category.name)
            except discord.Forbidden as e:
                # Log the specific role that failed and provide detailed feedback
                if isinstance(target, discord.Role):
                    skipped_hierarchy_count += 1
                    failed_hierarchy_roles.append(target.name)
                    
                    # Check if this is likely due to Discord's moderation permission restrictions
                    dangerous_perms = [
                        target.permissions.ban_members,
                        target.permissions.kick_members,
                        target.permissions.manage_roles,
                        target.permissions.manage_guild,
                        target.permissions.manage_channels,
                        target.permissions.manage_messages,
                        target.permissions.moderate_members,
                        target.permissions.administrator
                    ]
                    
                    if any(dangerous_perms):
                        logger.error('Failed to copy permissions for role %s - Discord restricts bots from managing roles with moderation permissions (ban_members, kick_members, etc.): %s', target.name, e)
                        await interaction.followup.send(
                            f' **Discord Restriction**: Cannot copy permissions for role **{target.name}** because Discord prevents bots from managing roles with moderation permissions like ban_members, kick_members, manage_roles, etc. You\'ll need to copy these permissions manually.',
                            ephemeral=True
                        )
                    else:
                        logger.error('Failed to copy permissions for role %s (likely hierarchy issue): %s', target.name, e)
                else:
                    logger.error('Failed to copy permissions for %s: %s', target, e)
                continue
            except discord.HTTPException as e:
                logger.error('Failed to copy permissions for %s: %s', target, e)
                await interaction.followup.send(
                    f'Warning: Failed to copy permissions for {target}: {e}',
                    ephemeral=True
                )
        
        success_msg = (
            f'Successfully cloned permissions from **{source_category.name}** to **{destination_category.name}**.\n'
            f'Copied {copied_count} permission overrides.'
        )
        
        notes = []
        if skipped_admin_count > 0:
            notes.append(f'Skipped {skipped_admin_count} Administrator role(s) for security reasons')
        if skipped_managed_count > 0:
            notes.append(f'Skipped {skipped_managed_count} managed role(s) (bot roles, booster roles, etc.)')
        if skipped_hierarchy_count > 0:
            notes.append(f'Skipped {skipped_hierarchy_count} role(s) due to Discord\'s restrictions on bots managing roles with moderation permissions (ban_members, kick_members, etc.)')
        
        if notes:
            success_msg += f'\n\n **Note:** {", ".join(notes)}.'
        
        await interaction.followup.send(success_msg, ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage permissions on one or both categories.\n\n'
            '**Possible causes:**\n'
            '• I lack "Manage Channels" permission\n'
            '• I lack "Manage Roles" permission\n'
            '• My role is not high enough in the hierarchy to modify permissions for some roles/members\n'
            '• Discord restricts bots from managing roles with moderation permissions (ban_members, kick_members, manage_roles, etc.)\n\n'
            '**Solutions:**\n'
            '• Ensure I have "Manage Channels" and "Manage Roles" permissions\n'
            '• Move my role higher than the roles you want to copy permissions for in Server Settings > Roles\n'
            '• For roles with moderation permissions, you\'ll need to copy their permissions manually as Discord prevents bots from managing these roles for security reasons',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clone_category_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred. Probably rate limiting. Trying a workaround...',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clone_category_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while cloning permissions.',
            ephemeral=True
        )

clone_category_permissions_error = _command_error_handler
async def clone_channel_permissions(interaction: discord.Interaction,  # pylint: disable=too-many-branches,too-many-statements,too-many-nested-blocks
                                  source_channel: discord.abc.GuildChannel,
                                  destination_channel: discord.abc.GuildChannel):
    """Clone permissions from source channel to destination channel."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Verify both channels are in the same guild
        if source_channel.guild.id != destination_channel.guild.id:
            await interaction.followup.send(
                'Source and destination channels must be in the same server.',
                ephemeral=True
            )
            return
        
        # Clear existing permissions on destination channel
        await interaction.followup.send(
            f'Clearing existing permissions on {destination_channel.name}...',
            ephemeral=True
        )
        
        # Get the bot's highest role for hierarchy checking
        bot_member = destination_channel.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        # Get all current overwrites and clear them (except Administrator, managed, and hierarchy-protected roles)
        for target in list(destination_channel.overwrites.keys()):
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        logger.info('Skipped clearing Administrator role %s on channel %s', target, destination_channel.name)
                        continue
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        logger.info('Skipped clearing managed role %s on channel %s', target, destination_channel.name)
                        continue
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                        logger.info('Skipped clearing role %s (hierarchy: role position %d >= bot position %d)',
                                   target.name, target.position, bot_top_role.position)
                        continue
                    await destination_channel.set_permissions(target, overwrite=None)
                    logger.info('Cleared permissions for %s on channel %s', target, destination_channel.name)
            except discord.Forbidden as e:
                logger.error('Failed to clear permissions for %s: %s', target, e)
                # Continue without spamming user with individual errors
                continue
            except discord.HTTPException as e:
                logger.error('Failed to clear permissions for %s: %s', target, e)
        
        # Copy permissions from source to destination
        await interaction.followup.send(
            f'Copying permissions from {source_channel.name} to {destination_channel.name}...',
            ephemeral=True
        )
        
        copied_count = 0
        skipped_admin_count = 0
        skipped_managed_count = 0
        skipped_hierarchy_count = 0
        
        # Get the bot's highest role for hierarchy checking
        bot_member = destination_channel.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        for target, overwrite in source_channel.overwrites.items():
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        skipped_admin_count += 1
                        logger.info('Skipped copying Administrator role %s from %s to %s',
                                   target, source_channel.name, destination_channel.name)
                        continue
                    
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        skipped_managed_count += 1
                        logger.info('Skipped copying managed role %s from %s to %s',
                                   target, source_channel.name, destination_channel.name)
                        continue
                    
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                        skipped_hierarchy_count += 1
                        logger.info('Skipped copying role %s (hierarchy: role position %d >= bot position %d)',
                                   target.name, target.position, bot_top_role.position)
                        continue
                    
                    # Check for Discord's restricted permissions that bots cannot manage
                    if isinstance(target, discord.Role):
                        dangerous_perms = [
                            target.permissions.ban_members,
                            target.permissions.kick_members,
                            target.permissions.manage_roles,
                            target.permissions.manage_guild,
                            target.permissions.manage_channels,
                            target.permissions.manage_messages,
                            target.permissions.moderate_members,
                            target.permissions.administrator
                        ]
                        
                        if any(dangerous_perms):
                            skipped_hierarchy_count += 1
                            logger.info('Skipped copying role %s (Discord restricts bots from managing roles with moderation permissions)',
                                       target.name)
                            continue
                    
                    await destination_channel.set_permissions(target, overwrite=overwrite)
                    copied_count += 1
                    logger.info('Copied permissions for %s from %s to %s',
                               target, source_channel.name, destination_channel.name)
            except discord.Forbidden as e:
                # Log the specific role that failed and provide detailed feedback
                if isinstance(target, discord.Role):
                    skipped_hierarchy_count += 1
                    
                    # Check if this is likely due to Discord's moderation permission restrictions
                    dangerous_perms = [
                        target.permissions.ban_members,
                        target.permissions.kick_members,
                        target.permissions.manage_roles,
                        target.permissions.manage_guild,
                        target.permissions.manage_channels,
                        target.permissions.manage_messages,
                        target.permissions.moderate_members,
                        target.permissions.administrator
                    ]
                    
                    if any(dangerous_perms):
                        logger.error('Failed to copy permissions for role %s - Discord restricts bots from managing roles with moderation permissions: %s', target.name, e)
                        await interaction.followup.send(
                            f' **Discord Restriction**: Cannot copy permissions for role **{target.name}** because Discord prevents bots from managing roles with moderation permissions like ban_members, kick_members, manage_roles, etc. You\'ll need to copy these permissions manually.',
                            ephemeral=True
                        )
                    else:
                        logger.error('Failed to copy permissions for role %s (likely hierarchy issue): %s', target.name, e)
                else:
                    logger.error('Failed to copy permissions for %s: %s', target, e)
                continue
            except discord.HTTPException as e:
                logger.error('Failed to copy permissions for %s: %s', target, e)
                await interaction.followup.send(
                    f'Warning: Failed to copy permissions for {target}: {e}',
                    ephemeral=True
                )
        
        success_msg = (
            f'Successfully cloned permissions from **{source_channel.name}** to **{destination_channel.name}**.\n'
            f'Copied {copied_count} permission overrides.'
        )
        
        notes = []
        if skipped_admin_count > 0:
            notes.append(f'Skipped {skipped_admin_count} Administrator role(s) for security reasons')
        if skipped_managed_count > 0:
            notes.append(f'Skipped {skipped_managed_count} managed role(s) (bot roles, booster roles, etc.)')
        if skipped_hierarchy_count > 0:
            notes.append(f'Skipped {skipped_hierarchy_count} role(s) due to hierarchy or Discord\'s restrictions on bots managing roles with moderation permissions')
        
        if notes:
            success_msg += f'\n\n **Note:** {", ".join(notes)}.'
        
        await interaction.followup.send(success_msg, ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage permissions on one or both channels.\n\n'
            '**Possible causes:**\n'
            '• I lack "Manage Channels" permission\n'
            '• I lack "Manage Roles" permission\n'
            '• My role is not high enough in the hierarchy to modify permissions for some roles/members\n'
            '• Discord restricts bots from managing roles with moderation permissions (ban_members, kick_members, etc.)\n'
            '• The channels are in categories I cannot access\n\n'
            '**Solutions:**\n'
            '• Ensure I have "Manage Channels" and "Manage Roles" permissions\n'
            '• Move my role higher than the roles you want to copy permissions for in Server Settings > Roles\n'
            '• Check that I can view and access both channels\n'
            '• For roles with moderation permissions, you\'ll need to copy their permissions manually',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clone_channel_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred. Probably rate limiting. Trying a workaround...',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clone_channel_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while cloning permissions.',
            ephemeral=True
        )

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
            # Create a copy of source permissions but exclude Administrator permission
            new_permissions = discord.Permissions(source_role.permissions.value)
            new_permissions.administrator = False
            
            await destination_role.edit(
                permissions=new_permissions,
                reason=f'Permissions cloned from {source_role.name} by {interaction.user} (Administrator permission excluded)'
            )
            
            # Check if Administrator permission was excluded
            admin_excluded = source_role.permissions.administrator and not new_permissions.administrator
            success_msg = (
                f'Successfully cloned permissions from **{source_role.name}** to **{destination_role.name}**.\n'
                f'The destination role now has the same server-wide permissions as the source role.'
            )
            
            if admin_excluded:
                success_msg += '\n\n **Note:** Administrator permission was excluded for security reasons.'
            
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
            'A Discord API error occurred. Probably rate limiting. Trying a workaround...',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clone_role_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while cloning permissions.',
            ephemeral=True
        )

clone_role_permissions_error = _command_error_handler
async def clear_category_permissions(interaction: discord.Interaction,  # pylint: disable=too-many-branches,too-many-statements,too-many-nested-blocks
                                   category: discord.CategoryChannel):
    """Clear all permission overwrites from a category."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Get the bot's highest role for hierarchy checking
        bot_member = category.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        cleared_count = 0
        skipped_admin_count = 0
        skipped_managed_count = 0
        skipped_hierarchy_count = 0
        failed_roles = []
        
        await interaction.followup.send(
            f'Clearing all permission overwrites from **{category.name}**...',
            ephemeral=True
        )
        
        # Get all current overwrites and clear them
        for target in list(category.overwrites.keys()):
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        skipped_admin_count += 1
                        logger.info('Skipped clearing Administrator role %s on category %s', target, category.name)
                        continue
                    
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        skipped_managed_count += 1
                        logger.info('Skipped clearing managed role %s on category %s', target, category.name)
                        continue
                    
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                        skipped_hierarchy_count += 1
                        logger.info('Skipped clearing role %s (hierarchy: role position %d >= bot position %d)',
                                   target.name, target.position, bot_top_role.position)
                        continue
                    
                    # Check for Discord's restricted permissions that bots cannot manage
                    if isinstance(target, discord.Role):
                        dangerous_perms = [
                            target.permissions.ban_members,
                            target.permissions.kick_members,
                            target.permissions.manage_roles,
                            target.permissions.manage_guild,
                            target.permissions.manage_channels,
                            target.permissions.manage_messages,
                            target.permissions.moderate_members,
                            target.permissions.administrator
                        ]
                        
                        if any(dangerous_perms):
                            skipped_hierarchy_count += 1
                            failed_roles.append(target.name)
                            logger.info('Skipped clearing role %s (Discord restricts bots from managing roles with moderation permissions)',
                                       target.name)
                            continue
                    
                    await category.set_permissions(target, overwrite=None)
                    cleared_count += 1
                    logger.info('Cleared permissions for %s on category %s', target, category.name)
                    
            except discord.Forbidden as e:
                # Log the specific role that failed and provide detailed feedback
                if isinstance(target, discord.Role):
                    skipped_hierarchy_count += 1
                    failed_roles.append(target.name)
                    
                    # Check if this is likely due to Discord's moderation permission restrictions
                    dangerous_perms = [
                        target.permissions.ban_members,
                        target.permissions.kick_members,
                        target.permissions.manage_roles,
                        target.permissions.manage_guild,
                        target.permissions.manage_channels,
                        target.permissions.manage_messages,
                        target.permissions.moderate_members,
                        target.permissions.administrator
                    ]
                    
                    if any(dangerous_perms):
                        logger.error('Failed to clear permissions for role %s - Discord restricts bots from managing roles with moderation permissions: %s', target.name, e)
                        await interaction.followup.send(
                            f' **Discord Restriction**: Cannot clear permissions for role **{target.name}** because Discord prevents bots from managing roles with moderation permissions. You\'ll need to clear these permissions manually.',
                            ephemeral=True
                        )
                    else:
                        logger.error('Failed to clear permissions for role %s (likely hierarchy issue): %s', target.name, e)
                else:
                    logger.error('Failed to clear permissions for %s: %s', target, e)
                continue
            except discord.HTTPException as e:
                logger.error('Failed to clear permissions for %s: %s', target, e)
                await interaction.followup.send(
                    f'Warning: Failed to clear permissions for {target}: {e}',
                    ephemeral=True
                )
        
        success_msg = (
            f'Successfully cleared permissions from **{category.name}**.\n'
            f'Cleared {cleared_count} permission overwrites.'
        )
        
        notes = []
        if skipped_admin_count > 0:
            notes.append(f'Skipped {skipped_admin_count} Administrator role(s) for security reasons')
        if skipped_managed_count > 0:
            notes.append(f'Skipped {skipped_managed_count} managed role(s) (bot roles, booster roles, etc.)')
        if skipped_hierarchy_count > 0:
            notes.append(f'Skipped {skipped_hierarchy_count} role(s) due to Discord\'s restrictions on bots managing roles with moderation permissions')
        
        if notes:
            success_msg += f'\n\n **Note:** {", ".join(notes)}.'
        
        await interaction.followup.send(success_msg, ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage permissions on this category.\n\n'
            '**Possible causes:**\n'
            '• I lack "Manage Channels" permission\n'
            '• I lack "Manage Roles" permission\n'
            '• My role is not high enough in the hierarchy to modify permissions for some roles/members\n'
            '• Discord restricts bots from managing roles with moderation permissions\n\n'
            '**Solutions:**\n'
            '• Ensure I have "Manage Channels" and "Manage Roles" permissions\n'
            '• Move my role higher than the roles you want to clear permissions for\n'
            '• For roles with moderation permissions, you\'ll need to clear their permissions manually',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clear_category_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while clearing permissions.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clear_category_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while clearing permissions.',
            ephemeral=True
        )

clear_category_permissions_error = _command_error_handler
async def clear_channel_permissions(interaction: discord.Interaction,  # pylint: disable=too-many-branches,too-many-statements,too-many-nested-blocks
                                  channel: discord.abc.GuildChannel):
    """Clear all permission overwrites from a channel."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Get the bot's highest role for hierarchy checking
        bot_member = channel.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        cleared_count = 0
        skipped_admin_count = 0
        skipped_managed_count = 0
        skipped_hierarchy_count = 0
        
        await interaction.followup.send(
            f'Clearing all permission overwrites from **{channel.name}**...',
            ephemeral=True
        )
        
        # Get all current overwrites and clear them
        for target in list(channel.overwrites.keys()):
            try:
                # Only process Member and Role objects, skip others
                if isinstance(target, (discord.Member, discord.Role)):
                    # Skip roles with Administrator permission to preserve Server Owner permissions
                    if isinstance(target, discord.Role) and target.permissions.administrator:
                        skipped_admin_count += 1
                        logger.info('Skipped clearing Administrator role %s on channel %s', target, channel.name)
                        continue
                    
                    # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                    if isinstance(target, discord.Role) and target.managed:
                        skipped_managed_count += 1
                        logger.info('Skipped clearing managed role %s on channel %s', target, channel.name)
                        continue
                    
                    # Skip roles that are higher than or equal to the bot's highest role
                    if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                        skipped_hierarchy_count += 1
                        logger.info('Skipped clearing role %s (hierarchy: role position %d >= bot position %d)',
                                   target.name, target.position, bot_top_role.position)
                        continue
                    
                    # Check for Discord's restricted permissions that bots cannot manage
                    if isinstance(target, discord.Role):
                        dangerous_perms = [
                            target.permissions.ban_members,
                            target.permissions.kick_members,
                            target.permissions.manage_roles,
                            target.permissions.manage_guild,
                            target.permissions.manage_channels,
                            target.permissions.manage_messages,
                            target.permissions.moderate_members,
                            target.permissions.administrator
                        ]
                        
                        if any(dangerous_perms):
                            skipped_hierarchy_count += 1
                            logger.info('Skipped clearing role %s (Discord restricts bots from managing roles with moderation permissions)',
                                       target.name)
                            continue
                    
                    await channel.set_permissions(target, overwrite=None)
                    cleared_count += 1
                    logger.info('Cleared permissions for %s on channel %s', target, channel.name)
                    
            except discord.Forbidden as e:
                # Log the specific role that failed and provide detailed feedback
                if isinstance(target, discord.Role):
                    skipped_hierarchy_count += 1
                    
                    # Check if this is likely due to Discord's moderation permission restrictions
                    dangerous_perms = [
                        target.permissions.ban_members,
                        target.permissions.kick_members,
                        target.permissions.manage_roles,
                        target.permissions.manage_guild,
                        target.permissions.manage_channels,
                        target.permissions.manage_messages,
                        target.permissions.moderate_members,
                        target.permissions.administrator
                    ]
                    
                    if any(dangerous_perms):
                        logger.error('Failed to clear permissions for role %s - Discord restricts bots from managing roles with moderation permissions: %s', target.name, e)
                        await interaction.followup.send(
                            f' **Discord Restriction**: Cannot clear permissions for role **{target.name}** because Discord prevents bots from managing roles with moderation permissions. You\'ll need to clear these permissions manually.',
                            ephemeral=True
                        )
                    else:
                        logger.error('Failed to clear permissions for role %s (likely hierarchy issue): %s', target.name, e)
                else:
                    logger.error('Failed to clear permissions for %s: %s', target, e)
                continue
            except discord.HTTPException as e:
                logger.error('Failed to clear permissions for %s: %s', target, e)
                await interaction.followup.send(
                    f'Warning: Failed to clear permissions for {target}: {e}',
                    ephemeral=True
                )
        
        success_msg = (
            f'Successfully cleared permissions from **{channel.name}**.\n'
            f'Cleared {cleared_count} permission overwrites.'
        )
        
        notes = []
        if skipped_admin_count > 0:
            notes.append(f'Skipped {skipped_admin_count} Administrator role(s) for security reasons')
        if skipped_managed_count > 0:
            notes.append(f'Skipped {skipped_managed_count} managed role(s) (bot roles, booster roles, etc.)')
        if skipped_hierarchy_count > 0:
            notes.append(f'Skipped {skipped_hierarchy_count} role(s) due to hierarchy or Discord\'s restrictions on bots managing roles with moderation permissions')
        
        if notes:
            success_msg += f'\n\n **Note:** {", ".join(notes)}.'
        
        await interaction.followup.send(success_msg, ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage permissions on this channel.\n\n'
            '**Possible causes:**\n'
            '• I lack "Manage Channels" permission\n'
            '• I lack "Manage Roles" permission\n'
            '• My role is not high enough in the hierarchy to modify permissions for some roles/members\n'
            '• Discord restricts bots from managing roles with moderation permissions\n\n'
            '**Solutions:**\n'
            '• Ensure I have "Manage Channels" and "Manage Roles" permissions\n'
            '• Move my role higher than the roles you want to clear permissions for\n'
            '• For roles with moderation permissions, you\'ll need to clear their permissions manually',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in clear_channel_permissions: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while clearing permissions.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in clear_channel_permissions: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while clearing permissions.',
            ephemeral=True
        )

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
async def sync_channel_perms(interaction: discord.Interaction,  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-nested-blocks
                           source_category: discord.CategoryChannel):
    """Sync permissions for all channels in the source category with the category's permissions."""
    try:
        await interaction.response.defer(ephemeral=True)
        
        # Check if guild exists
        if not interaction.guild:
            await interaction.followup.send('This command can only be used in a server.', ephemeral=True)
            return
        
        # Get all channels in the source category
        channels_in_category = source_category.channels
        
        if not channels_in_category:
            await interaction.followup.send(f'No channels found in category **{source_category.name}**.', ephemeral=True)
            return
        
        await interaction.followup.send(
            f'Syncing permissions for {len(channels_in_category)} channel(s) in **{source_category.name}** with the category permissions...',
            ephemeral=True
        )
        
        # Get the bot's highest role for hierarchy checking
        bot_member = source_category.guild.me
        bot_top_role = bot_member.top_role if bot_member else None
        
        synced_count = 0
        failed_channels = []
        total_overwrites_synced = 0
        
        for channel in channels_in_category:
            try:
                # Clear existing permissions on the channel first
                cleared_count = 0
                for target in list(channel.overwrites.keys()):
                    try:
                        # Only process Member and Role objects, skip others
                        if isinstance(target, (discord.Member, discord.Role)):
                            # Skip roles with Administrator permission to preserve Server Owner permissions
                            if isinstance(target, discord.Role) and target.permissions.administrator:
                                continue
                            # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                            if isinstance(target, discord.Role) and target.managed:
                                continue
                            # Skip roles that are higher than or equal to the bot's highest role
                            if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                                continue
                            # Check for Discord's restricted permissions that bots cannot manage
                            if isinstance(target, discord.Role):
                                dangerous_perms = [
                                    target.permissions.ban_members,
                                    target.permissions.kick_members,
                                    target.permissions.manage_roles,
                                    target.permissions.manage_guild,
                                    target.permissions.manage_channels,
                                    target.permissions.manage_messages,
                                    target.permissions.moderate_members,
                                    target.permissions.administrator
                                ]
                                if any(dangerous_perms):
                                    continue
                            
                            await channel.set_permissions(target, overwrite=None)
                            cleared_count += 1
                    except (discord.Forbidden, discord.HTTPException):
                        # Continue if we can't clear a specific permission
                        continue
                
                # Copy permissions from category to channel
                copied_count = 0
                for target, overwrite in source_category.overwrites.items():
                    try:
                        # Only process Member and Role objects, skip others
                        if isinstance(target, (discord.Member, discord.Role)):
                            # Skip roles with Administrator permission to preserve Server Owner permissions
                            if isinstance(target, discord.Role) and target.permissions.administrator:
                                continue
                            # Skip managed roles (bot roles, booster roles, etc.) that can't be modified
                            if isinstance(target, discord.Role) and target.managed:
                                continue
                            # Skip roles that are higher than or equal to the bot's highest role
                            if isinstance(target, discord.Role) and bot_top_role and target >= bot_top_role:
                                continue
                            # Check for Discord's restricted permissions that bots cannot manage
                            if isinstance(target, discord.Role):
                                dangerous_perms = [
                                    target.permissions.ban_members,
                                    target.permissions.kick_members,
                                    target.permissions.manage_roles,
                                    target.permissions.manage_guild,
                                    target.permissions.manage_channels,
                                    target.permissions.manage_messages,
                                    target.permissions.moderate_members,
                                    target.permissions.administrator
                                ]
                                if any(dangerous_perms):
                                    continue
                            
                            await channel.set_permissions(target, overwrite=overwrite)
                            copied_count += 1
                    except (discord.Forbidden, discord.HTTPException):
                        # Continue if we can't copy a specific permission
                        continue
                
                synced_count += 1
                total_overwrites_synced += copied_count
                logger.info('Synced permissions for channel %s in category %s (cleared: %d, copied: %d)',
                           channel.name, source_category.name, cleared_count, copied_count)
                
            except (discord.Forbidden, discord.HTTPException) as e:
                failed_channels.append(f"{channel.name} ({type(e).__name__})")
                logger.error('Failed to sync permissions for channel %s: %s', channel.name, e)
        
        # Send results
        success_msg = (
            f'Successfully synced permissions for **{synced_count}** out of **{len(channels_in_category)}** channel(s) in **{source_category.name}**.\n'
            f'Total permission overwrites synced: **{total_overwrites_synced}**'
        )
        
        if failed_channels:
            success_msg += f'\n\n **Failed to sync {len(failed_channels)} channel(s):**\n' + '\n'.join(f'• {name}' for name in failed_channels[:10])
            if len(failed_channels) > 10:
                success_msg += f'\n... and {len(failed_channels) - 10} more'
        
        success_msg += '\n\n📝 **Note:** Skipped Administrator roles, managed roles, and roles with moderation permissions for security reasons.'
        
        await interaction.followup.send(success_msg, ephemeral=True)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to manage permissions on this category or its channels.\n\n'
            '**Possible causes:**\n'
            '• I lack "Manage Channels" permission\n'
            '• I lack "Manage Roles" permission\n'
            '• My role is not high enough in the hierarchy to modify permissions for some roles/members\n'
            '• Discord restricts bots from managing roles with moderation permissions\n\n'
            '**Solutions:**\n'
            '• Ensure I have "Manage Channels" and "Manage Roles" permissions\n'
            '• Move my role higher than the roles you want to sync permissions for\n'
            '• For roles with moderation permissions, you\'ll need to sync their permissions manually',
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in sync_channel_perms: %s', e)
        await interaction.followup.send(
            'A Discord API error occurred while syncing permissions.',
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in sync_channel_perms: %s', e)
        await interaction.followup.send(
            'An unexpected error occurred while syncing permissions.',
            ephemeral=True
        )

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
        
        # Split users into chunks to avoid Discord's 1024 character limit per field
        chunk_size = 20  # Conservative chunk size to stay under 1024 characters
        user_chunks = [users_without_roles[i:i + chunk_size] for i in range(0, len(users_without_roles), chunk_size)]
        
        for i, chunk in enumerate(user_chunks):
            field_name = f"Users {i * chunk_size + 1}-{min((i + 1) * chunk_size, user_count)}"
            user_list = '\n'.join([f"• {member.display_name} ({member.mention})" for member in chunk])
            embed.add_field(name=field_name, value=user_list, inline=False)
        
        # Add footer with additional info
        embed.set_footer(text="Note: This list excludes bots and only shows users with no roles beyond @everyone")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
        logger.info('Listed %d users without roles for user %s in guild %s',
                   user_count, interaction.user, guild.name)
        
    except discord.Forbidden:
        await interaction.followup.send(
            'I don\'t have permission to view server members.\n'
            'Please ensure I have the "View Server Members" permission.',
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
        
        if assigned_members:
            assigned_list = ', '.join([member.display_name for member in assigned_members])
            response_parts.append(f' **Successfully assigned {role.mention} to {len(assigned_members)} member(s):** {assigned_list}')
        
        if already_had_role:
            already_had_list = ', '.join([member.display_name for member in already_had_role])
            response_parts.append(f' **Already had the role ({len(already_had_role)} member(s)):** {already_had_list}')
        
        if failed_to_find:
            failed_find_list = ', '.join(failed_to_find)
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
        
        if removed_members:
            removed_list = ', '.join([member.display_name for member in removed_members])
            response_parts.append(f' **Successfully removed {role.mention} from {len(removed_members)} member(s):** {removed_list}')
        
        if didnt_have_role:
            didnt_have_list = ', '.join([member.display_name for member in didnt_have_role])
            response_parts.append(f' **Didn\'t have the role ({len(didnt_have_role)} member(s)):** {didnt_have_list}')
        
        if failed_to_find:
            failed_find_list = ', '.join(failed_to_find)
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
        # Import config module to modify the setting
        import config
        
        # Update the configuration in the config module
        config.VOICE_CHAPERONE_ENABLED = enabled
        
        status = "enabled" if enabled else "disabled"
        current_status = "✅ Enabled" if config.VOICE_CHAPERONE_ENABLED else "❌ Disabled"
        
        await interaction.response.send_message(
            f'Voice channel chaperone functionality has been **{status}**.\n'
            f'Current status: {current_status}\n\n'
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

voice_chaperone_error = _command_error_handler
async def update_checking_command(interaction: discord.Interaction, enabled: bool):
    """Enable or disable the automatic update checking functionality."""
    try:
        # Import config module to modify the setting
        import config
        
        # Update the configuration in the config module
        config.UPDATE_CHECKING_ENABLED = enabled
        
        status = "enabled" if enabled else "disabled"
        current_status = "✅ Enabled" if config.UPDATE_CHECKING_ENABLED else "❌ Disabled"
        
        await interaction.response.send_message(
            f'Automatic update checking has been **{status}**.\n'
            f'Current status: {current_status}\n\n'
            f'ℹ️ This setting controls whether the bot automatically checks for updates from the GitHub repository '
            f'daily and notifies moderators when updates are available.',
            ephemeral=True
        )
        
        logger.info('Update checking %s by user %s', status, interaction.user)
        
    except Exception as e:
        logger.error('Error in update_checking command: %s', e)
        await interaction.response.send_message(
            'An error occurred while updating the update checking setting.',
            ephemeral=True
        )

update_checking_error = _command_error_handler
def register_update_checking_command():
    """Register the update checking command."""
    if tree is None:
        return

    @tree.command(name='update_checking',
                  description='Enable or disable the automatic update checking functionality')
    @app_commands.describe(enabled='True to enable, False to disable update checking')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _update_checking(interaction: discord.Interaction, enabled: bool):
        await update_checking_command(interaction, enabled)

    # Add error handler for update_checking
    _update_checking.on_error = update_checking_error

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
    import uuid
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

    with autoreplies_lock:
        for rule_id, rule_data in autoreplies.items():
            # Skip if rule is disabled or for different guild
            if not rule_data.get('enabled', True) or rule_data.get('guild_id') != guild_id:
                continue

            trigger_string = rule_data.get('trigger_string', '')
            case_sensitive = rule_data.get('case_sensitive', False)

            # Check if message contains the trigger string
            if case_sensitive:
                contains_trigger = trigger_string in message_content
            else:
                contains_trigger = trigger_string.lower() in message_content.lower()

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
            "/update_checking - Enable or disable automatic update checking",
            "/dashboard - Display this command dashboard"
        ],
        "💬 Autoreply System": [
            "/autoreply add - Add a new autoreply rule",
            "/autoreply list - List all autoreply rules for this server",
            "/autoreply remove - Remove an autoreply rule",
            "/autoreply toggle - Enable or disable an autoreply rule"
        ]
    }

def format_dashboard_message():
    """Format the dashboard message with all commands grouped by category."""
    categories = get_command_categories()
    
    message_parts = [
        "# 📊 JohnnyBot Command Dashboard\n",
        "*All available slash commands organized by category*\n",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    ]
    
    for category, commands in categories.items():
        message_parts.append(f"## {category}\n")
        for command in commands:
            message_parts.append(f"• {command}\n")
        message_parts.append("\n")
    
    message_parts.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
    message_parts.append("*Use `/help <command>` for more details on any command*")
    
    return ''.join(message_parts)

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
            
            # Format and send the dashboard
            dashboard_message = format_dashboard_message()
            
            # Post to the channel (not ephemeral)
            await interaction.channel.send(dashboard_message)
            
            logger.info('Dashboard posted by %s in channel %s',
                       interaction.user, interaction.channel.name if hasattr(interaction.channel, 'name') else 'DM')
            
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to post messages in this channel.",
            ephemeral=True
        )
    except discord.HTTPException as e:
        logger.error('Discord API error in dashboard_command: %s', e)
        await interaction.response.send_message(
            "A Discord API error occurred while posting the dashboard.",
            ephemeral=True
        )
    except Exception as e:
        logger.error('Unexpected error in dashboard_command: %s', e)
        await interaction.response.send_message(
            "An unexpected error occurred while posting the dashboard.",
            ephemeral=True
        )

dashboard_command_error = _command_error_handler
def register_autoreply_commands():
    """Register all autoreply commands."""
    if tree is None:
        return

    # Create autoreply command group
    autoreply_group = app_commands.Group(name='autoreply', description='Manage automatic reply rules')

    @autoreply_group.command(name='add', description='Add a new autoreply rule')
    @app_commands.describe(
        trigger='The string to watch for in messages',
        reply='The message to send when the trigger is found',
        case_sensitive='Whether the trigger matching should be case sensitive (default: False)'
    )
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _autoreply_add(interaction: discord.Interaction, trigger: str, reply: str, case_sensitive: bool = False):
        await autoreply_add_command(interaction, trigger, reply, case_sensitive)

    @autoreply_group.command(name='list', description='List all autoreply rules for this server')
    async def _autoreply_list(interaction: discord.Interaction):
        await autoreply_list_command(interaction)

    @autoreply_group.command(name='remove', description='Remove an autoreply rule')
    @app_commands.describe(rule_id='The ID of the autoreply rule to remove')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _autoreply_remove(interaction: discord.Interaction, rule_id: str):
        await autoreply_remove_command(interaction, rule_id)

    @autoreply_group.command(name='toggle', description='Enable or disable an autoreply rule')
    @app_commands.describe(rule_id='The ID of the autoreply rule to toggle')
    @app_commands.checks.has_role(MODERATOR_ROLE_NAME)
    async def _autoreply_toggle(interaction: discord.Interaction, rule_id: str):
        await autoreply_toggle_command(interaction, rule_id)

    # Add error handlers
    _autoreply_add.on_error = autoreply_command_error
    _autoreply_remove.on_error = autoreply_command_error
    _autoreply_toggle.on_error = autoreply_command_error

    # Add the group to the tree
    tree.add_command(autoreply_group)

autoreply_command_error = _command_error_handler
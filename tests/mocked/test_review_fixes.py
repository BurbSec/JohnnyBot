"""Regression tests for the five review findings fixed in this change.

Each test pins the *behaviour* the bug produced, so a revert fails here
rather than silently shipping again.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import discord
from discord import app_commands

import commands


# ---------------------------------------------------------------------
# B1 — /dashboard exceeded Discord's 2000-character message limit
# ---------------------------------------------------------------------

def test_dashboard_chunks_fit_discord_message_limit():
    chunks = commands.format_dashboard_messages()
    assert len(chunks) > 1, 'dashboard should need more than one message'
    for chunk in chunks:
        assert len(chunk) <= 2000, f'chunk is {len(chunk)} chars'


def test_dashboard_chunks_lose_no_content():
    assert ''.join(commands.format_dashboard_messages()) == \
        commands.format_dashboard_message()


def test_dashboard_lists_the_backup_commands():
    text = commands.format_dashboard_message()
    for cmd in ('/server_backup', '/server_restore', '/auto_backup'):
        assert cmd in text


@pytest.mark.asyncio
async def test_dashboard_command_sends_every_chunk():
    inter = MagicMock()
    inter.user.id = 42
    inter.response.send_message = AsyncMock()
    inter.channel.send = AsyncMock()
    inter.channel.name = 'general'

    commands.dashboard_confirmations[42] = 1  # already confirmed
    try:
        await commands.dashboard_command(inter)
    finally:
        commands.dashboard_confirmations.pop(42, None)

    expected = commands.format_dashboard_messages()
    assert inter.channel.send.await_count == len(expected)
    sent = [c.args[0] for c in inter.channel.send.await_args_list]
    assert sent == expected


# ---------------------------------------------------------------------
# B2 — zone-less feed datetimes were read as UTC instead of local
# ---------------------------------------------------------------------

def test_naive_datetime_is_localized_to_bot_timezone():
    naive = datetime(2026, 9, 15, 19, 0)
    result = commands._localize_naive(naive)  # pylint: disable=protected-access
    assert result.tzinfo is not None
    # 7pm Central, not 7pm UTC — the old code produced 19:00+00:00.
    assert result.utcoffset().total_seconds() != 0
    assert result.hour == 19
    assert result.astimezone(timezone.utc).hour == 0  # next day UTC


def test_allday_midnight_stays_on_its_own_date():
    """DTSTART;VALUE=DATE:20260915 became naive midnight, which as UTC
    displayed as 7pm on the 14th in Central."""
    midnight = datetime(2026, 9, 15, 0, 0)
    localized = commands._localize_naive(midnight)  # pylint: disable=protected-access
    assert localized.astimezone(commands.CENTRAL_TZ).date() == \
        midnight.date()


def test_aware_datetime_is_left_alone():
    aware = datetime(2026, 9, 15, 19, 0, tzinfo=timezone.utc)
    assert commands._localize_naive(aware) is aware  # pylint: disable=protected-access


# ---------------------------------------------------------------------
# B3 — /set_reminder leaked the log tail to unauthorized users
# ---------------------------------------------------------------------

def test_set_reminder_uses_the_permission_gated_error_handler():
    cmd = commands.create_set_reminder_command()
    assert cmd.on_error is commands._command_error_handler  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_missing_permissions_reply_hides_the_log(monkeypatch):
    """The user who triggers MissingPermissions is by definition not a
    moderator, so the log tail must never be appended for them."""
    monkeypatch.setattr(commands, 'get_last_log_line',
                        lambda: 'SECRET-LOG-LINE')
    inter = MagicMock()
    inter.user = MagicMock(spec=discord.User)  # no guild_permissions
    inter.response.is_done.return_value = False
    inter.response.send_message = AsyncMock()

    error = app_commands.errors.MissingPermissions(['manage_messages'])
    await commands._command_error_handler(inter, error)  # pylint: disable=protected-access

    msg = inter.response.send_message.call_args.args[0]
    assert 'SECRET-LOG-LINE' not in msg
    assert 'Last log' not in msg
    assert 'Manage Messages' in msg


# ---------------------------------------------------------------------
# B4 — _invoker_outranks must fail closed
# ---------------------------------------------------------------------

def _outrank_interaction(guild, user_id=1, top_role=None):
    inter = MagicMock()
    inter.guild = guild
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.user.top_role = top_role
    return inter


def test_invoker_outranks_denies_when_rank_is_unresolvable():
    """An unknown rank must deny the kick, not permit it."""
    guild = MagicMock()
    guild.owner_id = 999
    guild.get_member.return_value = None
    target = MagicMock()
    target.id = 200
    target.top_role = None

    inter = _outrank_interaction(guild, user_id=1, top_role=None)
    assert commands._invoker_outranks(inter, target) is False  # pylint: disable=protected-access


def test_invoker_outranks_denies_outside_a_guild():
    inter = _outrank_interaction(None)
    assert commands._invoker_outranks(inter, MagicMock()) is False  # pylint: disable=protected-access


def test_invoker_outranks_handles_absent_owner_id():
    """owner_id is Optional in discord.py; a None must not crash the
    check or silently grant it."""
    guild = MagicMock()
    guild.owner_id = None
    guild.get_member.return_value = None
    target = MagicMock()
    target.id = 200
    target.top_role = None

    inter = _outrank_interaction(guild, user_id=1, top_role=None)
    assert commands._invoker_outranks(inter, target) is False  # pylint: disable=protected-access


# ---------------------------------------------------------------------
# B5 — purge commands silently scanned only the last 100 messages
# ---------------------------------------------------------------------

def _purge_interaction():
    inter = MagicMock()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def _purge_channel():
    channel = MagicMock()
    channel.mention = '#general'
    channel.purge = AsyncMock(return_value=[MagicMock()])
    return channel


@pytest.mark.asyncio
async def test_purge_string_passes_an_explicit_limit():
    channel = _purge_channel()
    await commands.purge_string(_purge_interaction(), channel, 'spam')
    assert channel.purge.await_args.kwargs['limit'] == 1000


@pytest.mark.asyncio
async def test_purge_string_honours_a_caller_supplied_limit():
    channel = _purge_channel()
    await commands.purge_string(
        _purge_interaction(), channel, 'spam', limit=5000)
    assert channel.purge.await_args.kwargs['limit'] == 5000


@pytest.mark.asyncio
async def test_purge_webhooks_passes_an_explicit_limit():
    channel = _purge_channel()
    await commands.purge_webhooks(_purge_interaction(), channel)
    assert channel.purge.await_args.kwargs['limit'] == 1000


# ---------------------------------------------------------------------
# R1 — numeric parameters carry their bounds in the command schema
# ---------------------------------------------------------------------

def _range_of(func, param):
    """app_commands.Range resolves to a RangeTransformer carrying the
    declared bounds as min_value/max_value."""
    import typing
    return typing.get_type_hints(func, include_extras=True)[param]


@pytest.mark.parametrize('func_name,param,lo,hi', [
    ('purge_last_messages', 'limit', 1, 1000),
    ('purge_string', 'limit', 1, 10000),
    ('purge_webhooks', 'limit', 1, 10000),
    ('timeout_member', 'duration', 1, 2419200),   # Discord's 28-day cap
    ('log_tail_command', 'lines', 1, 200),
    ('message_dump_command', 'limit', 1, 100000),
])
def test_numeric_params_declare_bounds(func_name, param, lo, hi):
    rng = _range_of(getattr(commands, func_name), param)
    assert (rng.min_value, rng.max_value) == (lo, hi)


def test_log_tail_lines_cannot_be_negative():
    """A negative `lines` made deque(maxlen=-1) raise ValueError; the
    schema now forbids it."""
    assert _range_of(commands.log_tail_command, 'lines').min_value == 1


def test_reminder_interval_minimum_moved_to_the_schema():
    """validate_reminder_interval / InvalidReminderInterval are gone."""
    assert not hasattr(commands, 'validate_reminder_interval')
    assert not hasattr(commands, 'InvalidReminderInterval')


# ---------------------------------------------------------------------
# B13 — iCal DURATION must be honoured instead of a flat +1 hour
# ---------------------------------------------------------------------

def _vevent(body):
    from icalendar import Calendar
    raw = ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//t//EN\n"
           "BEGIN:VEVENT\n" + body + "\nEND:VEVENT\nEND:VCALENDAR")
    return list(Calendar.from_ical(raw.encode()).walk('VEVENT'))[0]


def _extract(body):
    feed = commands.EventFeed(MagicMock())
    return feed._extract_ical_event(_vevent(body))  # pylint: disable=protected-access


def test_duration_sets_the_real_end_time():
    ev = _extract("UID:c@x\nSUMMARY:Dur\n"
                  "DTSTART:20260915T190000Z\nDURATION:PT3H")
    assert (ev['end_date'] - ev['start_date']).total_seconds() == 3 * 3600


def test_dtend_still_wins_when_present():
    ev = _extract("UID:d@x\nSUMMARY:End\n"
                  "DTSTART:20260915T190000Z\nDTEND:20260915T203000Z")
    assert (ev['end_date'] - ev['start_date']).total_seconds() == 5400


def test_event_without_end_gets_an_hour():
    ev = _extract("UID:e@x\nSUMMARY:NoEnd\nDTSTART:20260915T190000Z")
    assert (ev['end_date'] - ev['start_date']).total_seconds() == 3600


def test_allday_event_spans_the_day():
    ev = _extract("UID:f@x\nSUMMARY:AllDay\nDTSTART;VALUE=DATE:20260915")
    assert ev['start_date'].date().isoformat() == '2026-09-15'
    assert ev['end_date'] > ev['start_date']


def test_vevent_without_dtstart_is_skipped_not_raised():
    assert _extract("UID:g@x\nSUMMARY:Broken") is None

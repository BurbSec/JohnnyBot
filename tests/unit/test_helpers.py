"""Pure-function tests for small helpers."""
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

import bot
from commands import (
    _format_list_with_overflow,
    _parse_members,
    get_time_based_message,
    _morning_bot_messages,
    _afternoon_bot_messages,
    _evening_bot_messages,
    _night_bot_messages,
    _pet_bot_responses,
)


# ── _format_list_with_overflow ──

def test_format_list_short():
    out = _format_list_with_overflow(['a', 'b'])
    assert out == '• a\n• b'


def test_format_list_overflow():
    items = [f'item{i}' for i in range(15)]
    out = _format_list_with_overflow(items, max_shown=10)
    assert 'item0' in out and 'item9' in out
    assert 'item10' not in out
    assert '... and 5 more' in out


def test_format_list_custom_prefix():
    out = _format_list_with_overflow(['x'], prefix='- ')
    assert out == '- x'


# ── get_time_based_message ──

@pytest.mark.parametrize("when,bucket", [
    ("2026-01-01 08:00:00", _morning_bot_messages),
    ("2026-01-01 14:00:00", _afternoon_bot_messages),
    ("2026-01-01 19:00:00", _evening_bot_messages),
    ("2026-01-01 23:00:00", _night_bot_messages),
])
def test_get_time_based_message_picks_right_bucket(when, bucket):
    with freeze_time(when):
        msg = get_time_based_message("Spot")
    # Should be a template from the bucket with BOTNAME substituted
    assert "BOTNAME" not in msg
    assert any(msg == template.replace("BOTNAME", "Spot") for template in bucket)


def test_pet_bot_responses_nonempty():
    assert len(_pet_bot_responses) > 0
    assert all("BOTNAME" in r for r in _pet_bot_responses)


# ── _parse_members ──

def _fake_guild(members_by_id, members_by_name=None):
    guild = MagicMock()
    members_by_name = members_by_name or {}
    guild.get_member.side_effect = lambda uid: members_by_id.get(uid)
    guild.members = list(members_by_id.values())
    return guild


def test_parse_members_mention_format():
    member = MagicMock(name='alice')
    guild = _fake_guild({123: member})
    found, failed = _parse_members(guild, '<@123>')
    assert found == [member]
    assert failed == []


def test_parse_members_raw_id():
    member = MagicMock(name='bob')
    guild = _fake_guild({456: member})
    found, failed = _parse_members(guild, '456')
    assert found == [member] and failed == []


def test_parse_members_unknown_id():
    guild = _fake_guild({})
    found, failed = _parse_members(guild, '<@999>')
    assert found == []
    assert failed == ['<@999>']


def test_parse_members_multiple_separated_by_whitespace():
    a, b = MagicMock(), MagicMock()
    guild = _fake_guild({1: a, 2: b})
    found, failed = _parse_members(guild, '<@1>\n<@2>')
    assert set(found) == {a, b} and failed == []


# ── _parse_repo_from_url ──

def test_parse_repo_simple():
    assert bot._parse_repo_from_url('https://github.com/owner/repo') == 'owner/repo'


def test_parse_repo_with_git_suffix():
    assert bot._parse_repo_from_url('https://github.com/owner/repo.git') == 'owner/repo'


def test_parse_repo_with_trailing_slash():
    assert bot._parse_repo_from_url('https://github.com/owner/repo/') == 'owner/repo'


def test_parse_repo_invalid():
    assert bot._parse_repo_from_url('https://example.com/foo') is None

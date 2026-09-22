"""Static checks on the registered command tree.

discord.py does not validate description length client-side — it just
forwards whatever is registered to Discord's API, which rejects the
entire bulk sync as one unit if ANY command or parameter description
exceeds 100 characters. That failure mode is silent unless someone reads
the log: the bot keeps running on its last successfully synced command
set, so a broken description doesn't surface as "this command is wrong"
but as "no command added since the last sync ever became available".
This is a regression test for exactly that: it walks the real tree that
bot.py builds at import time, with no network involved.
"""
import bot  # noqa: F401  (imported for its module-level setup_commands(bot) call)
from discord import app_commands

import commands

DISCORD_DESCRIPTION_LIMIT = 100


def _walk(cmds, path=''):
    """Yield (full_name, command) for every leaf command, recursing into
    groups like /raid and /autoreply."""
    for c in cmds:
        full = f'{path}{c.name}'
        if isinstance(c, app_commands.Group):
            yield from _walk(c.commands, full + ' ')
        else:
            yield full, c


def _text_len(value):
    """Descriptions come back as plain str or discord.py's locale_str
    (used for localization support), which has no __len__ of its own."""
    return len(str(value)) if value else 0


def test_command_tree_is_populated():
    """Sanity check the fixture itself — a false pass here (0 commands)
    would make every other assertion in this file meaningless."""
    assert commands.tree is not None
    assert len(commands.tree.get_commands()) > 30


def test_every_command_description_is_within_discords_limit():
    over_limit = [
        (full, len(c.description))
        for full, c in _walk(commands.tree.get_commands())
        if len(c.description) > DISCORD_DESCRIPTION_LIMIT
    ]
    assert not over_limit, (
        f'Command description(s) over Discord\'s '
        f'{DISCORD_DESCRIPTION_LIMIT}-char limit — this breaks command '
        f'sync for the ENTIRE tree, not just these commands: {over_limit}')


def test_every_parameter_description_is_within_discords_limit():
    over_limit = []
    for full, c in _walk(commands.tree.get_commands()):
        for pname, param in c._params.items():  # pylint: disable=protected-access
            length = _text_len(param.description)
            if length > DISCORD_DESCRIPTION_LIMIT:
                over_limit.append((full, pname, length))
    assert not over_limit, (
        f'Parameter description(s) over Discord\'s '
        f'{DISCORD_DESCRIPTION_LIMIT}-char limit: {over_limit}')


def test_group_descriptions_are_within_discords_limit():
    over_limit = [
        (c.name, len(c.description))
        for c in commands.tree.get_commands()
        if isinstance(c, app_commands.Group)
        and len(c.description) > DISCORD_DESCRIPTION_LIMIT
    ]
    assert not over_limit, f'Group description(s) over the limit: {over_limit}'

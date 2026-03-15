# JohnnyBot - The Missing Discord Server Management Toolkit

JohnnyBot does all of the stuff Discord bizarrely won't let you do!
Designed to automate tons of server management and enforce some rules while
you're at it. It provides features such as mass role management, message
moderation, permissions cloning, and event feed integration to ensure a smooth
server experience. Most commands are limited to users with the
MODERATOR_ROLE_NAME, however the PetBot commands can be leveraged by all users.

## Documentation

**For complete documentation, installation guides, and command references, visit the [JohnnyBot Wiki](../../wiki).**

### Quick Links
- **[Wiki Home](../../wiki/Home)** - Project overview and features
- **[Setup Guide](../../wiki/Setup-Guide)** - Complete installation and configuration
- **[Commands Reference](../../wiki/Commands-Reference)** - All available commands with examples

## Key Features

### Moderation

| Command | Description | Access |
|---|---|---|
| `/purge_last_messages` | Purge a specified number of messages from a channel | Mod |
| `/purge_string` | Purge all messages containing a specific string from a channel | Mod |
| `/purge_webhooks` | Purge all messages sent by webhooks or apps from a channel | Mod |
| `/kick` | Kick one or more members from the server | Mod |
| `/kick_role` | Kick all members with a specified role from the server | Mod |
| `/timeout` | Timeout a member for a specified duration | Mod |
| `/botsay` | Make the bot send a message to a specified channel | Mod |
| `/message_dump` | Dump a user's messages from a channel into a downloadable file (via temporary hosted link) | Mod |

### Permissions Management

| Command | Description | Access |
|---|---|---|
| `/clone_category_permissions` | Clone permission overwrites from one category to another | Mod |
| `/clone_channel_permissions` | Clone permission overwrites from one channel to another | Mod |
| `/clone_role_permissions` | Clone permissions from one role to another | Mod |
| `/clear_category_permissions` | Clear all permission overwrites from a category | Mod |
| `/clear_channel_permissions` | Clear all permission overwrites from a channel | Mod |
| `/clear_role_permissions` | Reset a role's permissions to default | Mod |
| `/sync_channel_perms` | Sync all channels in a category to match the category's permissions | Mod |

### Role Management

| Command | Description | Access |
|---|---|---|
| `/assign_role` | Mass-assign a role to multiple users at once | Mod |
| `/remove_role` | Mass-remove a role from multiple users at once | Mod |
| `/list_users_without_roles` | List all users with no server roles assigned | Mod |

### Reminders

| Command | Description | Access |
|---|---|---|
| `/set_reminder` | Set a recurring reminder message to a channel at a specified interval | Mod |
| `/list_reminders` | List all active reminders | Mod |
| `/delete_reminder` | Delete a reminder by title | Mod |
| `/delete_all_reminders` | Delete all active reminders | Mod |

### Event Feeds

| Command | Description | Access |
|---|---|---|
| `/add_event_feed` | Subscribe to an iCal or RSS feed (including Meetup.com); auto-detects feed type; configurable description, location, and link display; creates Discord Scheduled Events with duplicate detection | Mod |
| `/list_event_feeds` | List all registered event feeds | Mod |
| `/remove_event_feed` | Remove a feed by name | Mod |
| `/check_event_feeds` | Manually trigger a check of all feeds for new events | Mod |
| `/event_announce` | Enable weekly event announcements posted Mon & Thu at 10am CT; works independently of feeds | Mod |
| `/disable_event_announce` | Disable weekly event announcements | Mod |

### Autoreply

| Command | Description | Access |
|---|---|---|
| `/autoreply add` | Add an autoreply rule with a trigger string, reply text, and optional case sensitivity | Mod |
| `/autoreply list` | List all autoreply rules for this server | Mod |
| `/autoreply remove` | Remove an autoreply rule by ID | Mod |
| `/autoreply toggle` | Enable or disable an autoreply rule by ID | Mod |

### System & Utilities

| Command | Description | Access |
|---|---|---|
| `/voice_chaperone` | Enable/disable automatic voice channel safety monitoring (alerts mods when only 1 adult + 1 child are in a channel) | Mod |
| `/update_checking` | Enable/disable automatic daily checks for new commits on GitHub with moderator notifications | Mod |
| `/log_tail` | DM the last N lines of the bot log to yourself | Mod |
| `/dashboard` | Display all available commands grouped by category | Mod |

### PetBot Interactions

| Command | Description | Access |
|---|---|---|
| `/bot_mood` | Check the bot's current mood | All |
| `/pet_bot` | Pet the bot | All |
| `/bot_pick_fav` | See who the bot prefers between two users today | All |

## Quick Start

1. **Clone and Install:**
   ```shell
   git clone https://github.com/burbsec/johnnybot.git
   cd johnnybot
   pip install -r requirements.txt
   ```

2. **Configure:** Edit [`config.py`](config.py) with your server settings

3. **Set Token:** Add your Discord bot token as environment variable:
   ```shell
   export DISCORD_BOT_TOKEN="your_bot_token_here"
   ```

4. **Run:** `python bot.py`

**Need help?** Check the **[Setup Guide](../../wiki/Setup-Guide)** for detailed instructions.

## Requirements

- **Python 3.8+** (tested up to 3.13)
- **Dependencies:** Listed in [`requirements.txt`](requirements.txt)
- **Network:** TCP port access for message archive hosting (auto-selects a free port)

## Contributing

Contributions are welcome! If you encounter any bugs or have suggestions, feel free to open an issue or submit a pull request.

For detailed development information, see the **[Wiki](../../wiki)**.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

**Attribution:** Bot interaction functionality adapted from [PetBot](https://github.com/0xMetr0/PetBot) under MIT License.

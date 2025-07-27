# JohnnyBot - Discord Moderation Bot

JohnnyBot is a Discord bot designed to automate server management and enforce server rules. It provides features such as automatic role assignment, message deletion, and user management to ensure a smooth server experience. Most commands are limited to users with the MODERATOR_ROLE_NAME, however the PetBot commands can be leveraged by all users.  

## Features

- **Command-based Moderation:**
  - Provides moderators with slash commands to manage members, messages, and post announcements.

- **Voice Channel Chaperone:**
  - Monitors voice channels and mutes all members if only one adult and one child are present
  - Sends an alert to the moderators channel

- **Reminder System:**
  - Set recurring reminders to be sent to specific channels at regular intervals
  - Persistent reminder storage with automatic scheduling

- **Event Feed Integration:**
  - Subscribe to calendar feeds and get notifications for new events
  - Configurable notification channels

- **Logging and Notifications:**
  - Logs actions and errors to a rotating log file in the bot directory
  - DM a tail of the log on request

- **Message Archive:**
  - Allows moderators to dump and archive user messages from specific channels
  - Provides temporary download links for message archives
  - Automatically cleans up old archive files

- **Channel Write Protection:**
  - Deletes any non-moderator messages posted in the `PROTECTED_CHANNELS` you define in `config.py`
  - This is a hack to get around Discord's requirement of a minimum number of messagebale channels.

- **PetBot Interactions:**
  - Includes [PetBot](https://github.com/0xMetr0/PetBot) functionality with time-themed messages

## Requirements

- Python 3.7 (tested up to 3.13)
- All modules in `requirements.txt`
- Firewall rule allowing inbound connections on port TCP port 80 (for message archive hosting)

## Concurrency Model

Note the bot uses both asyncio and threading for different purposes. DO NOT CHANGE THIS:

- **asyncio** is used for:
  - All Discord API interactions (primary event loop)
  - Background tasks (like reminder checking)
  - Command handling
  - Network operations

- **threading** is used for:
  - Synchronous operations that cannot be made async (like file I/O)
  - Thread-safe caching of Discord objects
  - Synchronization primitives (locks) for shared resources

This hybrid approach allows the bot to:
1. Handle Discord async API efficiently
2. Perform blocking operations without stalling the event loop
3. Maintain thread safety for shared resources
4. Scale well under load

## Installation

1. Clone the repository:
   ```shell
   git clone https://github.com/burbsec/johnnybot.git
   ```

2. Install the required dependencies:
   ```shell
   pip install -r requirements.txt
   ```

3. Configure the bot settings:
   - Open the config.py file in a text editor.
   - Modify the following constants according to your server's setup:
     - `MODERATOR_ROLE_NAME`: Name of the modifier role. Necessary for mods to use the bot commands.
     - `PROTECTED_CHANNELS`: Channels you wish to force to read-only when Discord requires them not to be.
     - `MODERATORS_CHANNEL_NAME`: Name of the moderators chat channel for bot notifications to be sent to.

## Registering for a Discord Bot Token

Follow [THIS GUDE](https://www.upwork.com/resources/how-to-make-discord-bot) to register your bot with Discord and receive the `DISCORD_BOT_TOKEN` you will use when running the bot. 

**FOR STEP 8 IN THE ABOVE GUIDE**: JohnnyBot requires specific Discord permissions to function properly. When creating your bot application and generating an invite link, ensure these permissions are selected:

### General Permissions
- **Manage Server** - Required for server management features
- **Manage Roles** - Required for role permission cloning and voice channel safety features
- **Manage Channels** - Required for channel permission cloning and management
- **Kick Members** - Required for the `/kick` command
- **Manage Nicknames** - Required for voice channel safety (muting members)
- **Manage Events** - Required for creating Discord events from calendar feeds
- **View Channels** - Required to access and monitor channels
- **Moderate Members** - Required for the `/timeout` command

### Text Permissions
- **Send Messages** - Required to send bot responses and notifications
- **Manage Messages** - Required for purge commands and protected channel enforcement
- **Read Message History** - Required for message dump functionality and purge commands
- **Use Slash Commands** - Required for all slash command functionality
- **Embed Links** - Required for rich embed messages (event notifications)
- **Attach Files** - Required for log file attachments

### Voice Permissions
- **Connect** - Required to monitor voice channels for safety features
- **Mute Members** - Required for voice channel chaperone functionality
- **Move Members** - Required for voice channel management

### Additional Notes
- The bot does **NOT** require Administrator permissions
- Ensure the bot's role is positioned high enough in the role hierarchy to manage the roles and channels it needs to work with
- For permission cloning commands, the bot cannot clone permissions to/from roles higher than its own highest role


## Running the Bot

1. Ensure all installation steps are complete.
2. Add your Discord bot token to an OS environment variable called `DISCORD_BOT_TOKEN` via any secure means you desire.
3. Run the bot:
   ```shell
   python bot.py
   ```
   
   You may optionally choose to run the bot as a system service that starts at boot (recommended).

4. The bot should now show as online/active in your Discord server. If not, check the logs!

## Available Commands

### 1. `/set_reminder`
**Description:** Sets a reminder message to be sent to a specified channel at regular intervals.

- **Parameters:**
  - `channel`: The target channel.
  - `title`: Title of the reminder.
  - `message`: The reminder content.
  - `interval`: Interval (in seconds) between reminders.

### 2. `/list_reminders`
**Description:** Lists all current reminders.

### 3. `/delete_all_reminders`
**Description:** Deletes all active reminders.

### 4. `/delete_reminder`
**Description:** Deletes a reminder by title.

- **Parameters:**
  - `title`: Title of the reminder to delete.

### 5. `/purge_last_messages`
**Description:** Deletes a specified number of messages from a channel.

- **Parameters:**
  - `channel`: The channel to purge messages from.
  - `limit`: Number of messages to delete.

### 6. `/purge_string`
**Description:** Deletes all messages containing a specific string from a channel.

- **Parameters:**
  - `channel`: The channel to purge messages from.
  - `search_string`: String to search for in messages.

### 7. `/purge_webhooks`
**Description:** Deletes all messages sent by webhooks or apps from a channel.

- **Parameters:**
  - `channel`: The channel to purge messages from.

### 8. `/kick`
**Description:** Kicks a member from the server.

- **Parameters:**
  - `member`: Member to kick.
  - `reason`: Reason for the kick (optional).

### 9. `/botsay`
**Description:** Makes the bot send a message to a specified channel.

- **Parameters:**
  - `channel`: Target channel.
  - `message`: Message to send.

### 10. `/timeout`
**Description:** Timeouts a member for a specified duration.

- **Parameters:**
  - `member`: Member to timeout.
  - `duration`: Timeout duration in seconds.
  - `reason`: Reason for the timeout (optional).

### 11. `/log_tail`
**Description:** Sends the last specified number of lines from the bot's log file to the user via DM.

- **Parameters:**
  - `lines`: Number of lines to retrieve.

### 12. `/add_event_feed`
**Description:** Adds a calendar feed URL to check for events, and posts them to a channel. Adds events to Discord Server Events as well.

- **Parameters:**
  - `calendar_url`: URL of the calendar feed.
  - `channel_name`: Channel to post notifications (default: bot-trap).

### 14. `/list_event_feeds`
**Description:** Lists all registered calendar feeds.

### 15. `/remove_event_feed`
**Description:** Removes a calendar feed.

- **Parameters:**
  - `feed_url`: URL of the calendar feed to remove.

### 16. `/bot_mood`
**Description:** Check on what the PetBot is up to.

### 17. `/pet_bot`
**Description:** Pet JohnnyBot.

### 18. `/bot_pick_fav`
**Description:** See who JohnnyBot prefers today.

- **Parameters:**
  - `user1`: First potential favorite.
  - `user2`: Second potential favorite.

### 19. `/message_dump`
**Description:** Dumps a user's messages from a specified channel into a downloadable file. Compresses the file and hosts it via a temporary web server for 30 minutes. Make sure your firewall rules are set to allow inbound connections on port TCP port 80.

- **Parameters:**
  - `user`: User whose messages to dump.
  - `channel`: Channel to dump messages from.
  - `start_date`: Start date in YYYY-MM-DD format (e.g., 2025-01-01).
  - `limit`: Maximum number of messages to fetch (default: 1000).

- **Features:**
  - Retrieves messages with proper pagination
  - Handles Discord API rate limits
  - Automatically cleans up orphaned dump files
  - Provides a download link via DM
  - Link expires after 30 minutes

### 20. Permission Cloning Commands
**Description:** Clone permissions between categories, channels, or roles. All commands remove existing permissions from the destination before copying new ones.

- **`/clone_category_permissions`, `/clone_channel_permissions`, `/clone_role_permissions`** - Clone permissions from a source to a destination
  - `source_*`: Source category, channel or role to copy permissions from
  - `destination_*`: Destination category, channel or role to copy permissions to
  - *Note: Includes safety checks to prevent privilege escalation*

## Contributing

Contributions are welcome! If you encounter any bugs or have suggestions, feel free to open an issue or submit a pull request.

## Attribution

Bot interaction functionality adapted from [PetBot](https://github.com/0xMetr0/PetBot) under MIT License.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

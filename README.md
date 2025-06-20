# JohnnyBot - Discord Moderation Bot

JohnnyBot is a Discord moderation bot designed to automate role management and enforce server rules. It provides features such as automatic role assignment, message deletion, and user management to ensure a smooth server experience.

## Features

- **Automatic Role Management:**
  - Assigns the "bad bots" role to new members automatically.
  - Removes the "bad bots" role from members who complete onboarding.

- **Moderation Automation:**
  - Deletes messages sent by members with the "bad bots" role in non-DM channels.
  - Kicks members with the "bad bots" role who violate server guidelines.
  - Bans members with the "bad bots" role or no role who DM the bot, deleting their messages across the server.

- **Forum Management:**
  - Automatically replies to new threads in the "🧑💻・job_postings" forum with a required message

- **Command-based Moderation:**
  - Provides moderators with slash commands to manage members, messages, and post announcements.

- **Logging and Notifications:**
  - Logs actions and errors to a rotating log file.
  - Sends notifications to a designated moderators-only channel.

- **Message Archive:**
  - Allows moderators to dump and archive user messages from specific channels
  - Provides temporary download links for message archives
  - Automatically cleans up old archive files

- **Pet Interactions:**
  - Includes JohnnyBot functionality with time-based messages
  - `/pet` command to interact with JohnnyBot

## Requirements

- Python 3.7 or higher
- Official Discord.py library version 2.4 or higher
- Flask 2.0.0 or higher (for message dump web server)
- Waitress 2.1.2 or higher (for production-ready web server)

## Concurrency Model

Note the bot uses both asyncio and threading for different purposes. DON'T CHANGE THIS:

- **asyncio** is used for:
  - All Discord API interactions (primary event loop)
  - Background tasks (like reminder checking)
  - Command handling
  - Network operations

- **threading** is used for:
  - Synchronous operations that can't be made async (like file I/O)
  - Thread-safe caching of Discord objects
  - Synchronization primitives (locks) for shared resources

This hybrid approach allows the bot to:
1. Handle Discord's async API efficiently
2. Perform blocking operations without stalling the event loop
3. Maintain thread safety for shared resources
4. Scale well under load

## Installation

1. Clone the repository:
   ```shell
   git clone https://github.com/yourusername/johnnybot.git
   ```

2. Install the required dependencies:
   ```shell
   pip install -r requirements.txt
   ```

3. Configure the bot settings:
   - Open the `bot.py` file in a text editor.
   - Modify the following constants according to your server's setup:
     - `BAD_BOT_ROLE_NAME`: Role assigned to bad bots (default: 'bad bots').
     - `MODERATOR_ROLE_NAME`: Moderator role name (default: 'Moderators').
     - `DELAY_MINUTES`: Delay before assigning the "bad bots" role to new members (default: 4 minutes).
     - `LOG_FILE`: Name of the log file (default: 'johnnybot.log').
     - `MODERATORS_CHANNEL_NAME`: Name of the moderators channel for notifications (default: 'moderators_only').

## Running the Bot

1. Ensure all installation steps are complete.
2. Add your Discord bot token to an environment variable called `DISCORD_BOT_TOKEN`
3. Run the bot:
   ```shell
   python bot.py
   ```
4. The bot should now be online and moderating your Discord server.

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

### 12. `/add_event_feed_url`
**Description:** Adds a calendar feed URL to check for events.

- **Parameters:**
  - `calendar_url`: URL of the calendar feed.
  - `channel_name`: Channel to post notifications (default: bot-trap).

### 13. `/add_event_feed`
**Description:** Adds a calendar feed to check for events.

- **Parameters:**
  - `calendar_url`: URL of the calendar feed.

### 14. `/list_event_feeds`
**Description:** Lists all registered calendar feeds.

### 15. `/remove_event_feed`
**Description:** Removes a calendar feed.

- **Parameters:**
  - `feed_url`: URL of the calendar feed to remove.

### 16. `/cat`
**Description:** Check on JohnnyBot.

### 17. `/pet_cat`
**Description:** Pet JohnnyBot.

### 18. `/cat_pick_fav`
**Description:** See who JohnnyBot prefers today.

- **Parameters:**
  - `user1`: First potential favorite.
  - `user2`: Second potential favorite.

### 19. `/message_dump`
**Description:** Dumps a user's messages from a specified channel into a downloadable file. Compresses the file and hosts it via a temporary web server for 30 minutes.

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

## Usage

- The bot automatically manages roles and moderates the server based on the configured rules.
- Moderators can perform actions using the slash commands listed above.
- Log files are accessible, and the bot can DM logs on request through the `/log_tail` command.

## Contributing

Contributions are welcome! If you encounter any bugs or have suggestions, feel free to open an issue or submit a pull request.

## Attribution

Cat functionality adapted from [PetBot](https://github.com/0xMetr0/PetBot) under MIT License.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

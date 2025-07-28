# JohnnyBot - The Missing Discord Server Management Toolkit

JohnnyBot does all of the stuff Discord bizarrely won't let you do!
Designed to automate tons of server management and enforce some rules while
you're at it. It provides features such as automatic role assignment, message
deletion, and user management to ensure a smooth server experience. Most
commands are limited to users with the MODERATOR_ROLE_NAME, however the
PetBot commands can be leveraged by all users.

## Documentation

**For complete documentation, installation guides, and command references, visit the [JohnnyBot Wiki](../../wiki).**

### Quick Links
- **[Wiki Home](../../wiki/Home)** - Project overview and features
- **[Setup Guide](../../wiki/Setup-Guide)** - Complete installation and configuration
- **[Commands Reference](../../wiki/Commands-Reference)** - All available commands with examples

## Key Features

- **Command-based Moderation** - Slash commands for member and message management
- **Voice Channel Chaperone** - Automatic safety monitoring for voice channels
- **Reminder System** - Recurring reminders with persistent scheduling
- **Event Feed Integration** - Calendar feed notifications and Discord events
- **Message Archive** - User message dumps with temporary download links
- **Channel Write Protection** - Enforce read-only channels
- **PetBot Interactions** - Fun bot interactions for all users

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

- **Python 3.7+** (tested up to 3.13)
- **Dependencies:** Listed in [`requirements.txt`](requirements.txt)
- **Network:** TCP port 80 access for message archive hosting

## Contributing

Contributions are welcome! If you encounter any bugs or have suggestions, feel free to open an issue or submit a pull request.

For detailed development information, see the **[Wiki](../../wiki)**.

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

**Attribution:** Bot interaction functionality adapted from [PetBot](https://github.com/0xMetr0/PetBot) under MIT License.

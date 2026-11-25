# telegram-private-alerts

A lightweight [Telethon](https://docs.telethon.dev/) userbot that monitors Telegram groups and channels for promo/deal messages and forwards matching alerts (with extracted links and original message URLs) to a private target chat.

## Features

- **Flexible keyword matching:** Case-insensitive, diacritic-insensitive, and order-independent token matching (e.g. `"monitor oled"` matches `"Monitor Samsung OLED"`, and `"eletrica"` matches `"elétrica"`).
- **Two-level exclusions:** Global `exclude_keywords` plus optional per-keyword exclusion lists.
- **Rich alert formatting:** Includes matched keywords, source chat title, direct link to the original message (`t.me/...`), message snippet, and extracted URLs.
- **Live config via Telegram:** Manage keywords, exclusions, and watched chats directly from your alert chat using slash commands—changes persist to `config.jsonc`.

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables:**
   Create a `.env` file in the project root with your Telegram API credentials (from [my.telegram.org](https://my.telegram.org)):
   ```env
   TG_API_ID=123456
   TG_API_HASH=your_api_hash
   TG_SESSION_NAME=telegram_alert_session
   TG_BOT_TOKEN=your_bot_token_here
   ```
   (`TG_BOT_TOKEN` is optional but recommended so alerts are sent by a separate bot and trigger push notifications. Make sure to add the bot to your `alert_chat_id` group or start a DM with it.)

3. **Create `config.jsonc`:**
   Copy `config.jsonc.example` to `config.jsonc` and edit your watched chats, destination `alert_chat_id`, and keywords.
   You can find numeric `peer_id` values for your groups/channels by running:
   ```bash
   python list_chats.py
   ```

4. **Run the monitor:**
   ```bash
   python telegram_alert.py
   ```

## Slash Commands

Send these commands in your configured `alert_chat_id`:

| Command | Description |
| --- | --- |
| `/list` | List watched chats, keywords, and exclusions |
| `/add <keyword> -<excl1> -<excl2>` | Add a keyword with optional `-`-prefixed exclusions |
| `/add_exclusion <keyword>` | Add a global exclusion |
| `/remove <keyword>` | Remove a keyword |
| `/remove_exclusion <keyword>` | Remove a global exclusion |
| `/add_chat <chat ID>` | Add a watched chat (username or numeric ID) |
| `/remove_chat <chat ID>` | Remove a watched chat |
| `/help` | Show available commands |

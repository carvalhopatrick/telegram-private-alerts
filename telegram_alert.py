import json
import os
import re
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_NAME = os.environ["TG_SESSION_NAME"]

def strip_jsonc_comments(text: str) -> str:
    pattern = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
    return pattern.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)


CONFIG_PATH = Path(__file__).with_name("config.jsonc")
with CONFIG_PATH.open("r", encoding="utf-8") as f:
    _config = json.loads(strip_jsonc_comments(f.read()))

# Target chats: usernames (without @) or numeric IDs (e.g., -1001234567890)
SOURCE_CHATS: list[str | int] = _config.get("source_chats", [])

# Destination chat for alerts (private group/channel ID, or None to print only)
ALERT_CHAT_ID: int | None = _config.get("alert_chat_id")

INCLUDE_KEYWORDS: dict[str, list[str]] = _config.get("include_keywords", {})

EXCLUDE_KEYWORDS: list[str] = _config.get("exclude_keywords", [])

URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def extract_urls(event: events.NewMessage.Event) -> list[str]:
    urls = set(URL_PATTERN.findall(event.raw_text or ""))
    if event.entities:
        for entity, text_part in event.get_entities_text():
            if text_part and text_part.startswith(("http://", "https://")):
                urls.add(text_part)
            elif hasattr(entity, "url") and entity.url:
                urls.add(entity.url)
    return list(urls)


def keyword_matches(keyword: str, norm_text: str) -> bool:
    tokens = keyword.lower().split()
    return bool(tokens) and all(token in norm_text for token in tokens)


def matches_filters(raw_text: str) -> tuple[bool, list[str]]:
    if not raw_text:
        return False, []
    norm = normalize_text(raw_text)

    if any(bad.lower() in norm for bad in EXCLUDE_KEYWORDS):
        return False, []

    matched = [
        kw
        for kw, excludes in INCLUDE_KEYWORDS.items()
        if keyword_matches(kw, norm) and not any(bad.lower() in norm for bad in excludes)
    ]
    return (len(matched) > 0), matched


client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


def get_message_link(event: events.NewMessage.Event, chat) -> str | None:
    username = getattr(chat, "username", None)
    if not username:
        usernames = getattr(chat, "usernames", None) or []
        username = next((u.username for u in usernames if getattr(u, "active", True) and getattr(u, "username", None)), None)
    if username:
        return f"https://t.me/{username}/{event.id}"
    chat_id_str = str(event.chat_id or "")
    if chat_id_str.startswith("-100"):
        return f"https://t.me/c/{chat_id_str[4:]}/{event.id}"
    if getattr(event, "is_channel", False) and getattr(chat, "id", None):
        return f"https://t.me/c/{chat.id}/{event.id}"
    return None


@client.on(events.NewMessage(chats=SOURCE_CHATS or None))
async def on_new_message(event: events.NewMessage.Event) -> None:
    # Avoid looping if monitoring a chat where alerts are posted
    if ALERT_CHAT_ID and event.chat_id == ALERT_CHAT_ID:
        return

    text = event.raw_text or ""
    ok, matched = matches_filters(text)
    if not ok:
        return

    chat = await event.get_chat()
    chat_title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)
    msg_link = get_message_link(event, chat)
    urls = extract_urls(event)

    snippet = text.strip().replace("\n", " ")
    if len(snippet) > 350:
        snippet = snippet[:347] + "..."

    links_block = "\n".join(f"- {u}" for u in urls) if urls else "- (no direct URL found)"
    source_line = f"{chat_title} ({msg_link})" if msg_link else chat_title
    alert_msg = (
        f"**PROMO MATCH:**\n {', '.join(matched)}\n\n"
        f"**SOURCE:**\n {source_line}\n\n"
        f"**TEXT:**\n {snippet}\n\n"
        f"**LINKS:**\n{links_block}"
    )

    print(alert_msg + "\n" + "-" * 40)

    if ALERT_CHAT_ID:
        await client.send_message(ALERT_CHAT_ID, alert_msg, link_preview=False)


async def main() -> None:
    await client.start()
    print("Promo monitor running...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
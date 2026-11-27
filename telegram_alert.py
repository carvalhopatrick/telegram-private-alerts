import asyncio
import json
import os
import re
import unicodedata
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_NAME = os.environ["TG_SESSION_NAME"]
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")

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

INCLUDE_KEYWORDS: dict[str, list[str] | dict] = _config.get("include_keywords", {})

EXCLUDE_KEYWORDS: list[str] = _config.get("exclude_keywords", [])

URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
PRICE_PATTERN = re.compile(r"r\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?", re.IGNORECASE)


def save_config() -> None:
    data = {
        "source_chats": SOURCE_CHATS,
        "alert_chat_id": ALERT_CHAT_ID,
        "include_keywords": INCLUDE_KEYWORDS,
        "exclude_keywords": EXCLUDE_KEYWORDS,
    }
    CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def strip_diacritics(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def normalize_for_matching(text: str) -> str:
    return normalize_text(strip_diacritics(text))


def extract_urls(event: events.NewMessage.Event) -> list[str]:
    urls = set(URL_PATTERN.findall(event.raw_text or ""))
    if event.entities:
        for entity, text_part in event.get_entities_text():
            if text_part and text_part.startswith(("http://", "https://")):
                urls.add(text_part)
            elif hasattr(entity, "url") and entity.url:
                urls.add(entity.url)
    return list(urls)


def parse_keyword_rule(rule: list[str] | dict) -> dict:
    if isinstance(rule, list):
        return {
            "excludes": list(rule),
            "channels": [],
            "min_price": None,
            "max_price": None,
        }
    return {
        "excludes": list(rule.get("excludes", [])),
        "channels": list(rule.get("channels", [])),
        "min_price": rule.get("min_price"),
        "max_price": rule.get("max_price"),
    }


def serialize_keyword_rule(rule: dict) -> list[str] | dict:
    excludes = rule.get("excludes", [])
    channels = rule.get("channels", [])
    min_price = rule.get("min_price")
    max_price = rule.get("max_price")
    if not channels and min_price is None and max_price is None:
        return excludes
    data: dict = {}
    if excludes:
        data["excludes"] = excludes
    if channels:
        data["channels"] = channels
    if min_price is not None:
        data["min_price"] = min_price
    if max_price is not None:
        data["max_price"] = max_price
    return data


def extract_prices(raw_text: str) -> list[float]:
    prices: list[float] = []
    for int_part, dec_part in PRICE_PATTERN.findall(raw_text):
        cleaned_int = int_part.replace(".", "")
        num_str = f"{cleaned_int}.{dec_part}" if dec_part else cleaned_int
        try:
            prices.append(float(num_str))
        except ValueError:
            continue
    return prices


def price_matches_rule(prices: list[float], min_price: float | None, max_price: float | None) -> bool:
    if min_price is None and max_price is None:
        return True
    if not prices:
        return True
    return any(
        (min_price is None or p >= min_price) and (max_price is None or p <= max_price)
        for p in prices
    )


def channel_matches_rule(
    channels: list[str | int],
    chat_id: int | None,
    chat_usernames: list[str] | None,
) -> bool:
    if not channels:
        return True
    usernames_lower = {u.lstrip("@").lower() for u in (chat_usernames or []) if u}
    for ch in channels:
        if isinstance(ch, int) and chat_id is not None and chat_id == ch:
            return True
        if isinstance(ch, str) and ch.lstrip("@").lower() in usernames_lower:
            return True
    return False


def keyword_matches(keyword: str, norm_text: str) -> bool:
    tokens = normalize_for_matching(keyword).split()
    return bool(tokens) and all(token in norm_text for token in tokens)


def matches_filters(
    raw_text: str,
    chat_id: int | None = None,
    chat_usernames: list[str] | None = None,
) -> tuple[bool, list[str]]:
    if not raw_text:
        return False, []
    norm = normalize_for_matching(raw_text)

    if any(normalize_for_matching(bad) in norm for bad in EXCLUDE_KEYWORDS):
        return False, []

    prices: list[float] | None = None
    matched: list[str] = []
    for kw, raw_rule in INCLUDE_KEYWORDS.items():
        rule = parse_keyword_rule(raw_rule)
        if not channel_matches_rule(rule["channels"], chat_id, chat_usernames):
            continue
        if not keyword_matches(kw, norm):
            continue
        if any(normalize_for_matching(bad) in norm for bad in rule["excludes"]):
            continue
        if rule["min_price"] is not None or rule["max_price"] is not None:
            if prices is None:
                prices = extract_prices(raw_text)
            if not price_matches_rule(prices, rule["min_price"], rule["max_price"]):
                continue
        matched.append(kw)
    return (len(matched) > 0), matched


client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
bot_client = TelegramClient(f"{SESSION_NAME}_bot", API_ID, API_HASH) if BOT_TOKEN else None


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


def parse_chat_ref(raw: str) -> str | int | None:
    cleaned = raw.strip()
    if not cleaned:
        return None
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    return cleaned.lstrip("@")


def find_case_insensitive_key(mapping: dict[str, list[str]], target: str) -> str | None:
    target_lower = target.lower()
    for key in mapping:
        if key.lower() == target_lower:
            return key
    return None


def find_case_insensitive_item(items: list[str], target: str) -> str | None:
    target_lower = target.lower()
    for item in items:
        if item.lower() == target_lower:
            return item
    return None


def find_chat_entry(chats: list[str | int], target: str | int) -> str | int | None:
    for chat in chats:
        if isinstance(target, int) and isinstance(chat, int) and chat == target:
            return chat
        if isinstance(target, str) and isinstance(chat, str) and chat.lower() == target.lower():
            return chat
    return None


def format_price(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_keyword_rule(kw: str, raw_rule: list[str] | dict) -> str:
    rule = parse_keyword_rule(raw_rule)
    details: list[str] = []
    if rule["excludes"]:
        excl_str = ", ".join(f"`{e}`" for e in rule["excludes"])
        details.append(f"excludes: {excl_str}")
    if rule["channels"]:
        ch_str = ", ".join(f"`{c}`" for c in rule["channels"])
        details.append(f"channels: {ch_str}")
    if rule["min_price"] is not None or rule["max_price"] is not None:
        if rule["min_price"] is not None and rule["max_price"] is not None:
            details.append(f"price: `{format_price(rule['min_price'])}` - `{format_price(rule['max_price'])}`")
        elif rule["min_price"] is not None:
            details.append(f"min: `{format_price(rule['min_price'])}`")
        else:
            details.append(f"max: `{format_price(rule['max_price'])}`")
    if details:
        return f"- `{kw}` ({' | '.join(details)})"
    return f"- `{kw}`"


def format_config_list() -> str:
    if SOURCE_CHATS:
        chats_lines = "\n".join(f"- `{c}`" for c in SOURCE_CHATS)
    else:
        chats_lines = "- (none)"

    if INCLUDE_KEYWORDS:
        keywords_block = "\n".join(
            format_keyword_rule(kw, raw_rule)
            for kw, raw_rule in INCLUDE_KEYWORDS.items()
        )
    else:
        keywords_block = "- (none)"

    if EXCLUDE_KEYWORDS:
        excl_lines = "\n".join(f"- `{e}`" for e in EXCLUDE_KEYWORDS)
    else:
        excl_lines = "- (none)"

    return (
        f"**Watched chats:**\n{chats_lines}\n\n"
        f"**Include keywords:**\n{keywords_block}\n\n"
        f"**Global exclusions:**\n{excl_lines}"
    )


HELP_TEXT = (
    "**Available commands:**\n"
    "- `/list` — List keywords, exclusions, and watched chats\n"
    "- `/add <keyword> [-<excl>...] [ch:<chat> | @<chat>] [min:<price>] [max:<price>]` — Add or update keyword\n"
    "- `/add_exclusion <keyword>` — Add global exclusion\n"
    "- `/remove <keyword>` — Remove keyword\n"
    "- `/remove_exclusion <keyword>` — Remove global exclusion\n"
    "- `/add_chat <chat ID>` — Add watched chat (username or numeric ID)\n"
    "- `/remove_chat <chat ID>` — Remove watched chat"
)


def parse_price_arg(raw: str) -> float | None:
    cleaned = raw.strip().lower().removeprefix("r$").strip()
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_add_args(arg: str) -> tuple[str, list[str], list[str | int], float | None, float | None, str | None]:
    """Parse /add arguments into (keyword, exclusions, channels, min_price, max_price, error)."""
    channels: list[str | int] = []
    min_price: float | None = None
    max_price: float | None = None
    remaining_tokens: list[str] = []

    for token in arg.split():
        lower = token.lower()
        if lower.startswith("ch:"):
            chat_ref = parse_chat_ref(token[3:])
            if chat_ref is None:
                return "", [], [], None, None, f"Invalid channel filter `{token}`."
            if find_chat_entry(channels, chat_ref) is None:
                channels.append(chat_ref)
        elif token.startswith("@") and len(token) > 1:
            chat_ref = parse_chat_ref(token)
            if chat_ref is not None and find_chat_entry(channels, chat_ref) is None:
                channels.append(chat_ref)
        elif lower.startswith("min:"):
            val = parse_price_arg(token[4:])
            if val is None:
                return "", [], [], None, None, f"Invalid min price `{token}`."
            min_price = val
        elif lower.startswith("max:"):
            val = parse_price_arg(token[4:])
            if val is None:
                return "", [], [], None, None, f"Invalid max price `{token}`."
            max_price = val
        else:
            remaining_tokens.append(token)

    if min_price is not None and max_price is not None and min_price > max_price:
        return "", [], [], None, None, "Min price cannot be greater than max price."

    remaining = " ".join(remaining_tokens).strip()
    if not remaining or remaining.startswith("-"):
        return "", [], [], None, None, "Usage: `/add <keyword> [-<excl>...] [ch:<chat>] [min:<price>] [max:<price>]`"

    segments = re.split(r"\s+-", remaining)
    keyword = normalize_text(segments[0])
    if not keyword:
        return "", [], [], None, None, "Usage: `/add <keyword> [-<excl>...] [ch:<chat>] [min:<price>] [max:<price>]`"

    unique_exclusions: list[str] = []
    for raw_excl in segments[1:]:
        excl = normalize_text(raw_excl)
        if excl and find_case_insensitive_item(unique_exclusions, excl) is None:
            unique_exclusions.append(excl)

    return keyword, unique_exclusions, channels, min_price, max_price, None


def handle_command(raw_text: str) -> tuple[str | None, bool]:
    """Process a slash command and return (reply_text, chats_changed)."""
    stripped = raw_text.strip()
    if not stripped.startswith("/"):
        return None, False

    parts = stripped.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/list":
        return format_config_list(), False

    if cmd == "/help":
        return HELP_TEXT, False

    if cmd == "/add":
        if not arg:
            return "Usage: `/add <keyword> [-<excl>...] [ch:<chat>] [min:<price>] [max:<price>]`", False
        keyword, exclusions, channels, min_price, max_price, err = parse_add_args(arg)
        if err:
            return err, False

        existing_key = find_case_insensitive_key(INCLUDE_KEYWORDS, keyword)
        if existing_key is not None:
            rule = parse_keyword_rule(INCLUDE_KEYWORDS[existing_key])
            changed = False
            for excl in exclusions:
                if find_case_insensitive_item(rule["excludes"], excl) is None:
                    rule["excludes"].append(excl)
                    changed = True
            for ch in channels:
                if find_chat_entry(rule["channels"], ch) is None:
                    rule["channels"].append(ch)
                    changed = True
            if min_price is not None and rule["min_price"] != min_price:
                rule["min_price"] = min_price
                changed = True
            if max_price is not None and rule["max_price"] != max_price:
                rule["max_price"] = max_price
                changed = True
            if rule["min_price"] is not None and rule["max_price"] is not None and rule["min_price"] > rule["max_price"]:
                return "Min price cannot be greater than max price.", False
            if not changed:
                return f"Keyword `{existing_key}` already has those settings.", False
            INCLUDE_KEYWORDS[existing_key] = serialize_keyword_rule(rule)
            save_config()
            return f"Updated {format_keyword_rule(existing_key, INCLUDE_KEYWORDS[existing_key])[2:]}", False

        rule = {
            "excludes": exclusions,
            "channels": channels,
            "min_price": min_price,
            "max_price": max_price,
        }
        INCLUDE_KEYWORDS[keyword] = serialize_keyword_rule(rule)
        save_config()
        return f"Added {format_keyword_rule(keyword, INCLUDE_KEYWORDS[keyword])[2:]}", False

    if cmd == "/add_exclusion":
        excl = normalize_text(arg)
        if not excl:
            return "Usage: `/add_exclusion <keyword>`", False
        if find_case_insensitive_item(EXCLUDE_KEYWORDS, excl) is not None:
            return f"Global exclusion `{excl}` already exists.", False
        EXCLUDE_KEYWORDS.append(excl)
        save_config()
        return f"Added global exclusion `{excl}`.", False

    if cmd == "/remove":
        kw = normalize_text(arg)
        if not kw:
            return "Usage: `/remove <keyword>`", False
        existing_key = find_case_insensitive_key(INCLUDE_KEYWORDS, kw)
        if existing_key is None:
            return f"Keyword `{kw}` not found.", False
        del INCLUDE_KEYWORDS[existing_key]
        save_config()
        return f"Removed keyword `{existing_key}`.", False

    if cmd == "/remove_exclusion":
        excl = normalize_text(arg)
        if not excl:
            return "Usage: `/remove_exclusion <keyword>`", False
        existing = find_case_insensitive_item(EXCLUDE_KEYWORDS, excl)
        if existing is None:
            return f"Global exclusion `{excl}` not found.", False
        EXCLUDE_KEYWORDS.remove(existing)
        save_config()
        return f"Removed global exclusion `{existing}`.", False

    if cmd == "/add_chat":
        chat_ref = parse_chat_ref(arg)
        if chat_ref is None:
            return "Usage: `/add_chat <chat ID>`", False
        if find_chat_entry(SOURCE_CHATS, chat_ref) is not None:
            return f"Chat `{chat_ref}` is already watched.", False
        SOURCE_CHATS.append(chat_ref)
        save_config()
        return f"Added watched chat `{chat_ref}`.", True

    if cmd == "/remove_chat":
        chat_ref = parse_chat_ref(arg)
        if chat_ref is None:
            return "Usage: `/remove_chat <chat ID>`", False
        existing = find_chat_entry(SOURCE_CHATS, chat_ref)
        if existing is None:
            return f"Watched chat `{chat_ref}` not found.", False
        SOURCE_CHATS.remove(existing)
        save_config()
        return f"Removed watched chat `{existing}`.", True

    return f"Unknown command `{cmd}`.\n\n{HELP_TEXT}", False


async def on_new_message(event: events.NewMessage.Event) -> None:
    # Avoid looping if monitoring a chat where alerts are posted
    if ALERT_CHAT_ID and event.chat_id == ALERT_CHAT_ID:
        return

    text = event.raw_text or ""
    if not text:
        return

    chat = await event.get_chat()
    chat_usernames = []
    if getattr(chat, "username", None):
        chat_usernames.append(chat.username)
    for u in getattr(chat, "usernames", None) or []:
        if getattr(u, "active", True) and getattr(u, "username", None):
            chat_usernames.append(u.username)

    ok, matched = matches_filters(text, chat_id=event.chat_id, chat_usernames=chat_usernames)
    if not ok:
        return

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
        sender = bot_client or client
        await sender.send_message(ALERT_CHAT_ID, alert_msg, link_preview=False)


def register_source_handler() -> None:
    client.remove_event_handler(on_new_message)
    if SOURCE_CHATS:
        client.add_event_handler(on_new_message, events.NewMessage(chats=SOURCE_CHATS))


async def on_command(event: events.NewMessage.Event) -> None:
    text = event.raw_text or ""
    reply, chats_changed = handle_command(text)
    if reply is None:
        return
    if chats_changed:
        register_source_handler()
    await event.reply(reply, link_preview=False)


async def main() -> None:
    await client.start()
    register_source_handler()
    command_client = bot_client or client

    if bot_client:
        await bot_client.start(bot_token=BOT_TOKEN)

    if ALERT_CHAT_ID:
        command_client.add_event_handler(on_command, events.NewMessage(chats=[ALERT_CHAT_ID]))

    print("Promo monitor running...")
    try:
        if bot_client:
            await asyncio.gather(
                client.run_until_disconnected(),
                bot_client.run_until_disconnected(),
            )
        else:
            await client.run_until_disconnected()
    finally:
        if bot_client:
            await bot_client.disconnect()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
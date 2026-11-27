import asyncio
from datetime import datetime, timezone
import json
import os
import re
import unicodedata
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient, events

from historical_alarm import (
    DEFAULT_HISTORICAL_CONFIG,
    HistoricalAlarmEngine,
    HistoryStore,
    sync_history,
)

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

# Test chats: monitored for alerts & Historical Alarm, but never saved to history_cache.db
TEST_CHATS: list[str | int] = _config.get("test_chats", [])

# Destination chat for alerts (private group/channel ID, or None to print only)
ALERT_CHAT_ID: int | None = _config.get("alert_chat_id")

# Admin Telegram username (without @) allowed to run protected commands like /hist sync
ADMIN_USERNAME: str | None = _config.get("admin_username")

INCLUDE_KEYWORDS: dict[str, list[str] | dict] = _config.get("include_keywords", {})

EXCLUDE_KEYWORDS: list[str] = _config.get("exclude_keywords", [])

HISTORICAL_ALARM: dict = {
    **DEFAULT_HISTORICAL_CONFIG,
    **_config.get("historical_alarm", {}),
}

HISTORY_DB_PATH = Path(__file__).with_name("history_cache.db")
_history_store: HistoryStore | None = None
_history_engine: HistoricalAlarmEngine | None = None
_sync_lock = asyncio.Lock()

URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
PRICE_PATTERN = re.compile(r"r\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?", re.IGNORECASE)


def get_history_store() -> HistoryStore:
    global _history_store
    if _history_store is None:
        _history_store = HistoryStore(HISTORY_DB_PATH)
    return _history_store


def get_history_engine() -> HistoricalAlarmEngine:
    global _history_engine
    if _history_engine is None:
        _history_engine = HistoricalAlarmEngine(get_history_store())
    return _history_engine


def save_config() -> None:
    data = {
        "source_chats": SOURCE_CHATS,
        "test_chats": TEST_CHATS,
        "alert_chat_id": ALERT_CHAT_ID,
        "admin_username": ADMIN_USERNAME,
        "include_keywords": INCLUDE_KEYWORDS,
        "exclude_keywords": EXCLUDE_KEYWORDS,
        "historical_alarm": HISTORICAL_ALARM,
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


def is_wildcard_keyword(keyword: str) -> bool:
    return keyword.strip().startswith("*")


def keyword_matches(keyword: str, norm_text: str) -> bool:
    if is_wildcard_keyword(keyword):
        return bool(norm_text)
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
    "- `/remove_chat <chat ID>` — Remove watched chat\n"
    "- `/hist` — Show Historical Alarm status & cache stats\n"
    "- `/hist on` | `/hist off` — Enable or disable Historical Alarm\n"
    "- `/hist min <price>` — Set minimum price for Historical Alarm\n"
    "- `/hist percent <pct>` — Set % below historical low to trigger alarm\n"
    "- `/hist add_chat <chat ID>` | `/hist remove_chat <chat ID>` — Manage searched chats\n"
    "- `/hist sync` — Sync 60-day history cache (admin only)"
)


def format_historical_status() -> str:
    cfg = HISTORICAL_ALARM
    status = "ON" if cfg.get("enabled") else "OFF"
    chats = cfg.get("chats") or []
    chats_str = ", ".join(f"`{c}`" for c in chats) if chats else "(uses `source_chats`)"
    stats = get_history_store().get_stats()
    if stats["oldest_ts"] and stats["newest_ts"]:
        oldest = datetime.fromtimestamp(stats["oldest_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        newest = datetime.fromtimestamp(stats["newest_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        range_str = f"{oldest} to {newest}"
    else:
        range_str = "empty"

    return (
        f"**Historical Alarm:** `{status}`\n"
        f"- Min price: `{format_price(float(cfg.get('min_price', 500.0)))}`\n"
        f"- Drop threshold: `{cfg.get('drop_percent', 15.0)}%` below lowest\n"
        f"- Lookback: `{cfg.get('lookback_days', 60)}` days (min samples: `{cfg.get('min_samples', 2)}`)\n"
        f"- Search chats: {chats_str}\n"
        f"- Cache: `{stats['count']}` deals across `{stats['chats']}` chats ({range_str})"
    )


def handle_hist_command(arg: str, sender_username: str | None) -> tuple[str, bool]:
    """Handle /hist subcommands. Returns (reply_text, sync_requested)."""
    parts = arg.split(maxsplit=1)
    sub = parts[0].lower() if parts else "status"
    val = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("", "status"):
        return format_historical_status(), False

    if sub == "on":
        HISTORICAL_ALARM["enabled"] = True
        save_config()
        stats = get_history_store().get_stats()
        need_sync = stats["count"] == 0
        extra = " Cache is empty — syncing history in the background." if need_sync else ""
        return f"Historical Alarm enabled.{extra}", need_sync

    if sub == "off":
        HISTORICAL_ALARM["enabled"] = False
        save_config()
        return "Historical Alarm disabled.", False

    if sub == "min":
        price = parse_price_arg(val)
        if price is None:
            return "Usage: `/hist min <price>` (e.g. `/hist min 500`)", False
        HISTORICAL_ALARM["min_price"] = price
        save_config()
        return f"Historical Alarm min price set to `{format_price(price)}`.", False

    if sub == "percent":
        cleaned = val.strip().rstrip("%").replace(",", ".")
        try:
            pct = float(cleaned)
        except ValueError:
            pct = -1.0
        if not (0 < pct < 100):
            return "Usage: `/hist percent <1-99>` (e.g. `/hist percent 15`)", False
        HISTORICAL_ALARM["drop_percent"] = pct
        save_config()
        return f"Historical Alarm drop threshold set to `{pct:g}%`.", False

    if sub == "add_chat":
        chat_ref = parse_chat_ref(val)
        if chat_ref is None:
            return "Usage: `/hist add_chat <chat ID>`", False
        chats = HISTORICAL_ALARM.setdefault("chats", [])
        if find_chat_entry(chats, chat_ref) is not None:
            return f"Chat `{chat_ref}` is already in Historical Alarm search chats.", False
        chats.append(chat_ref)
        save_config()
        return f"Added `{chat_ref}` to Historical Alarm search chats.", False

    if sub == "remove_chat":
        chat_ref = parse_chat_ref(val)
        if chat_ref is None:
            return "Usage: `/hist remove_chat <chat ID>`", False
        chats = HISTORICAL_ALARM.setdefault("chats", [])
        existing = find_chat_entry(chats, chat_ref)
        if existing is None:
            return f"Chat `{chat_ref}` not found in Historical Alarm search chats.", False
        chats.remove(existing)
        save_config()
        return f"Removed `{existing}` from Historical Alarm search chats.", False

    if sub == "sync":
        if not ADMIN_USERNAME:
            return "Permission denied: `admin_username` is not configured in `config.jsonc`.", False
        if not sender_username or sender_username.lstrip("@").lower() != ADMIN_USERNAME.lstrip("@").lower():
            return "Permission denied: `/hist sync` is restricted to the configured admin user.", False
        if _sync_lock.locked():
            return "A historical sync is already in progress.", False
        return "Starting historical history sync in the background...", True

    return (
        "Usage: `/hist [status | on | off | min <price> | percent <pct> | add_chat <id> | remove_chat <id> | sync]`",
        False,
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


def handle_command(raw_text: str, sender_username: str | None = None) -> tuple[str | None, bool, bool]:
    """Process a slash command and return (reply_text, chats_changed, sync_requested)."""
    stripped = raw_text.strip()
    if not stripped.startswith("/"):
        return None, False, False

    parts = stripped.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/list":
        return format_config_list(), False, False

    if cmd == "/help":
        return HELP_TEXT, False, False

    if cmd == "/hist":
        reply, sync_requested = handle_hist_command(arg, sender_username)
        return reply, False, sync_requested

    if cmd == "/add":
        if not arg:
            return "Usage: `/add <keyword> [-<excl>...] [ch:<chat>] [min:<price>] [max:<price>]`", False, False
        keyword, exclusions, channels, min_price, max_price, err = parse_add_args(arg)
        if err:
            return err, False, False

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
                return "Min price cannot be greater than max price.", False, False
            if not changed:
                return f"Keyword `{existing_key}` already has those settings.", False, False
            INCLUDE_KEYWORDS[existing_key] = serialize_keyword_rule(rule)
            save_config()
            return f"Updated {format_keyword_rule(existing_key, INCLUDE_KEYWORDS[existing_key])[2:]}", False, False

        rule = {
            "excludes": exclusions,
            "channels": channels,
            "min_price": min_price,
            "max_price": max_price,
        }
        INCLUDE_KEYWORDS[keyword] = serialize_keyword_rule(rule)
        save_config()
        msg = f"Added {format_keyword_rule(keyword, INCLUDE_KEYWORDS[keyword])[2:]}"
        if is_wildcard_keyword(keyword) and not channels:
            msg += "\nNote: no channel filter set — this wildcard will match all watched chats."
        return msg, False, False

    if cmd == "/add_exclusion":
        excl = normalize_text(arg)
        if not excl:
            return "Usage: `/add_exclusion <keyword>`", False, False
        if find_case_insensitive_item(EXCLUDE_KEYWORDS, excl) is not None:
            return f"Global exclusion `{excl}` already exists.", False, False
        EXCLUDE_KEYWORDS.append(excl)
        save_config()
        return f"Added global exclusion `{excl}`.", False, False

    if cmd == "/remove":
        kw = normalize_text(arg)
        if not kw:
            return "Usage: `/remove <keyword>`", False, False
        existing_key = find_case_insensitive_key(INCLUDE_KEYWORDS, kw)
        if existing_key is None:
            return f"Keyword `{kw}` not found.", False, False
        del INCLUDE_KEYWORDS[existing_key]
        save_config()
        return f"Removed keyword `{existing_key}`.", False, False

    if cmd == "/remove_exclusion":
        excl = normalize_text(arg)
        if not excl:
            return "Usage: `/remove_exclusion <keyword>`", False, False
        existing = find_case_insensitive_item(EXCLUDE_KEYWORDS, excl)
        if existing is None:
            return f"Global exclusion `{excl}` not found.", False, False
        EXCLUDE_KEYWORDS.remove(existing)
        save_config()
        return f"Removed global exclusion `{existing}`.", False, False

    if cmd == "/add_chat":
        chat_ref = parse_chat_ref(arg)
        if chat_ref is None:
            return "Usage: `/add_chat <chat ID>`", False, False
        if find_chat_entry(SOURCE_CHATS, chat_ref) is not None:
            return f"Chat `{chat_ref}` is already watched.", False, False
        SOURCE_CHATS.append(chat_ref)
        save_config()
        return f"Added watched chat `{chat_ref}`.", True, False

    if cmd == "/remove_chat":
        chat_ref = parse_chat_ref(arg)
        if chat_ref is None:
            return "Usage: `/remove_chat <chat ID>`", False, False
        existing = find_chat_entry(SOURCE_CHATS, chat_ref)
        if existing is None:
            return f"Watched chat `{chat_ref}` not found.", False, False
        SOURCE_CHATS.remove(existing)
        save_config()
        return f"Removed watched chat `{existing}`.", True, False

    return f"Unknown command `{cmd}`.\n\n{HELP_TEXT}", False, False


async def run_background_sync(notify: bool = False) -> None:
    if _sync_lock.locked():
        return
    async with _sync_lock:
        raw_chats = HISTORICAL_ALARM.get("chats") or SOURCE_CHATS
        chats = [c for c in raw_chats if find_chat_entry(TEST_CHATS, c) is None]
        lookback_days = int(HISTORICAL_ALARM.get("lookback_days", 60))
        store = get_history_store()
        if TEST_CHATS:
            store.delete_chats(TEST_CHATS)
        indexed, synced_chats = await sync_history(client, store, chats, lookback_days)
        summary = (
            f"Historical sync complete: indexed `{indexed}` new deals "
            f"across `{synced_chats}` chats (last `{lookback_days}` days)."
        )
        print(f"[historical_alarm] {summary}")
        if notify and ALERT_CHAT_ID:
            sender = bot_client or client
            await sender.send_message(ALERT_CHAT_ID, summary, link_preview=False)


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

    chat_title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)
    msg_link = get_message_link(event, chat)

    # Passively record deal messages into the local SQLite history cache (skipping test_chats)
    is_test_chat = bool(TEST_CHATS) and channel_matches_rule(TEST_CHATS, event.chat_id, chat_usernames)
    hist_chats = HISTORICAL_ALARM.get("chats") or []
    in_hist_scope = not hist_chats or channel_matches_rule(hist_chats, event.chat_id, chat_usernames)
    if in_hist_scope and not is_test_chat and event.chat_id is not None:
        chat_key = (chat_usernames[0] if chat_usernames else str(event.chat_id)).lower()
        msg_dt = event.date or datetime.now(timezone.utc)
        if msg_dt.tzinfo is None:
            msg_dt = msg_dt.replace(tzinfo=timezone.utc)
        get_history_store().record_message(
            chat_key=chat_key,
            chat_id=event.chat_id,
            msg_id=event.id,
            msg_date=int(msg_dt.timestamp()),
            raw_text=text,
            msg_link=msg_link,
        )

    ok, matched = matches_filters(text, chat_id=event.chat_id, chat_usernames=chat_usernames)
    urls = extract_urls(event)
    snippet = text.strip().replace("\n", " ")
    if len(snippet) > 350:
        snippet = snippet[:347] + "..."
    links_block = "\n".join(f"- {u}" for u in urls) if urls else "- (no direct URL found)"
    source_line = f"{chat_title} ({msg_link})" if msg_link else chat_title

    if ok:
        alert_msg = (
            f"**PROMO MATCH:**\n {', '.join(matched)}\n\n"
            f"**SOURCE:**\n {source_line}\n\n"
            f"**TEXT:**\n {snippet}\n\n"
            f"**LINKS:**\n{links_block}"
        )
    elif (in_hist_scope or is_test_chat) and HISTORICAL_ALARM.get("enabled"):
        non_test_sources = [c for c in SOURCE_CHATS if find_chat_entry(TEST_CHATS, c) is None]
        hist_match = get_history_engine().evaluate(
            raw_text=text,
            config=HISTORICAL_ALARM,
            fallback_chats=non_test_sources,
            chat_id=event.chat_id,
            msg_id=event.id,
        )
        if hist_match is None:
            return
        prev_date = datetime.fromtimestamp(hist_match.lowest_deal.msg_date, tz=timezone.utc).strftime("%Y-%m-%d")
        prev_ref = (
            f"{format_price(hist_match.lowest_historical_price)} on {prev_date} ({hist_match.lowest_deal.msg_link})"
            if hist_match.lowest_deal.msg_link
            else f"{format_price(hist_match.lowest_historical_price)} on {prev_date}"
        )
        alert_msg = (
            f"**HISTORICAL ALARM (-{hist_match.drop_percent:.1f}%):**\n"
            f" {hist_match.title}\n"
            f" Now: `{format_price(hist_match.current_price)}` | "
            f"Previous low ({hist_match.sample_count} deals): {prev_ref}\n\n"
            f"**SOURCE:**\n {source_line}\n\n"
            f"**TEXT:**\n {snippet}\n\n"
            f"**LINKS:**\n{links_block}"
        )
    else:
        return

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
    if not text.strip().startswith("/"):
        return
    sender = await event.get_sender()
    sender_username = getattr(sender, "username", None)
    reply, chats_changed, sync_requested = handle_command(text, sender_username=sender_username)
    if reply is None:
        return
    if chats_changed:
        register_source_handler()
    await event.reply(reply, link_preview=False)
    if sync_requested:
        asyncio.create_task(run_background_sync(notify=True))


async def main() -> None:
    await client.start()
    register_source_handler()
    command_client = bot_client or client

    if bot_client:
        await bot_client.start(bot_token=BOT_TOKEN)

    if ALERT_CHAT_ID:
        command_client.add_event_handler(on_command, events.NewMessage(chats=[ALERT_CHAT_ID]))

    if HISTORICAL_ALARM.get("enabled"):
        asyncio.create_task(run_background_sync(notify=False))

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
        if _history_store is not None:
            _history_store.close()
        if bot_client:
            await bot_client.disconnect()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
import asyncio
import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from telethon import TelegramClient
from telethon.errors import FloodWaitError

PRICE_PATTERN = re.compile(r"r\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

INSTALLMENT_BEFORE_RE = re.compile(
    r"(?:\d{1,2}\s*x\s*(?:de\s*)?|parcela(?:s|do)?\s*(?:de\s*)?)$",
    re.IGNORECASE,
)
DISCOUNT_BEFORE_RE = re.compile(
    r"(?:cupom|desconto|economize|economia|frete|cashback|off|abatimento)\s*(?:de\s*)?$",
    re.IGNORECASE,
)
DISCOUNT_AFTER_RE = re.compile(
    r"^\s*(?:off\b|de\s+desconto\b|em\s+cashback\b)",
    re.IGNORECASE,
)

SKIP_LINE_RE = re.compile(
    r"^(?:https?://|r\$|cupom\b|codigo\b|frete\b|atencao\b|aviso\b|link\b|compre\b|acesse\b|aproveite\b|corre\b|corra\b|use\s+o\s+cupom\b)",
    re.IGNORECASE,
)

STOPWORDS = {
    "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "da", "do", "das", "dos",
    "em", "no", "na", "nos", "nas", "por", "para", "pra", "com", "sem", "sob", "sobre",
    "e", "ou", "que", "se", "ao", "aos", "ate", "mais", "menos", "muito", "so", "ja",
    "promocao", "promo", "oferta", "ofertas", "menor", "preco", "historico", "imperdivel",
    "cupom", "cupons", "frete", "gratis", "pix", "boleto", "cartao", "vista", "parcelado",
    "novo", "nova", "original", "oficial", "loja", "vendido", "entregue", "amazon",
    "kabum", "terabyte", "pichau", "magalu", "magazine", "mercado", "livre", "ml",
    "shopee", "aliexpress", "americanas", "casas", "bahia", "fast", "shop", "carrefour",
    "extra", "ponto", "submarino", "desconto", "off", "cashback", "prime", "full",
    "edicao", "edition", "versao", "version", "modelo", "cor", "preto", "preta",
    "branco", "branca", "cinza", "azul", "vermelho", "verde", "prata", "dourado",
    "unidades", "unidade", "kit", "combo", "apenas", "somente", "partir", "hoje",
    "black", "friday", "esquenta", "relampago", "super", "mega", "top",
}

DEFAULT_HISTORICAL_CONFIG = {
    "enabled": False,
    "min_price": 500.0,
    "drop_percent": 15.0,
    "chats": [],
    "lookback_days": 60,
    "min_samples": 2,
}

MIN_SIMILARITY = 0.55
PRICE_BAND_LOW = 0.35
PRICE_BAND_HIGH = 3.0
MIN_HISTORICAL_AGE_HOURS = 24
ALERT_COOLDOWN_SECONDS = 6 * 3600


def strip_diacritics(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def normalize_for_matching(text: str) -> str:
    return re.sub(r"\s+", " ", strip_diacritics(text).lower()).strip()


def extract_cash_prices(raw_text: str) -> list[float]:
    """Extract R$ prices while ignoring installments (10x de R$) and discounts/coupons/shipping."""
    if not raw_text:
        return []
    prices: list[float] = []
    for match in PRICE_PATTERN.finditer(raw_text):
        start, end = match.span()
        before = normalize_for_matching(raw_text[max(0, start - 35):start])
        after = normalize_for_matching(raw_text[end:min(len(raw_text), end + 25)])
        if INSTALLMENT_BEFORE_RE.search(before):
            continue
        if DISCOUNT_BEFORE_RE.search(before):
            continue
        if DISCOUNT_AFTER_RE.search(after):
            continue
        int_part, dec_part = match.group(1), match.group(2)
        cleaned_int = int_part.replace(".", "")
        num_str = f"{cleaned_int}.{dec_part}" if dec_part else cleaned_int
        try:
            value = float(num_str)
        except ValueError:
            continue
        if value > 0:
            prices.append(value)
    return prices


def extract_primary_price(raw_text: str) -> float | None:
    prices = extract_cash_prices(raw_text)
    return min(prices) if prices else None


def is_model_token(token: str) -> bool:
    """Tokens containing both letters and digits (e.g. ps5, 27gn65r, 9800x3d, 1tb) are strong anchors."""
    return any(c.isalpha() for c in token) and any(c.isdigit() for c in token)


def extract_product_signature(raw_text: str) -> tuple[str, list[str]]:
    """Return (display_title, normalized_tokens) from the most descriptive product line."""
    if not raw_text:
        return "", []

    best_line = ""
    for line in raw_text.splitlines():
        cleaned = URL_PATTERN.sub("", line).strip()
        if not cleaned:
            continue
        norm = normalize_for_matching(cleaned)
        if not norm or SKIP_LINE_RE.match(norm):
            continue
        # Strip non-alphanumeric to check if line has actual words
        alnum_only = re.sub(r"[^a-z0-9\s]", " ", norm).strip()
        if len(alnum_only) < 3:
            continue
        best_line = cleaned
        break

    if not best_line:
        return "", []

    norm_line = normalize_for_matching(best_line)
    # Remove any inline price from the title line
    norm_line = PRICE_PATTERN.sub(" ", norm_line)
    raw_tokens = re.findall(r"[a-z0-9]+", norm_line)

    tokens: list[str] = []
    seen: set[str] = set()
    for tok in raw_tokens:
        if tok in STOPWORDS:
            continue
        if len(tok) == 1 and not tok.isdigit():
            continue
        if tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    display_title = best_line[:120].strip()
    return display_title, tokens


def compute_token_similarity(tokens_a: Iterable[str], tokens_b: Iterable[str]) -> float:
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    if not set_a or not set_b:
        return 0.0

    def weight(tok: str) -> float:
        if is_model_token(tok):
            return 2.5
        if tok.isdigit():
            return 1.5
        return 1.0

    intersection = set_a & set_b
    if not intersection:
        return 0.0

    inter_w = sum(weight(t) for t in intersection)
    union_w = sum(weight(t) for t in (set_a | set_b))
    # Also compute containment on the smaller set to handle slightly longer/shorter titles
    min_w = min(sum(weight(t) for t in set_a), sum(weight(t) for t in set_b))
    jaccard = inter_w / union_w if union_w else 0.0
    containment = inter_w / min_w if min_w else 0.0
    return 0.65 * jaccard + 0.35 * containment


@dataclass
class HistoricalDeal:
    chat_key: str
    chat_id: int
    msg_id: int
    msg_date: int
    title: str
    tokens: list[str]
    price: float
    msg_link: str | None
    similarity: float = 0.0


@dataclass
class HistoricalAlarmMatch:
    title: str
    current_price: float
    lowest_historical_price: float
    drop_percent: float
    sample_count: int
    lowest_deal: HistoricalDeal


class HistoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                chat_key TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                msg_id INTEGER NOT NULL,
                msg_date INTEGER NOT NULL,
                title TEXT NOT NULL,
                tokens TEXT NOT NULL,
                price REAL NOT NULL,
                msg_link TEXT,
                PRIMARY KEY (chat_id, msg_id)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(msg_date)"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def record_message(
        self,
        chat_key: str,
        chat_id: int,
        msg_id: int,
        msg_date: int,
        raw_text: str,
        msg_link: str | None,
    ) -> bool:
        price = extract_primary_price(raw_text)
        if price is None:
            return False
        title, tokens = extract_product_signature(raw_text)
        if len(tokens) < 2:
            return False
        self._conn.execute(
            """
            INSERT OR REPLACE INTO messages
                (chat_key, chat_id, msg_id, msg_date, title, tokens, price, msg_link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_key.lower(), chat_id, msg_id, msg_date, title, json.dumps(tokens), price, msg_link),
        )
        self._conn.commit()
        return True

    def get_latest_msg_id(self, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT MAX(msg_id) FROM messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def prune_old(self, lookback_days: int) -> int:
        cutoff = int(time.time()) - (lookback_days + 7) * 86400
        cur = self._conn.execute("DELETE FROM messages WHERE msg_date < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    def delete_chats(self, chats: list[str | int]) -> int:
        if not chats:
            return 0
        id_filters = [c for c in chats if isinstance(c, int)]
        str_filters = [c.lstrip("@").lower() for c in chats if isinstance(c, str)]
        clauses = []
        params: list = []
        if id_filters:
            clauses.append(f"chat_id IN ({','.join('?' for _ in id_filters)})")
            params.extend(id_filters)
        if str_filters:
            clauses.append(f"chat_key IN ({','.join('?' for _ in str_filters)})")
            params.extend(str_filters)
        if not clauses:
            return 0
        cur = self._conn.execute("DELETE FROM messages WHERE " + " OR ".join(clauses), params)
        self._conn.commit()
        return cur.rowcount

    def get_stats(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT chat_id), MIN(msg_date), MAX(msg_date) FROM messages"
        ).fetchone()
        return {
            "count": row[0] or 0,
            "chats": row[1] or 0,
            "oldest_ts": row[2],
            "newest_ts": row[3],
        }

    def find_similar_deals(
        self,
        tokens: list[str],
        current_price: float,
        allowed_chats: list[str | int],
        lookback_days: int,
        now_ts: int | None = None,
        min_age_hours: int = MIN_HISTORICAL_AGE_HOURS,
        exclude_chat_id: int | None = None,
        exclude_msg_id: int | None = None,
    ) -> list[HistoricalDeal]:
        if len(tokens) < 2:
            return []

        now = now_ts if now_ts is not None else int(time.time())
        min_ts = now - lookback_days * 86400
        max_ts = now - min_age_hours * 3600
        price_low = current_price * PRICE_BAND_LOW
        price_high = current_price * PRICE_BAND_HIGH

        query = (
            "SELECT chat_key, chat_id, msg_id, msg_date, title, tokens, price, msg_link "
            "FROM messages "
            "WHERE msg_date >= ? AND msg_date <= ? AND price >= ? AND price <= ?"
        )
        params: list = [min_ts, max_ts, price_low, price_high]

        if allowed_chats:
            id_filters = [c for c in allowed_chats if isinstance(c, int)]
            str_filters = [c.lstrip("@").lower() for c in allowed_chats if isinstance(c, str)]
            clauses = []
            if id_filters:
                placeholders = ",".join("?" for _ in id_filters)
                clauses.append(f"chat_id IN ({placeholders})")
                params.extend(id_filters)
            if str_filters:
                placeholders = ",".join("?" for _ in str_filters)
                clauses.append(f"chat_key IN ({placeholders})")
                params.extend(str_filters)
            if clauses:
                query += " AND (" + " OR ".join(clauses) + ")"

        rows = self._conn.execute(query, params).fetchall()

        matches: list[HistoricalDeal] = []
        for chat_key, chat_id, msg_id, msg_date, title, tokens_json, price, msg_link in rows:
            if exclude_chat_id is not None and exclude_msg_id is not None:
                if chat_id == exclude_chat_id and msg_id == exclude_msg_id:
                    continue
            try:
                hist_tokens = json.loads(tokens_json)
            except json.JSONDecodeError:
                continue
            sim = compute_token_similarity(tokens, hist_tokens)
            if sim >= MIN_SIMILARITY:
                matches.append(
                    HistoricalDeal(
                        chat_key=chat_key,
                        chat_id=chat_id,
                        msg_id=msg_id,
                        msg_date=msg_date,
                        title=title,
                        tokens=hist_tokens,
                        price=price,
                        msg_link=msg_link,
                        similarity=sim,
                    )
                )
        return matches


class HistoricalAlarmEngine:
    def __init__(self, store: HistoryStore) -> None:
        self.store = store
        self._recent_alerts: dict[frozenset[str], float] = {}

    def _is_on_cooldown(self, tokens: list[str], now_ts: float) -> bool:
        expired = [k for k, ts in self._recent_alerts.items() if now_ts - ts > ALERT_COOLDOWN_SECONDS]
        for k in expired:
            del self._recent_alerts[k]
        for prev_tokens in self._recent_alerts:
            if compute_token_similarity(tokens, prev_tokens) >= MIN_SIMILARITY:
                return True
        return False

    def evaluate(
        self,
        raw_text: str,
        config: dict,
        fallback_chats: list[str | int],
        chat_id: int | None = None,
        msg_id: int | None = None,
        now_ts: int | None = None,
    ) -> HistoricalAlarmMatch | None:
        if not config.get("enabled", False):
            return None

        current_price = extract_primary_price(raw_text)
        min_price = float(config.get("min_price", DEFAULT_HISTORICAL_CONFIG["min_price"]))
        if current_price is None or current_price < min_price:
            return None

        title, tokens = extract_product_signature(raw_text)
        if len(tokens) < 2:
            return None

        now = now_ts if now_ts is not None else int(time.time())
        if self._is_on_cooldown(tokens, float(now)):
            return None

        allowed_chats = config.get("chats") or fallback_chats
        lookback_days = int(config.get("lookback_days", DEFAULT_HISTORICAL_CONFIG["lookback_days"]))
        min_samples = int(config.get("min_samples", DEFAULT_HISTORICAL_CONFIG["min_samples"]))
        drop_percent = float(config.get("drop_percent", DEFAULT_HISTORICAL_CONFIG["drop_percent"]))

        similar = self.store.find_similar_deals(
            tokens=tokens,
            current_price=current_price,
            allowed_chats=allowed_chats,
            lookback_days=lookback_days,
            now_ts=now,
            exclude_chat_id=chat_id,
            exclude_msg_id=msg_id,
        )
        if len(similar) < min_samples:
            return None

        lowest = min(similar, key=lambda d: d.price)
        if lowest.price <= 0:
            return None

        actual_drop = (1.0 - current_price / lowest.price) * 100.0
        if actual_drop < drop_percent:
            return None

        self._recent_alerts[frozenset(tokens)] = float(now)
        return HistoricalAlarmMatch(
            title=title,
            current_price=current_price,
            lowest_historical_price=lowest.price,
            drop_percent=actual_drop,
            sample_count=len(similar),
            lowest_deal=lowest,
        )


def _build_msg_link(chat, chat_id: int, msg_id: int) -> str | None:
    username = getattr(chat, "username", None)
    if not username:
        usernames = getattr(chat, "usernames", None) or []
        username = next(
            (u.username for u in usernames if getattr(u, "active", True) and getattr(u, "username", None)),
            None,
        )
    if username:
        return f"https://t.me/{username}/{msg_id}"
    chat_id_str = str(chat_id or "")
    if chat_id_str.startswith("-100"):
        return f"https://t.me/c/{chat_id_str[4:]}/{msg_id}"
    return None


async def sync_history(
    client: TelegramClient,
    store: HistoryStore,
    chats: list[str | int],
    lookback_days: int = 60,
) -> tuple[int, int]:
    """Backfill messages from the last `lookback_days` into SQLite. Returns (indexed_count, chats_synced)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    indexed = 0
    chats_synced = 0

    for chat_ref in chats:
        try:
            entity = await client.get_entity(chat_ref)
        except Exception as exc:
            print(f"[historical_alarm] Could not resolve chat {chat_ref}: {exc}")
            continue

        chat_id = await client.get_peer_id(entity)
        username = getattr(entity, "username", None)
        chat_key = (username or str(chat_ref)).lstrip("@").lower()
        min_id = store.get_latest_msg_id(chat_id)
        chats_synced += 1

        try:
            async for msg in client.iter_messages(entity, min_id=min_id):
                if not msg.date:
                    continue
                msg_date = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
                if msg_date < cutoff:
                    break
                text = msg.raw_text or ""
                if not text:
                    continue
                link = _build_msg_link(entity, chat_id, msg.id)
                if store.record_message(
                    chat_key=chat_key,
                    chat_id=chat_id,
                    msg_id=msg.id,
                    msg_date=int(msg_date.timestamp()),
                    raw_text=text,
                    msg_link=link,
                ):
                    indexed += 1
        except FloodWaitError as fw:
            print(f"[historical_alarm] FloodWait ({fw.seconds}s) while syncing {chat_ref}; sleeping...")
            await asyncio.sleep(fw.seconds + 1)
        except Exception as exc:
            print(f"[historical_alarm] Error syncing {chat_ref}: {exc}")

        await asyncio.sleep(0.5)

    store.prune_old(lookback_days)
    return indexed, chats_synced

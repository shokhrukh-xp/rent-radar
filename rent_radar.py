#!/usr/bin/env python3
"""
Rent Radar — мульти-источниковый монитор аренды квартир в Ташкенте.

Источники: OLX.uz (API), Uybor.uz (API), Birbir.uz (HTML),
Telegram-каналы (публичные веб-превью t.me/s/..., без логина).

Находит новые объявления, отсеивает дубли (один и тот же вариант из разных
каналов/от разных авторов) и шлёт уникальные в ваш Telegram.

Запуск: python3 rent_radar.py
"""

import html as html_lib
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "radar.db"

TASHKENT_TZ = timezone(timedelta(hours=5))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ru,uz;q=0.9,en;q=0.8",
}

log = logging.getLogger("rent-radar")


# ---------------------------------------------------------------- config ----

DEFAULT_CONFIG = {
    "telegram_bot_token": "PUT_YOUR_BOT_TOKEN_HERE",
    "telegram_chat_id": "PUT_YOUR_CHAT_ID_HERE",
    "uzs_per_usd": 11900,
    "max_price_usd": 1000,
    "min_price_usd": 0,
    "notify_max_age_days": 3,
    "notify_duplicates": False,
    "hot_keywords": [
        "без посредник", "без маклер", "bez makler", "maklersiz", "makler yo'q",
        "хозяин", "от хозяина", "собственник", "egasidan", "uy egasi",
        "посредникам не беспокоить",
    ],
    "makler_user_threshold": 3,
    "dedup": {
        "phone_days": 14,
        "fuzzy_days": 10,
        "fuzzy_threshold": 0.80,
        "price_tolerance": 0.07,
    },
    "sources": {
        "olx": {
            "enabled": True, "interval_seconds": 60,
            "category_id": 1147, "city_id": 4, "owner_type": "private",
        },
        "uybor": {
            "enabled": True, "interval_seconds": 120,
            "region_id": 13, "category_id": 7,
        },
        "birbir": {
            "enabled": True, "interval_seconds": 300,
            "list_url": "https://birbir.uz/ru/tashkent/cat/nedvizhimost/arenda/kvartiry",
        },
        "telegram": {
            "enabled": True, "interval_seconds": 180,
            "channels": [
                "arentash", "arendtashkent", "arendatashkent_uz",
                "arendakvartir_uz", "arenda_kvartira_v_tashkente",
            ],
            "include_keywords": ["сда", "аренд", "ижара", "ijara", "arenda", "rent"],
            "exclude_keywords": ["сниму", "ищу", "ищем", "нужна квартира", "куплю", "kerak", "izlayapman"],
        },
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Создан {CONFIG_PATH}. Впишите telegram_bot_token и telegram_chat_id и перезапустите.")
        sys.exit(1)
    cfg = deep_merge(DEFAULT_CONFIG, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    # Переменные окружения важнее config.json (для GitHub Actions и т.п. —
    # токен хранится в секретах, а не в файле):
    if os.environ.get("RADAR_BOT_TOKEN"):
        cfg["telegram_bot_token"] = os.environ["RADAR_BOT_TOKEN"]
    if os.environ.get("RADAR_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["RADAR_CHAT_ID"]
    if "PUT_YOUR" in str(cfg["telegram_bot_token"]) or "PUT_YOUR" in str(cfg["telegram_chat_id"]):
        print(f"Заполните telegram_bot_token и telegram_chat_id в {CONFIG_PATH} (см. README) "
              "или передайте их через переменные окружения RADAR_BOT_TOKEN / RADAR_CHAT_ID.")
        sys.exit(1)
    return cfg


# ------------------------------------------------------------ extraction ----

PHONE_RE = re.compile(
    r"(?:\+?998[\s\-().]{0,3})?(\d{2})[\s\-().]{0,3}(\d{3})[\s\-().]{0,3}(\d{2})[\s\-().]{0,3}(\d{2})\b"
)
VALID_PHONE_PREFIXES = {
    "88", "90", "91", "93", "94", "95", "97", "98", "99", "33", "55", "77", "71", "78",
}

ROOMS_RE = [
    re.compile(r"\b([1-6])\s*[-–]?\s*(?:комн|ком\b|к\.|хона|xona|xonali)", re.I),
    re.compile(r"\b([1-6])\s*/\s*\d{1,2}\s*/\s*\d{1,2}\b"),
]

PRICE_USD_RE = re.compile(r"(\d[\d\s.,]{0,9}\d|\d)\s*(?:у\.?\s?е|\$|usd)", re.I)
PRICE_UZS_RE = re.compile(r"(\d[\d\s.,]{5,14}\d)\s*(?:сум|so['’`]?m|sum)", re.I)

DISTRICTS = {
    "Яккасарай": ["яккасарай", "yakkasaroy", "yakkasaray"],
    "Мирабад": ["мирабад", "mirobod", "mirabad"],
    "Юнусабад": ["юнусабад", "yunusobod", "yunusabad"],
    "Чиланзар": ["чиланзар", "chilonzor", "chilanzar"],
    "Мирзо-Улугбек": ["мирзо улугбек", "мирзо-улугбек", "mirzo ulug", "улугбек"],
    "Шайхантахур": ["шайхантахур", "shayxontohur", "шайхантаур"],
    "Алмазар": ["алмазар", "olmazor", "almazar"],
    "Учтепа": ["учтепа", "uchtepa"],
    "Яшнабад": ["яшнабад", "yashnobod", "yashnabad"],
    "Сергели": ["сергели", "sergeli"],
    "Бектемир": ["бектемир", "bektemir"],
    "Янгихаёт": ["янгихаёт", "янгихает", "yangihayot", "янгихаят"],
}


# districtId у Uybor → район. Выведено статистически по 600 объявлениям
# (id 202 намеренно пропущен: голоса разделились, лучше определить по тексту).
UYBOR_DISTRICT_IDS = {
    196: "Мирзо-Улугбек", 197: "Юнусабад", 198: "Шайхантахур",
    203: "Чиланзар", 204: "Мирабад", 205: "Яккасарай", 206: "Сергели",
    1332: "Янгихаёт", 671085: "Алмазар", 674731: "Яшнабад",
}


def extract_phones(text: str) -> list:
    phones = []
    for m in PHONE_RE.finditer(text or ""):
        digits = "".join(m.groups())
        if digits[:2] in VALID_PHONE_PREFIXES and digits not in phones:
            phones.append(digits)
    return phones


def as_int(v):
    """Аккуратно приводит к int: API источников иногда отдают числа строками."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def extract_rooms(text: str):
    for rx in ROOMS_RE:
        m = rx.search(text or "")
        if m:
            return int(m.group(1))
    return None


def _num(s: str):
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


def extract_price_from_text(text: str):
    """Возвращает (value, currency) или (None, None)."""
    m = PRICE_USD_RE.search(text or "")
    if m:
        v = _num(m.group(1))
        if v and 30 <= v <= 20000:
            return v, "USD"
    m = PRICE_UZS_RE.search(text or "")
    if m:
        v = _num(m.group(1))
        if v and v >= 300000:
            return v, "UZS"
    return None, None


def extract_district(text: str):
    low = (text or "").lower()
    for name, variants in DISTRICTS.items():
        if any(v in low for v in variants):
            return name
    return None


def canon_district(*candidates):
    """Приводит район к каноническому виду: «Яккасарайский район» → «Яккасарай».
    Источники пишут по-разному, а фильтр сравнивает по каноническому имени."""
    for c in candidates:
        if c:
            hit = extract_district(str(c))
            if hit:
                return hit
    return None


# OLX помечает доллары кодом UYE (у.е.), Uybor — usd; всё это одна валюта.
CURRENCY_ALIASES = {
    "USD": "USD", "UYE": "USD", "УЕ": "USD", "У.Е.": "USD", "У.Е": "USD",
    "$": "USD", "YE": "USD", "Y.E.": "USD", "CU": "USD",
    "UZS": "UZS", "СУМ": "UZS", "СУМ.": "UZS", "SUM": "UZS", "SO'M": "UZS",
    "SOM": "UZS", "SOʻM": "UZS", "SO`M": "UZS",
}


def canon_currency(c):
    """Возвращает 'USD' / 'UZS' / None (неизвестную валюту лучше не угадывать)."""
    if not c:
        return None
    return CURRENCY_ALIASES.get(str(c).strip().upper().replace(" ", ""))


def to_usd(value, currency, cfg):
    if value is None:
        return None
    if (currency or "").upper() == "USD":
        return float(value)
    if (currency or "").upper() == "UZS":
        return round(float(value) / cfg["uzs_per_usd"], 1)
    return None


def normalize_text(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"https?://\S+", " ", t)
    t = PHONE_RE.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:500]


def hot_flags(text: str, cfg) -> list:
    low = (text or "").lower()
    return [kw for kw in cfg["hot_keywords"] if kw.lower() in low]


def parse_iso(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_days(ts: str):
    dt = parse_iso(ts)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TASHKENT_TZ)
    return (datetime.now(dt.tzinfo) - dt).total_seconds() / 86400


# --------------------------------------------------------------- sources ----
# Каждый источник возвращает список унифицированных dict-объявлений.

def fetch_olx(scfg: dict, cfg: dict) -> list:
    params = {
        "limit": 40,
        "category_id": scfg["category_id"],
        "city_id": scfg["city_id"],
        "sort_by": "created_at:desc",
    }
    if scfg.get("owner_type"):
        params["owner_type"] = scfg["owner_type"]
    r = requests.get("https://www.olx.uz/api/v1/offers/", params=params,
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    out = []
    for o in r.json().get("data", []):
        price_value, price_currency = None, None
        for p in o.get("params", []):
            if p.get("key") == "price":
                v = p.get("value") or {}
                price_value, price_currency = v.get("value"), v.get("currency")
                if price_value is None and v.get("label"):
                    price_value = _num(v["label"])
                    price_currency = "USD" if ("у.е" in v["label"] or "$" in v["label"]) else "UZS"
        loc = o.get("location") or {}
        text = f'{o.get("title") or ""}\n{(o.get("description") or "")[:800]}'
        photo_urls = []
        for ph in (o.get("photos") or [])[:6]:
            link = (ph or {}).get("link") or ""
            if link:
                photo_urls.append(
                    link.replace("{width}x{height}", "1280x1024")
                        .replace("{width}", "1280").replace("{height}", "1024"))
        out.append({
            "photo_urls": photo_urls,
            "key": f'olx:{o.get("id")}',
            "source": "OLX",
            "url": o.get("url") or "",
            "title": o.get("title") or "Без названия",
            "text": text,
            "price_value": price_value,
            "price_currency": canon_currency(price_currency),
            "rooms": extract_rooms(text),
            "district": canon_district((loc.get("district") or {}).get("name"), text),
            "district_raw": (loc.get("district") or {}).get("name"),
            "phones": extract_phones(text),
            "created_at": o.get("created_time"),
            "seller": (o.get("user") or {}).get("name") or "",
            "seller_id": f'olx:{(o.get("user") or {}).get("id")}',
            "is_business": bool(o.get("business")),
        })
    return out


def fetch_uybor(scfg: dict, cfg: dict) -> list:
    params = {
        "limit": 30,
        "operationType__eq": "rent",
        "category__eq": scfg["category_id"],
        "region__eq": scfg["region_id"],
        "sort": "-createdAt",
    }
    r = requests.get("https://api.uybor.uz/api/v1/listings", params=params,
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    out = []
    for o in r.json().get("results", []):
        desc = o.get("description") or ""
        price_value, price_currency = o.get("price"), canon_currency(o.get("priceCurrency"))
        rooms = as_int(o.get("room")) or extract_rooms(desc)
        price_value = as_int(price_value) if price_value is not None else None
        text = desc[:900]
        title = desc.strip().split("\n")[0][:80] or "Объявление Uybor"
        photo_urls = []
        for m_item in (o.get("media") or [])[:6]:
            u = None
            if isinstance(m_item, str):
                u = m_item
            elif isinstance(m_item, dict):
                for k in ("url", "link", "file", "path", "name", "filename"):
                    v = m_item.get(k)
                    if isinstance(v, str) and v:
                        u = v
                        break
            if not u:
                continue
            if not u.startswith("http"):
                u = f"https://api.uybor.uz/api/v1/media/n/{u.lstrip('/')}"
            photo_urls.append(u)
        out.append({
            "photo_urls": photo_urls,
            "key": f'uybor:{o.get("id")}',
            "source": "Uybor",
            "url": f'https://uybor.uz/listings/{o.get("id")}',
            "title": title,
            "text": text,
            "price_value": price_value,
            "price_currency": price_currency,
            "rooms": rooms,
            "district": (UYBOR_DISTRICT_IDS.get(o.get("districtId"))
                         or canon_district(text, o.get("address"))),
            "district_raw": o.get("address") or None,
            "phones": extract_phones(text),
            "created_at": o.get("createdAt"),
            "seller": "",
            "seller_id": f'uybor:{o.get("userId")}',
            "is_business": None,
        })
    return out


BIRBIR_LINK_RE = re.compile(r'href="((?:https://birbir\.uz)?/ru/[^"]*?/o/[^"]+-(\d{6,}))"')


def fetch_birbir(scfg: dict, cfg: dict) -> list:
    r = requests.get(scfg["list_url"], headers=HEADERS, timeout=25)
    r.raise_for_status()
    page = r.text
    out, seen_ids = [], set()
    for m in BIRBIR_LINK_RE.finditer(page):
        href, bid = m.group(1), m.group(2)
        if bid in seen_ids:
            continue
        seen_ids.add(bid)
        url = href if href.startswith("http") else f"https://birbir.uz{href}"
        # заголовок и цена — из ближайшего окружения ссылки
        chunk = page[m.start(): m.start() + 2500]
        chunk_txt = html_lib.unescape(re.sub(r"<[^>]+>", " ", chunk))
        chunk_txt = re.sub(r"\s+", " ", chunk_txt).strip()
        title_m = re.search(r"[А-ЯЁA-Z][^|]{10,90}", chunk_txt)
        title = (title_m.group(0).strip() if title_m else f"Birbir #{bid}")[:90]
        price_value, price_currency = extract_price_from_text(chunk_txt)
        out.append({
            "photo_urls": [],
            "key": f"birbir:{bid}",
            "source": "Birbir",
            "url": url,
            "title": title,
            "text": chunk_txt[:600],
            "price_value": price_value,
            "price_currency": price_currency,
            "rooms": extract_rooms(chunk_txt),
            "district": canon_district(chunk_txt),
            "district_raw": None,
            "phones": extract_phones(chunk_txt),
            "created_at": None,
            "seller": "",
            "seller_id": "",
            "is_business": None,
        })
    return out


TG_POST_RE = re.compile(r'data-post="([^"]+/(\d+))"')
TG_TEXT_RE = re.compile(
    r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S
)
TG_TIME_RE = re.compile(r'datetime="([^"]+)"')
TG_PHOTO_RE = re.compile(r"background-image:url\('([^']+)'\)")


def _strip_tags(fragment: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", fragment)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html_lib.unescape(t)
    return re.sub(r"[ \t]+", " ", t).strip()


def fetch_telegram(scfg: dict, cfg: dict) -> list:
    out = []
    inc = [k.lower() for k in scfg.get("include_keywords", [])]
    exc = [k.lower() for k in scfg.get("exclude_keywords", [])]
    for channel in scfg.get("channels", []):
        try:
            r = requests.get(f"https://t.me/s/{channel}", headers=HEADERS, timeout=20)
            if r.status_code != 200:
                log.warning("t.me/s/%s → HTTP %s, пропускаю", channel, r.status_code)
                continue
            page = r.text
        except requests.RequestException as e:
            log.warning("t.me/s/%s недоступен: %s", channel, e)
            continue

        posts = list(TG_POST_RE.finditer(page))
        for i, m in enumerate(posts):
            msg_id = m.group(2)
            start = m.start()
            end = posts[i + 1].start() if i + 1 < len(posts) else len(page)
            block = page[start:end]

            tm = TG_TEXT_RE.search(block)
            if not tm:
                continue
            text = _strip_tags(tm.group(1))
            low = text.lower()
            if inc and not any(k in low for k in inc):
                continue
            if any(k in low for k in exc):
                continue

            time_m = TG_TIME_RE.search(block)
            created = time_m.group(1) if time_m else None
            price_value, price_currency = extract_price_from_text(text)
            title = text.split("\n")[0][:80] or f"@{channel} #{msg_id}"
            photo_urls = [u for u in TG_PHOTO_RE.findall(block)
                          if "cdn" in u or "telegram" in u][:6]
            out.append({
                "photo_urls": photo_urls,
                "key": f"tg:{channel}:{msg_id}",
                "source": f"TG @{channel}",
                "url": f"https://t.me/{channel}/{msg_id}",
                "title": title,
                "text": text[:900],
                "price_value": price_value,
                "price_currency": price_currency,
                "rooms": extract_rooms(text),
                "district": canon_district(text),
                "district_raw": None,
                "phones": extract_phones(text),
                "created_at": created,
                "seller": "",
                "seller_id": f"tg:{channel}",
                "is_business": None,
            })
        time.sleep(1)
    return out


SOURCE_FETCHERS = {
    "olx": fetch_olx,
    "uybor": fetch_uybor,
    "birbir": fetch_birbir,
    "telegram": fetch_telegram,
}


# ----------------------------------------------------------------- store ----

class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS listings(
            key TEXT PRIMARY KEY, source TEXT, url TEXT, title TEXT,
            norm_text TEXT, price_usd REAL, rooms INTEGER, district TEXT,
            first_seen TEXT, notified INTEGER DEFAULT 0, dup_of TEXT)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS phones(
            phone TEXT, key TEXT, first_seen TEXT)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS seller_counts(
            seller_id TEXT PRIMARY KEY, cnt INTEGER)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS kv(
            key TEXT PRIMARY KEY, value TEXT)""")
        self.conn.commit()

    def get_kv(self, key, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_kv(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)",
                          (key, json.dumps(value, ensure_ascii=False)))
        self.conn.commit()

    def known(self, key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM listings WHERE key=?", (key,)).fetchone() is not None

    def bump_seller(self, seller_id: str) -> int:
        if not seller_id:
            return 0
        self.conn.execute(
            "INSERT INTO seller_counts(seller_id, cnt) VALUES(?,1) "
            "ON CONFLICT(seller_id) DO UPDATE SET cnt=cnt+1", (seller_id,))
        return self.conn.execute(
            "SELECT cnt FROM seller_counts WHERE seller_id=?", (seller_id,)).fetchone()[0]

    def find_dup(self, listing: dict, cfg: dict):
        """Возвращает key оригинала или None."""
        d = cfg["dedup"]
        now = datetime.now(timezone.utc)

        # 1) совпадение телефона
        if listing["phones"]:
            since = (now - timedelta(days=d["phone_days"])).isoformat()
            qmarks = ",".join("?" * len(listing["phones"]))
            row = self.conn.execute(
                f"SELECT key FROM phones WHERE phone IN ({qmarks}) AND first_seen > ?",
                (*listing["phones"], since)).fetchone()
            if row:
                return row[0]

        # 2) нечёткое совпадение текста (+ близкая цена, те же комнаты)
        norm = normalize_text(listing["text"])
        if len(norm) < 40:
            return None
        since = (now - timedelta(days=d["fuzzy_days"])).isoformat()
        p_usd = listing.get("price_usd")
        for key, other_norm, other_price, other_rooms in self.conn.execute(
                "SELECT key, norm_text, price_usd, rooms FROM listings "
                "WHERE first_seen > ? AND norm_text != ''", (since,)):
            if listing["rooms"] and other_rooms and listing["rooms"] != other_rooms:
                continue
            if p_usd and other_price:
                if abs(p_usd - other_price) / max(p_usd, other_price) > d["price_tolerance"]:
                    continue
            if SequenceMatcher(None, norm, other_norm).ratio() >= d["fuzzy_threshold"]:
                return key
        return None

    def save(self, listing: dict, notified: bool, dup_of=None):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO listings(key, source, url, title, norm_text, "
            "price_usd, rooms, district, first_seen, notified, dup_of) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (listing["key"], listing["source"], listing["url"], listing["title"],
             normalize_text(listing["text"]), listing.get("price_usd"),
             listing.get("rooms"), listing.get("district"), now,
             int(notified), dup_of))
        for ph in listing["phones"]:
            self.conn.execute(
                "INSERT INTO phones(phone, key, first_seen) VALUES(?,?,?)",
                (ph, listing["key"], now))
        self.conn.commit()

    def counts(self):
        total = self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        dups = self.conn.execute(
            "SELECT COUNT(*) FROM listings WHERE dup_of IS NOT NULL").fetchone()[0]
        return total, dups

    def prune(self, listing_days=60, phone_days=30):
        """Чистка старых записей, чтобы база не разрасталась."""
        now = datetime.now(timezone.utc)
        self.conn.execute("DELETE FROM listings WHERE first_seen < ?",
                          ((now - timedelta(days=listing_days)).isoformat(),))
        self.conn.execute("DELETE FROM phones WHERE first_seen < ?",
                          ((now - timedelta(days=phone_days)).isoformat(),))
        self.conn.commit()


# -------------------------------------------------------------- telegram ----

def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_phone(p: str) -> str:
    return f"+998 {p[:2]} {p[2:5]}-{p[5:7]}-{p[7:9]}"


def format_message(l: dict, cfg: dict, likely_makler: bool) -> str:
    lines = [f'🏠 <b>[{escape_html(l["source"])}]</b> {escape_html(l["title"])}']
    val, cur = l.get("price_value"), l.get("price_currency")
    if val:
        num = f"{val:,}".replace(",", " ")
        if cur == "USD":
            lines.append(f"💰 ${num}")
        elif cur == "UZS":
            usd = l.get("price_usd") or to_usd(val, "UZS", cfg)
            lines.append(f"💰 {num} сум" + (f" (~${usd:.0f})" if usd else ""))
        else:
            lines.append(f"💰 {num} (валюта не указана)")
    details = []
    if l.get("rooms"):
        details.append(f'🛏 {l["rooms"]}-комн')
    place = l.get("district") or l.get("district_raw")
    details.append(f'📍 {escape_html(str(place))}' if place else "📍 район не указан")
    if details:
        lines.append(" · ".join(details))
    if l.get("seller") or l.get("is_business") is not None:
        seller_type = ("Бизнес-аккаунт" if l.get("is_business")
                       else "Частное лицо" if l.get("is_business") is False else "")
        s = " · ".join(x for x in [escape_html(l.get("seller", "")), seller_type] if x)
        if s:
            lines.append(f"👤 {s}")
    if l["phones"]:
        lines.append("📞 " + ", ".join(fmt_phone(p) for p in l["phones"][:2]))
    flags = hot_flags(l["text"], cfg)
    if flags:
        lines.append("🔥 Похоже, от хозяина: " + ", ".join(f"«{f}»" for f in flags[:3]))
    if likely_makler:
        lines.append("⚠️ У продавца много объявлений — возможно, маклер")
    dt = parse_iso(l.get("created_at") or "")
    if dt:
        lines.append(f'🕐 {dt.astimezone(TASHKENT_TZ).strftime("%d.%m %H:%M")}')
    lines.append(f'\n<a href="{l["url"]}">Открыть объявление</a>  ⚡ Звоните сразу!')
    return "\n".join(lines)


def tg_call(cfg, method: str, payload: dict, timeout: int = 20, quiet: bool = False):
    api = f'https://api.telegram.org/bot{cfg["telegram_bot_token"]}/{method}'
    try:
        r = requests.post(api, data=payload, timeout=timeout)
        if r.status_code != 200:
            if not quiet:
                log.error("Telegram %s %s: %s", method, r.status_code, r.text[:200])
            return None
        return r.json()
    except requests.RequestException as e:
        log.error("Telegram недоступен: %s", e)
        return None


def send_telegram(cfg, text: str) -> bool:
    return tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "text": text,
        "parse_mode": "HTML",
    }) is not None


def send_photo_upload(cfg, photo_url: str, caption: str) -> bool:
    """Скачивает фото и загружает напрямую (для CDN-ссылок, которые
    Telegram отказывается пересылать по URL — WEBPAGE_MEDIA_EMPTY)."""
    try:
        img = requests.get(photo_url, headers=HEADERS, timeout=20)
        if img.status_code != 200 or len(img.content) < 1000:
            return False
        api = f'https://api.telegram.org/bot{cfg["telegram_bot_token"]}/sendPhoto'
        r = requests.post(
            api,
            data={"chat_id": cfg["telegram_chat_id"],
                  "caption": caption[:1000], "parse_mode": "HTML"},
            files={"photo": ("photo.jpg", img.content)},
            timeout=30)
        return r.status_code == 200
    except requests.RequestException as e:
        log.warning("Загрузка фото не удалась: %s", e)
        return False


def send_listing(cfg, settings: dict, l: dict, likely_makler: bool) -> bool:
    """Уведомление об объявлении: альбом с фото, если они есть и включены."""
    text = format_message(l, cfg, likely_makler)
    photos = (l.get("photo_urls") or []) if settings.get("photos", True) else []
    if photos:
        media = [{"type": "photo", "media": u} for u in photos[:4]]
        media[0]["caption"] = text[:1000]
        media[0]["parse_mode"] = "HTML"
        if tg_call(cfg, "sendMediaGroup", {
            "chat_id": cfg["telegram_chat_id"],
            "media": json.dumps(media),
        }) is not None:
            return True
        if send_photo_upload(cfg, photos[0], text):
            return True
        log.info("Фото не отправились, шлю текстом: %s", l["title"][:50])
    return send_telegram(cfg, text)


# ------------------------------------------------- настройки через бота ----

HELP_TEXT = """🤖 <b>Rent Radar</b>

Проще всего — <b>/menu</b>: там всё выбирается кнопками.

Команды (можно и без аргумента — покажу кнопки):
/menu — меню
/status — текущие фильтры и статистика
/max — максимальная цена
/min — минимальная цена
/rooms — комнатность
/district — районы
/photos — фото вкл/выкл
/pause — пауза, /resume — продолжить"""

DISTRICT_LIST = sorted(DISTRICTS)
PRICE_PRESETS = [300, 400, 500, 700, 1000, 1500]
MIN_PRESETS = [0, 200, 300, 400, 500]
ROOM_PRESETS = [("1", "1"), ("2", "2"), ("2–3", "2-3"), ("3+", "3-6"), ("любые", "*")]

ON_WORDS = {"on", "вкл", "да", "yes", "1"}
OFF_WORDS = {"off", "выкл", "нет", "no", "0"}
RESET_WORDS = {"все", "всё", "любые", "любая", "сброс", "all", "any", "reset"}


def default_settings() -> dict:
    return {"photos": True, "paused": False, "districts": [],
            "strict_district": False,
            "rooms_min": None, "rooms_max": None,
            "max_price_usd": None, "min_price_usd": None}


def effective_cfg(cfg: dict, settings: dict) -> dict:
    eff = dict(cfg)
    if settings.get("max_price_usd") is not None:
        eff["max_price_usd"] = settings["max_price_usd"]
    if settings.get("min_price_usd") is not None:
        eff["min_price_usd"] = settings["min_price_usd"]
    return eff


# --------------------------------------------------------- описание фильтров ---

def rooms_label(settings: dict) -> str:
    rmin, rmax = settings.get("rooms_min"), settings.get("rooms_max")
    if rmin is None:
        return "любые"
    return f"{rmin}" if rmin == rmax else f"{rmin}–{rmax}"


def districts_label(settings: dict) -> str:
    ds = settings.get("districts") or []
    if not ds:
        return "все"
    return ", ".join(ds) if len(ds) <= 2 else f"{len(ds)} выбрано"


def price_label(cfg: dict, settings: dict) -> str:
    eff = effective_cfg(cfg, settings)
    lo = eff.get("min_price_usd") or 0
    return f"до ${eff['max_price_usd']}" if not lo else f"${lo}–{eff['max_price_usd']}"


def _btn(text, data):
    return {"text": text, "callback_data": data}


# ------------------------------------------------------------- экраны меню ---

def kb_menu(cfg: dict, settings: dict) -> dict:
    return {"inline_keyboard": [
        [_btn(f"💰 Цена: {price_label(cfg, settings)}", "v:P")],
        [_btn(f"🛏 Комнаты: {rooms_label(settings)}", "v:R"),
         _btn(f"📍 Районы: {districts_label(settings)}", "v:D")],
        [_btn(f"🖼 Фото: {'вкл' if settings.get('photos', True) else 'выкл'}", "p"),
         _btn("▶️ Продолжить" if settings.get("paused") else "⏸ Пауза", "z")],
        [_btn("📊 Статус", "s"), _btn("❓ Помощь", "h")],
    ]}


def kb_price(cfg: dict, settings: dict) -> dict:
    cur = effective_cfg(cfg, settings)["max_price_usd"]
    rows, row = [], []
    for p in PRICE_PRESETS:
        row.append(_btn(("✅ " if cur == p else "") + f"${p}", f"m:{p}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_btn(f"Мин. цена: ${settings.get('min_price_usd') or 0} ▸", "v:N")])
    rows.append([_btn("← Меню", "v:M")])
    return {"inline_keyboard": rows}


def kb_min(cfg: dict, settings: dict) -> dict:
    cur = settings.get("min_price_usd") or 0
    return {"inline_keyboard": [
        [_btn(("✅ " if cur == p else "") + f"${p}", f"n:{p}") for p in MIN_PRESETS],
        [_btn("← Меню", "v:M")],
    ]}


def kb_rooms(cfg: dict, settings: dict) -> dict:
    rmin, rmax = settings.get("rooms_min"), settings.get("rooms_max")
    row = []
    for label, val in ROOM_PRESETS:
        if val == "*":
            active = rmin is None
        else:
            a, _, b = val.partition("-")
            active = (rmin, rmax) == (int(a), int(b) if b else int(a))
        row.append(_btn(("✅ " if active else "") + label, f"r:{val}"))
    return {"inline_keyboard": [row, [_btn("← Меню", "v:M")]]}


def kb_districts(cfg: dict, settings: dict) -> dict:
    ds = settings.get("districts") or []
    rows = []
    for i in range(0, len(DISTRICT_LIST), 2):
        rows.append([_btn(("✅ " if n in ds else "▫️ ") + n, f"d:{DISTRICT_LIST.index(n)}")
                     for n in DISTRICT_LIST[i:i + 2]])
    rows.append([_btn(("✅ " if not ds else "") + "Весь Ташкент", "da")])
    if ds:
        rows.append([_btn("🔒 Строго: только выбранные" if settings.get("strict_district")
                          else "🔓 Плюс объявления без района", "ds")])
    rows.append([_btn("← Меню", "v:M")])
    return {"inline_keyboard": rows}


VIEWS = {
    "M": (lambda cfg, s: "⚙️ <b>Меню Rent Radar</b>\nНажимайте кнопки — фильтры применяются сразу.", kb_menu),
    "P": (lambda cfg, s: f"💰 <b>Максимальная цена</b>\nСейчас: {price_label(cfg, s)}", kb_price),
    "N": (lambda cfg, s: f"💰 <b>Минимальная цена</b>\nСейчас: ${s.get('min_price_usd') or 0}", kb_min),
    "R": (lambda cfg, s: f"🛏 <b>Комнатность</b>\nСейчас: {rooms_label(s)}", kb_rooms),
    "D": (lambda cfg, s: ("📍 <b>Районы</b>\nНажмите, чтобы включить или убрать. "
                          f"Сейчас: {districts_label(s)}\n\n"
                          + ("<i>Строгий режим: объявления без указанного района "
                             "не присылаю.</i>" if s.get("strict_district")
                             else "<i>Объявления, где район не указан, присылаю тоже — "
                                  "чтобы не упустить вариант от хозяина. "
                                  "Переключается кнопкой ниже.</i>")), kb_districts),
}


def render_view(cfg, settings, view: str, message_id=None) -> bool:
    text_fn, kb_fn = VIEWS.get(view, VIEWS["M"])
    payload = {
        "chat_id": cfg["telegram_chat_id"],
        "text": text_fn(cfg, settings),
        "parse_mode": "HTML",
        "reply_markup": json.dumps(kb_fn(cfg, settings), ensure_ascii=False),
    }
    if message_id:
        payload["message_id"] = message_id
        if tg_call(cfg, "editMessageText", payload) is not None:
            return True
        payload.pop("message_id", None)  # сообщение не редактируется — шлём новое
    return tg_call(cfg, "sendMessage", payload) is not None


def status_text(cfg: dict, settings: dict, store) -> str:
    total, dups = store.counts()
    return (f"📊 <b>Статус Rent Radar</b>\n"
            f"💰 Цена: {price_label(cfg, settings)}\n"
            f"🛏 Комнаты: {rooms_label(settings)}\n"
            f"📍 Районы: {districts_label(settings)}\n"
            f"🖼 Фото: {'вкл' if settings.get('photos', True) else 'выкл'}\n"
            f"▶️ Уведомления: {'на паузе ⏸' if settings.get('paused') else 'работают'}\n"
            f"🗂 В базе: {total} объявлений (дублей отсеяно: {dups})")


# ------------------------------------------------------- команды и кнопки ---

def handle_command(text: str, settings: dict, store, cfg: dict):
    """Возвращает (ответ, view_или_None). view — какой экран показать кнопками."""
    t = (text or "").strip()
    low = t.lower()
    cmd, _, arg = low.partition(" ")
    cmd = cmd.split("@")[0]
    arg = arg.strip()
    raw_arg = t.partition(" ")[2].strip()

    if cmd in ("/start", "/help"):
        return HELP_TEXT, None
    if cmd == "/menu":
        return "", "M"
    if cmd == "/status":
        return status_text(cfg, settings, store), None

    if cmd in ("/max", "/min"):
        n = re.sub(r"[^\d]", "", arg)
        if not n:
            return "", ("P" if cmd == "/max" else "N")
        settings["max_price_usd" if cmd == "/max" else "min_price_usd"] = int(n)
        return f"✅ {'Макс' if cmd == '/max' else 'Мин'}. цена: ${n}", None

    if cmd == "/rooms":
        if not arg:
            return "", "R"
        if arg in RESET_WORDS:
            settings["rooms_min"] = settings["rooms_max"] = None
            return "✅ Фильтр комнат снят", None
        m = re.match(r"^(\d)\s*[-–]\s*(\d)$", arg) or re.match(r"^(\d)$", arg)
        if not m:
            return "Формат: /rooms 2 или /rooms 2-3", "R"
        a = int(m.group(1))
        b = int(m.group(2)) if m.lastindex and m.lastindex > 1 else a
        settings["rooms_min"], settings["rooms_max"] = min(a, b), max(a, b)
        return (f"✅ Комнаты: {min(a, b)}–{max(a, b)}" if a != b else f"✅ Комнаты: {a}"), None

    if cmd in ("/district", "/districts"):
        if not arg:
            return "", "D"
        if arg in RESET_WORDS:
            settings["districts"] = []
            return "✅ Слежу за всем Ташкентом", None
        chosen, unknown = [], []
        for part in re.split(r"[,;]+", raw_arg):
            p = part.strip().lower()
            if not p:
                continue
            hit = next((name for name, vs in DISTRICTS.items()
                        if p in [v.lower() for v in vs] + [name.lower()]
                        or any(v in p for v in vs)), None)
            (chosen if hit else unknown).append(hit or part.strip())
        if not chosen:
            return "Не узнал: " + ", ".join(unknown) + ". Выберите кнопками:", "D"
        settings["districts"] = sorted(set(chosen))
        reply = "✅ Районы: " + ", ".join(settings["districts"])
        if unknown:
            reply += "\n⚠️ Не узнал: " + ", ".join(unknown)
        return reply, None

    if cmd == "/photos":
        if arg in OFF_WORDS:
            settings["photos"] = False
        elif arg in ON_WORDS:
            settings["photos"] = True
        else:
            settings["photos"] = not settings.get("photos", True)
        return ("✅ Фото включены" if settings["photos"] else "✅ Фото выключены"), None

    if cmd == "/pause":
        settings["paused"] = True
        return "⏸ Уведомления на паузе. Вернуть — /resume", None
    if cmd == "/resume":
        settings["paused"] = False
        return "▶️ Уведомления снова работают", None

    if t.startswith("/"):
        return "Не знаю такую команду.", "M"
    return "", None


def handle_callback(data: str, settings: dict, store, cfg: dict):
    """Возвращает (всплывающая_подсказка, view_для_перерисовки)."""
    act, _, val = (data or "").partition(":")

    if act == "v":
        return "", (val if val in VIEWS else "M")
    if act == "m" and val.isdigit():
        settings["max_price_usd"] = int(val)
        return f"Макс. цена: ${val}", "P"
    if act == "n" and val.isdigit():
        settings["min_price_usd"] = int(val)
        return f"Мин. цена: ${val}", "N"
    if act == "r":
        if val == "*":
            settings["rooms_min"] = settings["rooms_max"] = None
            return "Комнаты: любые", "R"
        a, _, b = val.partition("-")
        if a.isdigit():
            settings["rooms_min"] = int(a)
            settings["rooms_max"] = int(b) if b.isdigit() else int(a)
            return f"Комнаты: {rooms_label(settings)}", "R"
        return "", "R"
    if act == "d" and val.isdigit() and int(val) < len(DISTRICT_LIST):
        name = DISTRICT_LIST[int(val)]
        ds = set(settings.get("districts") or [])
        if name in ds:
            ds.discard(name)
            toast = f"{name} убран"
        else:
            ds.add(name)
            toast = f"{name} добавлен"
        settings["districts"] = sorted(ds)
        return toast, "D"
    if act == "da":
        settings["districts"] = []
        return "Слежу за всем Ташкентом", "D"
    if act == "ds":
        settings["strict_district"] = not settings.get("strict_district")
        return ("Только выбранные районы" if settings["strict_district"]
                else "Плюс объявления без указанного района"), "D"
    if act == "p":
        settings["photos"] = not settings.get("photos", True)
        return ("Фото включены" if settings["photos"] else "Фото выключены"), "M"
    if act == "z":
        settings["paused"] = not settings.get("paused")
        return ("Пауза" if settings["paused"] else "Продолжаю"), "M"
    if act == "s":
        send_telegram(cfg, status_text(cfg, settings, store))
        return "Статус отправлен", None
    if act == "h":
        send_telegram(cfg, HELP_TEXT)
        return "Справка отправлена", None
    return "", None


def process_commands(cfg: dict, store, long_poll: int = 0) -> dict:
    """Читает новые сообщения/нажатия, применяет их, отвечает."""
    settings = {**default_settings(), **(store.get_kv("settings") or {})}
    offset = store.get_kv("tg_offset", 0)
    resp = tg_call(cfg, "getUpdates",
                   {"offset": offset + 1, "timeout": long_poll},
                   timeout=long_poll + 20)
    if not resp:
        return settings
    changed = False
    for upd in resp.get("result", []):
        offset = max(offset, upd.get("update_id", 0))

        cb = upd.get("callback_query")
        if cb:
            msg = cb.get("message") or {}
            if str((msg.get("chat") or {}).get("id") or "") != str(cfg["telegram_chat_id"]):
                continue
            toast, view = handle_callback(cb.get("data") or "", settings, store, cfg)
            # подсказка-«всплывашка»; для старых нажатий Telegram её отклоняет — это нормально
            tg_call(cfg, "answerCallbackQuery",
                    {"callback_query_id": cb.get("id"), "text": toast}, quiet=True)
            if view:
                render_view(cfg, settings, view, msg.get("message_id"))
            changed = True
            log.info("Кнопка: %s → %s", (cb.get("data") or "")[:30], toast[:40])
            continue

        msg = upd.get("message") or upd.get("edited_message") or {}
        if str((msg.get("chat") or {}).get("id") or "") != str(cfg["telegram_chat_id"]):
            continue  # игнорируем чужих
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        reply, view = handle_command(text, settings, store, cfg)
        if reply:
            send_telegram(cfg, reply)
        if view:
            render_view(cfg, settings, view)
        if reply or view:
            changed = True
            log.info("Команда: %s", text[:50])
    store.set_kv("tg_offset", offset)
    if changed:
        store.set_kv("settings", settings)
    return settings


def passes_filters(l: dict, cfg: dict) -> bool:
    l["price_usd"] = to_usd(l.get("price_value"), l.get("price_currency"), cfg)
    p = l["price_usd"]
    if p is not None:
        if cfg["max_price_usd"] and p > cfg["max_price_usd"]:
            return False
        if cfg["min_price_usd"] and p < cfg["min_price_usd"]:
            return False
    a = age_days(l.get("created_at") or "")
    if a is not None and cfg["notify_max_age_days"] and a > cfg["notify_max_age_days"]:
        return False
    return True


def passes_user_filters(l: dict, settings: dict) -> bool:
    """Фильтры, заданные командами бота. Неизвестные комнаты/район — пропускаем."""
    rmin, rmax = as_int(settings.get("rooms_min")), as_int(settings.get("rooms_max"))
    rooms = as_int(l.get("rooms"))
    if rmin is not None and rooms is not None:
        if not rmin <= rooms <= (rmax if rmax is not None else rmin):
            return False
    if settings.get("districts"):
        d = l.get("district")
        if d is None:
            # район не распознан: по умолчанию присылаем (чтобы не потерять
            # объявление от хозяина), в строгом режиме — отсекаем
            if settings.get("strict_district"):
                return False
        elif d not in settings["districts"]:
            return False
    return True


def run():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    once = "--once" in sys.argv  # один проход по всем источникам и выход
    minutes = 0.0                # --minutes N: работать N минут и выйти
    if "--minutes" in sys.argv:
        try:
            minutes = float(sys.argv[sys.argv.index("--minutes") + 1])
        except (IndexError, ValueError):
            minutes = 0.0
    deadline = time.time() + minutes * 60 if minutes else None
    cfg = load_config()
    store = Store(DB_PATH)
    store.prune()
    first_run = store.counts()[0] == 0

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    enabled = {name: s for name, s in cfg["sources"].items() if s.get("enabled")}
    next_run = {name: 0.0 for name in enabled}
    mode = " (разовый проход)" if once else (f" на {minutes:.0f} мин" if minutes else "")
    log.info("Rent Radar запущен%s. Источники: %s. Лимит: $%s",
             mode, ", ".join(enabled) or "нет", cfg["max_price_usd"])
    if first_run:
        log.info("Первый запуск: текущие объявления запоминаю без уведомлений")

    while not stop["flag"]:
        now = time.time()
        # long-poll: команды и нажатия кнопок ловим за ~секунду, а не раз в проход
        settings = process_commands(cfg, store, long_poll=0 if once else 20)
        eff = effective_cfg(cfg, settings)
        for name, scfg in enabled.items():
            if not once and now < next_run[name]:
                continue
            next_run[name] = now + scfg.get("interval_seconds", 120)
            try:
                listings = SOURCE_FETCHERS[name](scfg, cfg)
            except Exception as e:
                log.warning("[%s] ошибка получения: %s", name, e)
                next_run[name] = now + min(900, scfg.get("interval_seconds", 120) * 3)
                continue

            fresh = 0
            for l in listings:
              # одно битое объявление не должно ронять весь радар
              try:
                if store.known(l["key"]):
                    continue
                seller_cnt = store.bump_seller(l.get("seller_id", ""))

                if first_run:
                    store.save(l, notified=False)
                    continue

                if not passes_filters(l, eff) or not passes_user_filters(l, settings):
                    store.save(l, notified=False)
                    continue

                dup_of = store.find_dup(l, cfg)
                if dup_of:
                    store.save(l, notified=False, dup_of=dup_of)
                    log.info("[%s] дубль (%s ← %s): %s",
                             name, dup_of, l["key"], l["title"][:50])
                    if cfg["notify_duplicates"]:
                        send_telegram(cfg, f'🔁 Дубль в {escape_html(l["source"])}: '
                                           f'<a href="{l["url"]}">{escape_html(l["title"][:60])}</a>')
                    continue

                if settings.get("paused"):
                    store.save(l, notified=False)
                    continue

                likely_makler = seller_cnt >= cfg["makler_user_threshold"]
                ok = send_listing(cfg, settings, l, likely_makler)
                store.save(l, notified=ok)
                if ok:
                    fresh += 1
                    log.info("[%s] уведомление: %s", name, l["title"][:60])
                time.sleep(1)
              except Exception as e:
                log.warning("[%s] объявление %s пропущено из-за ошибки: %s",
                            name, l.get("key"), e)
                try:
                    store.save(l, notified=False)   # чтобы не спотыкаться о него снова
                except Exception:
                    pass

            if fresh:
                log.info("[%s] новых: %d", name, fresh)

        if first_run:
            total, _ = store.counts()
            log.info("Запомнил %d объявлений, дальше слежу только за новыми", total)
            first_run = False
        if once:
            break
        if deadline and time.time() >= deadline:
            log.info("Отработал отведённое время, выхожу (состояние сохранено)")
            break
        elapsed = time.time() - now  # страховка от холостого прокручивания цикла
        if elapsed < 3:
            time.sleep(3 - elapsed)

    log.info("Готово" if once or deadline else "Остановлено")


if __name__ == "__main__":
    run()

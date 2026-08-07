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


def extract_phones(text: str) -> list:
    phones = []
    for m in PHONE_RE.finditer(text or ""):
        digits = "".join(m.groups())
        if digits[:2] in VALID_PHONE_PREFIXES and digits not in phones:
            phones.append(digits)
    return phones


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
        out.append({
            "key": f'olx:{o.get("id")}',
            "source": "OLX",
            "url": o.get("url") or "",
            "title": o.get("title") or "Без названия",
            "text": text,
            "price_value": price_value,
            "price_currency": (price_currency or "").upper() or None,
            "rooms": extract_rooms(text),
            "district": (loc.get("district") or {}).get("name") or extract_district(text),
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
        price_value, price_currency = o.get("price"), (o.get("priceCurrency") or "").upper()
        rooms = o.get("room") or extract_rooms(desc)
        text = desc[:900]
        title = desc.strip().split("\n")[0][:80] or "Объявление Uybor"
        out.append({
            "key": f'uybor:{o.get("id")}',
            "source": "Uybor",
            "url": f'https://uybor.uz/listings/{o.get("id")}',
            "title": title,
            "text": text,
            "price_value": price_value,
            "price_currency": price_currency or None,
            "rooms": rooms,
            "district": extract_district(text) or (o.get("address") or None),
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
            "key": f"birbir:{bid}",
            "source": "Birbir",
            "url": url,
            "title": title,
            "text": chunk_txt[:600],
            "price_value": price_value,
            "price_currency": price_currency,
            "rooms": extract_rooms(chunk_txt),
            "district": extract_district(chunk_txt),
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
            out.append({
                "key": f"tg:{channel}:{msg_id}",
                "source": f"TG @{channel}",
                "url": f"https://t.me/{channel}/{msg_id}",
                "title": title,
                "text": text[:900],
                "price_value": price_value,
                "price_currency": price_currency,
                "rooms": extract_rooms(text),
                "district": extract_district(text),
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
    if l.get("price_value"):
        p = f'{l["price_value"]:,}'.replace(",", " ")
        cur = "$" if l.get("price_currency") == "USD" else "сум"
        extra = ""
        if l.get("price_usd") and l.get("price_currency") == "UZS":
            extra = f' (~${l["price_usd"]:.0f})'
        lines.append(f"💰 {p} {cur}{extra}")
    details = []
    if l.get("rooms"):
        details.append(f'🛏 {l["rooms"]}-комн')
    if l.get("district"):
        details.append(f'📍 {escape_html(str(l["district"]))}')
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


def send_telegram(cfg, text: str) -> bool:
    api = f'https://api.telegram.org/bot{cfg["telegram_bot_token"]}/sendMessage'
    try:
        r = requests.post(api, json={
            "chat_id": cfg["telegram_chat_id"], "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": False,
        }, timeout=15)
        if r.status_code != 200:
            log.error("Telegram %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except requests.RequestException as e:
        log.error("Telegram недоступен: %s", e)
        return False


# ------------------------------------------------------------------ main ----

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


def run():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    once = "--once" in sys.argv  # один проход по всем источникам и выход
    cfg = load_config()
    store = Store(DB_PATH)
    store.prune()
    first_run = store.counts()[0] == 0

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    enabled = {name: s for name, s in cfg["sources"].items() if s.get("enabled")}
    next_run = {name: 0.0 for name in enabled}
    log.info("Rent Radar запущен%s. Источники: %s. Лимит: $%s",
             " (разовый проход)" if once else "",
             ", ".join(enabled) or "нет", cfg["max_price_usd"])
    if first_run:
        log.info("Первый запуск: текущие объявления запоминаю без уведомлений")

    while not stop["flag"]:
        now = time.time()
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
                if store.known(l["key"]):
                    continue
                seller_cnt = store.bump_seller(l.get("seller_id", ""))

                if first_run:
                    store.save(l, notified=False)
                    continue

                if not passes_filters(l, cfg):
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

                likely_makler = seller_cnt >= cfg["makler_user_threshold"]
                ok = send_telegram(cfg, format_message(l, cfg, likely_makler))
                store.save(l, notified=ok)
                if ok:
                    fresh += 1
                    log.info("[%s] уведомление: %s", name, l["title"][:60])
                time.sleep(1)

            if fresh:
                log.info("[%s] новых: %d", name, fresh)

        if first_run:
            total, _ = store.counts()
            log.info("Запомнил %d объявлений, дальше слежу только за новыми", total)
            first_run = False
        if once:
            break
        time.sleep(15)

    log.info("Готово" if once else "Остановлено")


if __name__ == "__main__":
    run()

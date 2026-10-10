#!/usr/bin/env python3
"""
Рано — ассистент по поиску жилья (ядро: сбор данных и работа с маклерами).

Источники: OLX.uz (API), Uybor.uz (API), Birbir.uz (HTML),
Telegram-каналы (публичные веб-превью t.me/s/..., без логина).

Находит новые объявления, отсеивает дубли (один и тот же вариант из разных
каналов/от разных авторов) и шлёт уникальные в ваш Telegram.

Запуск: python3 rent_radar.py
"""

import hashlib
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

import analyst
import concierge
import followup
import sale_sources
import market

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
# Базы с данными (объявления, маклеры, телефоны) в публичный репозиторий не
# попадают: на GitHub Actions они живут в приватном rent-radar-state, workflow
# кладёт его в каталог из RADAR_STATE_DIR. Локально — рядом с кодом.
STATE_DIR = Path(os.environ.get("RADAR_STATE_DIR") or BASE_DIR)
DB_PATH = STATE_DIR / "radar.db"
# Поиск квартиры для покупки — отдельная база: цены продажи не должны
# попадать в арендную аналитику и дедупликацию.
SALE_DB_PATH = STATE_DIR / "sale.db"

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
    "assistant_name": "Ra'no",     # как ассистент представляется маклерам
    "owner_name": "Шохрух",        # от чьего имени ведётся поиск
    "bot_username": "",            # определяется автоматически через getMe
    "makler_user_threshold": 3,
    # Отсев нерелевантного: подселение/койко-места вместо целой квартиры
    "min_sane_price_usd": 150,        # дешевле — это комната/подселение, не квартира
    "require_price_or_district": True,  # без цены И без района толку нет
    "broker_min_ads": 3,         # от скольких объявлений считаем продавца маклером
    "max_owner_ads": 2,          # у настоящего хозяина 1–2 объявления, не больше
    "seller_cache_days": 3,      # как часто перепроверять число объявлений продавца
    # Покупка квартиры от собственника (личный поиск владельца бота).
    # Источник — Uybor: OLX и Birbir отдают 403 на автоматические запросы.
    "sale_search": {
        "enabled": False,
        "max_price_usd": 45000,
        "min_price_usd": 5000,       # дешевле — опечатка или цена за м²
        "rooms": [1, 2],
        "districts": ["Яккасарай", "Мирабад", "Шайхантахур", "Юнусабад"],
        # Квартира в продаже неделями остаётся актуальной (на Uybor объявление
        # живёт 45 дней), поэтому окно шире, чем у аренды.
        "notify_max_age_days": 45,
        "owner_only": True,          # False — присылать и маклеров/агентства (с пометкой)
        "max_owner_ads": 2,          # у собственника 1–2 объявления, у агентства — десятки
        "first_run_limit": 25,       # не больше стольких уведомлений за один проход
        "uybor": {
            "enabled": True, "interval_seconds": 60,
            "region_id": 13, "category_id": 7, "limit": 100,   # 100 — максимум API
        },
    },
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
    # Webhook-воркер (Cloudflare): он принимает обновления Telegram и ведёт
    # ИИ-интервью, а нам отдаёт очередь. Без него — старый getUpdates.
    if os.environ.get("RADAR_WORKER_URL"):
        cfg["worker_url"] = os.environ["RADAR_WORKER_URL"].rstrip("/")
    if os.environ.get("RADAR_WORKER_KEY"):
        cfg["worker_key"] = os.environ["RADAR_WORKER_KEY"]
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

# Отрицательный lookbehind нужен, чтобы «5/9 1100 у.е.» не читалось как «91100»:
# число не может начинаться сразу после цифры или дроби.
# Плюс (?<!\w) у варианта с разделителями тысяч: иначе «70м2 850$» читалось как 2850.
_NUM = r"(?<![\d/.,])((?<!\w)\d{1,3}(?:[\s.,]\d{3})+|\d+)"
PRICE_USD_RE = re.compile(_NUM + r"\s*(?:у\.?\s?е|\$|usd)", re.I)
PRICE_UZS_RE = re.compile(_NUM + r"\s*(?:сум|so['’`]?m|sum)", re.I)

DISTRICTS = {
    "Яккасарай": ["яккасарай", "yakkasaroy", "yakkasaray"],
    "Мирабад": ["мирабад", "mirobod", "mirabad"],
    "Юнусабад": ["юнусабад", "yunusobod", "yunusabad"],
    "Чиланзар": ["чиланзар", "chilonzor", "chilanzar"],
    "Мирзо-Улугбек": ["мирзо улугбек", "мирзо-улугбек", "mirzo ulug", "mirzo-ulug", "улугбек"],
    "Шайхантахур": ["шайхантахур", "shayxontohur", "shayxontoxur", "shaykhantakhur", "шайхантаур"],
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


# Признаки подселения / койко-места (это НЕ отдельная квартира).
# Осторожно: «хозяином» без «с» — это «сдаёт хозяин», наоборот хороший признак.
SHARED_KEYWORDS = [
    "шерик", "sherik",                     # шериклик / шерикчилик / sheriklikka
    "подселен", "койко", "койка", "сожител", "спальное место", "место в комнате",
    "с хозяйкой", "с хозяином", "с хозяйкою", "xo'jayin bilan", "ega bilan",
    "bola kerak", "bolalar kerak", "bollar kerak", "yigit kerak",
    "qiz kerak", "qizlar kerak", "qizlar olinadi", "bola olinadi",
    "оламиз", "olinadi", "ищу соседа", "ищу соседку", "ищем соседа",
    "сожительниц", "подсел",
    "sherilik", "sherkilik", "шериклик", "шерикчилик",   # частые опечатки
    "xona / joy", "xona/joy", "joy beriladi", "o'rin beriladi", "o'rindosh",
    "xona beriladi", "1 o'rin", "bitta joy",
    # аренда отдельной комнаты, а не квартиры
    "аренда комнаты", "аренда одной комнаты", "одной комнаты", "одну комнату",
    "сдается комната", "сдаётся комната", "сдам комнату", "сдаю комнату",
    "комната в квартире", "комнаты в квартире", "комната для", "комнату в аренду",
]


def looks_like_room_share(text: str) -> str:
    """Возвращает найденный признак подселения или ''."""
    low = (text or "").lower()
    for kw in SHARED_KEYWORDS:
        if kw in low:
            return kw
    return ""


def extract_phones(text: str) -> list:
    phones = []
    for m in PHONE_RE.finditer(text or ""):
        digits = "".join(m.groups())
        if digits[:2] in VALID_PHONE_PREFIXES and digits not in phones:
            phones.append(digits)
    return phones


def sane(v, lo, hi):
    """Отбрасывает мусорные значения из объявлений (площадь 4 м², этаж 99 и т.п.)."""
    return v if (v is not None and lo <= v <= hi) else None


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


def extract_price_from_text(text: str, max_usd: int = 20000):
    """Возвращает (value, currency) или (None, None).
    max_usd: для аренды цена выше $20 000 — ошибка; для продажи (варианты маклеров) — норма."""
    m = PRICE_USD_RE.search(text or "")
    if m:
        v = _num(m.group(1))
        if v and 30 <= v <= max_usd:
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
        prm = {}
        for p_ in o.get("params", []):
            v_ = p_.get("value") or {}
            prm[p_.get("key")] = v_.get("label") or v_.get("key")
        mp = o.get("map") or {}
        photo_urls = []
        for ph in (o.get("photos") or [])[:6]:
            link = (ph or {}).get("link") or ""
            if link:
                photo_urls.append(
                    link.replace("{width}x{height}", "1280x1024")
                        .replace("{width}", "1280").replace("{height}", "1024"))
        out.append({
            "photo_urls": photo_urls,
            "lat": mp.get("lat"), "lon": mp.get("lon"),
            "area": sane(as_int(prm.get("total_area")), 10, 500),
            "floor": sane(as_int(prm.get("floor")), 1, 60),
            "floors_total": sane(as_int(prm.get("total_floors")), 1, 60),
            "furnished": prm.get("furnished"),
            "house_type": prm.get("house_type"),
            "commission": prm.get("comission"),
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


UYBOR_API = "https://api.uybor.uz/api/v1/listings"


def uybor_listing(o: dict) -> dict:
    """Одно объявление Uybor → унифицированный dict (общий для аренды и продажи)."""
    desc = o.get("description") or ""
    price_value, price_currency = o.get("price"), canon_currency(o.get("priceCurrency"))
    rooms = as_int(o.get("room")) or extract_rooms(desc)
    price_value = as_int(price_value) if price_value is not None else None
    if o.get("priceType") == "sqm" and price_value and as_int(o.get("square")):
        price_value = price_value * as_int(o.get("square"))   # цена указана за м²
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
    return {
        "photo_urls": photo_urls,
        "lat": o.get("lat"), "lon": o.get("lng"),
        "area": sane(as_int(o.get("square")), 10, 500),
        "floor": sane(as_int(o.get("floor")), 1, 60),
        "floors_total": sane(as_int(o.get("floorTotal")), 1, 60),
        "house_type": o.get("foundation"),
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
    }


def fetch_uybor(scfg: dict, cfg: dict) -> list:
    params = {
        "limit": 30,
        "operationType__eq": "rent",
        "category__eq": scfg["category_id"],
        "region__eq": scfg["region_id"],
        "sort": "-createdAt",
    }
    r = requests.get(UYBOR_API, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return [uybor_listing(o) for o in r.json().get("results", [])]


def fetch_uybor_sale(ss: dict, cfg: dict) -> list:
    """Продажа квартир на Uybor: по запросу на каждый нужный район,
    сразу с фильтром по комнатам (фильтр района на стороне API — district__eq)."""
    u = ss.get("uybor") or {}
    district_ids = [i for i, name in UYBOR_DISTRICT_IDS.items()
                    if name in (ss.get("districts") or [])]
    out, seen = [], set()
    for did in district_ids:
        params = {
            "limit": u.get("limit", 50),
            "operationType__eq": "sale",
            "category__eq": u.get("category_id", 7),
            "region__eq": u.get("region_id", 13),
            "district__eq": did,
            "sort": "-createdAt",
        }
        if ss.get("rooms"):
            params["room__in"] = ",".join(str(r) for r in ss["rooms"])
        if ss.get("max_price_usd"):
            # У Uybor priceCurrency__eq — это валюта порога цены, а не фильтр по
            # валюте объявления: price__lte сравнивается с ценой, приведённой к ней,
            # так что объявления в сумах тоже попадают. Без валюты price__lte не работает.
            params["priceCurrency__eq"] = "usd"
            params["price__lte"] = int(ss["max_price_usd"])
        r = requests.get(UYBOR_API, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        for o in r.json().get("results", []):
            l = uybor_listing(o)
            if l["key"] in seen:
                continue
            seen.add(l["key"])
            l["key"] = "sale:" + l["key"]
            l["source"] = "Uybor · продажа"
            l["new_building"] = market.looks_new(o.get("isNewBuilding"), l["text"])
            l["repair"] = o.get("repair")
            out.append(l)
        time.sleep(0.5)
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


def fetch_realt24_rent(scfg: dict, cfg: dict) -> list:
    """Аренда на Realt24: открытый API, телефон и признак посредника — сразу в списке."""
    import sale_sources
    r = requests.get(f"{REALT24_API}?{REALT24_Q['rent']}&currency=usd&sortBy=dateDesc&page=1"
                     f"&perPage={scfg.get('limit', 50)}", headers=HEADERS, timeout=25)
    r.raise_for_status()
    me = sys.modules[__name__]
    return [l for l in (sale_sources._realt24_listing(me, it, "rent") for it in r.json().get("data") or []) if l]


def fetch_joymee_rent(scfg: dict, cfg: dict) -> list:
    """Аренда на Joymee: список свежих (без комнат и телефона) + карточка только для новых."""
    import sale_sources
    params = dict(JOYMEE_Q["rent"], region=JOYMEE_TASHKENT, ordering="newest", page=1)
    r = requests.get(JOYMEE_API, params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    st = Store(DB_PATH)
    try:
        new = [x for x in r.json().get("results") or [] if not st.known(f"joymee:{x.get('id')}")]
    finally:
        st.conn.close()
    me, out = sys.modules[__name__], []
    for x in new[:scfg.get("details_per_pass", 8)]:
        try:
            det = sale_sources._joymee_detail(me, x["id"])
        except (requests.RequestException, ValueError) as e:
            log.info("[joymee] %s: %s", x.get("id"), e)
            continue
        l = sale_sources._joymee_listing(me, x["id"], det)
        l.update(key=f"joymee:{x['id']}", source="Joymee",
                 is_business={1: False, 2: True}.get(det.get("advertiser_type")))
        out.append(l)
        time.sleep(0.3)
    return out


SOURCE_FETCHERS = {
    "olx": fetch_olx,
    "uybor": fetch_uybor,
    "birbir": fetch_birbir,
    "telegram": fetch_telegram,
    "realt24": fetch_realt24_rent,
    "joymee": fetch_joymee_rent,
}


def count_seller_ads(listing: dict, store, cfg) -> int:
    """Сколько активных объявлений у этого продавца.
    Для OLX спрашиваем напрямую — это точное число; для прочих считаем по своей базе."""
    sid = listing.get("seller_id") or ""
    if not sid:
        return -1
    cached = store.seller_ads_cached(sid, cfg.get("seller_cache_days", 3))
    if cached is not None:
        return cached

    cnt = -1
    if sid.startswith("olx:"):
        uid = sid.split(":", 1)[1]
        if uid and uid != "None":
            try:
                r = requests.get("https://www.olx.uz/api/v1/offers/", timeout=20,
                                 headers=HEADERS, params={"user_id": uid, "limit": 1})
                if r.status_code == 200:
                    md = (r.json() or {}).get("metadata") or {}
                    cnt = int(md.get("total_elements", md.get("visible_total_count", -1)))
            except (requests.RequestException, ValueError, TypeError) as e:
                log.info("не удалось узнать число объявлений %s: %s", sid, e)
    if cnt < 0:                                   # запасной вариант — своя статистика
        row = store.conn.execute(
            "SELECT cnt FROM seller_counts WHERE seller_id=?", (sid,)).fetchone()
        cnt = row[0] if row else -1
    if cnt >= 0:
        store.seller_ads_put(sid, cnt)
    return cnt


def phone_spread(listing: dict, store) -> int:
    """В скольких объявлениях встречался телефон продавца."""
    best = 0
    for ph in (listing.get("phones") or [])[:2]:
        row = store.conn.execute(
            "SELECT COUNT(DISTINCT key) FROM phones WHERE phone=?", (ph,)).fetchone()
        best = max(best, row[0] if row else 0)
    return best


def owner_only_reject(l: dict, store, cfg, settings) -> str:
    """Строгий режим: пропускаем только тех, кто почти наверняка хозяин."""
    if (l.get("commission") or "").strip().lower() in ("да", "ha", "yes"):
        return "продавец берёт комиссию"
    if l.get("is_business"):
        return "бизнес-аккаунт, а не частное лицо"

    text = f"{l.get('title', '')} {l.get('text', '')}".lower()
    says_owner = any(kw.lower() in text for kw in (cfg.get("hot_keywords") or []))

    spread = phone_spread(l, store)
    if spread > cfg.get("max_owner_ads", 2):
        return f"телефон встречается в {spread} объявлениях"

    ads = l.get("seller_ads")
    if ads is None:
        ads = count_seller_ads(l, store, cfg)
        l["seller_ads"] = ads
    limit = cfg.get("max_owner_ads", 2)
    if ads > limit:
        return f"у продавца {ads} объявлений — это маклер"
    if ads < 0 and not says_owner:
        return "не удалось подтвердить, что это хозяин"
    return ""


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
        self.conn.execute("""CREATE TABLE IF NOT EXISTS brokers(
            bid TEXT PRIMARY KEY, source TEXT, name TEXT, phone TEXT, ads INTEGER,
            districts TEXT, min_price REAL, max_price REAL,
            first_seen TEXT, status TEXT DEFAULT 'new', last_contact TEXT)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS broker_offers(
            oid INTEGER PRIMARY KEY AUTOINCREMENT,
            broker_chat TEXT, broker_name TEXT, broker_phone TEXT,
            media_group TEXT, text TEXT, photos TEXT,
            district TEXT, rooms INTEGER, area REAL, price_usd REAL, price_raw TEXT,
            floor INTEGER, floors_total INTEGER,
            created_at TEXT, status TEXT DEFAULT 'new', note TEXT,
            asked_at TEXT, replied_at TEXT, dup_of INTEGER)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS seller_ads(
            seller_id TEXT PRIMARY KEY, cnt INTEGER, checked_at TEXT)""")
        try:                                   # миграция старых баз
            self.conn.execute("ALTER TABLE listings ADD COLUMN data TEXT")
        except sqlite3.OperationalError:
            pass
        try:                                   # специализация маклера: ["rent"], ["sale"], оба
            self.conn.execute("ALTER TABLE brokers ADD COLUMN deals TEXT")
        except sqlite3.OperationalError:
            pass
        try:                                   # разбор варианта моделью: адрес, ремонт, комиссия…
            self.conn.execute("ALTER TABLE broker_offers ADD COLUMN extra TEXT")
        except sqlite3.OperationalError:
            pass
        try:                                   # Telegram-юзернейм — когда телефона нет (Realting)
            self.conn.execute("ALTER TABLE brokers ADD COLUMN tg TEXT")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def upsert_broker(self, bid, source, name, phone, ads, district, price, deal="rent", tg=None):
        """Копим карточку маклера: телефон, районы, диапазон цен, аренда/продажа.
        Диапазон цен копим только по аренде — цены продажи в нём бессмысленны."""
        row = self.conn.execute(
            "SELECT districts, min_price, max_price, phone, ads, deals, name, tg FROM brokers WHERE bid=?",
            (bid,)).fetchone()
        ds = set()
        lo = hi = None
        deals = {deal}
        if row:
            ds = set(json.loads(row[0] or "[]"))
            lo, hi = row[1], row[2]
            phone = phone or row[3]
            ads = max(ads or 0, row[4] or 0)
            deals |= set(json.loads(row[5])) if row[5] else {"rent"}
            name = name or row[6]
            tg = tg or row[7]
        if deal != "rent":
            price = None
        if district:
            ds.add(district)
        if price:
            lo = price if lo is None else min(lo, price)
            hi = price if hi is None else max(hi, price)
        now = datetime.now(timezone.utc).isoformat()
        dj = json.dumps(sorted(deals))
        if row:
            self.conn.execute(
                "UPDATE brokers SET name=?, phone=?, ads=?, districts=?, "
                "min_price=?, max_price=?, deals=?, tg=? WHERE bid=?",
                (name, phone, ads, json.dumps(sorted(ds), ensure_ascii=False), lo, hi, dj, tg, bid))
        else:
            self.conn.execute(
                "INSERT INTO brokers(bid, source, name, phone, ads, districts, "
                "min_price, max_price, first_seen, deals, tg) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (bid, source, name, phone, ads,
                 json.dumps(sorted(ds), ensure_ascii=False), lo, hi, now, dj, tg))
        self.conn.commit()

    def brokers(self, status=None, with_phone=True, limit=50, deal=None):
        q = "SELECT bid, source, name, phone, ads, districts, min_price, max_price, status, deals, tg " \
            "FROM brokers WHERE 1=1"
        args = []
        if status:
            q += " AND status=?"; args.append(status)
        if deal == "sale":
            q += " AND deals LIKE '%\"sale\"%'"
        elif deal == "rent":                   # старые записи без пометки — арендные
            q += " AND (deals IS NULL OR deals LIKE '%\"rent\"%')"
        if with_phone:                         # есть контакт: телефон или Telegram
            q += " AND ((phone IS NOT NULL AND phone != '') OR (tg IS NOT NULL AND tg != ''))"
        q += " ORDER BY ads DESC LIMIT ?"; args.append(limit)
        out = []
        for r in self.conn.execute(q, args):
            out.append({"bid": r[0], "source": r[1], "name": r[2], "phone": r[3],
                        "ads": r[4], "districts": json.loads(r[5] or "[]"),
                        "min_price": r[6], "max_price": r[7], "status": r[8],
                        "deals": json.loads(r[9]) if r[9] else ["rent"], "tg": r[10] or ""})
        return out

    def broker_status(self, bid, status):
        self.conn.execute(
            "UPDATE brokers SET status=?, last_contact=? WHERE bid=?",
            (status, datetime.now(timezone.utc).isoformat(), bid))
        self.conn.commit()

    def broker_stats(self, deal=None):
        cond = {"sale": " WHERE deals LIKE '%\"sale\"%'",
                "rent": " WHERE (deals IS NULL OR deals LIKE '%\"rent\"%')"}.get(deal, " WHERE 1=1")
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM brokers" + cond + " GROUP BY status").fetchall()
        total = self.conn.execute("SELECT COUNT(*) FROM brokers" + cond).fetchone()[0]
        withph = self.conn.execute(
            "SELECT COUNT(*) FROM brokers" + cond +
            " AND ((phone IS NOT NULL AND phone!='') OR (tg IS NOT NULL AND tg!=''))").fetchone()[0]
        return total, withph, dict(rows)

    def seller_ads_cached(self, seller_id: str, max_age_days: int):
        row = self.conn.execute(
            "SELECT cnt, checked_at FROM seller_ads WHERE seller_id=?",
            (seller_id,)).fetchone()
        if not row:
            return None
        try:
            when = datetime.fromisoformat(row[1])
        except (TypeError, ValueError):
            return None
        if (datetime.now(timezone.utc) - when).days > max_age_days:
            return None
        return row[0]

    def seller_ads_put(self, seller_id: str, cnt: int):
        self.conn.execute(
            "INSERT OR REPLACE INTO seller_ads(seller_id, cnt, checked_at) VALUES(?,?,?)",
            (seller_id, cnt, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    KEEP = ("key", "source", "url", "title", "text", "price_value", "price_currency",
            "price_usd", "rooms", "district", "district_raw", "phones", "created_at",
            "seller", "seller_id", "is_business", "photo_urls", "lat", "lon", "area",
            "floor", "floors_total", "furnished", "house_type", "commission",
            "seller_ads", "premium", "seller_kind", "listed_since", "new_building", "repair",
            "site", "seller_hint", "mortgage", "price_note", "score", "why", "alts", "repair_photo")

    def pack(self, listing: dict) -> str:
        d = {k: listing.get(k) for k in self.KEEP}
        d["text"] = (d.get("text") or "")[:600]
        d["photo_urls"] = (d.get("photo_urls") or [])[:4]
        return json.dumps(d, ensure_ascii=False)

    def has_data(self, key: str) -> bool:
        row = self.conn.execute(
            "SELECT data IS NOT NULL FROM listings WHERE key=?", (key,)).fetchone()
        return bool(row and row[0])

    def backfill(self, listing: dict):
        self.conn.execute("UPDATE listings SET data=? WHERE key=?",
                          (self.pack(listing), listing["key"]))
        self.conn.commit()

    def recent(self, days=7):
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT data FROM listings WHERE first_seen > ? AND dup_of IS NULL "
            "AND data IS NOT NULL", (since,)).fetchall()
        out = []
        for (raw,) in rows:
            try:
                out.append(json.loads(raw))
            except (TypeError, ValueError):
                pass
        return out

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
            "price_usd, rooms, district, first_seen, notified, dup_of, data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (listing["key"], listing["source"], listing["url"], listing["title"],
             normalize_text(listing["text"]), listing.get("price_usd"),
             listing.get("rooms"), listing.get("district"), now,
             int(notified), dup_of, self.pack(listing)))
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

    ev = []
    if l.get("price_note"):
        ev.append(l["price_note"])
    if l.get("seller_ads") is not None and l["seller_ads"] >= 0:
        ev.append(f"{l['seller_ads']} объявл. у продавца")
    if (l.get("commission") or "").lower() in ("нет", "yo'q", "no"):
        ev.append("без комиссии")
    if ev:
        lines.append("🔑 " + " · ".join(ev))

    if l.get("score") is not None:              # разбор от аналитика
        lines.append(f'\n<b>Оценка {l["score"]}/10</b>')
        for x in (l.get("pros") or [])[:5]:
            lines.append(escape_html(x))
        for x in (l.get("cons") or [])[:3]:
            lines.append("⚠️ " + escape_html(x))
        q = analyst.ask_seller(l)
        if q:
            lines.append("❓ Спросить: " + escape_html("; ".join(q)))

    lines.append(f'\n<a href="{l["url"]}">Открыть объявление</a>  ⚡ Звоните сразу!')
    return "\n".join(lines)


def detect_bot_username(cfg) -> str:
    """Спрашиваем у Telegram, как бот называется сейчас.
    Так переименование в BotFather подхватывается само, без правок кода."""
    r = tg_call(cfg, "getMe", {}, quiet=True)
    name = ((r or {}).get("result") or {}).get("username") or ""
    if name:
        cfg["bot_username"] = name
    return name


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
        if r.status_code != 200:
            return False
        try:
            return _msg_ids(r.json()) or True
        except ValueError:
            return True
    except requests.RequestException as e:
        log.warning("Загрузка фото не удалась: %s", e)
        return False


def _msg_ids(resp) -> list:
    """id отправленных сообщений из ответа Telegram (одно сообщение или альбом)."""
    res = (resp or {}).get("result") if isinstance(resp, dict) else None
    items = res if isinstance(res, list) else [res] if isinstance(res, dict) else []
    return [m["message_id"] for m in items if isinstance(m, dict) and m.get("message_id")]


def send_listing(cfg, settings: dict, l: dict, likely_makler: bool, text: str = None, silent=False):
    """Уведомление об объявлении: альбом с фото, если они есть и включены.
    Возвращает id отправленных сообщений (или True, если id неизвестны); False — не ушло.
    silent — без звука (ночью)."""
    text = text or format_message(l, cfg, likely_makler)
    photos = (l.get("photo_urls") or []) if settings.get("photos", True) else []
    quiet = {"disable_notification": True} if silent else {}
    if photos:
        media = [{"type": "photo", "media": u} for u in photos[:4]]
        media[0]["caption"] = text[:1000]
        media[0]["parse_mode"] = "HTML"
        r = tg_call(cfg, "sendMediaGroup", {
            "chat_id": cfg["telegram_chat_id"],
            "media": json.dumps(media), **quiet,
        })
        if r is not None:
            return _msg_ids(r) or True
        up = send_photo_upload(cfg, photos[0], text)
        if up:
            return up
        log.info("Фото не отправились, шлю текстом: %s", l["title"][:50])
    r = tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML", **quiet})
    return False if r is None else (_msg_ids(r) or True)


def is_night(now=None) -> bool:
    """23:00–08:00 по Ташкенту: срочное присылаем, но без звука."""
    h = (now or datetime.now(timezone.utc)).astimezone(TASHKENT_TZ).hour
    return h >= 23 or h < 8


# ------------------------------------------------- настройки через бота ----

WELCOME_TEXT = (
    "Привет! Я <b>Ra'no</b> 👋 — ИИ-ассистент, которая обожает квартиры в Ташкенте: аренда и покупка.\n\n"
    "Ищу двумя способами:\n"
    "🔎 Сначала сама — каждый день прочёсываю сайты и Telegram-каналы, выгодное приношу сразу.\n"
    "📇 Если на сайтах пусто — подключу маклеров: составлю запрос, вы отправите его в пару нажатий.\n"
    "К каждому варианту — честный разбор цены: дешевле рынка или кто-то загнул 😉\n\n"
    "С чего начнём? Напишите своими словами, что ищете, — например: «купить двушку в центре до $50 000, "
    "нужна ипотека» 👇")

HELP_TEXT = """🏠 <b>Ra'no</b> — ваш ИИ-ассистент по поиску жилья. Рассказываю, как со мной дружить 🙂

<b>Команды запоминать не нужно.</b> Внизу три кнопки — два способа искать:
🔎 <b>Ищет Ra'no</b> — я сама смотрю сайты (Uybor, Realt24, Joymee, Realting, Yangiuylar) и Telegram-каналы. Выгодное — сразу, остальное — подборкой в 19:30
📇 <b>Через маклеров</b> — если на сайтах пусто: запрос маклерам по одному, их варианты карточками, шортлист
⋯ <b>Ещё</b> — вариант из WhatsApp, цены рынка, текст запроса, начать заново

Поменять поиск — просто напишите («бюджет 60 тысяч», «добавь Юнусабад»).
Варианты из WhatsApp — перешлите сюда, соберу карточку с анализом цены.
Понравилось объявление с сайта — «👍 В шортлист». В шортлисте нажмите номер: уточнить, назначить просмотр, заметка, «посмотрел».
Напоминаю сама: если маклер молчит сутки, утром в день просмотра и за 2 часа до него; в 20:00 — итоги дня. Я пунктуальная 😉

Для тонкой настройки радара остались команды: /menu, /status, /owner, /segment, /work, /photos, /pause, /resume"""

DISTRICT_LIST = sorted(DISTRICTS)
PRICE_PRESETS = [300, 400, 500, 700, 1000, 1500]
MIN_PRESETS = [0, 200, 300, 400, 500]
ROOM_PRESETS = [("1", "1"), ("2", "2"), ("2–3", "2-3"), ("3+", "3-6"), ("любые", "*")]

ON_WORDS = {"on", "вкл", "да", "yes", "1"}
OFF_WORDS = {"off", "выкл", "нет", "no", "0"}
RESET_WORDS = {"все", "всё", "любые", "любая", "сброс", "all", "any", "reset"}


def default_settings() -> dict:
    return {"photos": True, "paused": False, "districts": [],
            "strict_district": True, "exclude_shared": True, "segment": "any",
            "owner_only": True,
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
        [_btn("🔎 Подобрать лучшее сейчас", "f")],
        # Мини-апп открывается только с reply-клавиатуры: Telegram разрешает
        # WebApp.sendData() исключительно оттуда. Здесь — кнопка, которая её пришлёт.
        [_btn("💬 Задать поиск", "ank"), _btn("📇 Написать маклерам", "b")],
        [_btn("📥 Новые варианты", "off"), _btn("📋 Шортлист", "sl")],
        [_btn("🏢 Класс: новый ЖК с ремонтом" if settings.get("segment") == "premium"
              else "🏢 Класс: любой", "sg")],
        [_btn("🔑 Только хозяева" if settings.get("owner_only", True)
              else "🔓 Все, включая маклеров", "oo")],
        [_btn(f"💰 Цена: {price_label(cfg, settings)}", "v:P")],
        [_btn(f"🛏 Комнаты: {rooms_label(settings)}", "v:R"),
         _btn(f"📍 Районы: {districts_label(settings)}", "v:D")],
        [_btn("🚫 Подселение: скрыто" if settings.get("exclude_shared", True)
              else "⚠️ Подселение: показываю", "sh")],
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
            f"🔑 Только хозяева: {'да' if settings.get('owner_only', True) else 'нет'}\n"
            f"🏢 Класс: {'новый ЖК' if settings.get('segment') == 'premium' else 'любой'}\n"
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
    cfg = effective_sale_cfg(cfg, store)       # покупка из чата меняет и поиск /sale
    arg = arg.strip()
    raw_arg = t.partition(" ")[2].strip()

    if cmd == "/start" and raw_arg.startswith("p"):
        # параметры из мини-аппа, открытого кнопкой меню (там нет sendData)
        ans = concierge.decode_start_code(raw_arg[1:])
        if not ans or not concierge.apply_webapp_data(
                cfg, store, json.dumps({"v": 2, "ans": ans}, ensure_ascii=False)):
            concierge.send_app_button(
                cfg, store, "Не получилось прочитать параметры — опишите поиск словами, я соберу заново.")
        return "", None
    if cmd == "/start":
        # Первое касание: тёплое знакомство, одно понятное действие, честное
        # ожидание. Стена команд отпугивает — её показываем только по /help.
        concierge.send_app_button(cfg, store, WELCOME_TEXT)
        return "", None
    if cmd == "/help":
        return HELP_TEXT, None
    if cmd in ("/rano", "/via"):
        send_screen(cfg, (rano_screen if cmd == "/rano" else via_screen)(cfg, store))
        return "", None
    if cmd == "/menu":
        return "", "M"
    if cmd == "/status":
        return status_text(cfg, settings, store), None
    if cmd in ("/sale", "/buy", "/kupit", "/покупка"):
        return sale_status_text(cfg), None
    if cmd in ("/rynok", "/market", "/рынок"):
        return sale_market_text(cfg), None
    if cmd in ("/owner", "/hozyain"):
        if arg in OFF_WORDS or arg in RESET_WORDS:
            settings["owner_only"] = False
        elif arg in ON_WORDS:
            settings["owner_only"] = True
        else:
            settings["owner_only"] = not settings.get("owner_only", True)
        return (("🔑 Только хозяева: проверяю комиссию, число объявлений продавца "
                 "и повторы телефона" if settings["owner_only"]
                 else "🔓 Показываю всех, включая маклеров"), None)
    if cmd in ("/segment", "/class"):
        if arg in ("премиум", "premium", "жк", "новостройка", "вкл", "on"):
            settings["segment"] = "premium"
        elif arg in RESET_WORDS or arg in OFF_WORDS:
            settings["segment"] = "any"
        else:
            settings["segment"] = "any" if settings.get("segment") == "premium" else "premium"
        return (("✅ Ищу только новые ЖК с дизайнерским ремонтом"
                 if settings["segment"] == "premium" else "✅ Класс жилья: любой"), None)
    if cmd == "/work":
        if not raw_arg:
            wp = settings.get("work_point")
            cur = (f"Сейчас: {settings.get('work_label') or wp}" if wp
                   else "Пока не задан.")
            return ("🏢 <b>Адрес работы</b> — агент будет считать до него расстояние.\n"
                    f"{cur}\nЗадать: <code>/work Амир Темур 107Б</code>\n"
                    "Убрать: <code>/work нет</code>"), None
        if arg in RESET_WORDS or arg in OFF_WORDS:
            settings["work_point"] = None
            settings["work_label"] = None
            return "✅ Адрес работы убран", None
        g = analyst.geocode(raw_arg, requests)
        if not g:
            return "Не нашла такой адрес 🙈 Попробуйте иначе, например: /work метро Айбек", None
        lat, lon, label = g
        settings["work_point"] = [lat, lon]
        settings["work_label"] = raw_arg
        m = analyst.nearest_metro(lat, lon)
        extra = f"\nБлижайшее метро: «{m[0]}» ({m[2]} мин пешком)" if m else ""
        return (f"✅ Работа: {escape_html(raw_arg)}\n📍 {escape_html(label)}{extra}\n"
                "Теперь в разборе появится расстояние до неё."), None
    if cmd in ("/app", "/mini", "/開"):
        concierge.send_app_button(cfg, store)
        return "", None
    if cmd in ("/anketa", "/start_search", "/profile"):
        concierge.send_app_button(
            cfg, store,
            "📋 Опишите своими словами, что ищете, — я уточню остальное.\n"
            "Если хочется по старинке, пошагово кнопками — /steps")
        return "", None
    if cmd == "/steps":
        concierge.start_anketa(cfg, store)
        return "", None
    if cmd in ("/shortlist", "/short"):
        concierge.show_shortlist(cfg, store)
        return "", None
    if cmd in ("/offers", "/varianty"):
        concierge.show_offers(cfg, store)
        return "", None
    if cmd in ("/prices", "/index"):
        return concierge.concierge_status(store), None
    if cmd in ("/request", "/text"):
        t = store.get_kv("request_text")
        if not t:
            return "Текст запроса ещё не готов — пройдите /anketa", None
        return f"📝 <b>Текущий запрос маклерам</b>\n\n<code>{escape_html(t)}</code>", None
    if cmd in ("/brokers", "/makler", "/outreach"):
        send_broker_cards(cfg, store, settings)
        return "", None
    if cmd in ("/find", "/top", "/search"):
        run_search(cfg, store, settings)
        return "", None

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


def harvest_broker(l: dict, store, cfg, ads: int):
    """Маклер — не проблема, а канал: копим его контакт и специализацию."""
    if ads is None or ads < cfg.get("broker_min_ads", 3):
        return
    phones = l.get("phones") or []
    bid = l.get("seller_id") or (f"tel:{phones[0]}" if phones else "")
    if not bid:
        return
    store.upsert_broker(bid, l.get("source", ""), (l.get("seller") or "")[:60],
                        phones[0] if phones else None, ads,
                        l.get("district"), l.get("price_usd"))


# ------------------------------------------ маклеры по продаже: сбор контактов ----
# Откуда: Uybor (продажа; телефон — только если продавец написал его в описании:
# кнопка «показать телефон» на сайте защищена капчей, её не обходим) и публичные
# Telegram-каналы с объявлениями о продаже. OLX и Birbir с серверов отвечают 403.
SALE_BROKER_CHANNELS = [
    "Kvartiritashkenta", "kvartiry_tashkent", "tashkent_nedvizhimost", "toshkent_kvartira",
    "Tashkentflat", "uybor", "domtutuzb", "nedvizhimost_tashkent",
    # арендные каналы тоже публикуют продажу — с них берём только объявления о продаже
    "arentash", "arendakvartir_uz", "arendatashkent_uz", "arenda_kvartira_v_tashkente",
]
SALE_POST_WORDS = ("прода", "sotiladi", "sotuvda", "sotaman", "sotuv", "купить", "ипотек", "ipoteka",
                   "сотилади", "сотувда", "сотаман")
RENT_POST_WORDS = ("аренд", "сдает", "сдаёт", "сдается", "сдаётся", "ijara", "/мес", "oyiga", "ижара", "ойига",
                   "в месяц", "посуточ", "kunlik", "сниму", "ищу", "kerak")
BROKER_POST_WORDS = ("агентств", "риелт", "риэлт", "маклер", "makler", "rieltor", "agentlik",
                     "комисси", "vositachi", "услуг", "xizmat", "realty", "estate")


def _sale_post(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in SALE_POST_WORDS) and not any(w in low for w in RENT_POST_WORDS)


def effective_sale_cfg(cfg, store):
    """Покупка из чата-интервью → поиск Uybor по тем же параметрам.
    Без районов в запросе — районы из настроек (по умолчанию центр)."""
    ans = (store.get_kv("anketa") or {}).get("ans") or {}
    if ans.get("deal") != "buy" or ans.get("object", "flat") != "flat":
        return cfg
    ss = dict(cfg.get("sale_search") or {})
    b = str(ans.get("budget") or "")
    if b.isdigit() and int(b) >= 5000:
        ss["max_price_usd"] = int(b)
    rooms = sorted({int(r) for r in (ans.get("rooms") or []) if str(r).isdigit()})
    if 4 in rooms:
        rooms += [5, 6]
    ss["rooms"] = rooms
    ds = [DISTRICT_LIST[int(i)] for i in (ans.get("districts") or [])
          if str(i).isdigit() and int(i) < len(DISTRICT_LIST)]
    if ds:
        ss["districts"] = ds
    elif ans.get("districts_any"):              # клиент сказал «любой район» — весь Ташкент
        ss["districts"] = list(DISTRICT_LIST)
    ss["mortgage"] = "ипотек" in str(ans.get("note") or "").lower() or ans.get("payment") == "mortgage"
    ss["enabled"] = True
    return {**cfg, "sale_search": ss}


def harvest_sale_brokers(cfg, store) -> int:
    """Пополняет базу маклерами по ПРОДАЖЕ. Возвращает, сколько контактов обновлено.

    Маклер — телефон, который встречается в 2+ объявлениях о продаже, или
    объявление со словами агентства/риелтора; на Uybor — продавец с 3+ объявлениями."""
    ss = cfg.get("sale_search") or {}
    n = 0
    seen = store.get_kv("sale_broker_posts") or {}          # телефон → ключи объявлений
    # --- Telegram-каналы
    chans = ss.get("broker_channels") or SALE_BROKER_CHANNELS
    try:
        posts = fetch_telegram({"channels": chans}, cfg)
    except Exception as e:
        log.warning("[маклеры продажи] Telegram: %s", e)
        posts = []
    for l in posts:
        if not _sale_post(l.get("text")):
            continue
        broker_words = any(w in l["text"].lower() for w in BROKER_POST_WORDS)
        for ph in (l.get("phones") or [])[:2]:
            keys = seen.setdefault(ph, [])
            if l["key"] not in keys:
                keys.append(l["key"])
                del keys[:-50]
            if len(keys) >= 2 or broker_words:
                store.upsert_broker(f"tel:{ph}", l["source"], "", ph, len(keys),
                                    l.get("district"), None, deal="sale")
                n += 1
    if len(seen) > 5000:                                     # не раздуваем kv
        seen = dict(sorted(seen.items(), key=lambda kv: -len(kv[1]))[:3000])
    store.set_kv("sale_broker_posts", seen)
    # --- Uybor: свежие объявления о продаже по всему Ташкенту
    u = ss.get("uybor") or {}
    items = []
    for page in range(ss.get("broker_uybor_pages", 3)):     # телефон в описании — редкость, берём шире
        try:
            r = requests.get(UYBOR_API, params={
                "limit": 100, "offset": page * 100, "operationType__eq": "sale",
                "category__eq": u.get("category_id", 7), "region__eq": u.get("region_id", 13),
                "sort": "-createdAt"}, headers=HEADERS, timeout=25)
            r.raise_for_status()
            items += [uybor_listing(o) for o in r.json().get("results", [])]
        except Exception as e:
            log.warning("[маклеры продажи] Uybor: %s", e)
            break
    for l in items:
        if not l.get("phones"):
            continue
        uid = (l.get("seller_id") or "").partition(":")[2]
        ads = uybor_user_ads(uid, store, cfg)
        low = (l.get("text") or "").lower()
        if ads >= 3 or any(w in low for w in BROKER_POST_WORDS):
            store.upsert_broker(l["seller_id"], "Uybor · продажа", "", l["phones"][0],
                                max(ads, 1), l.get("district"), None, deal="sale")
            n += 1
    if n:
        log.info("[маклеры продажи] обновлено контактов: %d", n)
    return n


# ------------------------------- маклеры с площадок, где контакт открыт ----
# Realt24: API отдаёт телефон и флаг isCommissioned (с комиссией = посредник).
# Joymee: фильтр advertiser_type=2 («агентство/посредник»), телефон — в карточке.
# Realting: у агентств открыт Telegram (телефоны на сайте зашифрованы — не трогаем).
# OLX и Birbir закрыты защитой от ботов (403 даже с домашнего IP) — не обходим.
# Yangiuylar — каталог застройщиков, маклеров там нет.
REALT24_API = "https://api.realt24.uz/api/properties"
REALT24_Q = {"sale": "categoryIds=1&categoryType=sale&subCategoryIds=4%2C6",
             "rent": "categoryIds=21&categoryType=rent&subCategoryIds=24"}
JOYMEE_API = "https://api.joymee.uz/api/v1/announcement/"
JOYMEE_Q = {"sale": {"deal_type": 3, "category": 8}, "rent": {"deal_type": 2, "category": 4}}
JOYMEE_TASHKENT = 59          # region id «Toshkent shahri»
JOYMEE_AGENT = 2              # advertiser_type: 1 — собственник, 2 — агентство/посредник


def harvest_realt24(store, deal, pages=2) -> int:
    items = []
    for page in range(1, pages + 1):
        r = requests.get(f"{REALT24_API}?{REALT24_Q[deal]}&currency=usd&sortBy=dateDesc"
                         f"&page={page}&perPage=100", headers=HEADERS, timeout=25)
        r.raise_for_status()
        d = r.json()
        items += d.get("data") or []
        if not (d.get("meta") or {}).get("hasNext"):
            break
        time.sleep(0.5)
    rows = []
    for it in items:
        addr = (((it.get("address") or {}).get("fullAddress") or {}).get("ru") or "")
        ph = extract_phones(str(it.get("phone") or ""))
        if ph and addr.startswith("Ташкент"):
            rows.append((it, ph[0], addr))
    per_phone = {}
    for _, ph, _ in rows:
        per_phone[ph] = per_phone.get(ph, 0) + 1
    n = 0
    for it, ph, addr in rows:
        if not (it.get("isCommissioned") or per_phone[ph] >= 2):
            continue
        u = it.get("propertyUser") or {}
        name = " ".join(x.strip() for x in (u.get("firstName") or "", u.get("lastName") or "") if x).strip()
        store.upsert_broker(f"tel:{ph}", "Realt24", name[:60], ph, per_phone[ph],
                            canon_district(addr), None, deal=deal)
        n += 1
    return n


def harvest_joymee(store, deal, pages=3) -> int:
    known = store.get_kv("joymee_agents") or {}          # id продавца → телефон
    counts, fresh = {}, []
    for page in range(1, pages + 1):
        params = dict(JOYMEE_Q[deal], region=JOYMEE_TASHKENT, advertiser_type=JOYMEE_AGENT, page=page)
        r = requests.get(JOYMEE_API, params=params, headers=HEADERS, timeout=25)
        r.raise_for_status()
        d = r.json()
        for x in d.get("results") or []:
            sid = str((x.get("created_by") or {}).get("id") or "")
            if not sid:
                continue
            counts[sid] = counts.get(sid, 0) + 1
            if sid not in known and all(sid != f[0] for f in fresh):
                fresh.append((sid, x))
        if not d.get("next"):
            break
        time.sleep(0.5)
    n = 0
    for sid, x in fresh[:30]:                             # телефон — отдельным запросом карточки
        try:
            r = requests.get(f"{JOYMEE_API}{x['id']}/", headers=HEADERS, timeout=20)
            r.raise_for_status()
            det = r.json()
        except (requests.RequestException, ValueError) as e:
            log.info("[маклеры] Joymee %s: %s", x.get("id"), e)
            continue
        ph = extract_phones(str(det.get("phone_number") or ""))
        agent = bool(ph) and det.get("advertiser_type") == JOYMEE_AGENT
        known[sid] = ph[0] if agent else ""             # хозяина запоминаем пустым — не маклер
        if not agent:
            continue
        seller = det.get("seller") or {}
        name = " ".join(v for v in (seller.get("first_name"), seller.get("last_name")) if v)
        dist = (det.get("district") or {}).get("name") if isinstance(det.get("district"), dict) else ""
        store.upsert_broker(f"joymee:{sid}", "Joymee", name[:60], ph[0], counts.get(sid, 1),
                            canon_district(dist or ""), None, deal=deal)
        n += 1
        time.sleep(0.4)
    for sid, ph in known.items():                         # знакомым — только обновить счётчик
        if ph and sid in counts:
            store.upsert_broker(f"joymee:{sid}", "Joymee", "", ph, counts[sid], None, None, deal=deal)
    store.set_kv("joymee_agents", known)
    return n


REALTING_AGENCIES = "https://realting.uz/agencies"
REALTING_TG_RE = re.compile(r'href="https://(?:telegram\.me|t\.me)/([A-Za-z][A-Za-z0-9_]{3,31})[?"]')
REALTING_SKIP = {"realting_uz_news", "realtinguzloginbot", "share"}


def parse_realting_agencies(page: str) -> list:
    """Карточки агентств: id, название, город, число объектов, Telegram.
    Телефоны на странице зашифрованы и раскрываются скриптом сайта по клику — их не трогаем."""
    out = []
    parts = page.split('class="teaser-company')[1:]
    for part in parts:
        mid = re.search(r'data-id="(\d+)"', part)
        name = re.search(r'<div class="title">\s*<a [^>]*>([^<]+)</a>', part)
        addr = re.search(r'<div class="address">([^<]+)</div>', part)
        units = sum(int(x) for x in re.findall(r'class="unit-item"[^>]*>.*?<span>(\d+)</span>', part, re.S))
        tg = next((u for u in REALTING_TG_RE.findall(part) if u.lower() not in REALTING_SKIP), "")
        if mid:
            out.append({"id": mid.group(1), "name": html_lib.unescape(name.group(1)).strip() if name else "",
                        "city": html_lib.unescape(addr.group(1)).strip() if addr else "",
                        "objects": units, "tg": tg})
    return out


def harvest_realting(store, pages=3) -> int:
    """Агентства Realting по кругу — по несколько страниц за проход."""
    start = store.get_kv("realting_page") or 1
    n, page = 0, start
    for page in range(start, start + pages):
        r = requests.get(REALTING_AGENCIES, params={"page": page}, headers=HEADERS, timeout=25)
        r.raise_for_status()
        cards = parse_realting_agencies(r.text)
        if not cards:                                  # каталог кончился — в следующий раз с начала
            page = 0
            break
        for c in cards:
            if c["tg"] and "Ташкент" in c["city"]:
                store.upsert_broker(f"realting:{c['id']}", "Realting", c["name"][:60], None,
                                    c["objects"], None, None, deal="sale", tg=c["tg"])
                n += 1
        time.sleep(1)
    store.set_kv("realting_page", page + 1)
    return n


def harvest_market_brokers(cfg, store) -> int:
    """Маклеры по аренде и продаже с Realt24 и Joymee."""
    n = 0
    for deal in ("sale", "rent"):
        for name, fn in (("Realt24", harvest_realt24), ("Joymee", harvest_joymee)):
            try:
                k = fn(store, deal)
                n += k
                if k:
                    log.info("[маклеры] %s · %s: %d", name, "продажа" if deal == "sale" else "аренда", k)
            except Exception as e:                        # одна площадка не роняет остальные
                log.warning("[маклеры] %s · %s: %s", name, deal, e)
    try:                                                  # агентства Realting — продажа
        k = harvest_realting(store)
        n += k
        if k:
            log.info("[маклеры] Realting · агентства: %d", k)
    except Exception as e:
        log.warning("[маклеры] Realting: %s", e)
    return n


def backfill_sale_brokers(store, sale_store) -> int:
    """Разово: маклеры из уже собранных объявлений о продаже (sale.db)."""
    if sale_store is None or store.get_kv("sale_brokers_backfilled"):
        return 0
    n = 0
    for (data,) in sale_store.conn.execute("SELECT data FROM listings WHERE data IS NOT NULL"):
        try:
            l = json.loads(data)
        except (TypeError, ValueError):
            continue
        phones = l.get("phones") or []
        if phones and (l.get("seller_kind") == "agency" or (l.get("seller_ads") or 0) >= 3):
            store.upsert_broker(l.get("seller_id") or f"tel:{phones[0]}", "Uybor · продажа", "",
                                phones[0], max(l.get("seller_ads") or 0, 1), l.get("district"),
                                None, deal="sale")
            n += 1
    store.set_kv("sale_brokers_backfilled", True)
    log.info("[маклеры продажи] из базы продаж добавлено: %d", n)
    return n


def outreach_text(cfg, settings) -> str:
    """Запрос маклеру, собранный из ваших текущих фильтров."""
    eff = effective_cfg(cfg, settings)
    parts = ["Здравствуйте! Ищу квартиру в долгосрочную аренду в Ташкенте."]
    what = []
    rmin, rmax = settings.get("rooms_min"), settings.get("rooms_max")
    if rmin:
        what.append(f"{rmin}–{rmax} комнаты" if rmax and rmax != rmin else f"{rmin} комнаты")
    ds = settings.get("districts") or []
    if ds:
        what.append("районы: " + ", ".join(ds))
    lo = eff.get("min_price_usd") or 0
    what.append(f"бюджет до ${eff['max_price_usd']}" if not lo
                else f"бюджет ${lo}–{eff['max_price_usd']}")
    parts.append("Параметры: " + "; ".join(what) + ".")
    if settings.get("segment") == "premium":
        parts.append("Интересует новый ЖК с хорошим (авторским) ремонтом, "
                     "меблированная, желательно не первый и не последний этаж.")
    parts.append("Если есть подходящие варианты — пришлите, пожалуйста, фото, "
                 "точный адрес, этаж, площадь и цену.")
    parts.append("Сразу уточните, пожалуйста, размер комиссии. Спасибо!")
    return "\n".join(parts)


def wa_link(phone: str, text: str) -> str:
    from urllib.parse import quote
    digits = re.sub(r"[^\d]", "", phone or "")
    if len(digits) == 9:
        digits = "998" + digits
    return f"https://wa.me/{digits}?text={quote(text)}"


def tg_user_link(username: str, text: str) -> str:
    """Чат с пользователем Telegram с уже набранным текстом (как у Realting)."""
    from urllib.parse import quote
    return f"https://t.me/{username.lstrip('@')}?text={quote(text)}"


def tg_phone_link(phone: str) -> str:
    digits = re.sub(r"[^\d]", "", phone or "")
    if len(digits) == 9:
        digits = "998" + digits
    return f"https://t.me/+{digits}"


def request_deal(store) -> str:
    """Под какую сделку подбирать маклеров: покупка → продающие, иначе — арендные."""
    ans = (store.get_kv("anketa") or {}).get("ans") or {}
    return "sale" if ans.get("deal") == "buy" else "rent"


def rent_search_on(store) -> bool:
    """Карточки аренды — только когда клиент ищет аренду. Ищет покупку (или параметры ещё не заданы) —
    объявления аренды молча запоминаем: из них собираем маклеров и рынок, но в чат не шлём."""
    if store.get_kv("fresh_start"):
        return False
    ans = (store.get_kv("anketa") or {}).get("ans") or {}
    return ans.get("deal", "rent") == "rent"


CENTRAL_DISTRICTS = {"Мирабад", "Яккасарай", "Шайхантахур", "Юнусабад"}


def request_districts(store) -> set:
    """Районы из запроса; «ближе к центру» без районов — центральные."""
    ans = (store.get_kv("anketa") or {}).get("ans") or {}
    ds = {DISTRICT_LIST[int(i)] for i in (ans.get("districts") or []) if str(i).isdigit()
          and int(i) < len(DISTRICT_LIST)}
    if not ds and "центр" in str(ans.get("note") or "").lower():
        ds = set(CENTRAL_DISTRICTS)
    return ds


def ranked_brokers(store, deal, limit=200):
    """Сначала те, кто работает в нужных районах, потом — у кого больше объявлений."""
    want = request_districts(store)
    pool = store.brokers(status="new", with_phone=True, limit=limit, deal=deal)
    return sorted(pool, key=lambda b: (-len(want & set(b["districts"])), -(b["ads"] or 0)))


def _kind(deal):
    return "по продаже" if deal == "sale" else "по аренде"


def outreach_empty_text(store, deal) -> str:
    total, withph, _ = store.broker_stats(deal)
    return (f"📇 Новых маклеров {_kind(deal)} с контактом пока нет.\n"
            f"Всего в базе {_kind(deal)}: {total} (с контактом {withph}).\n"
            + ("Собираю их с Realt24, Joymee, Realting, Uybor и из Telegram-каналов — "
               "загляните через час — кнопка «📇 Через маклеров»" if deal == "sale" else
               "База пополняется по мере работы радара — попробуйте позже."))


def outreach_header(store, deal, text, n) -> str:
    _, _, by_status = store.broker_stats(deal)
    want = request_districts(store)
    return (f"📇 <b>Рассылка маклерам {_kind(deal)}</b> — в очереди {n}\n"
            + (f"Сначала те, кто работает в районах: {escape_html(', '.join(sorted(want)))}.\n" if want else "")
            + f"Уже написано раньше: {by_status.get('contacted', 0)}.\n\n"
            "Текст запроса:\n"
            f"<code>{escape_html(text)}</code>\n\n"
            "Покажу маклеров по одному: жмите «WhatsApp» или «Telegram» — откроется чат с "
            "набранным текстом, отправьте и нажмите «✅ Отправил → следующий».")


def broker_card(b, deal, text):
    """(текст карточки без строки прогресса, первая строка кнопок со ссылками)."""
    d = ", ".join(b["districts"][:3]) or "—"
    price = ""
    if deal == "rent" and b["min_price"] and b["max_price"]:
        price = f" · ${b['min_price']:.0f}–{b['max_price']:.0f}"
    phone = b["phone"] or ""
    contact = (f"📞 {escape_html(fmt_phone(phone) if len(phone) == 9 else phone)}" if phone
               else f"✈️ @{escape_html(b['tg'])}")
    body = (f"📇 <b>{escape_html(b['name'] or 'Маклер')}</b> · {escape_html(b['source'])}\n"
            f"{contact}\n"
            f"🏘 {b['ads']} {concierge.plural(b['ads'] or 0, 'объявление', 'объявления', 'объявлений')}" + (f" · районы: {escape_html(d)}" if b["districts"] else "") + price)
    row = ([{"text": "📱 WhatsApp с текстом", "url": wa_link(phone, text)},
            {"text": "✈️ Telegram", "url": tg_phone_link(phone)}] if phone else
           [{"text": "✈️ Telegram с текстом", "url": tg_user_link(b["tg"], text)}])
    return body, row


def outreach_progress(st, left) -> str:
    return (f"\n\n<i>Написано {st.get('sent', 0)} · пропущено {st.get('skipped', 0)} · "
            f"в очереди ещё {left}</i>")


def send_broker_cards(cfg, store, settings, limit=10, text=None, deal=None) -> str:
    """Рассылка маклерам — по одному: карточка, «Отправил → следующий», прогресс."""
    text = text or store.get_kv("request_text") or outreach_text(cfg, settings)
    deal = deal or request_deal(store)
    pool = ranked_brokers(store, deal)
    if not pool:
        send_telegram(cfg, outreach_empty_text(store, deal))
        return ""
    store.set_kv("outreach", {"deal": deal, "text": text, "sent": 0, "skipped": 0})
    send_telegram(cfg, outreach_header(store, deal, text, len(pool)))
    send_next_broker(cfg, store)
    return ""


def send_next_broker(cfg, store) -> bool:
    """Следующая карточка маклера в рассылке."""
    st = store.get_kv("outreach") or {}
    deal = st.get("deal") or request_deal(store)
    text = st.get("text") or store.get_kv("request_text") or ""
    pool = ranked_brokers(store, deal)
    if not pool:
        send_telegram(cfg, f"✅ Всех прошли: написали {st.get('sent', 0)}, "
                           f"пропустили {st.get('skipped', 0)}. Новые маклеры появляются сами — "
                           "загляните через пару часов в «📇 Через маклеров», подкину ещё.")
        return False
    b = pool[0]
    body, first_row = broker_card(b, deal, text)
    kb = {"inline_keyboard": [
        first_row,
        [{"text": "✅ Отправил → следующий", "callback_data": f"bw:{b['bid']}"},
         {"text": "⏭ Пропустить", "callback_data": f"bx:{b['bid']}"}],
    ]}
    tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "text": body + outreach_progress(st, len(pool) - 1),
        "parse_mode": "HTML", "reply_markup": json.dumps(kb, ensure_ascii=False)})
    return True


# ------------------------------- снимок для воркера: «Варианты» и «Маклерам» сразу ----
# Python спит большую часть суток, а кнопки должны отвечать за секунды. Поэтому
# Python заранее отдаёт воркеру готовые карточки (варианты, очередь маклеров, текст
# запроса), а воркер показывает их сам. Нажатия потом доходят до Python и сохраняются.
SNAPSHOT_EVERY = 15


def show_site_listing(cfg, key) -> str:
    """Полная карточка объявления с сайта: фото, описание, разбор цены, 👍 / Мимо."""
    if not SALE_DB_PATH.exists():
        return "Объявление не найдено"
    sst = Store(SALE_DB_PATH)
    try:
        row = sst.conn.execute("SELECT data FROM listings WHERE key LIKE ?",
                               (key.replace("%", "") + "%",)).fetchone()
        if not row:
            return "Объявление не найдено"
        l = json.loads(row[0] or "{}")
        fresh = sale_sources.refresh_listing(l)      # у Joymee ссылки на фото живут 10 минут
        if sale_sources.photo_repair(cfg, l) or fresh:
            sst.conn.execute("UPDATE listings SET data=? WHERE key=?", (sst.pack(l), l["key"]))
            sst.conn.commit()
        ids = send_listing(cfg, {"photos": True}, l, False, text=format_sale_message(l, cfg))
        if not ids:
            return "Не получилось отправить 🙈"
        send_sale_analysis(cfg, sst, l, kb=sale_kb(l["key"], ids))
    finally:
        sst.conn.close()
    return "" if l.get("photo_urls") else "Фото у этого объявления нет — только описание"


LIKE_WORDS = re.compile(r"похож|такие же|такую же|подобн|аналог|o'?xshash|ўхшаш|shunga o", re.I)


def link_kb(key, ids):
    kb = sale_kb(key, ids)
    kb["inline_keyboard"].append([{"text": "🔎 Похожие", "callback_data": f"L:sim:{key}"[:64]},
                                  {"text": "🎯 Искать такие", "callback_data": f"L:like:{key}"[:64]}])
    return kb


def handle_link(cfg, store, msg):
    """Владелец прислал ссылку на объявление: открыть, разобрать как карточку с анализом,
    сказать, видели ли эту квартиру раньше (и у кого), по просьбе — показать похожие."""
    url = msg["_link"]
    note = (msg.get("text") or "").replace(url, " ").strip()
    sst = Store(SALE_DB_PATH)
    try:
        try:
            l, deal = sale_sources.listing_from_url(cfg, url)
        except Exception as e:
            log.warning("ссылка %s: %s", url[:80], e)
            l, deal = None, "сайт не ответил"
        if not l:
            olx = "olx.uz" in url.lower()
            send_telegram(cfg, f"🙈 Не получилось открыть объявление — {escape_html(deal)}."
                          + (" OLX закрыт для программ, даже для меня." if olx else "")
                          + "\nПришлите, пожалуйста, скриншот объявления (цена, параметры, фото) — разберу по нему.")
            return
        if deal in ("rent", "daily"):
            handle_rent_link(cfg, store, sst, l, note)
            return
        sale_sources.normalize(l, cfg)
        l["price_usd"] = to_usd(l.get("price_value"), l.get("price_currency"), cfg)
        ss = effective_sale_cfg(cfg, store).get("sale_search") or {}
        try:
            classify_sale_seller(l, ss, sst, cfg)
        except Exception as e:
            log.info("продавец по ссылке не определён: %s", e)
        sale_sources.photo_repair(cfg, l)
        sc, why, _, _ = sale_sources.score(sst, l, cfg, ss)
        l["score"], l["why"] = sc, why
        before = sale_sources.seen_before(sst, l)
        if sst.known(l["key"]):
            sst.conn.execute("UPDATE listings SET data=?, notified=1 WHERE key=?", (sst.pack(l), l["key"]))
            sst.conn.commit()
        else:
            sst.save(l, notified=True)
        text = format_sale_message(l, cfg)
        if before:
            kinds = {"owner": "собственник", "agency": "маклер"}
            first = before[0]
            when = parse_iso(first.get("_first_seen") or "")
            text += ("\n\n👀 <b>Эту квартиру я уже видела</b>: " + ", ".join(
                f'<a href="{o.get("url")}">{escape_html(o.get("site") or "Uybor")}</a> — ${_money(o.get("price_usd") or 0)}'
                + (f' ({kinds[o["seller_kind"]]})' if o.get("seller_kind") in kinds else "") for o in before)
                + (f'. Впервые — {when.astimezone(TASHKENT_TZ).strftime("%d.%m %H:%M")}' if when else ""))
        if not l.get("price_usd"):
            text += "\n\n⚠️ Цену на странице не нашла — разбор неполный."
        ids = send_listing(cfg, {"photos": True}, l, False, text=text)
        send_sale_analysis(cfg, sst, l, kb=link_kb(l["key"], ids))
        if LIKE_WORDS.search(note):
            send_similar(cfg, sst, l)
    finally:
        sst.conn.close()


def rent_key(l):
    k = l.get("key") or ""
    return k[5:] if k.startswith("sale:") else k                # аренда хранится в radar.db без «sale:»


def handle_rent_link(cfg, store, sst, l, note=""):
    """Ссылка на аренду: карточка с оценкой цены против похожих в аренде, «уже видела», похожие, «искать такие»."""
    l = dict(l, key=rent_key(l), source=l.get("site") or "по ссылке")
    l["price_usd"] = to_usd(l.get("price_value"), l.get("price_currency"), cfg)
    try:
        analyst.score_listing(l, store, cfg, analyst.market_stats(store), store.get_kv("settings") or {})
    except Exception as e:
        log.info("оценка аренды по ссылке: %s", e)
    if store.known(l["key"]):
        store.conn.execute("UPDATE listings SET data=?, notified=1 WHERE key=?", (store.pack(l), l["key"]))
        store.conn.commit()
    else:
        store.save(l, notified=True)
    text = format_message(l, cfg, l.get("is_business") is True)
    sim = sale_sources.similar_rent(store, sst, l)
    if sim:
        med = sorted(o["price_usd"] for o in sim)[len(sim) // 2]
        if l.get("price_usd"):
            gap = l["price_usd"] / med - 1
            text += (f"\n\n📊 Похожие ({len(sim)}) сдают в среднем за ~${_money(med)}/мес — "
                     + (f"эта на {abs(gap) * 100:.0f}% {'дешевле' if gap < 0 else 'дороже'}" if abs(gap) >= 0.03 else "эта в рынке"))
    ids = send_listing(cfg, {"photos": True}, l, False, text=text)
    kb = {"inline_keyboard": [[{"text": "🔎 Похожие", "callback_data": f"L:sim:{l['key']}"[:64]},
                               {"text": "🎯 Искать такие", "callback_data": f"L:like:{l['key']}"[:64]}]]}
    tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": "Что делаем с этой арендой?",
                                 "reply_markup": json.dumps(kb, ensure_ascii=False)})
    if LIKE_WORDS.search(note or ""):
        send_similar_rent(cfg, store, sst, l)


def send_similar_rent(cfg, store, sst, l) -> int:
    found = sale_sources.similar_rent(store, sst, l)
    if not found:
        send_telegram(cfg, "🔎 Похожей аренды за последний месяц не нашла. Нажмите «🎯 Искать такие» — буду ловить новые.")
        return 0
    lines = [f"🔎 <b>Похожая аренда</b> · ±20% к цене — {len(found)} шт., дешёвые сверху", ""]
    for i, o in enumerate(found, 1):
        bits = [f"{o['rooms']}к" if o.get("rooms") else "", f"{o['area']:g} м²" if o.get("area") else "",
                o.get("district") or ""]
        lines.append(f'{i}. <b>${_money(o["price_usd"])}</b>/мес · ' + " · ".join(b for b in bits if b)
                     + f' · <a href="{o["url"]}">{escape_html(o.get("site") or o.get("source") or "")}</a>')
    tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": "\n".join(lines)[:4000],
                                 "parse_mode": "HTML", "disable_web_page_preview": True})
    return len(found)


def send_similar(cfg, sst, l) -> int:
    found = sale_sources.similar(sst, l)
    if not found:
        send_telegram(cfg, "🔎 Похожих за последние полтора месяца не нашла. Нажмите «🎯 Искать такие» — "
                           "буду ловить новые и пришлю, как только появятся.")
        return 0
    for o in found:                       # чтобы 📷/👍 по номеру работали и для объявлений из среза Uybor
        if not sst.known(o["key"]):
            sst.save({**o, "text": o.get("text") or ""}, notified=True)
    top = [{"key": o["key"], "line": sale_sources.pick_line(o, o.get("why") or [])} for o in found]
    where = ", ".join(x for x in (f'{l["rooms"]}-комн' if l.get("rooms") else "", l.get("district") or "") if x)
    text, kb = sale_sources._pick_message(
        top, f"🔎 <b>Похожие</b>{' · ' + escape_html(where) if where else ''} · ±20% к цене — {len(top)} шт., "
             "дешёвые за м² сверху")
    tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML",
                                 "disable_web_page_preview": True, "reply_markup": json.dumps(kb, ensure_ascii=False)})
    sst.set_kv("last_pick", top)
    return len(top)


def search_like(cfg, store, l) -> str:
    """«🎯 Искать такие»: условия поиска — по этому объявлению (комнаты, район, цена +10%)."""
    ans = dict((concierge.get_anketa(store).get("ans") or {}))
    rent = not str(l.get("key") or "").startswith("sale:")
    ans.update(deal="rent" if rent else "buy", object="flat", city="tashkent")
    if l.get("rooms"):
        ans["rooms"] = [str(l["rooms"])]
    if l.get("district") in DISTRICT_LIST:
        ans["districts"] = [str(DISTRICT_LIST.index(l["district"]))]
        ans.pop("districts_any", None)
    if l.get("price_usd"):
        ans["budget"] = str(int(round(l["price_usd"] * 1.1, -1 if rent else -3)))
    concierge.apply_webapp_data(cfg, store, json.dumps({"v": 3, "replace": True, "src": "link", "ans": ans}))
    if rent:                                   # фильтры радара аренды — по тем же условиям
        store.set_kv("settings", concierge.rent_settings(ans, store.get_kv("settings") or default_settings()))
    bits = [f'{l["rooms"]}-комн' if l.get("rooms") else "", l.get("district") or "",
            f'до ${_money(int(ans["budget"]))}' if ans.get("budget") else ""]
    send_telegram(cfg, "🎯 <b>Ищу такие же</b>: " + escape_html(" · ".join(b for b in bits if b))
                  + "\nУже пробегаюсь по сайтам — свежие от собственников пришлю сразу 🔄")
    return ""


def _plain(html_text) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", html_text or "")).strip()


def chat_context(cfg, store) -> dict:
    """Что сейчас есть у бота — для разговора: подборка с номерами, шортлист, счётчики.
    Модель опирается только на это и не выдумывает, что показала."""
    ctx = {"fresh_start": bool(store.get_kv("fresh_start"))}
    try:
        ss = (effective_sale_cfg(cfg, store).get("sale_search") or {})
        ctx["site_search"] = sale_criteria_text(ss).split("\n")[0] if ss.get("enabled") else "выключен"
    except Exception:
        pass
    if SALE_DB_PATH.exists():
        sst = Store(SALE_DB_PATH)
        try:
            day = sale_sources.day_stats(sst)
            ctx["today"] = {k: day.get(k, 0) for k in ("seen", "fit", "instant", "picked")}
            ctx["pick_pending"] = len(sale_sources.pick_pending(sst))
            ctx["last_pick"] = [{"n": i, "key": x["key"], "text": _plain(x["line"])[:140]}
                                for i, x in enumerate(sst.get_kv("last_pick") or [], 1)]
            ctx["shown_today"] = [{"key": x["key"], "text": _plain(x["line"])[:140]}
                                  for x in (sst.get_kv("shown_recent") or [])[-5:]]
        finally:
            sst.conn.close()
    _, rows, _ = concierge.shortlist_items(store, cfg, "n")
    ctx["shortlist"] = [{"n": i, "oid": r["oid"], "text": _plain(r["line"])[:120], "stage": r["stage"]}
                        for i, r in enumerate(rows[:10], 1)]
    q = lambda sql: store.conn.execute(sql).fetchone()[0]
    ctx["broker_offers_new"] = q("SELECT COUNT(*) FROM broker_offers WHERE status IN ('new','later')")
    ctx["brokers_written"] = q("SELECT COUNT(*) FROM brokers WHERE status='contacted'")
    return ctx


SRC_LABEL = {"realt24": "Realt24", "joymee": "Joymee", "realting": "Realting",
             "yangiuylar": "Yangiuylar", "telegram": "Telegram-каналы"}


def rano_screen(cfg, store) -> dict:
    """«🔎 Ищет Ra'no»: что ищу сама, где, что нашла сегодня, что ждёт в подборке."""
    if store.get_kv("fresh_start"):
        return {"text": "🔎 <b>Ищет Ra'no</b> — сама смотрю сайты и каналы, без маклеров\n\n"
                        "Сначала расскажите, что ищем, — например: «купить двушку в центре до $50 000, "
                        "нужна ипотека». Как только соберу параметры — побегу по всем сайтам 🏃‍♀️",
                "kb": {"inline_keyboard": [[{"text": "✏️ Рассказать, что ищу", "callback_data": "cmd:/mysearch"}]]}}
    eff = effective_sale_cfg(cfg, store)
    ss = eff.get("sale_search") or {}
    if not ss.get("enabled"):
        srcs = ", ".join(n for n, x in (cfg.get("sources") or {}).items() if x.get("enabled")) or "—"
        return {"text": "🔎 <b>Ищет Ra'no</b> — сама смотрю объявления, без маклеров\n\n"
                        f"Сейчас слежу за арендой: {escape_html(srcs)}. Подходящие присылаю сразу.\n"
                        "Поиск покупки по всем сайтам включается, когда в чате вы ищете купить квартиру.",
                "kb": {"inline_keyboard": [[{"text": "✏️ Изменить, что ищем", "callback_data": "cmd:/mysearch"}]]}}
    st, pend, sent_total, stats = {}, 0, 0, {}
    if SALE_DB_PATH.exists():
        sst = Store(SALE_DB_PATH)
        try:
            st = sale_sources.day_stats(sst)
            pend = len(sale_sources.pick_pending(sst))
            sent_total = sst.conn.execute("SELECT COUNT(*) FROM listings WHERE notified=1").fetchone()[0]
            stats = sst.get_kv("sale_src_stats") or {}
        finally:
            sst.conn.close()
    srcs = ["Uybor"] + [SRC_LABEL[k] + (" ⚠️" if (stats.get(k) or {}).get("err") else "")
                        for k in sale_sources.SOURCES if k not in (ss.get("sources_off") or [])]
    lines = ["🔎 <b>Ищет Ra'no</b> — сама смотрю сайты и каналы, без маклеров", "",
             "Ищу: " + escape_html(sale_criteria_text(ss).split("\n")[0])
             + (" · нужна ипотека" if ss.get("mortgage") else ""),
             "Где: " + ", ".join(srcs), "",
             f"Сегодня: новых объявлений {st.get('seen', 0)}, подошло {st.get('fit', 0)}",
             f"Прислала сразу: {st.get('instant', 0)} · в подборке ждут: {pend}",
             f"Всего прислала: {sent_total}", "",
             "Самые выгодные (ниже рынка, снижена цена) несу сразу. Остальные — подборкой в 19:30, лучшие сверху ✨"]
    rows = []
    if pend:
        rows.append([{"text": f"📬 Показать подборку сейчас ({pend})", "callback_data": "R:pick"}])
    rows.append([{"text": "🔄 Проверить сайты сейчас", "callback_data": "R:check"}])
    rows.append([{"text": "✏️ Изменить, что ищем", "callback_data": "cmd:/mysearch"},
                 {"text": "📊 Цены рынка", "callback_data": "cmd:/rynok"}])
    rows.append([{"text": "📋 Шортлист", "callback_data": "s:show"}])
    return {"text": "\n".join(lines), "kb": {"inline_keyboard": rows}}


def via_screen(cfg, store) -> dict:
    """«📇 Через маклеров»: запрос маклерам, их варианты, ожидание ответов."""
    q = lambda sql, *a: store.conn.execute(sql, a).fetchone()[0]
    written = q("SELECT COUNT(*) FROM brokers WHERE status='contacted'")
    got = q("SELECT COUNT(*) FROM broker_offers WHERE broker_chat NOT LIKE 'site:%' AND status != 'message'")
    pool = q("SELECT COUNT(*) FROM broker_offers WHERE status IN ('new','later')")
    sl = q("SELECT COUNT(*) FROM broker_offers WHERE status IN ('shortlist','asked')")
    waiting = q("SELECT COUNT(*) FROM broker_offers WHERE status='asked' AND replied_at IS NULL")
    lines = ["📇 <b>Через маклеров</b> — запрос уходит маклерам, варианты приходят сюда карточками", "",
             f"Маклерам написали: {written} · прислали вариантов: {got}",
             f"🏠 Ждут вашего решения: {pool} · 📋 в шортлисте: {sl}"]
    if waiting:
        lines.append(f"⏳ Ждём ответа на уточнение: {waiting}")
    lines += ["", "Пишите маклерам понемногу — 10–15 в день, и ваш номер не попадёт в спам 😉"]
    rows = []
    if pool:
        rows.append([{"text": f"🏠 Варианты ({pool})", "callback_data": "cmd:/offers"}])
    rows.append([{"text": "📨 Написать маклерам" if written else "📨 Разослать запрос маклерам",
                  "callback_data": "b"}])
    rows.append([{"text": "📋 Шортлист", "callback_data": "s:show"},
                 {"text": "💬 Вариант из WhatsApp", "callback_data": "cmd:/add"}])
    return {"text": "\n".join(lines), "kb": {"inline_keyboard": rows}}


def send_screen(cfg, scr):
    tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": scr["text"],
                                 "parse_mode": "HTML", "disable_web_page_preview": True,
                                 "reply_markup": json.dumps(scr["kb"], ensure_ascii=False)})


def ui_snapshot(cfg, store, settings) -> dict:
    pool = concierge.offers_by_status(store, "new") + concierge.offers_by_status(store, "later")
    idx = concierge.price_index(store)
    offers = [{"oid": o["oid"], "photos": (o["photos"] or [])[:4],
               "text": concierge.offer_card(store, cfg, o, idx=idx, pos=i + 1, total=len(pool))}
              for i, o in enumerate(pool[:12])]
    shortlist = len(concierge.offers_by_status(store, "shortlist")) + \
        len(concierge.offers_by_status(store, "asked"))
    written = store.conn.execute("SELECT COUNT(*) FROM brokers WHERE status='contacted'").fetchone()[0]
    deal = request_deal(store)
    text = store.get_kv("request_text") or outreach_text(cfg, settings)
    ranked = ranked_brokers(store, deal)
    brokers = []
    for b in ranked[:40]:
        body, row = broker_card(b, deal, text)
        brokers.append({"bid": b["bid"], "body": body, "row": row})
    # шортлист — во всех трёх сортировках; выделение и сортировку ведёт воркер
    sl = {}
    for srt in ("n", "p", "m"):
        title, rows, n_ask = concierge.shortlist_items(store, cfg, srt)
        sl[srt] = {"title": title, "items": rows, "askable": n_ask,
                   "sort_label": concierge.SORTS[srt][0]}
    cards = {}                                           # карточки вариантов шортлиста — по номеру сразу
    for r in sl["n"]["items"][:30]:
        o = concierge.get_offer(store, r["oid"])
        if o:
            ctext, ckb = concierge.offer_view(store, cfg, o, idx=idx)
            cards[str(r["oid"])] = {"text": ctext, "kb": ckb}
    eff = effective_sale_cfg(cfg, store)
    texts = {"/help": HELP_TEXT, "/sale": sale_status_text(eff)}
    rq = store.get_kv("request_text")
    texts["/request"] = (f"📝 <b>Текущий запрос маклерам</b>\n\n<code>{escape_html(rq)}</code>" if rq else
                         "Текст запроса ещё не готов — расскажите, что ищем, и я его соберу ✍️")
    mk = store.get_kv("rynok_cache") or {}
    if time.time() - mk.get("at", 0) > 600:              # сводка рынка тяжелее — раз в 10 минут
        try:
            mk = {"at": time.time(), "text": sale_market_text(eff)}
            store.set_kv("rynok_cache", mk)
        except Exception as e:
            log.info("сводка рынка для снимка: %s", e)
    if mk.get("text"):
        texts["/rynok"] = mk["text"]
    try:
        ctx = chat_context(cfg, store)
    except Exception as e:
        log.info("контекст разговора для снимка: %s", e)
        ctx = {}
    screens = {}
    for name, fn in (("/rano", rano_screen), ("/via", via_screen)):
        try:
            screens[name] = fn(cfg, store)
        except Exception as e:
            log.info("экран %s для снимка: %s", name, e)
    return {"offers": offers, "offers_total": len(pool), "shortlist": shortlist, "written": written,
            "sl": sl, "sl_empty": concierge.SL_EMPTY, "cards": cards, "texts": texts,
            "screens": screens, "ctx": ctx,
            "free": cfg.get("free_offers", 2), "deal": deal,
            "brokers": brokers, "brokers_total": len(ranked),
            "header": outreach_header(store, deal, text, len(ranked)) if ranked else "",
            "brokers_empty": outreach_empty_text(store, deal) if not ranked else ""}


def push_snapshot(cfg, store, settings, force=False) -> bool:
    if not cfg.get("worker_url"):
        return False
    now = time.time()
    last = store.get_kv("snapshot_meta") or {}
    if not force and now - last.get("at", 0) < SNAPSHOT_EVERY:
        return False
    try:
        snap = ui_snapshot(cfg, store, settings)
    except Exception as e:
        log.warning("снимок для воркера не собран: %s", e)
        return False
    sig = hashlib.sha1(json.dumps(snap, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if sig == last.get("sig") and now - last.get("at", 0) < 600:
        store.set_kv("snapshot_meta", {"at": now, "sig": sig, "pushed": last.get("pushed", 0)})
        return False
    try:
        r = requests.post(cfg["worker_url"] + "/svc/snapshot", json=snap, timeout=20,
                          headers={"x-svc": cfg.get("worker_key", ""), "user-agent": "rano-radar/1.0"})
        ok = r.status_code == 200
    except requests.RequestException as e:
        log.warning("снимок не отправлен: %s", e)
        ok = False
    store.set_kv("snapshot_meta", {"at": now, "sig": sig if ok else "", "pushed": now if ok else last.get("pushed", 0)})
    return ok


def run_search(cfg, store, settings, limit=5, days=7) -> str:
    """Анализирует всё, что накоплено, и присылает лучшее прямо сейчас."""
    stats = analyst.market_stats(store)
    pool = store.recent(days=days)
    ok = []
    for l in pool:
        try:
            if not passes_filters(l, effective_cfg(cfg, settings)):
                continue
            if not passes_user_filters(l, settings) or \
                    relevance_reject(l, cfg, settings, store):
                continue
            ok.append(analyst.score_listing(l, store, cfg, stats, settings))
        except Exception as e:
            log.warning("оценка %s не удалась: %s", l.get("key"), e)
    ok.sort(key=lambda x: x.get("score", 0), reverse=True)

    if not ok:
        send_telegram(cfg, f"🔍 За последние {days} дн. подходящих вариантов нет.\n"
                           f"Просмотрено {len(pool)}. Попробуйте ослабить фильтры в /menu.")
        return ""

    head = [f"🔎 <b>Лучшее сейчас</b> — {min(limit, len(ok))} из {len(ok)} подходящих "
            f"(просмотрено {len(pool)} за {days} дн.)"]
    med = stats["rooms"]
    if med:
        head.append("📊 Медианы: " + ", ".join(
            f"{r}-комн ${m:.0f}" for r, (m, n) in sorted(med.items())))
    send_telegram(cfg, "\n".join(head))

    for i, l in enumerate(ok[:limit], 1):
        l["title"] = f"#{i} · {l.get('title', '')}"
        send_listing(cfg, settings, l, likely_makler=False)
        time.sleep(1)
    return ""


def apply_worker_done(data, cfg, store):
    """Воркер сам провёл нажатие (рассылка по одному) — здесь только сохраняем статус."""
    kind, _, bid = data.partition(":")
    if kind in ("bw", "bx") and bid:
        store.broker_status(bid, "contacted" if kind == "bw" else "skipped")
        st = store.get_kv("outreach") or {}
        key = "sent" if kind == "bw" else "skipped"
        st[key] = st.get(key, 0) + 1
        store.set_kv("outreach", st)


def handle_callback(data: str, settings: dict, store, cfg: dict, message_id=None):
    """Возвращает (всплывающая_подсказка, view_для_перерисовки)."""
    d = data or ""
    if d.startswith("a:"):
        toast, _ = concierge.handle_anketa_cb(d, cfg, store, message_id)
        return toast, None
    if d.startswith("q:"):
        toast, _ = concierge.handle_request_cb(d, cfg, store, settings)
        return toast, None
    if d.startswith("t:"):
        toast, _ = concierge.handle_triage_cb(d, cfg, store)
        return toast, None
    if d.startswith("o:"):
        toast, _ = concierge.handle_offer_cb(d, cfg, store, message_id)
        return toast, None
    if d == "R:last":                          # «покажи подборку ещё раз»
        if not SALE_DB_PATH.exists():
            return "Подборок ещё не было", None
        sst = Store(SALE_DB_PATH)
        try:
            n = sale_sources.resend_last_pick(cfg, sst)
        finally:
            sst.conn.close()
        return ("" if n else "Подборок ещё не было — скоро будет 🙂"), None
    if d.startswith(("L:sim:", "L:like:")):    # похожие / искать такие же
        key = d.split(":", 2)[2]
        if not SALE_DB_PATH.exists():
            return "Объявление не найдено", None
        sst = Store(SALE_DB_PATH)
        try:
            rent = not key.startswith("sale:")
            src = store if rent else sst
            row = src.conn.execute("SELECT data FROM listings WHERE key LIKE ?",
                                   (key.replace("%", "") + "%",)).fetchone()
            if not row:
                return "Объявление не найдено", None
            l = json.loads(row[0] or "{}")
            if not l.get("price_usd"):
                l["price_usd"] = to_usd(l.get("price_value"), l.get("price_currency"), cfg)
            if d.startswith("L:sim:"):
                (send_similar_rent(cfg, store, sst, l) if rent else send_similar(cfg, sst, l))
                return "", None
        finally:
            sst.conn.close()
        return search_like(cfg, store, l), None
    if d.startswith("L:v:"):                   # фото и разбор объявления из подборки
        return show_site_listing(cfg, d[4:].split("|")[0]), None
    if d.startswith("L:"):                     # объявление с сайта: в шортлист / мимо
        kind, key = d[2:3], d[4:].split("|")[0]
        row = None
        if SALE_DB_PATH.exists():
            sst = Store(SALE_DB_PATH)
            try:
                row = sst.conn.execute("SELECT data FROM listings WHERE key LIKE ?",
                                       (key.replace("%", "") + "%",)).fetchone()
            finally:
                sst.conn.close()
        if not row:
            return "Объявление не найдено", None
        if kind == "n":                        # «мимо»: из подборки убрать, отметить — пригодится для оценки
            sst = Store(SALE_DB_PATH)
            try:
                sst.set_kv("sale_pick", [x for x in (sst.get_kv("sale_pick") or []) if x["key"] != key])
                gone = (sst.get_kv("sale_dismissed") or [])[-500:]
                if key not in gone:
                    sst.set_kv("sale_dismissed", gone + [key])
            finally:
                sst.conn.close()
            return "👎 Убрала — больше не покажу", None
        oid, new = concierge.add_site_offer(cfg, store, json.loads(row[0] or "{}"))
        return ("👍 В шортлисте — там уточнение, просмотр, заметки" if new else "Уже в шортлисте"), None
    if d == "R:pick":
        if not SALE_DB_PATH.exists():
            return "Подборка пуста", None
        sst = Store(SALE_DB_PATH)
        try:
            n = sale_sources.send_pick(cfg, sst, reason="Подборка")
        finally:
            sst.conn.close()
        return ("" if n else "Подборка пуста — новое пришлю, как найду"), None
    if d == "R:check":
        store.set_kv("sale_force", "button")
        return "Проверяю все сайты — 1–2 минуты", None
    if d.startswith("s:"):
        toast, done = concierge.handle_shortlist_cb(d, cfg, store, message_id)
        if done:
            return toast, None
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
    if act == "oo":
        settings["owner_only"] = not settings.get("owner_only", True)
        return ("Только хозяева" if settings["owner_only"]
                else "Показываю всех, включая маклеров"), "M"
    if act == "sg":
        settings["segment"] = "any" if settings.get("segment") == "premium" else "premium"
        return ("Только новые ЖК с ремонтом" if settings["segment"] == "premium"
                else "Любой класс жилья"), "M"
    if act == "sh":
        settings["exclude_shared"] = not settings.get("exclude_shared", True)
        return ("Подселение и койко-места скрыты" if settings["exclude_shared"]
                else "Показываю в том числе подселение"), "M"
    if act == "p":
        settings["photos"] = not settings.get("photos", True)
        return ("Фото включены" if settings["photos"] else "Фото выключены"), "M"
    if act == "z":
        settings["paused"] = not settings.get("paused")
        return ("Пауза" if settings["paused"] else "Продолжаю"), "M"
    if act == "f":
        run_search(cfg, store, settings)
        return "Подбираю лучшее…", None
    if act == "ank":
        concierge.send_app_button(cfg, store)
        return "Опишите поиск в чате", None
    if act == "sl":
        concierge.show_shortlist(cfg, store)
        return "Шортлист", None
    if act == "off":
        shown = concierge.show_offers(cfg, store)
        return (f"Показываю {shown}" if shown else "Новых вариантов нет"), None
    if act == "off2":                       # «показать ещё» — остальные варианты
        concierge.show_offers(cfg, store, batch=99)
        return "Показываю остальные", None
    if act == "b":
        send_broker_cards(cfg, store, settings)
        return "Готовлю рассылку…", None
    if act in ("bw", "bx") or data.startswith(("bw:", "bx:")):
        kind, _, bid = data.partition(":")
        store.broker_status(bid, "contacted" if kind == "bw" else "skipped")
        st = store.get_kv("outreach") or {}
        st["sent" if kind == "bw" else "skipped"] = st.get("sent" if kind == "bw" else "skipped", 0) + 1
        store.set_kv("outreach", st)
        if message_id:                       # кнопки у пройденной карточки больше не нужны
            tg_call(cfg, "editMessageReplyMarkup", {"chat_id": cfg["telegram_chat_id"],
                                                    "message_id": message_id,
                                                    "reply_markup": json.dumps({"inline_keyboard": []})},
                    quiet=True)
        send_next_broker(cfg, store)
        return ("✅ Отмечено" if kind == "bw" else "Пропущен"), None
    if act == "s":
        send_telegram(cfg, status_text(cfg, settings, store))
        return "Статус отправлен", None
    if act == "h":
        send_telegram(cfg, HELP_TEXT)
        return "Справка отправлена", None
    return "", None


def broker_ack(cfg) -> str:
    """Имя клиента маклерам не раскрываем — и незачем, и склонения ломаются.

    Двуязычно (ру + уз): часть маклеров пишет только на узбекском. Тот же текст — у воркера."""
    a = cfg.get("assistant_name", "Ra'no")
    return (f"Здравствуйте! Я {a}, ИИ-ассистент — ищу жильё для клиента и передаю ему варианты.\n"
            f"Rahmat, получила! 🙌 Если зацепит — вернусь с вопросами. "
            f"Есть ещё что-то по параметрам — присылайте.\n\n"
            f"Assalomu alaykum! Men {a}, AI-yordamchiman — mijoz uchun uy-joy qidiryapman. "
            f"Rahmat, qabul qildim! Mos kelsa, savollar bilan qaytaman.")


BROKER_QUIET_SECONDS = 40      # маклер замолчал — значит, вариант дописан, показываем


def _offer_parts(msg):
    text = (msg.get("text") or msg.get("caption") or "").strip()
    photos = []
    ph = msg.get("photo") or []
    if ph:                                  # берём самый крупный размер
        best = max(ph, key=lambda x: (x.get("width") or 0) * (x.get("height") or 0))
        if best.get("file_id"):
            photos.append(best["file_id"])
    return text, photos


def intake_offer(cfg, store, group, chat_key, name, text, photos, media_group=None):
    """Общий приём варианта: склейка частей от одного отправителя, отложенный показ.
    Возвращает (oid, новый_ли)."""
    pend = store.get_kv("pending_offers") or {}
    now = time.time()
    frag = next((int(k) for k, v in pend.items()
                 if v.get("group") == group and now - v.get("at", 0) < 180), None)
    if frag and not media_group:
        concierge.merge_into_offer(store, cfg, frag, text, photos)
        pend[str(frag)]["at"] = now
        store.set_kv("pending_offers", pend)
        return frag, False
    oid, is_new = concierge.save_offer(store, cfg, chat_key, name, text, photos, media_group)
    pend[str(oid)] = {"at": now, "group": group}
    store.set_kv("pending_offers", pend)
    return oid, is_new


def handle_broker_message(cfg, store, msg):
    """Маклер пишет боту напрямую — сохраняем вариант и показываем владельцу."""
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    name = " ".join(x for x in [chat.get("first_name"), chat.get("last_name")] if x) \
        or chat.get("username") or "маклер"
    text, photos = _offer_parts(msg)
    if not text and not photos:
        return

    if msg.get("_welcomed"):                 # воркер уже познакомил маклера с запросом
        store.set_kv(f"welcomed:{chat_id}", time.time())
        return
    # /start, «здравствуйте» — это не вариант: знакомим и показываем, что ищем
    if not photos and (text.startswith("/") or (len(text) < 25 and not re.search(r"\d", text))):
        last = store.get_kv(f"welcomed:{chat_id}") or 0
        if text.startswith("/") or time.time() - last > 6 * 3600:
            tg_call(cfg, "sendMessage", {"chat_id": chat_id,
                                         "text": concierge.broker_welcome(cfg, store)})
            store.set_kv(f"welcomed:{chat_id}", time.time())
        return

    # ответ на «уточните детали» — к своему варианту
    if not photos:
        asked = concierge.pending_answer(store, chat_id)
        if asked:
            concierge.attach_answer(cfg, store, asked, text)
            if not msg.get("_acked"):
                tg_call(cfg, "sendMessage", {"chat_id": chat_id,
                                             "text": "Rahmat, передала клиенту! 🙌 / Mijozga yetkazdim!"})
            return

    try:
        oid, is_new = intake_offer(cfg, store, f"chat:{chat_id}", chat_id, name, text, photos,
                                   msg.get("media_group_id"))
    except Exception as e:
        log.warning("не сохранил вариант от %s: %s", chat_id, e)
        return
    log.info("вариант #%s от %s (%s)", oid, name, "новый" if is_new else "дополнение")
    if is_new and not msg.get("_acked"):      # воркер уже ответил маклеру мгновенно
        tg_call(cfg, "sendMessage", {"chat_id": chat_id, "text": broker_ack(cfg)})


def forward_name(msg) -> str:
    """От кого пересланное сообщение (чат WhatsApp не перешлёшь — тогда «вручную»)."""
    fo = msg.get("forward_origin") or {}
    u = fo.get("sender_user") or msg.get("forward_from") or {}
    if u:
        return " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x) or u.get("username") or ""
    if fo.get("sender_user_name") or msg.get("forward_sender_name"):
        return fo.get("sender_user_name") or msg.get("forward_sender_name")
    c = fo.get("chat") or fo.get("sender_chat") or msg.get("forward_from_chat") or {}
    return c.get("title") or ""


def handle_owner_offer(cfg, store, msg):
    """Владелец переслал или вставил вариант (из WhatsApp, другого чата) — в карточки."""
    text, photos = _offer_parts(msg)
    if not text and not photos:
        return
    who = forward_name(msg) or "добавлено вручную"
    oid, is_new = intake_offer(cfg, store, f"owner:{who}", "owner", who, text, photos,
                               msg.get("media_group_id"))
    if is_new:
        send_telegram(cfg, f"📥 Приняла — вариант #{oid}. Есть ещё фото или текст — досылайте, "
                           "карточку соберу, как закончите.")


def worker_post(cfg, path, payload, timeout=60):
    try:
        r = requests.post(cfg["worker_url"] + path, json=payload, timeout=timeout,
                          headers={"x-svc": cfg.get("worker_key", ""), "user-agent": "rano-radar/1.0"})
        if r.status_code != 200:
            log.info("Воркер %s %s: %s", path, r.status_code, r.text[:200])
            return None
        return r.json()
    except (requests.RequestException, ValueError) as e:
        log.info("Воркер %s недоступен: %s", path, e)
        return None


def ai_parse(cfg, store, text, photos):
    """Разбор сообщения маклера моделью (текст + фото). None — модель недоступна, остаётся regex."""
    if not cfg.get("worker_url"):
        return None
    deal = request_deal(store)
    r = worker_post(cfg, "/svc/parse", {"text": text or "", "photos": photos or [], "deal": deal})
    return (r or {}).get("offer") if (r or {}).get("ok") else None


def flush_pending_offer(cfg, store, sale_store=None):
    """Показываем вариант, когда отправитель замолчал: альбом и текст дособраны."""
    pend = store.get_kv("pending_offers") or {}
    if not pend:
        return
    quiet = cfg.get("broker_quiet_seconds", BROKER_QUIET_SECONDS)
    now = time.time()
    due = [k for k, v in pend.items() if now - v.get("at", 0) >= quiet]
    if not due:
        return
    for k in due:
        pend.pop(k, None)
    store.set_kv("pending_offers", pend)
    for k in due:
        try:
            o = concierge.get_offer(store, int(k))
            ai = ai_parse(cfg, store, o["text"], o["photos"]) if o else None
            if ai is not None and ai.get("is_offer") is False and o and not o["photos"]:
                concierge.mark_as_message(cfg, store, int(k))   # «позвоню», «есть варианты» — не карточка
                continue
            if ai:
                concierge.enrich_offer(cfg, store, int(k), ai)
            concierge.notify_offer(cfg, store, int(k))
            if request_deal(store) == "sale":
                send_offer_analysis(cfg, store, int(k), sale_store)
        except Exception as e:
            log.warning("не показал вариант %s: %s", k, e)


def send_offer_analysis(cfg, store, oid, sale_store=None) -> bool:
    """Покупка: к варианту маклера — тот же анализ цены, что к объявлениям Uybor."""
    o = concierge.get_offer(store, oid)
    if not o or not o.get("price_usd") or o["price_usd"] < 5000:
        return False
    own = sale_store is None
    st = sale_store or Store(SALE_DB_PATH)
    try:
        l = {"key": f"offer:{oid}", "price_usd": o["price_usd"], "area": o.get("area"),
             "rooms": o.get("rooms"), "district": o.get("district"), "text": o.get("text") or "",
             "title": "", "created_at": o.get("created_at"), "floor": o.get("floor"),
             "repair": o.get("repair") or ""}
        if o.get("photos") and cfg.get("worker_url"):
            r = worker_post(cfg, "/svc/repair", {"file_ids": o["photos"][:4]}, timeout=90)
            if (r or {}).get("ok"):
                l["repair_photo"] = r["repair"]
        text = market.format_analysis(st, l, cfg)
    except Exception as e:
        log.info("анализ варианта #%s не удался: %s", oid, e)
        return False
    finally:
        if own:
            st.conn.close()
    return bool(text) and send_telegram(cfg, text.replace("📊 <b>Анализ</b>", f"📊 <b>Анализ варианта #{oid}</b>", 1))


RUN_DEADLINE = None      # до какого времени (epoch) живёт этот процесс — сообщаем воркеру


def worker_call(cfg, path, params=None, timeout=20):
    try:
        r = requests.get(cfg["worker_url"] + path, params=params or {}, timeout=timeout,
                         headers={"x-svc": cfg.get("worker_key", ""),
                                  "user-agent": "rano-radar/1.0"})
        if r.status_code != 200:
            log.error("Воркер %s %s: %s", path, r.status_code, r.text[:200])
            return None
        return r.json()
    except requests.RequestException as e:
        log.error("Воркер недоступен: %s", e)
        return None


def fetch_updates(cfg, store, long_poll=0):
    """Обновления: из очереди воркера (если он настроен) или getUpdates.

    Возвращает (resp, offset_key). У воркера свои номера — отдельный offset."""
    if not cfg.get("worker_url"):
        offset = store.get_kv("tg_offset", 0)
        return tg_call(cfg, "getUpdates", {"offset": offset + 1, "timeout": long_poll},
                       timeout=long_poll + 20), "tg_offset"
    offset = store.get_kv("wq_offset", 0)
    until = int((RUN_DEADLINE or time.time() + 60) + 90)
    end = time.time() + long_poll
    while True:                                   # «long-poll» опросом раз в 3 с
        resp = worker_call(cfg, "/svc/updates", {"after": offset, "until": until})
        if not resp or resp.get("result") or time.time() >= end:
            return resp, "wq_offset"
        time.sleep(3)


def process_commands(cfg: dict, store, long_poll: int = 0) -> dict:
    """Читает новые сообщения/нажатия, применяет их, отвечает."""
    settings = {**default_settings(), **(store.get_kv("settings") or {})}
    resp, offset_key = fetch_updates(cfg, store, long_poll)
    offset = store.get_kv(offset_key, 0)
    if not resp:
        return settings
    changed = False
    snap = False                                  # шортлист/карточки поменялись — воркеру сразу
    for upd in resp.get("result", []):
        offset = max(offset, upd.get("update_id", 0))

        cb = upd.get("callback_query")
        if cb:
            msg = cb.get("message") or {}
            if str((msg.get("chat") or {}).get("id") or "") != str(cfg["telegram_chat_id"]):
                continue
            if cb.get("_worker_done"):          # воркер уже ответил и показал следующее — сохраняем
                apply_worker_done(cb.get("data") or "", cfg, store)
                changed = True
                continue
            toast, view = handle_callback(cb.get("data") or "", settings, store, cfg,
                                          msg.get("message_id"))
            snap = snap or (cb.get("data") or "")[:2] in ("o:", "s:", "t:")
            # подсказка-«всплывашка»; для старых нажатий Telegram её отклоняет — это нормально
            tg_call(cfg, "answerCallbackQuery",
                    {"callback_query_id": cb.get("id"), "text": toast}, quiet=True)
            if view:
                render_view(cfg, settings, view, msg.get("message_id"))
            changed = True
            log.info("Кнопка: %s → %s", (cb.get("data") or "")[:30], toast[:40])
            continue

        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if str(chat.get("id") or "") != str(cfg["telegram_chat_id"]):
            handle_broker_message(cfg, store, msg)   # это маклер прислал вариант
            continue

        if msg.get("_link"):                 # воркер: владелец прислал ссылку на объявление
            handle_link(cfg, store, msg)
            continue
        if msg.get("_owner_offer"):          # воркер: владелец переслал/вставил вариант
            handle_owner_offer(cfg, store, msg)
            changed = True
            continue
        if msg.get("_view"):                 # воркер разобрал время просмотра и уже ответил
            v = msg["_view"]
            concierge.set_viewing(cfg, store, int(v.get("oid") or 0), v.get("at"), v.get("label", ""),
                                  v.get("notime"), msg.get("text") or "")
            changed = snap = True
            continue
        if msg.get("_note"):                 # заметка к варианту — воркер уже подтвердил
            concierge.add_note(store, int(msg["_note"].get("oid") or 0), msg.get("text") or "")
            changed = snap = True
            continue

        wad = msg.get("web_app_data") or {}
        if wad.get("data"):
            if concierge.apply_webapp_data(cfg, store, wad["data"]):
                log.info("параметры получены из мини-аппа")
                changed = True
                ans = (store.get_kv("anketa") or {}).get("ans") or {}
                if ans.get("deal") == "rent":     # «ищу сама» по аренде — по параметрам из разговора
                    settings.update(concierge.rent_settings(ans, settings))
                push_snapshot(cfg, store, settings, force=True)   # новый текст запроса — в ссылки маклерам
            continue

        text = (msg.get("text") or "").strip()
        if store.get_kv("awaiting_text") and text and not text.startswith("/"):
            store.set_kv("request_text", text)
            store.set_kv("awaiting_text", False)
            push_snapshot(cfg, store, settings, force=True)
            tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"],
                                         "text": "✅ Текст запроса сохранён.",
                                         "reply_markup": json.dumps({"inline_keyboard": [[
                                             {"text": "📇 Разослать маклерам", "callback_data": "b"}]]}, ensure_ascii=False)})
            changed = True
            continue
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
    store.set_kv(offset_key, offset)
    if changed:
        store.set_kv("settings", settings)
    if snap:
        push_snapshot(cfg, store, settings, force=True)
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


def relevance_reject(l: dict, cfg: dict, settings: dict, store=None) -> str:
    """Возвращает причину отсева как нерелевантного или '' если объявление годится."""
    if settings.get("exclude_shared", True):
        hit = looks_like_room_share(f'{l.get("title","")} {l.get("text","")}')
        if hit:
            return f"подселение (по слову «{hit}»)"
        usd = l.get("price_usd")
        floor = cfg.get("min_sane_price_usd") or 0
        if usd is not None and floor and usd < floor:
            return f"цена ${usd:.0f} — это комната, а не квартира"
    if cfg.get("require_price_or_district") and not l.get("price_value") \
            and not l.get("district"):
        return "нет ни цены, ни района"
    if settings.get("segment") == "premium":
        sig = analyst.premium_signals(l)
        l["premium"] = sig
        if not analyst.is_premium(l):
            return "не тот класс жилья (нужен новый ЖК с дизайнерским ремонтом)"
    if settings.get("owner_only", True) and store is not None:
        why = owner_only_reject(l, store, cfg, settings)
        if why:
            return "не хозяин: " + why
    return ""


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


# ------------------------------------------------ покупка от собственника ----
# Личный поиск владельца бота: квартира для покупки, только от собственников.
# Живёт отдельно от арендного радара (своя база sale.db, свой формат карточки),
# чтобы цены продажи не смешивались с арендной аналитикой.

SALE_AGENCY_WORDS = [
    "агентство недвижимости", "агентства недвижимости", "риелтор", "риэлтор",
    "realtor", "rieltor", "услуги агентства", "наши услуги", "agentlik",
]
# Фразы собственника, которые перевешивают слова-признаки агентства
# («риелторам не беспокоить» — это как раз хозяин).
SALE_OWNER_WORDS = [
    "риелторам не", "риэлторам не", "без риелтор", "без риэлтор", "маклерам не",
    "maklerlar kerak emas", "маклерлар керак эмас", "агентствам не",
]
SALE_NOT_FLAT_WORDS = [
    "продается комната", "продаётся комната", "продам комнату",
    "комната в общежитии", "доля в квартире", "долю в квартире",
    "ҳовли уй", "hovli uy", "ер жой сотилади", "yer sotiladi", "участок", "дача сотилади", "сотих", "sotix",
]
# значения Uybor (собраны по живым объявлениям); неизвестное показываем как есть
REPAIR_RU = {
    "evro": "евроремонт", "sredniy": "средний ремонт", "custom": "авторский проект",
    "chernovaya": "черновая отделка", "kapital": "требует ремонта",
}
FOUNDATION_RU = {
    "kirpich": "кирпич", "monolit": "монолит", "panel": "панель", "blok": "блок",
    "other": "",
}


def uybor_user_ads(uid: str, store, cfg) -> int:
    """Сколько активных объявлений у пользователя Uybor во всех разделах.
    У собственника 1–2, у агентства — десятки и сотни. -1 = узнать не удалось."""
    if not uid or uid == "None":
        return -1
    sid = f"uybor:{uid}"
    cached = store.seller_ads_cached(sid, cfg.get("seller_cache_days", 3))
    if cached is not None:
        return cached
    try:
        r = requests.get(UYBOR_API, params={"limit": 1, "user__eq": uid},
                         headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return -1
        data = r.json() or {}
        res = data.get("results") or []
        # если API перестанет понимать user__eq, вернётся весь сайт — не верим
        if res and str(res[0].get("userId")) != str(uid):
            log.warning("[продажа] Uybor не отфильтровал по продавцу %s", uid)
            return -1
        cnt = int(data.get("total", -1))
    except (requests.RequestException, ValueError, TypeError) as e:
        log.info("[продажа] не удалось узнать число объявлений %s: %s", sid, e)
        return -1
    if cnt >= 0:
        store.seller_ads_put(sid, cnt)
    return cnt


def sale_reject(l: dict, ss: dict, store, cfg) -> tuple:
    """('', False) — подходит; (причина, False) — отсеять навсегда;
    (причина, True) — не удалось проверить, попробовать в следующий проход."""
    l["price_usd"] = to_usd(l.get("price_value"), l.get("price_currency"), cfg)
    p = l["price_usd"]
    if p is None:
        return "цена не указана", False
    if p > ss.get("max_price_usd", 0):
        return f"дороже бюджета (${p:,.0f})", False
    if p < (ss.get("min_price_usd") or 0):
        return f"подозрительно низкая цена (${p:,.0f})", False
    rooms = as_int(l.get("rooms"))
    if ss.get("rooms") and rooms is not None and rooms not in ss["rooms"]:
        return f"{rooms}-комн", False
    if ss.get("districts") and l.get("district") not in ss["districts"]:
        return f"район {l.get('district') or 'не указан'}", False
    a = age_days(l.get("created_at") or "")
    if a is not None and a > ss.get("notify_max_age_days", 14):
        return f"объявлению {a:.0f} дн.", False

    low = f"{l.get('title', '')} {l.get('text', '')}".lower()
    hit = next((w for w in SALE_NOT_FLAT_WORDS if w in low), "")
    if hit:
        return f"продаётся не квартира («{hit}»)", False

    why = classify_sale_seller(l, ss, store, cfg)
    if not ss.get("owner_only", True):
        return "", False                 # подходят и маклеры — продавца только помечаем
    if l["seller_kind"] == "unknown":
        return "не удалось проверить продавца", True
    if l["seller_kind"] == "agency":
        return why, False
    return "", False


def classify_sale_seller(l: dict, ss: dict, store, cfg) -> str:
    """Кто продаёт: l['seller_kind'] = owner | agency | unknown.
    Возвращает причину, по которой продавец признан агентством/маклером."""
    low = f"{l.get('title', '')} {l.get('text', '')}".lower()
    says_owner = bool(hot_flags(l.get("text") or "", cfg)) \
        or any(w in low for w in SALE_OWNER_WORDS)
    limit = ss.get("max_owner_ads", 2)
    sid = l.get("seller_id") or ""
    uid = sid.partition(":")[2]
    ads = uybor_user_ads(uid, store, cfg) if sid.startswith("uybor:") else -1   # у других сайтов — своя пометка
    l["seller_ads"] = ads
    hint = l.get("seller_hint") or ""

    why = ""
    if not says_owner:
        hit = next((w for w in SALE_AGENCY_WORDS if w in low), "")
        if hit:
            why = f"текст агентства («{hit}»)"
    if not why:
        spread = phone_spread(l, store)
        if spread > limit:
            why = f"телефон встречается в {spread} объявлениях"
    if not why and ads > limit:
        why = f"у продавца {ads} объявлений — агентство или маклер"
    if not why and hint in ("agency", "developer"):
        why = "застройщик" if hint == "developer" else f"агентство/посредник по данным {l.get('site') or 'сайта'}"
    if why:
        l["seller_kind"] = "agency"
    elif hint == "owner" or ads >= 0:
        l["seller_kind"] = "owner"
    else:
        l["seller_kind"] = "unknown"
    return why


def fmt_area(a) -> str:
    """38.0 → 38, 27.21 → 27.2."""
    try:
        return f"{round(float(a), 1):g}"
    except (TypeError, ValueError):
        return str(a)


def _money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


def format_sale_message(l: dict, cfg: dict) -> str:
    district = l.get("district") or "район не указан"
    who = {"owner": "от собственника", "agency": "агентство / маклер"}.get(
        l.get("seller_kind"), "продавец не проверен")
    if l.get("seller_hint") == "developer":
        who = "от застройщика"
    lines = [f"🏷 <b>Продажа · {who}</b> · {escape_html(district)}"]
    spec = []
    if l.get("rooms"):
        spec.append(f'{l["rooms"]}-комн')
    if l.get("area"):
        spec.append(f'{fmt_area(l["area"])} м²')
    if l.get("floor"):
        spec.append(f'этаж {l["floor"]}'
                    + (f'/{l["floors_total"]}' if l.get("floors_total") else ""))
    if spec:
        lines.append("🛏 " + " · ".join(spec))
    p = l.get("price_usd")
    if p:
        price = f"💰 ${_money(p)}"
        if (l.get("price_currency") or "").upper() == "UZS":
            price = f'💰 {_money(l["price_value"])} сум (~${_money(p)})'
        if l.get("area"):
            price += f' · ~${_money(p / l["area"])}/м²'
        lines.append(price)
    bits = ["новостройка" if l.get("new_building") else "вторичка",
            FOUNDATION_RU.get(l.get("house_type") or "", l.get("house_type") or ""),
            REPAIR_RU.get(l.get("repair") or "", l.get("repair") or "")]
    lines.append("🏗 " + " · ".join(escape_html(b) for b in bits if b))
    if l.get("district_raw"):
        lines.append(f'📍 {escape_html(l["district_raw"])}')
    text = " ".join((l.get("text") or "").split())
    if text:
        lines.append("\n" + escape_html(text[:220]) + ("…" if len(text) > 220 else ""))
    ev = []
    if l.get("price_note"):
        ev.append(l["price_note"])
    if l.get("seller_ads") is not None and l["seller_ads"] >= 0:
        ev.append(f'объявлений у продавца на Uybor: {l["seller_ads"]}')
    flags = hot_flags(l.get("text") or "", cfg)
    if flags:
        ev.append(f"«{flags[0]}»")
    if ev:
        lines.append("🔑 " + " · ".join(ev))
    if l.get("phones"):
        lines.append("📞 " + ", ".join(fmt_phone(x) for x in l["phones"][:2]))
    alts = [a for a in (l.get("alts") or []) if a.get("price_usd") and a.get("url")]
    if alts:
        kinds = {"owner": ", собственник", "agency": ", маклер"}
        refs = [f'<a href="{a["url"]}">${_money(a["price_usd"])}</a> ({escape_html(a.get("site") or "Uybor")}'
                f'{kinds.get(a.get("kind"), "")})' for a in alts[:3]]
        more = f" и ещё {len(alts) - 3}" if len(alts) > 3 else ""
        lines.append("👥 Эту же квартиру продают ещё: " + ", ".join(refs) + more)
    dt = parse_iso(l.get("created_at") or "")
    if dt:
        lines.append(f'🕐 {dt.astimezone(TASHKENT_TZ).strftime("%d.%m %H:%M")}')
    since = parse_iso(l.get("listed_since") or "")
    if since:
        days = age_days(l["listed_since"])
        lines.append(f'🔁 Перевыложено: впервые замечено {since.astimezone(TASHKENT_TZ).strftime("%d.%m.%Y")}'
                     + (f" — на рынке ~{days:.0f} дн." if days else ""))
    if l.get("why"):
        lines.append("⭐ " + escape_html(", ".join(l["why"])))
    lines.append(f'\n<a href="{l["url"]}">Открыть на {escape_html(l.get("site") or "Uybor")}</a>')
    return "\n".join(lines)


def sale_criteria_text(ss: dict) -> str:
    rooms = "–".join(str(r) for r in (ss.get("rooms") or []))
    rooms = f"{rooms} комн" if rooms else "любая комнатность"
    districts = ", ".join(ss.get("districts") or []) or "любые районы"
    if ss.get("owner_only", True):
        sellers = (f'Только собственники: у продавца не больше {ss.get("max_owner_ads", 2)} '
                   f'объявлений и нет признаков агентства.')
    else:
        sellers = "Продавцы — собственники и маклеры/агентства; в карточке помечено, кто продаёт."
    return (f'{rooms} · до ${_money(ss.get("max_price_usd", 0))} · {districts}\n'
            f'{sellers} Источники — Uybor, Realt24, Joymee, Realting, Yangiuylar и Telegram-каналы.')


def sale_fingerprint(ss: dict) -> str:
    """Условия поиска одной строкой: изменились — пересматриваем отсеянное."""
    keys = ("max_price_usd", "min_price_usd", "rooms", "districts", "owner_only",
            "max_owner_ads", "notify_max_age_days")
    return json.dumps({k: ss.get(k) for k in keys}, ensure_ascii=False, sort_keys=True)


def _areas_differ(a, b) -> bool:
    return bool(a and b and abs(a - b) / max(a, b) > 0.10)


def _same_sale(a: dict, b: dict, cfg: dict) -> bool:
    """Одна и та же квартира в двух объявлениях текущего прохода (как find_dup).
    Телефон сравниваем только у собственников: у агентства один номер на десятки квартир."""
    if a.get("rooms") and b.get("rooms") and a["rooms"] != b["rooms"]:
        return False
    if _areas_differ(a.get("area"), b.get("area")):
        return False
    if "agency" not in (a.get("seller_kind"), b.get("seller_kind")) \
            and set(a.get("phones") or []) & set(b.get("phones") or []):
        return True
    pa, pb = a.get("price_usd"), b.get("price_usd")
    if pa and pb and abs(pa - pb) / max(pa, pb) > cfg["dedup"]["price_tolerance"]:
        return False
    na, nb = normalize_text(a.get("text") or ""), normalize_text(b.get("text") or "")
    if len(na) < 40 or len(nb) < 40:
        return False
    return SequenceMatcher(None, na, nb).ratio() >= cfg["dedup"]["fuzzy_threshold"]


def find_sale_dup(l: dict, store, cfg: dict):
    """find_dup для продажи: у агентств не сравниваем телефоны, и разная площадь — не дубль."""
    probe = dict(l, phones=[]) if l.get("seller_kind") == "agency" else l
    key = store.find_dup(probe, cfg)
    if not key:
        return None
    row = store.conn.execute("SELECT data, notified FROM listings WHERE key=?", (key,)).fetchone()
    try:
        other = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        other = {}
    if _areas_differ(l.get("area"), other.get("area")):
        return None
    if row and not row[1]:
        # та же квартира, но прежнее объявление мы не присылали (например, оно было
        # слишком старым) — это перевыкладка: присылаем и показываем, с какого времени
        # квартира на рынке; это аргумент для торга
        first = other.get("created_at")
        if first and (not l.get("listed_since") or first < l["listed_since"]):
            l["listed_since"] = first
        return None
    return key


def run_sale_search(cfg: dict, store, settings: dict, force: bool = False) -> int:
    """Один проход поиска квартиры для покупки по всем источникам.
    Сильные варианты (ниже рынка, снижена цена) — сразу, остальные — в подборку дня.
    Возвращает число отправленных сразу."""
    ss = cfg.get("sale_search") or {}
    listings = []
    if (ss.get("uybor") or {}).get("enabled", True):
        try:
            listings = fetch_uybor_sale(ss, cfg)
        except Exception as e:
            log.warning("[продажа] Uybor: %s", e)
    try:
        listings += sale_sources.fetch_due(ss, cfg, store, force=force)
    except Exception as e:
        log.warning("[продажа] другие источники: %s", e)
    # раз в сутки — срез рынка для анализа (сам ловит свои ошибки)
    market.maybe_scan(store, cfg, sale_district_ids(ss))

    # Условия поиска поменялись — всё, что раньше отсеяли, пересматриваем заново.
    # Уже присланное не трогаем, чтобы не было повторов.
    fp = sale_fingerprint(ss)
    intro_sent = store.get_kv("sale_intro_sent", False)
    criteria_changed = intro_sent and store.get_kv("sale_criteria") != fp
    if criteria_changed:
        store.conn.execute("DELETE FROM phones WHERE key IN "
                           "(SELECT key FROM listings WHERE notified=0)")
        store.conn.execute("DELETE FROM listings WHERE notified=0")
        store.conn.commit()
        store.set_kv("sale_pick", [])
        log.info("[продажа] условия изменились — пересматриваю отсеянные объявления")

    candidates, seen = [], 0
    for l in listings:
        try:
            if store.known(l["key"]):
                continue
            seen += 1
            sale_sources.normalize(l, cfg)
            why, retry = ("в цене только первый взнос, полной цены нет", False) \
                if l.get("down_payment_only") else sale_reject(l, ss, store, cfg)
            if why:
                if not retry:
                    store.save(l, notified=False)
                log.info("[продажа] мимо — %s: %s", why, l["title"][:45])
                continue
            candidates.append(l)
        except Exception as e:
            log.warning("[продажа] объявление %s пропущено из-за ошибки: %s", l.get("key"), e)
            try:
                store.save(l, notified=False)
            except Exception:
                pass

    # одна квартира, выложенная несколькими продавцами, — одно уведомление;
    # повтор пройдёт через find_dup в следующий проход, когда оригинал уже в базе
    # главной становится самая дешёвая копия, остальные — «👥 ещё у N» в её карточке
    groups = []
    for l in candidates:
        g = next((g for g in groups if any(_same_sale(l, u, cfg) or sale_sources.same_flat(l, u) for u in g)), None)
        if g is None:
            groups.append([l])
        else:
            g.append(l)
    unique = []
    for g in groups:
        rep = min(g, key=lambda x: x.get("price_usd") or 1e12)
        if len(g) > 1:
            rep["alts"] = sale_sources.merge_alts(rep.get("alts"), [x for x in g if x is not rep], rep)
        unique.append(rep)

    if settings.get("paused"):
        return 0          # ничего не сохраняем — пришлём после /resume
    first = not intro_sent or criteria_changed
    if first:
        days = ss.get("notify_max_age_days", 14)
        found = (f"За последние {days} дней нашлось подходящих: {len(unique)}. Самые выгодные пришлю "
                 f"отдельно, остальные — одной подборкой, лучшие сверху."
                 if unique else
                 f"За последние {days} дней подходящих нет — но я на посту, пришлю, как только появятся 👀")
        title = "Условия поиска обновлены" if intro_sent else "Поиск квартиры для покупки включён"
        intro = (f"🏷 <b>{title}</b>\n" + sale_criteria_text(ss) + "\n\n" + found
                 + "\nСтатус — кнопка «🔎 Ищет Ra'no»")
        if not send_telegram(cfg, intro):
            return 0      # Telegram недоступен — всё повторим в следующий проход
        store.set_kv("sale_intro_sent", True)
        store.set_kv("sale_criteria", fp)

    # анализ к вариантам, присланным до его появления, — до новых карточек,
    # чтобы новые не получили анализ дважды
    backfill_sale_analysis(cfg, store, settings)

    day = sale_sources.day_stats(store)
    cap = ss.get("instant_per_day", 6)         # сразу — не больше стольких в день, остальное подборкой
    photo_budget = [ss.get("photo_checks_per_run", 25)]   # ремонт по фото — модель, не больше стольких за проход
    sent = queued = capped = 0                 # capped — в счёт дневного лимита «сразу»
    for l in unique[:ss.get("first_run_limit", 25) * 4]:
        dup = find_sale_dup(l, store, cfg) or sale_sources.structural_dup(store, l)
        if dup:
            act = sale_sources.attach_dup(cfg, store, l, dup, ss)
            sent += act == "cheaper"
            queued += act == "queued"
            continue
        if photo_budget[0] > 0 and sale_sources.photo_repair(cfg, l):
            photo_budget[0] -= 1
        sc, why, strong, _ = sale_sources.score(store, l, cfg, ss)
        l["score"], l["why"] = sc, why
        mins = fresh_owner_minutes(l, ss) if not first else None     # первый проход — без «горячих»
        if mins is not None or (strong and day.get("instant", 0) + capped < cap):
            silent = is_night()
            text = format_sale_message(l, cfg)
            if mins is not None:
                text = (f"🔥 <b>Только что от собственника</b> · выложено {ago_text(mins)}\n"
                        "Позвоните первым — такие варианты маклеры перехватывают за пару часов.\n\n" + text)
            ids = send_listing(cfg, settings, l, False, text=text, silent=silent)
            if not ids:
                log.info("[продажа] не отправилось, повторю позже: %s", l["title"][:45])
                break     # не сохраняем — объявление придёт в следующий проход
            store.save(l, notified=True)
            sent += 1
            capped += mins is None
            log.info("[продажа] %s (%s): %s", "горячее от собственника" if mins is not None else "сразу", sc, l["title"][:60])
            send_sale_analysis(cfg, store, l, kb=sale_kb(l["key"], ids), silent=silent)
            sale_sources.remember_shown(store, l, why)
            time.sleep(1)
        else:
            store.save(l, notified=False)
            sale_sources.queue_pick(store, l, sc, why)
            queued += 1
    sale_sources.day_stats(store, {"seen": seen, "fit": len(unique), "instant": capped,
                                   "hot": sent - capped, "queued": queued})
    if first:                                  # первый проход по новым условиям — для «на сайтах пусто → маклеры»
        store.set_kv("first_pass", {"at": datetime.now(timezone.utc).isoformat(), "fit": len(unique)})
    if first and queued:
        sale_sources.send_pick(cfg, store, reason="Что нашлось сейчас")
    return sent


def fresh_owner_minutes(l: dict, ss: dict):
    """Сколько минут назад собственник выложил объявление — если недавно (по умолчанию ≤ 3 ч), иначе None.
    Такое присылаем сразу и вне дневного лимита: успеть раньше маклеров."""
    if l.get("seller_kind") != "owner":
        return None
    a = age_days(l.get("created_at") or "")
    if a is None:
        return None
    mins = max(0, a * 1440)
    return mins if mins <= ss.get("fresh_owner_minutes", 180) else None


def ago_text(mins: float) -> str:
    if mins < 2:
        return "только что"
    if mins < 60:
        return f"{mins:.0f} мин назад"
    h, m = divmod(int(mins), 60)
    return f"{h} ч {m:02d} мин назад"


def sale_district_ids(ss: dict) -> list:
    return [i for i, name in UYBOR_DISTRICT_IDS.items() if name in (ss.get("districts") or [])]


def sale_kb(key, card_ids=None):
    """«Мимо» несёт id карточки над анализом — воркер удалит из чата и её, и анализ."""
    no = f"L:n:{key}"
    ids = sorted(card_ids) if isinstance(card_ids, list) else []
    if ids and ids == list(range(ids[0], ids[0] + len(ids))):
        ref = f"{no}|{ids[0]}.{len(ids)}"
        no = ref if len(ref) <= 64 else no
    return {"inline_keyboard": [[{"text": "👍 В шортлист", "callback_data": f"L:s:{key}"[:64]},
                                 {"text": "👎 Мимо", "callback_data": no[:64]}]]}


def send_sale_analysis(cfg: dict, store, l: dict, kb=None, silent=False) -> bool:
    """Анализ варианта — отдельным сообщением: в подпись к фото (1024 символа) не влезает.
    С кнопками «В шортлист / Мимо», если передали kb."""
    try:
        text = market.format_analysis(store, l, cfg)
    except Exception as e:
        log.warning("[продажа] анализ %s не удался: %s", l.get("key"), e)
        text = ""
    if not kb:
        return bool(text) and send_telegram(cfg, text)
    return tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "text": text or "Что делаем с этим вариантом?",
        "parse_mode": "HTML", "disable_web_page_preview": True, **({"disable_notification": True} if silent else {}),
        "reply_markup": json.dumps(kb, ensure_ascii=False)}) is not None


def backfill_sale_analysis(cfg: dict, store, settings: dict) -> int:
    """Один раз: анализ для вариантов, присланных до появления анализа."""
    if store.get_kv("sale_analysis_backfill", False) or settings.get("paused"):
        return 0
    if not store.get_kv("market_scan_at"):
        return 0                     # без среза рынка анализ пустой — подождём
    rows = store.conn.execute(
        "SELECT data FROM listings WHERE notified=1 ORDER BY first_seen").fetchall()
    active = {k for (k,) in store.conn.execute(
        "SELECT key FROM market WHERE op='sale' AND removed_at IS NULL")}
    todo = []
    for (data,) in rows:
        try:
            l = json.loads(data or "{}")
        except ValueError:
            continue
        if l.get("key") in active:
            todo.append(l)
    if todo and not send_telegram(cfg, f"📊 <b>Добавила анализ цены</b> к вариантам, которые уже "
                                       f"присылала и которые ещё в продаже ({len(todo)}). "
                                       f"Сводка рынка — «⋯ Ещё» → «Цены рынка»"):
        return 0
    n = 0
    for l in todo[:20]:
        if send_sale_analysis(cfg, store, l):
            n += 1
        time.sleep(1)
    store.set_kv("sale_analysis_backfill", True)
    return n


def sale_market_text(cfg: dict) -> str:
    ss = cfg.get("sale_search") or {}
    if not ss.get("enabled"):
        return "📊 Поиск квартиры для покупки выключен."
    store = Store(SALE_DB_PATH)
    try:
        return market.summary_text(store, cfg, ss.get("districts") or [])
    finally:
        store.conn.close()


def sale_status_text(cfg: dict) -> str:
    ss = cfg.get("sale_search") or {}
    if not ss.get("enabled"):
        return "🏷 Поиск квартиры для покупки выключен."
    store = Store(SALE_DB_PATH)
    try:
        total = store.counts()[0]
        rows = store.conn.execute(
            "SELECT url, data FROM listings WHERE notified=1 "
            "ORDER BY first_seen DESC LIMIT 7").fetchall()
        n_sent = store.conn.execute(
            "SELECT COUNT(*) FROM listings WHERE notified=1").fetchone()[0]
    finally:
        store.conn.close()
    lines = ["🏷 <b>Поиск квартиры для покупки</b>", sale_criteria_text(ss), "",
             f"Проверено объявлений: {total}, прислано: {n_sent}"]
    for url, data in rows:
        try:
            d = json.loads(data or "{}")
        except ValueError:
            d = {}
        bits = [f'{d["rooms"]}-комн' if d.get("rooms") else "",
                f'{fmt_area(d["area"])} м²' if d.get("area") else "",
                f'${_money(d["price_usd"])}' if d.get("price_usd") else ""]
        label = ", ".join(b for b in bits if b) or "вариант"
        lines.append(f'• <a href="{url}">{label}</a> — {escape_html(d.get("district") or "")}')
    lines.append("\n📊 Цены за м², аренда и сценарии по районам — «⋯ Ещё» → «Цены рынка»")
    return "\n".join(lines)


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
    global RUN_DEADLINE
    RUN_DEADLINE = deadline
    cfg = load_config()
    store = Store(DB_PATH)
    store.prune()
    first_run = store.counts()[0] == 0

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    detect_bot_username(cfg)
    if not once and store.get_kv("kb_v") != 2:      # новые кнопки внизу — показать один раз
        if tg_call(cfg, "sendMessage", {
                "chat_id": cfg["telegram_chat_id"], "parse_mode": "HTML",
                "text": "✨ <b>Обновила кнопки внизу</b>\n\n"
                        "🔎 <b>Ищет Ra'no</b> — я сама смотрю Uybor, Realt24, Joymee, Realting, Yangiuylar и "
                        "Telegram-каналы. Выгодное присылаю сразу, остальное — подборкой в 19:30.\n"
                        "📇 <b>Через маклеров</b> — запрос маклерам, их варианты и шортлист.\n"
                        "⋯ <b>Ещё</b> — всё остальное.",
                "reply_markup": json.dumps(concierge.OWNER_KB, ensure_ascii=False)}) is not None:
            store.set_kv("kb_v", 2)
    enabled = {name: s for name, s in cfg["sources"].items() if s.get("enabled")}
    next_run = {name: 0.0 for name in enabled}
    mode = " (разовый проход)" if once else (f" на {minutes:.0f} мин" if minutes else "")
    log.info("Rent Radar запущен%s. Источники: %s. Лимит: $%s",
             mode, ", ".join(enabled) or "нет", cfg["max_price_usd"])

    sale_cfg = effective_sale_cfg(cfg, store).get("sale_search") or {}   # покупка из чата включает поиск
    sale_store, next_sale = None, 0.0
    next_sale_brokers = time.time() + 120     # сначала — поиск по сайтам, сбор маклеров потом
    if sale_cfg.get("enabled") and (sale_cfg.get("uybor") or {}).get("enabled", True):
        sale_store = Store(SALE_DB_PATH)
        sale_store.prune()
        log.info("Покупка: %s", sale_criteria_text(sale_cfg).replace("\n", " "))
    if first_run:
        log.info("Первый запуск: текущие объявления запоминаю без уведомлений")

    while not stop["flag"]:
        now = time.time()
        # long-poll: команды и нажатия кнопок ловим за ~секунду, а не раз в проход
        settings = process_commands(cfg, store, long_poll=0 if once else 20)
        flush_pending_offer(cfg, store, sale_store)
        push_snapshot(cfg, store, settings)
        if not once:
            followup.run(cfg, store, sale_store)     # напоминания, просмотры, вечерняя сводка
        eff = effective_cfg(cfg, settings)
        market = analyst.market_stats(store)
        rent_on = rent_search_on(store)
        for name, scfg in enabled.items():
            if not once and now < next_run[name]:
                continue
            # аренда не нужна (клиент покупает) — смотрим реже: только для маклеров и рынка
            next_run[name] = now + scfg.get("interval_seconds", 120) * (1 if rent_on else 5)
            try:
                listings = SOURCE_FETCHERS[name](scfg, cfg)
            except Exception as e:
                log.warning("[%s] ошибка получения: %s", name, e)
                next_run[name] = now + min(900, scfg.get("interval_seconds", 120) * 3)
                continue

            fresh = 0
            warm = not store.get_kv(f"src_warm:{name}")     # новый источник: текущее — запомнить молча
            for l in listings:
              # одно битое объявление не должно ронять весь радар
              try:
                if store.known(l["key"]):
                    if not store.has_data(l["key"]):
                        store.backfill(l)       # дозаполняем гео и характеристики
                    continue
                seller_cnt = store.bump_seller(l.get("seller_id", ""))

                if first_run or warm:
                    store.save(l, notified=False)
                    continue

                ads_cnt = count_seller_ads(l, store, cfg)
                l["seller_ads"] = ads_cnt
                harvest_broker(l, store, cfg, ads_cnt)

                if not rent_on:                     # клиент покупает — аренду в чат не шлём
                    store.save(l, notified=False)
                    continue

                if not passes_filters(l, eff) or not passes_user_filters(l, settings):
                    store.save(l, notified=False)
                    continue

                reject = relevance_reject(l, cfg, settings, store)
                if reject:
                    store.save(l, notified=False)
                    log.info("[%s] не по теме — %s: %s", name, reject, l["title"][:45])
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
                try:
                    analyst.score_listing(l, store, cfg, market, settings)
                except Exception as e:
                    log.warning("анализ %s не удался: %s", l.get("key"), e)
                ok = send_listing(cfg, settings, l, likely_makler, silent=is_night())
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

            if warm and listings:
                store.set_kv(f"src_warm:{name}", True)
            if fresh:
                log.info("[%s] новых: %d", name, fresh)

        if not once and now >= next_sale_brokers:   # маклеры по продаже — для запросов на покупку
            next_sale_brokers = now + (sale_cfg.get("broker_interval_seconds") or 900)
            try:
                backfill_sale_brokers(store, sale_store)
                harvest_sale_brokers(cfg, store)
                harvest_market_brokers(cfg, store)
            except Exception as e:
                log.warning("[маклеры продажи] сбор не удался: %s", e)

        force_flag = store.get_kv("sale_force")                # «Проверить сайты сейчас» или новые параметры
        force_sale = bool(force_flag)
        fresh = bool(store.get_kv("fresh_start"))              # после сброса — ждём новых параметров из чата
        if sale_store is not None and not fresh and (once or now >= next_sale or force_sale):
            next_sale = now + (sale_cfg.get("uybor") or {}).get("interval_seconds", 60)
            if force_sale:
                store.set_kv("sale_force", False)
            try:
                n = run_sale_search(effective_sale_cfg(cfg, store), sale_store, settings, force=force_sale)
                if force_flag == "button":
                    st = sale_sources.day_stats(sale_store)
                    send_telegram(cfg, f"🔄 Пробежалась по всем сайтам! Сегодня просмотрено {st.get('seen', 0)}, "
                                       f"подошло {st.get('fit', 0)}; в подборке ждут "
                                       f"{len(sale_sources.pick_pending(sale_store))}.")
                if n:
                    log.info("[продажа] новых: %d", n)
            except Exception as e:      # поиск покупки не должен ронять радар
                log.warning("[продажа] проход не удался: %s", e)
        if sale_store is not None and not once and not fresh:
            try:
                sale_sources.maybe_daily_pick(cfg, sale_store)   # 19:30 — подборка дня
            except Exception as e:
                log.warning("[продажа] подборка: %s", e)

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

    if cfg.get("worker_url") and not once:
        try:                                       # свежие карточки — воркеру, пока мы спим
            push_snapshot(cfg, store, {**default_settings(), **(store.get_kv("settings") or {})}, force=True)
        except Exception as e:
            log.warning("финальный снимок не отправлен: %s", e)
        worker_call(cfg, "/svc/bye", timeout=10)   # воркер будет будить нас сам
    log.info("Готово" if once or deadline else "Остановлено")


if __name__ == "__main__":
    run()

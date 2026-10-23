#!/usr/bin/env python3
"""
Рано — ассистент по поиску жилья (ядро: сбор данных и работа с маклерами).

Источники: OLX.uz (API), Uybor.uz (API), Birbir.uz (HTML),
Telegram-каналы (публичные веб-превью t.me/s/..., без логина).

Находит новые объявления, отсеивает дубли (один и тот же вариант из разных
каналов/от разных авторов) и шлёт уникальные в ваш Telegram.

Запуск: python3 rent_radar.py
"""

import collections
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
import socket

_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_retry(*a, **kw):
    """DNS на сервере иногда отвечает «Temporary failure» (десяток раз в сутки) — две повторные попытки."""
    for i in range(3):
        try:
            return _getaddrinfo(*a, **kw)
        except socket.gaierror as e:
            if e.errno != socket.EAI_AGAIN or i == 2:
                raise
            time.sleep(1 + i)


socket.getaddrinfo = _getaddrinfo_retry

import analyst
import concierge
import followup
import sale_sources
import market
import i18n
import i18n_texts
from i18n import t, dist
import geo
import tenants
import outreach
import miniapp

if __name__ == "__main__":
    # Запуск скриптом: модуль называется __main__, а concierge/sale_sources/followup делают `import rent_radar` —
    # без этого получили бы вторую копию модуля со своим состоянием (дедлайн прохода, pump, кэши).
    sys.modules.setdefault("rent_radar", sys.modules[__name__])

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
# Чья база сейчас: владелец (STATE_DIR) или другой пользователь (STATE_DIR/users/<chat>) — см. tenants.py.
# У пользователя своя sale.db (что видел, подборка), а срез рынка — общий: база владельца подключена как «shared».
CUR = {"sale": None, "shared": None}          # None — владелец: SALE_DB_PATH


def sale_path() -> Path:
    return CUR["sale"] or SALE_DB_PATH


def open_sale():
    st = Store(sale_path())
    if CUR.get("shared"):
        st.attach_shared(CUR["shared"])
    return st

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
VALID_PHONE_PREFIXES = {     # мобильные коды Узбекистана (20 — OQ, 50 — Ucell, 87 — Mobiuz) и городские Ташкента
    "20", "33", "50", "55", "77", "87", "88", "90", "91", "93", "94", "95", "97", "98", "99", "71", "78",
}

ROOMS_RE = [
    re.compile(r"\b([1-6])\s*[-–]?\s*(?:(?:х|ух|ти)\s*[-–]?\s*)?(?:комн|ком\b|к\.|хона|xona|xonali)", re.I),
    re.compile(r"\b([1-6])\s*/\s*\d{1,2}\s*/\s*\d{1,2}\b"),
]

# Отрицательный lookbehind нужен, чтобы «5/9 1100 у.е.» не читалось как «91100»:
# число не может начинаться сразу после цифры или дроби.
# Плюс (?<!\w) у варианта с разделителями тысяч: иначе «70м2 850$» читалось как 2850.
_NUM = r"(?<![\d/.,])((?<!\w)\d{1,3}(?:[\s.,]\d{3})+|\d+)"
PRICE_USD_RE = re.compile(_NUM + r"\s*(?:у\.?\s?е|\$|usd)", re.I)
PRICE_UZS_RE = re.compile(_NUM + r"\s*(?:сум|so['’`]?m|sum)", re.I)
PRICE_USD_PRE_RE = re.compile(r"(?:\$|\busd)\s?(\d{1,3}(?:[\s.,]\d{3})+|\d+)(?![\d.,]\d)", re.I)   # «$500», «Цена $75 000»
PRICE_MLN_RE = re.compile(r"(?<![\d.,])(\d{1,4}(?:[.,]\d{1,3})?)\s*(?:млн|mln|million)\.?\s*(?:сум|so['’`]?m|sum)?", re.I)

DISTRICTS = {
    "Яккасарай": ["яккасарай", "яккасарой", "yakkasaroy", "yakkasaray"],
    "Мирабад": ["мирабад", "миробод", "mirobod", "mirabad"],
    "Юнусабад": ["юнусабад", "юнусобод", "yunusobod", "yunusabad"],
    "Чиланзар": ["чиланзар", "чилонзор", "chilonzor", "chilanzar"],
    "Мирзо-Улугбек": ["мирзо улугбек", "мирзо-улугбек", "мирзо улуғбек", "mirzo ulug", "mirzo-ulug", "улугбек", "улубек",
                      "улуғбек"],
    "Шайхантахур": ["шайхантахур", "шайхонтоҳур", "шайхонтохур", "shayxontohur", "shayxontoxur", "shaykhantakhur",
                    "шайхантаур"],
    "Алмазар": ["алмазар", "олмазор", "olmazor", "almazar"],
    "Учтепа": ["учтепа", "uchtepa"],
    "Яшнабад": ["яшнабад", "яшнобод", "yashnobod", "yashnabad"],
    "Сергели": ["сергели", "sergeli"],
    "Бектемир": ["бектемир", "bektemir"],
    "Янгихаёт": ["янгихаёт", "янгихает", "янгиҳаёт", "yangihayot", "янгихаят"],
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
    if isinstance(v, float):
        return int(round(v)) if v == v else None        # Uybor отдаёт площадь дробью: 94.94
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        try:
            return int(round(float(str(v).strip().replace(",", "."))))
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
    for rx in (PRICE_USD_RE, PRICE_USD_PRE_RE):
        m = rx.search(text or "")
        if m:
            v = _num(m.group(1))
            if v and 30 <= v <= max_usd:
                return v, "USD"
    m = PRICE_UZS_RE.search(text or "")
    if m:
        v = _num(m.group(1))
        if v and v >= 300000:
            return v, "UZS"
    m = PRICE_MLN_RE.search(text or "")
    if m and not re.search(r"\$|у\.?\s?е|usd|долл", (text or "")[m.start():m.end() + 6], re.I):
        v = float(m.group(1).replace(",", ".")) * 1e6
        if v >= 300000:
            return round(v), "UZS"
    return None, None


def extract_district(text: str):
    """Первый по тексту названный район (не первый по списку: «Юнусабад…, 5 мин до Мирабадского рынка» — Юнусабад)."""
    low = re.sub(r"['’ʻ`‘]", "", (text or "").lower())
    best = None
    for name, variants in DISTRICTS.items():
        for v in variants:
            i = low.find(v.replace("'", ""))
            if i >= 0 and (best is None or i < best[0]):
                best = (i, name)
    return best[1] if best else None


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


SOURCE_FETCHERS = {          # одно скачивание на всех пользователей за минуту (tenants.shared_fetch)
    "olx": tenants.shared_fetch("olx", fetch_olx),
    "uybor": tenants.shared_fetch("uybor", fetch_uybor),
    "birbir": tenants.shared_fetch("birbir", fetch_birbir),
    "telegram": tenants.shared_fetch("telegram", fetch_telegram),
    "realt24": tenants.shared_fetch("realt24", fetch_realt24_rent),
    "joymee": tenants.shared_fetch("joymee", fetch_joymee_rent),
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
    if l.get("seller_id"):                     # с первого раза продавец мог выложить ещё десятки — считаем сейчас
        row = store.conn.execute("SELECT cnt FROM seller_counts WHERE seller_id=?", (l["seller_id"],)).fetchone()
        if row and row[0] and row[0] > ads:
            ads = row[0]
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

    def attach_shared(self, path):
        """Общий срез рынка (market, market_prices) из базы владельца: запросы без схемы находят его сами,
        своих таблиц market у пользователя нет."""
        if path and Path(path) != Path(self.conn.execute("PRAGMA database_list").fetchone()[2] or ""):
            if not any(r[1] == "shared" for r in self.conn.execute("PRAGMA database_list")):
                self.conn.execute("ATTACH DATABASE ? AS shared", (str(path),))
        return self

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
            "site", "seller_hint", "mortgage", "mortgage_src", "price_note", "score", "why", "alts", "repair_photo",
            "geo", "text_place", "near_km", "geo_tried", "rt_detail",
            "offplan", "price_listed_usd", "district_site", "district_by", "complex", "down_payment_only")

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

    def find_dup(self, listing: dict, cfg: dict, phone_shown_only=True):
        """Возвращает key оригинала или None.
        По телефону — только уже присланное с теми же комнатами: у маклера с одним номером десятки разных
        квартир, а перевыкладку непосланного (например, подешевевшую до бюджета) надо разобрать заново."""
        d = cfg["dedup"]
        now = datetime.now(timezone.utc)

        # 1) совпадение телефона
        if listing["phones"]:
            since = (now - timedelta(days=d["phone_days"])).isoformat()
            qmarks = ",".join("?" * len(listing["phones"]))
            for key, notified, rooms in self.conn.execute(
                    f"SELECT p.key, l.notified, l.rooms FROM phones p LEFT JOIN listings l ON l.key = p.key "
                    f"WHERE p.phone IN ({qmarks}) AND p.first_seen > ?", (*listing["phones"], since)):
                if phone_shown_only and not notified:
                    continue
                if listing.get("rooms") and rooms and listing["rooms"] != rooms:
                    continue
                return key

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
    """Номер как его пишут в Узбекистане: +998 90 123-45-67 (на входе 9 цифр, +998…, с пробелами — всё равно)."""
    d = re.sub(r"\D", "", str(p or ""))
    if len(d) == 12 and d.startswith("998"):
        d = d[3:]
    if len(d) != 9:
        return str(p or "")
    return f"+998 {d[:2]} {d[2:5]}-{d[5:7]}-{d[7:9]}"


def loc_tag(l: dict) -> str:
    """Насколько точно место: по метке на сайте, примерно по ориентиру, или известен только район."""
    g = l.get("geo") or {}
    fixed = (f" (на сайте — {escape_html(l['district_site'])})"
             if l.get("district_by") == "точка" and l.get("district_site") else "")
    if g.get("src") == "сайт" and not g.get("conflict") and float(g.get("spread") or 0) <= 1.0:
        return " · по метке на карте" + fixed
    if g.get("conflict"):
        return " · метка на карте не совпадает с текстом — место примерное"
    if g.get("src") in ("ориентир", "метро"):
        lab = (g.get("label") or "").strip()
        return " · примерно" + (f", по ориентиру «{escape_html(lab)}»" if lab else "")
    return " · точного адреса нет"


def format_message(l: dict, cfg: dict, likely_makler: bool) -> str:
    """Карточка аренды: сначала главное (что за квартира, цена, где), потом продавец и факты. Без «оценок из 10»
    и советов — факты: дешевле/дороже похожих, сколько объявлений у продавца, далеко ли метро."""
    spec = []
    if l.get("rooms"):
        spec.append(f'{l["rooms"]}-комн')
    if l.get("area"):
        spec.append(f'{float(l["area"]):g} м²')
    if l.get("floor"):
        spec.append(f'этаж {l["floor"]}' + (f'/{l["floors_total"]}' if l.get("floors_total") else ""))
    lines = [f'🏠 <b>{" · ".join(spec) or "Квартира"}</b> · {escape_html(l.get("source") or "")}']
    title = re.sub(r"^#\d+\s*·\s*", "", str(l.get("title") or "")).strip()
    num = re.match(r"^(#\d+)\s*·", str(l.get("title") or ""))
    if num:
        lines[0] = f"{num.group(1)} · " + lines[0]
    if len(title) >= 12 and not re.match(r"^\W*(?:ID|\[ID)\b", title, re.I):
        lines.append(f"<i>«{escape_html(title[:80])}»</i>")
    val, cur = l.get("price_value"), l.get("price_currency")
    if val:
        num_ = f"{int(round(float(val))):,}".replace(",", " ")
        if cur == "USD":
            lines.append(f"💰 ${num_}/мес")
        elif cur == "UZS":
            usd = l.get("price_usd") or to_usd(val, "UZS", cfg)
            lines.append(f"💰 {num_} сум/мес" + (f" (~${usd:.0f})" if usd else ""))
        else:
            lines.append(f"💰 {num_} (валюта не указана)")
    try:
        if not l.get("geo"):
            geo.locate(l)                      # точка и район по точке — до строки «📍», чтобы показать честно
    except Exception:
        pass
    place = l.get("district") or l.get("district_raw")
    if place:
        extra = l.get("district_raw") if l.get("district_raw") and l.get("district_raw") != place and \
            len(str(l.get("district_raw"))) <= 60 else ""
        lines.append(f'📍 {escape_html(str(place))}' + (f", {escape_html(str(extra))}" if extra else "")
                     + loc_tag(l))
    else:
        lines.append("📍 район не указан")
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
        a_ = age_days(l.get("created_at") or "")
        ago = "" if a_ is None else (f" · {max(1, int(a_ * 24))} ч назад" if a_ < 1 else f" · {int(a_)} дн. назад")
        lines.append(f'🕐 Выложено {dt.astimezone(TASHKENT_TZ).strftime("%d.%m %H:%M")}{ago}')

    ev = []
    if l.get("price_note"):
        ev.append(l["price_note"])
    if l.get("seller_ads") is not None and l["seller_ads"] >= 0:
        ev.append(f"{l['seller_ads']} объявл. у продавца")
    if (l.get("commission") or "").lower() in ("нет", "yo'q", "no"):
        ev.append("без комиссии")
    if ev:
        lines.append("🔑 " + " · ".join(ev))

    if l.get("score") is not None:              # факты от аналитика: цена к похожим, метро, продавец
        facts = [escape_html(x) for x in (l.get("pros") or [])[:5]] + \
                ["⚠️ " + escape_html(x) for x in (l.get("cons") or [])[:3]]
        if facts:
            lines.append("\n<b>Что видно</b>")
            lines += facts
        q = analyst.ask_seller(l)
        if q:
            lines.append("❓ Спросить: " + escape_html("; ".join(q)))

    a_ = age_days(l.get("created_at") or "")
    hurry = "  🆕 свежее" if a_ is not None and a_ <= 2 else ""
    lines.append(f'\n<a href="{l["url"]}">Открыть объявление</a>{hurry}')
    return "\n".join(lines)


def detect_bot_username(cfg) -> str:
    """Спрашиваем у Telegram, как бот называется сейчас.
    Так переименование в BotFather подхватывается само, без правок кода."""
    r = tg_call(cfg, "getMe", {}, quiet=True)
    name = ((r or {}).get("result") or {}).get("username") or ""
    if name:
        cfg["bot_username"] = name
    return name


def tg_call_files(cfg, method: str, data: dict, files: dict, timeout: int = 90):
    """Запрос к Telegram с файлами (альбом с картой и фото). None — не ушло."""
    api = f'https://api.telegram.org/bot{cfg["telegram_bot_token"]}/{method}'
    data = _owner_translate(cfg, data)
    try:
        r = requests.post(api, data=data, files=files or None, timeout=timeout)
        if r.status_code != 200:
            log.warning("Telegram %s %s: %s", method, r.status_code, r.text[:200])
            return None
        return r.json()
    except requests.RequestException as e:
        log.warning("Telegram %s не ушёл: %s", method, e)
        return None


def _owner_no_buttons(cfg, payload):
    """Клиенту кнопок не шлём — управление словами. Ссылки в тексте остаются; убрать кнопки (пустая
    клавиатура) и remove_keyboard — можно."""
    rm = payload.get("reply_markup")
    if not rm or str(payload.get("chat_id")) != str(cfg.get("telegram_chat_id")):
        return payload
    try:
        kb = json.loads(rm) if isinstance(rm, str) else rm
    except ValueError:
        return payload
    if isinstance(kb, dict) and kb.get("inline_keyboard"):
        btns = [b for row in kb["inline_keyboard"] for b in row]
        if btns and all(str(b.get("callback_data", "")).startswith("Q:") or b.get("url") for b in btns) \
                and any(str(b.get("callback_data", "")).startswith("Q:") for b in btns):
            return payload                       # кнопки карточки из очереди — их клиент просил
        payload = dict(payload)
        payload.pop("reply_markup", None)
    return payload


def sync_lang(store):
    """Язык клиента — из анкеты (ru / uz / en): на нём карточки, подсказки и ответы."""
    try:
        return i18n.set_lang(((store.get_kv("anketa") or {}).get("ans") or {}).get("lang") or "ru")
    except Exception:
        return i18n.LANG


def _owner_translate(cfg, payload):
    """Клиент не на русском: что осталось по-русски (не переведено вручную) — переводит модель."""
    if payload.get("_noTr"):
        payload = dict(payload)
        payload.pop("_noTr", None)
        return payload
    if i18n.LANG == "ru" or str(payload.get("chat_id")) != str(cfg.get("telegram_chat_id")):
        return payload
    out = payload
    for k in ("text", "caption"):
        v = payload.get(k)
        if isinstance(v, str) and i18n.has_russian(v):
            out = dict(out)
            out[k] = i18n.ai(cfg, v)
    md = payload.get("media")
    if md:
        try:
            items = json.loads(md) if isinstance(md, str) else md
            changed = False
            for it in items:
                if isinstance(it.get("caption"), str) and i18n.has_russian(it["caption"]):
                    it["caption"] = i18n.ai(cfg, it["caption"])
                    changed = True
            if changed:
                out = dict(out)
                out["media"] = json.dumps(items, ensure_ascii=False) if isinstance(md, str) else items
        except (ValueError, TypeError, AttributeError):
            pass
    return out


def tg_call(cfg, method: str, payload: dict, timeout: int = 20, quiet: bool = False):
    api = f'https://api.telegram.org/bot{cfg["telegram_bot_token"]}/{method}'
    if method != "editMessageReplyMarkup":
        payload = _owner_no_buttons(cfg, payload)
    payload = _owner_translate(cfg, payload)
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


def send_telegram(cfg, text: str, _admin: bool = False) -> bool:
    """Сообщение клиенту. _admin — служебное (источники, сбои): владельцу сервиса, по-русски, без перевода."""
    p = {"chat_id": (cfg.get("admin_chat_id") or cfg["telegram_chat_id"]) if _admin else cfg["telegram_chat_id"],
         "text": text, "parse_mode": "HTML"}
    if _admin:
        p["_noTr"] = True
    return tg_call(cfg, "sendMessage", p) is not None


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
    if cfg.get("card_queue") and _queue_store() is not None:   # по одной с кнопками — с фото или без
        ids = send_card(cfg, l if photos else dict(l, photo_urls=[]), text, silent=silent,
                        ref="L:" + (l.get("key") or ""), queue=True)
        if ids:
            return ids
    if photos:                                   # альбом: карта с отметкой + фото, подпись под ним
        ids = send_card(cfg, l, text, silent=silent, ref="L:" + (l.get("key") or ""))
        if ids:
            return ids
        log.info("Фото не отправились, шлю текстом: %s", l["title"][:50])
    r = tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML", **quiet})
    return False if r is None else (_msg_ids(r) or True)


def is_night(now=None) -> bool:
    """23:00–08:00 по Ташкенту: срочное присылаем, но без звука."""
    h = (now or datetime.now(timezone.utc)).astimezone(TASHKENT_TZ).hour
    return h >= 23 or h < 8


# ------------------------------------------------- настройки через бота ----

WELCOME_TEXT = i18n_texts.WELCOME["ru"]

HELP_TEXT = i18n_texts.HELP["ru"]

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


VIEW_HINT = i18n_texts.VIEW_HINT["ru"]


def render_view(cfg, settings, view: str, message_id=None) -> bool:
    """Старые экраны с кнопками больше не показываем: подсказываем, как сказать словами."""
    return send_telegram(cfg, t(VIEW_HINT))
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

def after_new_anketa(cfg, store, settings):
    """Новые параметры поиска (мини-апп, /start p…, разговор): аренда — фильтры радара по ним,
    подборка продажи от прошлого поиска сброшена, первые варианты — сразу из уже собранного."""
    ans = (store.get_kv("anketa") or {}).get("ans") or {}
    if ans.get("deal") != "rent":
        return False
    settings.update(concierge.rent_settings(ans, settings, cfg, store))
    store.set_kv("settings", settings)
    if cfg.get("card_queue") and _queue_store() is not None:
        recheck_open_card(cfg, _queue_store())           # открытая карточка под старые условия — убрать
    if sale_path().exists():
        try:
            sp_ = open_sale()
            sp_.set_kv("sale_pick", [])
            sp_.set_kv("last_pick", [])
            sp_.conn.close()
        except Exception as e:
            log.info("подборку продажи не очистила: %s", e)
    try:
        run_search(cfg, store, settings, days=14)
    except Exception as e:
        log.warning("первые варианты аренды не собрала: %s", e)
    return True


def sale_on(cfg, store) -> bool:
    try:
        return bool((effective_sale_cfg(cfg, store).get("sale_search") or {}).get("enabled"))
    except Exception:
        return False


def handle_command(text: str, settings: dict, store, cfg: dict):
    """Возвращает (ответ, view_или_None). view — какой экран показать кнопками."""
    txt_ = (text or "").strip()
    low = txt_.lower()
    cmd, _, arg = low.partition(" ")
    cmd = cmd.split("@")[0]
    cfg = effective_sale_cfg(cfg, store)       # покупка из чата меняет и поиск /sale
    arg = arg.strip()
    raw_arg = txt_.partition(" ")[2].strip()

    if cmd == "/start" and raw_arg.startswith("p"):
        # параметры из мини-аппа, открытого кнопкой меню (там нет sendData)
        ans = concierge.decode_start_code(raw_arg[1:])
        if not ans or not concierge.apply_webapp_data(
                cfg, store, json.dumps({"v": 2, "ans": ans}, ensure_ascii=False)):
            concierge.send_app_button(
                cfg, store, "Не получилось прочитать параметры — опишите поиск словами, я соберу заново.")
        else:
            after_new_anketa(cfg, store, settings)
        return "", None
    if cmd == "/start":
        # Первое касание: тёплое знакомство, одно понятное действие, честное
        # ожидание. Стена команд отпугивает — её показываем только по /help.
        concierge.send_app_button(cfg, store, t(WELCOME_TEXT))
        return "", None
    if cmd == "/help":
        return t(HELP_TEXT), None
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
    if cmd in ("/sources", "/kanallar", "/источники"):
        return sources_text(cfg, store), None
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
            "📋 Опишите своими словами, что ищете, — я уточню остальное.")
        return "", None
    if cmd == "/steps":
        return "📋 Пошаговой анкеты с кнопками больше нет — опишите своими словами, что ищете, я уточню остальное.", None
    if cmd in ("/shortlist", "/short"):
        concierge.show_shortlist(cfg, store)
        return "", None
    if cmd in ("/offers", "/varianty"):
        concierge.show_offers(cfg, store)
        return "", None
    if cmd in ("/prices", "/index"):
        return concierge.concierge_status(store), None
    if cmd in ("/request", "/text"):
        rq_ = store.get_kv("request_text")
        if not rq_:
            return "Текст запроса ещё не готов — расскажите, что ищем, и я его соберу ✍️", None
        return f"📝 <b>Текущий запрос маклерам</b>\n\n<code>{escape_html(rq_)}</code>", None
    if cmd in ("/brokers", "/makler", "/outreach"):
        if not concierge.broker_mode(store):        # сначала предлагаем подключить Telegram — маклеров пришлю после
            store.set_kv("brokers_pending", True)
            store.set_kv("biz_offered_at", time.time())
            send_telegram(cfg, t("📇 Маклеров подберу. Но сначала предлагаю подключить меня к вашему Telegram — "
                                 "тогда общаться с маклерами буду я, а не вы.") + "\n\n" + concierge.biz_guide(cfg, store) +
                          "\n\n" + t("Как подключите — сразу пришлю маклеров."))
            push_snapshot(cfg, store, settings, force=True)
            return "", None
        return send_broker_batch(cfg, store, settings), None   # ссылки с вписанным текстом, без кнопок
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
            return ("Не узнал: " + ", ".join(unknown) + ". Напишите районы через запятую, например "
                    "«Мирабад, Юнусабад».", None)
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

    if txt_.startswith("/"):
        return "Не знаю такую команду — пишите своими словами, я пойму. Что умею — /help.", None
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


CITY_PROFILE_DAYS = 120          # что считается центром — перепроверяем в интернете раз в ~4 месяца


def city_profile(cfg, city="Ташкент", force=False) -> dict:
    """Центр города: модель ищет в интернете, что там называют центром (граница, места внутри и вне),
    Ra'no проверяет названия по карте и считает середину и радиус. Хранится в sale.db; без интернета —
    оценка по размеру города в OpenStreetMap."""
    key = (city or "Ташкент").strip().lower()
    overrides = cfg.get("cities") or {}
    prof = None
    sst = open_sale()
    try:
        prof = sst.get_kv(f"city:{key}")
        age = age_days((prof or {}).get("at") or "")
        if prof and not force and age is not None and age < CITY_PROFILE_DAYS:
            geo.CITY_PROFILES[key] = prof
            return prof
        geo.CITY_PROFILES.pop(key, None)
        base = geo.city_center(city, overrides)
        facts = worker_post(cfg, "/svc/city", {"city": city}, timeout=120) if cfg.get("worker_url") else None
        if (facts or {}).get("ok") and (facts.get("in") or facts.get("radius_km")):
            market.ensure_tables(sst.conn)
            geo.ensure(sst)

            def resolve(name):              # свой справочник ориентиров (Ташкент) → карта OSM
                pt = geo.point_for(name) if key == "ташкент" else None
                return (pt["lat"], pt["lon"]) if pt else geo.geocode_in(name, base)
            prof = geo.center_from_research(base, facts, resolve)
            prof["anchor"] = facts.get("anchor") or ""
            prof["at"] = datetime.now(timezone.utc).isoformat()
            sst.set_kv(f"city:{key}", prof)
            log.info("[центр] %s: %s, радиус %.1f км (%s)", city, prof.get("boundary"), prof["center_km"], prof["checked"])
        else:
            prof = base                      # интернет не ответил — оценка по размеру, спросим позже
    finally:
        sst.conn.close()
    geo.CITY_PROFILES[key] = prof
    return prof


def near_point(cfg, store, text, km_=None, city="Ташкент"):
    """Ориентир клиента («метро Ойбек», «Ц-1») → точка и радиус для поиска. Кэш в kv радара;
    не нашли на карте — один раз скажем клиенту и ищем по районам."""
    text = " ".join(str(text or "").split())[:60]
    if not text:
        return None
    try:
        km_ = min(max(float(str(km_ or 2).replace(",", ".")), 0.3), 10.0)
    except (TypeError, ValueError):
        km_ = 2.0
    key = f"{geo.skel(text)}|{km_:g}|{city}"
    c = store.get_kv("near_point") or {}
    if c.get("key") == key:
        return c.get("pt")
    if geo.is_center_query(text):          # «центр» — что считается центром именно в этом городе
        prof = city_profile(cfg, city)
        pt = {"lat": prof["lat"], "lon": prof["lon"], "label": "центр", "src": "центр", "city": city,
              "km": prof["near_km"], "center_km": prof["center_km"]}
        pt["districts"] = geo.districts_within(pt["lat"], pt["lon"], pt["km"] + 0.5) if city.lower() == "ташкент" else []
        why = f' — {escape_html(prof["boundary"])}' if prof.get("boundary") else ""
        src = (prof.get("sources") or [None])[0]
        send_telegram(cfg, f"📍 <b>Центр {escape_html(city)}</b>{why}.\nИщу в центре и рядом с ним "
                           f"(до {str(pt['km']).replace('.', ',')} км от середины); что «у центра», а не в нём, — помечу."
                      + (f'\n<a href="{src}">Откуда это знаю</a>' if src else ""))
        store.set_kv("near_point", {"key": key, "pt": pt})
        return pt
    pt = None
    try:
        sst = open_sale()
        try:
            market.ensure_tables(sst.conn)
            geo.ensure(sst)
            pt = geo.point_for(text, sst, requests)
        finally:
            sst.conn.close()
    except Exception as e:
        log.warning("[место] «%s» не нашла: %s", text, e)
    if pt:
        pt["km"] = km_
        pt["districts"] = geo.districts_within(pt["lat"], pt["lon"], km_ + 0.5) or \
            [d for d in [geo.district_at(pt["lat"], pt["lon"], 1.5)] if d]
        log.info("[место] «%s» → %.5f, %.5f (%s), районы: %s", text, pt["lat"], pt["lon"], pt["src"],
                 ", ".join(pt["districts"]))
    else:
        send_telegram(cfg, f"📍 Не нашла на карте «{escape_html(text)}» — пока ищу по районам. "
                           "Напишите ориентир иначе (например, «метро Ойбек», «Госпитальный рынок», «Ц-1»).")
    store.set_kv("near_point", {"key": key, "pt": pt})
    return pt


def effective_sale_cfg(cfg, store):
    """Покупка из чата-интервью → поиск Uybor по тем же параметрам.
    Без районов в запросе — районы из настроек (по умолчанию центр)."""
    ans = (store.get_kv("anketa") or {}).get("ans") or {}
    if ans.get("deal") and (ans.get("deal") != "buy" or ans.get("object", "flat") != "flat"):
        # клиент ищет аренду (или не квартиру): поиск продажи выключен, а не «по настройкам по умолчанию»
        return {**cfg, "sale_search": {**(cfg.get("sale_search") or {}), "enabled": False}}
    if ans.get("deal") != "buy":
        if cfg.get("tenant"):                   # новый пользователь без параметров — не искать по настройкам владельца
            return {**cfg, "sale_search": {**(cfg.get("sale_search") or {}), "enabled": False}}
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
    near_txt = ans.get("near")
    if not near_txt and not ds and geo.is_center_query(str(ans.get("note") or "")):
        near_txt = "центр"                    # раньше «ближе к центру» лежало в пожеланиях; районы, выбранные явно, важнее
    city = ans.get("city_other") if ans.get("city") == "other" and ans.get("city_other") else "Ташкент"
    ss["near"] = near_point(cfg, store, near_txt, ans.get("near_km"), city=city) if near_txt else None
    areas = (store.get_kv("settings") or {}).get("areas") or []
    if areas:                                   # обвёл на карте — области вместо ориентира; выбранные районы — тоже
        ss["near"], ss["areas"] = None, areas
        ss["area_districts"] = ds or []
        try:
            sst = open_sale()
            geo.ensure(sst)
            sst.conn.close()
        except Exception:
            pass
        ss["districts"] = geo.areas_districts(areas) or list(DISTRICT_LIST)
    if ss["near"]:
        # «в центре», «прямо в центре» — строго: радиус самого центра и только объявления с точным местом.
        # «рядом с центром», «ближе к центру», «около» — мягко: центр и полоса вокруг.
        said = f'{ans.get("near") or ""} {ans.get("note") or ""}'.lower()
        soft = bool(re.search(r"рядом|около|недалеко|ближе|у центра|возле|поблизости|atrof|yaqin|near", said))
        nr = dict(ss["near"], strict=not soft)
        if nr["strict"] and nr.get("src") == "центр" and nr.get("center_km"):
            nr["km"] = nr["center_km"]
        if city.lower() == "ташкент":
            # районы — только чтобы спросить сайты; с запасом 2 км: сайты путают районы (Куйлюк с пометкой
            # «Мирабад»), а квартира в круге с «чужим» районом иначе до нас бы не дошла. Отсев — по точке.
            nr["districts"] = geo.districts_within(nr["lat"], nr["lon"], nr["km"] + 2.0) or nr.get("districts")
        ss["near"] = nr
    if ss["near"] and ss["near"].get("districts"):
        ss["districts"] = ss["near"]["districts"]      # сайты спрашиваем по районам круга, отсев — по радиусу
    ss["enabled"] = True
    import sale_sources                          # каналы, которые добавили пользователи
    base = list(ss.get("sale_channels") or sale_sources.SALE_CHANNELS)
    ss["sale_channels"] = live_channels(store, base + [c for c in user_channels(store, "sale")
                                                       if c.lower() not in {b.lower() for b in base}])
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
        if r.status_code == 404:                       # страниц стало меньше — каталог кончился
            page = 0
            break
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
            pump()
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
    if not ans.get("deal") and getattr(store, "tenant", False):   # пользователь ещё не сказал, что ищет
        return False
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
               "загляните через час или попросите меня написать маклерам" if deal == "sale" else
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


OUTREACH_BATCH = 5          # маклеров за раз
OUTREACH_DAY_MAX = 15       # и не больше в день: бережём аккаунт клиента от жалоб «спам»
BATCH_ONE_MSG_MAX = 3500    # подборка длиннее — по маклеру в сообщении


def send_broker_batch(cfg, store, settings, n=OUTREACH_BATCH) -> str:
    """Подбирает маклеров и присылает одним сообщением ссылки —
    нажали на имя, открылся чат с маклером, запрос уже вписан, осталось «Отправить». Что ушло — Ra'no видит
    сама (handle_business_update), дальше ведёт переписку. Кнопок нет."""
    text = store.get_kv("request_text") or concierge.compose_request(cfg, store)
    deal = request_deal(store)
    today = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    day = store.get_kv("outreach_day") or {}
    if day.get("d") != today:
        day = {"d": today, "n": 0}
    room = OUTREACH_DAY_MAX - day["n"]
    if room <= 0:
        return t("Сегодня уже дала {n} маклеров — на сегодня хватит, чтобы Telegram не счёл это рассылкой. "
                 "Завтра пришлю следующих, а пока жду ответы.", n=OUTREACH_DAY_MAX)
    pool = [b for b in ranked_brokers(store, deal) if b.get("tg") or len(re.sub(r"\D", "", b.get("phone") or "")) >= 9]
    if not pool:
        return outreach_empty_text(store, deal)
    pick = pool[:min(n, room)]
    want = request_districts(store)
    biz = concierge.biz_ready(store)
    lines = [t("📇 <b>Маклеры с похожими объявлениями</b> — {n} из {total}", n=len(pick), total=len(pool)),
             t("Список — по их объявлениям на сайтах (районы и цены как у вас); деньги от маклеров Ra'no не берёт и "
               "никого не продвигает. Кому писать — решаете вы: нажмите на имя — откроется чат, запрос уже вписан."),
             ""]
    for i, b in enumerate(pick, 1):
        ds = [d for d in b["districts"] if d in want][:2] or b["districts"][:2]
        bits = [", ".join(dist(d) for d in ds)] if ds else []
        if b.get("ads"):
            bits.append(t("{n} объявл.", n=b["ads"]) if i18n.LANG != "ru" else
                        f"{b['ads']} {concierge.plural(b['ads'], 'объявление', 'объявления', 'объявлений')}")
        if b.get("tg"):
            link = tg_user_link(b["tg"], text)
        else:
            digits = re.sub(r"\D", "", b["phone"])
            digits = "998" + digits if len(digits) == 9 else digits
            from urllib.parse import quote
            link = f"https://t.me/+{digits}?text={quote(text)}"
            bits.append(t("по номеру — если чат не откроется, значит, номер скрыт"))
            if not biz:                               # без подключения Telegram ответы всё равно приходят вам
                bits.append("WA")
        name = escape_html((b.get("name") or "Маклер").strip()[:40])
        wa = ""
        if "WA" in bits:
            bits.remove("WA")
            wa = f' · <a href="{wa_link(b["phone"], text)}">WhatsApp</a>'
        lines.append(f'{i}. <a href="{link}">{name}</a>' + (" — " + escape_html(" · ".join(bits)) if bits else "") + wa)
        store.broker_status(b["bid"], "offered")       # показан — второй раз не предложим
    left = len(pool) - len(pick)
    more = t(" Ещё {n} — напишите «ещё маклеров».", n=left) if left and room - len(pick) > 0 else ""
    mode = concierge.broker_mode(store)
    lines += ["", (t("Как отправите — я увижу сама и буду следить за ответами: соберу карточки и уточню детали.") if biz else
                   t("Маклеры ответят вам или сразу мне. Ответили вам — перешлите их сообщения мне "
                     "(держите сообщение → «Переслать» → Ra'no), сделаю карточки.")
                   + ("" if mode == "manual" else t(" Хотите, чтобы я читала ответы сама, — «подключить телеграм».")))
              + more]
    day["n"] += len(pick)
    store.set_kv("outreach_day", day)
    st_ = store.get_kv("search_start") or {}
    if st_ and not st_.get("offered"):            # маклеров уже дали — «давайте подключим маклеров?» не нужно
        st_["offered"] = datetime.now(timezone.utc).isoformat()
        store.set_kv("search_start", st_)
    msg = "\n".join(lines)
    opt = {"chat_id": cfg["telegram_chat_id"], "parse_mode": "HTML", "disable_web_page_preview": True}
    # В ссылках целиком вписан запрос (на двух языках) — пять таких в одном сообщении Telegram не пропускает
    # (ENTITIES_TOO_LONG). Поэтому длинную подборку шлём по маклеру в сообщении: шапка — в первом, подсказка — в последнем.
    if len(msg) <= BATCH_ONE_MSG_MAX and tg_call(cfg, "sendMessage", {**opt, "text": msg}) is not None:
        return ""
    items = lines[3:3 + len(pick)]
    tail = "\n".join(lines[3 + len(pick):]).strip()
    for i, item in enumerate(items):
        part = ([lines[0], lines[1], ""] if i == 0 else []) + [item]
        if i == len(items) - 1 and tail:
            part += ["", tail]
        if tg_call(cfg, "sendMessage", {**opt, "text": "\n".join(part)}) is None and "wa.me" in item:
            # две длинные ссылки (Telegram и WhatsApp) в одном сообщении — тоже бывает слишком; без WhatsApp
            part = [re.sub(r' · <a href="https://wa\.me[^"]*">WhatsApp</a>', "", x) for x in part]
            tg_call(cfg, "sendMessage", {**opt, "text": "\n".join(part)})
    return ""


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
                           "загляните через пару часов — подкину ещё.")
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
    if not sale_path().exists():
        return "Объявление не найдено"
    sst = open_sale()
    try:
        row = sst.conn.execute("SELECT data FROM listings WHERE key LIKE ?",
                               (key.replace("%", "") + "%",)).fetchone()
        if not row:
            return "Объявление не найдено"
        l = json.loads(row[0] or "{}")
        fresh = sale_sources.refresh_listing(l)      # у Joymee ссылки на фото живут 10 минут
        if sale_sources.photo_repair(cfg, l, sst) or fresh:
            sst.conn.execute("UPDATE listings SET data=? WHERE key=?", (sst.pack(l), l["key"]))
            sst.conn.commit()
        ids = send_card(cfg, l, format_sale_card(sst, l, cfg), ref="L:" + l["key"])
        if not ids:
            return "Не получилось отправить 🙈"
    finally:
        sst.conn.close()
    return "" if l.get("photo_urls") else "Фото у этого объявления нет — только описание"


# ------------------------------------------------ каналы от пользователей --
# Общая база источников: клиенты и маклеры присылают каналы, Ra'no проверяет и добавляет,
# владелец получает уведомление и может убрать. Хранится в radar.db (kv «user_channels»).

CH_PER_DAY = 10


def user_channels(store, deal=None) -> list:
    reg = store.get_kv("user_channels") or {}
    return [n for n, v in reg.items() if v.get("active", True) and not v.get("blocked")
            and (deal is None or v.get("deal") in (deal, "both"))]


def known_channels(cfg) -> set:
    import sale_sources
    base = set(((cfg.get("sources") or {}).get("telegram") or {}).get("channels") or [])
    return {c.lower() for c in base | set(sale_sources.SALE_CHANNELS)}


def handle_channel_suggestion(cfg, store, msg) -> int:
    """Пользователь прислал канал(ы): проверить, добавить в общую базу, ответить ему и сообщить владельцу."""
    import sale_sources
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    owner = str(chat_id) == str(cfg["telegram_chat_id"])
    who = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or "пользователь"
    day = datetime.now(TASHKENT_TZ).date().isoformat()
    used = store.get_kv(f"ch_sugg:{chat_id}:{day}") or 0
    reg = store.get_kv("user_channels") or {}
    base = known_channels(cfg)
    lines, added = [], 0
    for name in (msg.get("_channel") or [])[:5]:
        low = name.lower()
        ex = next((k for k in reg if k.lower() == low), None)
        if ex and reg[ex].get("blocked"):
            lines.append(f"• @{name} — этот канал уже смотрели, он не подошёл")
            continue
        if low in base or (ex and reg[ex].get("active", True)):
            lines.append(f"• @{name} — уже отслеживаю 👍")
            continue
        if used >= CH_PER_DAY and not owner:
            lines.append(f"• @{name} — на сегодня хватит, пришлите завтра 🙏")
            continue
        used += 1
        v = sale_sources.verify_channel(name)
        if not v.get("ok"):
            lines.append(f"• @{name} — не добавила: {v.get('reason')}")
            continue
        deal = v["deal"]
        reg[name] = {"deal": deal, "title": v.get("title"), "subs": v.get("subs"), "per_day": v.get("per_day"),
                     "by": who[:40], "by_chat": str(chat_id), "at": datetime.now(timezone.utc).isoformat(), "active": True}
        what = {"sale": "продажа", "rent": "аренда", "both": "продажа и аренда"}[deal]
        lines.append(f"• @{name} — ✅ добавила в поиск: {what}, ~{v.get('per_day')} постов в день")
        added += 1
        if not owner:
            tg_call(cfg, "sendMessage", {
                "chat_id": cfg["telegram_chat_id"], "parse_mode": "HTML", "disable_notification": True,
                "text": f"📡 <b>Новый источник</b> от {escape_html(who)}: "
                        f'<a href="https://t.me/s/{name}">@{escape_html(name)}</a> — {escape_html(v.get("title") or "")}'
                        f'{", " + escape_html(v["subs"]) + " подписчиков" if v.get("subs") else ""}, '
                        f"{what}, ~{v.get('per_day')} постов в день. Уже в поиске.",
                "reply_markup": json.dumps({"inline_keyboard": [[{"text": "🗑 Убрать", "callback_data": f"ch:rm:{name}"[:64]}]]},
                                           ensure_ascii=False)})
    store.set_kv("user_channels", reg)
    store.set_kv(f"ch_sugg:{chat_id}:{day}", used)
    head = "📡 <b>Спасибо!</b> " + ("Добавила в общую базу — теперь его видят все поиски Ra'no 🙌" if added
                                     else "Проверила:")
    tg_call(cfg, "sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "disable_web_page_preview": True,
                                 "text": head + "\n" + escape_html("\n".join(lines))})
    return added


CH_RECHECK_DAYS = 7


def channel_paused(store, name) -> bool:
    return bool(((store.get_kv("channel_health") or {}).get(name.lower()) or {}).get("paused"))


def live_channels(store, names) -> list:
    """Каналы без отключённых еженедельной проверкой."""
    h = store.get_kv("channel_health") or {}
    return [n for n in names if not (h.get(n.lower()) or {}).get("paused")]


def recheck_channels(cfg, store, now=None, force=False) -> dict:
    """Раз в неделю: каждый канал (встроенные и добавленные пользователями) проверяется заново.
    Замолчал больше месяца или закрылся — отключаем сразу; не прошёл проверку две недели подряд — тоже.
    Ожил — возвращаем. Сбой сети не в счёт. Владельцу — короткий отчёт, только если что-то поменялось."""
    import sale_sources
    now = now or datetime.now(timezone.utc)
    last = store.get_kv("ch_check_at") or 0
    if not force and time.time() - last < CH_RECHECK_DAYS * 86400:
        return {}
    store.set_kv("ch_check_at", time.time())
    base = list(((cfg.get("sources") or {}).get("telegram") or {}).get("channels") or []) + list(sale_sources.SALE_CHANNELS)
    names, seen = [], set()
    for n in base + [n for n, v in (store.get_kv("user_channels") or {}).items() if not v.get("blocked")]:
        if n.lower() not in seen:
            seen.add(n.lower()); names.append(n)
    health = store.get_kv("channel_health") or {}
    off, back = [], []
    for n in names:
        v = sale_sources.verify_channel(n, now=now)
        h = health.get(n.lower()) or {}
        if "Telegram не ответил" in (v.get("reason") or ""):
            continue                                  # сеть — не повод отключать
        if v.get("ok"):
            if h.get("paused"):
                back.append(n)
            h = {"ok": True, "fails": 0, "paused": False}
        else:
            prev = parse_iso(h.get("checked") or "")
            recent = prev is not None and (now - prev).total_seconds() < 5 * 86400
            fails = h.get("fails", 0) + (0 if recent and not h.get("ok", True) else 1)   # «две недели подряд» — не две проверки за день
            hard = (v.get("newest_days") or 0) > 30 or "группа" in (v.get("reason") or "")
            if not h.get("paused") and (hard or fails >= 2):
                off.append((n, v.get("reason")))
            h = {"ok": False, "fails": fails, "paused": h.get("paused") or hard or fails >= 2, "reason": v.get("reason")}
        h["checked"] = now.isoformat()
        health[n.lower()] = h
        time.sleep(1)
    store.set_kv("channel_health", health)
    if off or back:
        lines = ["📡 <b>Проверка источников</b> (раз в неделю)"]
        lines += [f"⏸ Отключила @{escape_html(n)} — {escape_html(r or '')}" for n, r in off]
        lines += [f"▶️ Вернула @{escape_html(n)} — снова живой" for n in back]
        n_live = len(live_channels(store, names))
        lines.append(f"\nСейчас в поиске Telegram-каналов: {n_live} из {len(names)}.")
        send_telegram(cfg, "\n".join(lines))
    return {"off": [n for n, _ in off], "back": back, "checked": len(names)}


def sources_text(cfg, store):
    reg = store.get_kv("user_channels") or {}
    act = [(n, v) for n, v in reg.items() if v.get("active", True) and not v.get("blocked")]
    lines = ["📡 <b>Источники</b>",
             f"Сайты: Uybor, Joymee, Realt24, Realting, Yangiuylar. Telegram-каналов: {len(known_channels(cfg)) + len(act)}.",
             "", "Знаете канал с объявлениями по Ташкенту? Пришлите ссылку (t.me/…) или @имя — проверю и добавлю."]
    paused = [n for n, h in (store.get_kv("channel_health") or {}).items() if h.get("paused")]
    if paused:
        lines += ["", "⏸ <b>Отключены проверкой</b> (молчат или без объявлений): " + ", ".join("@" + escape_html(n) for n in paused)]
    if act:
        lines += ["", "<b>Добавили пользователи:</b>"]
        what = {"sale": "продажа", "rent": "аренда", "both": "продажа и аренда"}
        for n, v in sorted(act, key=lambda x: x[1].get("at", ""), reverse=True)[:20]:
            lines.append(f'• <a href="https://t.me/s/{n}">@{escape_html(n)}</a> — {what.get(v.get("deal"), "")}, '
                         f'от {escape_html(v.get("by") or "")}')
    return "\n".join(lines)


def remove_channel(store, name) -> str:
    reg = store.get_kv("user_channels") or {}
    if name not in reg:
        return "Такого источника нет"
    reg[name].update(active=False, blocked=True)
    store.set_kv("user_channels", reg)
    return f"🗑 Убрала @{name} из поиска"


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
    sst = open_sale()
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
        sale_sources.photo_repair(cfg, l, sst)
        sc, why, _, _ = sale_sources.score(sst, l, cfg, ss)
        l["score"], l["why"] = sc, why
        before = sale_sources.seen_before(sst, l)
        if sst.known(l["key"]):
            sst.conn.execute("UPDATE listings SET data=?, notified=1 WHERE key=?", (sst.pack(l), l["key"]))
            sst.conn.commit()
        else:
            sst.save(l, notified=True)
        seen = ""
        if before:
            kinds = {"owner": "собственник", "agency": "маклер"}
            first = before[0]
            when = parse_iso(first.get("_first_seen") or "")
            seen = ("👀 <b>Уже видела</b>: " + ", ".join(
                f'<a href="{o.get("url")}">{escape_html(o.get("site") or "Uybor")}</a> — ${_money(o.get("price_usd") or 0)}'
                + (f' ({kinds[o["seller_kind"]]})' if o.get("seller_kind") in kinds else "") for o in before)
                + (f'. Впервые — {when.astimezone(TASHKENT_TZ).strftime("%d.%m %H:%M")}' if when else ""))
        if not l.get("price_usd"):
            seen = (seen + "\n" if seen else "") + "⚠️ Цену на странице не нашла — разбор неполный."
        send_card(cfg, l, format_sale_card(sst, l, cfg, note=seen), ref="L:" + l["key"])
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
    kb = {"inline_keyboard": [[{"text": "🔎 Похожие", "callback_data": f"L:sim:{l['key']}"[:64]},
                               {"text": "🎯 Искать такие", "callback_data": f"L:like:{l['key']}"[:64]}]]}
    send_card(cfg, l, text, ref="L:" + l["key"])
    if LIKE_WORDS.search(note or ""):
        send_similar_rent(cfg, store, sst, l)


def send_similar_rent(cfg, store, sst, l) -> int:
    found = sale_sources.similar_rent(store, sst, l)
    if not found:
        send_telegram(cfg, "🔎 Похожей аренды за последний месяц не нашла. Ответьте на карточку «ищи такие» — буду ловить новые.")
        return 0
    n, items = 0, found[:6]
    for i, o in enumerate(items, 1):                 # каждая — отдельной карточкой со своими фото
        if not o.get("title"):                       # строка среза Uybor — открыть объявление целиком
            try:
                full, _ = sale_sources.listing_from_url(cfg, o["url"])
            except Exception:
                full = None
            if not full:
                continue
            o = dict(full, key=rent_key(full), source=full.get("site") or "Uybor")
            o["price_usd"] = to_usd(o.get("price_value"), o.get("price_currency"), cfg)
        if not o.get("photo_urls"):
            continue                                 # без фото не показываем
        text = f"🔎 <b>Похожая аренда</b> · {i} из {len(items)}\n\n" + format_message(o, cfg, o.get("is_business") is True)
        kb = {"inline_keyboard": [[{"text": "🎯 Искать такие", "callback_data": f"L:like:{o['key']}"[:64]}]]}
        if not store.known(o["key"]):
            store.save(o, notified=True)
        n += bool(send_card(cfg, o, text, ref="L:" + o["key"]))
        time.sleep(1)
    return n


def send_similar(cfg, sst, l) -> int:
    found = sale_sources.similar(sst, l)
    if not found:
        send_telegram(cfg, "🔎 Похожих за последние полтора месяца не нашла. Ответьте на карточку «ищи такие» — "
                           "буду ловить новые и пришлю, как только появятся.")
        return 0
    for o in found:                       # чтобы 📷/👍 по номеру работали и для объявлений из среза Uybor
        if not sst.known(o["key"]):
            sst.save({**o, "text": o.get("text") or ""}, notified=True)
    top = [{"key": o["key"], "line": sale_sources.pick_line(o, o.get("why") or [])} for o in found[:5]]
    sst.set_kv("last_pick", top)
    return send_site_cards(cfg, sst, [o["key"] for o in found[:5]], title="Похожий вариант")


def search_like(cfg, store, l, settings=None) -> str:
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
        base = settings if settings is not None else {**default_settings(), **(store.get_kv("settings") or {})}
        new = concierge.rent_settings(ans, base, cfg, store)
        if settings is not None:               # тот же словарь сохранит и _process_updates — не затрём
            settings.update(new)
        store.set_kv("settings", new)
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
    try:
        if rent_search_on(store):              # что ищем в аренде — модель отвечает на «что ты ищешь?» по этому
            st_ = {**default_settings(), **(store.get_kv("settings") or {})}
            rm = (st_.get("rooms_min"), st_.get("rooms_max"))
            bits = [f"{rm[0]}–{rm[1]} комн" if rm[0] and rm[1] and rm[0] != rm[1] else f"{rm[0]} комн" if rm[0] else "",
                    f"до ${st_['max_price_usd']}" if st_.get("max_price_usd") else "",
                    (f"только внутри областей, обведённых на карте ({len(st_['areas'])}), и только с точным адресом — "
                     "районы не важны") if st_.get("areas") else
                    ("районы: " + ", ".join(st_["districts"])) if st_.get("districts") else "весь Ташкент",
                    "только от хозяев" if st_.get("owner_only", True) else "хозяева и маклеры"]
            ctx["rent_search"] = "аренда: " + ", ".join(b for b in bits if b)
            since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            ctx["rent_today"] = {"new": store.conn.execute("SELECT COUNT(*) FROM listings WHERE first_seen>?",
                                                           (since,)).fetchone()[0],
                                 "sent": store.conn.execute("SELECT COUNT(*) FROM listings WHERE first_seen>? AND "
                                                            "notified=1", (since,)).fetchone()[0]}
    except Exception:
        pass
    if sale_path().exists() and sale_on(cfg, store):        # аренда — подборок продажи нет
        sst = open_sale()
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
    ctx["brokers_written"] = q("SELECT COUNT(*) FROM brokers WHERE status IN ('contacted', 'offered')")
    return ctx


SRC_LABEL = {"realt24": "Realt24", "joymee": "Joymee", "realting": "Realting", "uybor": "Uybor",
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
        srcs = ", ".join(SRC_LABEL.get(n, n.capitalize()) for n, x in (cfg.get("sources") or {}).items()
                         if x.get("enabled")) or "—"
        rc = chat_context(cfg, store)
        what = (rc.get("rent_search") or "").replace("аренда: ", "")
        td = rc.get("rent_today") or {}
        return {"text": "🔎 <b>Ищет Ra'no</b> — сама смотрю объявления, без маклеров\n\n"
                        + (f"Ищу аренду: {escape_html(what)}.\n" if what else "")
                        + f"Слежу за: {escape_html(srcs)}. Подходящие присылаю сразу.\n"
                        + (f"За сутки новых объявлений {td.get('new', 0)}, подошло и прислала {td.get('sent', 0)}.\n"
                           if td else "")
                        + "Поиск покупки по всем сайтам включается, когда в чате вы ищете купить квартиру.",
                "kb": {"inline_keyboard": [[{"text": "✏️ Изменить, что ищем", "callback_data": "cmd:/mysearch"}]]}}
    st, pend, sent_total, stats = {}, 0, 0, {}
    if sale_path().exists():
        sst = open_sale()
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
             "Самые выгодные (ниже рынка, снижена цена) несу сразу. Остальные — в 19:30: лучшие, каждая своей карточкой с фото ✨",
             "", "Можно сказать: «покажи подборку», «проверь сайты сейчас», «цены рынка», «шортлист», «поменяй поиск»."]
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
    written = q("SELECT COUNT(*) FROM brokers WHERE status IN ('contacted', 'offered')")
    got = q("SELECT COUNT(*) FROM broker_offers WHERE broker_chat NOT LIKE 'site:%' AND status != 'message'")
    pool = q("SELECT COUNT(*) FROM broker_offers WHERE status IN ('new','later')")
    sl = q("SELECT COUNT(*) FROM broker_offers WHERE status IN ('shortlist','asked')")
    waiting = q("SELECT COUNT(*) FROM broker_offers WHERE status='asked' AND replied_at IS NULL")
    lines = ["📇 <b>Через маклеров</b> — запрос уходит маклерам, варианты приходят сюда карточками", "",
             f"Маклерам написали: {written} · прислали вариантов: {got}",
             f"🏠 Ждут вашего решения: {pool} · 📋 в шортлисте: {sl}"]
    if waiting:
        lines.append(f"⏳ Ждём ответа на уточнение: {waiting}")
    lines += ["", "Скажите «напиши маклерам» — пришлю 5 маклеров со вписанным запросом (до 15 в день, чтобы Telegram "
              "не счёл это спамом). Маклер ответил в WhatsApp — перешлите мне, сделаю карточку."]
    if not concierge.broker_mode(store):
        lines.append("Чтобы я сама читала ответы маклеров в ваших чатах — напишите «подключить телеграм».")
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
    written = store.conn.execute("SELECT COUNT(*) FROM brokers WHERE status IN ('contacted', 'offered')").fetchone()[0]
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
    texts = {"/help": t(HELP_TEXT), "/sale": sale_status_text(eff)}
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
    try:
        app = miniapp.app_snapshot(cfg, store, settings)          # мини-приложение: избранное, карта, просмотры
    except Exception as e:
        log.info("снимок мини-приложения: %s", e)
        app = None
    return {"app": app, "offers": offers, "offers_total": len(pool), "shortlist": shortlist, "written": written,
            "sl": sl, "sl_empty": concierge.SL_EMPTY, "cards": cards, "texts": texts,
            "screens": screens, "ctx": ctx,
            "free": cfg.get("free_offers", 2), "deal": deal,
            "brokers": brokers, "brokers_total": len(ranked), "biz": concierge.biz_ready(store),
            "broker_mode": concierge.broker_mode(store), "biz_offered_at": store.get_kv("biz_offered_at") or 0,
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
                          params={"chat": str(cfg["telegram_chat_id"])},
                          headers={"x-svc": cfg.get("worker_key", ""), "user-agent": "rano-radar/1.0"})
        ok = r.status_code == 200
    except requests.RequestException as e:
        log.warning("снимок не отправлен: %s", e)
        ok = False
    store.set_kv("snapshot_meta", {"at": now, "sig": sig if ok else "", "pushed": now if ok else last.get("pushed", 0)})
    return ok


def still_actual(cfg, l, older_h=20) -> str:
    """Свежо ли объявление сейчас: "ok" | "changed" (цена другая — уже обновили в l) | "gone" (сняли).
    Проверяем только старше older_h часов и только Uybor/Joymee (их открытые API); не вышло спросить — "ok"."""
    a = age_days(l.get("created_at") or "")
    url = str(l.get("url") or "")
    if a is not None and a * 24 < older_h:
        return "ok"
    try:
        if sale_sources.UYBOR_ID_RE.search(url) or sale_sources.JOYMEE_ID_RE.search(url):
            n, deal = sale_sources.listing_from_url(cfg, url)
            if not n:
                return "gone"
            if n.get("photo_urls"):            # ссылки на фото у Joymee живут минуты — берём свежие
                l["photo_urls"] = n["photo_urls"]
            pv, pc = n.get("price_value"), n.get("price_currency")
            if pv and (pv, pc) != (l.get("price_value"), l.get("price_currency")):
                old = l.get("price_usd")
                l["price_value"], l["price_currency"] = pv, pc
                l["price_usd"] = to_usd(pv, pc, cfg)
                if old and l["price_usd"] and abs(l["price_usd"] - old) >= 1:
                    l["price_note"] = f"цена изменилась: было ${old:,.0f}".replace(",", " ")
                    return "changed"
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (404, 410):
            return "gone"
    except Exception as e:
        log.info("не проверила, живо ли %s: %s", url, e)
    return "ok"


def run_search(cfg, store, settings, limit=5, days=7) -> str:
    """Анализирует всё, что накоплено за days дней, и присылает лучшее прямо сейчас.
    Свежесть — по тому же окну (не notify_max_age_days: тот предел — для новых уведомлений, иначе поиск
    «за 14 дней» молча отбрасывал всё старше трёх). Ничего не подошло — честно, сколько и почему отсеяно."""
    stats = analyst.market_stats(store)
    pool = store.recent(days=days)
    ok = []
    eff = effective_cfg(cfg, settings)
    areas = settings.get("areas") or []
    why = collections.Counter()
    shown = {r[0] for r in store.conn.execute("SELECT key FROM listings WHERE notified=1")}
    waiting = waiting_cards(_queue_store()) if cfg.get("card_queue") else set()
    already = waits = 0
    for l in pool:
        try:
            if areas:
                r_ = geo.area_reject(l, areas)
                if r_:
                    why["вне области" if r_.startswith("вне") else "без точного адреса"] += 1
                    continue
            if not passes_filters(l, eff, max_age=days):
                why["цена"] += 1
                continue
            if not passes_user_filters(l, settings):
                why["комнаты" if areas and not settings.get("districts") else "комнаты или район"] += 1
                continue
            rj = relevance_reject(l, cfg, settings, store)
            if rj:
                why["агентство или маклер" if rj.startswith("не хозяин") else "не та квартира"] += 1
                continue
            if l.get("key") in shown:          # уже присылала — второй раз ту же карточку не шлём
                if l.get("key") in waiting:    # в очереди «по одной» — ещё не показана
                    waits += 1
                else:
                    already += 1
                continue
            ok.append(analyst.score_listing(l, store, cfg, stats, settings))
        except Exception as e:
            log.warning("оценка %s не удалась: %s", l.get("key"), e)
    ok.sort(key=lambda x: x.get("score", 0), reverse=True)

    if not ok and waits:
        qs = _queue_store()
        n = waits + already
        send_telegram(cfg, f"🔎 Под эти условия подходит {n} {concierge.plural(n, 'вариант', 'варианта', 'вариантов')}"
                           + (f", {already} уже видели" if already else "")
                           + f". Ещё {waits} не показала — присылаю по одной: сохраните или пропустите, "
                             "и сразу придёт следующая 👇")
        if not reshow_open_card(cfg, qs, older_s=0):
            qs.set_kv("card_open", {})            # старая запись без самой карточки — показать следующую
            show_next_card(cfg, qs)
        return ""
    if not ok and already:
        fp = f"already:{already}:" + json.dumps(settings.get("max_price_usd"))
        last = store.get_kv("empty_search") or {}
        if last.get("fp") != fp or time.time() - float(last.get("at") or 0) >= 1800:
            store.set_kv("empty_search", {"fp": fp, "at": time.time()})
            send_telegram(cfg, f"🔎 Под эти условия за {days} дн. подходит {already} "
                               f"{concierge.plural(already, 'вариант', 'варианта', 'вариантов')} — все уже присылала "
                               "(выше в чате). Новые пришлю сразу, как появятся.")
        return ""
    if not ok:
        fp = json.dumps({k: settings.get(k) for k in ("areas", "districts", "rooms_min", "rooms_max", "max_price_usd",
                                                      "near", "owner_only")}, sort_keys=True, default=str)
        last = store.get_kv("empty_search") or {}
        if last.get("fp") == fp and time.time() - float(last.get("at") or 0) < 1800:
            return ""                          # то же самое только что говорила — не повторяю
        store.set_kv("empty_search", {"fp": fp, "at": time.time()})
        if areas:
            inside = len(pool) - why["вне области"] - why["без точного адреса"]
            parts = [f"{n} — {k}" for k, n in why.most_common() if k not in ("вне области", "без точного адреса")]
            txt = (f"🔍 Внутри области за {days} дн. — {inside} "
                   f"{concierge.plural(inside, 'объявление', 'объявления', 'объявлений')}, подходящих нет"
                   + (f" ({', '.join(parts)})" if parts else "") + ".")
            if why["без точного адреса"]:
                txt += (f"\nЕщё {why['без точного адреса']} — без точного адреса: проверить, внутри ли они, не могу, "
                        "поэтому не показываю.")
            txt += "\nМожно расширить область («Меню» → «Карта») или ослабить условия."
        else:
            txt = (f"🔍 За последние {days} дн. подходящих вариантов нет.\n"
                   f"Просмотрено {len(pool)}. Можно ослабить условия — напишите, например, «бюджет до 800» или «любой район».")
        send_telegram(cfg, txt)
        return ""
    store.set_kv("empty_search", {})

    # из накопленного за дни: объявление могли снять или поменять цену — проверяем до отправки
    fresh_ok, gone = [], 0
    for l in [x for x in ok if x.get("photo_urls")]:
        if len(fresh_ok) >= limit:
            break
        st_ = still_actual(cfg, l)
        if st_ == "gone" or (st_ == "changed" and not passes_filters(l, eff, max_age=days)):
            gone += 1
            store.conn.execute("UPDATE listings SET notified=2 WHERE key=?", (l["key"],))   # не предлагать снова
            store.conn.commit()
            continue
        fresh_ok.append(l)
    if not fresh_ok:
        send_telegram(cfg, f"🔍 Подходили {gone} {concierge.plural(gone, 'вариант', 'варианта', 'вариантов')}, но их уже "
                           "сняли или подняли цену выше бюджета. Новые пришлю сразу, как появятся.")
        return ""
    head = [f"🔎 <b>Лучшее сейчас</b> — {len(fresh_ok)} из {len(ok) - gone} новых подходящих "
            f"(просмотрено {len(pool)} за {days} дн." + (f"; ещё {already} уже присылала" if already else "")
            + (f"; {gone} уже сняли или подняли цену" if gone else "") + ")"]
    if cfg.get("card_queue") and _queue_store() is not None and len(fresh_ok) > 1:
        head.append("Показываю по одной: сохраните, пропустите или попросите уточнить — следующая придёт сразу.")
    med = stats["rooms"]
    if med:
        head.append("📊 Медианы: " + ", ".join(
            f"{r}-комн ${m:.0f}" for r, (m, n) in sorted(med.items())))
    send_telegram(cfg, "\n".join(head))

    for i, l in enumerate(fresh_ok, 1):         # только с фото
        l["title"] = f"#{i} · {l.get('title', '')}"
        if send_listing(cfg, settings, l, likely_makler=False):
            store.conn.execute("UPDATE listings SET notified=1 WHERE key=?", (l["key"],))
            store.conn.commit()
        time.sleep(1)
    qs = _queue_store() if cfg.get("card_queue") else None
    if qs is not None:                          # открытая карточка осталась выше заголовка — повторить её внизу
        reshow_open_card(cfg, qs)
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


def _listing_any(store, key):
    """Объявление по ключу: покупка — в sale.db, аренда — в базе радара."""
    for path, st in ((sale_path(), None), (None, store)):
        if path is not None and not path.exists():
            continue
        s_ = st or open_sale()
        try:
            row = s_.conn.execute("SELECT data FROM listings WHERE key=?", (key,)).fetchone()
        finally:
            if st is None:
                s_.conn.close()
        if row:
            return json.loads(row[0] or "{}")
    return None


def handle_callback(data: str, settings: dict, store, cfg: dict, message_id=None):
    """Возвращает (всплывающая_подсказка, view_для_перерисовки)."""
    d = data or ""
    if d.startswith("Q:"):                     # кнопки карточки из очереди
        return card_action(cfg, store, settings, d, message_id), None
    if d.startswith(("L:w:", "o:where:")):     # «где это?» про карточку — что известно о месте, честно
        send_telegram(cfg, where_text(cfg, store, d))
        return "", None
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
    if d in ("R:last", "R:pick") and not sale_on(cfg, store):
        return t("Сейчас ищу аренду — подборки продажи нет"), None
    if d == "R:last":                          # «покажи подборку ещё раз»
        if not sale_path().exists():
            return "Подборок ещё не было", None
        sst = open_sale()
        try:
            n = sale_sources.resend_last_pick(cfg, sst)
        finally:
            sst.conn.close()
        return ("" if n else "Подборок ещё не было — скоро будет 🙂"), None
    if d.startswith("ch:rm:"):                 # убрать канал из общей базы
        msg_ = remove_channel(store, d[6:])
        send_telegram(cfg, msg_)
        return msg_, None
    if d.startswith(("L:sim:", "L:like:")):    # похожие / искать такие же
        key = d.split(":", 2)[2]
        if not sale_path().exists():
            return "Объявление не найдено", None
        sst = open_sale()
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
        return search_like(cfg, store, l, settings), None
    if d.startswith(("L:a:", "L:loc:")):       # из чата: «уточни» по объявлению — в шортлист и спросить
        key = d.split(":", 2)[2]
        l = _listing_any(store, key)
        if not l:
            return "Объявление не найдено", None
        oid, _ = concierge.add_site_offer(cfg, store, l)
        if d.startswith("L:loc:"):             # ответ продавца проверим: внутри ли области
            concierge._set_extra(store, oid, loc_ask=True)
        return concierge.request_details(cfg, store, [oid]), None
    if d.startswith("L:v:"):                   # фото и разбор объявления из подборки
        return show_site_listing(cfg, d[4:].split("|")[0]), None
    if d.startswith("L:"):                     # объявление с сайта: в шортлист / мимо
        kind, key = d[2:3], d[4:].split("|")[0]
        row = None
        if sale_path().exists():
            sst = open_sale()
            try:
                row = sst.conn.execute("SELECT data FROM listings WHERE key LIKE ?",
                                       (key.replace("%", "") + "%",)).fetchone()
            finally:
                sst.conn.close()
        if not row and store is not None:      # аренда — в базе радара
            row = store.conn.execute("SELECT data FROM listings WHERE key=?", (key,)).fetchone()
        if not row:
            return "Объявление не найдено", None
        if kind == "n":                        # «мимо»: из подборки убрать, отметить — пригодится для оценки
            sst = open_sale()
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
        if not sale_path().exists():
            return "Подборка пуста", None
        sst = open_sale()
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
        send_telegram(cfg, t(HELP_TEXT))
        return "Справка отправлена", None
    return "", None


BROKER_ACK_SHORT = "Rahmat, получила — передаю клиенту 🙌\nRahmat, qabul qildim — mijozga yetkazaman."   # уже знакомы


def broker_ack(cfg) -> str:
    """Двуязычно (ру + уз): часть маклеров пишет только на узбекском. Имя клиента — ссылкой на его Telegram
    (подставляет concierge.broker_send), чтобы было видно, что клиент настоящий. Тот же текст — у воркера."""
    a = cfg.get("assistant_name", "Ra'no")
    return (f"Здравствуйте! Я {a}, ИИ-ассистент — ищу квартиру для своего клиента: {concierge.CLIENT}. "
            f"Варианты передаю ему.\n"
            f"Rahmat, получила! 🙌 Если зацепит — вернусь с вопросами. "
            f"Есть ещё что-то по параметрам — присылайте.\n\n"
            f"Assalomu alaykum! Men {a}, AI-yordamchiman — mijoz uchun uy-joy qidiryapman. "
            f"Rahmat, qabul qildim! Mos kelsa, savollar bilan qaytaman.")


BROKER_QUIET_SECONDS = 40      # маклер замолчал — значит, вариант дописан, показываем


def _offer_parts(msg):
    text = (msg.get("text") or msg.get("caption") or "").strip()
    ve = msg.get("venue") or {}
    loc = ve.get("location") or msg.get("location") or {}
    if loc.get("latitude") is not None:              # маклер прислал точку на карте (или место)
        place = " ".join(x for x in (ve.get("title"), ve.get("address")) if x)
        text = "\n".join(x for x in (text, f"📍 Локация: {loc['latitude']:.6f}, {loc['longitude']:.6f}"
                                      + (f" ({place})" if place else "")) if x)
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
    if frag and media_group:                 # текст, потом альбом с фото — это один вариант
        row = store.conn.execute("SELECT media_group, photos FROM broker_offers WHERE oid=?", (frag,)).fetchone()
        if row and not row[0] and row[1] in (None, "", "[]"):
            store.conn.execute("UPDATE broker_offers SET media_group=? WHERE oid=?", (media_group, frag))
            store.conn.commit()
            media_group = None
    if frag and not media_group:
        concierge.merge_into_offer(store, cfg, frag, text, photos)
        pend[str(frag)]["at"] = now
        store.set_kv("pending_offers", pend)
        return frag, False
    oid, is_new = concierge.save_offer(store, cfg, chat_key, name, text, photos, media_group)
    pend[str(oid)] = {"at": now, "group": group}
    store.set_kv("pending_offers", pend)
    return oid, is_new


BIZ_REQUEST_RE = re.compile(r"ИИ-помощник|sun'iy intellekt yordamchisi|AI-yordamchi|AI assistant", re.I)


def handle_business_update(cfg, store, upd) -> bool:
    """Telegram для бизнеса: клиент подключил Ra'no к своему аккаунту.
    Подключение — запоминаем и пишем клиенту, что дальше. Сообщения — только из чатов, куда клиент отправил
    запрос (в нём строка про ИИ-помощника); остальные чаты не читаем и не храним."""
    bc = upd.get("business_connection")
    if bc:
        owner = str(cfg["telegram_chat_id"])
        if str(bc.get("user_chat_id") or "") != owner:
            log.info("[бизнес] подключение не от владельца — пока не поддерживаем")
            return True
        rights = bc.get("rights") or {}
        can_reply = bool(rights.get("can_reply") or bc.get("can_reply"))
        prev = store.get_kv("biz_conn") or {}
        conn = {"id": bc.get("id"), "user_id": (bc.get("user") or {}).get("id"),
                "enabled": bool(bc.get("is_enabled")), "can_reply": can_reply, "at": time.time()}
        store.set_kv("biz_conn", conn)
        a = cfg.get("assistant_name", "Ra'no")
        if prev.get("enabled") and prev.get("can_reply") and not (conn["enabled"] and can_reply):
            try:                                   # «присылайте сюда» больше неправда — запрос со ссылкой на бота
                store.set_kv("request_text", concierge.compose_request(cfg, store))
                push_snapshot(cfg, store, {**default_settings(), **(store.get_kv("settings") or {})}, force=True)
            except Exception as e:
                log.info("[бизнес] запрос не пересобрала: %s", e)
        if not conn["enabled"]:
            send_telegram(cfg, t("🔌 {a} отключена от вашего Telegram — ответы маклеров в ваших чатах больше не вижу. "
                                 "Подключить снова: напишите «подключить телеграм».", a=a))
        elif not can_reply:
            send_telegram(cfg, t("⚠️ Почти готово: {a} подключена, но без права отвечать — уточнять у маклеров не смогу.\n"
                                 "Откройте Настройки → Telegram для бизнеса → Чат-боты → @{bot} и включите "
                                 "<b>Управление сообщениями</b> (Manage Messages).", a=a,
                                 bot=cfg.get("bot_username") or "rano_smart_bot"))
        elif not (prev.get("enabled") and prev.get("can_reply")):
            req = concierge.compose_request(cfg, store)
            store.set_kv("request_text", req)
            store.set_kv("biz_offered_at", 0)
            if store.get_kv("brokers_pending"):          # просили маклеров — присылаю сразу после подключения
                store.set_kv("brokers_pending", False)
                send_telegram(cfg, t("✅ <b>{a} подключена к вашему Telegram.</b> Ваши чаты, кроме маклерских, не читаю. "
                                     "Вот маклеры 👇", a=a))
                bt_ = send_broker_batch(cfg, store, default_settings())
                if bt_:
                    send_telegram(cfg, bt_)
                return True
            send_telegram(cfg, t("✅ <b>{a} подключена к вашему Telegram.</b>\n\nКак это работает: вы отправляете маклеру "
                                 "запрос — я вижу его ответ, собираю карточку с картой и сама уточняю фото, адрес и цену. "
                                 "Другие ваши чаты не читаю.\n\n", a=a)
                          + t("Скажите «напиши маклерам» — пришлю 5 маклеров под ваш запрос. Нажмёте на имя — откроется "
                              "чат, текст уже вписан, останется «Отправить». Дальше я сама.\n\nВот что они получат:\n")
                          + f"<code>{escape_html(req)}</code>")
        return True
    m = upd.get("business_message") or upd.get("edited_business_message")
    if not m:
        return bool(upd.get("deleted_business_messages"))
    conn = store.get_kv("biz_conn") or {}
    if not conn.get("id") or m.get("business_connection_id") != conn["id"]:
        return True
    peer = (m.get("chat") or {}).get("id")
    if not peer or (m.get("chat") or {}).get("type", "private") != "private":
        return True
    chats = store.get_kv("biz_chats") or {}
    k = f"biz:{peer}"
    outgoing = str((m.get("from") or {}).get("id") or "") == str(conn.get("user_id") or "")
    if outgoing:
        if m.get("sender_business_bot"):            # это наше же сообщение маклеру
            return True
        seen = store.get_kv("biz_out_seen") or {}    # для диагностики: исходящие клиента до нас доходят
        store.set_kv("biz_out_seen", {"n": int(seen.get("n", 0)) + 1, "at": time.time()})
        text = m.get("text") or m.get("caption") or ""
        oid_ = concierge.match_manual_ask(store, text)   # наш вопрос продавцу с сайта — ответ приложим к варианту
        if oid_:
            concierge.link_manual_ask(store, oid_, k)
            send_telegram(cfg, f"👀 Вижу ваш вопрос продавцу по варианту #{oid_} — как ответит, принесу сюда и "
                               "сама уточню остальное.")
        if k not in chats and (BIZ_REQUEST_RE.search(text) or oid_):
            ch = m.get("chat") or {}
            chats[k] = {"since": time.time(), "username": ch.get("username"),
                        "name": " ".join(x for x in [ch.get("first_name"), ch.get("last_name")] if x)}
            store.set_kv("biz_chats", chats)
            un = (ch.get("username") or "").lower()
            if un:                                   # маклер из нашей базы — отмечаем «написали»
                store.conn.execute("UPDATE brokers SET status='contacted', last_contact=? "
                                   "WHERE lower(ltrim(COALESCE(tg,''),'@'))=? AND status IN ('new', 'offered')",
                                   (datetime.now(timezone.utc).isoformat(), un))
                store.conn.commit()
            log.info("[бизнес] запрос ушёл маклеру %s", chats[k]["name"] or un or peer)
        return True
    if k not in chats:                               # чат без нашего запроса — не читаем…
        ch = m.get("chat") or {}
        how = biz_chat_fallback(store, ch, m)        # …если только это не ответ маклеру, которому мы дали ссылку
        if not how:
            return True
        chats[k] = {"since": time.time(), "username": ch.get("username"), "via": how,
                    "name": " ".join(x for x in [ch.get("first_name"), ch.get("last_name")] if x)}
        store.set_kv("biz_chats", chats)
        log.info("[бизнес] чат с маклером %s узнала по ответу (%s)", chats[k]["name"] or peer, how)
    if upd.get("edited_business_message"):
        return True
    handle_broker_message(cfg, store, m, key=k, quiet=True)
    return True


BIZ_FALLBACK_DAYS = 14


def biz_chat_fallback(store, ch: dict, m: dict) -> str:
    """Исходящий запрос клиента до нас мог не дойти (Telegram присылает их не всегда) — узнаём чат по ответу:
    это маклер из выданной подборки (тот же юзернейм) или ответ цитатой на наш запрос. Иначе — чужой чат, не читаем."""
    rep = m.get("reply_to_message") or {}
    if BIZ_REQUEST_RE.search(rep.get("text") or rep.get("caption") or ""):
        return "reply"
    un = (ch.get("username") or "").lower().lstrip("@")
    if not un:
        return ""
    since = (datetime.now(timezone.utc) - timedelta(days=BIZ_FALLBACK_DAYS)).isoformat()
    row = store.conn.execute(
        "SELECT bid FROM brokers WHERE lower(ltrim(COALESCE(tg,''),'@'))=? AND status IN ('offered', 'contacted') "
        "AND COALESCE(last_contact, first_seen, '') >= ?", (un, since)).fetchone()
    if not row:
        return ""
    store.conn.execute("UPDATE brokers SET status='contacted', last_contact=? WHERE bid=?",
                       (datetime.now(timezone.utc).isoformat(), row[0]))
    store.conn.commit()
    return "username"


GREETING_RE = re.compile(r"\W*(?:здравствуйте|здрасте|привет|добрый\s+(?:день|вечер)|доброе\s+утро|салом|салам|"
                         r"assalomu\s+alaykum|assalom|hello|hi|/?start)\W*", re.I)


def handle_broker_message(cfg, store, msg, key=None, quiet=False):
    """Маклер пишет боту напрямую — сохраняем вариант и показываем владельцу.
    key/quiet — маклер пишет в чат клиента (Telegram для бизнеса): ключ biz:<чат>, сами ему не отвечаем —
    уточнит concierge.biz_auto_ask после показа карточки."""
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    name = " ".join(x for x in [chat.get("first_name"), chat.get("last_name")] if x) \
        or chat.get("username") or "маклер"
    text, photos = _offer_parts(msg)
    if not text and not photos:
        return
    if quiet:
        k = key or f"biz:{chat_id}"
        i18n.broker_lang(store, k, text)            # на каком языке пишет маклер — на том и ответим
        if not photos and concierge.handle_viewing_reply(cfg, store, k, text):   # ответ про время просмотра
            return
        all_asked = store.get_kv("biz_asked") or {}
        asked = all_asked.get(k) or {}
        now_ = time.time()
        ans_at = asked.get("answered_at")
        if (asked.get("oid") and now_ - asked.get("at", 0) < 72 * 3600
                and (not ans_at or now_ - ans_at < 1800            # ответ — одним заходом, не через сутки
                     or not k.startswith("biz:"))                 # чат только про это объявление — всё к нему
                and not concierge.looks_like_new_offer(store, cfg, int(asked["oid"]), text)):
            # ответ на наши уточнения: дописываем в тот же вариант и покажем обновлённую карточку
            if not ans_at:
                asked["answered_at"] = now_
                all_asked[k] = asked
                store.set_kv("biz_asked", all_asked)
            o_ = concierge.get_offer(store, int(asked["oid"])) or {}
            if text and not photos and concierge.is_site_offer(o_):   # объявление с сайта — ответ сразу клиенту
                concierge.attach_answer(cfg, store, int(asked["oid"]), text)
                return
            concierge.merge_into_offer(store, cfg, int(asked["oid"]), text, photos)
            pend = store.get_kv("pending_offers") or {}
            pend[str(asked["oid"])] = {"at": time.time(), "group": f"chat:{k}", "update": True}
            store.set_kv("pending_offers", pend)
            return
        try:
            oid, is_new = intake_offer(cfg, store, f"chat:{k}", k, name, text, photos, msg.get("media_group_id"))
            log.info("вариант #%s из чата клиента от %s (%s)", oid, name, "новый" if is_new else "дополнение")
        except Exception as e:
            log.warning("не сохранил вариант из чата клиента %s: %s", chat_id, e)
        return

    if msg.get("_welcomed"):                 # воркер уже познакомил маклера с запросом
        store.set_kv(f"welcomed:{chat_id}", time.time())
        return
    if text:
        i18n.broker_lang(store, chat_id, text)      # на каком языке пишет маклер — на том и ответим
    # ответ на «можно посмотреть …?» — клиенту (раньше приветствий: «Да, можно» — короткий ответ)
    if not photos and not text.startswith("/") and concierge.handle_viewing_reply(cfg, store, chat_id, text):
        return
    # ответ на «уточните детали» — к своему варианту (раньше приветствий: «Да, актуален» — короткий ответ)
    if not photos and text and not text.startswith("/"):
        asked = concierge.pending_answer(store, chat_id)
        if asked and not GREETING_RE.fullmatch(text) and not concierge.looks_like_new_offer(store, cfg, asked, text):
            concierge.attach_answer(cfg, store, asked, text)
            if not msg.get("_acked"):
                tg_call(cfg, "sendMessage", {"chat_id": chat_id,
                                             "text": "Rahmat, передала клиенту! 🙌 / Mijozga yetkazdim!"})
            return
    # /start, «здравствуйте» — это не вариант: знакомим и показываем, что ищем
    if not photos and (text.startswith("/") or (len(text) < 25 and not re.search(r"\d", text))):
        last = store.get_kv(f"welcomed:{chat_id}") or 0
        if text.startswith("/") or time.time() - last > 6 * 3600:
            concierge.broker_send(cfg, store, chat_id, concierge.broker_welcome(cfg, store), translate=False)
            store.set_kv(f"welcomed:{chat_id}", time.time())
        return

    try:
        oid, is_new = intake_offer(cfg, store, f"chat:{chat_id}", chat_id, name, text, photos,
                                   msg.get("media_group_id"))
    except Exception as e:
        log.warning("не сохранил вариант от %s: %s", chat_id, e)
        return
    log.info("вариант #%s от %s (%s)", oid, name, "новый" if is_new else "дополнение")
    if is_new and not msg.get("_acked"):      # воркер уже ответил маклеру мгновенно
        known = time.time() - max(store.get_kv(f"welcomed:{chat_id}") or 0, store.get_kv(f"acked:{chat_id}") or 0) < 7 * 86400
        store.set_kv(f"acked:{chat_id}", time.time())
        concierge.broker_send(cfg, store, chat_id, BROKER_ACK_SHORT if known else broker_ack(cfg), translate=False)


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


def handle_seller_phone(cfg, store, msg):
    """Телефон на сайте скрыт (Uybor прячет его за проверкой) — клиент открыл объявление и прислал номер.
    Номер — к объявлению с карточки, на которую ответили (или открытой сейчас), и сразу вопрос продавцу."""
    ph = msg.get("_phone") or {}
    ref = str(ph.get("ref") or (store.get_kv("card_open") or {}).get("ref") or "")
    digits = re.sub(r"\D", "", str(ph.get("number") or ""))
    if len(digits) < 9:
        return
    if not ref.startswith("L:"):
        send_telegram(cfg, "📞 Чей это номер? Ответьте им на карточку варианта — и я напишу продавцу.")
        return
    l = _listing_any(store, ref[2:])
    if not l:
        send_telegram(cfg, "Не нашла это объявление 🙈 Пришлите ссылку на него — разберу заново.")
        return
    phone = "+" + (digits if digits.startswith("998") else "998" + digits[-9:])
    l["phones"] = [phone]
    oid, _ = concierge.add_site_offer(cfg, store, l)
    concierge._set_extra(store, oid, phones=[phone])
    concierge.request_details(cfg, store, [oid])


def handle_owner_offer(cfg, store, msg):
    """Владелец переслал или вставил вариант (из WhatsApp, другого чата) — в карточки."""
    text, photos = _offer_parts(msg)
    if not text and not photos:
        return
    pend = concierge.pending_manual_asks(store)
    fwd = bool(msg.get("forward_origin") or msg.get("forward_from") or msg.get("forward_sender_name"))
    if fwd and text and len(pend) == 1 and concierge.get_offer(store, pend[0]["oid"]):
        oid_ = pend[0]["oid"]                   # спрашивали одного продавца — пересланный ответ про его вариант
        concierge.link_manual_ask(store, oid_, f"owner:{forward_name(msg) or 'ответ'}")
        concierge.attach_answer(cfg, store, oid_, text)
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
    due_meta = {k: pend.get(k) or {} for k in due}
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
                concierge.enrich_offer(cfg, store, int(k), ai, fill_only=bool(due_meta.get(k, {}).get("update")))
            concierge.locate_offer(store, int(k))         # точка, ссылка на карту, координаты — для карты на карточке
            if due_meta.get(k, {}).get("update"):        # маклер ответил на уточнения в чате клиента
                send_telegram(cfg, f"💬 Маклер дослал по варианту #{k} — обновила карточку.")
            concierge.notify_offer(cfg, store, int(k))
            if request_deal(store) == "sale":
                send_offer_analysis(cfg, store, int(k), sale_store)
            concierge.biz_auto_ask(cfg, store, int(k))   # чат клиента: сама спрашиваю, чего не хватает
            concierge.manual_auto_ask(cfg, store, int(k))  # переслали: готовый текст уточнения клиенту
        except Exception as e:
            log.warning("не показал вариант %s: %s", k, e)


def send_offer_analysis(cfg, store, oid, sale_store=None) -> bool:
    """Покупка: к варианту маклера — тот же анализ цены, что к объявлениям Uybor."""
    o = concierge.get_offer(store, oid)
    if not o or not o.get("price_usd") or o["price_usd"] < 5000:
        return False
    own = sale_store is None
    st = sale_store or open_sale()
    try:
        l = {"key": f"offer:{oid}", "price_usd": o["price_usd"], "area": o.get("area"),
             "rooms": o.get("rooms"), "district": o.get("district"), "text": o.get("text") or "",
             "title": "", "created_at": o.get("created_at"), "floor": o.get("floor"),
             "repair": o.get("repair") or "",
             "lat": (o.get("extra") or {}).get("lat"), "lon": (o.get("extra") or {}).get("lon")}
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
    """Читает новые сообщения/нажатия, применяет их, отвечает. Очередь воркера одна на всех: каждое
    обновление — в контексте своего пользователя (tenants.route); без многих пользователей — всё владельцу."""
    settings = {**default_settings(), **(store.get_kv("settings") or {})}
    resp, offset_key = fetch_updates(cfg, store, long_poll)
    offset = store.get_kv(offset_key, 0)
    if not resp:
        return settings
    ups = resp.get("result", [])
    for upd in ups:
        offset = max(offset, upd.get("update_id", 0))
    groups = {}
    for upd in ups:
        groups.setdefault(tenants.route(upd), []).append(upd)
    owner = str(cfg["telegram_chat_id"])
    try:
        for chat, group in groups.items():
            t_ = tenants.get(chat)
            try:
                if t_ is None or t_.owner or chat == owner:
                    with tenants.ctx(tenants.REG["owner"] or tenants.Tenant(owner, cfg, owner=True, store=store)):
                        settings = _process_updates(cfg, store, group, settings)
                else:
                    with tenants.ctx(t_):
                        _process_updates(t_.cfg, t_.store, group, t_.settings())
            except Exception as e:                 # чужая ошибка не держит очередь остальных
                log.exception("[пользователи] %s: обновления не разобрала: %s", chat, e)
    finally:
        store.set_kv(offset_key, offset)       # иначе те же обновления придут снова и ответы задвоятся
        if not tenants.in_ctx():               # вне чужого контекста — работаем от имени владельца:
            concierge._BIZ_STORE["store"] = store   # ctx вернул прежнее (None) и прежний язык
            sync_lang(store)
    return settings


def _process_updates(cfg: dict, store, ups: list, settings: dict) -> dict:
    """Обновления одного пользователя (cfg — с его чатом, store — его база)."""
    changed = False
    snap = False                                  # шортлист/карточки поменялись — воркеру сразу
    concierge._BIZ_STORE["store"] = store          # подключён ли Telegram для бизнеса — для отправки маклерам
    sync_lang(store)                                # язык клиента — для карточек и подсказок
    def _one(upd):
        nonlocal changed, snap
        frm = ((upd.get("message") or upd.get("edited_message") or upd.get("callback_query") or {}).get("from")
               or {})
        if str(frm.get("id") or "") == str(cfg["telegram_chat_id"]):
            concierge.remember_client(store, frm)      # имя и ник — для представления маклерам
        if any(upd.get(x) for x in ("business_connection", "business_message", "edited_business_message",
                                     "deleted_business_messages")):
            try:
                handle_business_update(cfg, store, upd)
            except Exception as e:
                log.warning("[бизнес] не разобрала обновление: %s", e)
            return

        cb = upd.get("callback_query")
        if cb:
            msg = cb.get("message") or {}
            if str((msg.get("chat") or {}).get("id") or "") != str(cfg["telegram_chat_id"]):
                return
            if cb.get("_worker_done"):          # воркер уже ответил и показал следующее — сохраняем
                apply_worker_done(cb.get("data") or "", cfg, store)
                changed = True
                return
            toast, view = handle_callback(cb.get("data") or "", settings, store, cfg,
                                          msg.get("message_id"))
            try:                                # решили словами про открытую карточку — показать следующую
                card_followup(cfg, store, cb.get("data") or "")
            except Exception as e:
                log.info("очередь карточек: %s", e)
            snap = snap or (cb.get("data") or "")[:2] in ("o:", "s:", "t:")
            # подсказка-«всплывашка»; для старых нажатий Telegram её отклоняет — это нормально
            tg_call(cfg, "answerCallbackQuery",
                    {"callback_query_id": cb.get("id"), "text": toast}, quiet=True)
            if view:
                render_view(cfg, settings, view, msg.get("message_id"))
            changed = True
            log.info("Кнопка: %s → %s", (cb.get("data") or "")[:30], toast[:40])
            return

        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if msg.get("_channel"):              # прислали канал — владелец или маклер
            handle_channel_suggestion(cfg, store, msg)
            return
        if str(chat.get("id") or "") != str(cfg["telegram_chat_id"]):
            handle_broker_message(cfg, store, msg)   # это маклер прислал вариант
            return

        if msg.get("_biz_guide"):            # «подключи к моему телеграму» — пошаговая инструкция
            send_telegram(cfg, concierge.biz_guide(cfg, store))
            return
        if msg.get("_biz_decline"):          # «не хочу давать доступ» — работаем через пересылку
            store.set_kv("broker_mode", "manual")
            store.set_kv("biz_offered_at", 0)
            send_telegram(cfg, t(concierge.MANUAL_HOWTO) if not concierge.biz_ready(store) else
                          "Ra'no сейчас подключена к вашему Telegram. Чтобы отключить: Настройки → Telegram для "
                          "бизнеса → Чат-боты → Ra'no → «Отключить бота». Дальше буду работать через пересылку.")
            if store.get_kv("brokers_pending") and not concierge.biz_ready(store):
                store.set_kv("brokers_pending", False)
                bt_ = send_broker_batch(cfg, store, settings)
                if bt_:
                    send_telegram(cfg, bt_)
            push_snapshot(cfg, store, settings, force=True)
            return
        if "_areas" in msg:                  # обвёл на карте (воркер проверил подпись Telegram)
            areas = [a for a in (msg.get("_areas") or []) if isinstance(a, list) and len(a) >= 4][:6]
            settings["areas"] = areas
            store.set_kv("settings", settings)
            store.set_kv("sale_force", True)          # пересмотреть отсеянное по новым границам
            changed = True
            if cfg.get("card_queue") and _queue_store() is not None:
                recheck_open_card(cfg, _queue_store())
            if areas:
                want = settings.get("districts") or []
                sq = sum(geo.area_km2(a) for a in areas)
                txt = t("🗺 <b>Сохранила: областей — {n}.</b> Ищу только внутри них и только объявления с точным адресом",
                        n=len(areas))
                txt += (t(" — и только в выбранных районах: {d}.", d=", ".join(want)) if want else ".")
                if sq >= 35:
                    txt += t("\nОбласть большая — около {k} км², это почти весь центр. Чтобы точнее, обведите поменьше.",
                             k=round(sq))
                send_telegram(cfg, txt + t(" Поменять или убрать — «Меню» → «Карта»."))
                if rent_search_on(store):
                    try:
                        run_search(cfg, store, settings, days=14)
                    except Exception as e:
                        log.warning("варианты внутри областей не собрала: %s", e)
            else:
                send_telegram(cfg, t("🗺 Области убрала — ищу как раньше, по вашим условиям."))
            push_snapshot(cfg, store, settings, force=True)
            return
        if msg.get("_link"):                 # воркер: владелец прислал ссылку на объявление
            handle_link(cfg, store, msg)
            return
        if msg.get("_app"):                  # действие из мини-приложения (подпись проверил воркер)
            if miniapp.handle_action(cfg, store, settings, msg["_app"]):
                changed = snap = True
            return
        if msg.get("_owner_offer"):          # воркер: владелец переслал/вставил вариант
            handle_owner_offer(cfg, store, msg)
            changed = True
            return
        if msg.get("_phone"):                # клиент прислал номер продавца (на сайте он скрыт)
            handle_seller_phone(cfg, store, msg)
            changed = True
            return
        for tag in ("_view", "_note"):       # объявление с сайта (ответ на карточку) — сначала в шортлист
            v = msg.get(tag) or {}
            if v.get("key") and not v.get("oid"):
                l_ = _listing_any(store, v["key"])
                if l_:
                    v["oid"] = concierge.add_site_offer(cfg, store, l_)[0]
        if msg.get("_view"):                 # воркер разобрал время просмотра и уже ответил
            v = msg["_view"]
            if concierge.set_viewing(cfg, store, int(v.get("oid") or 0), v.get("at"), v.get("label", ""),
                                     v.get("notime"), msg.get("text") or ""):
                concierge.arrange_viewing(cfg, store, int(v.get("oid") or 0))   # договориться с маклером
            changed = snap = True
            return
        if msg.get("_note"):                 # заметка к варианту — воркер уже подтвердил
            concierge.add_note(store, int(msg["_note"].get("oid") or 0),
                               msg["_note"].get("text") or msg.get("text") or "")
            changed = snap = True
            return

        wad = msg.get("web_app_data") or {}
        if wad.get("data"):
            if concierge.apply_webapp_data(cfg, store, wad["data"]):
                log.info("параметры получены из мини-аппа")
                changed = True
                after_new_anketa(cfg, store, settings)
                push_snapshot(cfg, store, settings, force=True)   # новый текст запроса — в ссылки маклерам
            return

        text = (msg.get("text") or "").strip()
        if store.get_kv("awaiting_text") and text and not text.startswith("/"):
            store.set_kv("request_text", text)
            store.set_kv("awaiting_text", False)
            push_snapshot(cfg, store, settings, force=True)
            tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"],
                                         "text": "✅ Текст запроса сохранён. Скажите «напиши маклерам» — подберу маклеров с этим текстом."})
            changed = True
            return
        if not text:
            return
        reply, view = handle_command(text, settings, store, cfg)
        if reply:
            send_telegram(cfg, reply)
        if view:
            render_view(cfg, settings, view)
        if reply or view:
            changed = True
            log.info("Команда: %s", text[:50])
    for upd in ups:
        try:
            _one(upd)
        except Exception as e:            # одно «сломанное» обновление не должно ронять бота для всех
            log.exception("обновление %s не разобрала: %s", upd.get("update_id"), e)
    if changed:
        store.set_kv("settings", settings)
    if snap:
        push_snapshot(cfg, store, settings, force=True)
    return settings


def passes_filters(l: dict, cfg: dict, max_age=None) -> bool:
    """Цена и свежесть. max_age — свой предел (поиск по накопленному за N дней), иначе notify_max_age_days."""
    l["price_usd"] = to_usd(l.get("price_value"), l.get("price_currency"), cfg)
    p = l["price_usd"]
    if p is not None:
        if cfg["max_price_usd"] and p > cfg["max_price_usd"]:
            return False
        if cfg["min_price_usd"] and p < cfg["min_price_usd"]:
            return False
    a = age_days(l.get("created_at") or "")
    lim = max_age if max_age is not None else cfg["notify_max_age_days"]
    if a is not None and lim and a > lim:
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
    if settings.get("min_photos") and len(l.get("photo_urls") or []) < int(settings["min_photos"]):
        return False                         # «мало фото» — клиент сам попросил строже
    if settings.get("repair_min") and poor_repair(l):
        return False                         # «ремонт не тот» — без черновой и «требует ремонта»
    if settings.get("areas"):                # обвёл на карте — внутри области И в выбранных районах (если выбраны)
        try:
            if not l.get("geo"):
                geo.locate(l)
        except Exception:
            pass
        if geo.area_reject(l, settings["areas"]):
            return False
        return geo.district_ok(l, settings.get("districts"))
    near = settings.get("near") or {}
    if near.get("lat") is not None:          # аренда у ориентира — по радиусу
        try:
            if not l.get("geo"):
                geo.locate(l)
        except Exception:
            pass
        return not geo.too_far(l, near)
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


POOR_REPAIR_RE = re.compile(r"черновая|без ремонта|требует ремонта|под ремонт|старый ремонт|косметический ремонт нужен|"
                            r"ta'mir talab|ta'mirsiz|qora suvoq|remont kerak", re.I)


def poor_repair(l) -> bool:
    """Ремонт явно слабый: так указано на сайте (Uybor: черновая / требует ремонта) или в тексте объявления."""
    if str(l.get("repair") or "").lower() in ("chernovaya", "kapital"):
        return True
    return bool(POOR_REPAIR_RE.search(f'{l.get("title", "")} {l.get("text", "")}'))


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
    try:                                     # где квартира: координаты сайта или ориентир из текста
        geo.ensure(store)
        geo.locate(l)
    except Exception as e:
        log.info("[место] %s: %s", l.get("key"), e)
    a = age_days(l.get("created_at") or "")      # дешёвые проверки — до поиска места моделью
    if a is not None and a > ss.get("notify_max_age_days", 14):
        return f"объявлению {a:.0f} дн.", False
    low = f"{l.get('title', '')} {l.get('text', '')}".lower()
    hit = next((w for w in SALE_NOT_FLAT_WORDS if w in low), "")
    if hit:
        return f"продаётся не квартира («{hit}»)", False
    near = ss.get("near") or {}
    if ss.get("areas") or near.get("lat") is not None:   # области с карты или радиус у ориентира — решает точка
        if not ((l.get("geo") or {}).get("src") not in (None, "район")):
            # точки нет — ищем так же, как для карточки (описание читает модель), иначе в фильтре
            # квартира «где-то в районе», а в карточке потом оказывается за 10 км
            try:
                l.pop("geo", None)
                ensure_place(cfg, store, l)
            except Exception as e:
                log.info("[место] %s: %s", l.get("key"), e)
        far = geo.area_reject(l, ss["areas"]) if ss.get("areas") else geo.too_far(l, near)
        if not far and ss.get("areas") and not geo.district_ok(l, ss.get("area_districts")):
            return f"район {l.get('district')} — внутри области, но не из выбранных", False
        if far:
            if not (geo.loc_unsure(l, ss.get("areas"), near) and can_ask_seller("L:" + (l.get("key") or ""), l)):
                return far, False
            l["_loc_ask"] = True               # адрес неточный, но район подходит — спросим продавца
    elif ss.get("districts") and l.get("district") not in ss["districts"]:
        return f"район {l.get('district') or 'не указан'}", False

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


# ------------------------------------------- карточка покупки: одно сообщение --
# Коллаж из фото + подпись (≤1024 символа у Telegram) + кнопки. Вывод сверху, факты
# короткими строками без эмодзи в начале, экономика тремя строками, остальное — в «Подробнее».

CAPTION_MAX = 1000
TESTING = False         # тесты: без запросов адреса по карте          # лимит Telegram — 1024 видимых символа (UTF-16); держим запас


def tg_len(s: str) -> int:
    """Сколько символов Telegram насчитает в подписи: без тегов, с раскрытыми &amp;, в UTF-16."""
    t = html_lib.unescape(re.sub(r"<[^>]+>", "", s or ""))
    return len(t.encode("utf-16-le")) // 2


def _cut(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:max(0, n - 1)].rsplit(" ", 1)[0].rstrip(",.;:—- ") + "…"


def _desc_clean(text: str) -> str:
    """Описание продавца без хэштегов и телефонов (телефон — отдельной строкой)."""
    t = re.sub(r"#\S+", " ", text or "")
    t = re.sub(r"(?:\+?998[\s\-()]*)?\d{2}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}", " ", t)
    return " ".join(t.split())


def ensure_place(cfg, store, l: dict):
    """Где квартира — для карты первым фото. По порядку: координаты сайта → ориентир, метро и знакомые
    названия в тексте → модель читает описание и находит адрес/ориентир → хотя бы район (круг «где-то тут»)."""
    if l.get("geo"):
        return l["geo"]
    if (l.get("key") or "").startswith("sale:realting:") and l.get("lat") is None and not l.get("rt_detail") \
            and not TESTING:
        try:                                        # у Realting точка и полный текст — на странице объявления
            import sale_sources
            sale_sources.refresh_listing(l)
        except Exception as e:
            log.info("[место] %s: %s", l.get("key"), e)
    try:
        geo.ensure(store)
        if geo.locate(l):
            return l["geo"]
    except Exception as e:
        log.info("[место] %s: %s", l.get("key"), e)
    if not l.get("geo_tried") and cfg.get("worker_url") and (l.get("text") or l.get("title")) and not TESTING:
        l["geo_tried"] = True                       # модель спрашиваем один раз на объявление — и на всех клиентов
        facts = tenants.memo_get(store, "sale_parse", l["key"]) if l.get("key") else None
        if facts is None:
            r = worker_post(cfg, "/svc/parse", {"text": f'{l.get("title") or ""}\n{l.get("text") or ""}'[:2500],
                                                "photos": [], "deal": "sale"}, timeout=40)
            facts = (r or {}).get("offer") or {}
            if r and l.get("key"):
                tenants.memo_put(store, "sale_parse", l["key"], facts)
        for name, approx in ((facts.get("landmark"), 0.6), (facts.get("address"), 0.4)):
            if not name or geo.generic_place(str(name)):
                continue                             # «Мечеть», «школа», «базар» — такие есть в каждом квартале
            pt = geo.point_for(str(name))
            if not pt:
                try:
                    pt_ = geo.geocode_in(str(name), geo.city_center("Ташкент"))
                except Exception:
                    pt_ = None
                pt = {"lat": pt_[0], "lon": pt_[1]} if pt_ else None
            if pt and geo.in_city(pt["lat"], pt["lon"]):
                dd = geo.district_at(pt["lat"], pt["lon"], 1.2)
                if not (l.get("district") and dd and dd != l["district"]):
                    l["geo"] = {"lat": pt["lat"], "lon": pt["lon"], "src": "ориентир", "spread": approx,
                                "label": geo.short_label(str(facts.get("landmark") or name))}
                    return l["geo"]
    d = l.get("district")                            # последнее — район: круг там, где его объявления
    pts = [(r[0], r[1]) for r in geo.GAZ["pts"] if r[2] == d] if d else []
    if len(pts) >= 10:
        import statistics as _st
        la, lo = _st.median(p[0] for p in pts), _st.median(p[1] for p in pts)
        spread = sorted(geo.km(la, lo, *p) for p in pts)[int(len(pts) * 0.6)]
        l["geo"] = {"lat": la, "lon": lo, "src": "район", "spread": round(min(3.0, max(1.0, spread)), 1), "label": ""}
        return l["geo"]
    return None


def format_sale_card(store, l: dict, cfg: dict, head: str = "", note: str = "") -> str:
    """Карточка + разбор цены одним текстом для подписи к коллажу."""
    try:
        ensure_place(cfg, store, l)
    except Exception as e:
        log.info("[место] %s: %s", l.get("key"), e)
    try:
        x = market.analyze(store, l, cfg) if l.get("price_usd") else {}
    except Exception as e:
        log.info("[продажа] анализ %s не удался: %s", l.get("key"), e)
        x = {}
    if x and not x.get("price"):
        x = {}
    top = []
    if head:
        top += [head, ""]
    if x.get("verdict"):
        em, title, why = x["verdict"]
        top += [f"{em} <b>{escape_html(title)}</b> — {escape_html(why)}", ""]

    # факты
    spec = []
    if l.get("rooms"):
        spec.append(t("{n}-комн", n=l["rooms"]))
    if l.get("area"):
        spec.append(t("{a} м²", a=fmt_area(l["area"])))
    if l.get("floor"):
        spec.append(t("этаж {f}", f=str(l["floor"]) + (f'/{l["floors_total"]}' if l.get("floors_total") else "")))
    if spec:
        top.append("<b>" + " · ".join(spec) + "</b>")
    g = l.get("geo") or {}
    where = dist(l.get("district") or "") or t("район не указан")
    street = ""
    if g.get("src") == "сайт" and not TESTING:     # точный адрес — улица и махалля по карте
        try:
            street = geo.reverse(g["lat"], g["lon"])
        except Exception:
            street = ""
    if street:
        where += ", " + street
    if g.get("label") and not g["label"].startswith("м. "):
        where += t(" (у «{label}»)", label=g["label"]) if g.get("src") == "сайт" else f', {g["label"]}'
    nb = l["_nb"] if "_nb" in l else l.get("new_building")
    place = [where, t("новостройка") if nb else t("вторичка"),
             FOUNDATION_RU.get(l.get("house_type") or "", l.get("house_type") or "")]
    top.append(escape_html(" · ".join(p for p in place if p)))
    pline, pdet = market.place_lines(x, l) if x else ("", "")
    if pline:
        top.append(escape_html(pline))
    p = l.get("price_usd")
    if p:
        price = f"<b>${_money(p)}</b>"
        if l.get("price_listed_usd"):                # переуступка / рассрочка: в объявлении — не вся цена
            price = t("≈ {p} всего", p=price)
        elif (l.get("price_currency") or "").upper() == "UZS" and l.get("price_value"):
            price = "<b>" + t("{mln} млн сум", mln=market._mln(l["price_value"])) + f"</b> (~${_money(p)})"
        if l.get("area"):
            price += f' · ${_money(p / l["area"])}/м²'
        if x.get("fair") and x.get("gap") is not None:
            price += " · " + t("рынок ≈ ${v}", v=_money(market._r500(x["fair"])))
        if l.get("price_listed_usd") and l.get("price_note"):
            price += "\n" + escape_html(l["price_note"])
        top.append(price)

    # ремонт
    short = {"good": t("хороший"), "average": t("средний"), "none": t("без ремонта"), "box": t("коробка")}
    cls, rsrc, claimed = x.get("repair"), x.get("repair_src"), x.get("repair_claimed")
    by = {"фото": t("по фото"), "описание": t("по описанию"), "пометка сайта": t("по пометке сайта")}.get(rsrc, "")
    if cls:
        r = t("Ремонт {cls}", cls=short[cls]) + (f" ({by})" if by else "")
        if x.get("repair_as_box"):
            r = (t("Ремонт коробка ({by}: «без ремонта» в новостройке)", by=by) if by else
                 t("Ремонт коробка (новостройка без ремонта)"))
        if cls == "good":
            r += t(" — можно заезжать")
        elif x.get("reno"):
            r += t(" — освежить ~${v}" if cls == "average" else " — ремонт ~${v}", v=_money(x["reno"]))
        if rsrc == "фото" and claimed and claimed != cls:
            r += t("; в объявлении «{c}»", c=short[claimed])
        top.append(r)
    elif rsrc == "строится":
        yr = (l.get("offplan") or {}).get("year")
        top.append(t("Дом строится, сдача {y} — фото квартиры ещё нет", y=yr)
                   + (t("; ремонт обещан по договору: «{c}»", c=short[claimed]) if claimed else ""))
    elif rsrc == "нет фото":
        top.append((t("Ремонт: в объявлении «{c}», но на фото только планировки и документы — попросите фото комнат",
                      c=short[claimed]) if claimed else
                    t("Фото комнат нет — только планировки и документы; попросите фото у продавца")))
    elif x:
        top.append(t("Ремонт не понять — уточните у продавца"))
    elif l.get("repair"):
        top.append("Ремонт: " + escape_html(REPAIR_RU.get(l["repair"], l["repair"])))

    # продавец и время
    who = {"owner": t("Собственник"), "agency": t("Маклер / агентство")}.get(l.get("seller_kind"),
                                                                            t("Продавец не проверен"))
    if l.get("seller_hint") == "developer":
        who = t("Застройщик")
    if (l.get("offplan") or {}).get("resale"):
        who += t(" (переуступка договора)")
    if l.get("district_site") and l.get("district_by") == "точка":
        who_d = t("Район по точке на карте — {d} (на сайте указан {s})", d=dist(l["district"]), s=dist(l["district_site"]))
    elif l.get("district_site"):
        who_d = t("Район по тексту объявления — {d} (на сайте ошибочно: {s})", d=dist(l["district"]), s=dist(l["district_site"]))
    else:
        who_d = ""
    if l.get("phones"):
        who += " · " + fmt_phone(l["phones"][0])
    top.append(who)
    if who_d:
        top.append(who_d)
    dt = parse_iso(l.get("created_at") or "")
    since = parse_iso(l.get("listed_since") or "")
    when = []
    if dt:
        when.append(t("выложено {d}", d=dt.astimezone(TASHKENT_TZ).strftime("%d.%m %H:%M") if i18n.LANG != "ru"
                      else dt.astimezone(TASHKENT_TZ).strftime("%d.%m в %H:%M")))
    if since:
        days = age_days(l["listed_since"])
        when.append(t("перевыложено") + (t(", на рынке ~{n} дн.", n=f"{days:.0f}") if days else ""))
    if when:
        top.append(when[0][0].upper() + ", ".join(when)[1:])
    alts = [a for a in (l.get("alts") or []) if a.get("price_usd") and a.get("url")]
    if alts:
        kinds = {"owner": ", собств.", "agency": ", маклер"}
        refs = [f'<a href="{a["url"]}">${_money(a["price_usd"])}</a> ({escape_html(a.get("site") or "Uybor")}'
                f'{kinds.get(a.get("kind"), "")})' for a in alts[:2]]
        top.append(t("Эту же продают ещё: {refs}", refs=", ".join(refs))
                   + (t(" и ещё {n}", n=len(alts) - 2) if len(alts) > 2 else ""))
    if "dorm" in (x.get("flags") or []):
        top.append(t("⚠️ Бывшее общежитие — проверьте кадастр"))

    # экономика — три строки
    econ = []
    m = x.get("mortgage")
    if x:                                            # можно ли в ипотеку — прямо, как написал продавец
        mline = escape_html(market.mortgage_line(x).split(" — ", 1)[-1])
        if m:
            mline += t(" · взнос ${down}, ≈ {mln} млн сум/мес на {years} лет", down=_money(m["down"]),
                       mln=market._mln(m["pay_uzs"]), years=m["years"])
        econ.append(f'<b>{t("Ипотека")}</b> — {mline}')
    bg = x.get("bargain")
    if bg:
        econ.append(f'<b>{t("Торг")}</b> — ' + (t("цена уже ниже рынка", v=_money(bg["offer"]))
                    if bg["calm"] else t("по похожим разумная цена ~${a}–${b}", a=_money(bg["offer"]),
                                         b=_money(bg["ceiling"]))))
    if x.get("rent"):
        y = f'{x["net_yield"] * 100:.1f}'.replace(".", ",")
        econ.append(f'<b>{t("Сдавать")}</b> — ' + t("~${r}/мес, {y}% в год чистыми", r=_money(x["rent"]), y=y))

    # подробнее — сколько влезет
    det = []
    if x.get("repair_signs"):
        det.append(t("На фото: {s}", s=escape_html(x["repair_signs"])))
    if m and x.get("rent"):
        diff = m["pay_usd"] - x["rent"]
        det.append(t("Ипотека ~${p}/мес против аренды такой же ~${r}", p=_money(m["pay_usd"]), r=_money(x["rent"]))
                   + (t(" — дороже аренды на ${d}", d=_money(diff)) if diff > 30 else t(" — примерно как аренда")))
    if bg and bg.get("args"):
        det.append(t("Аргументы в торге: {a}", a=escape_html(", ".join(bg["args"]))))
    if x.get("rent"):
        dep = x["assumptions"]["usd_deposit"]
        pb = x.get("payback_years")
        det.append(t("Вклад в $ даёт {p}%", p=f"{dep * 100:.0f}") + (t("; окупится за ~{y} лет", y=f"{pb:.0f}") if pb else ""))
    if pdet:
        det.append(escape_html(pdet))
    v = x.get("values")
    if v:
        det.append(t("Через 5 лет — скорее ~${b} (${p}–${o})", b=_money(market._r500(v["base"][5])),
                     p=_money(market._r500(v["pess"][5])), o=_money(market._r500(v["opt"][5]))))
    c = x.get("comp")
    if c:
        det.append(t("Сравнивала с {n} объявл.: {label}, медиана ${m}/м²", n=c["n"], label=escape_html(c["label"]),
                     m=_money(c["median_m2"]))
                   + (t(", обычно ${a}–{b}", a=_money(c["p20"]), b=_money(c["p80"])) if c.get("p20") else ""))
    if x.get("days_on_market") is not None and x.get("price_change"):
        pc = x["price_change"]
        det.append(t("Цена менялась: ${a} → ${b}", a=_money(pc[0]), b=_money(pc[1])))
    flags = hot_flags(l.get("text") or "", cfg)
    if flags:
        det.append(t("В тексте: «{f}»", f=escape_html(flags[0])))
    desc = _desc_clean(l.get("text") or "")

    foot = (f'<a href="{l["url"]}">' + t("Открыть на {site}", site=escape_html(l.get("site") or "Uybor")) + "</a>"
            if l.get("url") else "")
    if g and cfg.get("worker_url"):
        foot += (" · " if foot else "") + f'<a href="{map_url(cfg, g["lat"], g["lon"])}">{t("карта цен")}</a>'
    if x:
        foot += (" · " if foot else "") + f'<a href="{market.REPORT_URL}">{t("как считаю")}</a>'

    def build(top_, econ_, det_, desc_):
        parts = ["\n".join(top_).strip()]
        if econ_:
            parts.append("\n".join(econ_))
        if note:
            parts.append(note.strip())
        body = ["• " + d for d in det_]
        if desc_:
            body.append(t("Из объявления: «{d}»", d=escape_html(desc_)))
        if body:
            parts.append(f"<blockquote expandable><b>{t('Подробнее')}</b>\n" + "\n".join(body) + "</blockquote>")
        text_ = "\n\n".join(parts)
        return text_ + ("\n" + foot if body else "\n\n" + foot) if foot else text_

    # сначала всё обязательное, потом добираем детали и описание, пока влезает
    text = build(top, econ, [], "")
    while tg_len(text) > CAPTION_MAX and econ:
        econ = econ[:-1]
        text = build(top, econ, [], "")
    while tg_len(text) > CAPTION_MAX and len(top) > 3:
        top = top[:-1]
        text = build(top, econ, [], "")
    keep = []
    for d in det:
        cand = build(top, econ, keep + [d], "")
        if tg_len(cand) <= CAPTION_MAX:
            keep.append(d)
            text = cand
    if desc:
        room = CAPTION_MAX - tg_len(build(top, econ, keep, "")) - 24
        if room >= 60:
            text = build(top, econ, keep, _cut(desc, min(room, 320)))
    return text


def _fit(img, w, h):
    """Вписать с обрезкой по центру (как обложка)."""
    from PIL import Image
    k = max(w / img.width, h / img.height)
    img = img.resize((max(w, round(img.width * k)), max(h, round(img.height * k))), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


_PIL_TRIED = [False]


def _pil():
    """Pillow для коллажа. На сервере старый образ мог собраться без него — доставим один раз."""
    try:
        import PIL.Image  # noqa: F401
        return True
    except ImportError:
        pass
    if _PIL_TRIED[0]:
        return False
    _PIL_TRIED[0] = True
    try:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", "Pillow"],
                       timeout=120, check=True, capture_output=True)
        import importlib
        importlib.invalidate_caches()
        import PIL.Image  # noqa: F401,F811
        return True
    except Exception as e:
        log.warning("Pillow не поставился — коллажа не будет, пришлю одно фото: %s", e)
        return False


def photo_bytes(cfg, ref):
    """Фото по ссылке или по file_id Telegram (фото, которые прислал маклер)."""
    if str(ref).startswith("http"):
        r = requests.get(ref, headers=HEADERS, timeout=15)
        return r.content if r.status_code == 200 else None
    if not cfg or not cfg.get("telegram_bot_token"):
        return None
    f = tg_call(cfg, "getFile", {"file_id": ref}, quiet=True)
    path = ((f or {}).get("result") or {}).get("file_path")
    if not path:
        return None
    r = requests.get(f'https://api.telegram.org/file/bot{cfg["telegram_bot_token"]}/{path}', timeout=20)
    return r.content if r.status_code == 200 else None


def make_collage(urls, more: int = 0, width: int = 1280, cfg=None):
    """До 4 фото в одну картинку, как альбом Telegram: 1 крупно сверху + до 3 снизу.
    more — сколько фото не влезло (подпишем «+N» на последнем). Возвращает JPEG или None."""
    if not urls or not _pil():
        return None
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    imgs = []
    for u in urls[:4]:
        try:
            content = photo_bytes(cfg, u)
            if content and len(content) > 1000:
                im = Image.open(BytesIO(content))
                im.load()
                imgs.append(im.convert("RGB"))
        except Exception as e:
            log.info("фото для коллажа не скачалось: %s", e)
    if not imgs:
        return None
    big = max(range(len(imgs)), key=lambda i: imgs[i].width * imgs[i].height)
    imgs.insert(0, imgs.pop(big))          # крупно — самое чёткое фото, мелкие не растягиваем
    width = max(640, min(width, imgs[0].width))
    gap, W = 6, width
    n = len(imgs)
    if n == 1:
        im = imgs[0]
        h = min(round(W * im.height / im.width), round(W * 1.25))
        canvas = _fit(im, W, h)
    elif n == 2:
        w2, h = (W - gap) // 2, round(W * 0.62)
        canvas = Image.new("RGB", (W, h), (24, 24, 24))
        canvas.paste(_fit(imgs[0], w2, h), (0, 0))
        canvas.paste(_fit(imgs[1], W - w2 - gap, h), (w2 + gap, 0))
    else:
        top_h = round(W * 0.56)
        k = n - 1
        bw = (W - gap * (k - 1)) // k
        bh = round(W * (0.40 if k == 2 else 0.30))
        canvas = Image.new("RGB", (W, top_h + gap + bh), (24, 24, 24))
        canvas.paste(_fit(imgs[0], W, top_h), (0, 0))
        x0 = 0
        for i, im in enumerate(imgs[1:]):
            w_i = bw if i < k - 1 else W - x0
            canvas.paste(_fit(im, w_i, bh), (x0, top_h + gap))
            x0 += w_i + gap
    if more > 0:
        d = ImageDraw.Draw(canvas, "RGBA")
        label = f"+{more}"          # в шрифте по умолчанию нет кириллицы
        try:
            font = ImageFont.load_default(size=40)
        except TypeError:
            font = ImageFont.load_default()
        tw, th = d.textbbox((0, 0), label, font=font)[2:]
        x1, y1 = canvas.width - tw - 44, canvas.height - th - 40
        d.rounded_rectangle((x1 - 16, y1 - 12, canvas.width - 28, canvas.height - 24), 14, fill=(0, 0, 0, 150))
        d.text((x1, y1), label, font=font, fill=(255, 255, 255, 255))
    out = BytesIO()
    canvas.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def _jpeg(content: bytes):
    """Фото для Telegram: JPEG не больше 2048 px (у Yangiuylar рендеры по 10 МБ, у Realting — webp)."""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(content))
        im = im.convert("RGB")
        im.thumbnail((2048, 2048))
        out = BytesIO()
        im.save(out, "JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return content


def _fit_caption(text: str) -> str:
    """Подпись к альбому — до 1024 символов: сначала уходит «Подробнее», потом хвост."""
    if tg_len(text) <= 1024:
        return text
    t = re.sub(r"\n*<blockquote expandable>.*?</blockquote>", "", text, flags=re.S)
    lines = t.split("\n")
    while lines and tg_len("\n".join(lines)) > 1020:
        lines.pop(-2 if len(lines) > 1 else -1)    # последняя строка — ссылка на сайт, её держим
    return "\n".join(lines)


_PLACES = ("Чиланзар", "Мирабад", "Юнусабад", "Яккасарай", "Яшнабад", "Сергели", "Алмазар", "Мирзо", "Шайхантахур",
           "Янгихаёт", "Бектемир", "Учтепа")


def card_line(text: str) -> str:
    """Короткая подпись варианта для воркера: «2-комн · 46 м² · Чиланзар · $66 000».
    Карточки продажи и аренды пишут по-разному («$700» / «💰 $700», «🛏 3-комн · 📍 Мирабад») — берём кусочки."""
    plain = html_lib.unescape(re.sub(r"<[^>]+>", "", text or ""))
    rows = [r.strip() for r in plain.split("\n") if r.strip()]
    parts = [re.sub(r"^[^\w$]+", "", p).strip() for r in rows[1:] for p in r.split(" · ")]
    pick = lambda f: next((p for p in parts if p and f(p)), "")
    spec = pick(lambda p: re.match(r"\d\s*-?\s*комн", p))
    area = pick(lambda p: "м²" in p and not p.startswith("$") and "/м²" not in p and len(p) < 12)
    place = pick(lambda p: p.startswith(_PLACES))
    price = pick(lambda p: re.match(r"\$\s?\d", p) and "/м" not in p) or pick(lambda p: "сум" in p[:20])
    out = " · ".join(x for x in (spec, area, place.split(",")[0], price) if x)[:120]
    return out or (re.sub(r"^[^\w$]+", "", rows[0])[:120] if rows else "")


CARD_LOG = []          # последние отправленные карточки (ref, текст) — для отладки и тестов


def remember_card(cfg, ids, ref, text):
    """Воркеру: какие сообщения — какой вариант, чтобы «в шортлист» ответом на карточку понималось."""
    CARD_LOG.append((ref, text))
    del CARD_LOG[:-50]
    if not ref or not ids or ids is True or not cfg.get("worker_url"):
        return
    worker_post(cfg, "/svc/cards", {"ids": list(ids), "ref": ref, "line": card_line(text),
                                    "chat": str(cfg.get("telegram_chat_id") or "")}, timeout=10)


# ------------------------------------------------------- карточки по одной --
# Найденное не сыпется пачкой: карточки встают в очередь, клиент видит одну — с кнопками «Сохранить»,
# «Пропустить», «Уточнить у продавца»; следующая приходит после нажатия (или ответа словами на карточку).
CARD_QUEUE_MAX = 60
CARD_KEEP = ("key", "url", "photo_urls", "geo", "lat", "lon", "title", "district", "text_place", "site", "source",
             "created_at", "price_value", "price_currency", "price_usd", "phones", "seller")


def _queue_store():
    return concierge._BIZ_STORE.get("store")


def can_ask_seller(ref, l) -> bool:
    """«Уточнить у продавца» — только когда есть как связаться: маклер в боте или в чате клиента (O:…), у
    объявления — телефон или ник продавца. Иначе честнее кнопка «Открыть объявление»."""
    if str(ref).startswith("O:"):
        return True
    return bool([p for p in (l.get("phones") or []) if p]) or str(l.get("seller") or "").startswith("@")


def card_kb(ref, left=0, l=None, kind=""):
    l = l or {}
    if kind == "loc":                          # адрес неточный — сначала уточнить у продавца
        return {"inline_keyboard": [[{"text": t("📍 Уточнить адрес"), "callback_data": f"Q:l|{ref}"[:64]},
                                     {"text": t("⏭ Пропустить"), "callback_data": f"Q:n|{ref}"[:64]}]]}
    rows = [[{"text": t("💾 Сохранить"), "callback_data": f"Q:s|{ref}"[:64]},
             {"text": t("⏭ Пропустить"), "callback_data": f"Q:n|{ref}"[:64]}]]
    if can_ask_seller(ref, l):
        rows.append([{"text": t("✍️ Уточнить у продавца"), "callback_data": f"Q:a|{ref}"[:64]}])
    elif str(l.get("url") or "").startswith("http"):
        rows.append([{"text": t("🔗 Открыть объявление — там контакт"), "url": l["url"]}])
    return {"inline_keyboard": rows}


def enqueue_card(cfg, store, l, text, silent=False, ref=None, kind=""):
    """В очередь. Ничего не открыто — сразу показываем первую. Возвращает ["q"] (принято) или id показанной."""
    q = store.get_kv("card_queue") or []
    op = store.get_kv("card_open") or {}
    if op.get("ref") == ref or any(x.get("ref") == ref for x in q):
        return ["q"]
    q.append({"ref": ref, "l": {k: l.get(k) for k in CARD_KEEP if l.get(k) is not None},
              "text": text, "silent": bool(silent), "at": time.time(), **({"kind": kind} if kind else {})})
    store.set_kv("card_queue", q[-CARD_QUEUE_MAX:])
    if not op.get("ref"):
        return show_next_card(cfg, store) or ["q"]
    return ["q"]


FREE_CARDS_PER_DAY = 3


def free_limit_hit(cfg, store, q) -> bool:
    """Оплата включена, а тарифа нет: бесплатно — FREE_CARDS_PER_DAY карточек в день, остальные ждут в очереди.
    Один раз в день — сказать, сколько ждёт и как снять лимит. Без оплаты (бета) — никаких ограничений."""
    chat = str(cfg.get("telegram_chat_id") or "")
    if tenants.paid(chat):
        return False
    day = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    fc = store.get_kv("free_cards") or {}
    if fc.get("d") != day:
        fc = {"d": day, "n": 0}
        store.set_kv("free_cards", fc)
    if fc["n"] < FREE_CARDS_PER_DAY:
        return False
    if not fc.get("told"):
        fc["told"] = True
        store.set_kv("free_cards", fc)
        tg_call(cfg, "sendMessage", {"chat_id": chat, "parse_mode": "HTML", "text":
                f"На сегодня показала {FREE_CARDS_PER_DAY} бесплатных варианта. Ещё {len(q)} ждут в очереди — "
                "завтра покажу дальше. Без ограничений — /plans"})
        if cfg.get("worker_url"):
            worker_post(cfg, "/svc/event", {"chat": chat, "ev": "free_limit", "meta": len(q)}, timeout=10)
    return True


def free_cards_left(cfg, store) -> bool:
    """Без побочных действий: можно ли сегодня показать ещё карточку (тариф или бесплатные не кончились)."""
    if tenants.paid(str(cfg.get("telegram_chat_id") or "")):
        return True
    fc = store.get_kv("free_cards") or {}
    return fc.get("d") != datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d") or int(fc.get("n") or 0) < FREE_CARDS_PER_DAY


def _count_free(cfg, store):
    if tenants.paid(str(cfg.get("telegram_chat_id") or "")):
        return
    fc = store.get_kv("free_cards") or {}
    fc["n"] = int(fc.get("n") or 0) + 1
    store.set_kv("free_cards", fc)


def card_fits_now(cfg, store, it) -> bool:
    """Карточка ждала в очереди, а клиент тем временем поменял условия (обвёл область, бюджет, комнаты) —
    подходит ли она ещё. Только аренда из базы радара; остальное не трогаем."""
    ref = str(it.get("ref") or "")
    if it.get("kind") == "loc" or not ref.startswith("L:") or ref.startswith("L:sale:"):
        return True
    try:
        row = store.conn.execute("SELECT data FROM listings WHERE key=?", (ref[2:],)).fetchone()
    except Exception:
        return True
    if not row:
        return True
    l = {**json.loads(row[0] or "{}"), **{k: v for k, v in (it.get("l") or {}).items() if v is not None}}
    st = {**default_settings(), **(store.get_kv("settings") or {})}
    try:
        return passes_filters(l, effective_cfg(cfg, st), max_age=60) and passes_user_filters(l, st)
    except Exception as e:
        log.info("[очередь] проверка %s: %s", ref, e)
        return True


def recheck_open_card(cfg, store) -> bool:
    """После смены условий: открытая карточка больше не подходит — снять с неё кнопки, пометить и показать следующую."""
    op = store.get_kv("card_open") or {}
    if not op.get("ref") or card_fits_now(cfg, store, op.get("it") or {"ref": op["ref"]}):
        return False
    if op.get("mid"):
        tg_call(cfg, "editMessageText", {"chat_id": cfg["telegram_chat_id"], "message_id": op["mid"],
                                         "text": (op.get("text") or "")[:3900] + "\n\n<i>Не подходит под новые условия — убрала.</i>",
                                         "parse_mode": "HTML", "disable_web_page_preview": True,
                                         "reply_markup": json.dumps({"inline_keyboard": []})}, quiet=True)
    log.info("[очередь] открытая карточка не подходит под новые условия: %s", op["ref"])
    store.set_kv("card_open", {})
    show_next_card(cfg, store)
    return True


def show_next_card(cfg, store):
    """Следующая карточка из очереди: фото (с картой) альбомом, под ними текст с кнопками."""
    q = store.get_kv("card_queue") or []
    if q and free_limit_hit(cfg, store, q):
        return None
    while q:
        it = q.pop(0)
        store.set_kv("card_queue", q)
        if not card_fits_now(cfg, store, it):           # условия поменялись, пока ждала
            log.info("[очередь] не подходит под новые условия: %s", it["ref"])
            continue
        left = len(q)
        text = it["text"]
        if time.time() - float(it.get("at") or 0) > 1800:     # долго ждала в очереди — жива ли, та же ли цена
            l_ = dict(it["l"])
            st_ = still_actual(cfg, l_, older_h=0)
            if st_ == "gone":
                log.info("[очередь] снято, пока ждало: %s", it["ref"])
                continue
            if st_ == "changed":
                text += "\n\n⚠️ " + escape_html(l_.get("price_note") or "цена изменилась") + \
                        (f" — сейчас ${_money(l_['price_usd'])}" if l_.get("price_usd") else "")
            it["l"] = l_
        elif "joymee" in str(it["l"].get("url") or ""):        # ссылки на фото Joymee живут минуты
            l_ = dict(it["l"])
            still_actual(cfg, l_, older_h=0)
            it["l"] = l_
        if it.get("kind") != "loc" and not can_ask_seller(it["ref"], it["l"]) and \
                str(it["l"].get("url") or "").startswith("http"):
            text += "\n\n<i>Телефон продавца — на сайте. Пришлите его номер ответом на это сообщение — дальше напишу сама.</i>"
        text += (f"\n\n<i>В очереди ещё {left}</i>" if left else "")
        ids = _send_card_now(cfg, it["l"], text, kb=card_kb(it["ref"], left, it["l"], it.get("kind", "")),
                             silent=it.get("silent"), ref=it["ref"])
        if ids:
            _count_free(cfg, store)
            store.set_kv("card_open", {"ref": it["ref"], "mid": (ids[-1] if isinstance(ids, list) else None),
                                       "ids": ids if isinstance(ids, list) else [], "text": text,
                                       "at": time.time(), "it": it})
            return ids
    store.set_kv("card_open", {})
    return None


def waiting_cards(store) -> set:
    """Ключи объявлений, которые ждут в очереди «по одной» или открыты сейчас — клиент их ещё не видел до конца."""
    if store is None:
        return set()
    refs = [x.get("ref") or "" for x in (store.get_kv("card_queue") or [])]
    refs.append((store.get_kv("card_open") or {}).get("ref") or "")
    return {r[2:] for r in refs if r.startswith("L:")}


def reshow_open_card(cfg, store, older_s=120) -> bool:
    """Открытая карточка ушла вверх чата (новый поиск, разговор) — прислать её снова внизу, с теми же кнопками.
    Старое сообщение остаётся; его кнопки по-прежнему работают (та же ссылка на вариант)."""
    op = store.get_kv("card_open") or {}
    it = op.get("it")
    if not it or time.time() - float(op.get("at") or 0) < older_s:
        return False
    if op.get("ids"):
        tg_call(cfg, "editMessageReplyMarkup", {"chat_id": cfg["telegram_chat_id"], "message_id": op["mid"],
                                                "reply_markup": json.dumps({"inline_keyboard": []})}, quiet=True)
    q = store.get_kv("card_queue") or []
    it = dict(it, text=re.sub(r"\n\n<i>В очереди ещё \d+</i>$", "", it.get("text") or ""), at=time.time())
    store.set_kv("card_queue", [it] + [x for x in q if x.get("ref") != it["ref"]])
    store.set_kv("card_open", {})
    return bool(show_next_card(cfg, store))


def card_done(cfg, store, ref, status=""):
    """С открытой карточкой решили (кнопкой или словами) — отметить и показать следующую."""
    op = store.get_kv("card_open") or {}
    if not op.get("ref") or op["ref"] != ref:
        return None
    if op.get("mid") and status:
        tg_call(cfg, "editMessageText", {"chat_id": cfg["telegram_chat_id"], "message_id": op["mid"],
                                         "text": (op.get("text") or "")[:3900] + "\n\n<b>" + status + "</b>",
                                         "parse_mode": "HTML", "disable_web_page_preview": True,
                                         "reply_markup": json.dumps({"inline_keyboard": []})}, quiet=True)
    store.set_kv("card_open", {})
    return show_next_card(cfg, store)


SHORTLIST_CHECK_EVERY = 20 * 3600


def check_shortlist(cfg, store, now=None, limit=20, force=False) -> int:
    """Раз в сутки: живы ли объявления из шортлиста (Uybor, Joymee) и та же ли цена. Сняли — сказать и убрать
    из шортлиста; цена другая — сказать и обновить. Возвращает, сколько изменений."""
    now = now or time.time()
    if (globals().get("TESTING") and not force) or \
            (not force and now - float(store.get_kv("sl_live_at") or 0) < SHORTLIST_CHECK_EVERY):
        return 0
    store.set_kv("sl_live_at", now)
    items = [o for o in concierge.offers_by_status(store, "shortlist") + concierge.offers_by_status(store, "asked")
             if o and str(o.get("broker_chat") or "").startswith("site:") and (o.get("extra") or {}).get("url")]
    n = 0
    for o in items[:limit]:
        ex = o["extra"]
        l = {"url": ex["url"], "price_value": o.get("price_usd"), "price_currency": "USD", "price_usd": o.get("price_usd")}
        st = still_actual(cfg, l, older_h=0)
        tag = concierge.offer_tag_client(o) or f"#{o['oid']}"
        if st == "gone":
            concierge.set_offer_status(store, o["oid"], "rejected")
            concierge._set_extra(store, o["oid"], gone_at=datetime.now(timezone.utc).isoformat())
            send_telegram(cfg, f"❌ Из шортлиста сняли объявление: {escape_html(tag)} — похоже, уже сдали или продали. "
                               f"Убрала его из шортлиста.")
            n += 1
        elif st == "changed" and l.get("price_usd") and o.get("price_usd"):
            down = l["price_usd"] < o["price_usd"]
            store.conn.execute("UPDATE broker_offers SET price_usd=? WHERE oid=?", (l["price_usd"], o["oid"]))
            store.conn.commit()
            send_telegram(cfg, f"{'💸 Подешевело' if down else '📈 Подорожало'} из шортлиста: {escape_html(tag)} — "
                               f"было ${_money(o['price_usd'])}, сейчас ${_money(l['price_usd'])}.")
            n += 1
    return n


def where_text(cfg, store, d) -> str:
    """Что известно о месте квартиры: адрес/ориентир из объявления, точка и насколько ей верить, метро, ссылка
    на карту. Не знаем — так и говорим и предлагаем уточнить у продавца."""
    if d.startswith("L:w:"):
        l = _listing_any(store, d[4:]) or {}
    else:
        o = concierge.get_offer(store, int(d.split(":")[2])) or {}
        ex = o.get("extra") or {}
        l = {"title": "", "text": o.get("text") or "", "district": o.get("district"), "lat": ex.get("lat"),
             "lon": ex.get("lon"), "district_raw": ex.get("address")}
    if not l:
        return "Не нашла это объявление 🙈"
    g = l.get("geo") or geo.locate(l) or {}
    bits = []
    addr = ", ".join(x for x in (l.get("district"), l.get("district_raw") if l.get("district_raw") != l.get("district")
                                 else None) if x)
    if addr:
        bits.append(f"📍 В объявлении: {escape_html(addr)}")
    ph = geo.landmarks(f"{l.get('title') or ''}. {l.get('text') or ''}")
    if ph:
        bits.append(f"🧭 Ориентир от продавца: «{escape_html(ph[0][:80])}»")
    if g.get("lat"):
        exact = g.get("src") == "сайт" and not g.get("conflict")
        how = ("метка продавца на карте сайта" if exact else
               f"примерно (±{float(g.get('spread') or 0.6):.1f} км, по {'ориентиру' if g.get('src') == 'ориентир' else 'метро'})")
        if g.get("conflict"):
            how = f"метка на сайте спорит с текстом (в тексте — {g['conflict']}), ей не верю"
        url = f"https://yandex.uz/maps/?pt={g['lon']:.5f},{g['lat']:.5f}&z=16&l=map"
        bits.append(f'🗺 Точка: <a href="{url}">на карте</a> — {how}')
        try:
            mt = analyst.nearest_metro(g["lat"], g["lon"])
            if mt:
                bits.append(f"🚇 Ближайшее метро: {escape_html(str(mt[0]))} — ~{mt[1]:.1f} км, ~{mt[2]} мин пешком")
        except Exception:
            pass
    if not g.get("lat") or g.get("src") != "сайт" or g.get("conflict"):
        bits.append("Точного адреса нет — могу уточнить у продавца: ответьте на карточку «уточни адрес».")
    return "\n".join(bits) or "В объявлении о месте ничего нет — могу уточнить у продавца."


def card_followup(cfg, store, data):
    """Ответ словами («нравится», «мимо», «уточни») на открытую карточку — та же реакция, что у кнопки."""
    m = re.match(r"L:([sna]):(.+)$", data) or re.match(r"o:(like|skip|ask):(\d+)", data)
    if not m:
        return None
    ref = ("L:" + m.group(2).split("|")[0]) if data.startswith("L:") else "O:" + m.group(2)
    st = {"s": "💾 Сохранила в шортлист", "like": "💾 Сохранила в шортлист", "n": "⏭ Пропущено",
          "skip": "⏭ Пропущено", "a": "✍️ Уточняю у продавца", "ask": "✍️ Уточняю у продавца"}[m.group(1)]
    return card_done(cfg, store, ref, st)


SKIP_ASK_AFTER = 3                      # пропустил подряд столько карточек — спросить, что не так
SKIP_REASONS = (("price", "💸 Дорого"), ("area", "📍 Не тот район"), ("repair", "🛠 Ремонт"), ("photos", "📷 Мало фото"),
                ("other", "✍️ Другое"), ("next", "▶️ Просто дальше"))


def skip_question(cfg, store):
    """Вместо следующей карточки — вопрос одной строкой: почему пропускаете. Ответ поправит поиск."""
    rows, row = [], []
    for k, label in SKIP_REASONS:
        row.append({"text": t(label), "callback_data": f"Q:k|{k}"})
        if len(row) == 2:
            rows.append(row); row = []
    left = len(store.get_kv("card_queue") or [])
    return tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "parse_mode": "HTML",
                                        "text": f"Пропустили {SKIP_ASK_AFTER} подряд — что не так? Поправлю поиск."
                                                + (f"\n<i>В очереди ещё {left}</i>" if left else ""),
                                        "reply_markup": json.dumps({"inline_keyboard": rows}, ensure_ascii=False)})


def skip_reason(cfg, store, settings, reason, message_id=None) -> str:
    """Ответ на «что не так?»: поправить поиск, сказать что поменялось и как вернуть, показать следующую."""
    st = {**default_settings(), **(store.get_kv("settings") or {})}
    undo = None
    if reason == "price":                    # бюджет не трогаем — сначала самые выгодные из подходящих
        q = store.get_kv("card_queue") or []
        q.sort(key=lambda it: (it.get("l") or {}).get("price_usd") or 1e9)
        store.set_kv("card_queue", q)
        lim = st.get("max_price_usd")
        msg = ("💸 Поняла: дальше — сначала самые недорогие." + (f" Бюджет оставила до ${_money(lim)}; хотите ниже — "
               f"напишите, например, «бюджет до {_money(round(lim * 0.85 / 50) * 50)}»." if lim else ""))
    elif reason == "area":
        msg = ("📍 Показываю только внутри вашей области. Поменять её — «Меню» → «Карта»." if st.get("areas") else
               "📍 Обведите на карте, где искать: «Меню» → «Карта». Или напишите районы, например, «только Мирабад».")
    elif reason == "repair":
        st["repair_min"] = True
        undo = "Q:k|undo_repair"
        msg = "🛠 Убрала варианты с черновой отделкой и «требует ремонта»."
    elif reason == "photos":
        st["min_photos"] = 5
        undo = "Q:k|undo_photos"
        msg = "📷 Дальше — только варианты, где 5 фото и больше."
    elif reason == "undo_repair":
        st.pop("repair_min", None); msg = "↩️ Вернула: ремонт любой."
    elif reason == "undo_photos":
        st.pop("min_photos", None); msg = "↩️ Вернула: показываю и с небольшим числом фото."
    elif reason == "other":
        msg = "✍️ Напишите в двух словах, что не так, — поправлю поиск."
    else:
        msg = ""
    store.set_kv("settings", st)
    settings.update({k: v for k, v in st.items()})
    for k in ("repair_min", "min_photos"):
        if k not in st:
            settings.pop(k, None)
    if message_id:                           # вопрос с кнопками — заменить на ответ
        tg_call(cfg, "editMessageText", {"chat_id": cfg["telegram_chat_id"], "message_id": message_id,
                                         "text": msg or "▶️ Дальше",
                                         "reply_markup": json.dumps({"inline_keyboard": [[{"text": t("↩️ Отменить"),
                                                                     "callback_data": undo}]]} if undo else
                                                                    {"inline_keyboard": []}, ensure_ascii=False)},
                quiet=True)
    elif msg:
        send_telegram(cfg, msg)
    store.set_kv("skip_streak", {"n": 0, "asked_at": time.time()})
    if not reason.startswith("undo") and not (store.get_kv("card_open") or {}).get("ref"):
        if not show_next_card(cfg, store) and reason in ("repair", "photos") and not store.get_kv("card_queue"):
            send_telegram(cfg, "Под новые условия в очереди ничего не осталось — новые пришлю сразу, как появятся.")
    return {"price": "💸 Сначала недорогие", "repair": "🛠 Учла", "photos": "📷 Учла"}.get(reason, "👌")


def card_action(cfg, store, settings, d, message_id=None):
    """Кнопка карточки: Q:s|ref — сохранить, Q:n|ref — пропустить, Q:a|ref — уточнить у продавца,
    Q:k|причина — ответ на «что не так?» после нескольких пропусков подряд."""
    act, _, ref = d[2:].partition("|")
    if act == "k":
        return skip_reason(cfg, store, settings, ref, message_id)
    streak = store.get_kv("skip_streak") or {}
    if act == "n":
        streak["n"] = int(streak.get("n") or 0) + 1
    elif act in ("s", "a", "l"):
        streak["n"] = 0
    store.set_kv("skip_streak", streak)
    ask_now = (act == "n" and streak["n"] >= SKIP_ASK_AFTER
               and time.time() - float(streak.get("asked_at") or 0) > 6 * 3600)
    kind, _, key = ref.partition(":")
    if kind == "L":
        sub = {"s": f"L:s:{key}", "n": f"L:n:{key}", "a": f"L:a:{key}", "l": f"L:loc:{key}"}.get(act)
    elif kind == "O":
        sub = {"s": f"o:like:{key}", "n": f"o:skip:{key}:x", "a": f"o:ask:{key}"}.get(act)
    else:
        sub = None
    toast = ""
    if sub:
        toast, _ = handle_callback(sub, settings, store, cfg, None)
    status = {"s": "💾 Сохранила в шортлист", "n": "⏭ Пропущено",
              "a": "✍️ Уточняю у продавца — подробности ниже",
              "l": "📍 Уточняю адрес у продавца — подробности ниже"}.get(act, "")
    op = store.get_kv("card_open") or {}
    if op.get("ref") == ref:
        if act == "n" and op.get("ids"):     # пропущенную убираем из чата целиком, остаётся строка «пропущено»
            tg_call(cfg, "deleteMessages", {"chat_id": cfg["telegram_chat_id"],
                                            "message_ids": json.dumps([i for i in op["ids"] if i != op.get("mid")])},
                    quiet=True)
            tg_call(cfg, "editMessageText", {"chat_id": cfg["telegram_chat_id"], "message_id": op["mid"],
                                             "text": "⏭ Пропущено: " + escape_html(card_line(op.get("text") or "")),
                                             "reply_markup": json.dumps({"inline_keyboard": []})}, quiet=True)
            store.set_kv("card_open", {})
            if ask_now and (store.get_kv("card_queue") or []) and free_cards_left(cfg, store):
                store.set_kv("skip_streak", {"n": 0, "asked_at": time.time()})
                skip_question(cfg, store)            # следующая карточка — после ответа
            else:
                show_next_card(cfg, store)
        else:
            card_done(cfg, store, ref, status)
    elif message_id:                          # нажали на старой карточке — просто убрать кнопки
        tg_call(cfg, "editMessageReplyMarkup", {"chat_id": cfg["telegram_chat_id"], "message_id": message_id,
                                                "reply_markup": json.dumps({"inline_keyboard": []})}, quiet=True)
    return toast or status


def send_card(cfg, l: dict, text: str, kb: dict = None, silent: bool = False, ref: str = None, queue=False,
              kind=""):
    """Карточка. queue=True — найденное само (не по просьбе клиента): в очередь «по одной с кнопками»,
    если она включена (cfg card_queue). Иначе — сразу. kind="loc" — «адрес неточный, уточнить?»."""
    st = _queue_store() if queue and ref and cfg.get("card_queue") else None
    if st is not None:
        try:
            return enqueue_card(cfg, st, l, text, silent=silent, ref=ref, kind=kind)
        except Exception as e:
            log.warning("очередь карточек: %s — шлю сразу", e)
    if kind and kb is None:
        kb = card_kb(ref, 0, l, kind)
    return _send_card_now(cfg, l, text, kb=kb, silent=silent, ref=ref)


LOC_ASK_PER_DAY = 6


def loc_ask_ok(l: dict, settings: dict) -> bool:
    """Отсеялось только из-за места: адрес неточный, но район подходит, продавцу можно написать,
    а по остальным условиям вариант подходит — тогда не выбрасываем, а предлагаем уточнить."""
    if not geo.loc_unsure(l, settings.get("areas"), settings.get("near")):
        return False
    if not can_ask_seller("L:" + (l.get("key") or ""), l):
        return False                           # спросить некого — такие не показываем, как и раньше
    rest = {k: v for k, v in settings.items() if k not in ("areas", "near")}
    return passes_user_filters(l, rest)


def send_loc_ask(cfg, store, l: dict, card_text: str, silent=False):
    """Вместо полной карточки — «есть вариант, адрес неточный, уточнить?» (с фото). Не больше LOC_ASK_PER_DAY в день.
    Нажал «Уточнить адрес» — Ra'no спрашивает продавца от имени клиента; полная карточка — с его ответом."""
    day = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    b = store.get_kv("loc_ask_day") or {}
    if b.get("d") != day:
        b = {"d": day, "n": 0}
    if b["n"] >= LOC_ASK_PER_DAY:
        return False
    g = l.get("geo") or {}
    seen_ = ", ".join(x for x in (l.get("district") and f"{l['district']} район",
                                  g.get("label") if g.get("src") != "район" else "") if x)
    text = ("📍 <b>Есть вариант, но адрес неточный</b> — не могу проверить, что он в нужном месте.\n"
            f"{escape_html(card_line(card_text))}\n"
            + (f"Видно только: {escape_html(seen_)}\n" if seen_ else "")
            + "По остальным условиям подходит. Уточнить у продавца, где именно? Спрошу от вашего имени — "
              "полную карточку пришлю с его ответом.")
    if str(l.get("url") or "").startswith("http"):
        text += f'\n<a href="{escape_html(l["url"])}">Объявление</a>'
    ids = send_card(cfg, l, text, silent=silent, ref="L:" + (l.get("key") or ""), queue=True, kind="loc")
    if ids:
        b["n"] += 1
        store.set_kv("loc_ask_day", b)
        log.info("[место] спросить адрес? %s", (l.get("title") or "")[:50])
    return ids


def _send_card_now(cfg, l: dict, text: str, kb: dict = None, silent: bool = False, ref: str = None):
    """Вариант — обычным альбомом Telegram: первым — карта с отметкой, где квартира, дальше фото.
    Без кнопок — подпись под альбомом; с кнопками (kb) — альбом без подписи, а текст с кнопками — следом
    (у альбома в Telegram кнопок не бывает).
    ref — какой это вариант («L:<ключ>» объявление, «O:<номер>» вариант маклера) — для ответов в чате.
    Возвращает id сообщений списком; False — не ушло."""
    full = text
    text = _fit_caption(text)
    photos = list(l.get("photo_urls") or [])[:6]
    media, files = [], {}
    g = l.get("geo")
    if not g:
        try:
            g = geo.locate(dict(l))                  # координаты сайта, ориентир или метро из текста
        except Exception:
            g = None
    if g:
        approx = None if g.get("src") == "сайт" else max(0.4, float(g.get("spread") or 0.6))
        try:
            mp = geo.static_map(g["lat"], g["lon"], approx_km=approx)
        except Exception as e:
            log.info("[карта] %s", e)
            mp = None
        if mp:
            files["map"] = ("map.jpg", mp, "image/jpeg")
            media.append({"type": "photo", "media": "attach://map"})
    for i, ref_ in enumerate(photos):
        if str(ref_).startswith("http"):
            try:
                b = photo_bytes(cfg, ref_)
            except Exception as e:                   # фото не скачалось — без него
                log.info("фото не скачалось: %s", e)
                b = None
            if b and len(b) > 1000:
                files[f"p{i}"] = (f"p{i}.jpg", _jpeg(b), "image/jpeg")
                media.append({"type": "photo", "media": f"attach://p{i}"})
        else:                                        # фото из Telegram (прислал маклер) — по file_id
            media.append({"type": "photo", "media": ref_})
    quiet = {"disable_notification": True} if silent else {}
    ids = None
    if media and kb:
        data = {"chat_id": cfg["telegram_chat_id"], **quiet}
        if len(media) == 1:
            m = media[0]
            r = (tg_call_files(cfg, "sendPhoto", data, {"photo": files[m["media"][9:]]})
                 if m["media"].startswith("attach://") else tg_call(cfg, "sendPhoto", {**data, "photo": m["media"]},
                                                                     quiet=True))
        else:
            data["media"] = json.dumps(media, ensure_ascii=False)
            r = tg_call_files(cfg, "sendMediaGroup", data, files) if files else tg_call(cfg, "sendMediaGroup", data)
        album = (_msg_ids(r) or []) if r is not None else []
        r = tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": full[:4096], "parse_mode": "HTML",
                                         "disable_web_page_preview": True, **quiet,
                                         "reply_markup": json.dumps(kb, ensure_ascii=False)})
        if r is None:
            return False
        ids = list(album) + (_msg_ids(r) or [])
        media = []                                   # уже отправили
        try:
            remember_card(cfg, ids, ref, full)
        except Exception as e:
            log.info("карточка не запомнена: %s", e)
        return ids or True
    if media:
        media[0]["caption"] = text
        media[0]["parse_mode"] = "HTML"
        if len(media) == 1:
            m = media[0]
            data = {"chat_id": cfg["telegram_chat_id"], "caption": text, "parse_mode": "HTML", **quiet}
            if m["media"].startswith("attach://"):
                r = tg_call_files(cfg, "sendPhoto", data, {"photo": files[m["media"][9:]]})
            else:
                r = tg_call(cfg, "sendPhoto", {**data, "photo": m["media"]}, quiet=True)
        else:
            data = {"chat_id": cfg["telegram_chat_id"], "media": json.dumps(media, ensure_ascii=False), **quiet}
            r = tg_call_files(cfg, "sendMediaGroup", data, files) if files else tg_call(cfg, "sendMediaGroup", data)
        if r is not None:
            ids = _msg_ids(r) or True
    if not ids:                                      # запасной путь — текстом
        r = tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": full[:4096], "parse_mode": "HTML",
                                         "disable_web_page_preview": True, **quiet,
                                         **({"reply_markup": json.dumps(kb, ensure_ascii=False)} if kb else {})})
        ids = False if r is None else (_msg_ids(r) or True)
    if ids:
        try:
            remember_card(cfg, ids, ref, text)
        except Exception as e:
            log.info("карточка не запомнена: %s", e)
    return ids


def send_site_cards(cfg, sst, items, title="", more=0, more_cb="R:pick", limit=5, queue=False) -> int:
    """Варианты — каждый отдельной карточкой со своими фото и разбором (без сводных списков).
    items — ключи или объявления; title — подпись сверху «Подборка дня · 2 из 5».
    Без фото вариант не показываем. more — сколько ещё ждёт: на последней карточке «Ещё варианты»."""
    ready = []
    for it in list(items):
        if len(ready) >= limit:
            break
        l = it
        if isinstance(it, str) or "price_usd" not in it:
            key = it if isinstance(it, str) else it.get("key")
            row = sst.conn.execute("SELECT data FROM listings WHERE key=?", (key,)).fetchone()
            if not row:
                continue
            l = json.loads(row[0] or "{}")
        l = dict(l)
        try:
            fresh = sale_sources.refresh_listing(l)          # у Joymee ссылки на фото живут 10 минут
            if sale_sources.photo_repair(cfg, l, sst) or fresh:
                if sst.known(l["key"]):
                    sst.conn.execute("UPDATE listings SET data=? WHERE key=?", (sst.pack(l), l["key"]))
                    sst.conn.commit()
        except Exception as e:
            log.info("[продажа] карточка %s: %s", l.get("key"), e)
        if l.get("photo_urls"):
            ready.append(l)
        else:
            log.info("[продажа] без фото — не показываю: %s", l.get("key"))
    sent = 0
    for i, l in enumerate(ready, 1):
        ttl = escape_html(t(title)) if title else ""
        head = f"🔎 <b>{ttl}</b> · " + t("{i} из {n}", i=i, n=len(ready)) if title and len(ready) > 1 else \
            (f"🔎 <b>{ttl}</b>" if title else "")
        if more and i == len(ready):                 # кнопок нет — подсказка словами
            head += "\n" + t("Есть ещё {n} — скажите, если хотите посмотреть", n=more)
        g0 = l.get("geo")
        if send_card(cfg, l, format_sale_card(sst, l, cfg, head=head), ref="L:" + l["key"], queue=queue):
            sent += 1
        if l.get("geo") and l.get("geo") != g0 and sst.known(l["key"]):   # нашли место — запомнить, не искать снова
            sst.conn.execute("UPDATE listings SET data=? WHERE key=?", (sst.pack(l), l["key"]))
            sst.conn.commit()
        time.sleep(1)
    return sent


def sale_criteria_text(ss: dict) -> str:
    rooms = "–".join(str(r) for r in (ss.get("rooms") or []))
    rooms = t("{r} комн", r=rooms) if rooms else t("любая комнатность")
    districts = ", ".join(dist(d) for d in ss.get("districts") or []) or t("любые районы")
    nr = ss.get("near") or {}
    if ss.get("areas"):
        districts = t("внутри областей, обведённых на карте ({n}), только с точным адресом", n=len(ss["areas"]))
    elif nr.get("src") == "центр" and nr.get("strict"):
        districts = t("строго центр города (до {km} км от середины, только с точным адресом)",
                      km=str(nr.get("km")).replace(".", ","))
    elif nr.get("src") == "центр":
        districts = t("центр города и рядом с ним")
    elif nr.get("lat") is not None:
        districts = t("до {km} км от «{label}»", km=str(round(nr["km"], 1)).replace(".0", "").replace(".", ","),
                      label=nr["label"])
    if ss.get("owner_only", True):
        sellers = t("Только собственники: у продавца не больше {n} объявлений и нет признаков агентства.",
                    n=ss.get("max_owner_ads", 2))
    else:
        sellers = t("Продавцы — собственники и маклеры/агентства; в карточке помечено, кто продаёт.")
    return (t("{rooms} · до ${max} · {districts}", rooms=rooms, max=_money(ss.get("max_price_usd", 0)),
              districts=districts) + "\n" + sellers + " "
            + t("Источники — Uybor, Realt24, Joymee, Realting, Yangiuylar и Telegram-каналы."))


def sale_fingerprint(ss: dict) -> str:
    """Условия поиска одной строкой: изменились — пересматриваем отсеянное."""
    keys = ("max_price_usd", "min_price_usd", "rooms", "districts", "owner_only",
            "max_owner_ads", "notify_max_age_days", "near", "areas")
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
    key = store.find_dup(probe, cfg, phone_shown_only=False)
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


def _UYBOR_SALE_SHARED(ss, cfg):
    return tenants.shared_fetch("uybor_sale", lambda a, b: fetch_uybor_sale(a, b))(ss, cfg)


def run_sale_search(cfg: dict, store, settings: dict, force: bool = False) -> int:
    """Один проход поиска квартиры для покупки по всем источникам.
    Сильные варианты (ниже рынка, снижена цена) — сразу, остальные — в подборку дня.
    Возвращает число отправленных сразу."""
    ss = cfg.get("sale_search") or {}
    listings = []
    if (ss.get("uybor") or {}).get("enabled", True):
        try:
            listings = _UYBOR_SALE_SHARED(ss, cfg)
        except Exception as e:
            log.warning("[продажа] Uybor: %s", e)
    pump()
    try:
        listings += sale_sources.fetch_due(ss, cfg, store, force=force)
    except Exception as e:
        log.warning("[продажа] другие источники: %s", e)
    if not cfg.get("tenant"):                     # срез рынка и карта цен — общие, их ведёт проход владельца
        # раз в сутки — срез рынка для анализа (сам ловит свои ошибки)
        market.maybe_scan(store, cfg, sale_district_ids(ss))
        push_price_map(cfg, store)

    # Условия поиска поменялись — всё, что раньше отсеяли, пересматриваем заново.
    # Уже присланное не трогаем, чтобы не было повторов.
    fp = sale_fingerprint(ss)
    intro_sent = store.get_kv("sale_intro_sent", False)
    criteria_changed = intro_sent and store.get_kv("sale_criteria") != fp
    if criteria_changed:
        store.conn.execute("DELETE FROM phones WHERE key IN "
                           "(SELECT key FROM listings WHERE notified=0)")
        try:
            store.conn.execute("DELETE FROM photo_hash WHERE key IN (SELECT key FROM listings WHERE notified=0)")
        except sqlite3.Error:
            pass
        store.conn.execute("DELETE FROM listings WHERE notified=0")
        store.conn.commit()
        store.set_kv("sale_pick", [])
        log.info("[продажа] условия изменились — пересматриваю отсеянные объявления")

    candidates, seen = [], 0
    for l in listings:
        try:
            if store.known(l["key"]):
                continue
            if pump():                             # клиент поменял условия — этот разбор уже не нужен
                return 0
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
        found = (t("За последние {d} дней нашлось подходящих: {n}. Самые выгодные пришлю сразу, остальные — "
                   "лучшими карточками с фото, по одной.", d=days, n=len(unique))
                 if unique else
                 t("За последние {d} дней подходящих нет — но я на посту, пришлю, как только появятся 👀", d=days))
        title = t("Условия поиска обновлены") if intro_sent else t("Поиск квартиры для покупки включён")
        intro = (f"🏷 <b>{title}</b>\n" + sale_criteria_text(ss) + "\n\n" + found
                 + "\n" + t("Как идёт поиск — спросите «как идёт поиск?»"))
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
        if pump():                                 # условия поменялись — остальное разберём по новым
            break
        dup = find_sale_dup(l, store, cfg) or sale_sources.structural_dup(store, l) or sale_sources.photo_dup(store, l)
        act = sale_sources.attach_dup(cfg, store, l, dup, ss) if dup else None
        if act:
            sent += act == "cheaper"
            queued += act == "queued"
            continue
        if photo_budget[0] > 0 and sale_sources.photo_repair(cfg, l, store) is True:
            photo_budget[0] -= 1                     # из общего кэша — не в счёт бюджета модели
        if sale_sources.photo_facts(cfg, store, l):      # на фото график платежей или ЖК — цена и место точнее
            why_ = sale_sources.recheck(l, ss, store, cfg)
            if not why_:
                l.pop("_loc_ask", None)                  # место нашлось — и оно внутри
            elif l.get("_loc_ask") and why_.startswith(geo.LOC_UNSURE):
                why_ = ""
            if why_:
                store.save(l, notified=False)
                log.info("[продажа] мимо после фото — %s: %s", why_, l["title"][:45])
                continue
        sc, why, strong, _ = sale_sources.score(store, l, cfg, ss)
        l["score"], l["why"] = sc, why
        mins = fresh_owner_minutes(l, ss) if not first else None     # первый проход — без «горячих»
        if not sale_sources.has_photos(l):           # без фото не сравнить — не показываем
            store.save(l, notified=False)
            log.info("[продажа] без фото — не показываю: %s", l["title"][:50])
            continue
        if l.pop("_loc_ask", False):                 # адрес неточный — не карточкой, а «уточнить у продавца?»
            ids = send_loc_ask(cfg, store, l if settings.get("photos", True) else dict(l, photo_urls=[]),
                               format_sale_card(store, l, cfg), silent=is_night())
            store.save(l, notified=bool(ids))
            continue
        if mins is not None or (strong and day.get("instant", 0) + capped < cap):
            silent = is_night()
            head = (f"🔥 <b>Только что от собственника</b> · выложено {ago_text(mins)}\n"
                    "Позвоните первым — маклеры перехватывают такие за пару часов") if mins is not None else ""
            ids = send_card(cfg, l if settings.get("photos", True) else dict(l, photo_urls=[]),
                            format_sale_card(store, l, cfg, head=head), silent=silent, ref="L:" + l["key"],
                            queue=True)
            if not ids:
                log.info("[продажа] не отправилось, повторю позже: %s", l["title"][:45])
                break     # не сохраняем — объявление придёт в следующий проход
            store.save(l, notified=True)
            sent += 1
            capped += mins is None
            log.info("[продажа] %s (%s): %s", "горячее от собственника" if mins is not None else "сразу", sc, l["title"][:60])
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


def push_price_map(cfg, store) -> bool:
    """После среза — сетка цен на страницу «Карта цен» (воркер /map)."""
    if not store.get_kv("price_grid_dirty") or not cfg.get("worker_url"):
        return False
    try:
        grid = geo.price_grid(store.conn)
    except Exception as e:
        log.warning("[карта] сетка не собралась: %s", e)
        return False
    if not grid["cells"]:
        return False
    grid["at"] = store.get_kv("market_scan_at")
    grid["metro"] = [[n.split(" (")[0], la, lo] for n, (la, lo) in analyst.METRO.items()]
    if worker_post(cfg, "/svc/pricemap", grid):
        store.set_kv("price_grid_dirty", False)
        log.info("[карта] обновила карту цен: %d клеток", len(grid["cells"]))
        return True
    return False


def load_geo():
    try:
        if sale_path().exists():
            gs = open_sale()
            try:
                market.ensure_tables(gs.conn)
                geo.load(gs)
            finally:
                gs.conn.close()
    except Exception as e:
        log.warning("[место] справочник не загрузился: %s", e)


def map_url(cfg, lat=None, lon=None) -> str:
    base = (cfg.get("worker_url") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/map" + (f"?p={lat:.5f},{lon:.5f}" if lat is not None else "")


def sale_district_ids(ss: dict) -> list:
    return [i for i, name in UYBOR_DISTRICT_IDS.items() if name in (ss.get("districts") or [])]


def sale_kb(key, card_ids=None):
    """«Мимо» несёт id карточки над анализом — воркер удалит из чата и её, и анализ."""
    no = f"L:n:{key}"
    if card_ids == "self":                 # карточка и анализ — одно сообщение: удалить только его
        ref = f"{no}|0.0"
        no = ref if len(ref) <= 64 else no
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
                                       f"Спросите про цены рынка — покажу сводку"):
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
    store = open_sale()
    try:
        return market.summary_text(store, cfg, ss.get("districts") or [])
    finally:
        store.conn.close()


def sale_status_text(cfg: dict) -> str:
    ss = cfg.get("sale_search") or {}
    if not ss.get("enabled"):
        return "🏷 Поиск квартиры для покупки выключен."
    store = open_sale()
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
    lines.append("\n📊 Цены за м², аренда и сценарии по районам — спросите, покажу")
    return "\n".join(lines)


AI_PLACE_PER_DAY = 400          # на всех: кэш и счётчик общие (tenants.shared_store)
PLACE_KEYS = ("street", "house", "landmark", "mahalla", "kvartal", "metro", "district")


def ai_locate(cfg, store, l, budget=AI_PLACE_PER_DAY):
    """Точки нет (пост из канала, «Мирабад, рядом с …»): модель выписывает из текста улицу и дом, ориентир, махаллю,
    квартал, метро — и ищем их в справочнике, у метро или на карте OSM. Только когда ищем по области или радиусу,
    не больше budget объявлений в сутки. Результат по объявлению запоминаем."""
    g = l.get("geo") or geo.locate(l) or {}
    if g.get("lat") and g.get("src") != "район" and float(g.get("spread") or 0) <= 1.0:
        return g
    if not cfg.get("worker_url") or not l.get("key"):
        return g
    store = tenants.shared_store(store)            # одно объявление — один разбор моделью на всех
    cache = store.get_kv("ai_place") or {}
    f = cache.get(l["key"])
    if f is None:
        day = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
        b = store.get_kv("ai_place_day") or {}
        if b.get("d") != day:
            b = {"d": day, "n": 0}
        if b["n"] >= budget:
            return g
        b["n"] += 1
        store.set_kv("ai_place_day", b)
        r = worker_post(cfg, "/svc/place", {"text": f"{l.get('title') or ''}\n{l.get('text') or ''}"[:2000]}, timeout=30)
        if not (r or {}).get("ok"):
            return g
        f = {k: r[k] for k in PLACE_KEYS if r.get(k)}
        cache[l["key"]] = f
        if len(cache) > 3000:
            cache = dict(list(cache.items())[-2000:])
        store.set_kv("ai_place", cache)
    if not f:
        return g
    dist = l.get("district") or ""
    tries = []
    if f.get("landmark"):
        tries.append((f["landmark"], 0.5))
    if f.get("street") and f.get("house"):
        tries.append((f"{f['street']} {f['house']}", 0.4))
    if f.get("kvartal"):
        tries.append((f["kvartal"], 0.9))
    if f.get("mahalla"):
        tries.append((f"махалля {f['mahalla']}", 0.9))
    if f.get("metro"):
        tries.append((f"метро {f['metro']}", 0.6))
    for q, sp in tries:
        try:
            p = geo.point_for(q, store, requests)
        except Exception as e:
            log.info("[место] %s: %s", q, e)
            p = None
        if not p:
            continue
        dd = geo.district_at(p["lat"], p["lon"]) if geo.GAZ["pts"] else None
        if dist and dd and dd != dist:              # нашлось в другом районе — однофамилец, не верим
            continue
        l["geo"] = {"lat": p["lat"], "lon": p["lon"], "src": "ориентир", "label": geo.short_label(q),
                    "spread": sp, "by": "текст"}
        return l["geo"]
    return g


def _rent_listings(cfg, store, settings, name, listings, first_run, market, harvest=True):
    """Новые объявления аренды одного источника — одному пользователю: запомнить, отсеять, прислать."""
    eff = effective_cfg(cfg, settings)
    rent_on = rent_search_on(store)
    fresh = 0
    warm = not store.get_kv(f"src_warm:{name}")     # новый источник: текущее — запомнить молча
    for l in listings:
      n0 = _PUMP["n"]
      pump()                                    # клиент пишет — отвечаем, не дожидаясь конца прохода
      if _PUMP["n"] != n0:                      # разобрали сообщения — «пауза», смена условий действуют сразу
          rent_on = rent_search_on(store)
          settings = {**default_settings(), **(store.get_kv("settings") or {})}
          eff = effective_cfg(cfg, settings)
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
        if harvest:                         # маклеров собирает проход владельца, у остальных — копия каталога
            harvest_broker(l, store, cfg, ads_cnt)

        if not rent_on:                     # клиент покупает — аренду в чат не шлём
            store.save(l, notified=False)
            continue

        if (settings.get("areas") or (settings.get("near") or {}).get("lat") is not None) and passes_filters(l, eff) \
                and passes_user_filters(l, {k: settings.get(k) for k in ("rooms_min", "rooms_max")}):
            try:                                # ищем по месту, а точки нет — адрес и ориентир из текста
                ai_locate(cfg, store, l)
            except Exception as e:
                log.info("[место] не определила по тексту: %s", e)
        if not passes_filters(l, eff):
            store.save(l, notified=False)
            continue
        if not passes_user_filters(l, settings):
            if not loc_ask_ok(l, settings):
                store.save(l, notified=False)
                continue
            l["_loc_ask"] = True                # адрес неточный — дальше те же проверки, но вместо карточки вопрос

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

        if settings.get("photos", True) and not l.get("photo_urls"):
            store.save(l, notified=False)          # без фото не сравнить — не показываем
            log.info("[%s] без фото — не показываю: %s", name, l["title"][:50])
            continue
        likely_makler = seller_cnt >= cfg["makler_user_threshold"]
        try:
            analyst.score_listing(l, store, cfg, market, settings)
        except Exception as e:
            log.warning("анализ %s не удался: %s", l.get("key"), e)
        if l.pop("_loc_ask", False):
            ok = send_loc_ask(cfg, store, l, format_message(l, cfg, likely_makler), silent=is_night())
        else:
            ok = send_listing(cfg, settings, l, likely_makler, silent=is_night())
        store.save(l, notified=bool(ok))
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
    return fresh


def tenant_sales(next_at: dict, now: float) -> int:
    """Поиск квартиры для покупки — каждому пользователю (кроме владельца: его проход в run()) по его анкете.
    Сайты скачиваются один раз на всех (tenants.shared_fetch), отсев и подборка — у каждого свои."""
    n = 0
    for t_ in tenants.others():
        try:
            with tenants.ctx(t_):
                tcfg = effective_sale_cfg(t_.cfg, t_.store)
                ss = tcfg.get("sale_search") or {}
                if not ss.get("enabled") or t_.store.get_kv("fresh_start"):
                    continue
                force = bool(t_.store.get_kv("sale_force"))
                if force or now >= next_at.get(t_.chat, 0):
                    next_at[t_.chat] = now + (ss.get("uybor") or {}).get("interval_seconds", 60)
                    if force:
                        t_.store.set_kv("sale_force", False)
                    n += run_sale_search(tcfg, t_.sale, t_.settings(), force=force)
                sale_sources.maybe_daily_pick(t_.cfg, t_.sale)
        except Exception as e:
            log.warning("[продажа] пользователь %s: %s", t_.chat, e)
    return n


SOURCE_DOWN_HOURS = 6                              # источник с ошибками дольше — сообщить владельцу


def note_source(store, name, err=""):
    """Здоровье источника аренды: когда последний раз ответил без ошибки."""
    h = store.get_kv("rent_src_health") or {}
    now = time.time()
    cur = h.get(name) or {}
    h[name] = {"ok_at": now if not err else cur.get("ok_at", now), "err": err[:120], "at": now}
    store.set_kv("rent_src_health", h)


def source_health(cfg, store, sale_store, now=None) -> list:
    """Раз в час: какие источники не отвечают дольше SOURCE_DOWN_HOURS. Владельцу — одно сообщение на
    источник в сутки и одно, когда он снова заработал. Возвращает список упавших."""
    now = now or time.time()
    if now - float(store.get_kv("health_at") or 0) < 3600:
        return []
    store.set_kv("health_at", now)
    on = {k for k, x in (cfg.get("sources") or {}).items() if x.get("enabled")} if cfg.get("sources") else None
    rows = {f"аренда · {k}": v for k, v in (store.get_kv("rent_src_health") or {}).items()
            if on is None or k in on}                  # выключенные источники — не тревожим
    if sale_store is not None:
        rows.update({f"продажа · {k}": v for k, v in (sale_store.get_kv("sale_src_stats") or {}).items()
                     if k in sale_sources.SOURCES})
    sent = store.get_kv("health_alerts") or {}
    down = []
    for name, v in rows.items():
        ok_at = float(v.get("ok_at") or v.get("at") or now)
        bad = bool(v.get("err")) and now - ok_at > SOURCE_DOWN_HOURS * 3600
        if bad:
            down.append(name)
            if now - float(sent.get(name) or 0) > 86400:
                if send_telegram(cfg, f"⚠️ <b>Источник не отвечает</b>: {escape_html(name)} — уже "
                                      f"{(now - ok_at) / 3600:.0f} ч.\nОшибка: <code>{escape_html(v['err'][:120])}</code>\n"
                                      "Остальные источники работают; как заработает — скажу.", _admin=True):
                    sent[name] = now
        elif sent.get(name) and not v.get("err"):
            send_telegram(cfg, f"✅ Источник снова работает: {escape_html(name)}", _admin=True)
            sent.pop(name, None)
    store.set_kv("health_alerts", sent)
    return down


BROKER_HARVEST_EVERY = 3 * 3600
PUMP_EVERY = 3                                   # с; как часто заглядывать в сообщения посреди долгой работы
_PUMP = {"ctx": None, "last": 0.0, "busy": False, "n": 0}


def pump(force: bool = False) -> bool:
    """Ответить клиенту, не дожидаясь конца прохода: посреди долгой работы (сайты, фото, разборы)
    раз в несколько секунд забираем сообщения и обрабатываем их. Без вложенности: если обработка
    сама дошла до pump — просто выходим. True — клиент поменял условия/сбросил поиск: текущий
    разбор пора прервать (необработанное придёт в следующий раз — оно ещё не сохранено)."""
    ctx = _PUMP["ctx"]
    if not ctx or _PUMP["busy"]:
        return False
    now = time.time()
    if not force and now - _PUMP["last"] < PUMP_EVERY:
        return False
    _PUMP["last"], _PUMP["busy"] = now, True
    cfg, store, sale_store = ctx
    try:
        cur = (concierge._BIZ_STORE.get("store") if tenants.in_ctx() else None) or store   # чей сейчас проход
        before = (cur.get_kv("sale_force"), cur.get_kv("fresh_start"))
        process_commands(cfg, store, long_poll=0)
        owner_t = tenants.REG["owner"] or tenants.Tenant(str(cfg.get("telegram_chat_id") or ""), cfg,
                                                         owner=True, store=store)
        with tenants.ctx(owner_t):                 # варианты владельца — на его языке и с его базами
            flush_pending_offer(cfg, store, sale_store)
        _PUMP["n"] += 1
        after = (cur.get_kv("sale_force"), cur.get_kv("fresh_start"))
        return after != before and any(after)
    except Exception as e:
        log.warning("не обработала сообщения посреди прохода: %s", e)
        return False
    finally:
        _PUMP["busy"] = False


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
    sync_lang(store)
    first_run = store.counts()[0] == 0

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    detect_bot_username(cfg)
    if not once and store.get_kv("kb_v") != 4:      # кнопки внизу убраны — сказать один раз и убрать их
        if tg_call(cfg, "sendMessage", {
                "chat_id": cfg["telegram_chat_id"], "parse_mode": "HTML",
                "text": "✨ <b>Теперь без кнопок — просто пишите мне своими словами</b>\n\n"
                        "Каждый вариант приходит альбомом: первым — карта, где квартира, потом фото.\n"
                        "Что понравилось, что нет, что уточнить, когда смотреть — пишите как удобно, я пойму. "
                        "Про конкретную квартиру проще ответить на её сообщение.",
                "reply_markup": json.dumps(concierge.OWNER_KB, ensure_ascii=False)}) is not None:
            store.set_kv("kb_v", 4)
    enabled = {name: s for name, s in cfg["sources"].items() if s.get("enabled")}
    next_run = {name: 0.0 for name in enabled}
    mode = " (разовый проход)" if once else (f" на {minutes:.0f} мин" if minutes else "")
    log.info("Rent Radar запущен%s. Источники: %s. Лимит: $%s. Версия %s",
             mode, ", ".join(enabled) or "нет", cfg["max_price_usd"], code_version())
    if not once:
        try:
            health_flush(cfg, store, started=True)
        except Exception as e:
            log.info("здоровье: %s", e)

    load_geo()                                 # справочник ориентиров — для «где квартира» и поиска у ориентира
    sale_cfg = effective_sale_cfg(cfg, store).get("sale_search") or {}   # покупка из чата включает поиск
    sale_store, next_sale = None, 0.0
    # сбор маклеров: справочники меняются медленно — раз в BROKER_HARVEST_EVERY, срок храним в базе
    # (проход живёт 10 минут; раньше сбор шёл в каждом проходе и съедал ~35 с)
    next_sale_brokers = max(time.time() + 120, float(store.get_kv("next_brokers_at") or 0))
    if sale_cfg.get("enabled") and (sale_cfg.get("uybor") or {}).get("enabled", True):
        sale_store = Store(SALE_DB_PATH)
        sale_store.prune()
        log.info("Покупка: %s", sale_criteria_text(sale_cfg).replace("\n", " "))
    if first_run:
        log.info("Первый запуск: текущие объявления запоминаю без уведомлений")
    _PUMP["ctx"] = None if once else (cfg, store, sale_store)
    if not once:
        tenants.refresh(cfg, store, sale_store, force=True)       # много пользователей — список с воркера
    next_sale_t = {}                                              # chat → когда следующий поиск по сайтам

    while not stop["flag"]:
        now = time.time()
        # long-poll: команды и нажатия кнопок ловим за ~секунду, а не раз в проход
        settings = process_commands(cfg, store, long_poll=0 if once else 20)
        flush_pending_offer(cfg, store, sale_store)
        _PUMP["last"] = time.time()
        push_snapshot(cfg, store, settings)
        if not once:
            followup.run(cfg, store, sale_store)     # напоминания, просмотры, вечерняя сводка
            tenants.refresh(cfg, store, sale_store)  # раз в минуту — кто сейчас пользователь
            for t_ in tenants.others():              # то же — каждому пользователю
                with tenants.ctx(t_):
                    try:
                        flush_pending_offer(t_.cfg, t_.store, t_.sale)
                        push_snapshot(t_.cfg, t_.store, t_.settings())
                        followup.run(t_.cfg, t_.store, t_.sale)
                    except Exception as e:
                        log.warning("пользователь %s: %s", t_.chat, e)
            try:
                outreach.tick(cfg, store)            # Ra'no пишет продавцам со своего аккаунта и читает ответы
            except Exception as e:
                log.warning("[ra] такт: %s", e)
            try:
                recheck_channels(cfg, store)         # раз в неделю — живы ли каналы
            except Exception as e:
                log.warning("проверка каналов не удалась: %s", e)
        eff = effective_cfg(cfg, settings)
        market = analyst.market_stats(store)
        rent_on = rent_search_on(store)
        if not rent_on:                        # владелец покупает, а кто-то из пользователей ищет аренду — не реже
            for t_ in tenants.others():
                try:
                    if rent_search_on(t_.store):
                        rent_on = True
                        break
                except Exception:
                    continue
        for name, scfg in enabled.items():
            if not once and now < next_run[name]:
                continue
            # аренда не нужна (клиент покупает) — смотрим реже: только для маклеров и рынка
            next_run[name] = now + scfg.get("interval_seconds", 120) * (1 if rent_on else 5)
            try:
                if name == "telegram":           # + каналы, которые добавили пользователи; без замолчавших
                    extra = [c for c in user_channels(store, "rent") if c.lower() not in
                             {x.lower() for x in scfg.get("channels") or []}]
                    chans = live_channels(store, list(scfg.get("channels") or []) + extra)
                    listings = SOURCE_FETCHERS[name](dict(scfg, channels=chans), cfg)
                else:
                    listings = SOURCE_FETCHERS[name](scfg, cfg)
            except Exception as e:
                log.warning("[%s] ошибка получения: %s", name, e)
                next_run[name] = now + min(900, scfg.get("interval_seconds", 120) * 3)
                note_source(store, name, str(e))
                continue
            note_source(store, name)

            _rent_listings(cfg, store, settings, name, listings, first_run, market)
            for t_ in tenants.others():                # те же объявления — другим пользователям, каждому по-своему
                t0_ = time.time()
                with tenants.ctx(t_):
                    try:
                        _rent_listings(t_.cfg, t_.store, t_.settings(), name, [dict(x) for x in listings],
                                       t_.store.counts()[0] == 0, analyst.market_stats(t_.store), harvest=False)
                    except Exception as e:
                        log.warning("[%s] пользователь %s: %s", name, t_.chat, e)
                HEALTH["slow"] = max(HEALTH.get("slow", 0.0), time.time() - t0_)   # самый долгий разбор на одного
                HEALTH["users"] = len(tenants.others())

        if not once and now >= next_sale_brokers:   # маклеры по продаже — для запросов на покупку
            next_sale_brokers = now + (sale_cfg.get("broker_interval_seconds") or BROKER_HARVEST_EVERY)
            store.set_kv("next_brokers_at", next_sale_brokers)
            try:
                backfill_sale_brokers(store, sale_store)
                harvest_sale_brokers(cfg, store)
                harvest_market_brokers(cfg, store)
            except Exception as e:
                log.warning("[маклеры продажи] сбор не удался: %s", e)

        force_flag = store.get_kv("sale_force")                # «Проверить сайты сейчас» или новые параметры
        force_sale = bool(force_flag)
        fresh = bool(store.get_kv("fresh_start"))              # после сброса — ждём новых параметров из чата
        sale_on = bool((effective_sale_cfg(cfg, store).get("sale_search") or {}).get("enabled"))
        if not sale_on:
            fresh = True                                       # клиент ищет аренду — продажу не ищем и не шлём
        elif sale_store is None and not once:                  # переключился на покупку посреди работы
            sale_cfg = effective_sale_cfg(cfg, store).get("sale_search") or {}
            if (sale_cfg.get("uybor") or {}).get("enabled", True):
                sale_store = Store(SALE_DB_PATH)
                sale_store.prune()
                _PUMP["ctx"] = (cfg, store, sale_store)
                tenants.refresh(cfg, store, sale_store, force=False)
                log.info("Покупка включена: %s", sale_criteria_text(sale_cfg).replace("\n", " "))
        if sale_store is not None and not fresh and (once or now >= next_sale or force_sale):
            next_sale = now + (sale_cfg.get("uybor") or {}).get("interval_seconds", 60)
            if force_sale:
                store.set_kv("sale_force", False)
            try:
                n = run_sale_search(effective_sale_cfg(cfg, store), sale_store, settings, force=force_sale)
                if force_flag == "button":
                    st = sale_sources.day_stats(sale_store)
                    send_telegram(cfg, t("🔄 Пробежалась по всем сайтам! Сегодня просмотрено {a}, подошло {b}; "
                                         "в подборке ждут {c}.", a=st.get('seen', 0), b=st.get('fit', 0),
                                         c=len(sale_sources.pick_pending(sale_store))))
                if n:
                    log.info("[продажа] новых: %d", n)
            except Exception as e:      # поиск покупки не должен ронять радар
                log.warning("[продажа] проход не удался: %s", e)
        if not once:
            try:
                source_health(cfg, store, sale_store)            # источник молчит часами — сказать владельцу
            except Exception as e:
                log.warning("проверка источников не удалась: %s", e)
        if sale_store is not None and not once and not fresh:
            try:
                sale_sources.maybe_daily_pick(cfg, sale_store)   # 19:30 — подборка дня
            except Exception as e:
                log.warning("[продажа] подборка: %s", e)
        if not once:
            tenant_sales(next_sale_t, now)                       # поиск покупки — каждому пользователю

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
        try:
            health_flush(cfg, store)
        except Exception as e:
            log.info("здоровье: %s", e)
    log.info("Готово" if once or deadline else "Остановлено")


# ------------------------------------------------------------------ здоровье --
# Сколько проходов, падений и предупреждений за сутки — владельцу сервиса раз в день (без звука), падение
# прохода — сразу (не чаще раза в час). Версия кода — в логе при старте: видно, что работает свежий код.
HEALTH = {"warn": collections.Counter(), "errors": 0, "slow": 0.0, "users": 0}


class _HealthCounter(logging.Handler):
    def emit(self, record):
        try:
            key = re.sub(r"\d+", "#", record.getMessage().split("\n")[0])[:70]
            HEALTH["warn"][key] += 1
            if record.levelno >= logging.ERROR:
                HEALTH["errors"] += 1
        except Exception:
            pass


def code_version() -> str:
    try:
        head = (BASE_DIR / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = BASE_DIR / ".git" / head[5:].strip()
            if ref.exists():
                return ref.read_text().strip()[:7]
            packed = (BASE_DIR / ".git" / "packed-refs").read_text()
            m = re.search(r"^([0-9a-f]{40}) " + re.escape(head[5:].strip()) + "$", packed, re.M)
            return m.group(1)[:7] if m else "?"
        return head[:7]
    except Exception:
        return "?"


def health_flush(cfg, store, started=False, crashed=None):
    """Счётчики прохода — в базу (за сутки по Ташкенту); новые сутки — отчёт за прошлые владельцу сервиса."""
    day = datetime.now(TASHKENT_TZ).strftime("%Y-%m-%d")
    h = store.get_kv("health_day") or {}
    if h.get("day") and h["day"] != day:
        try:
            send_health_report(cfg, h)
        except Exception as e:
            log.info("отчёт о здоровье не ушёл: %s", e)
        h = {}
    if not h:
        h = {"day": day, "passes": 0, "crashes": 0, "errors": 0, "warn": {}}
    h["passes"] += int(started)
    h["crashes"] += int(bool(crashed))
    h["errors"] += HEALTH["errors"]
    w = collections.Counter(h.get("warn") or {})
    w.update(HEALTH["warn"])
    h["warn"] = dict(w.most_common(30))
    h["version"] = code_version()
    h["slow"] = round(max(float(h.get("slow") or 0), HEALTH.get("slow", 0.0)), 1)
    h["users"] = max(int(h.get("users") or 0), HEALTH.get("users", 0))
    HEALTH["warn"].clear()
    HEALTH["errors"] = 0
    HEALTH["slow"] = 0.0
    store.set_kv("health_day", h)
    return h


def send_health_report(cfg, h):
    top = sorted((h.get("warn") or {}).items(), key=lambda x: -x[1])[:3]
    d = datetime.strptime(h["day"], "%Y-%m-%d").strftime("%d.%m")
    ok = not h.get("crashes") and not h.get("errors")
    lines = [f"🩺 <b>Ra'no за {d}</b>: {'всё работает' if ok else 'были сбои'} · проходов {h.get('passes', 0)}, "
             f"падений {h.get('crashes', 0)}, ошибок {h.get('errors', 0)}, предупреждений "
             f"{sum((h.get('warn') or {}).values())} · версия {h.get('version', '?')}"]
    if h.get("users"):
        lines.append(f"• клиентов {h['users'] + 1}, самый долгий разбор объявлений на одного — {h.get('slow', 0)} с")
    lines += [f"• ×{n} {escape_html(k)}" for k, n in top]
    tg_call(cfg, "sendMessage", {"chat_id": cfg.get("admin_chat_id") or cfg["telegram_chat_id"], "parse_mode": "HTML",
                                 "text": "\n".join(lines), "disable_notification": True, "_noTr": True})


def main():
    logging.getLogger("rent-radar").addHandler(_HealthCounter(level=logging.WARNING))
    try:
        run()
    except Exception as e:
        log.exception("проход упал: %s", e)
        try:                                   # владельцу сервиса — сразу, но не чаще раза в час
            cfg = load_config()
            st = Store(DB_PATH)
            health_flush(cfg, st, crashed=True)
            last = float(st.get_kv("crash_alert_at") or 0)
            if time.time() - last > 3600:
                st.set_kv("crash_alert_at", time.time())
                tg_call(cfg, "sendMessage", {"chat_id": cfg.get("admin_chat_id") or cfg["telegram_chat_id"],
                                             "_noTr": True, "text": f"⚠️ Проход Ra'no упал (версия {code_version()}): "
                                             f"{type(e).__name__}: {str(e)[:300]}. Перезапускаюсь сам."})
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()

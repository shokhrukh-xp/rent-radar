"""
Аналитический слой Rent Radar: карты, рынок, скоринг «хозяин или маклер»
и ранжирование вариантов. Работает без LLM — на данных и правилах.
"""

import math
import statistics
from datetime import datetime, timedelta, timezone

TASHKENT_TZ = timezone(timedelta(hours=5))

# Станции метро Ташкента (OpenStreetMap). Нужны, чтобы считать реальную
# пешую доступность, а не верить словам «рядом с метро» в объявлении.
METRO = {
    "Абдулла Кадыри": (41.32019, 69.28176), "Айбек": (41.29801, 69.27405),
    "Алишер Навои": (41.31892, 69.2543), "Алмазар (Сабир Рахимов)": (41.25667, 69.1961),
    "Алмас": (41.28171, 69.36033), "Бадамзар": (41.33717, 69.28457),
    "Беруни": (41.34462, 69.2062), "Буюк Ипак Йули (Максим Горький)": (41.32611, 69.32856),
    "Гафур Гулям": (41.32788, 69.24583), "Дружба Народов": (41.3119, 69.2431),
    "Дустлик (Чкалов)": (41.29364, 69.32224), "Кипчак": (41.20542, 69.22141),
    "Киёт": (41.24448, 69.29973), "Космонавтов": (41.30516, 69.26472),
    "Куйлюк": (41.23746, 69.327), "Курувчилар (Строителей)": (41.22164, 69.2605),
    "Матонат": (41.24447, 69.30832), "Машиносозлар (Ташсельмаш)": (41.29898, 69.30513),
    "Миллий Бог (Комсомольская)": (41.30339, 69.23567), "Минг Урик": (41.29966, 69.27441),
    "Минор": (41.32689, 69.28342), "Мирзо Улугбек (50 лет СССР)": (41.28203, 69.21258),
    "Мустакиллик майдони": (41.31495, 69.27106), "Новза (Хамза)": (41.29187, 69.22362),
    "Пахтакор": (41.31779, 69.25509), "Пушкин": (41.32195, 69.3111),
    "Рахат": (41.26529, 69.36475), "Сквер Амира Тимура": (41.31267, 69.28327),
    "Таларык": (41.24451, 69.28496), "Ташкент": (41.29329, 69.28772),
    "Технопарк": (41.29463, 69.32319), "Тинчлик": (41.3323, 69.21912),
    "Тузель": (41.29201, 69.35618), "Туран": (41.21068, 69.23415),
    "Туркистан": (41.37752, 69.29602), "Узбекистан": (41.31194, 69.25341),
    "Узгариш": (41.22734, 69.20397), "Хамид Алимджан": (41.31816, 69.29574),
    "Ханабад": (41.23001, 69.27044), "Чаштепа": (41.23825, 69.19603),
    "Чиланзар": (41.27436, 69.20497), "Чинара": (41.2067, 69.21896),
    "Чорсу": (41.32586, 69.23682), "Шахристан": (41.35312, 69.28811),
    "Юнyc Раджаби": (41.31389, 69.28351), "Юнусабад": (41.36684, 69.2923),
    "Янгиабад": (41.25651, 69.35872), "Янгихаёт": (41.21351, 69.21402),
    "Яшнабад": (41.29759, 69.34978),}

WALK_M_PER_MIN = 75          # спокойный шаг ~4.5 км/ч
FAR_FROM_METRO_MIN = 25      # дальше — считаем «не пешком»


# --------------------------------------------------------------- гео ----

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_metro(lat, lon):
    """→ (название, км, минут пешком) или None."""
    if lat is None or lon is None:
        return None
    best = min(METRO.items(), key=lambda kv: haversine_km(lat, lon, kv[1][0], kv[1][1]))
    km = haversine_km(lat, lon, best[1][0], best[1][1])
    # по прямой короче, чем по улицам: накидываем ~25%
    walk = int(round(km * 1000 * 1.25 / WALK_M_PER_MIN))
    return best[0], round(km, 2), walk


def distance_to(lat, lon, target):
    """target = (lat, lon) — например, работа. → (км, минут пешком)."""
    if lat is None or lon is None or not target:
        return None
    km = haversine_km(lat, lon, target[0], target[1])
    return round(km, 2), int(round(km * 1000 * 1.25 / WALK_M_PER_MIN))


# ------------------------------------------------------------ рынок ----

def market_stats(store, days=45, min_sample=4):
    """Медианы цен по (район, комнаты) и по комнатам целиком — из своей базы."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = store.conn.execute(
        "SELECT district, rooms, price_usd FROM listings "
        "WHERE first_seen > ? AND price_usd IS NOT NULL AND price_usd > 0 "
        "AND dup_of IS NULL", (since,)).fetchall()
    by_pair, by_rooms = {}, {}
    for d, r, p in rows:
        if r:
            by_rooms.setdefault(r, []).append(p)
            if d:
                by_pair.setdefault((d, r), []).append(p)
    stats = {"pair": {}, "rooms": {}, "sample": len(rows)}
    for k, v in by_pair.items():
        if len(v) >= min_sample:
            stats["pair"][k] = (statistics.median(v), len(v))
    for k, v in by_rooms.items():
        if len(v) >= min_sample:
            stats["rooms"][k] = (statistics.median(v), len(v))
    return stats


def price_verdict(listing, stats):
    """→ (отклонение_в_%, пояснение) или (None, '')."""
    p = listing.get("price_usd")
    rooms, district = listing.get("rooms"), listing.get("district")
    if not p or not rooms:
        return None, ""
    ref = stats["pair"].get((district, rooms))
    scope = f"{district}, {rooms}-комн"
    if not ref:
        ref = stats["rooms"].get(rooms)
        scope = f"{rooms}-комн по городу"
    if not ref:
        return None, ""
    median, n = ref
    if not median:
        return None, ""
    delta = (p - median) / median * 100
    if delta <= -20:
        word = "заметно дешевле рынка — стоит поспешить"
    elif delta <= -8:
        word = "дешевле рынка"
    elif delta < 8:
        word = "по рынку"
    elif delta < 20:
        word = "дороже рынка"
    else:
        word = "сильно дороже рынка"
    return delta, f"{word} ({delta:+.0f}% к медиане ${median:.0f}; {scope}, выборка {n})"


# ------------------------------------------- хозяин или маклер (0..100) ----

def owner_score(listing, store, cfg):
    """Вероятность, что общаетесь с хозяином, а не с маклером."""
    score, why = 50, []
    text = f"{listing.get('title', '')} {listing.get('text', '')}".lower()

    com = (listing.get("commission") or "").lower()
    if com in ("нет", "yo'q", "no"):
        score += 20; why.append("в объявлении указано «без комиссии»")
    elif com in ("да", "ha", "yes"):
        score -= 35; why.append("продавец берёт комиссию — это посредник")

    if listing.get("is_business"):
        score -= 30; why.append("бизнес-аккаунт")
    elif listing.get("is_business") is False:
        score += 5

    for kw in (cfg.get("hot_keywords") or []):
        if kw.lower() in text:
            score += 12; why.append(f"в тексте «{kw}»")
            break
    if "маклер" in text and "без маклер" not in text and "bez makler" not in text:
        score -= 10; why.append("текст упоминает маклера")

    sid = listing.get("seller_id") or ""
    if sid:
        row = store.conn.execute(
            "SELECT cnt FROM seller_counts WHERE seller_id=?", (sid,)).fetchone()
        n = row[0] if row else 0
        if n >= 8:
            score -= 30; why.append(f"у продавца {n} объявлений")
        elif n >= 4:
            score -= 15; why.append(f"у продавца {n} объявлений")
        elif n <= 1:
            score += 10; why.append("единственное объявление продавца")

    for ph in (listing.get("phones") or [])[:2]:
        row = store.conn.execute(
            "SELECT COUNT(DISTINCT key) FROM phones WHERE phone=?", (ph,)).fetchone()
        if row and row[0] >= 4:
            score -= 25; why.append(f"телефон встречается в {row[0]} объявлениях")
            break

    return max(0, min(100, score)), why


# --------------------------------------------------------- оценка ----

def days_on_market(listing):
    ts = listing.get("created_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TASHKENT_TZ)
    return max(0.0, (datetime.now(dt.tzinfo) - dt).total_seconds() / 86400)


def floor_note(listing):
    f, tf = listing.get("floor"), listing.get("floors_total")
    if not f:
        return None, 0
    if f == 1:
        return "первый этаж", -1
    if tf and f == tf:
        return "последний этаж", -1
    return None, 1


def score_listing(listing, store, cfg, stats, prefs=None):
    """→ dict со сводной оценкой 0..10 и человекочитаемыми пояснениями."""
    prefs = prefs or {}
    pros, cons = [], []
    pts = 5.0

    delta, ptext = price_verdict(listing, stats)
    if delta is not None:
        if delta <= -20: pts += 2.0
        elif delta <= -8: pts += 1.2
        elif delta < 8: pts += 0.3
        elif delta < 20: pts -= 0.8
        else: pts -= 1.8
        (pros if delta < 8 else cons).append("💵 " + ptext)

    m = nearest_metro(listing.get("lat"), listing.get("lon"))
    if m:
        name, km, walk = m
        listing["metro"] = {"name": name, "km": km, "walk": walk}
        if walk <= 10: pts += 1.2; pros.append(f"🚇 {walk} мин пешком до «{name}»")
        elif walk <= 20: pts += 0.4; pros.append(f"🚇 {walk} мин до «{name}»")
        elif walk >= FAR_FROM_METRO_MIN:
            pts -= 1.0; cons.append(f"🚇 до метро «{name}» ~{walk} мин пешком — далеко")
        else: pros.append(f"🚇 {walk} мин до «{name}»")

    work = prefs.get("work_point")
    if work:
        d = distance_to(listing.get("lat"), listing.get("lon"), work)
        if d:
            km, walk = d
            listing["to_work_km"] = km
            if km <= 2: pts += 0.8; pros.append(f"🏢 до работы {km} км ({walk} мин пешком)")
            elif km <= 5: pros.append(f"🏢 до работы {km} км")
            elif km >= 12: pts -= 0.8; cons.append(f"🏢 до работы далеко — {km} км")

    osc, why = owner_score(listing, store, cfg)
    listing["owner_score"] = osc
    listing["owner_why"] = why
    if osc >= 70: pts += 1.5; pros.append(f"👤 похоже на хозяина ({osc}%): " + "; ".join(why[:2]))
    elif osc <= 35: pts -= 1.5; cons.append(f"👤 похоже на маклера ({osc}%): " + "; ".join(why[:2]))
    else: pros.append(f"👤 хозяин/маклер — не ясно ({osc}%)")

    dom = days_on_market(listing)
    if dom is not None:
        listing["days_on_market"] = round(dom, 1)
        if dom <= 0.5: pts += 1.0; pros.append("🆕 опубликовано только что")
        elif dom <= 2: pts += 0.4; pros.append(f"🆕 свежее ({dom:.0f} дн.)")
        elif dom >= 14: pts -= 0.5; cons.append(f"⏳ висит {dom:.0f} дн. — можно торговаться")

    note, fp = floor_note(listing)
    if note: cons.append(f"🏢 {note}")
    pts += fp * 0.3

    area = listing.get("area")
    if area and listing.get("price_usd"):
        listing["price_per_m2"] = round(listing["price_usd"] / area, 2)
        pros.append(f"📐 {area} м² · ${listing['price_per_m2']:.1f}/м²")
    if listing.get("furnished") in ("Да", "yes", True):
        pts += 0.3; pros.append("🛋 с мебелью")
    if (listing.get("house_type") or "").lower().startswith("кирпич"):
        pts += 0.2; pros.append("🧱 кирпичный дом")
    if listing.get("photo_urls"):
        pts += 0.2
    else:
        cons.append("📷 без фото")

    listing["score"] = round(max(0.0, min(10.0, pts)), 1)
    listing["pros"], listing["cons"] = pros, cons
    return listing


def ask_seller(listing):
    """Что уточнить при звонке именно по этому варианту."""
    q = []
    if (listing.get("owner_score") or 50) < 70:
        q.append("вы собственник? есть комиссия?")
    if not listing.get("area"):
        q.append("какая площадь?")
    if not listing.get("floor"):
        q.append("какой этаж?")
    if (listing.get("days_on_market") or 0) >= 14:
        q.append("почему долго не сдаётся, торг возможен?")
    q.append("депозит и срок договора?")
    return q[:4]


# ------------------------------------------------------- геокодирование ----

def geocode(query: str, requests_mod, headers=None):
    """Адрес → (lat, lon, подпись). Бесплатный OpenStreetMap Nominatim."""
    if not query or len(query.strip()) < 3:
        return None
    try:
        r = requests_mod.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{query}, Ташкент, Узбекистан", "format": "json", "limit": 1},
            headers={"User-Agent": "rent-radar/1.0 (personal apartment search)",
                     "Accept-Language": "ru"},
            timeout=20)
        if r.status_code != 200:
            return None
        d = r.json()
        if not d:
            return None
        return float(d[0]["lat"]), float(d[0]["lon"]), d[0].get("display_name", query)[:80]
    except Exception:
        return None

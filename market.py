"""
Ra'no · рыночный анализ вариантов на покупку.

1. Раз в сутки снимает срез Uybor и хранит его в sale.db:
   вся продажа квартир в Ташкенте (~2 000 объявлений) и свежая аренда
   в районах поиска. По каждому объявлению копится история цены и дата,
   когда оно пропало с сайта (продано или снято).
2. Для варианта считает: цену за м² против похожих, срок на рынке и снижения
   цены, оценку аренды и доходность, сценарии цены на 1/3/5 лет, сравнение
   «сдавать / жить» с депозитом — и короткий вердикт.

Внешние допущения (сценарии, ставки, налоги) — в DEFAULT_ASSUMPTIONS с
источниками и датой. Их обновляют в config.json (sale_search.market), а не в коде.
Все цены — цены предложения (объявления), не сделок: так устроены и
официальные индексы (stat.uz строит гедонический индекс по OLX).
"""

import logging
import statistics
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("rent-radar")

UYBOR_API = "https://api.uybor.uz/api/v1/listings"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "ru,uz;q=0.9,en;q=0.8",
}
UYBOR_DISTRICTS = {
    196: "Мирзо-Улугбек", 197: "Юнусабад", 198: "Шайхантахур", 203: "Чиланзар",
    204: "Мирабад", 205: "Яккасарай", 206: "Сергели", 1332: "Янгихаёт",
    671085: "Алмазар", 674731: "Яшнабад",
}

REPORT_URL = "https://claude.ai/code/artifact/4052e7a8-58b5-46e4-824a-d8283a8c5327"

# Допущения на 06.10.2026. Источники и обоснование — в отчёте REPORT_URL.
DEFAULT_ASSUMPTIONS = {
    "as_of": "2026-10-06",
    # Годовое изменение цены вторички в $ по годам 1…5.
    # pess — повтор коррекции 2024–2025 (stat.uz: −8,4% от пика 2024-Q1 до 2025-Q4),
    #        потом медленный рост; base — гедонический индекс stat.uz по Ташкенту
    #        +3,2% г/г (2026-Q2) при ожидаемом ослаблении сума на 1–2,5% в год;
    # opt  — темп весны 2026 (ЦЭИР: +6,1% г/г в $ в апреле), потом замедление.
    "scenarios": {
        "pess": [-0.06, -0.02, 0.02, 0.02, 0.02],
        "base": [0.03, 0.03, 0.03, 0.03, 0.03],
        "opt": [0.08, 0.08, 0.05, 0.05, 0.05],
    },
    "usd_deposit": 0.05,         # bank.uz 06.10.2026: вклады в $ обычно 3–6%, максимум 8%
    "uzs_deposit": 0.18,         # вклады в сумах на год 16–22%
    "rent_tax": 0.12,            # НДФЛ с аренды
    "gain_tax": 0.12,            # налог с прироста при продаже раньше 3 лет владения
    "gain_tax_free_years": 3,
    "vacancy_months": 1,         # простой между арендаторами, мес. в год (допущение)
    "dorm_rent_discount": 0.15,  # бывшее общежитие сдаётся дешевле похожих (допущение)
    "city_gross_yield": 0.093,   # валовая доходность аренды в Ташкенте, ЦБ, 2025
    # средняя цена м² по району, Нац. центр массовой оценки на 01.09.2026 (gazeta.uz, spot.uz);
    # смесь новостроек и вторички — ориентир, не цена конкретной квартиры
    "district_avg_m2_official": {"Шайхантахур": 2144, "Мирабад": 1966, "Яккасарай": 1737,
                                 "Юнусабад": 1441},
    # аренда $/м² в месяц, если своих данных мало: ЦЭИР, апрель 2026;
    # Юнусабад — средняя по Ташкенту, ЦБ, 2-й кв. 2026
    "rent_m2_fallback": {"Мирабад": 11.5, "Шайхантахур": 11.2, "Яккасарай": 10.5,
                         "Юнусабад": 9.5, "*": 9.5},
    # ремонт «работа + материалы», $/м²: Ustabor, смета 60 м² (21.05.2026): эконом ~$53, средний ~$93,
    # комфорт ~$137; косметика $49–98. Берём середину для каждого состояния.
    "renovation_m2": {"average": 50, "none": 95, "box": 130},
    # ипотека на вторичку (bank.uz / depozit.uz, Hamkorbank, 10.2026): 24–27% в сумах, взнос от 25%,
    # до 10 лет, до ~800 млн сум; льготная (Минэкономфин через банки) — 17,5%, до 420 млн, взнос 15%, 20 лет
    "mortgage": {"rate": 0.25, "down": 0.25, "years": 10, "max_uzs": 800_000_000,
                 "soft": {"rate": 0.175, "down": 0.15, "years": 20, "max_uzs": 420_000_000}},
    "scan_every_hours": 24,
    "rent_pages_per_district": 5,   # 500 свежих объявлений аренды на район хватает для медиан
    "sale_max_pages": 40,
}

# Uybor часто ставит «вторичка» новостройкам из ЖК — смотрим и на текст
NEW_WORDS = ["новостройк", "novostroyk", "yangi bino", "янги бино", "yangi uy"]


def looks_new(o_flag, text: str) -> bool:
    return bool(o_flag) or any(w in (text or "").lower() for w in NEW_WORDS)


DORM_WORDS = ["общежит", "галерей", "галерейк", "санузел общий", "санузел на этаже",
              "туалет на этаже", "yotoqxona", "ётоқхона"]


# состояние квартиры: good — можно заезжать, average — освежить, none — капитальный, box — с нуля
REPAIR_FIELD = {"evro": "good", "custom": "good", "sredniy": "average", "chernovaya": "box",
                "евроремонт": "good", "хороший ремонт": "good", "дизайнерский ремонт": "good",
                "авторский ремонт": "good", "средний ремонт": "average", "без ремонта": "none",
                "требует ремонта": "none", "черновая отделка": "box", "коробка": "box"}
REPAIR_WORDS = [  # порядок важен: «требует ремонта» раньше «ремонт»
    ("box", ["коробк", "karobka", "korobka", "черновая", "chernovaya", "без отделки", "qora suvoq",
             "предчистов", "белый каркас"]),
    ("none", ["без ремонта", "bez remont", "требует ремонта", "требует капитальн", "нужен ремонт",
              "под ремонт", "ремонтсиз", "remontsiz", "ta'mirsiz", "tamirsiz", "старый ремонт",
              "eski remont", "ремонт эски", "ремонт: нет", "без ремонт"]),
    ("good", ["евро", "evro", "yevro", "хороший ремонт", "дизайнер", "свежий ремонт", "новый ремонт",
              "yangi remont", "zo'r remont", "ремонти яхши", "yaxshi remont", "капитальный ремонт сделан",
              "сделан капитальный"]),
    ("average", ["средний ремонт", "ремонт средн", "средни", "o'rta remont", "orta remont", "o'rtacha",
                 "ўртача", "ортача", "sredniy", "косметическ", "жилое состояние"]),
]
REPAIR_RU = {"good": "хороший — можно заезжать", "average": "средний — хватит косметики",
             "none": "без ремонта — нужен капитальный", "box": "коробка — отделка с нуля"}
MARKET_REPAIR = {"good": ("evro", "custom"), "average": ("sredniy",), "box": ("chernovaya",)}


def repair_class(l: dict):
    """Состояние квартиры по полю сайта, иначе по тексту объявления; None — не понять."""
    f = (l.get("repair") or "").strip().lower()
    if f in REPAIR_FIELD:
        return REPAIR_FIELD[f]
    text = f"{l.get('title') or ''} {l.get('text') or ''}".lower()
    for cls, words in REPAIR_WORDS:
        if any(w in text for w in words):
            return cls
    return None


def mortgage(price_usd: float, m: dict, uzs_per_usd: float):
    """Аннуитет в сумах: взнос, платёж в месяц, переплата. Не хватает лимита — растёт взнос."""
    down = price_usd * m["down"]
    loan_uzs = (price_usd - down) * uzs_per_usd
    if loan_uzs > m["max_uzs"]:
        loan_uzs = m["max_uzs"]
        down = price_usd - loan_uzs / uzs_per_usd
    r, n = m["rate"] / 12, m["years"] * 12
    pay = loan_uzs * r / (1 - (1 + r) ** -n)
    return {"down": down, "down_share": down / price_usd, "pay_uzs": pay, "pay_usd": pay / uzs_per_usd,
            "over_usd": (pay * n - loan_uzs) / uzs_per_usd, "rate": m["rate"], "years": m["years"]}


def ms_cap(a):
    return f'{a["mortgage"]["soft"]["max_uzs"] / 1e6:.0f}'


def _mln(v):
    return f"{v / 1e6:.1f}".replace(".", ",")


def _r500(v):
    return round(v / 500) * 500


def assumptions(cfg: dict) -> dict:
    over = ((cfg.get("sale_search") or {}).get("market") or {})
    out = dict(DEFAULT_ASSUMPTIONS)
    out.update(over)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_since(ts: str):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def _money(v) -> str:
    return f"{v:,.0f}".replace(",", " ")


def area_band(area) -> str:
    if not area:
        return "?"
    return "s" if area <= 35 else ("m" if area <= 60 else "l")


BAND_RU = {"s": "до 35 м²", "m": "35–60 м²", "l": "больше 60 м²"}


# ------------------------------------------------------------- хранилище ----

def ensure_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS market(
        key TEXT PRIMARY KEY, op TEXT, district TEXT, rooms INTEGER, area REAL,
        price_usd REAL, new_building INTEGER, repair TEXT, created_at TEXT,
        user_id TEXT, dorm INTEGER, first_seen TEXT, last_seen TEXT, removed_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS market_prices(
        key TEXT, seen_at TEXT, price_usd REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS market_op ON market(op, district)")
    conn.commit()


def _row_from_uybor(o: dict, op: str, uzs_per_usd: float):
    """Объявление Uybor → строка среза. None, если цену нельзя привести к $."""
    try:
        price = float(o.get("price"))
        area = float(o.get("square")) if o.get("square") else None
    except (TypeError, ValueError):
        return None
    if op == "rent" and (o.get("pricePeriodUnit") or "month") != "month":
        return None                              # посуточная аренда — другой рынок
    if o.get("priceType") == "sqm":
        if not area:
            return None
        price *= area                            # цена указана за м²
    cur = (o.get("priceCurrency") or "").lower()
    if cur in ("uzs", "sum", "сум"):
        price /= uzs_per_usd
    elif cur not in ("usd", "uye", "у.е."):
        return None
    if not area or not (8 <= area <= 400) or price <= 0:
        return None
    m2 = price / area
    if op == "sale" and not (250 <= m2 <= 8000):
        return None                              # опечатки и «цена за м² вместо цены»
    if op == "rent" and not (2 <= m2 <= 80):
        return None
    try:
        rooms = int(o.get("room")) if o.get("room") not in (None, "") else None
    except (TypeError, ValueError):
        rooms = None
    desc = (o.get("description") or "").lower()
    return {
        "key": f'{op}:uybor:{o.get("id")}', "op": op,
        "district": UYBOR_DISTRICTS.get(o.get("districtId")),
        "rooms": rooms, "area": area, "price_usd": round(price, 1),
        "new_building": 1 if looks_new(o.get("isNewBuilding"), desc) else 0,
        "repair": o.get("repair"), "created_at": o.get("createdAt"),
        "user_id": str(o.get("userId")),
        "dorm": 1 if any(w in desc for w in DORM_WORDS) else 0,
    }


def _upsert(conn, r: dict, now: str):
    old = conn.execute("SELECT price_usd FROM market WHERE key=?", (r["key"],)).fetchone()
    if old is None:
        conn.execute(
            "INSERT INTO market(key, op, district, rooms, area, price_usd, new_building, "
            "repair, created_at, user_id, dorm, first_seen, last_seen, removed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (r["key"], r["op"], r["district"], r["rooms"], r["area"], r["price_usd"],
             r["new_building"], r["repair"], r["created_at"], r["user_id"], r["dorm"],
             now, now))
        conn.execute("INSERT INTO market_prices(key, seen_at, price_usd) VALUES(?,?,?)",
                     (r["key"], now, r["price_usd"]))
        return
    if abs((old[0] or 0) - r["price_usd"]) >= 1:
        conn.execute("INSERT INTO market_prices(key, seen_at, price_usd) VALUES(?,?,?)",
                     (r["key"], now, r["price_usd"]))
    conn.execute("UPDATE market SET price_usd=?, area=?, rooms=?, district=?, last_seen=?, "
                 "removed_at=NULL WHERE key=?",
                 (r["price_usd"], r["area"], r["rooms"], r["district"], now, r["key"]))


def _fetch(params: dict) -> list:
    r = requests.get(UYBOR_API, params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return (r.json() or {}).get("results") or []


def scan(store, cfg: dict, district_ids=None) -> dict:
    """Полный срез: продажа по всему Ташкенту + свежая аренда в районах поиска.
    Объявления, которых нет в полном срезе продажи, помечаются снятыми."""
    a = assumptions(cfg)
    conn = store.conn
    ensure_tables(conn)
    rate = cfg.get("uzs_per_usd", 11900)
    started = _now()
    stats = {"sale": 0, "rent": 0}

    complete = False
    for page in range(1, a["sale_max_pages"] + 1):
        res = _fetch({"operationType__eq": "sale", "category__eq": 7, "region__eq": 13,
                      "limit": 100, "page": page, "sort": "-createdAt"})
        for o in res:
            row = _row_from_uybor(o, "sale", rate)
            if row:
                _upsert(conn, row, started)
                stats["sale"] += 1
        if len(res) < 100:
            complete = True
            break
        time.sleep(0.4)
    if complete:
        conn.execute("UPDATE market SET removed_at=? WHERE op='sale' AND removed_at IS NULL "
                     "AND last_seen < ?", (started, started))

    for did in district_ids or []:
        for page in range(1, a["rent_pages_per_district"] + 1):
            res = _fetch({"operationType__eq": "rent", "category__eq": 7, "region__eq": 13,
                          "district__eq": did, "limit": 100, "page": page,
                          "sort": "-createdAt"})
            for o in res:
                row = _row_from_uybor(o, "rent", rate)
                if row:
                    _upsert(conn, row, started)
                    stats["rent"] += 1
            if len(res) < 100:
                break
            time.sleep(0.4)
    conn.commit()
    store.set_kv("market_scan_at", started)
    log.info("[рынок] срез Uybor: продажа %d, аренда %d", stats["sale"], stats["rent"])
    return stats


def maybe_scan(store, cfg: dict, district_ids=None) -> bool:
    a = assumptions(cfg)
    last = _days_since(store.get_kv("market_scan_at") or "")
    if last is not None and last * 24 < a["scan_every_hours"]:
        return False
    tried = _days_since(store.get_kv("market_scan_try") or "")
    if tried is not None and tried * 24 < 1:
        return False                             # после неудачи пробуем не чаще раза в час
    store.set_kv("market_scan_try", _now())
    try:
        scan(store, cfg, district_ids)
        return True
    except Exception as e:                       # анализ не должен ронять поиск
        log.warning("[рынок] срез не удался: %s", e)
        return False


# ---------------------------------------------------------------- расчёты ----

def _median(xs):
    return statistics.median(xs) if xs else None


def is_dorm(l: dict) -> bool:
    text = f"{l.get('title', '')} {l.get('text', '')}".lower()
    return any(w in text for w in DORM_WORDS)


def comparables(store, l: dict, min_n: int = 6, repair=None):
    """Медиана $/м² похожих активных объявлений о продаже.
    Бывшее общежитие сравниваем только с общежитиями: это другой рынок
    (в срезе 06.10 их $/м² вдвое ниже обычных квартир той же площади).
    Похожая площадь — ±30%, потом ±50%; сегмент (новостройка/вторичка) и район
    ослабляем последними, пока не наберётся min_n объявлений."""
    ensure_tables(store.conn)
    area = l.get("area")
    if not area:
        return None
    nb = 1 if looks_new(l.get("new_building"), l.get("text")) else 0
    dorm = 1 if is_dorm(l) else 0
    own = l.get("key") or ""                  # ключи среза и поиска совпадают: sale:uybor:<id>
    codes = MARKET_REPAIR.get(repair) if repair else None
    if repair and not codes:
        return None
    rows = [r[:6] for r in store.conn.execute(
        "SELECT key, district, area, price_usd, new_building, dorm, repair FROM market "
        "WHERE op='sale' AND removed_at IS NULL AND area > 0").fetchall()
        if r[0] != own and (not codes or r[6] in codes)]

    def pick(district, segment, spread):
        return [price / ar for key, d, ar, price, rnb, rdorm in rows
                if rdorm == dorm
                and (not district or d == l.get("district"))
                and (not segment or rnb == nb)
                and (1 - spread) * area <= ar <= (1 + spread) * area]

    kind = "бывшие общежития" if dorm else ("новостройки" if nb else "вторичка")
    d = l.get("district")
    for dist, seg, spread in ((True, True, 0.3), (True, True, 0.5), (False, True, 0.3),
                              (False, True, 0.5), (True, False, 0.5)):
        vals = pick(dist, seg, spread)
        if len(vals) >= min_n:
            where = d if dist else "весь Ташкент"
            what = kind if (seg or dorm) else "вся продажа"
            rng = f"{(1 - spread) * area:.0f}–{(1 + spread) * area:.0f} м²"
            rep = {"good": ", хороший ремонт", "average": ", средний ремонт", "box": ", коробка"}.get(repair, "")
            return {"median_m2": _median(vals), "n": len(vals), "label": f"{where}, {what}{rep}, {rng}"}
    return None


def rent_comparables(store, l: dict, min_n: int = 5):
    """Ожидаемая аренда, $/мес: медиана объявлений аренды в том же районе
    с тем же числом комнат и похожей площадью (±30%); иначе $/м² района × площадь."""
    ensure_tables(store.conn)
    area, rooms = l.get("area"), l.get("rooms")
    rows = store.conn.execute(
        "SELECT district, rooms, area, price_usd FROM market "
        "WHERE op='rent' AND removed_at IS NULL AND area > 0").fetchall()
    d = l.get("district")
    if area:
        same = [p for dd, r, ar, p in rows
                if dd == d and (rooms is None or r == rooms) and 0.7 * area <= ar <= 1.3 * area]
        if len(same) >= min_n:
            return {"rent": _median(same), "n": len(same),
                    "label": f"{d}, {rooms or '?'}-комн, {0.7 * area:.0f}–{1.3 * area:.0f} м²"}
        band = area_band(area)
        per = [p / ar for dd, r, ar, p in rows if dd == d and area_band(ar) == band]
        if len(per) >= min_n:
            return {"rent": _median(per) * area, "n": len(per),
                    "label": f"{d}, {BAND_RU.get(band, '')}, $/м² × площадь"}
    return None


def price_history(store, l: dict):
    ensure_tables(store.conn)
    key = (l.get("key") or "")
    rows = store.conn.execute(
        "SELECT seen_at, price_usd FROM market_prices WHERE key=? ORDER BY seen_at",
        (key,)).fetchall()
    return rows


def path_value(price: float, rates: list, years: int) -> float:
    v = price
    for i in range(years):
        v *= 1 + rates[min(i, len(rates) - 1)]
    return v


def analyze(store, l: dict, cfg: dict) -> dict:
    """Все цифры анализа варианта. Ничего не печатает — для этого format_analysis."""
    a = assumptions(cfg)
    price, area = l.get("price_usd"), l.get("area")
    out = {"price": price, "area": area, "flags": [], "assumptions": a}
    if not price:
        return out
    cls = repair_class(l)
    out["repair"] = cls
    if area:
        out["m2"] = price / area
        comp = comparables(store, l, repair=cls) if cls else None    # сначала — с таким же ремонтом
        out["comp_same_repair"] = bool(comp)
        comp = comp or comparables(store, l)
        if comp:
            out["comp"] = comp
            gap = out["m2"] / comp["median_m2"] - 1
            if abs(gap) <= 0.4:               # разница больше 40% — сравниваем не с тем
                out["gap"] = gap
                out["fair"] = comp["median_m2"] * area
            else:
                out["comp_unreliable"] = gap
        off = a["district_avg_m2_official"].get(l.get("district"))
        if off:
            out["official_m2"] = off
        reno_m2 = (a.get("renovation_m2") or {}).get(cls)
        if reno_m2:                                   # во что обойдётся «как у похожих с хорошим ремонтом»
            out["reno"] = reno_m2 * area
            out["all_in"] = price + out["reno"]
            good = comparables(store, l, repair="good")
            if good:
                g = out["all_in"] / area / good["median_m2"] - 1
                if abs(g) <= 0.4:
                    out["gap_all_in"], out["good_comp"] = g, good

    listed = l.get("listed_since") or l.get("created_at")
    out["days_on_market"] = _days_since(listed)
    hist = price_history(store, l)
    if len(hist) >= 2 and hist[0][1] and hist[-1][1] and hist[-1][1] != hist[0][1]:
        out["price_change"] = (hist[0][1], hist[-1][1])

    # аренда: свои данные Uybor (район, комнаты, похожая площадь), иначе средние ЦЭИР/ЦБ
    rent, rent_src = None, None
    if area:
        rc = rent_comparables(store, l)
        if rc:
            rent, rent_src = rc["rent"], f'медиана {rc["n"]} объявл.: {rc["label"]}'
        else:
            fb = a["rent_m2_fallback"]
            rent = fb.get(l.get("district"), fb.get("*")) * area
            rent_src = "средняя $/м² по району (ЦЭИР/ЦБ, 2026) × площадь"
    rent_ads = None
    base_cost = out.get("all_in") or price          # вложено: цена + ремонт
    if rent and rent * 12 / base_cost > a["city_gross_yield"] * 1.6:
        # по объявлениям аренды доходность выходит слишком высокой — в расчёт берём
        # консервативную среднюю по району, а цифру из объявлений показываем рядом
        rent_ads = rent
        fb = a["rent_m2_fallback"]
        rent = fb.get(l.get("district"), fb.get("*")) * area
        rent_src = "средняя $/м² по району (ЦЭИР/ЦБ, 2026) × площадь"
    if rent:
        if is_dorm(l):
            k = 1 - a["dorm_rent_discount"]       # своих данных по аренде общежитий почти нет
            rent *= k
            rent_ads = rent_ads * k if rent_ads else None
            rent_src += f', −{a["dorm_rent_discount"] * 100:.0f}% за общежитие (допущение)'
        out["rent_ads"] = rent_ads
        months = 12 - a["vacancy_months"]
        net_year = rent * months * (1 - a["rent_tax"])
        out.update(rent=rent, rent_src=rent_src,
                   gross_yield=rent * 12 / base_cost,
                   net_yield=net_year / base_cost,
                   payback_years=base_cost / net_year if net_year else None)

    scen = a["scenarios"]
    out["values"] = {k: {y: path_value(price, scen[k], y) for y in (1, 3, 5)} for k in scen}

    def total(years, scenario):
        v = path_value(price, scen[scenario], years)
        gain = v - price
        tax = a["gain_tax"] * gain if gain > 0 and years < a["gain_tax_free_years"] else 0
        rent_total = 0.0
        if out.get("rent"):
            rent_total = out["rent"] * (12 - a["vacancy_months"]) * (1 - a["rent_tax"]) * years
        return gain - tax + rent_total - out.get("reno", 0)

    out["invest"] = {y: {k: total(y, k) for k in scen} for y in (1, 3, 5)}
    out["deposit_usd"] = {y: price * ((1 + a["usd_deposit"]) ** y - 1) for y in (1, 3, 5)}
    out["forgone_month"] = base_cost * a["usd_deposit"] / 12
    m = a.get("mortgage")
    if m:
        rate = cfg.get("uzs_per_usd") or 11900
        out["mortgage"] = mortgage(price, m, rate)
        if m.get("soft"):
            out["mortgage_soft"] = mortgage(price, m["soft"], rate)

    if is_dorm(l):
        out["flags"].append("dorm")
    if (out.get("days_on_market") or 0) > 60:
        out["flags"].append("stale")
    if l.get("seller_kind") == "agency":
        out["flags"].append("agency")
    if l.get("new_building"):
        out["flags"].append("new_building")

    # торг: от рыночной цены (если дороже похожих) — минус аргументы
    gap0 = out.get("gap")
    k, args = 0.03, []
    dom = out.get("days_on_market") or 0
    if dom >= 45:
        k += 0.02
        args.append(f"висит {dom:.0f} дн.")
    pc = out.get("price_change")
    if pc and pc[1] < pc[0]:
        k += 0.02
        args.append("цену уже снижали")
    if cls in ("none", "box"):
        k += 0.02
        args.append(f"нужен ремонт ~${_money(out.get('reno') or 0)}")
    if gap0 is not None and gap0 >= 0.03:
        args.insert(0, f"дороже похожих на {gap0 * 100:.0f}%")
    if l.get("seller_kind") == "agency":
        args.append("комиссию маклера — тоже в торг")
    calm = gap0 is not None and gap0 <= -0.10 and cls not in ("none", "box")
    if calm:
        k = 0.02                                       # цена уже ниже рынка — давить не стоит
    k = min(k, 0.12)
    start = min(price, out["fair"]) if out.get("fair") else price
    out["bargain"] = {"offer": _r500(start * (1 - k)), "ceiling": _r500(min(price, start * (1 - k / 3))),
                      "args": args, "calm": calm}

    # для вывода: похожие с тем же ремонтом — честное сравнение; нет таких — «цена + ремонт» против хорошего
    use_all_in = out.get("gap_all_in") is not None and not out.get("comp_same_repair")
    gap = out["gap_all_in"] if use_all_in else out.get("gap")
    out["gap_fair"] = gap
    good_yield = out.get("net_yield") is not None and out["net_yield"] >= a["usd_deposit"] + 0.02
    pct = f"{abs(gap) * 100:.0f}%" if gap is not None else ""
    with_reno = (" даже с ремонтом" if gap < 0 else " с учётом ремонта") if use_all_in else \
        (" с таким же ремонтом" if out.get("comp_same_repair") else "")
    if gap is not None and gap >= 0.10:
        verdict = ("🔴", "Дорого", f"на {pct} дороже похожих{with_reno}")
    elif gap is not None and gap <= -0.10:
        verdict = ("🟢", "Хорошая цена", f"на {pct} дешевле похожих{with_reno}")
    elif gap is None and out.get("comp_unreliable") is not None and out["comp_unreliable"] < 0:
        verdict = ("⚪", "Подозрительно дёшево", "в разы дешевле похожих в районе — проверьте адрес, "
                                                  "документы и состояние")
    elif gap is None:
        verdict = ("⚪", "Сравнить не с чем", "мало похожих объявлений — оценивайте на месте")
    else:
        verdict = ("🟡", "Цена в рынке", (f"на {pct} {'дешевле' if gap < 0 else 'дороже'} похожих{with_reno}"
                                          if abs(gap) >= 0.02 else "как у похожих") + " — торгуйтесь")
    if "dorm" in out["flags"] and verdict[0] != "🔴":
        verdict = ("🟠", verdict[1] + ", но бывшее общежитие", verdict[2] + "; проверьте кадастр и приватизацию")
    out["verdict"] = verdict
    return out


def format_analysis(store, l: dict, cfg: dict) -> str:
    """Разбор варианта: вывод сверху, дальше — ремонт, ипотека, торг, вложение;
    как считала — в сворачиваемом блоке."""
    x = analyze(store, l, cfg)
    if not x.get("price"):
        return ""
    a, price, area = x["assumptions"], x["price"], x.get("area")
    head = [f'{l["rooms"]}-комн' if l.get("rooms") else "", f'{area:g} м²' if area else "", f'${_money(price)}']
    lines = ["📊 <b>Анализ</b> · " + ", ".join(h for h in [l.get("district") or ""] + head if h), ""]
    em, title, why = x["verdict"]
    lines.append(f"{em} <b>{title}</b> — {why}")

    # цена
    c = x.get("comp")
    if x.get("gap") is not None:
        side = "ниже" if x["gap"] < 0 else "выше"
        size = f'на {abs(x["gap"]) * 100:.0f}% {side}' if abs(x["gap"]) >= 0.02 else "на уровне"
        lines.append(f'💵 ${_money(x["m2"])}/м² — {size} похожих (${_money(c["median_m2"])}/м²)'
                     + (f' · по рынку ≈ ${_money(_r500(x["fair"]))}' if x.get("fair") else ""))
    elif c:
        lines.append(f'💵 ${_money(x["m2"])}/м², а похожие — ${_money(c["median_m2"])}/м²: разница слишком '
                     f'большая, чтобы сравнивать, — смотрите квартиру')
    elif x.get("m2"):
        lines.append(f'💵 ${_money(x["m2"])}/м² — похожих пока мало для сравнения')

    # ремонт
    cls = x.get("repair")
    if cls == "good":
        lines.append("🛠 Ремонт хороший — можно заезжать")
    elif cls and x.get("reno"):
        what = {"average": "освежить", "none": "капитальный", "box": "отделка с нуля"}[cls]
        line = f'🛠 Ремонт: {REPAIR_RU[cls].split(" — ")[0]} — {what} ~${_money(x["reno"])}. С ним ≈ ${_money(x["all_in"])}'
        g = x.get("gap_all_in")
        if g is not None:
            line += (f', на {abs(g) * 100:.0f}% {"дешевле" if g < 0 else "дороже"} похожих с хорошим ремонтом'
                     if abs(g) >= 0.02 else ", как похожие с хорошим ремонтом")
        lines.append(line)
    else:
        lines.append(f'🛠 Ремонт не указан — уточните. Если нужен капитальный, добавьте ~$'
                     f'{_money((a.get("renovation_m2") or {}).get("none", 95) * (area or 0))}' if area else
                     "🛠 Ремонт не указан — уточните у продавца")

    # ипотека
    m = x.get("mortgage")
    if m:
        lines.append(f'🏦 Ипотека: взнос ${_money(m["down"])} ({m["down_share"] * 100:.0f}%), '
                     f'≈ {_mln(m["pay_uzs"])} млн сум/мес (~${_money(m["pay_usd"])}) на {m["years"]} лет')
        if x.get("rent"):
            diff = m["pay_usd"] - x["rent"]
            lines.append(f'   снимать такую же — ~${_money(x["rent"])}/мес'
                         + (f': ипотека дороже на ~${_money(diff)}/мес, зато квартира ваша' if diff > 30 else
                            ": платёж как аренда — выгоднее брать"))
        if "dorm" in x["flags"]:
            lines.append("   ⚠️ Бывшее общежитие: без кадастра банки ипотеку не дают")

    # торг
    bg = x.get("bargain")
    if bg:
        if bg["calm"]:
            line = f'🤝 Торг: цена уже ниже рынка — просите ~${_money(bg["offer"])}, сильно давить не стоит'
        else:
            line = f'🤝 Торг: начните с ~${_money(bg["offer"])}, потолок ~${_money(bg["ceiling"])}'
        if bg["args"]:
            line += " · аргументы: " + ", ".join(bg["args"])
        lines.append(line)

    # вложение
    if x.get("rent"):
        pb = x.get("payback_years")
        dep = a["usd_deposit"]
        cmp_dep = "лучше" if x["net_yield"] >= dep + 0.005 else ("как" if x["net_yield"] >= dep - 0.005 else "хуже")
        lines.append(f'📈 Как вложение: сдавать ~${_money(x["rent"])}/мес → ~{f"{x['net_yield'] * 100:.1f}".replace(".", ",")}% в год чистыми, '
                     f'{cmp_dep} вклада в $ ({dep * 100:.0f}%)' + (f', окупится за ~{pb:.0f} лет' if pb else ""))
    v = x["values"]
    lines.append(f'   через 5 лет квартира скорее ~${_money(_r500(v["base"][5]))} '
                 f'(от ${_money(_r500(v["pess"][5]))} до ${_money(_r500(v["opt"][5]))})')
    # как считала — свёрнуто
    d = []
    if c:
        d.append(f'Похожие: медиана ${_money(c["median_m2"])}/м², {c["n"]} объявл. ({c["label"]})')
    if x.get("good_comp"):
        gc = x["good_comp"]
        d.append(f'С хорошим ремонтом: ${_money(gc["median_m2"])}/м², {gc["n"]} объявл.')
    if x.get("official_m2"):
        d.append(f'средняя по району: ${_money(x["official_m2"])}/м² (госоценка на 01.09.2026, новостройки и вторичка вместе)')
    if x.get("days_on_market") is not None:
        t = f'На рынке ~{x["days_on_market"]:.0f} дн.'
        pc = x.get("price_change")
        if pc:
            t += f'; цена: ${_money(pc[0])} → ${_money(pc[1])} ({(pc[1] / pc[0] - 1) * 100:+.0f}%)'
        d.append(t)
    rm = a.get("renovation_m2") or {}
    if rm:
        d.append(f'Ремонт с материалами (Ustabor, 2026): освежить ~${rm.get("average")}/м², '
                 f'капитальный ~${rm.get("none")}/м², с нуля ~${rm.get("box")}/м²')
    if m:
        t = (f'Ипотека — типовые условия на вторичку: ~{m["rate"] * 100:.0f}% в сумах, взнос от 25%, '
             f'{m["years"]} лет, переплата ~${_money(m["over_usd"])}')
        ms = x.get("mortgage_soft")
        if ms:
            soft_rate = f'{ms["rate"] * 100:.1f}'.replace(".", ",")
            t += (f'. Льготная ({soft_rate}%, {ms["years"]} лет, до {ms_cap(a)} млн сум), если подходите: '
                  f'≈ {_mln(ms["pay_uzs"])} млн сум/мес, взнос ${_money(ms["down"])}')
        d.append(t + ". Платёж в сумах: если сум слабеет, в $ он со временем легче")
    if x.get("rent"):
        t = f'Аренда: {x["rent_src"]}; налог {a["rent_tax"] * 100:.0f}%, месяц простоя в год'
        if x.get("rent_ads"):
            t += f'; в объявлениях похожие сдают за ~${_money(x["rent_ads"])} — взята осторожная оценка'
        d.append(t)
        inv, depo = x["invest"], x["deposit_usd"]
        base_cost = x.get("all_in") or price
        d.append(f'Купить{" с ремонтом" if x.get("reno") else ""} и сдавать 3 года: ≈ '
                 f'{inv[3]["base"] / base_cost * 100:+.0f}% (${_money(inv[3]["base"])}); вклад в $: '
                 f'{depo[3] / price * 100:+.0f}%')
    d.append(f'Цена через 1 / 3 / 5 лет (база): ${_money(v["base"][1])} / ${_money(v["base"][3])} / ${_money(v["base"][5])}')
    d.append(f'Цены — предложения, не сделок; допущения на {a["as_of"]}')
    lines.append("")
    lines.append("<blockquote expandable>🔍 <b>Как считала</b>\n" + "\n".join("• " + t for t in d) + "</blockquote>")
    lines.append(f'<a href="{REPORT_URL}">Подробно о рынке и методике</a>')
    return "\n".join(lines)


def summary_text(store, cfg: dict, districts: list) -> str:
    """/rynok — сводка среза по районам поиска."""
    ensure_tables(store.conn)
    a = assumptions(cfg)
    last = store.get_kv("market_scan_at")
    if not last:
        return "📊 Среза рынка ещё нет — он появится после ближайшего прохода поиска."
    lines = [f"📊 <b>Рынок по срезу Uybor</b> на {last[:10]}",
             "Медиана $/м², вторичка · новостройки · аренда в месяц"]
    for d in districts:
        rows = store.conn.execute(
            "SELECT op, new_building, price_usd/area FROM market "
            "WHERE removed_at IS NULL AND district=? AND area>0", (d,)).fetchall()
        sec = [r[2] for r in rows if r[0] == "sale" and not r[1]]
        new = [r[2] for r in rows if r[0] == "sale" and r[1]]
        rent = [r[2] for r in rows if r[0] == "rent"]
        def fmt(xs, dec=0):
            return (f"${xs:,.{dec}f}".replace(",", " ") if xs else "—")
        lines.append(f"• {d}: {fmt(_median(sec))} ({len(sec)}) · {fmt(_median(new))} ({len(new)})"
                     f" · {fmt(_median(rent), 1)}/м² ({len(rent)})")
    sold = store.conn.execute(
        "SELECT COUNT(*) FROM market WHERE op='sale' AND removed_at IS NOT NULL").fetchone()[0]
    lines.append(f"\nСнято с продажи с начала наблюдений: {sold}")
    s = a["scenarios"]
    lines.append(f'Сценарии на год (цена в $): спад {s["pess"][0] * 100:+.0f}%, '
                 f'база {s["base"][0] * 100:+.0f}%, рост {s["opt"][0] * 100:+.0f}%')
    lines.append(f'<a href="{REPORT_URL}">Полный отчёт о рынке</a>')
    return "\n".join(lines)

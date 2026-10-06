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


def comparables(store, l: dict, min_n: int = 6):
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
    rows = [r for r in store.conn.execute(
        "SELECT key, district, area, price_usd, new_building, dorm FROM market "
        "WHERE op='sale' AND removed_at IS NULL AND area > 0").fetchall() if r[0] != own]

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
            return {"median_m2": _median(vals), "n": len(vals), "label": f"{where}, {what}, {rng}"}
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
    if area:
        out["m2"] = price / area
        comp = comparables(store, l)
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
    if rent and rent * 12 / price > a["city_gross_yield"] * 1.6:
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
        out.update(rent=rent, rent_src=rent_src,
                   gross_yield=rent * 12 / price,
                   net_yield=rent * months * (1 - a["rent_tax"]) / price)

    scen = a["scenarios"]
    out["values"] = {k: {y: path_value(price, scen[k], y) for y in (1, 3, 5)} for k in scen}

    def total(years, scenario):
        v = path_value(price, scen[scenario], years)
        gain = v - price
        tax = a["gain_tax"] * gain if gain > 0 and years < a["gain_tax_free_years"] else 0
        rent_total = 0.0
        if out.get("rent"):
            rent_total = out["rent"] * (12 - a["vacancy_months"]) * (1 - a["rent_tax"]) * years
        return gain - tax + rent_total

    out["invest"] = {y: {k: total(y, k) for k in scen} for y in (1, 3, 5)}
    out["deposit_usd"] = {y: price * ((1 + a["usd_deposit"]) ** y - 1) for y in (1, 3, 5)}
    out["forgone_month"] = price * a["usd_deposit"] / 12

    if is_dorm(l):
        out["flags"].append("dorm")
    if (out.get("days_on_market") or 0) > 60:
        out["flags"].append("stale")
    if l.get("seller_kind") == "agency":
        out["flags"].append("agency")
    if l.get("new_building"):
        out["flags"].append("new_building")

    gap = out.get("gap")
    good_yield = out.get("net_yield") is not None and out["net_yield"] >= a["usd_deposit"] + 0.02
    if gap is not None and gap >= 0.10:
        verdict = ("🔴", "дороже похожих — торговаться или искать дальше")
    elif gap is not None and gap <= -0.10 and good_yield:
        verdict = ("🟢", "ниже рынка, и сдавать выгоднее депозита в $")
    elif gap is not None and gap <= -0.10:
        verdict = ("🟢", "ниже рынка — запас против падения цен")
    elif gap is None:
        verdict = ("⚪", "мало похожих объявлений для сравнения")
    else:
        verdict = ("🟡", "в рынке — имеет смысл торговаться")
    if "dorm" in out["flags"] and verdict[0] != "🔴":
        verdict = ("🟠", verdict[1] + "; но бывшее общежитие — проверьте документы")
    out["verdict"] = verdict
    return out


def format_analysis(store, l: dict, cfg: dict) -> str:
    x = analyze(store, l, cfg)
    if not x.get("price"):
        return ""
    a = x["assumptions"]
    head = [f'{l["rooms"]}-комн' if l.get("rooms") else "",
            f'{l["area"]:g} м²' if l.get("area") else "",
            f'${_money(x["price"])}']
    lines = ["📊 <b>Анализ</b> · " + (l.get("district") or "") + ", "
             + ", ".join(h for h in head if h)]

    if x.get("gap") is not None:
        c = x["comp"]
        side = "ниже" if x["gap"] < 0 else "выше"
        size = f'на {abs(x["gap"]) * 100:.0f}% {side}' if abs(x["gap"]) >= 0.02 else "на уровне"
        lines.append(f'💵 ${_money(x["m2"])}/м² — {size} похожих '
                     f'(медиана ${_money(c["median_m2"])}/м², {c["n"]} объявл.: {c["label"]})')
    elif x.get("comp"):
        c = x["comp"]
        lines.append(f'💵 ${_money(x["m2"])}/м² — похожие ({c["label"]}) стоят ${_money(c["median_m2"])}/м², '
                     f'разница слишком большая, чтобы считать их аналогами — смотрите квартиру')
    elif x.get("m2"):
        lines.append(f'💵 ${_money(x["m2"])}/м² — похожих в срезе пока мало для сравнения')
    if x.get("official_m2"):
        lines.append(f'   средняя по району: ${_money(x["official_m2"])}/м² '
                     f'(госоценка на 01.09.2026, новостройки и вторичка вместе)')

    dom = x.get("days_on_market")
    pc = x.get("price_change")
    market_line = f"⏳ На рынке ~{dom:.0f} дн." if dom is not None else ""
    if pc:
        delta = pc[1] / pc[0] - 1
        market_line += f' · цена менялась: ${_money(pc[0])} → ${_money(pc[1])} ({delta * 100:+.0f}%)'
    if market_line:
        lines.append(market_line)

    if x.get("rent"):
        lines.append(f'🔑 Сдать: ~${_money(x["rent"])}/мес → {x["gross_yield"] * 100:.1f}% годовых, '
                     f'~{x["net_yield"] * 100:.1f}% после налога 12% и месяца простоя '
                     f'(депозит в $ ~{a["usd_deposit"] * 100:.0f}%)')
        note = f'   аренда — {x["rent_src"]}'
        if x.get("rent_ads"):
            note += f'; в объявлениях похожие сдают за ~${_money(x["rent_ads"])} — взята осторожная оценка'
        elif x["gross_yield"] > a["city_gross_yield"] * 1.3:
            note += (f'; это выше средней по Ташкенту ({a["city_gross_yield"] * 100:.1f}%, ЦБ) — '
                     f'проверьте реальную аренду на месте')
        lines.append(note)
        lines.append(f'🏠 Жить: не платите ~${_money(x["rent"])}/мес аренды, а теряете '
                     f'~${_money(x["forgone_month"])}/мес процентов по вкладу в $')

    v = x["values"]
    lines.append(f'📈 Цена через 1 / 3 / 5 лет, базовый сценарий: '
                 f'${_money(v["base"][1])} / ${_money(v["base"][3])} / ${_money(v["base"][5])}')
    lines.append(f'   разброс через 5 лет: ${_money(v["pess"][5])} (спад) … ${_money(v["opt"][5])} (рост)')
    if x.get("rent"):
        inv, dep = x["invest"], x["deposit_usd"]
        lines.append(f'💼 Купить и сдавать 3 года (база): ≈ {inv[3]["base"] / x["price"] * 100:+.0f}% '
                     f'(${_money(inv[3]["base"])}); вклад в $: {dep[3] / x["price"] * 100:+.0f}%')

    if "dorm" in x["flags"]:
        lines.append("⚠️ Бывшее общежитие: банки без кадастра ипотеку не дают — "
                     "покупателей при перепродаже меньше; проверьте приватизацию и кадастр")
    if "stale" in x["flags"]:
        lines.append("↘️ Долго продаётся — хороший аргумент для торга")
    if x.get("fair") and x.get("gap", 0) > 0.03:
        lines.append(f'🎯 Цель для торга: ~${_money(x["fair"])} (медиана похожих × площадь)')
    if "agency" in x["flags"]:
        lines.append("ℹ️ Продаёт агентство: уточните комиссию и кто её платит")

    em, why = x["verdict"]
    lines.append(f"\n{em} <b>Вердикт:</b> {why}")
    lines.append(f'<a href="{REPORT_URL}">Как считается и что с рынком</a> · '
                 f'допущения на {a["as_of"]}, цены предложения')
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

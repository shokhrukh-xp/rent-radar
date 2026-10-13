"""
Ra'no · где квартира внутри района.

В Ташкенте место понимают по ориентирам: «Узбум», «Ц-1», «Госпитальный», «метро Ойбек».
Здесь:
  • ориентир из текста объявления («Ориентир: …», «рядом с метро …», «мўлжал: …»);
  • справочник ориентиров, собранный из самих объявлений: у объявлений Uybor есть и текст
    с ориентиром, и координаты — медиана координат всех, кто пишет «Узбум», и есть Узбум.
    Названия, которые встречаются по всему городу («Корзинка», «школа»), отсеиваются сами:
    у них большой разброс точек;
  • точка объявления: координаты сайта → ориентир → станция метро; с проверкой района
    (точка, которая попала в чужой район, — ошибка, её не берём);
  • соседи в радиусе и «дорогая / дешёвая часть района»;
  • точка по запросу клиента («рядом с метро Ойбек», «у Ц-1») для поиска в радиусе.
"""

import logging
import math
import re
import statistics
import time

log = logging.getLogger("rent-radar")

# Ташкент с ближними массивами — всё, что за рамкой, считаем ошибкой координат
BBOX = (41.13, 41.47, 69.05, 69.52)

_CYR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "j", "з": "z",
        "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "a", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
        "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "a", "қ": "k", "ғ": "g",
        "ҳ": "h"}

# слова, которые ничего не говорят о месте (в «скелетной» записи, см. skel)
STOP = {
    "ryadam", "vazle", "akala", "naprativ", "blizka", "nedaleka", "ryadam", "u", "v", "s", "na", "i",
    "za", "ot", "da", "do", "pa", "k", "ili", "eta", "esli", "tak", "uje", "ochen", "vse", "est",
    "ul", "ulitsa", "ulitsi", "kucha", "kuchasi", "prospekt", "pr", "prasp", "pereulak", "tupik",
    "massiv", "massiva", "mavze", "mahalla", "kvartal", "kvartala", "rayan", "rayana", "tuman",
    "tumani", "gorad", "shahri", "shahar", "tashkent", "tashkenta", "toshkent", "uzbekistan",
    "metra", "m", "st", "stantsiya", "stantsii", "bekat", "dam", "dama", "jk", "jilaya", "kampleks",
    "kvartira", "kvartiri", "pradaetsya", "pradaem", "pradam", "satiladi", "sotiladi", "ijara",
    "minut", "min", "pashkam", "peshkam", "piyada", "tsentr", "tsentra", "markaz", "astanavka",
    "astanavki", "magazin", "magazini", "rinak", "rinka", "bazar", "shkala", "shkali", "detsad",
    "sad", "bolnitsa", "park", "mechet", "masjid", "ryad", "taraf", "tarafi", "yaqin", "yakin",
    "yanida", "oldida", "raparasida", "ryadom", "lyuboy", "vid", "vidam", "yuli",
    "arientir", "arientiri", "maljal", "mulijal", "muljal", "ar r", "shase", "radam", "masiv",
    "naberejnaya", "plashad", "ploshad", "kalledj", "litsey", "instutut", "institut", "universitet",
    "yangi", "nahiyasi", "naviy", "navaya", "stariy", "staraya", "raspalajen",
}
DISTRICT_WORDS = {"mirabad", "mirzaulugbek", "ulugbek", "yunusabad", "shayhantahur", "shayhantaur",
                  "chilanzar", "yakkasaray", "sergeli", "yangihayat", "almazar", "yashnabad",
                  "bektemir", "uchtepa", "mirzaulugbekskiy", "mirabadskiy", "yunusabadskiy",
                  "chilanzarskiy", "yakkasarayskiy", "sergeliyskiy", "almazarskiy", "yashnabadskiy",
                  "shayhantahurskiy", "uchtepinskiy", "bektemirskiy"}

LANDMARK_RE = re.compile(
    r"(?:ориентир[ыа]?|ор-р|мўлжал|мулжал|mo['ʻ`’]?ljal|orientir)\s*[:\-–—]?\s*([^\n.;!?]{3,90})", re.I)
NEAR_RE = re.compile(r"(?:рядом с|возле|напротив|около|недалеко от|yonida|yaqinida)\s+([^\n.,;!?]{3,50})", re.I)
METRO_AFTER_RE = re.compile(r"((?:[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳʻ'’\-]+\s+){1,3}?)(?:метро|metro)\b", re.I)
METRO_RE = re.compile(r"(?:метро|м\.|ст\.?\s*м\.?|metro|бекат)\s*[«\"“]?([A-Za-zА-Яа-яЁёЎўҚқҒғҲҳʻ'’\- ]{3,30})", re.I)


def skel(s: str) -> str:
    """Запись, в которой «Яшнабад», «Yashnobod» и «Яшнобод» совпадают:
    кириллица → латиница, о→а, x→h, q→k, без апострофов и двойных букв."""
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[ʻ'’`]", "", s)
    s = "".join(_CYR.get(ch, ch) for ch in s)
    s = s.replace("x", "h").replace("q", "k").replace("w", "v").replace("o", "a")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(\d+) ?(?:y|iy|ya|e|go|chi|nchi|ji)\b", r"\1", s)      # «14-й», «4-chi» → 14, 4
    s = re.sub(r"([a-z])\1+", r"\1", s)
    return " ".join(s.split())


def grams(phrase: str) -> set:
    """Слова и пары слов ориентира. Номер — только в паре («ц 1», «квартал 14», «школа 64»)."""
    out = set()
    for part in re.split(r"[,;/|]", phrase or ""):
        out |= _grams_part(skel(part).split())
    return out


def _grams_part(toks: list) -> set:
    out = set()
    for i, t in enumerate(toks):
        if len(t) >= 4 and not t.isdigit() and t not in STOP and t not in DISTRICT_WORDS:
            out.add(t)
        if i + 1 < len(toks):
            a, b = t, toks[i + 1]
            if a in DISTRICT_WORDS and b in STOP and not b.isdigit():
                out.add(f"{a} {b}")              # «Мирабадский рынок», «Чиланзарский парк» — это места
            elif (a not in DISTRICT_WORDS and b not in DISTRICT_WORDS
                    and not (a in STOP and b in STOP) and (len(a) >= 2 or a.isalpha())):
                if b.isdigit() or a.isdigit() or (a not in STOP and b not in STOP):
                    out.add(f"{a} {b}")
    return out


def landmarks(text: str) -> list:
    """Фразы-ориентиры из текста: «Ориентир: Узбум, Фаргона Йули» → ["Узбум, Фаргона Йули"]."""
    t = text or ""
    out = [m.group(1).strip(" ,:-–—«»\"") for m in LANDMARK_RE.finditer(t)]
    out += [m.group(1).strip(" ,:-–—«»\"") for m in NEAR_RE.finditer(t)]
    return [x for x in out if len(x) >= 3][:4]


def short_label(phrase: str) -> str:
    """Для карточки: первый ориентир из перечня, без хэштегов, эмодзи и хвостов."""
    p = re.sub(r"[^\w\s\-.'’ʻ№]", " ", phrase or "")
    p = re.split(r"[,(/)]| и | va |\s{2,}", " ".join(p.split()) if "," not in (phrase or "") else
                 re.sub(r"[^\w\s\-.,'’ʻ№]", " ", phrase))[0]
    p = " ".join(p.split()).strip(" -–—.")
    p = re.sub(r"^(?:рядом с|возле|напротив|около|бывш(?:ий|ее|ая)?)\s+", "", p, flags=re.I)
    p = re.sub(r"\s+(?:метро|metro)$", "", p, flags=re.I)
    if len(p) > 28:
        p = p[:28].rsplit(" ", 1)[0]
    return p[:1].upper() + p[1:]


def km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def in_city(lat, lon) -> bool:
    try:
        return BBOX[0] <= float(lat) <= BBOX[1] and BBOX[2] <= float(lon) <= BBOX[3]
    except (TypeError, ValueError):
        return False


def _center(pts):
    la = statistics.median(p[0] for p in pts)
    lo = statistics.median(p[1] for p in pts)
    spread = statistics.median(km(la, lo, p[0], p[1]) for p in pts)
    return la, lo, spread


# ------------------------------------------------------------- метро ----

def _metro():
    import analyst
    out = {}
    for name, (la, lo) in analyst.METRO.items():
        for part in re.split(r"[()]", name):
            s = skel(part)
            if len(s) >= 3:
                out[s] = (name.split(" (")[0], la, lo)
    return out


def metro_in_text(text: str):
    """«рядом с метро Ойбек» → ("Айбек", lat, lon)."""
    mt = _metro()
    found = [m.group(1) for m in METRO_RE.finditer(text or "")]
    for m in METRO_AFTER_RE.finditer(text or ""):
        w = m.group(1).split()
        found += [" ".join(w[-k:]) for k in (3, 2, 1) if len(w) >= k]
    for raw in found:
        s = skel(raw)
        for key in sorted(mt, key=len, reverse=True):
            if s.startswith(key) or (len(s) >= 5 and key.startswith(s[:6])):
                return mt[key]
    return None


# ------------------------------------------------------- справочник ----

GAZ = {"gaz": {}, "pts": [], "at": 0, "db": None}     # в памяти процесса: справочник и точки среза


def build(conn) -> dict:
    """Справочник ориентиров из среза: слово/пара → [lat, lon, n, разброс км].
    Берём только «точечные» названия: ≥3 объявления и половина из них в пределах 0,8 км."""
    rows = conn.execute(
        "SELECT lat, lng, landmark, addr, user_id, district FROM market WHERE lat IS NOT NULL AND removed_at IS NULL "
        "AND (landmark IS NOT NULL OR addr IS NOT NULL)").fetchall()
    by = {}
    for la, lo, lm, ad, uid, d in rows:
        if not in_city(la, lo):
            continue
        for g in grams(f"{lm or ''} , {ad or ''}"):
            # «11 квартал» есть и в Юнусабаде, и в Чиланзаре — у каждого района свой справочник
            for scope in ("*", d or "?"):
                by.setdefault(f"{scope}|{g}", {}).setdefault(uid or f"?{la}{lo}", []).append((la, lo))
    gaz = {}
    for g, users in by.items():
        if len(users) < 3:                       # одно агентство с 50 объявлениями — ещё не ориентир
            continue
        pts = [(statistics.median(p[0] for p in v), statistics.median(p[1] for p in v)) for v in users.values()]
        la, lo, spread = _center(pts)
        if spread <= 0.8:
            gaz[g] = [round(la, 6), round(lo, 6), len(pts), round(spread, 3)]
    return gaz


def load(store, rebuild=False):
    """Справочник и точки среза — в память (раз за проход; после среза — заново)."""
    conn = store.conn
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(market)")}
        if "lat" not in cols:
            return GAZ
        gaz = None if rebuild else store.get_kv("geo_gazetteer")
        if not gaz:
            gaz = build(conn)
            try:
                store.set_kv("geo_gazetteer", gaz)
            except Exception:                  # база только для чтения — справочник живёт в памяти
                pass
        GAZ["gaz"] = gaz
        GAZ["pts"] = [r for r in conn.execute(
            "SELECT lat, lng, district, op, new_building, dorm, area, price_usd, repair, key FROM market "
            "WHERE lat IS NOT NULL AND removed_at IS NULL AND area > 0").fetchall() if in_city(r[0], r[1])]
        GAZ["at"] = time.time()
        GAZ["db"] = _db_name(conn)
    except Exception as e:
        log.warning("[место] справочник не загрузился: %s", e)
    return GAZ


def _db_name(conn):
    try:
        return conn.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        return None


def ensure(store, max_age=1800):
    """Загрузить справочник, если он из другой базы или старше получаса."""
    if GAZ["db"] != _db_name(store.conn) or time.time() - GAZ["at"] > max_age:
        load(store)
    return GAZ


def district_at(lat, lon, radius=0.8):
    """Чей это район: большинство объявлений среза в радиусе."""
    near = [d for la, lo, d, *_ in GAZ["pts"] if d and abs(la - lat) < 0.02 and km(lat, lon, la, lo) <= radius]
    if len(near) < 3:
        return None
    best = max(set(near), key=near.count)
    return best if near.count(best) / len(near) >= 0.5 else None


def resolve_phrase(phrase: str, district=None):
    """Ориентир → (lat, lon, разброс км, n) по справочнику; сначала — справочник района,
    пара слов точнее одного слова."""
    for scope in ([district] if district else []) + ["*"]:
        best = None
        for g in grams(phrase):
            v = GAZ["gaz"].get(f"{scope}|{g}")
            if not v:
                continue
            cand = (v[3] - (0.15 if " " in g else 0), -v[2], v)
            if best is None or cand < best:
                best = cand
        if best:
            return tuple(best[2])
    return None


def locate(l: dict) -> dict:
    """Где объявление. Ставит l['geo'] = {lat, lon, src, label, district?}; возвращает его или {}.
    Порядок: координаты сайта (если не в чужом районе) → ориентир из текста → метро из текста."""
    text = f"{l.get('title') or ''}. {l.get('text') or ''}"
    phrases = landmarks(text)
    label = short_label(phrases[0]) if phrases else short_label(l.get("text_place") or "")
    d = l.get("district")

    def ok(la, lo):
        if not in_city(la, lo):
            return False
        dd = district_at(la, lo) if GAZ["pts"] else None
        return not (d and dd and dd != d)

    la, lo = l.get("lat"), l.get("lon")
    try:
        la, lo = float(la), float(lo)
    except (TypeError, ValueError):
        la = lo = None
    if la is not None and ok(la, lo):
        g = {"lat": la, "lon": lo, "src": "сайт", "label": label}
    else:
        g = None
        for ph in phrases:
            mt = metro_in_text(ph) if re.search(r"метро|metro|м\.|Ⓜ", ph, re.I) else None
            if mt and ok(mt[1], mt[2]):           # «метро X» — точнее справочника
                g = {"lat": mt[1], "lon": mt[2], "src": "метро", "label": f"м. {mt[0]}", "spread": 0.6}
                break
            r = resolve_phrase(ph, d)
            if r and ok(r[0], r[1]):
                g = {"lat": r[0], "lon": r[1], "src": "ориентир", "label": short_label(ph), "spread": r[3]}
                break
        if g is None:
            mt = metro_in_text(text)
            if mt and ok(mt[1], mt[2]):
                g = {"lat": mt[1], "lon": mt[2], "src": "метро", "label": f"м. {mt[0]}", "spread": 0.6}
    if g:
        if not d:                                  # район не указан — по соседям на карте
            dd = district_at(g["lat"], g["lon"])
            if dd:
                l["district"] = g["district"] = dd
        l["geo"] = g
        return g
    l.pop("geo", None)
    return {}


# --------------------------------------------- соседи и часть района ----

def neighbors(lat, lon, radius, op="sale"):
    return [r for r in GAZ["pts"] if r[3] == op and abs(r[0] - lat) < radius / 100
            and km(lat, lon, r[0], r[1]) <= radius]


def locality(l: dict, radius=1.0, min_local=8, min_district=15):
    """Дорогая ли это часть района: медиана $/м² продажи в радиусе против района
    (тот же сегмент: новостройки или вторичка; общежития отдельно)."""
    g = l.get("geo") or {}
    if not g or not l.get("district"):
        return None
    nb = 1 if l.get("_nb") else 0
    dorm = 1 if l.get("_dorm") else 0
    loc = [r[7] / r[6] for r in neighbors(g["lat"], g["lon"], radius) if r[4] == nb and r[5] == dorm
           and r[9] != l.get("key")]
    dist = [r[7] / r[6] for r in GAZ["pts"] if r[3] == "sale" and r[2] == l["district"] and r[4] == nb
            and r[5] == dorm]
    if len(loc) < min_local or len(dist) < min_district:
        return None
    lm, dm = statistics.median(loc), statistics.median(dist)
    return {"local_m2": lm, "n": len(loc), "district_m2": dm, "dn": len(dist), "diff": lm / dm - 1,
            "radius": radius}


def place_word(diff: float) -> str:
    if diff >= 0.10:
        return "дорогая часть района"
    if diff <= -0.10:
        return "недорогая часть района"
    return "средняя по цене часть района"


# --------------------------------------------- точка по запросу клиента ----

def point_for(query: str, store=None, requests_mod=None):
    """«метро Ойбек», «Ц-1», «Мегапланет» → {lat, lon, label, src}. Справочник → метро → OSM."""
    q = (query or "").strip()
    if not q:
        return None
    said_metro = bool(re.search(r"метро|metro|м\.", q, re.I))
    mt = metro_in_text(q if said_metro else f"метро {q}")
    if mt and said_metro:
        return {"lat": mt[1], "lon": mt[2], "label": f"м. {mt[0]}", "src": "метро"}
    sq = skel(q)                                   # «Чиланзар 11 квартал» — справочник этого района
    scope = next((d for d in {r[2] for r in GAZ["pts"] if r[2]} if skel(d)[:5] in sq), None)
    r = resolve_phrase(q, scope)
    if r and (grams(q) - {g for g in grams(q) if g in DISTRICT_WORDS}):
        return {"lat": r[0], "lon": r[1], "label": q[:40], "src": "ориентир"}
    if mt and sq.startswith(skel(mt[0])[:5]):
        return {"lat": mt[1], "lon": mt[2], "label": f"м. {mt[0]}", "src": "метро"}
    if store is not None and requests_mod is not None:
        cache = store.get_kv("geo_osm") or {}
        key = skel(q)
        if key in cache:
            return cache[key]
        import analyst
        g = analyst.geocode(q, requests_mod)
        res = None
        if g and in_city(g[0], g[1]):
            res = {"lat": g[0], "lon": g[1], "label": q[:40], "src": "карта"}
        cache[key] = res
        store.set_kv("geo_osm", cache)
        return res
    return None


def districts_within(lat, lon, radius):
    """Районы, которые задевает круг поиска (по объявлениям среза)."""
    ds = [r[2] for r in GAZ["pts"] if r[2] and abs(r[0] - lat) < radius / 80 and km(lat, lon, r[0], r[1]) <= radius]
    return sorted({d for d in ds if ds.count(d) >= 2})


def too_far(l: dict, near: dict):
    """Причина, если объявление дальше радиуса от точки клиента; '' — подходит.
    Место неизвестно — подходит, только если район задевает круг."""
    if not near or near.get("lat") is None:
        return ""
    g = l.get("geo") or locate(l)
    if g:
        dkm = km(near["lat"], near["lon"], g["lat"], g["lon"])
        l["near_km"] = round(dkm, 1)
        if dkm > near.get("km", 2) + (g.get("spread") or 0) * 0.5:
            return f"{dkm:.1f} км от «{near.get('label')}»"
        return ""
    if not l.get("district"):
        return "место не указано"
    if near.get("districts") and l["district"] not in near["districts"]:
        return f"район {l['district']} — далеко от «{near.get('label')}»"
    return ""


# ------------------------------------------------------------ карта цен ----

def price_grid(conn, cell_km=0.7, min_n=3) -> dict:
    """Сетка медиан $/м² продажи по городу — для страницы-карты."""
    dlat = cell_km / 111.0
    dlon = cell_km / (111.0 * math.cos(math.radians(41.3)))
    rows = conn.execute(
        "SELECT lat, lng, new_building, area, price_usd, district FROM market WHERE op='sale' "
        "AND removed_at IS NULL AND lat IS NOT NULL AND area > 0 AND dorm = 0").fetchall()
    cells = {}
    for la, lo, nb, ar, p, d in rows:
        if not in_city(la, lo):
            continue
        k = (int((la - BBOX[0]) / dlat), int((lo - BBOX[2]) / dlon))
        c = cells.setdefault(k, {"all": [], "new": [], "old": [], "d": []})
        m2 = p / ar
        c["all"].append(m2)
        c["new" if nb else "old"].append(m2)
        c["d"].append(d)
    out = []
    for (i, j), c in cells.items():
        if len(c["all"]) < min_n:
            continue
        lat0, lon0 = BBOX[0] + i * dlat, BBOX[2] + j * dlon
        md = lambda xs: round(statistics.median(xs)) if len(xs) >= min_n else None   # noqa: E731
        ds = [x for x in c["d"] if x]
        out.append({"b": [round(lat0, 5), round(lon0, 5), round(lat0 + dlat, 5), round(lon0 + dlon, 5)],
                    "all": md(c["all"]), "new": md(c["new"]), "old": md(c["old"]), "n": len(c["all"]),
                    "nn": len(c["new"]), "no": len(c["old"]),
                    "d": max(set(ds), key=ds.count) if ds else ""})
    dist = {}
    for la, lo, nb, ar, p, d in rows:
        if d:
            dist.setdefault(d, []).append(p / ar)
    return {"cells": out, "districts": {d: {"m2": round(statistics.median(v)), "n": len(v)}
                                        for d, v in dist.items() if len(v) >= 10},
            "city_m2": round(statistics.median([p / ar for *_, ar, p, d in rows])) if rows else None,
            "cell_km": cell_km}

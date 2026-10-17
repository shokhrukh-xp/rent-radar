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
    "avenue", "avenyu", "residence", "rezidens", "residens", "city", "siti", "tower", "taver", "plaza", "garden",
    "house", "haus", "premium", "luxury", "komfort", "comfort", "elite", "elit", "bisnes", "biznes", "klass",
    "naberejnaya", "plashad", "ploshad", "kalledj", "litsey", "instutut", "institut", "universitet",
    "yangi", "nahiyasi", "naviy", "navaya", "stariy", "staraya", "raspalajen",
}
DISTRICT_WORDS = {"mirabad", "mirzaulugbek", "ulugbek", "yunusabad", "yunsabad", "shayhantahur", "shayhantaur",
                  "chilanzar", "yakkasaray", "sergeli", "yangihayat", "almazar", "yashnabad",
                  "bektemir", "uchtepa", "mirzaulugbekskiy", "mirabadskiy", "yunusabadskiy",
                  "chilanzarskiy", "yakkasarayskiy", "sergeliyskiy", "almazarskiy", "yashnabadskiy",
                  "shayhantahurskiy", "uchtepinskiy", "bektemirskiy"}

LANDMARK_RE = re.compile(
    r"(?:ориентир[ыа]?|ор-р|мўлжал|мулжал|mo['ʻ`’]?ljal|orientir)\s*[:\-–—]?\s*([^\n.;!?]{3,90})", re.I)
NEAR_RE = re.compile(r"(?:рядом с|возле|напротив|около|недалеко от|yonida|yaqinida)\s+([^\n.,;!?]{3,50})", re.I)
# «Buyuk ipak yo'li metrosidan», «Ойбек метроси ёнида» — по-узбекски у слова «метро» бывают окончания
METRO_AFTER_RE = re.compile(r"((?:[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳʻ'’\-]+\s+){1,3}?)(?:метро|metro)\w*\b", re.I)
METRO_RE = re.compile(r"(?:метро|м\.|ст\.?\s*м\.?|metro|бекат)\s*[«\"“]?([A-Za-zА-Яа-яЁёЎўҚқҒғҲҳʻ'’\- ]{3,30})", re.I)


NUM_JUNK = {"arientir", "arientiri", "maljal", "muljal", "mulijal", "etaj", "etajni", "etaji", "kvm", "kv m",
            "ham", "hanali", "hana", "kamnat", "kamnatnaya", "kamnati", "sotih", "kvadrat", "metr", "ue", "usd",
            "mln", "ming", "yil", "gad", "uy", "dam", "dama", "padezd", "kvartira", "kvartiru", "kvartiri"}


def skel(s: str) -> str:
    """Запись, в которой «Яшнабад», «Yashnobod» и «Яшнобод» совпадают:
    кириллица → латиница, о→а, x→h, q→k, без апострофов и двойных букв."""
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[ʻ'’`‘]", "", s)
    s = "".join(_CYR.get(ch, ch) for ch in s)
    s = s.replace("x", "h").replace("q", "k").replace("w", "v").replace("o", "a")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", s)           # «Чиланзар6», «23кв» → «chilanzar 6», «23 kv»
    s = re.sub(r"\b(\d+) ?(?:y|iy|ya|e|go|chi|nchi|ji)\b", r"\1", s)      # «14-й», «4-chi» → 14, 4
    s = re.sub(r"\b(?:kv|kvartal\w*|mavze\w*|hudud\w*|massiv\w*)\b", "kvartal", s)   # 6 кв = 6 квартал = 6-mavze, «квартале»
    # «Чиланзарский», «в юнусабаде», «Учтепинский», «Yunusobodda» — район без окончаний
    s = re.sub(r"\b(chilanzar|yunusabad|yakkasaray|mirabad|sergeli|almazar|yashnabad|bektemir|shayhantahur|ulugbek)"
               r"(?:skiy|skay|skam|skii|ski|sk|e|a|da|dagi|ning|ga|i)\b", r"\1", s)
    s = re.sub(r"\buchtepin\w*\b", "uchtepa", s)
    s = re.sub(r"\b(?:bekat|bekati|st|stantsiya)\b", "bekat", s)
    s = re.sub(r"([a-z])\1+", r"\1", s)
    return " ".join(s.split())


AREA_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:кв\.?\s*м\.?|м\s*[²2]|kv\.?\s*m\.?|m\s*[²2]|кв\.\s*метр\w*|sotix|сотих|сотки)", re.I)


def grams(phrase: str) -> set:
    """Слова и пары слов ориентира. Номер — только в паре («ц 1», «квартал 14», «школа 64»).
    Площадь («40 кв.м») — не место: иначе «кв» станет «кварталом». Названия, которые пишут то слитно, то
    раздельно («Каракамыш» / «Қора қамиш», «Юнусабад» / «Юнус обод»), дают ещё и слитную форму."""
    out = set()
    for part in re.split(r"[,;/|]", AREA_RE.sub(" ", phrase or "")):
        toks = skel(part).split()
        out |= _grams_part(toks)
        for a, b in zip(toks, toks[1:]):
            if (a.isalpha() and b.isalpha() and len(a) >= 3 and len(b) >= 3 and len(a + b) >= 7
                    and a not in STOP and b not in STOP and a not in DISTRICT_WORDS and b not in DISTRICT_WORDS):
                out.add(a + b)
    return out


def _grams_part(toks: list) -> set:
    out = set()
    for i, t in enumerate(toks):
        if len(t) >= 4 and not t.isdigit() and t not in STOP and t not in DISTRICT_WORDS:
            out.add(t)
        if i + 1 < len(toks):
            a, b = t, toks[i + 1]
            if a in DISTRICT_WORDS and (b in STOP or b.isdigit()):
                out.add(f"{a} {b}")              # «Мирабадский рынок», «Чиланзар 9», «Сергели 5» — это места
            elif (a not in DISTRICT_WORDS and b not in DISTRICT_WORDS
                    and not (a in STOP and b in STOP) and (len(a) >= 2 or a.isalpha() or a.isdigit())):
                if b.isdigit() or a.isdigit():
                    w = a if b.isdigit() else b
                    if len(w) >= 2 and w not in NUM_JUNK:     # «3/4/4 Ориентир», «55 м2», «2 этаж» — не места
                        out.add(f"{a} {b}")
                elif a not in STOP and b not in STOP:
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
GAZ_VERSION = 2      # 16.10.2026: слитные формы, районы без окончаний, «квартале» → квартал


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
        gaz = None if rebuild else (_shared_kv(conn, "geo_gazetteer") or store.get_kv("geo_gazetteer"))
        if gaz and gaz.get("_v") != GAZ_VERSION:      # поменялось, как пишем названия (skel/grams) — собрать заново
            gaz = None
        if not gaz:
            gaz = build(conn)
            gaz["_v"] = GAZ_VERSION
            try:
                store.set_kv("geo_gazetteer", gaz)
            except Exception:                  # база только для чтения — справочник живёт в памяти
                pass
        GAZ["gaz"] = gaz
        GAZ["pts"] = [r for r in conn.execute(
            "SELECT lat, lng, district, op, new_building, dorm, area, price_usd, repair, key, user_id FROM market "
            "WHERE lat IS NOT NULL AND removed_at IS NULL AND area > 0").fetchall() if in_city(r[0], r[1])]
        GAZ["at"] = time.time()
        GAZ["db"] = _db_name(conn)
    except Exception as e:
        log.warning("[место] справочник не загрузился: %s", e)
    return GAZ


def _shared_kv(conn, key):
    """Значение из kv базы владельца (если она подключена как «shared») — справочник ориентиров общий."""
    import json
    try:
        if any(r[1] == "shared" for r in conn.execute("PRAGMA database_list")):
            row = conn.execute("SELECT value FROM shared.kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
    except Exception:
        return None
    return None


def _db_name(conn):
    """Откуда срез: у пользователя (tenants.py) — общая база владельца, подключённая как «shared»."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        return next((r[2] for r in rows if r[1] == "shared"), rows[0][2])
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
    if la is not None and in_city(la, lo):
        # координаты сайта точнее поля «район»: у Uybor район выведен по id и бывает чужим
        # (Куйлюк с пометкой «Мирабад»). Точка в другом районе — исправляем район, а не выбрасываем точку.
        dd = district_at(la, lo) if GAZ["pts"] else None
        if d and dd and dd != d:
            l.setdefault("district_site", d)
            l["district_by"] = "точка"
            l["district"] = d = dd
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
        if g is None:                               # «Ориентир:» не написан — знакомые названия в тексте (ЖК, массив)
            rt = resolve_text(text, d)
            if rt and ok(rt[0], rt[1]):
                g = {"lat": rt[0], "lon": rt[1], "src": "ориентир", "label": "", "spread": max(0.5, rt[2])}
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
    strict = near.get("strict", False)
    g = l.get("geo") or locate(l)
    if g and g.get("src") != "район":
        dkm = km(near["lat"], near["lon"], g["lat"], g["lon"])
        l["near_km"] = round(dkm, 1)
        spread = g.get("spread") or 0
        if strict and spread > 1.0:              # «где-то у рынка» с разбросом в километр-два — центр не проверить
            return f"место примерное (±{spread:.1f} км), а нужно строго в радиусе {near.get('km', 2):g} км"
        if dkm > near.get("km", 2) + (0 if strict else spread * 0.5):
            return f"{dkm:.1f} км от «{near.get('label')}»"
        return ""
    if strict:                                   # строго в центре / у ориентира: без точного места не показываем
        return "точный адрес не указан — не проверить, что это в нужном месте"
    if not l.get("district"):
        return "место не указано"
    if near.get("districts") and l["district"] not in near["districts"]:
        return f"район {l['district']} — далеко от «{near.get('label')}»"
    share = district_share(l["district"], near)
    if share is not None and share < DISTRICT_SHARE_MIN:
        # район лишь задевает круг (Мирзо-Улугбек тянется на 10+ км от центра): без точки — не берём
        return f"место не указано, а {l['district']} в основном дальше {near.get('km', 2):g} км от «{near.get('label')}»"
    return ""


DISTRICT_SHARE_MIN = 0.5
_SHARE = {}


def district_share(district: str, near: dict):
    """Какая доля объявлений района (по срезу) попадает в круг клиента; None — мало данных."""
    k = (district, round(near["lat"], 3), round(near["lon"], 3), near.get("km", 2), len(GAZ["pts"]))
    if k in _SHARE:
        return _SHARE[k]
    pts = [(r[0], r[1]) for r in GAZ["pts"] if r[2] == district and r[3] == "sale"]
    out = None
    if len(pts) >= 20:
        r_ = near.get("km", 2)
        out = sum(1 for la, lo in pts if km(near["lat"], near["lon"], la, lo) <= r_) / len(pts)
    _SHARE[k] = out
    return out


GENERIC_PLACE = {"mechet", "masjid", "shkala", "shkali", "detsad", "sad", "bazar", "rinak", "rinka", "magazin",
                 "bolnitsa", "palikliniki", "paliklinika", "park", "astanavka", "metra", "makro", "karzinka",
                 "supermarket", "apteka", "bank", "kafe", "restaran", "mektab", "bagcha", "dakan", "hakimiyat",
                 "mebelniy", "mebel", "tsentr", "markaz"}


def generic_place(name: str) -> bool:
    """«Мечеть», «Школа», «Мебельный магазин» — ориентир без названия: таких по городу сотни."""
    toks = skel(name).split()
    if any(x.isdigit() for x in toks):             # «школа 64», «ТТЗ-2» — конкретное место
        return False
    return not [x for x in toks if x not in STOP and x not in GENERIC_PLACE]


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


# ------------------------------------------------- карта места для карточки ----

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_UA = "rano-bot/1.0 (+https://t.me/rano_smart_bot; apartment search assistant)"


def _tile_xy(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def _tile(z, x, y):
    """Тайл OSM с кэшем на диске (правила OSM: свой User-Agent, не качать одно и то же)."""
    import os
    import tempfile
    import requests
    path = os.path.join(tempfile.gettempdir(), "rano_tiles", str(z), str(x), f"{y}.png")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 30 * 86400:
        with open(path, "rb") as f:
            return f.read()
    r = requests.get(TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": TILE_UA}, timeout=15)
    r.raise_for_status()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(r.content)
    return r.content


def static_map(lat, lon, approx_km=None, w=800, h=560):
    """Картинка-карта: где квартира. Точный адрес — метка; место по ориентиру — круг «где-то здесь».
    Возвращает JPEG или None (нет Pillow / сети)."""
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    z = 13 if (approx_km or 0) > 1.2 else 15         # 15 — улицы и места вокруг; 13 — весь район
    cx, cy = _tile_xy(lat, lon, z)
    px, py = cx * 256, cy * 256                      # центр в пикселях мира
    x0, y0 = px - w / 2, py - h / 2
    img = Image.new("RGB", (w, h), (235, 235, 235))
    try:
        for tx in range(int(x0 // 256), int((x0 + w) // 256) + 1):
            for ty in range(int(y0 // 256), int((y0 + h) // 256) + 1):
                t = Image.open(BytesIO(_tile(z, tx, ty))).convert("RGB")
                img.paste(t, (int(tx * 256 - x0), int(ty * 256 - y0)))
    except Exception as e:
        log.info("[карта] тайлы не скачались: %s", e)
        return None
    d = ImageDraw.Draw(img, "RGBA")
    mx, my = w / 2, h / 2
    if approx_km:
        m_per_px = 156543.03 * math.cos(math.radians(lat)) / 2 ** z
        r = max(40, min(240, approx_km * 1000 / m_per_px))
        d.ellipse((mx - r, my - r, mx + r, my + r), fill=(47, 111, 222, 60), outline=(47, 111, 222, 230), width=4)
        d.ellipse((mx - 7, my - 7, mx + 7, my + 7), fill=(47, 111, 222, 255), outline=(255, 255, 255, 255), width=3)
    else:                                            # метка-«капля»
        d.ellipse((mx - 17, my - 52, mx + 17, my - 18), fill=(220, 53, 69, 255), outline=(255, 255, 255, 255), width=4)
        d.polygon([(mx - 12, my - 26), (mx + 12, my - 26), (mx, my)], fill=(220, 53, 69, 255))
        d.ellipse((mx - 6, my - 41, mx + 6, my - 29), fill=(255, 255, 255, 255))
    d.rectangle((w - 150, h - 18, w, h), fill=(255, 255, 255, 200))
    d.text((w - 146, h - 15), "(c) OpenStreetMap", fill=(60, 60, 60, 255))
    out = BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


_REV_LAST = [0.0]


def reverse(lat, lon) -> str:
    """Улица и махалля по координатам (OSM Nominatim): «ул. Мукими», «ул. Укувчилар, махалля А.Фитрат».
    Кэш на диске, не чаще запроса в секунду (правила Nominatim). '' — не знаем."""
    import json
    import os
    import tempfile
    import requests
    key = f"{float(lat):.4f},{float(lon):.4f}"
    path = os.path.join(tempfile.gettempdir(), "rano_rev.json")
    try:
        cache = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    wait = 1.1 - (time.time() - _REV_LAST[0])
    if wait > 0:
        time.sleep(wait)
    _REV_LAST[0] = time.time()
    try:
        r = requests.get("https://nominatim.openstreetmap.org/reverse",
                         params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 17, "addressdetails": 1,
                                 "accept-language": "ru"},
                         headers={"User-Agent": TILE_UA}, timeout=15)
        a = (r.json() or {}).get("address") or {} if r.status_code == 200 else {}
    except Exception as e:
        log.info("[место] адрес по точке: %s", e)
        return ""
    road = a.get("road") or ""
    road = re.sub(r"^(.*?)\s+(улица|ulitsasi|ko['ʻ’]?chasi)$", r"ул. \1", road, flags=re.I)
    road = re.sub(r"^(.*?)\s+(проспект|prospekti)$", r"пр. \1", road, flags=re.I)
    hood = a.get("neighbourhood") or a.get("quarter") or a.get("suburb") or ""
    hood = re.sub(r"^(.*?)\s+махалля$", r"махалля \1", hood)
    out = ", ".join(x for x in (road, hood) if x)[:60]
    cache[key] = out
    try:
        json.dump(cache, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass
    return out


# ---------------------------------------------------- «центр» любого города ----
# Системно, для любого города: середина — точка города в OpenStreetMap (у Ташкента — сквер Амира Темура),
# радиус центра — по размеру города (Ташкент ~3,5 км — это примерно Малая кольцевая, Самарканд ~1,8 км).
# «У центра» — ещё полтора-два километра за этой границей. Можно поправить в config: cities.<город>.center_km.

CITY_FALLBACK = {"ташкент": {"name": "Ташкент", "lat": 41.3123, "lon": 69.2787, "center_km": 3.5}}
CENTER_WORDS = ("tsentr", "markaz", "center", "centre", "sentr")


def is_center_query(text: str) -> bool:
    s = skel(text)
    return any(w in s for w in CENTER_WORDS)


CITY_PROFILES = {}       # город → профиль центра, найденный в интернете и проверенный по карте (кладёт rent_radar)


def city_center(city: str = "Ташкент", overrides: dict = None) -> dict:
    """{name, lat, lon, center_km, near_km} — середина города и радиус «центра».
    Есть исследованный профиль (интернет + карта) — он; иначе — по размеру города в OpenStreetMap."""
    prof = CITY_PROFILES.get((city or "Ташкент").strip().lower())
    if prof:
        return prof
    import json
    import os
    import tempfile
    import requests
    key = (city or "Ташкент").strip().lower()
    over = (overrides or {}).get(key) or {}
    path = os.path.join(tempfile.gettempdir(), "rano_cities.json")
    try:
        cache = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    c = cache.get(key)
    if not c:
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search",
                             params={"q": f"{city}, Узбекистан", "format": "jsonv2", "limit": 1,
                                     "featureType": "city", "accept-language": "ru"},
                             headers={"User-Agent": TILE_UA}, timeout=15)
            d = (r.json() or [None])[0] if r.status_code == 200 else None
        except Exception as e:
            log.info("[центр] %s: %s", city, e)
            d = None
        if d:
            s_, n_, w_, e_ = (float(x) for x in d["boundingbox"])
            lat = float(d["lat"])
            size = math.sqrt((n_ - s_) * 111.0 * (e_ - w_) * 111.0 * math.cos(math.radians(lat)))
            c = {"name": city, "lat": lat, "lon": float(d["lon"]),
                 "center_km": round(min(4.0, max(1.0, 0.11 * size)), 1)}
            cache[key] = c
            try:
                json.dump(cache, open(path, "w", encoding="utf-8"), ensure_ascii=False)
            except OSError:
                pass
        else:
            c = CITY_FALLBACK.get(key) or CITY_FALLBACK["ташкент"]
    c = {**c, **{k: v for k, v in over.items() if k in ("lat", "lon", "center_km")}}
    c["near_km"] = round(c["center_km"] + max(1.0, 0.4 * c["center_km"]), 1)
    return c


def center_relation(lat, lon, city: dict) -> tuple:
    """('центр' | 'у центра' | 'не центр', км до середины)."""
    d = km(lat, lon, city["lat"], city["lon"])
    if d <= city["center_km"]:
        return "центр", d
    if d <= city["near_km"]:
        return "у центра", d
    return "не центр", d


def geocode_in(name: str, city: dict):
    """Место по названию внутри города (Nominatim, рамка вокруг города; кэш на диске)."""
    import json
    import os
    import tempfile
    import requests
    key = f'{city.get("name", "")}|{skel(name)}'
    path = os.path.join(tempfile.gettempdir(), "rano_geocode.json")
    try:
        cache = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    wait = 1.1 - (time.time() - _REV_LAST[0])
    if wait > 0:
        time.sleep(wait)
    _REV_LAST[0] = time.time()
    d = 0.25
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": f'{name}, {city.get("name", "")}', "format": "jsonv2", "limit": 1,
                                 "accept-language": "ru", "bounded": 1,
                                 "viewbox": f'{city["lon"] - d},{city["lat"] + d},{city["lon"] + d},{city["lat"] - d}'},
                         headers={"User-Agent": TILE_UA}, timeout=15)
        js = r.json() if r.status_code == 200 else []
        res = [float(js[0]["lat"]), float(js[0]["lon"])] if js else None
    except Exception as e:
        log.info("[центр] %s: %s", name, e)
        return None
    cache[key] = res
    try:
        json.dump(cache, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass
    return res


def center_from_research(city: dict, facts: dict, resolve=None) -> dict:
    """Что модель нашла в интернете (центральная точка, места внутри, рядом и вне центра) → середина и
    радиус центра, проверенные по карте. resolve(name) → (lat, lon) | None; по умолчанию — Nominatim."""
    resolve = resolve or (lambda n: geocode_in(n, city))
    out = dict(city)
    if facts.get("anchor"):
        a = resolve(facts["anchor"])
        if a and km(a[0], a[1], city["lat"], city["lon"]) < 6:
            out["lat"], out["lon"] = a
    pts = lambda names: [p for p in (resolve(n) for n in names) if p]      # noqa: E731
    d_in = sorted(km(out["lat"], out["lon"], *p) for p in pts(facts.get("in") or []))
    d_out = sorted(km(out["lat"], out["lon"], *p) for p in pts(facts.get("out") or []))
    d_in = [x for x in d_in if x < 10]                    # явные промахи геокодера — мимо
    r = facts.get("radius_km") or city["center_km"]
    if len(d_in) >= 4:                                    # «внутри центра» — на таком расстоянии от середины
        r_in = d_in[min(len(d_in) - 1, int(len(d_in) * 0.85))]
        r = (r + r_in) / 2
    if d_out:                                             # «точно не центр» — граница ближе
        far = [x for x in d_out if x > 0.5 * r]
        if far and far[0] < r:
            r = far[0] - 0.2
    out["center_km"] = round(min(6.0, max(0.8, r)), 1)
    out["near_km"] = round(out["center_km"] + max(1.0, 0.4 * out["center_km"]), 1)
    out.update(boundary=facts.get("boundary") or "", in_names=(facts.get("in") or [])[:12],
               sources=facts.get("sources") or [], researched=True,
               checked=f"{len(d_in)} внутри, {len(d_out)} вне — по карте")
    return out


def resolve_text(text: str, district=None):
    """Место по всему тексту объявления, когда «Ориентир:» не написан: ищем в тексте названия из
    справочника (ЖК, массивы, улицы, рынки). Берём самое «точечное» (малый разброс) и только пары слов
    или длинные одиночные названия — чтобы случайное слово не дало точку."""
    best = None
    gs = grams(text or "")
    if district:                                 # «16 квартал» в Юнусабаде = «Юнусабад-16» в справочнике
        dw = skel(district).replace(" ", "")
        for g in list(gs):
            m = re.fullmatch(r"(\d{1,2}) kvartal|kvartal (\d{1,2})", g)
            if m:
                gs.add(f"{dw} {m.group(1) or m.group(2)}")
    for g in gs:
        if " " not in g and len(g) < 6:
            continue
        for scope in ([district] if district else []) + ["*"]:
            v = GAZ["gaz"].get(f"{scope}|{g}")
            if v and v[3] <= 0.5 and v[2] >= 3:
                cand = (v[3] - (0.1 if " " in g else 0) - (0.05 if scope != "*" else 0), v, g)
                if best is None or cand[0] < best[0]:
                    best = cand
                break
    if not best:
        return None
    v, g = best[1], best[2]
    return v[0], v[1], v[3], g


# ================================================ ЛОКАЦИЯ ОТ МАКЛЕРА ======
# Маклер присылает точку Telegram, ссылку Яндекс/Google/2ГИС/Apple/OSM (в т.ч. короткую) или координаты текстом.

_NUM = r"(-?\d{1,3}\.\d{2,})"           # в ссылках карт бывает и 2 знака («ll=41.30,69.24»)
_NUM_TXT = r"(-?\d{2}\.\d{4,})"         # в тексте — только точные (≥4 знака), чтобы не путать с ценой/этажом
MAP_URL_RE = re.compile(r"https?://[^\s<>\"']*(?:yandex\.[a-z.]+/(?:maps|web-maps)|maps\.yandex|ya\.cc|"
                        r"google\.[a-z.]+/maps|maps\.google|goo\.gl/maps|maps\.app\.goo\.gl|g\.co/kgs|"
                        r"2gis\.[a-z.]+|go\.2gis\.com|maps\.apple\.com|openstreetmap\.org|osm\.org|"
                        r"waze\.com)[^\s<>\"']*", re.I)
SHORT_MAP_RE = re.compile(r"maps\.app\.goo\.gl|goo\.gl/maps|g\.co/kgs|go\.2gis\.com|ya\.cc|"
                          r"yandex\.[a-z.]+/maps/-/|2gis\.[a-z.]+/[a-z]+/firm/|/maps/org/", re.I)
_URL_CACHE = {}


def _pair(a, b, lonlat=False):
    try:
        la, lo = (float(b), float(a)) if lonlat else (float(a), float(b))
    except (TypeError, ValueError):
        return None
    return (la, lo) if in_city(la, lo) else None


def coords_in_url(url: str):
    """Координаты из адреса карты. Порядок: Яндекс и 2ГИС пишут «долгота,широта», остальные — «широта,долгота»."""
    from urllib.parse import unquote
    u = unquote(url or "")
    for pat, lonlat in ((rf"[?&](?:pt|ll|whatshere%5Bpoint%5D|whatshere\[point\])={_NUM},{_NUM}", "yandex" in u.lower()),
                        (rf"!3d{_NUM}!4d{_NUM}", False),                      # google: …!3d41.31!4d69.27
                        (rf"@{_NUM},{_NUM}", False),                          # google: /@41.31,69.27,17z
                        (rf"[?&](?:q|query|ll|sll|daddr|destination)={_NUM},\s*{_NUM}", False),
                        (rf"[?&]m={_NUM},{_NUM}", True),                      # 2gis: ?m=69.27,41.31/16
                        (rf"/geo/{_NUM},{_NUM}", True),                       # 2gis: /geo/69.27,41.31
                        (rf"mlat={_NUM}&mlon={_NUM}", False),                 # osm
                        (rf"#map=\d+/{_NUM}/{_NUM}", False)):
        m = re.search(pat, u, re.I)
        if m:
            p = _pair(m.group(1), m.group(2), lonlat)
            if p:
                return p
    return None


def resolve_map_link(url: str, timeout=8):
    """Короткая ссылка (maps.app.goo.gl, go.2gis.com, yandex …/maps/-/…) → полный адрес карты. С кэшем."""
    if url in _URL_CACHE:
        return _URL_CACHE[url]
    import requests
    out = url
    try:
        r = requests.get(url, headers={"User-Agent": TILE_UA}, timeout=timeout, allow_redirects=True)
        out = r.url or url
        if not coords_in_url(out):                     # Яндекс/Google иногда отдают точку только в странице
            m = re.search(r"(?:ll|center)[\"'=:\[\s]+" + _NUM + r"[,\s\]]+" + _NUM, r.text[:200000])
            if m:
                out += f"&ll={m.group(1)},{m.group(2)}" if "yandex" in out else f"?q={m.group(1)},{m.group(2)}"
    except Exception as e:
        log.info("[место] ссылка на карту %s не открылась: %s", url[:60], e)
    _URL_CACHE[url] = out
    return out


def coords_from_text(text: str, resolve=True):
    """(lat, lon, откуда) из сообщения маклера или None: точка Telegram («📍 41.31,69.27»), ссылка на карту,
    координаты числами."""
    t = text or ""
    m = re.search(rf"📍[^\d-]{{0,20}}{_NUM}\s*,\s*{_NUM}", t)
    if m and _pair(m.group(1), m.group(2)):
        return (*_pair(m.group(1), m.group(2)), "точка")
    for url in MAP_URL_RE.findall(t):
        p = coords_in_url(url)
        if not p and resolve and SHORT_MAP_RE.search(url):
            p = coords_in_url(resolve_map_link(url))
        if p:
            return (*p, "ссылка на карту")
    m = re.search(rf"(?<![\d.]){_NUM_TXT}\s*[,;\s]\s*{_NUM_TXT}(?![\d.])", t)
    if m:
        p = _pair(m.group(1), m.group(2)) or _pair(m.group(1), m.group(2), lonlat=True)
        if p:
            return (*p, "координаты")
    return None

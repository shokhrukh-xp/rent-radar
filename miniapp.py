"""Мини-приложение Ra'no в Telegram (вкладки Избранное · Карта · Просмотры · Поиск).

Данные: Python кладёт в снимок для воркера раздел «app» (app_snapshot) — варианты из шортлиста с фото, точкой,
этапом, ценой против рынка, ответом продавца, просмотром, заметками и чек-листом; параметры поиска; точку работы.
Воркер отдаёт снимок странице /app после проверки подписи Telegram, а действия со страницы кладёт в очередь
сообщением с полем _app — их разбирает handle_action (уточнить, нравится, убрать, чек-лист, работа, только хозяева).
Время в пути до работы считает сама страница — по прямой с поправкой на дороги (это оценка, так и подписано).
Телефоны продавцов в снимок не попадают: страницу можно показать близким.
"""
import json
import logging
import re
import statistics
from datetime import datetime, timedelta, timezone

log = logging.getLogger("rent-radar")
TZ = timezone(timedelta(hours=5))
PHONE_RE = re.compile(r"(?:\+?998[\s\-()]*)?\(?\d{2}\)?[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
MAX_OFFERS = 40


def _rr():
    import rent_radar
    return rent_radar


def _cg():
    import concierge
    return concierge


def no_phones(text: str) -> str:
    return PHONE_RE.sub("📞 …", text or "")


def stage_kind(o) -> str:
    ex = o.get("extra") or {}
    v = ex.get("viewing") or {}
    if ex.get("seen") == "g":
        return "liked"
    if ex.get("seen") == "m":
        return "think"
    if v.get("at"):
        return "view"
    if o.get("replied_at"):
        return "answer"
    if o.get("status") == "asked" or o.get("asked_at"):
        return "wait"
    return "saved"


def last_answer(o) -> str:
    """Что ответил продавец/маклер: последняя строка «💬 …» в заметке варианта."""
    parts = [p.strip() for p in (o.get("note") or "").split("💬") if p.strip()]
    if not parts or not o.get("replied_at"):
        return ""
    return no_phones(parts[-1].split("\n👎")[0].strip())[:400]


def _point(store, o):
    ex = o.get("extra") or {}
    if ex.get("lat") is not None and ex.get("lon") is not None:
        return float(ex["lat"]), float(ex["lon"])
    if _cg().is_site_offer(o):
        try:
            l = _rr()._listing_any(store, o["broker_chat"][5:]) or {}
        except Exception:
            l = {}
        g = l.get("geo") or {}
        if g.get("lat") is not None and g.get("src") != "район":
            return float(g["lat"]), float(g["lon"])
        if l.get("lat") is not None and l.get("lon") is not None:
            return float(l["lat"]), float(l["lon"])
    return None


def nearest_metro(lat, lon):
    import analyst
    import geo
    best = None
    for name, (la, lo) in analyst.METRO.items():
        d = geo.km(lat, lon, la, lo)
        if best is None or d < best[1]:
            best = (name.split(" (")[0], d)
    if not best or best[1] > 3:
        return None
    return {"name": best[0], "min": max(1, round(best[1] * 1.3 / 4.5 * 60))}   # пешком ~4,5 км/ч, улицы не по прямой


def market_ranges(store, deal="rent", days=45):
    """(район, комнаты) → медиана и разброс (25–75%) по своей базе объявлений; аренда — radar.db, покупка — sale.db."""
    rr = _rr()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    st, own = store, False
    if deal in ("buy", "sale"):
        try:
            st, own = rr.open_sale(), True
        except Exception:
            return {}
    try:
        rows = st.conn.execute("SELECT district, rooms, price_usd FROM listings WHERE first_seen > ? AND "
                               "price_usd > 0 AND dup_of IS NULL", (since,)).fetchall()
    except Exception:
        rows = []
    finally:
        if own:
            st.conn.close()
    by = {}
    for d, r, p in rows:
        if d and r:
            by.setdefault((d, int(r)), []).append(p)
    out = {}
    for k, v in by.items():
        if len(v) >= 6:
            v = sorted(v)
            q = statistics.quantiles(v, n=4)
            out[k] = {"med": round(statistics.median(v)), "lo": round(q[0]), "hi": round(q[2]), "n": len(v)}
    return out


def app_offer(cfg, store, o, ranges):
    cg = _cg()
    ex = o.get("extra") or {}
    site = cg.is_site_offer(o)
    photos = []
    for p in (o.get("photos") or [])[:6]:
        if isinstance(p, str) and p.startswith("http"):
            photos.append({"u": p})
        elif isinstance(p, str) and p:
            photos.append({"f": p})
    pt = _point(store, o)
    deal = "sale" if ex.get("deal") == "sale" else "rent"
    mk = ranges.get((o.get("district"), int(o["rooms"]))) if o.get("rooms") and o.get("district") else None
    v = ex.get("viewing") or {}
    out = {
        "id": o["oid"], "site": o.get("broker_name") or ("сайт" if site else "маклер"), "from_site": site,
        "kind": stage_kind(o), "deal": deal, "price": o.get("price_usd"), "rooms": o.get("rooms"),
        "area": o.get("area"), "floor": o.get("floor"), "floors": o.get("floors_total"),
        "district": o.get("district"), "addr": ", ".join(x for x in (ex.get("address"), ex.get("landmark")) if x)[:120],
        "photos": photos, "url": ex.get("url") if site else None,
        "answer": last_answer(o), "answer_at": o.get("replied_at"), "asked_at": o.get("asked_at"),
        "repair": ex.get("repair"), "building": ex.get("building"), "furniture": ex.get("furniture"),
        "commission": ex.get("commission"), "mortgage": ex.get("mortgage"), "documents": ex.get("documents"),
        "text": no_phones(cg.dedupe_text((o.get("text") or "").strip()))[:500],
        "notes": [n.get("text", "") for n in (ex.get("notes") or [])][-5:],
        "check": ex.get("check") or {}, "check_note": ex.get("check_note") or "",
        "seen": ex.get("seen"), "added": o.get("created_at"),
    }
    if pt:
        out["lat"], out["lon"] = round(pt[0], 5), round(pt[1], 5)
        m = nearest_metro(*pt)
        if m:
            out["metro"] = m
    if mk and o.get("price_usd"):
        out["market"] = dict(mk, delta=round((o["price_usd"] - mk["med"]) / mk["med"] * 100))
    if v.get("at"):
        out["view"] = {"at": v["at"], "label": v.get("label") or "", "confirmed": bool(v.get("confirmed")),
                       "notime": bool(v.get("notime"))}
    return {k: val for k, val in out.items() if val not in (None, "", [], {})} | {"id": o["oid"], "kind": out["kind"]}


def app_snapshot(cfg, store, settings) -> dict:
    cg = _cg()
    items = [o for o in cg.offers_by_status(store, "shortlist") + cg.offers_by_status(store, "asked") if o]
    items.sort(key=lambda o: o["oid"], reverse=True)
    ans = (cg.get_anketa(store).get("ans") or {})
    deal = "sale" if ans.get("deal") in ("buy", "sale") else "rent"
    try:
        ranges = market_ranges(store, deal)
    except Exception as e:
        log.info("[app] рынок: %s", e)
        ranges = {}
    offers = []
    for o in items[:MAX_OFFERS]:
        try:
            offers.append(app_offer(cfg, store, o, ranges))
        except Exception as e:
            log.info("[app] вариант #%s: %s", o.get("oid"), e)
    rooms = [int(r) for r in (ans.get("rooms") or []) if str(r).isdigit()] or [2]
    bars = []
    for (d, r), m in sorted(ranges.items(), key=lambda kv: -kv[1]["n"]):
        if r == rooms[0] and len(bars) < 8:
            bars.append({"d": d, "med": m["med"], "n": m["n"]})
    return {"v": 1, "offers": offers, "deal": deal, "rooms": rooms[0], "market": bars,
            "owner_only": bool(settings.get("owner_only", True)), "work": settings.get("work") or None,
            "areas": len(settings.get("areas") or []), "at": datetime.now(TZ).isoformat(timespec="minutes")}


# ----------------------------------------------------------------- действия --

CHECK_KEYS = ("water", "windows", "damp", "noise", "net", "entry", "furn", "owner", "meters")
CHECK_RU = {"water": "вода", "windows": "окна и отопление", "damp": "сырость", "noise": "шум", "net": "связь",
            "entry": "подъезд и двор", "furn": "мебель и техника", "owner": "документы собственника",
            "meters": "счётчики и долги"}


def handle_action(cfg, store, settings, a) -> bool:
    """Действие из мини-приложения (воркер уже проверил подпись Telegram и что это клиент)."""
    rr, cg = _rr(), _cg()
    act = str(a.get("a") or "")
    try:
        oid = int(a.get("oid") or 0)
    except (TypeError, ValueError):
        oid = 0
    o = cg.get_offer(store, oid) if oid else None
    if act in ("ask", "like", "think", "skip") and not o:
        return False
    if act == "ask":
        cg.request_details(cfg, store, [oid])
    elif act in ("like", "think", "skip"):
        cg.handle_offer_cb(f"o:seen:{oid}:{ {'like': 'g', 'think': 'm', 'skip': 'n'}[act] }", cfg, store)
    elif act == "check" and o:
        raw = a.get("check") or {}
        chk = {k: int(raw[k]) for k in CHECK_KEYS if str(raw.get(k, "")).isdigit() and int(raw[k]) in (0, 1, 2)}
        note = str(a.get("text") or "").strip()[:300]
        cg._set_extra(store, oid, check=chk, check_note=note,
                      check_at=datetime.now(TZ).isoformat(timespec="minutes"))
        bad = [CHECK_RU[k] for k, v in chk.items() if v == 2]
        if bad or note:
            cg.add_note(store, oid, "Просмотр: " + ("проблемы — " + ", ".join(bad) if bad else "без проблем")
                        + (f". {note}" if note else ""))
    elif act == "work":
        try:
            lat, lon = float(a.get("lat")), float(a.get("lon"))
        except (TypeError, ValueError):
            return False
        if not (40.9 < lat < 41.7 and 68.9 < lon < 69.8):
            return False
        settings["work"] = {"lat": round(lat, 5), "lon": round(lon, 5), "label": str(a.get("label") or "")[:60]}
        store.set_kv("settings", settings)
    elif act == "owner_only":
        settings["owner_only"] = bool(a.get("on"))
        store.set_kv("settings", settings)
    else:
        return False
    log.info("[app] %s %s", act, oid or "")
    return True

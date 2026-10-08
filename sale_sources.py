"""Поиск квартиры для покупки по всем открытым источникам, кроме Uybor (он — в rent_radar).

Realt24 и Joymee — открытые API; Realting — HTML-каталог; Yangiuylar — API новостроек
(цена застройщика за м² × площадь планировки); Telegram — публичные превью каналов t.me/s.
OLX, Birbir и Uysot закрыты защитой от ботов — их не обходим.

Каждый fetch_* возвращает объявления в общем формате (как uybor_listing) с ключом "sale:…".
"""
import html as html_lib
import json
import re
import time
from datetime import datetime, timezone

import requests


def _rr():
    import rent_radar
    return rent_radar


REALT24_URL = "https://realt24.uz/listing/{id}/"
JOYMEE_URL = "https://joymee.uz/announcements/{id}"
JOYMEE_DISTRICTS = {"Чиланзар": 1, "Сергели": 2, "Яккасарай": 3, "Бектемир": 4, "Янгихаёт": 149,
                    "Мирзо-Улугбек": 152, "Яшнабад": 153, "Учтепа": 198, "Юнусабад": 199,
                    "Мирабад": 200, "Шайхантахур": 201, "Алмазар": 202}
JOYMEE_REPAIR = {1: "без ремонта", 2: "средний ремонт", 3: "хороший ремонт", 4: "евроремонт",
                 5: "дизайнерский ремонт"}
REALTING_LIST = "https://realting.uz/apartments"
REALTING_ROOMS = {1: "1-bedroom", 2: "2-bedrooms", 3: "3-bedrooms", 4: "4-bedrooms"}
YU_API = "https://yangiuylar.uz/api"
YU_TASHKENT = 12                       # region_id «г. Ташкент» в справочнике Yangiuylar
SALE_CHANNELS = ["Kvartiritashkenta", "kvartiry_tashkent", "tashkent_nedvizhimost",
                 "toshkent_kvartira", "uybor", "uybozor"]

# имя источника для карточки и ссылки «Открыть на …»
SITE = {"realt24": "Realt24", "joymee": "Joymee", "realting": "Realting",
        "yangiuylar": "Yangiuylar", "telegram": "Telegram"}


def _listing(**kw):
    base = {"photo_urls": [], "key": "", "source": "", "site": "", "url": "", "title": "", "text": "",
            "price_value": None, "price_currency": "USD", "rooms": None, "district": None,
            "district_raw": None, "phones": [], "created_at": None, "seller": "", "seller_id": "",
            "is_business": None, "area": None, "floor": None, "floors_total": None,
            "new_building": False, "repair": None, "seller_hint": "", "mortgage": None}
    base.update(kw)
    return base


def _num(s):
    try:
        return float(str(s).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


ROOMS_RE = re.compile(r"(\d+)\s*-?\s*(?:комн|xona)", re.I)
AREA_RE = re.compile(r"(\d{2,3}(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м|m²|m2)", re.I)
FLOOR_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})")


# ------------------------------------------------------------------ Realt24 --

def fetch_realt24(ss, cfg, store):
    rr = _rr()
    out = []
    price_to = f"&priceTo={int(ss['max_price_usd'])}" if ss.get("max_price_usd") else ""
    for page in (1, 2):
        r = requests.get(f"{rr.REALT24_API}?{rr.REALT24_Q['sale']}&currency=usd&sortBy=dateDesc"
                         f"&page={page}&perPage=100{price_to}", headers=rr.HEADERS, timeout=25)
        r.raise_for_status()
        d = r.json()
        for it in d.get("data") or []:
            addr = (((it.get("address") or {}).get("fullAddress") or {}).get("ru") or "")
            if not addr.startswith("Ташкент"):
                continue
            name = ((it.get("name") or {}).get("ru") or "")
            desc = ((it.get("description") or {}).get("ru") or "")
            rooms = 6 if name.startswith("Более 5") else rr.as_int((ROOMS_RE.search(name) or [None, None])[1])
            area = _num((AREA_RE.search(name) or [None, None])[1])
            fl = FLOOR_RE.search(name)
            role = (((it.get("user") or {}).get("role") or {}).get("key") or "")
            hint = "agency" if it.get("isCommissioned") or role in ("agent", "agency", "realtor") or it.get("company") \
                else ("owner" if role == "owner" else "")
            photos = [x.get("w600") or x.get("original") for x in (it.get("imageSets") or [])[:6]
                      if isinstance(x, dict) and (x.get("w600") or x.get("original"))]
            out.append(_listing(
                key=f"sale:realt24:{it.get('id')}", source="Realt24 · продажа", site="Realt24",
                url=REALT24_URL.format(id=it.get("id")), title=name[:90], text=(desc or name)[:900],
                price_value=((it.get("price") or {}).get("usd")), price_currency="USD",
                rooms=rooms, area=rr.sane(area, 10, 500),
                floor=rr.sane(rr.as_int(fl.group(1)), 1, 60) if fl else None,
                floors_total=rr.sane(rr.as_int(fl.group(2)), 1, 60) if fl else None,
                district=rr.canon_district(addr, desc), district_raw=addr,
                phones=rr.extract_phones(str(it.get("phone") or "")),
                created_at=it.get("publishedAt") or it.get("createdAt"),
                seller_id=f"realt24:{(it.get('user') or {}).get('id')}", seller_hint=hint,
                photo_urls=photos, new_building=bool(it.get("residence"))))
        if not (d.get("meta") or {}).get("hasNext"):
            break
        time.sleep(0.5)
    return out


# ------------------------------------------------------------------- Joymee --

def _joymee_detail(rr, lid):
    r = requests.get(f"{rr.JOYMEE_API}{lid}/", headers=rr.HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_joymee(ss, cfg, store):
    """Список — по районам, сразу с бюджетом и комнатами; детали (телефон, площадь, этаж) —
    только для новых объявлений."""
    rr = _rr()
    base = dict(rr.JOYMEE_Q["sale"], region=rr.JOYMEE_TASHKENT, ordering="newest")
    if ss.get("max_price_usd"):
        base["max_price"] = int(ss["max_price_usd"])
    rooms = ss.get("rooms") or []
    if len(rooms) == 1:
        base["room_quantity"] = rooms[0]
    dists = [JOYMEE_DISTRICTS[d] for d in (ss.get("districts") or []) if d in JOYMEE_DISTRICTS] or [None]
    if len(dists) >= 8:                        # почти весь город — одним запросом без фильтра района
        dists = [None]
    items = {}
    for did in dists:
        for page in (1, 2):
            params = dict(base, page=page)
            if did:
                params["district"] = did
            r = requests.get(rr.JOYMEE_API, params=params, headers=rr.HEADERS, timeout=25)
            r.raise_for_status()
            d = r.json()
            for x in d.get("results") or []:
                items[x["id"]] = x
            if not d.get("next"):
                break
            time.sleep(0.4)
    out, details = [], 0
    for lid, x in items.items():
        key = f"sale:joymee:{lid}"
        if store.known(key) or details >= 12:           # за проход — не больше 12 карточек
            continue
        try:
            det = _joymee_detail(rr, lid)
            details += 1
        except (requests.RequestException, ValueError) as e:
            rr.log.info("[продажа] Joymee %s: %s", lid, e)
            continue
        dt = det.get("detail") or {}
        pr = det.get("pricing") or {}
        cur = "USD" if str(pr.get("currency")) == "2" else "UZS"
        dist = (det.get("district") or {}).get("name") if isinstance(det.get("district"), dict) else ""
        photos = [((m.get("file") or {}).get("url")) for m in (det.get("media") or [])[:6]
                  if isinstance(m, dict) and (m.get("file") or {}).get("url")]
        seller = det.get("seller") or {}
        out.append(_listing(
            key=key, source="Joymee · продажа", site="Joymee", url=JOYMEE_URL.format(id=lid),
            title=(det.get("title") or "")[:90], text=(det.get("description") or det.get("title") or "")[:900],
            price_value=_num(pr.get("price")), price_currency=cur,
            rooms=rr.as_int(dt.get("room_quantity")), area=rr.sane(_num(dt.get("area_m2")), 10, 500),
            floor=rr.sane(rr.as_int(dt.get("floor_number")), 1, 60),
            floors_total=rr.sane(rr.as_int(dt.get("floors_count")), 1, 60),
            district=rr.canon_district(dist, det.get("address_line"), det.get("description")),
            district_raw=det.get("address_line"),
            phones=rr.extract_phones(str(det.get("phone_number") or "")),
            created_at=det.get("ads_at"), seller=" ".join(v for v in (seller.get("first_name"),
                                                                        seller.get("last_name")) if v),
            seller_id=f"joymee:{seller.get('id') or (det.get('created_by') or {}).get('id')}",
            seller_hint={1: "owner", 2: "agency"}.get(det.get("advertiser_type"), ""),
            photo_urls=photos, repair=JOYMEE_REPAIR.get(dt.get("repair")),
            new_building=dt.get("apartment_type") == 2 or "новостро" in (det.get("title") or "").lower(),
            mortgage=bool(det.get("mortgage_available"))))
        time.sleep(0.3)
    return out


# ----------------------------------------------------------------- Realting --

def parse_realting(page):
    rr = _rr()
    out, seen = [], set()
    parts = re.split(r'(?=href="https://realting\.uz/property/\d+")', page)
    blocks = {}
    for p in parts[1:]:
        m = re.match(r'href="https://realting\.uz/property/(\d+)"', p)
        if m:
            blocks.setdefault(m.group(1), []).append(p[:9000])
    for pid, chunks in blocks.items():
        blk = " ".join(chunks)
        if pid in seen:
            continue
        seen.add(pid)
        title = re.search(r'teaser-title[^"]*">([^<]+)<', blk)
        route = re.search(r'<div class="route">([^<]+)<', blk)
        if not route or "Ташкент" not in route.group(1):
            continue
        units = {t: v for t, v in re.findall(r'title="([^"]+)">\s*<img[^>]*>\s*<span>([^<]+)</span>', blk)}
        txt = re.search(r'<div class="clamp-3">(.*?)</div>', blk, re.S)
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", txt.group(1))).strip() if txt else ""
        usd = re.search(r'data-price-USD="\$([\d\s]+)"', blk)
        tg = re.search(r'href="https://(?:telegram\.me|t\.me)/([A-Za-z][A-Za-z0-9_]{3,31})\?', blk)
        fl = FLOOR_RE.search(units.get("Этаж", ""))
        price = _num(usd.group(1)) if usd else None
        out.append(_listing(
            key=f"sale:realting:{pid}", source="Realting · продажа", site="Realting",
            url=f"https://realting.uz/property/{pid}",
            title=html_lib.unescape(title.group(1)).strip()[:90] if title else f"Realting #{pid}",
            text=text[:900], price_value=price, price_currency="USD",
            rooms=rr.as_int(units.get("Число комнат")),
            area=rr.sane(_num((AREA_RE.search(units.get("Площадь", "")) or [None, None])[1]), 10, 500),
            floor=rr.sane(rr.as_int(fl.group(1)), 1, 60) if fl else None,
            floors_total=rr.sane(rr.as_int(fl.group(2)), 1, 60) if fl else None,
            district=rr.canon_district(text), district_raw=route.group(1).strip(),
            phones=rr.extract_phones(text), seller_hint="agency" if tg else "",
            seller=("@" + tg.group(1)) if tg else "", seller_id=f"realting:{tg.group(1) if tg else pid}",
            new_building="жк" in text.lower() or "новостро" in text.lower()))
    return out


def fetch_realting(ss, cfg, store):
    rr = _rr()
    urls = [f"{REALTING_LIST}/{REALTING_ROOMS[r]}" for r in (ss.get("rooms") or []) if r in REALTING_ROOMS] \
        or [REALTING_LIST]
    out = []
    for u in urls:
        for page in (1, 2):
            r = requests.get(u, params={"page": page} if page > 1 else None, headers=rr.HEADERS, timeout=25)
            r.raise_for_status()
            out += parse_realting(r.text)
            time.sleep(1)
    return out


# ---------------------------------------------------------------- Telegram --

def fetch_tg_sale(ss, cfg, store):
    """Публичные каналы: превью t.me/s отдаёт несколько последних постов — опрашиваем часто."""
    rr = _rr()
    out = []
    for l in rr.fetch_telegram({"channels": ss.get("sale_channels") or SALE_CHANNELS}, cfg):
        if not rr._sale_post(l.get("text")):
            continue
        txt = l.get("text") or ""
        if l.get("price_value") is None:
            pv, pc = rr.extract_price_from_text(txt, max_usd=3_000_000)
            l["price_value"], l["price_currency"] = pv, pc
        a = AREA_RE.search(txt)
        fl = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*(?:эт|qavat|этаж)?", txt)
        out.append(_listing(**{**l, "key": "sale:" + l["key"], "site": "Telegram",
                               "area": rr.sane(_num(a.group(1)), 10, 500) if a else None,
                               "floor": rr.sane(rr.as_int(fl.group(1)), 1, 60) if fl else None,
                               "floors_total": rr.sane(rr.as_int(fl.group(2)), 1, 60) if fl else None,
                               "seller_hint": "agency" if any(w in txt.lower() for w in rr.BROKER_POST_WORDS) else "",
                               "mortgage": True if "ипотек" in txt.lower() or "ipotek" in txt.lower() else None}))
    return out


# --------------------------------------------------------------- Yangiuylar --

def fetch_yangiuylar(ss, cfg, store):
    """Новостройки: планировки с ценой застройщика. Цена за м² × площадь = ориентир «от»."""
    rr = _rr()
    r = requests.get(f"{YU_API}/object", params={"limit": 300}, headers=rr.HEADERS, timeout=30)
    r.raise_for_status()
    objs = {o["id"]: o for o in r.json().get("data") or []
            if o.get("region_id") == YU_TASHKENT and not o.get("is_archive") and not o.get("is_commercial")}
    usd_rate = cfg.get("uzs_per_usd") or 12000
    out = []
    for rooms in (ss.get("rooms") or [1, 2, 3]):
        for page in range(1, 6):
            r = requests.get(f"{YU_API}/planning", params={"filter[rooms]": rooms, "limit": 100, "page": page},
                             headers=rr.HEADERS, timeout=30)
            r.raise_for_status()
            d = r.json()
            for p in d.get("data") or []:
                o = objs.get(p.get("object_id"))
                price, area = _num(p.get("price")), _num(p.get("total_space"))
                if not o or not price or not area:
                    continue
                usd = price if p.get("currency_type") == 2 else price / usd_rate
                total = usd * area if p.get("price_type", 1) == 1 else usd   # 1 — цена за м²
                bits = [f"ЖК «{o.get('name')}»", (o.get("address") or "")[:120]]
                if o.get("installment"):
                    bits.append(f"рассрочка до {o['installment']} мес.")
                if o.get("initial_payment"):
                    bits.append(f"взнос от {o['initial_payment']}%")
                if o.get("completion_date"):
                    bits.append(f"сдача {str(o['completion_date'])[:7]}")
                out.append(_listing(
                    key=f"sale:yu:{p['id']}", source="Yangiuylar · новостройка", site="Yangiuylar",
                    url=f"https://yangiuylar.uz/object/{o['id']}/apartment-details/{p['id']}",
                    title=f"ЖК {o.get('name')}: {rooms}-комн, {area:g} м²",
                    text=" · ".join(b for b in bits if b), price_value=round(total), price_currency="USD",
                    rooms=rooms, area=rr.sane(area, 10, 500), district=rr.canon_district(o.get("address")),
                    district_raw=o.get("address"), phones=rr.extract_phones(str(o.get("phone") or "")),
                    created_at=p.get("updated_at") or o.get("updated_at"), seller=o.get("name") or "",
                    seller_id=f"yu:{o.get('company_id')}", seller_hint="developer", new_building=True))
            if not (d.get("meta") or {}).get("next"):
                break
            time.sleep(0.4)
    return out


DOWN_RE = re.compile(r"п\s*[/\\]\s*в|первоначальн|первый взнос|взнос от|boshlang.?ich|ot\s+\d+\s*%\s*vznos", re.I)
USD_IN_TEXT = re.compile(r"(?<![\d.,])(\d{1,3}(?:[\s.,]\d{3})+|\d{4,7})\s*(?:y\.?\s?e|у\.?\s?е|\$|usd|dollar)", re.I)


def _usd_amounts(text):
    out = []
    for m in USD_IN_TEXT.finditer(text or ""):
        v = _num(re.sub(r"[\s.,](?=\d{3}(?!\d))", "", m.group(1)))
        if v:
            out.append(v)
    return out


def normalize(l, cfg):
    """Общая доводка цены.
    - «700$» за 52 м² — это цена за м² → полная цена;
    - в цене первый взнос («П/В 23 300 у.е., цена 77 550») → берём полную цену из текста,
      а если её нет — помечаем, чтобы не принять взнос за стоимость квартиры."""
    rr = _rr()
    p = rr.to_usd(l.get("price_value"), l.get("price_currency"), cfg)
    if p and l.get("area") and 250 <= p <= 4000 and p * l["area"] >= 10000:
        l["price_value"], l["price_currency"] = round(p * l["area"]), "USD"
        l["price_note"] = f"цена указана за м² (${p:,.0f}) — пересчитала на площадь".replace(",", " ")
        return l
    text = f"{l.get('title') or ''} {l.get('text') or ''}"
    if p and DOWN_RE.search(text):
        bigger = [v for v in _usd_amounts(text) if v > p * 1.3]
        if bigger:
            full = max(bigger)
            l["price_value"], l["price_currency"] = round(full), "USD"
            l["price_note"] = (f"в цене объявления — первый взнос ${p:,.0f}; полная цена ${full:,.0f}"
                               .replace(",", " "))
        elif l.get("area") and p / l["area"] < 600:
            l["down_payment_only"] = True
            l["price_note"] = "похоже, указан только первый взнос ($" + f"{p:,.0f}".replace(",", " ") + ")"
    return l


SOURCES = {"realt24": (fetch_realt24, 900), "joymee": (fetch_joymee, 900),
           "realting": (fetch_realting, 1800), "telegram": (fetch_tg_sale, 1200),
           "yangiuylar": (fetch_yangiuylar, 6 * 3600)}


def fetch_due(ss, cfg, store, force=False):
    """Источники, которым пора: свои интервалы, ошибки одного не мешают остальным."""
    rr = _rr()
    now = time.time()
    nxt = store.get_kv("sale_src_next") or {}
    stats = store.get_kv("sale_src_stats") or {}
    out = []
    off = set(ss.get("sources_off") or [])
    for name, (fn, every) in SOURCES.items():
        if name in off or (not force and now < nxt.get(name, 0)):
            continue
        try:
            got = fn(ss, cfg, store)
            out += got
            nxt[name] = now + every
            stats[name] = {"at": now, "n": len(got), "err": ""}
        except Exception as e:
            nxt[name] = now + min(3600, every * 2)
            stats[name] = {**stats.get(name, {}), "at": now, "err": str(e)[:120]}
            rr.log.warning("[продажа] %s: %s", name, e)
    store.set_kv("sale_src_next", nxt)
    store.set_kv("sale_src_stats", stats)
    return out


# ============================================================ оценка и подача ==

def photo_repair(cfg, l) -> bool:
    """Ремонт по фото объявления (модель через воркер). True — сделали новую оценку.
    Оценку храним в объявлении, повторно не зовём; воркер недоступен — попробуем в другой раз."""
    rr = _rr()
    if "repair_photo" in l or not l.get("photo_urls") or not cfg.get("worker_url"):
        return False
    r = rr.worker_post(cfg, "/svc/repair", {"urls": l["photo_urls"][:4]}, timeout=90)
    if not (r or {}).get("ok"):
        return False
    l["repair_photo"] = r["repair"]
    rr.log.info("[продажа] ремонт по фото: %s (%.2f) %s — %s", r["repair"].get("state"),
                r["repair"].get("confidence") or 0, r["repair"].get("signs", ""), l.get("title", "")[:40])
    return True


def score(store, l, cfg, ss):
    """Насколько вариант стоит внимания: 0…100, причины, анализ рынка."""
    import market
    try:
        x = market.analyze(store, l, cfg)
    except Exception:
        x = {}
    s, why = 50.0, []
    gap = x.get("gap_fair", x.get("gap"))           # с учётом ремонта, если похожих с таким же нет
    if gap is not None:
        s += max(-30, min(30, -gap * 200))
        if gap <= -0.05:
            why.append(f"{gap * 100:+.0f}% к рынку")
        elif gap >= 0.08:
            why.append(f"дороже рынка на {gap * 100:.0f}%")
    kind = l.get("seller_kind")
    if kind == "owner":
        s += 8
        why.append("собственник")
    elif l.get("seller_hint") == "developer":
        why.append("застройщик")
    elif kind == "agency":
        s -= 4
        why.append("агентство/маклер")
    pc = x.get("price_change")
    if pc and pc[1] < pc[0]:
        s += 10
        why.append(f"цена снижена ${pc[0]:,.0f} → ${pc[1]:,.0f}".replace(",", " "))
    if l.get("photo_urls"):
        s += 4
    if l.get("area"):
        s += 3
    if ss.get("mortgage") and (l.get("mortgage") or "ипотек" in (l.get("text") or "").lower()):
        s += 6
        why.append("ипотека")
    if "dorm" in (x.get("flags") or []):
        s -= 30
        why.append("бывшее общежитие")
    dom = x.get("days_on_market") or 0
    if l.get("listed_since") and dom:
        why.append(f"перевыложено, на рынке ~{dom:.0f} дн. — можно торговаться")
    elif dom > 60:
        why.append("долго продаётся — можно торговаться")
    strong = (gap is not None and gap <= -0.08 and "dorm" not in (x.get("flags") or [])) \
        or bool(pc and pc[1] <= pc[0] * 0.95)
    return round(max(0, min(100, s))), why, strong, x


def pick_line(l, why):
    rr = _rr()
    bits = []
    if l.get("rooms"):
        bits.append(f"{l['rooms']}к")
    if l.get("area"):
        bits.append(f"{round(l['area'], 1):g} м²")
    if l.get("floor"):
        bits.append(f"{l['floor']}/{l['floors_total']}" if l.get("floors_total") else f"эт. {l['floor']}")
    if l.get("district"):
        bits.append(l["district"])
    p = l.get("price_usd") or 0
    price = f"<b>${p:,.0f}</b>".replace(",", " ")
    if l.get("area") and p:
        price += f" (${p / l['area']:,.0f}/м²)".replace(",", " ")
    tail = (" — " + ", ".join(why)) if why else ""
    ps = sorted(a["price_usd"] for a in (l.get("alts") or []) if a.get("price_usd"))
    if ps:
        rng = f"${ps[0]:,.0f}" if len(ps) == 1 or ps[0] == ps[-1] else f"${ps[0]:,.0f}–{ps[-1]:,.0f}"
        tail += f" · 👥 ещё у {len(ps)}: {rng}".replace(",", " ")
    return (f"{price} · {' · '.join(bits)}{tail} · "
            f"<a href=\"{l.get('url')}\">{rr.escape_html(l.get('site') or 'Uybor')}</a>")


def queue_pick(store, l, sc, why):
    q = store.get_kv("sale_pick") or []
    if any(x["key"] == l["key"] for x in q):
        return
    q.append({"key": l["key"], "score": sc, "line": pick_line(l, why), "at": time.time()})
    q = sorted(q, key=lambda x: -x["score"])[:80]
    store.set_kv("sale_pick", q)


def pick_pending(store):
    q = store.get_kv("sale_pick") or []
    fresh = [x for x in q if time.time() - x.get("at", 0) < 4 * 86400]   # старше 4 дней — уже не новость
    if len(fresh) != len(q):
        store.set_kv("sale_pick", fresh)
    return fresh


def _pick_message(top, header, rest=0):
    """Текст и кнопки подборки: номер — строка; 📷 N — фото и разбор, 👍 N — в шортлист."""
    lines = [header, ""]
    for i, x in enumerate(top, 1):
        lines.append(f"{i}. {x['line']}")
    lines += ["", "📷 + номер — фото и разбор цены, 👍 + номер — заберу в шортлист. "
                  "Можно и словами: «покажи 2 и 4», «есть фото у первой?»"]
    rows = []
    for icon, act in (("📷", "v"), ("👍", "s")):
        row = []
        for i, x in enumerate(top, 1):
            row.append({"text": f"{icon} {i}", "callback_data": f"L:{act}:{x['key']}"[:64]})
            if len(row) == 5:
                rows.append(row); row = []
        if row:
            rows.append(row)
    if rest:
        rows.append([{"text": f"Показать ещё ({rest})", "callback_data": "R:pick"}])
    return "\n".join(lines)[:4000], {"inline_keyboard": rows}


def send_pick(cfg, store, limit=7, reason="Подборка дня"):
    """Лучшие из накопленного одним сообщением; присланное помечаем, остальное ждёт.
    Показанное запоминаем как «последнюю подборку» — на неё ссылаются «покажи 2 и 4»."""
    rr = _rr()
    q = pick_pending(store)
    if not q:
        return 0
    top, rest = q[:limit], q[limit:]
    text, kb = _pick_message(top, f"🔎 <b>{reason}</b> — {len(top)} из {len(q)} новых, самые интересные сверху ✨",
                             len(rest))
    ok = rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True, "reply_markup": json.dumps(kb, ensure_ascii=False)})
    if ok is None:
        return 0
    for x in top:
        store.conn.execute("UPDATE listings SET notified=1 WHERE key=?", (x["key"],))
    store.conn.commit()
    store.set_kv("sale_pick", rest)
    store.set_kv("last_pick", [{"key": x["key"], "line": x["line"]} for x in top])
    st = store.get_kv("sale_day") or {}
    st["picked"] = st.get("picked", 0) + len(top)
    store.set_kv("sale_day", st)
    return len(top)


def resend_last_pick(cfg, store):
    """«Покажи подборку ещё раз» — последняя подборка с теми же номерами."""
    rr = _rr()
    top = store.get_kv("last_pick") or []
    if not top:
        return 0
    text, kb = _pick_message(top, f"🔎 <b>Последняя подборка</b> — {len(top)} шт., номера те же")
    rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True, "reply_markup": json.dumps(kb, ensure_ascii=False)})
    return len(top)


def remember_shown(store, l, why):
    """Карточка ушла сразу (не в подборке) — тоже запоминаем, чтобы понимать «а та, что утром?»."""
    rec = (store.get_kv("shown_recent") or [])[-14:]
    rec.append({"key": l["key"], "line": pick_line(l, why), "at": time.time()})
    store.set_kv("shown_recent", rec)


KIND_RU = {"owner": "собственник", "agency": "маклер"}


def alt_entry(l):
    return {"key": l["key"], "price_usd": l.get("price_usd"), "site": l.get("site") or "Uybor",
            "url": l.get("url"), "kind": l.get("seller_kind") or "", "seller": _seller(l)}


def merge_alts(alts, extra, own):
    """Другие продавцы той же квартиры: без повторов, без самой карточки и без перевыкладок
    того же продавца (это не «ещё у одного маклера»), дешёвые сверху."""
    out, seen = [], {own["key"]}
    me = ((own.get("site") or "Uybor"), _seller(own))
    for a in list(alts or []) + [alt_entry(x) for x in extra]:
        if a.get("key") in seen or (me[1] and (a.get("site"), a.get("seller")) == me):
            continue
        seen.add(a["key"])
        out.append(a)
    return sorted(out, key=lambda a: a.get("price_usd") or 1e12)[:8]


def _put_data(store, l):
    store.conn.execute("UPDATE listings SET data=? WHERE key=?", (store.pack(l), l["key"]))
    store.conn.commit()


def attach_dup(cfg, store, l, dup, ss=None):
    """Нашлась уже известная квартира у другого продавца.
    Дороже или так же — тихо дописываем к ней «👥 ещё у N». Заметно дешевле — новая
    становится главной: в подборке заменяет прежнюю, а если прежнюю уже присылали —
    коротко сообщаем «та же квартира дешевле». Возвращает "alt" | "queued" | "cheaper"."""
    rr = _rr()
    row = store.conn.execute("SELECT data, notified FROM listings WHERE key=?", (dup,)).fetchone()
    try:
        o = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        o = {}
    if not o:
        store.save(l, notified=False, dup_of=dup)
        return "alt"
    q = store.get_kv("sale_pick") or []
    queued = any(x["key"] == dup for x in q)
    lp, op = l.get("price_usd") or 0, o.get("price_usd") or 0
    cheaper = bool(lp and op and lp <= op * 0.98 and op - lp >= 500)
    if not cheaper:
        o["alts"] = merge_alts(o.get("alts"), [l], o)
        _put_data(store, o)
        if queued:
            for x in q:
                if x["key"] == dup:
                    x["line"] = pick_line(o, o.get("why"))
            store.set_kv("sale_pick", q)
        store.save(l, notified=False, dup_of=dup)
        return "alt"
    l["alts"] = merge_alts(o.get("alts"), [o], l)
    photo_repair(cfg, l)
    sc, why, _, _ = score(store, l, cfg, ss or cfg.get("sale_search") or {})
    l["score"], l["why"] = sc, why
    if not row[1]:                               # прежняя ещё ждёт в подборке — заменяем дешёвой
        store.set_kv("sale_pick", [x for x in q if x["key"] != dup])
        store.save(l, notified=False)
        store.conn.execute("UPDATE listings SET dup_of=? WHERE key=?", (l["key"], dup))
        store.conn.commit()
        queue_pick(store, l, sc, why)
        return "queued"
    text = (f"💸 <b>Та же квартира — дешевле на ${op - lp:,.0f}</b>\n{pick_line(l, why)}\n"
            f"Раньше присылала её за ${op:,.0f} ({rr.escape_html(o.get('site') or 'Uybor')}"
            f"{', ' + KIND_RU[o['seller_kind']] if o.get('seller_kind') in KIND_RU else ''}).").replace(",", " ")
    kb = {"inline_keyboard": [[{"text": "📷 Фото и разбор", "callback_data": f"L:v:{l['key']}"[:64]},
                               {"text": "👍 В шортлист", "callback_data": f"L:s:{l['key']}"[:64]}]]}
    ok = rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True, "reply_markup": json.dumps(kb, ensure_ascii=False)})
    store.save(l, notified=ok is not None, dup_of=dup)
    if ok is not None:
        remember_shown(store, l, why)
    return "cheaper"


def maybe_daily_pick(cfg, store, now=None):
    """В 19:30 по Ташкенту — подборка дня (до вечерних итогов в 20:00)."""
    import concierge as cg
    loc = (now or datetime.now(timezone.utc)).astimezone(cg.TZ)
    today = loc.date().isoformat()
    if not (19 * 60 + 30 <= loc.hour * 60 + loc.minute < 23 * 60) or store.get_kv("sale_pick_day") == today:
        return 0
    store.set_kv("sale_pick_day", today)
    return send_pick(cfg, store)


def day_stats(store, add=None):
    """Счётчики дня для экрана «Ищет Ra'no»: просмотрено, подошло, прислала сразу, в подборку."""
    import concierge as cg
    today = datetime.now(cg.TZ).date().isoformat()
    st = store.get_kv("sale_day") or {}
    if st.get("day") != today:
        st = {"day": today}
    for k, v in (add or {}).items():
        st[k] = st.get(k, 0) + v
    if add:
        store.set_kv("sale_day", st)
    return st


_BLOCK_NAMED = re.compile(
    r"(?:юнусабад|юнусобод|yunusobod|yunusabad|чиланзар|чилонзор|chilonzor|chilanzar|сергели|sergeli|"
    r"куйлюк|қўйлиқ|qo'?yliq|кушбеги|qo'?shbegi)\w*\s*[-–]?\s*(\d{1,2})(?!\d|/|[.,]\d)", re.I)
_BLOCK_WORD = re.compile(r"(?<![\d/])(\d{1,2})\s*[-–]?\s*(?:й\s*|ый\s*)?(?:квартал|kvartal|kvartl|kvrtal|кв-л|массив|massiv)", re.I)


def block_no(l):
    """Номер квартала/массива из адреса и текста («Юнусабад-14», «2 квартал», «6кв») — или None."""
    src = " ".join(str(l.get(k) or "") for k in ("title", "district_raw")) + " " + (l.get("text") or "")[:300]
    for rx in (_BLOCK_NAMED, _BLOCK_WORD):
        m = rx.search(src)
        if m and 0 < int(m.group(1)) <= 40:
            return int(m.group(1))
    return None


def _seller(l):
    return str(l.get("seller_id") or l.get("seller") or "").strip().lower()


def same_flat(a, b):
    """Похоже на одну квартиру: комнаты, этаж и этажность, район, квартал, площадь и цена.
    Разные продавцы (другой сайт или другой маклер на том же сайте) — площадь ±3%, цена ±12%:
    маклеры перевыкладывают квартиру собственника со своей наценкой 5–10%.
    Тот же продавец или продавец неизвестен на том же сайте — строже (±1%), чтобы одинаковые
    планировки разных квартир в одном ЖК не склеились."""
    if not (a.get("area") and b.get("area") and a.get("price_usd") and b.get("price_usd")):
        return False
    if not (a.get("floor") and b.get("floor")) or a["floor"] != b["floor"]:
        return False
    for k in ("rooms", "district", "floors_total"):
        if a.get(k) and b.get(k) and a[k] != b[k]:
            return False
    ba, bb = block_no(a), block_no(b)
    if ba and bb and ba != bb:
        return False
    same_site = (a.get("site") or "Uybor") == (b.get("site") or "Uybor")
    sa, sb = _seller(a), _seller(b)
    strict = same_site and (not sa or not sb or sa == sb)
    if not strict and not (a.get("district") and a.get("district") == b.get("district")):
        strict = True                       # район неизвестен — мягкое сравнение рискованно
    da, dp = (0.01, 0.01) if strict else (0.03, 0.12)
    return abs(a["area"] - b["area"]) / a["area"] <= da and \
        abs(a["price_usd"] - b["price_usd"]) / a["price_usd"] <= dp


def structural_dup(store, l, days=45):
    """Та же квартира, уже показанная (или ждущая в подборке), — по same_flat."""
    if not (l.get("area") and l.get("price_usd")):
        return None
    queued = {x["key"] for x in (store.get_kv("sale_pick") or [])}
    rows = store.conn.execute(
        "SELECT key, data, notified FROM listings WHERE price_usd BETWEEN ? AND ? AND key != ?",
        (l["price_usd"] * 0.88, l["price_usd"] * 1.14, l["key"])).fetchall()
    for key, data, notified in rows:
        if not notified and key not in queued:      # отсеянное раньше — не «уже показанное»
            continue
        try:
            o = json.loads(data or "{}")
        except ValueError:
            continue
        if same_flat(l, o):
            return key
    return None

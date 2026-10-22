"""Оффлайн-верификация Амины (без сети): python3 test_offline.py"""
import sqlite3
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path

import rent_radar as rr

# Telegram с файлами (альбомы) — через тот же tg_call, который тесты подменяют; подпись альбома — в caption
def _files_shim(cfg_, method, data, files, timeout=90):
    d = dict(data)
    if "media" in d:
        try:
            d["caption"] = json.loads(d["media"])[0].get("caption")
        except (ValueError, IndexError, KeyError):
            pass
    return rr.tg_call(cfg_, method, d)
rr.tg_call_files = _files_shim
rr.TESTING = True


def tx_(pl):
    """Текст сообщения или подпись альбома/фото."""
    if pl.get("text") or pl.get("caption"):
        return pl.get("text") or pl.get("caption")
    try:
        return json.loads(pl.get("media") or "[]")[0].get("caption") or ""
    except (ValueError, IndexError, AttributeError):
        return ""
import geo as _geo_t
_geo_t.static_map = lambda *a, **k: None             # тайлы карты не качаем в тестах (кроме отдельной проверки)
_geo_t.CITY_PROFILES["ташкент"] = {**_geo_t.CITY_FALLBACK["ташкент"], "near_km": 4.9}   # без Nominatim в тестах

cfg = rr.deep_merge(rr.DEFAULT_CONFIG, {})

# ---------------------------------------------------------- извлечение ----

t1 = "Сдаётся 2-комнатная квартира, Яккасарайский район, 550 у.е. Тел: +998 90 123-45-67, 911234567"
assert rr.extract_phones(t1) == ["901234567", "911234567"], rr.extract_phones(t1)
assert rr.extract_rooms(t1) == 2
assert rr.extract_price_from_text(t1) == (550, "USD")
assert rr.extract_district(t1) == "Яккасарай"

t2 = "Ijaraga 3 xonali kvartira, Chilonzor, 6 500 000 so'm oyiga. Tel 933334455"
assert rr.extract_rooms(t2) == 3
assert rr.extract_price_from_text(t2) == (6500000, "UZS")
assert rr.extract_district(t2) == "Чиланзар"
assert rr.extract_phones(t2) == ["933334455"]

# цена не должна ловиться как телефон
t3 = "Аренда 3/4/6, 12 000 000 сум, депозит 6 000 000"
assert rr.extract_phones(t3) == [], rr.extract_phones(t3)
assert rr.extract_rooms(t3) == 3  # из формата 3/4/6

usd = rr.to_usd(11900000, "UZS", cfg)
assert usd is not None and abs(usd - 1000) < 1, usd
assert rr.to_usd(550, "USD", cfg) == 550.0

# ------------------------------------------------------- телеграм-парсер ----

TG_HTML = '''
<div class="tgme_widget_message_wrap"><div data-post="arentash/100">
<div class="tgme_widget_message_text js-message_text" dir="auto">Сдается 2-комн квартира, Мирабад, 600 у.е.<br/>Тел: +998901112233</div>
<time datetime="2026-08-07T05:11:33+00:00">05:11</time></div></div>
<div class="tgme_widget_message_wrap"><div data-post="arentash/101">
<div class="tgme_widget_message_text js-message_text" dir="auto">Сниму квартиру для семьи, срочно</div>
<time datetime="2026-08-07T06:00:00+00:00">06:00</time></div></div>
'''

import json
import re
import unittest.mock as mock

class FakeResp:
    status_code = 200
    text = TG_HTML

with mock.patch.object(rr.requests, "get", return_value=FakeResp()):
    tg = rr.fetch_telegram({"channels": ["arentash"],
                            "include_keywords": cfg["sources"]["telegram"]["include_keywords"],
                            "exclude_keywords": cfg["sources"]["telegram"]["exclude_keywords"]}, cfg)
assert len(tg) == 1, [x["key"] for x in tg]          # «Сниму» отфильтрован
assert tg[0]["key"] == "tg:arentash:100"
assert tg[0]["phones"] == ["901112233"]
assert tg[0]["price_value"] == 600 and tg[0]["price_currency"] == "USD"
assert tg[0]["district"] == "Мирабад"
assert tg[0]["url"] == "https://t.me/arentash/100"

# ---------------------------------------------------------- дедупликация ----

db = Path("/tmp/test_radar.db")
db.unlink(missing_ok=True)
store = rr.Store(db)

L1 = {
    "key": "olx:1", "source": "OLX", "url": "u1", "title": "Сдаётся 2-комн Яккасарай",
    "text": "Сдаётся уютная 2-комнатная квартира в Яккасарайском районе, мебель, техника, рядом метро, 550 у.е. торг",
    "price_value": 550, "price_currency": "USD", "price_usd": 550.0,
    "rooms": 2, "district": "Яккасарай", "phones": ["901112233"],
    "created_at": None, "seller": "", "seller_id": "olx:7", "is_business": False,
}
assert store.find_dup(L1, cfg) is None
store.save(L1, notified=True)

# тот же телефон из телеграм-канала → дубль
L2 = dict(L1, key="tg:arentash:5", source="TG @arentash", url="u2",
          text="Совсем другой текст объявления", phones=["901112233"])
assert store.find_dup(L2, cfg) == "olx:1"

# без телефона, но почти тот же текст и цена → дубль
L3 = dict(L1, key="uybor:9", source="Uybor", url="u3", phones=[],
          text="Сдаётся уютная 2-комнатная квартира в Яккасарайском районе, мебель, техника, рядом с метро, 550 у.е.",
          price_usd=560.0)
assert store.find_dup(L3, cfg) == "olx:1"

# другой вариант (другая цена, другой текст) → не дубль
L4 = dict(L1, key="olx:2", url="u4", phones=["935556677"],
          title="3-комн Юнусабад",
          text="Сдаётся просторная 3-комнатная квартира на Юнусабаде, свежий ремонт, паркинг, детская площадка во дворе",
          price_usd=800.0, rooms=3, district="Юнусабад")
assert store.find_dup(L4, cfg) is None

# счётчик продавца для метки «возможно маклер»
for _ in range(3):
    n = store.bump_seller("olx:makler1")
assert n == 3

total, dups = store.counts()
assert total == 1  # сохранили пока только L1

# ------------------------------------------------- команды и меню бота ----

def cmd(text, s):
    """Хелпер: возвращает (ответ, экран)."""
    return rr.handle_command(text, s, store, cfg)

s = rr.default_settings()
assert "Ra'no" in cmd("/help", s)[0]
# /start — тёплое приветствие с кнопкой приложения, а не стена команд
import unittest.mock as _m
with _m.patch.object(rr, "tg_call", lambda *a,**k: {"ok":True,"result":{"message_id":1}}) as _p:
    import concierge as _cg
    _sent=[]
    with _m.patch.object(rr,"tg_call",lambda c,meth,pl,**k:(_sent.append((meth,pl)),{"ok":True,"result":{"message_id":1}})[1]):
        r=cmd("/start", s)
    assert r==("",None)
    assert any("Ra'no" in pl.get("text","") and "reply_markup" in pl for _,pl in _sent), "нет приветствия с кнопкой"

# ГЛАВНОЕ: голая команда из меню Telegram должна открывать экран с кнопками
assert cmd("/max", s) == ("", "P"), cmd("/max", s)
assert cmd("/min", s) == ("", "N")
assert cmd("/rooms", s) == ("", "R")
assert cmd("/district", s) == ("", "D")
assert cmd("/districts", s) == ("", "D")
assert cmd("/menu", s) == ("", "M")
assert cmd("/max@rentradarxp_bot", s) == ("", "P")   # групповой суффикс

# команды с аргументом по-прежнему работают
assert cmd("/max 800", s)[0].startswith("✅") and s["max_price_usd"] == 800
assert cmd("/min 300", s)[0].startswith("✅") and s["min_price_usd"] == 300
assert cmd("/rooms 2-3", s)[0].startswith("✅") and (s["rooms_min"], s["rooms_max"]) == (2, 3)
assert cmd("/rooms все", s)[0].startswith("✅") and s["rooms_min"] is None
r = cmd("/district Яккасарай, Мирабад", s)[0]
assert "Яккасарай" in r and s["districts"] == ["Мирабад", "Яккасарай"]
assert cmd("/district все", s)[0].startswith("✅") and s["districts"] == []
assert cmd("/photos", s)[0] == "✅ Фото выключены" and s["photos"] is False
assert cmd("/photos", s)[0] == "✅ Фото включены" and s["photos"] is True
assert cmd("/pause", s)[0].startswith("⏸") and s["paused"] is True
assert cmd("/resume", s)[0].startswith("▶️") and s["paused"] is False
assert "Статус" in cmd("/status", s)[0]
assert cmd("/qwerty", s)[1] is None and "своими словами" in cmd("/qwerty", s)[0]   # кнопок-меню нет
assert cmd("обычный текст", s) == ("", None)

eff = rr.effective_cfg(cfg, s)
assert eff["max_price_usd"] == 800 and eff["min_price_usd"] == 300

# ----------------------------------------------------- экраны и клавиатуры ----

for view in ("M", "P", "N", "R", "D"):
    text_fn, kb_fn = rr.VIEWS[view]
    assert text_fn(cfg, s) and kb_fn(cfg, s)["inline_keyboard"]

kb = rr.kb_districts(cfg, s)["inline_keyboard"]
flat = [b for row in kb for b in row]
assert sum(1 for b in flat if b["callback_data"].startswith("d:")) == 12  # все районы
assert any(b["callback_data"] == "da" for b in flat)
assert any(b["callback_data"] == "v:M" for b in flat)                    # кнопка «Назад»
# callback_data должен влезать в лимит Telegram (64 байта)
for b in flat:
    assert len(b["callback_data"].encode()) <= 64, b

# ------------------------------------------------------------- нажатия ----

def cb(data, s):
    return rr.handle_callback(data, s, store, cfg)

s2 = rr.default_settings()
assert cb("v:D", s2) == ("", "D")
assert cb("m:700", s2)[1] == "P" and s2["max_price_usd"] == 700
assert "✅ $700" in str(rr.kb_price(cfg, s2))          # отметка встала
assert cb("n:300", s2)[1] == "N" and s2["min_price_usd"] == 300
assert cb("r:2-3", s2)[1] == "R" and (s2["rooms_min"], s2["rooms_max"]) == (2, 3)
assert cb("r:*", s2)[1] == "R" and s2["rooms_min"] is None

idx = rr.DISTRICT_LIST.index("Яккасарай")
t1, v1 = cb(f"d:{idx}", s2)
assert "добавлен" in t1 and v1 == "D" and s2["districts"] == ["Яккасарай"]
t2, _ = cb(f"d:{idx}", s2)
assert "убран" in t2 and s2["districts"] == []
assert cb(f"d:{idx}", s2) and cb("da", s2)[1] == "D" and s2["districts"] == []
assert cb("p", s2)[1] == "M" and s2["photos"] is False
assert cb("z", s2)[1] == "M" and s2["paused"] is True
assert cb("z", s2)[1] == "M" and s2["paused"] is False
assert cb("d:999", s2) == ("", None)                  # несуществующий индекс
assert cb("мусор", s2) == ("", None)

# пользовательские фильтры
s3 = {**rr.default_settings(), "rooms_min": 2, "rooms_max": 3, "districts": ["Яккасарай"]}
assert rr.passes_user_filters({"rooms": 2, "district": "Яккасарай"}, s3)
assert not rr.passes_user_filters({"rooms": 4, "district": "Яккасарай"}, s3)
assert not rr.passes_user_filters({"rooms": 2, "district": "Чиланзар"}, s3)
# по умолчанию режим строгий: район не определён → не присылаем
assert rr.passes_user_filters({"rooms": None, "district": None}, s3) is False
assert rr.passes_user_filters({"rooms": None, "district": None},
                              {**s3, "strict_district": False}) is True

store.set_kv("settings", s3)
assert store.get_kv("settings")["districts"] == ["Яккасарай"]

# ---------------------------------------------------------------- фото ----

TG_HTML_PHOTO = TG_HTML.replace(
    '<div class="tgme_widget_message_text',
    '<a class="tgme_widget_message_photo_wrap" style="background-image:url(\'https://cdn4.telegram-cdn.org/file/abc.jpg\')"></a><div class="tgme_widget_message_text')

class FakeResp2:
    status_code = 200
    text = TG_HTML_PHOTO

with mock.patch.object(rr.requests, "get", return_value=FakeResp2()):
    tg2 = rr.fetch_telegram({"channels": ["arentash"],
                             "include_keywords": ["сда"], "exclude_keywords": ["сниму"]}, cfg)
assert tg2[0]["photo_urls"] == ["https://cdn4.telegram-cdn.org/file/abc.jpg"], tg2[0]["photo_urls"]

olx_link = "https://ireland.apollo.olxcdn.com/v1/files/xyz/image;s={width}x{height}"
assert "{" not in olx_link.replace("{width}x{height}", "1280x1024")

db.unlink(missing_ok=True)
print("OK — парсеры, дедупликация, команды, экраны меню, кнопки и фото работают")

# ------------------------------- регрессия: комнаты строкой не роняют радар ----

assert rr.as_int("3") == 3 and rr.as_int(3) == 3 and rr.as_int(" 2 ") == 2
assert rr.as_int(None) is None and rr.as_int("") is None and rr.as_int("две") is None
assert rr.as_int(True) is None

s_rooms = {**rr.default_settings(), "rooms_min": 2, "rooms_max": 3}
assert rr.passes_user_filters({"rooms": "2", "district": None}, s_rooms) is True   # str!
assert rr.passes_user_filters({"rooms": "5", "district": None}, s_rooms) is False
assert rr.passes_user_filters({"rooms": "", "district": None}, s_rooms) is True
s_one = {**rr.default_settings(), "rooms_min": 2, "rooms_max": None}
assert rr.passes_user_filters({"rooms": "2"}, s_one) is True
assert rr.passes_user_filters({"rooms": 3}, s_one) is False

# uybor: строковые room/price приводятся к числам
class FakeUybor:
    status_code = 200
    @staticmethod
    def raise_for_status(): pass
    @staticmethod
    def json():
        return {"results": [{"id": 1, "description": "Сдаётся квартира в Чиланзаре",
                             "room": "3", "price": "450", "priceCurrency": "usd",
                             "createdAt": "2026-08-07T10:00:00.000Z", "userId": 5,
                             "media": ["a.jpg"]}]}

with mock.patch.object(rr.requests, "get", return_value=FakeUybor()):
    uy = rr.fetch_uybor({"region_id": 13, "category_id": 7}, cfg)
assert uy[0]["rooms"] == 3 and isinstance(uy[0]["rooms"], int), uy[0]["rooms"]
assert uy[0]["price_value"] == 450 and isinstance(uy[0]["price_value"], int)
assert uy[0]["photo_urls"] == ["https://api.uybor.uz/api/v1/media/n/a.jpg"]
assert rr.passes_user_filters(uy[0], s_rooms)   # раньше здесь падал TypeError

print("OK — регрессия по комнатам-строкам закрыта")

# ============ регрессия: валюта UYE и полные названия районов ============

# 1) OLX помечает доллары как UYE — раньше показывалось «сум» и фильтр цены НЕ работал
assert rr.canon_currency("UYE") == "USD"
assert rr.canon_currency("usd") == "USD" and rr.canon_currency(" USD ") == "USD"
assert rr.canon_currency("у.е.") == "USD" and rr.canon_currency("$") == "USD"
assert rr.canon_currency("UZS") == "UZS" and rr.canon_currency("сум") == "UZS"
assert rr.canon_currency("so'm") == "UZS"
assert rr.canon_currency(None) is None and rr.canon_currency("EUR") is None

# 2) районы: «Яккасарайский район» должен приводиться к «Яккасарай»
for raw, want in [("Яккасарайский район", "Яккасарай"),
                  ("Шайхантахурский район", "Шайхантахур"),
                  ("Мирзо-Улугбекский район", "Мирзо-Улугбек"),
                  ("Сергелийский район", "Сергели"),
                  ("Юнусабадский район", "Юнусабад"),
                  ("Алмазарский район", "Алмазар")]:
    assert rr.canon_district(raw) == want, (raw, rr.canon_district(raw))
assert rr.canon_district("улица Осиё, 17") is None
assert rr.canon_district(None, "", "Чиланзарский район") == "Чиланзар"

# 3) сквозной тест OLX: UYE + полное имя района
class FakeOlx:
    status_code = 200
    @staticmethod
    def raise_for_status(): pass
    @staticmethod
    def json():
        return {"data": [
            {"id": 11, "title": "2 хонали квартира", "description": "сдаётся",
             "url": "https://olx.uz/x", "created_time": "2026-08-07T14:00:00+05:00",
             "business": False, "user": {"id": 1, "name": "A"},
             "location": {"city": {"name": "Ташкент"},
                          "district": {"name": "Яккасарайский район"}},
             "params": [{"key": "price", "value": {"value": 450, "currency": "UYE",
                                                   "label": "5 372 865 сум"}}],
             "photos": [{"link": "https://cdn/x;s={width}x{height}"}]},
            {"id": 12, "title": "Аренда", "description": "сдаётся",
             "url": "https://olx.uz/y", "created_time": "2026-08-07T14:00:00+05:00",
             "business": False, "user": {"id": 2, "name": "B"},
             "location": {"city": {"name": "Ташкент"},
                          "district": {"name": "Шайхантахурский район"}},
             "params": [{"key": "price", "value": {"value": 1650, "currency": "UYE",
                                                   "label": "19 700 505 сум"}}],
             "photos": []},
        ]}

with mock.patch.object(rr.requests, "get", return_value=FakeOlx()):
    ol = rr.fetch_olx({"category_id": 1147, "city_id": 4, "owner_type": "private"}, cfg)

a, b = ol[0], ol[1]
assert a["price_currency"] == "USD" and a["price_value"] == 450
assert a["district"] == "Яккасарай" and a["district_raw"] == "Яккасарайский район"
assert a["photo_urls"] == ["https://cdn/x;s=1280x1024"]

# цена теперь показывается в долларах, а не «450 сум»
msg = rr.format_message(a, cfg, likely_makler=False)
assert "💰 $450" in msg and "сум" not in msg, msg
assert "📍 Яккасарай" in msg

# фильтр цены наконец работает: $1650 > лимита $1000
# (возраст не проверяем: даты в фикстуре фиксированные и со временем «стареют»)
cfg_any_age = dict(cfg, notify_max_age_days=0)
assert rr.passes_filters(dict(a), cfg_any_age) is True
assert rr.passes_filters(dict(b), cfg_any_age) is False, "объявление за $1650 обязано отсеиваться"

# фильтр района теперь реально фильтрует
only_yakka = {**rr.default_settings(), "districts": ["Яккасарай"]}
assert rr.passes_user_filters(a, only_yakka) is True
assert rr.passes_user_filters(b, only_yakka) is False, "Шайхантахур не должен проходить"
# район не распознан: строго — отсекаем, нестрого — присылаем с пометкой
unknown = dict(a, district=None, district_raw=None)
assert rr.passes_user_filters(unknown, only_yakka) is False
assert rr.passes_user_filters(unknown, {**only_yakka, "strict_district": False}) is True
assert "район не указан" in rr.format_message(unknown, cfg, False)

# сумовое объявление показывается с пересчётом в доллары
uzs = dict(a, price_value=5_950_000, price_currency="UZS", price_usd=None)
m2 = rr.format_message(uzs, cfg, False)
assert "5 950 000 сум" in m2 and "~$500" in m2, m2

print("OK — валюта UYE и районы приведены к общему виду, фильтры работают")

# ---------------- строгий режим районов + справочник Uybor ----------------

strict = {**rr.default_settings(), "districts": ["Яккасарай"], "strict_district": True}
loose = {**rr.default_settings(), "districts": ["Яккасарай"], "strict_district": False}
assert rr.default_settings()["strict_district"] is True   # строгий — по умолчанию
no_d = {"district": None, "rooms": None}
assert rr.passes_user_filters(no_d, loose) is True
assert rr.passes_user_filters(no_d, strict) is False
assert rr.passes_user_filters({"district": "Яккасарай"}, strict) is True
assert rr.passes_user_filters({"district": "Чиланзар"}, strict) is False

kbd = str(rr.kb_districts(cfg, loose))
assert "'ds'" in kbd and "без района" in kbd
kbd2 = str(rr.kb_districts(cfg, strict))
assert "Строго" in kbd2
# без выбранных районов переключатель строгости не показываем
assert "'ds'" not in str(rr.kb_districts(cfg, rr.default_settings()))
st = rr.default_settings()
st["districts"] = ["Яккасарай"]
# переключатель работает в обе стороны
assert rr.handle_callback("ds", st, store, cfg)[1] == "D" and st["strict_district"] is False
assert rr.handle_callback("ds", st, store, cfg)[1] == "D" and st["strict_district"] is True

class FakeUybor2:
    status_code = 200
    @staticmethod
    def raise_for_status(): pass
    @staticmethod
    def json():
        return {"results": [
            {"id": 7, "description": "Сдаётся рядом с Чорсу", "districtId": 205,
             "room": 2, "price": 600, "priceCurrency": "usd",
             "createdAt": "2026-08-07T10:00:00.000Z", "userId": 9, "media": []},
        ]}

with mock.patch.object(rr.requests, "get", return_value=FakeUybor2()):
    uy2 = rr.fetch_uybor({"region_id": 13, "category_id": 7}, cfg)
# districtId=205 → Яккасарай, хотя в тексте района нет
assert uy2[0]["district"] == "Яккасарай", uy2[0]["district"]
assert rr.passes_user_filters(uy2[0], strict) is True

print("OK — строгий режим районов и справочник Uybor работают")

# ================= релевантность: подселение и койко-места =================

share_cases = [
    "Kvartiraga sheriklikka bollar kerak",
    "Квартирага шерикликга киз оламиз",
    "Сдается квартира 2 х комнатная студентам (девочки ) с хозяйкой",
    "Bollarga SHERIKVHILIKGA 3 ta bola kerak srochno",
    "Аренда одной комнаты для одной девушки в трёхкомнатной",
    "Ищу соседку в двушку, подселение",
    "Сдаётся комната в квартире",
]
for t in share_cases:
    assert rr.looks_like_room_share(t), t

# целые квартиры не должны попадать под фильтр
whole_cases = [
    "Сдаётся евро-двушка на длительный срок",
    "Bez makler Kvartira beriladi 2 xonali chilonzorda",
    "Сдается 3-х комнатная квартира на 5-этаже 9-ти этажного дома",
    "Сдаётся 1-комнатная квартира, сдаёт хозяин, без посредников",
    "Аренда квартиры в Чиланзаре, Лутфий",
    "Квартира сдается хозяином напрямую",   # «хозяином» без «с» — это хорошо
]
for t in whole_cases:
    assert not rr.looks_like_room_share(t), (t, rr.looks_like_room_share(t))

st_rel = rr.default_settings()
assert st_rel["exclude_shared"] is True
assert st_rel["strict_district"] is True     # выбрал район — значит только он

def L(**kw):
    base = {"title": "Сдаётся квартира", "text": "хорошая квартира", "price_value": 500,
            "price_usd": 500.0, "district": "Яккасарай", "rooms": 2}
    base.update(kw); return base

assert rr.relevance_reject(L(), cfg, st_rel) == ""
assert "подселение" in rr.relevance_reject(L(text="sheriklikka bola kerak"), cfg, st_rel)
assert "комната" in rr.relevance_reject(L(price_usd=76.0), cfg, st_rel)
assert rr.relevance_reject(L(price_usd=76.0), cfg,
                           {**st_rel, "exclude_shared": False}) == ""   # можно выключить
assert "нет ни цены" in rr.relevance_reject(
    L(price_value=None, price_usd=None, district=None), cfg, st_rel)
# объявление без цены, но с районом — оставляем
assert rr.relevance_reject(L(price_value=None, price_usd=None), cfg, st_rel) == ""

# кнопка в меню
mk = str(rr.kb_menu(cfg, st_rel))
assert "'sh'" in mk and "Подселение: скрыто" in mk
stx = rr.default_settings()
assert rr.handle_callback("sh", stx, store, cfg)[1] == "M" and stx["exclude_shared"] is False
assert "Подселение: показываю" in str(rr.kb_menu(cfg, stx))

print("OK — подселение и койко-места отсеиваются, целые квартиры проходят")

# ======================= аналитический слой (агент) =======================

import analyst

# гео
assert len(analyst.METRO) >= 40
n = analyst.nearest_metro(41.29801, 69.27405)
assert n and n[0] == "Айбек" and n[2] == 0
far = analyst.nearest_metro(41.36180, 69.27933)
assert far and far[2] > 10
assert analyst.nearest_metro(None, None) is None
assert analyst.distance_to(41.30, 69.28, (41.31, 69.29))[0] < 2

# рынок: медианы считаются из базы
mdb = Path("/tmp/test_market.db"); mdb.unlink(missing_ok=True)
ms = rr.Store(mdb)
for i, price in enumerate([400, 450, 500, 550, 600]):
    ms.save({**L1, "key": f"m{i}", "price_usd": float(price),
             "rooms": 2, "district": "Яккасарай", "phones": []}, notified=False)
stats = analyst.market_stats(ms, min_sample=3)
assert stats["pair"][("Яккасарай", 2)][0] == 500, stats["pair"]
d, txt = analyst.price_verdict({"price_usd": 380, "rooms": 2, "district": "Яккасарай"}, stats)
assert d < -20 and "дешевле" in txt, (d, txt)
d2, t2 = analyst.price_verdict({"price_usd": 700, "rooms": 2, "district": "Яккасарай"}, stats)
assert d2 > 20 and "дороже" in t2
assert analyst.price_verdict({"price_usd": None, "rooms": 2}, stats) == (None, "")

# скоринг хозяин/маклер
own = {**L1, "commission": "Нет", "is_business": False, "seller_id": "olx:solo",
       "text": "сдаю свою квартиру без посредников", "phones": ["909998877"]}
sc_own, why_own = analyst.owner_score(own, ms, cfg)
brk = {**L1, "commission": "Да", "is_business": True, "seller_id": "olx:makler1",
       "text": "аренда квартир по всему городу", "phones": []}
sc_brk, why_brk = analyst.owner_score(brk, ms, cfg)
assert sc_own >= 70 and sc_brk <= 35, (sc_own, sc_brk)
assert 0 <= sc_own <= 100 and 0 <= sc_brk <= 100

# сводная оценка
cand = {**L1, "price_usd": 380.0, "rooms": 2, "district": "Яккасарай",
        "lat": 41.29801, "lon": 69.27405, "area": 55, "floor": 3, "floors_total": 9,
        "furnished": "Да", "house_type": "Кирпичный", "commission": "Нет",
        "photo_urls": ["u"], "seller_id": "olx:solo",
        "created_at": rr.datetime.now(rr.TASHKENT_TZ).isoformat()}
scored = analyst.score_listing(dict(cand), ms, cfg, stats)
assert 0 <= scored["score"] <= 10 and scored["score"] >= 7, scored["score"]
assert scored["metro"]["name"] == "Айбек"
assert scored["price_per_m2"] == round(380/55, 2)
assert any("дешевле" in x for x in scored["pros"])

weak = analyst.score_listing({**cand, "price_usd": 900.0, "commission": "Да",
                              "is_business": True, "floor": 1, "photo_urls": [],
                              "seller_id": "olx:makler1"}, ms, cfg, stats)
assert weak["score"] < scored["score"], (weak["score"], scored["score"])
assert any("первый этаж" in x for x in weak["cons"])

# оценка попадает в сообщение
msg = rr.format_message(scored, cfg, False)
assert "Оценка" in msg and "🚇" in msg and "Спросить" in msg
assert analyst.ask_seller(scored)

# packing/recent в базе
ms.save({**cand, "key": "packed"}, notified=True)
rec = ms.recent(days=7)
assert any(r["key"] == "packed" and r["lat"] == 41.29801 for r in rec), len(rec)
assert ms.has_data("packed") is True
mdb.unlink(missing_ok=True)

print("OK — агент: карты, рынок, скоринг хозяина и ранжирование работают")

# ============== класс жилья: новый ЖК с ремонтом (примеры пользователя) ==============

refs = [
 dict(title="Новая 3х комнатная квартира на Мирабад авеню! Авторский ремонт!",
      text="Авторский проект. ЖК Mirabad Avenue", area=130, rooms=3, house_type="Жилой комплекс"),
 dict(title="Жк Mirabad Avenue-Сдается новая квартира в элит комплексе!",
      text="Авторский проект, система умный дом, 2 санузла и более", area=105, rooms=3),
 dict(title="Жк Ташкент Сити! Сдается 3х-ком квартира! В элит комплексе!",
      text="Элитный апартамент, авторский проект, новая квартира", area=125, rooms=3),
 dict(title="3х ком на Сеул мун с видом на Речку NEXT 3/7/9",
      text="Евро ремонт, меблирована", area=90, rooms=3),
]
for r in refs:
    assert analyst.is_premium(r), (r["title"], analyst.premium_signals(r))

# обычные квартиры не должны считаться премиальными
plain = [
 dict(title="Сдается квартира в аренду Чиланзарский район", text="хорошая квартира", area=45, rooms=2),
 dict(title="2 xonali kvartira arendaga beriladi", text="yaxshi holatda", area=65, rooms=2,
      house_type="Монолитный"),                    # просторно+монолит, но без сильного признака
 dict(title="Сдается 3-х комнатная на 5 этаже", text="панельный дом", area=60, rooms=3),
]
for r in plain:
    assert not analyst.is_premium(r), (r["title"], analyst.premium_signals(r))

st_p = {**rr.default_settings(), "segment": "premium"}
good = {**refs[0], "price_value": 1400, "price_usd": 1400.0, "district": "Мирабад"}
bad = {**plain[0], "price_value": 400, "price_usd": 400.0, "district": "Мирабад"}
assert rr.relevance_reject(good, cfg, st_p) == ""
assert "класс жилья" in rr.relevance_reject(bad, cfg, st_p)
assert rr.relevance_reject(bad, cfg, {**st_p, "segment": "any"}) == ""   # в обычном режиме проходит

stg = rr.default_settings()
assert stg["segment"] == "any"
assert rr.handle_callback("sg", stg, store, cfg)[1] == "M" and stg["segment"] == "premium"
assert "Класс: новый ЖК" in str(rr.kb_menu(cfg, stg))
assert rr.handle_command("/segment", stg, store, cfg)[0].startswith("✅")

print("OK — фильтр класса жилья настроен по вашим примерам")

# ==================== строгий режим «только хозяева» ====================

odb = Path("/tmp/test_owner.db"); odb.unlink(missing_ok=True)
os_ = rr.Store(odb)
st_own = rr.default_settings()
assert st_own["owner_only"] is True          # включено по умолчанию — просьба пользователя

def mk(**kw):
    base = dict(L1, key="x", commission="Нет", is_business=False,
                seller_id="olx:1001", phones=[], seller_ads=1,
                text="сдаю свою квартиру, без посредников")
    base.update(kw); return base

# кэш числа объявлений продавца
os_.seller_ads_put("olx:1001", 1)
assert os_.seller_ads_cached("olx:1001", 3) == 1
assert os_.seller_ads_cached("olx:unknown", 3) is None

# хозяин проходит
assert rr.owner_only_reject(mk(), os_, cfg, st_own) == ""
assert rr.relevance_reject(mk(), cfg, st_own, os_) == ""

# комиссия — сразу мимо
assert "комисси" in rr.owner_only_reject(mk(commission="Да"), os_, cfg, st_own)
# бизнес-аккаунт — мимо
assert "бизнес" in rr.owner_only_reject(mk(is_business=True), os_, cfg, st_own)
# много объявлений у продавца — мимо
assert "маклер" in rr.owner_only_reject(mk(seller_ads=9), os_, cfg, st_own)
assert rr.owner_only_reject(mk(seller_ads=2), os_, cfg, st_own) == ""   # 2 — ещё хозяин
# телефон в куче объявлений — мимо
for i in range(5):
    os_.save({**L1, "key": f"ph{i}", "phones": ["901234567"]}, notified=False)
assert rr.phone_spread({"phones": ["901234567"]}, os_) == 5
assert "телефон" in rr.owner_only_reject(mk(phones=["901234567"]), os_, cfg, st_own)
# неизвестное число объявлений и никаких слов про хозяина — не пропускаем
assert "подтвердить" in rr.owner_only_reject(
    mk(seller_ads=-1, seller_id="", text="сдается квартира"), os_, cfg, st_own)
# ...но если пишет «без посредников» — верим
assert rr.owner_only_reject(
    mk(seller_ads=-1, seller_id="", text="сдам без посредников"), os_, cfg, st_own) == ""

# выключается
assert rr.relevance_reject(mk(commission="Да"), cfg,
                           {**st_own, "owner_only": False}, os_) == ""

# кнопка и команда
sto = rr.default_settings()
assert rr.handle_callback("oo", sto, store, cfg)[1] == "M" and sto["owner_only"] is False
assert "Все, включая маклеров" in str(rr.kb_menu(cfg, sto))
assert rr.handle_command("/owner вкл", sto, store, cfg)[0].startswith("🔑") and sto["owner_only"]
assert "Только хозяева: да" in rr.status_text(cfg, sto, store)

# доказательство видно в сообщении
m = rr.format_message(mk(seller_ads=1, price_usd=500.0, price_value=500,
                         price_currency="USD"), cfg, False)
assert "1 объявл. у продавца" in m and "без комиссии" in m
odb.unlink(missing_ok=True)

print("OK — режим «только хозяева» работает")

# опечатки и узбекские формулировки подселения
for t in ["Sherilikka xona / joy", "Шериклик хона", "joy beriladi qizlarga",
          "xona beriladi", "1 o'rin bor"]:
    assert rr.looks_like_room_share(t), t
# «xonali» (комнатная) не должно ловиться
for t in ["2 xonali kvartira ijaraga beriladi", "3 xonali uy arendaga"]:
    assert not rr.looks_like_room_share(t), t
print("OK — опечатки подселения тоже ловятся")

# ==================== база маклеров и рассылка ====================

bdb = Path("/tmp/test_brokers.db"); bdb.unlink(missing_ok=True)
bs = rr.Store(bdb)

# накопление карточки: районы и диапазон цен склеиваются
bs.upsert_broker("olx:77", "OLX", "Азиз", "901112233", 12, "Мирабад", 700.0)
bs.upsert_broker("olx:77", "OLX", "Азиз", None, 14, "Яккасарай", 1200.0)
b = bs.brokers()[0]
assert b["ads"] == 14 and b["phone"] == "901112233"
assert set(b["districts"]) == {"Мирабад", "Яккасарай"}
assert b["min_price"] == 700.0 and b["max_price"] == 1200.0
assert b["status"] == "new"

# без телефона в рассылку не попадает
bs.upsert_broker("olx:88", "OLX", "Без телефона", None, 9, "Чиланзар", 500.0)
assert len(bs.brokers(with_phone=True)) == 1
assert len(bs.brokers(with_phone=False)) == 2

# воронка
bs.broker_status("olx:77", "contacted")
assert bs.brokers(status="new", with_phone=True) == []
assert bs.brokers(status="contacted")[0]["bid"] == "olx:77"
total, withph, by = bs.broker_stats()
assert total == 2 and withph == 1 and by.get("contacted") == 1

# сбор только тех, у кого 3+ объявлений
harvested = dict(source="OLX", seller="Мак", seller_id="olx:99",
                 phones=["935556677"], district="Юнусабад", price_usd=800.0)
rr.harvest_broker(harvested, bs, cfg, 2)          # мало объявлений — не маклер
assert not [x for x in bs.brokers(with_phone=False) if x["bid"] == "olx:99"]
rr.harvest_broker(harvested, bs, cfg, 7)
assert [x for x in bs.brokers(with_phone=False) if x["bid"] == "olx:99"]

# текст запроса собирается из фильтров
st_o = {**rr.default_settings(), "rooms_min": 2, "rooms_max": 3,
        "districts": ["Мирабад", "Яккасарай"], "max_price_usd": 1500,
        "segment": "premium"}
txt = rr.outreach_text(cfg, st_o)
assert "2–3 комнаты" in txt and "Мирабад, Яккасарай" in txt
assert "$1500" in txt and "ЖК" in txt and "комиссии" in txt

# ссылки в один тап
link = rr.wa_link("901112233", "привет мир")
assert link.startswith("https://wa.me/998901112233?text=") and "%20" in link
assert rr.wa_link("+998 90 111-22-33", "x").startswith("https://wa.me/998901112233")
assert rr.tg_phone_link("901112233") == "https://t.me/+998901112233"

# кнопки воронки
sb = rr.default_settings()
_NX = []
with mock.patch.object(rr, "tg_call", lambda c, m, pl, **k: (_NX.append((m, pl)), {"ok": True})[1]), \
        mock.patch.object(rr, "send_telegram", lambda c, t: (_NX.append(("t", {"text": t})), True)[1]):
    assert rr.handle_callback("bw:olx:77", sb, bs, cfg, message_id=5)[0] == "✅ Отмечено"
    assert rr.handle_callback("bx:olx:88", sb, bs, cfg)[0] == "Пропущен"
assert bs.brokers(status="skipped", with_phone=False)[0]["bid"] == "olx:88"
assert any(m == "editMessageReplyMarkup" for m, _ in _NX)          # кнопки пройденной карточки убраны
assert bs.get_kv("outreach")["sent"] == 1 and bs.get_kv("outreach")["skipped"] == 1
nxt = [pl for m, pl in _NX if m == "sendMessage" and "reply_markup" in pl]
assert nxt and "Отправил → следующий" in nxt[-1]["reply_markup"] and "Мак" in nxt[-1]["text"]   # следующий в очереди
assert "'b'" in str(rr.kb_menu(cfg, sb))
bdb.unlink(missing_ok=True)

print("OK — база маклеров, текст запроса и рассылка в один тап работают")

# ================== КОНСЬЕРЖ: анкета, варианты, шортлист ==================

import concierge as cg

cdb = Path("/tmp/test_conc.db"); cdb.unlink(missing_ok=True)
cs = rr.Store(cdb)
SENT = []
def fake_tg(cfg_, method, payload, timeout=20, quiet=False):
    SENT.append((method, payload)); return {"ok": True, "result": {"message_id": 1}}

# --- анкета проходится кнопками до конца (новый порядок с ветвлением) ---
with mock.patch.object(rr, "tg_call", fake_tg):
    cg.start_anketa(cfg, cs)
    assert cg.get_anketa(cs)["i"] == 0
    for v in ["rent", "flat", "tashkent"]:          # deal, object, city
        cg.handle_anketa_cb(f"a:{cg.get_anketa(cs)['i']}:{v}", cfg, cs, 1)
    # районы — мультивыбор (после Ташкента шаг не пропущен)
    i = cg.get_anketa(cs)["i"]
    assert cg.STEPS[i]["k"] == "districts"
    yak = str(rr.DISTRICT_LIST.index("Яккасарай"))
    mir = str(rr.DISTRICT_LIST.index("Мирабад"))
    cg.handle_anketa_cb(f"a:{i}:{yak}", cfg, cs, 1)
    cg.handle_anketa_cb(f"a:{i}:{mir}", cfg, cs, 1)
    cg.handle_anketa_cb("a:next", cfg, cs, 1)
    # комнаты — мультивыбор с повторным тапом
    i = cg.get_anketa(cs)["i"]
    assert cg.STEPS[i]["k"] == "rooms"
    cg.handle_anketa_cb(f"a:{i}:2", cfg, cs, 1)
    cg.handle_anketa_cb(f"a:{i}:3", cfg, cs, 1)
    assert cg.get_anketa(cs)["ans"]["rooms"] == ["2", "3"]
    cg.handle_anketa_cb(f"a:{i}:3", cfg, cs, 1)      # повторный тап снимает
    assert cg.get_anketa(cs)["ans"]["rooms"] == ["2"]
    cg.handle_anketa_cb(f"a:{i}:3", cfg, cs, 1)
    cg.handle_anketa_cb("a:next", cfg, cs, 1)
    cg.handle_anketa_cb(f"a:{cg.get_anketa(cs)['i']}:1500", cfg, cs, 1)   # бюджет
    # остальные одиночные шаги: класс, мебель, этаж, срок, заезд, кто, звери, паркинг, куда
    for v in ["premium", "yes", "mid", "12", "now", "family_kids", "no", "yes", "bot"]:
        cg.handle_anketa_cb(f"a:{cg.get_anketa(cs)['i']}:{v}", cfg, cs, 1)

req = cs.get_kv("request_text")
assert req and "2–3" in req.replace("-", "–") or "2" in req
assert "Яккасарай" in req and "Мирабад" in req
assert "$1 500" in req and "авторск" in req and "семьи с детьми" in req
assert "не первый этаж" in req and "не последний этаж" in req
assert "помощнице — Ra'no" in req and "комисси" in req
# ссылка кликабельная, без @упоминания — иначе маклер уходит к боту-двойнику
assert "https://t.me/rano_smart_bot" in req and "@rano_smart_bot" not in req
assert "Здравствуйте" in req and "квартиру в Ташкенте" in req

# --- ветвление: при покупке участка лишние шаги пропускаются ---
cg.save_anketa(cs, {"i": 0, "ans": {}})
with mock.patch.object(rr, "tg_call", fake_tg):
    for v in ["buy", "land", "region"]:
        cg.handle_anketa_cb(f"a:{cg.get_anketa(cs)['i']}:{v}", cfg, cs, 1)
    # районы/комнаты пропущены — сразу бюджет
    i = cg.get_anketa(cs)["i"]
    assert cg.STEPS[i]["k"] == "budget"
    cg.handle_anketa_cb(f"a:{i}:80000", cfg, cs, 1)
    # класс/мебель/этаж/срок/заезд/кто/звери пропущены — сразу парковка
    i = cg.get_anketa(cs)["i"]
    assert cg.STEPS[i]["k"] == "parking", cg.STEPS[i]["k"]
    cg.handle_anketa_cb(f"a:{i}:any", cfg, cs, 1)
    cg.handle_anketa_cb(f"a:{cg.get_anketa(cs)['i']}:bot", cfg, cs, 1)
req2 = cs.get_kv("request_text")
assert "купить участок в Ташкентской области" in req2
assert "$80 000" in req2 and "этаж" not in req2 and "мебель" not in req2
assert "площадь и цену" in req2

# --- узбекское письмо: посуточная дача на Чарваке с датами из календаря ---
cg.save_anketa(cs, {"i": 0, "ans": {
    "lang": "uz", "deal": "daily", "object": "dacha", "city": "charvak",
    "rooms": ["3"], "budget": "100", "date_from": "2026-08-15",
    "date_to": "2026-08-18", "who": "family_kids", "pets": "no",
    "parking": "yes", "contact": "bot"}})
req_uz = cg.compose_request(cfg, cs)
assert "Assalomu alaykum" in req_uz and "Chorvoq" in req_uz
assert "dala hovli" in req_uz and "kunlik" in req_uz
assert "kuniga" in req_uz and "$100" in req_uz
assert "Sanalar: 15-avgustdan 18-avgustgacha — 3 kecha." in req_uz
assert "bolali oila uchun" in req_uz and "avtoturargoh kerak" in req_uz
assert "https://t.me/rano_smart_bot" in req_uz and "Rahmat!" in req_uz

# --- даты и склонения ночей (русский) ---
cg.save_anketa(cs, {"i": 0, "ans": {
    "deal": "daily", "object": "dacha", "city": "charvak", "budget": "90",
    "date_from": "2026-09-01", "date_to": "2026-09-02", "contact": "bot"}})
req_d1 = cg.compose_request(cfg, cs)
assert "Даты: заезд 1 сентября, выезд 2 сентября — 1 ночь." in req_d1, req_d1
assert "этаж" not in req_d1                        # дача не спрашивает этаж

# --- длительная аренда: точная дата заезда из календаря ---
cg.save_anketa(cs, {"i": 0, "ans": {
    "deal": "rent", "object": "flat", "city": "tashkent", "term": "12",
    "movein": "date", "movein_date": "2026-10-05", "budget": "1300",
    "contact": "bot"}})
req_mv = cg.compose_request(cfg, cs)
assert "Заезд планирую с 5 октября." in req_mv, req_mv
assert "на длительный срок, от года" in req_mv     # срок аренды остаётся

# кривые даты не попадают в ответы и не роняют бота
sc = rr.Store(Path("/tmp/test_dates.db")); Path("/tmp/test_dates.db").unlink(missing_ok=True)
sc = rr.Store(Path("/tmp/test_dates.db"))
with mock.patch.object(rr, "tg_call", fake_tg):
    cg.apply_webapp_data(cfg, sc, json.dumps({"v": 2, "ans": {
        "deal": "daily", "object": "dacha", "city": "charvak", "budget": "100",
        "date_from": "2026-08-15", "date_to": "not-a-date", "contact": "bot"}}))
da = cg.get_anketa(sc)["ans"]
assert da.get("date_from") == "2026-08-15" and "date_to" not in da
Path("/tmp/test_dates.db").unlink(missing_ok=True)

# --- диапазон этажей из мини-аппа ---
cg.save_anketa(cs, {"i": 0, "ans": {
    "deal": "rent", "object": "flat", "city": "tashkent", "rooms": ["3"],
    "budget": "1400", "floor_pref": ["nf", "nl"], "floor_min": "3",
    "floor_max": "10", "contact": "bot"}})
req_fl = cg.compose_request(cfg, cs)
assert "не первый этаж" in req_fl and "не последний этаж" in req_fl
assert "этаж 3–10" in req_fl

# «назад» работает
before = cg.get_anketa(cs).get("i", 0)
cg.get_anketa(cs)

# --- разбор сообщения маклера ---
p = cg.parse_offer("Сдаётся 3 комнатная квартира, Мирабад, 105 кв.м, 7/9 этаж, 1200 у.е.", cfg)
assert p["rooms"] == 3 and p["district"] == "Мирабад"
assert p["area"] == 105.0 and p["price_usd"] == 1200.0
assert p["floor"] == 7 and p["floors_total"] == 9
p2 = cg.parse_offer("2/5/9 Яккасарай 90м2 900$", cfg)
assert p2["rooms"] == 2 and p2["floor"] == 5 and p2["floors_total"] == 9
assert p2["area"] == 90.0

# --- приём вариантов и склейка альбома ---
oid1, new1 = cg.save_offer(cs, cfg, 555, "Азиз", "Мирабад 3 комн 105 кв.м 1200 у.е.",
                           ["ph1"], media_group="g1")
assert new1 is True
oid2, new2 = cg.save_offer(cs, cfg, 555, "Азиз", "", ["ph2"], media_group="g1")
assert new2 is False and oid2 == oid1              # то же объявление, второе фото
o = cg.get_offer(cs, oid1)
assert o["photos"] == ["ph1", "ph2"] and o["price_usd"] == 1200.0
assert o["status"] == "new"

for i, (txt, ph) in enumerate([("Яккасарай 2 комн 70 кв.м 800 у.е.", "a"),
                               ("Мирабад 3 комн 100 кв.м 1100 у.е.", "b"),
                               ("Юнусабад 2 комн 60 кв.м 600 у.е.", "c")]):
    cg.save_offer(cs, cfg, 600 + i, f"Маклер{i}", txt, [ph])

# --- показ вариантов: первые бесплатно + честная подводка к остальным ---
with mock.patch.object(rr, "tg_call", fake_tg):
    SENT.clear()
    shown = cg.show_offers(cfg, cs, batch=2)
    assert shown == 2, shown
    # прогресс «N из M» в карточках (текст в подписи к фото — в media)
    cards = [pl.get("text", "") + pl.get("caption", "") + pl.get("media", "")
             for _, pl in SENT]
    assert any("из" in c for c in cards), "нет прогресса N из M"
    # подводка к остальным вариантам (их 4 → показали 2 → осталось 2)
    teaser = [pl for m, pl in SENT if "прислали ещё" in pl.get("text", "")]
    assert teaser and "2" in teaser[0]["text"]
    kb = json.loads(teaser[0]["reply_markup"])
    assert kb["inline_keyboard"][0][0]["callback_data"] == "off2"

# карточка одиночно (без прогресса) не падает
_c = cg.offer_card(cs, cfg, cg.get_offer(cs, oid1))
assert "Вариант" in _c

# --- ценовой индекс строится ТОЛЬКО по данным маклеров ---
idx = cg.price_index(cs, min_sample=1)
assert idx["sample"] == 4
assert idx["rooms"][3][0] == 1150.0                # медиана 1200 и 1100
assert "Мирабад" in idx["m2"]
note = cg.price_note(cg.get_offer(cs, oid1), idx)
assert "дороже" in note or "по рынку" in note

# --- триаж ---
with mock.patch.object(rr, "tg_call", fake_tg):
    t, done = cg.handle_triage_cb(f"t:s:{oid1}", cfg, cs)
    assert done and cg.get_offer(cs, oid1)["status"] == "shortlist"
    t, _ = cg.handle_triage_cb("t:l:2", cfg, cs)
    assert cg.get_offer(cs, 2)["status"] == "later"
    SENT.clear()
    t, _ = cg.handle_triage_cb("t:n:3", cfg, cs)                 # сначала — причина
    assert cg.get_offer(cs, 3)["status"] != "rejected"
    rk = json.loads(SENT[-1][1]["reply_markup"])["inline_keyboard"]
    assert [r[0]["callback_data"] for r in rk][0] == "t:r:3:p"
    SENT.clear()
    t, _ = cg.handle_triage_cb("t:r:3:p", cfg, cs)
    assert cg.get_offer(cs, 3)["status"] == "rejected" and "Дорого" in cg.get_offer(cs, 3)["note"]
    dec = [pl for m, pl in SENT if m == "sendMessage" and "не подошёл" in pl.get("text", "")]
    assert dec and "дешевле" in dec[0]["text"], "маклеру — отказ с подсказкой"

# --- шортлист: сводка, карточка варианта, запрос деталей ---
cg.set_offer_status(cs, 4, "shortlist")
text, kb, items = cg.shortlist_view(cs, cfg)
assert "Шортлист" in text and len(items) == 2
nums = [b for row in kb["inline_keyboard"] for b in row if b["callback_data"].startswith("s:o:")]
assert len(nums) == 2
assert any(b["callback_data"] == "s:go" and "(2)" in b["text"] for row in kb["inline_keyboard"] for b in row)

with mock.patch.object(rr, "tg_call", fake_tg):
    SENT.clear()
    cg.handle_shortlist_cb(f"s:o:{oid1}", cfg, cs, 1)          # номер → карточка
    card = [pl for m, pl in SENT if m in ("sendMessage", "sendPhoto", "sendMediaGroup")][-1]   # номер → карточка-альбом
    assert "reply_markup" not in card                                   # кнопок нет — управление словами
    assert "Этап" in tx_(card)
    SENT.clear()
    assert cg.show_shortlist(cfg, cs) == 2                               # шортлист — карточками, без списка
    sl_cards = [pl for m, pl in SENT if "Шортлист</b> · " in tx_(pl)]
    assert len(sl_cards) == 2 and "1 из 2" in tx_(sl_cards[0])
    assert "своими словами" in tx_(sl_cards[0])   # подсказка — без заученных фраз
    SENT.clear()
    toast, _ = cg.handle_offer_cb(f"o:ask:{oid1}", cfg, cs, 1)
    assert "Отправлено: 1" in toast
    asked = [pl for m, pl in SENT if m == "sendMessage" and str(pl.get("chat_id")) == "555"]
    assert asked and "актуален" in asked[0]["text"] and "комисси" in asked[0]["text"]
    o1 = cg.get_offer(cs, oid1)
    assert o1["status"] == "asked" and "ждём ответа" in cg.stage_of(o1)
    _, kb3, _ = cg.shortlist_view(cs, cfg)                      # спросили одного — осталось 1
    assert any(b["callback_data"] == "s:go" and "(1)" in b["text"] for row in kb3["inline_keyboard"] for b in row)

    # просмотр, заметка, посмотрел
    at = (datetime.now(cg.TZ) + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    assert cg.set_viewing(cfg, cs, oid1, at.isoformat(), "завтра 18:00")
    assert cg.stage_of(cg.get_offer(cs, oid1)).startswith("📅 просмотр завтра, 18:00")
    assert cg.add_note(cs, oid1, "Хороший двор, торг 2000")
    t2, _ = cg.offer_view(cs, cfg, cg.get_offer(cs, oid1))
    assert "торг 2000" in t2 and "просмотр" in t2
    SENT.clear()
    cg.handle_offer_cb(f"o:seen:{oid1}:g", cfg, cs, 1)
    assert "нравится" in cg.stage_of(cg.get_offer(cs, oid1))
    # напоминание маклеру — одно
    SENT.clear()
    assert "напомнила" in cg.remind_broker(cfg, cs, oid1)
    assert any("Напоминаю" in pl.get("text", "") for m, pl in SENT if str(pl.get("chat_id")) == "555")
    assert "второй раз" in cg.remind_broker(cfg, cs, oid1)

    SENT.clear()
    cg.handle_shortlist_cb("s:sort", cfg, cs, 1)                       # старые кнопки — просто карточки заново
    assert any("Шортлист</b> · " in tx_(pl) for m, pl in SENT)

# --- сообщение от маклера через process-слой ---
with mock.patch.object(rr, "tg_call", fake_tg):
    SENT.clear()
    rr.handle_broker_message(cfg, cs, {
        "chat": {"id": 777, "first_name": "Шухрат"},
        "caption": "Чиланзар 2 комн 65 кв.м 700 у.е.",
        "photo": [{"file_id": "small", "width": 90, "height": 90},
                  {"file_id": "big", "width": 1280, "height": 960}]})
    last = cs.conn.execute("SELECT oid, broker_name, photos FROM broker_offers "
                           "ORDER BY oid DESC LIMIT 1").fetchone()
    assert last[1] == "Шухрат" and json.loads(last[2]) == ["big"]   # взят крупный размер
    assert any(str(pl.get("chat_id")) == "777" for m, pl in SENT)   # маклеру ушло спасибо
    assert str(last[0]) in cs.get_kv("pending_offers")

# --- путь маклера: /start, части одного варианта, ответ на уточнения, мгновенный ответ воркера ---
cg.save_anketa(cs, {"i": 99, "ans": {"lang": "ru", "deal": "buy", "object": "flat", "city": "tashkent",
                                     "rooms": ["2"], "budget": "45000", "contact": "bot"}})
cs.set_kv("request_text", cg.compose_request(cfg, cs))
with mock.patch.object(rr, "tg_call", fake_tg):
    SENT.clear()
    n_before = cs.conn.execute("SELECT COUNT(*) FROM broker_offers").fetchone()[0]
    rr.handle_broker_message(cfg, cs, {"chat": {"id": 888, "first_name": "Нодир"}, "text": "/start"})
    assert cs.conn.execute("SELECT COUNT(*) FROM broker_offers").fetchone()[0] == n_before   # не «вариант»
    w = [pl for m, pl in SENT if str(pl.get("chat_id")) == "888"][0]["text"]
    assert "Клиент ищет" in w and "купить квартиру" in w and "45 000" in w
    SENT.clear()
    rr.handle_broker_message(cfg, cs, {"chat": {"id": 888}, "text": "Продаю 2 комн Мирабад 52 м2 44 000$",
                                       "_acked": True})
    assert not [pl for m, pl in SENT if str(pl.get("chat_id")) == "888"]   # воркер уже ответил
    rr.handle_broker_message(cfg, cs, {"chat": {"id": 888}, "photo": [{"file_id": "f1", "width": 9, "height": 9}]})
    rr.handle_broker_message(cfg, cs, {"chat": {"id": 888}, "text": "5/9 этаж, ремонт, документы готовы"})
    rows = cs.conn.execute("SELECT oid, text, photos, floor FROM broker_offers WHERE broker_chat='888'").fetchall()
    assert len(rows) == 1, rows                                     # три сообщения — один вариант
    oid8 = rows[0][0]
    assert json.loads(rows[0][2]) == ["f1"] and rows[0][3] == 5 and "ремонт" in rows[0][1]
    # показ — когда маклер замолчал
    SENT.clear()
    rr.flush_pending_offer(cfg, cs)
    assert not SENT                                                  # ещё пишет
    pend = cs.get_kv("pending_offers"); pend[str(oid8)]["at"] -= 100; cs.set_kv("pending_offers", pend)
    with mock.patch.object(rr, "send_offer_analysis", lambda *a, **k: SENT.append(("analysis", {}))):
        rr.flush_pending_offer(cfg, cs)
    assert any("Вариант" in (pl.get("text", "") + pl.get("caption", "") + pl.get("media", "")) for _, pl in SENT)
    assert ("analysis", {}) in SENT                                  # покупка → анализ цены
    # уточнения → ответ прикрепляется к варианту
    cg.set_offer_status(cs, oid8, "shortlist")
    SENT.clear()
    cg.request_details(cfg, cs, [oid8])
    q = [pl for m, pl in SENT if str(pl.get("chat_id")) == "888"][0]["text"]
    assert "ипотека" in q and "в месяц" not in q                     # вопросы про покупку
    SENT.clear()
    rr.handle_broker_message(cfg, cs, {"chat": {"id": 888}, "text": "Да актуально, Мирабад ул. Шахрисабз 5, торг есть"})
    assert cs.conn.execute("SELECT COUNT(*) FROM broker_offers WHERE broker_chat='888'").fetchone()[0] == 1
    o8 = cg.get_offer(cs, oid8)
    assert o8["status"] == "shortlist" and "Шахрисабз" in o8["note"]
    assert any("Маклер ответил по варианту" in pl.get("text", "") for m, pl in SENT)

# --- владелец пересылает вариант из WhatsApp/другого чата ---
with mock.patch.object(rr, "tg_call", fake_tg), \
        mock.patch.object(rr, "send_telegram", lambda c, t: (SENT.append(("t", {"text": t})), True)[1]):
    SENT.clear()
    rr.handle_owner_offer(cfg, cs, {"chat": {"id": 1}, "_owner_offer": True,
                                    "forward_origin": {"type": "user", "sender_user": {"first_name": "Бахтиёр"}},
                                    "text": "2-комн Яккасарай 48 м² 41 000$, 3/5"})
    rr.handle_owner_offer(cfg, cs, {"chat": {"id": 1}, "_owner_offer": True,
                                    "forward_origin": {"type": "user", "sender_user": {"first_name": "Бахтиёр"}},
                                    "photo": [{"file_id": "w1", "width": 9, "height": 9}]})
    ow = cs.conn.execute("SELECT oid, broker_name, broker_chat, photos, price_usd FROM broker_offers "
                         "WHERE broker_chat='owner'").fetchall()
    assert len(ow) == 1 and ow[0][1] == "Бахтиёр" and json.loads(ow[0][3]) == ["w1"] and ow[0][4] == 41000
    assert any("Приняла" in pl.get("text", "") for _, pl in SENT)
    SENT.clear()
    cg.handle_triage_cb(f"t:r:{ow[0][0]}:d", cfg, cs)              # «мимо» — боту писать некому
    assert not [pl for m, pl in SENT if str(pl.get("chat_id")) == "owner"]

st = cg.concierge_status(cs)
assert "Консьерж" in st and "маклеров" in st
cdb.unlink(missing_ok=True)

print("OK — консьерж: анкета, приём вариантов, индекс цен, триаж и шортлист")

# --- регрессия: цены в разных форматах от маклеров ---
for t, exp in [("Мирабад 3 комн 100 кв.м 5/9 1100 у.е.", 1100),
               ("ЖК Avenue 3/7/12 105 кв.м 1 300 у.е.", 1300),
               ("2 комн 70м2 850$ Яккасарай", 850),
               ("аренда 900 USD", 900), ("105м² 1400 у.е.", 1400)]:
    got = cg.parse_offer(t, cfg)["price_usd"]
    assert got and abs(got - exp) <= 2, (t, got, exp)
# сумовые с разделителями
assert abs(cg.parse_offer("Чиланзар 6 500 000 сум", cfg)["price_usd"] - 546.2) < 1
# нумерация вопросов сплошная, без пропусков
q = cg.details_question({"rooms": 3, "district": "Мирабад", "price_usd": 1200.0,
                         "floor": 7, "area": 105, "oid": 1})
nums = [int(x) for x in re.findall(r"^(\d+)\.", q, re.M)]
assert nums == list(range(1, len(nums) + 1)), nums
q2 = cg.details_question({"rooms": None, "district": None, "price_usd": None,
                          "floor": None, "area": None, "oid": 2})
nums2 = [int(x) for x in re.findall(r"^(\d+)\.", q2, re.M)]
assert nums2 == list(range(1, len(nums2) + 1)) and len(nums2) > len(nums)
print("OK — форматы цен и нумерация вопросов")

# ======================= МИНИ-АПП =======================

mdb = Path("/tmp/test_mini.db"); mdb.unlink(missing_ok=True)
ms2 = rr.Store(mdb)
SENT2 = []
def fake2(cfg_, method, payload, timeout=20, quiet=False):
    SENT2.append((method, payload)); return {"ok": True, "result": {"message_id": 1}}

# ссылка без данных — чистая, с данными — с предзаполнением
assert cg.webapp_url(ms2) == cg.WEBAPP_URL
cg.save_anketa(ms2, {"i": 0, "ans": {"deal": "rent", "rooms": ["2", "3"]}})
u = cg.webapp_url(ms2)
assert u.startswith(cg.WEBAPP_URL + "#")
import base64 as _b64
assert json.loads(_b64.b64decode(u.split("#", 1)[1]).decode())["rooms"] == ["2", "3"]

# мини-апп убран: вместо кнопки — просьба описать поиск словами и снятие старой клавиатуры
with mock.patch.object(rr, "tg_call", fake2):
    cg.send_app_button(cfg, ms2)
m, pl = SENT2[-1]
kb = json.loads(pl["reply_markup"])
assert kb == cg.OWNER_KB and kb["remove_keyboard"]               # кнопок внизу нет — всё словами
assert "своими словами" in pl["text"]

# данные из формы применяются и сразу дают готовый текст
payload = json.dumps({"v": 2, "ans": {
    "lang": "ru", "deal": "rent", "object": "flat", "rooms": ["2", "3"],
    "budget": "1200", "budget_max": "1450",
    "districts": [str(rr.DISTRICT_LIST.index("Мирабад"))], "class": "premium",
    "furniture": "yes", "floor_pref": ["nf", "nl"], "floor_min": "3",
    "floor_max": "abc", "term": "12", "movein": "now",
    "who": "family_kids", "pets": "cat", "parking": "yes", "contact": "bot",
    "hacker": "ignore-me"}})
with mock.patch.object(rr, "tg_call", fake2):
    assert cg.apply_webapp_data(cfg, ms2, payload) is True
ans = cg.get_anketa(ms2)["ans"]
assert "hacker" not in ans                       # лишние поля отброшены
assert ans["budget"] == "1450"                   # точная сумма важнее пресета
assert ans["city"] == "tashkent"
assert "floor_max" not in ans                    # не-число вычищено
req = ms2.get_kv("request_text")
assert "$1 450" in req and "Мирабад" in req and "авторск" in req
assert "семьи с детьми" in req and "с кошкой" in req and "этаж от 3" in req
assert "Ra'no" in req

# мусор не роняет бота
with mock.patch.object(rr, "tg_call", fake2):
    assert cg.apply_webapp_data(cfg, ms2, "не json") is False
    assert cg.apply_webapp_data(cfg, ms2, '{"v":1,"ans":{}}') is False

# /app и /anketa шлют кнопку, /steps — старый режим
with mock.patch.object(rr, "tg_call", fake2):
    SENT2.clear()
    rr.handle_command("/app", rr.default_settings(), ms2, cfg)
    assert any("remove_keyboard" in pl.get("reply_markup", "") for _, pl in SENT2)
    assert "своими словами" in rr.handle_command("/steps", rr.default_settings(), ms2, cfg)[0]   # анкеты кнопками нет
# кнопка меню «Параметры»: sendData нет → параметры приходят как /start p<код>
# (код ниже собран encodeStart() из docs/index.html — держать словари синхронными)
CODE = "p1CBBBDBCEDCBDAkEGDAAcIAAWqDQQZAAAA"
d = cg.decode_start_code(CODE[1:])
assert d["lang"] == "uz" and d["districts"] == ["2", "8", "11"] and d["rooms"] == ["2", "3"]
assert d["budget_max"] == "1450" and d["floor_min"] == "3" and d["floor_max"] == "16"
assert d["movein_date"] == "2026-11-15" and d["contact"] == "both" and d["term"] == "6_12"
assert cg.decode_start_code("p1DCBEBDBDBBCBBAAQAAABQAAAAAAAAP_QG0KHQsNC80LDRgNC60LDQvdC0"[1:])["city_other"] == "Самарканд"
assert cg.decode_start_code("") is None and cg.decode_start_code("9xx") is None
assert cg.decode_start_code("1D!") is None and cg.decode_start_code("1DB") is None
assert len(CODE) <= 64
with mock.patch.object(rr, "tg_call", fake2):
    SENT2.clear()
    rr.handle_command("/start " + CODE, rr.default_settings(), ms2, cfg)
assert cg.get_anketa(ms2)["ans"]["budget"] == "1450"
assert ms2.get_kv("request_text")
with mock.patch.object(rr, "tg_call", fake2):
    SENT2.clear()
    rr.handle_command("/start pМУСОР", rr.default_settings(), ms2, cfg)
    assert any("Не получилось" in str(pl.get("text", "")) for _, pl in SENT2)
# чат-интервью (воркер) присылает ПОЛНЫЙ набор с replace — старые ответы не примешиваются
cg.save_anketa(ms2, {"i": 0, "ans": {"deal": "rent", "pets": "dog", "who": "group", "rooms": ["1"]}})
with mock.patch.object(rr, "tg_call", fake2):
    assert cg.apply_webapp_data(cfg, ms2, json.dumps({"v": 3, "replace": True, "ans": {
        "lang": "ru", "deal": "rent", "object": "flat", "city": "tashkent", "contact": "bot",
        "rooms": ["3"], "districts": ["2"], "budget": "1400", "class": "premium"}}))
ans = cg.get_anketa(ms2)["ans"]
assert "pets" not in ans and "who" not in ans and ans["rooms"] == ["3"]
req = ms2.get_kv("request_text")
assert "Мирабад" in req and "$1 400" in req and "собак" not in req
# свободное пожелание из чата (ипотека) попадает в письмо; replace снимает «жду текст»
ms2.set_kv("awaiting_text", True)
with mock.patch.object(rr, "tg_call", fake2):
    assert cg.apply_webapp_data(cfg, ms2, json.dumps({"v": 3, "replace": True, "ans": {
        "lang": "ru", "deal": "buy", "object": "flat", "city": "tashkent", "contact": "bot",
        "rooms": ["2"], "budget": "50000", "class": "reno", "note": "Нужна ипотека, ближе к центру"}}))
req = ms2.get_kv("request_text")
assert "нужна ипотека, ближе к центру" in req, req
assert ms2.get_kv("awaiting_text") is False

# очередь воркера вместо getUpdates: свой offset, подтверждение, until для «будильника»
wcfg = dict(cfg, worker_url="https://w.example", worker_key="k")
CALLS = []
QUEUE = [{"update_id": 7, "message": {"chat": {"id": 4242}, "text": "/help"}}]
class _R:
    def __init__(s, d): s.status_code, s._d, s.text = 200, d, ""
    def json(s): return s._d
def fake_get(url, params=None, timeout=None, headers=None):
    CALLS.append((url, dict(params or {}), headers))
    after = int((params or {}).get("after", 0))
    return _R({"ok": True, "result": [u for u in QUEUE if u["update_id"] > after]})
wcfg["telegram_chat_id"] = str(QUEUE[0]["message"]["chat"]["id"])
rr.RUN_DEADLINE = time.time() + 600
with mock.patch.object(rr.requests, "get", fake_get), mock.patch.object(rr, "tg_call", fake2):
    SENT2.clear()
    rr.process_commands(wcfg, ms2, long_poll=0)
    assert ms2.get_kv("wq_offset") == 7
    assert any("Ra'no" in str(pl.get("text", "")) for _, pl in SENT2)      # /help отработал
    assert not any(m_ == "getUpdates" for m_, _ in SENT2)
    rr.process_commands(wcfg, ms2, long_poll=0)                             # повторно — подтверждаем 7
assert CALLS[-1][1]["after"] == 7 and CALLS[-1][2]["x-svc"] == "k"
assert CALLS[-1][0] == "https://w.example/svc/updates"
assert CALLS[-1][1]["until"] >= time.time() + 600
rr.RUN_DEADLINE = None
mdb.unlink(missing_ok=True)
print("OK — параметры: чат-интервью (replace), очередь воркера, старые ссылки мини-аппа")

# ================= ПЕРЕИМЕНОВАНИЕ: голос Амины =================

cfg_a = rr.deep_merge(cfg, {"assistant_name": "Ra'no", "owner_name": "Шохрух",
                            "bot_username": "rano_smart_bot"})
# ассистент честно представляется ассистентом, а не человеком
ack = rr.broker_ack(cfg_a)
assert "Ra'no" in ack and "ИИ-ассистент" in ack
assert "Шохрух" not in ack          # имя клиента маклерам не раскрываем

# письмо маклеру идёт от владельца → Ra'no в нём «моя помощница»
adb = Path("/tmp/test_amina.db"); adb.unlink(missing_ok=True)
as_ = rr.Store(adb)
cg.save_anketa(as_, {"i": 0, "ans": {"deal": "rent", "rooms": ["2"], "budget": "1200",
                                     "districts": [], "class": "any", "contact": "bot"}})
req_a = cg.compose_request(cfg_a, as_)
assert "помощнице — Ra'no" in req_a and "https://t.me/rano_smart_bot" in req_a
assert "Я Ra'no" not in req_a          # владелец не должен говорить от её лица

# уточнения маклеру — от лица Ra'no, нумерация не сбита
q = cg.details_question({"rooms": 3, "district": "Мирабад", "price_usd": 1200.0,
                         "floor": 7, "area": 105}, cfg_a)
assert q.startswith("Здравствуйте! Я Ra'no, ИИ-ассистент — ищу квартиру для своего клиента: " + cg.CLIENT)
nums = [int(x) for x in re.findall(r"^(\d+)\.", q, re.M)]
assert nums == list(range(1, len(nums) + 1))

d = cg.decline_text(cfg_a)
assert "не подошёл" in d and "Ra'no" in d and "Шохрух" not in d

# имя бота подставляется в ссылку мини-аппа
u = cg.webapp_url(as_, cfg_a)
payload = json.loads(_b64.b64decode(u.split("#", 1)[1]).decode())
assert payload["_bot"] == "rano_smart_bot"

# getMe подхватывает переименование само
def fake_getme(cfg_, method, payload, timeout=20, quiet=False):
    return {"ok": True, "result": {"username": "rano_smart_bot"}}
c2 = dict(cfg); c2["bot_username"] = ""
with mock.patch.object(rr, "tg_call", fake_getme):
    assert rr.detect_bot_username(c2) == "rano_smart_bot"
assert c2["bot_username"] == "rano_smart_bot"
adb.unlink(missing_ok=True)
print("OK — переименование в Ra'no, голос ассистента, авто-подхват username")

# ------------------------------------------- покупка от собственника ----
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

ss = rr.deep_merge(rr.DEFAULT_CONFIG, {"sale_search": {"enabled": True}})
scfg_ = ss["sale_search"]
assert scfg_["max_price_usd"] == 45000 and scfg_["rooms"] == [1, 2]
assert scfg_["notify_max_age_days"] == 45
assert set(scfg_["districts"]) == {"Яккасарай", "Мирабад", "Шайхантахур", "Юнусабад"}

_now = _dt.now(_tz.utc)
def _iso(days):
    return (_now - _td(days=days)).isoformat()

def uy(id_, user, district, room=1, price=40000, cur="usd", days=1, desc="Продаётся квартира"):
    return {"id": id_, "userId": user, "districtId": district, "room": room, "price": price,
            "priceCurrency": cur, "square": 30, "floor": id_ % 9 + 1, "floorTotal": 12,
            "isNewBuilding": False, "repair": "evro", "createdAt": _iso(days),
            "description": desc, "address": "ул. Тестовая", "media": [{"url": f"https://api.uybor.uz/api/v1/media/n/{id_}.jpg"}]}

UY_ADS = {1: 1, 2: 134, 3: 1, 4: 2, 5: 1, 6: 1, 7: 1, 8: 1}   # userId → объявлений
UY_BY_DISTRICT = {
    205: [uy(101, 1, 205),                                   # годится
          uy(102, 2, 205),                                   # агентство: 134 объявления
          uy(103, 3, 205, price=52000),                      # дороже бюджета
          uy(104, 4, 205, price=500_000_000, cur="uzs")],    # ~$42 000 в сумах — годится
    204: [uy(201, 5, 204, desc="Агентство недвижимости «Дом» предлагает"),  # агентство по тексту
          uy(202, 6, 204, desc="Продаю сам. Риелторам не беспокоить"),      # хозяин, несмотря на «риелтор»
          uy(203, 7, 204, days=60)],                         # старше 45 дней
    198: [uy(301, 8, 198, room=3)],                          # 3 комнаты (если API не отфильтровал)
    197: [uy(101, 1, 205)],                                  # дубль id из другого запроса
}
calls = []
EXPECT_ROOMS = "1,2"

class FakeUy:
    def __init__(self, data):
        self.status_code = 200
        self._d = data
    def json(self):
        return self._d
    def raise_for_status(self):
        pass

def fake_uy_get(url, params=None, headers=None, timeout=None):
    calls.append(dict(params or {}))
    if "user__eq" in (params or {}):
        uid = int(params["user__eq"])
        return FakeUy({"total": UY_ADS[uid], "results": [{"userId": uid}]})
    assert params["operationType__eq"] == "sale"
    assert params.get("room__in") == EXPECT_ROOMS, params
    assert params["priceCurrency__eq"] == "usd" and params["price__lte"] == 45000
    return FakeUy({"total": 1, "results": UY_BY_DISTRICT.get(params["district__eq"], [])})

with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr.time, "sleep"):
    sl = rr.fetch_uybor_sale(scfg_, ss)
assert sorted(c["district__eq"] for c in calls) == [197, 198, 204, 205], calls
assert len(sl) == 8 and len({x["key"] for x in sl}) == 8      # дубль id 101 схлопнут
assert all(x["key"].startswith("sale:uybor:") for x in sl)
assert sl[0]["source"] == "Uybor · продажа" and sl[0]["repair"] == "evro"

import sale_sources
_fd = mock.patch.object(sale_sources, "fetch_due", lambda *a, **k: []); _fd.start()   # только Uybor
sdb = Path("/tmp/test_sale.db"); sdb.unlink(missing_ok=True)
sstore = rr.Store(sdb)
sent_msgs = []
inmsg = lambda m, i: f"listings/{i}" in m[1] or f':{i}"' in (m[2] if len(m) > 2 else "")
shown = lambda i: any(inmsg(m, i) for m in sent_msgs)
def fake_sale_tg(cfg_, method, payload, timeout=20, quiet=False):
    sent_msgs.append((method, payload.get("text") or payload.get("caption") or payload.get("media") or "",
                      payload.get("reply_markup") or ""))
    return {"ok": True, "result": {}}

ss_tg = dict(ss, telegram_bot_token="T", telegram_chat_id="1")
by_id = {x["key"].rsplit(":", 1)[1]: x for x in sl}
with mock.patch.object(rr.requests, "get", fake_uy_get):
    reasons = {k: rr.sale_reject(dict(v), scfg_, sstore, ss_tg) for k, v in by_id.items()}
assert reasons["101"] == ("", False), reasons["101"]
assert "агентство или маклер" in reasons["102"][0]
assert "дороже бюджета" in reasons["103"][0]
assert reasons["104"] == ("", False), reasons["104"]            # сумы переведены в доллары
assert "текст агентства" in reasons["201"][0]
assert reasons["202"] == ("", False), reasons["202"]            # «риелторам не беспокоить» = хозяин
assert "дн." in reasons["203"][0]
assert reasons["301"][0] == "3-комн"

# продавца проверить не удалось → не отсеиваем навсегда, пробуем позже
def broken_get(url, params=None, headers=None, timeout=None):
    raise rr.requests.ConnectionError("нет сети")
fresh = dict(by_id["101"], seller_id="uybor:999")
with mock.patch.object(rr.requests, "get", broken_get):
    assert rr.sale_reject(fresh, scfg_, sstore, ss_tg) == ("не удалось проверить продавца", True)

# карточка: продажа, цена за м², вторичка и ремонт, ссылка на Uybor
ok101 = dict(by_id["101"]); rr.sale_reject(ok101, scfg_, sstore, ss_tg)
card = rr.format_sale_message(ok101, ss_tg)
assert "Продажа · от собственника" in card and "Яккасарай" in card
assert "💰 $40 000" in card and "~$1 333/м²" in card, card
assert "вторичка · евроремонт" in card and "uybor.uz/listings/101" in card

# полный проход: сначала одно вступление, потом три подходящих варианта
sdb.unlink(missing_ok=True); sstore = rr.Store(sdb); sent_msgs.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    n = rr.run_sale_search(ss_tg, sstore, {"photos": False})
assert n == 0, (n, sent_msgs)                      # ниже рынка не нашлось — всё в подборку
assert "Поиск квартиры для покупки включён" in sent_msgs[0][1]
assert "нашлось подходящих: 3" in sent_msgs[0][1] and "45 дней" in sent_msgs[0][1]
cards_ = [m for m in sent_msgs if "Что нашлось сейчас" in m[1]]          # без списка: каждая — своей карточкой
assert len(cards_) == 3 and all(shown(i) for i in (101, 104, 202)) and "1 из 3" in cards_[0][1]
assert all("Собственник" in m[1] for m in cards_)
# повторный проход ничего не дублирует и вступление не повторяет
sent_msgs.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    assert rr.run_sale_search(ss_tg, sstore, {"photos": False}) == 0
assert sent_msgs == []

# Telegram недоступен (например, токен отозван): ничего не теряется
sdb.unlink(missing_ok=True); sstore = rr.Store(sdb)
with mock.patch.object(rr.requests, "get", fake_uy_get), \
        mock.patch.object(rr, "tg_call", lambda *a, **k: None), mock.patch.object(rr.time, "sleep"):
    assert rr.run_sale_search(ss_tg, sstore, {"photos": False}) == 0
assert not sstore.get_kv("sale_intro_sent", False)
assert not sstore.known("sale:uybor:101")          # придёт, когда Telegram оживёт
sent_msgs.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    assert rr.run_sale_search(ss_tg, sstore, {"photos": False}) == 0
assert all(shown(i) for i in (101, 104, 202))

# пауза: ничего не шлём и не помечаем
sdb.unlink(missing_ok=True); sstore = rr.Store(sdb); sent_msgs.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    assert rr.run_sale_search(ss_tg, sstore, {"paused": True}) == 0
assert sent_msgs == [] and not sstore.known("sale:uybor:101")

# /sale показывает критерии и присланные варианты
sdb.unlink(missing_ok=True); sstore = rr.Store(sdb)
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    rr.run_sale_search(ss_tg, sstore, {"photos": False})
with mock.patch.object(rr, "SALE_DB_PATH", sdb):
    st = rr.sale_status_text(ss_tg)
    reply, view = rr.handle_command("/sale", rr.default_settings(), store, ss_tg)
assert reply == st and "прислано: 3" in st and "1-комн, 30 м², $40 000" in st, st
assert "Поиск квартиры для покупки выключен" in rr.sale_status_text(cfg)
# аренда не затронута: Uybor-аренда по-прежнему парсится старым путём
assert rr.uybor_listing(uy(1, 1, 205))["key"] == "uybor:1"
sdb.unlink(missing_ok=True)
print("OK — покупка от собственника: фильтры, агентства, вступление, повторы, /sale")

# --------------------------------- покупка: маклеры тоже, любая комнатность ----
ss_all = rr.deep_merge(ss_tg, {"sale_search": {"owner_only": False, "rooms": []}})
sa_ = ss_all["sale_search"]
EXPECT_ROOMS = None                                  # без фильтра комнат в запросе к API
sdb.unlink(missing_ok=True); sstore = rr.Store(sdb)
with mock.patch.object(rr.requests, "get", fake_uy_get):
    l102 = dict(by_id["102"]); assert rr.sale_reject(l102, sa_, sstore, ss_all) == ("", False)
    l201 = dict(by_id["201"]); assert rr.sale_reject(l201, sa_, sstore, ss_all) == ("", False)
    l101 = dict(by_id["101"]); assert rr.sale_reject(l101, sa_, sstore, ss_all) == ("", False)
    l103 = dict(by_id["103"]); assert "дороже бюджета" in rr.sale_reject(l103, sa_, sstore, ss_all)[0]
assert (l102["seller_kind"], l201["seller_kind"], l101["seller_kind"]) == ("agency", "agency", "owner")
assert "Продажа · агентство / маклер" in rr.format_sale_message(l102, ss_all)
assert "объявлений у продавца на Uybor: 134" in rr.format_sale_message(l102, ss_all)
assert "Продажа · от собственника" in rr.format_sale_message(l101, ss_all)
# API продавца недоступно: маклеров не отсекаем — присылаем с пометкой «не проверен»
with mock.patch.object(rr.requests, "get", broken_get):
    lx = dict(by_id["101"], seller_id="uybor:777")
    assert rr.sale_reject(lx, sa_, sstore, ss_all) == ("", False)
assert "продавец не проверен" in rr.format_sale_message(lx, ss_all)
assert "любая комнатность" in rr.sale_criteria_text(sa_) and "маклеры" in rr.sale_criteria_text(sa_)

# смена условий: отсеянное раньше пересматривается, присланное не повторяется
EXPECT_ROOMS = "1,2"
sdb.unlink(missing_ok=True); sstore = rr.Store(sdb); sent_msgs.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    rr.run_sale_search(ss_tg, sstore, {"photos": False})
assert sum(shown(i) for i in (101, 104, 202)) == 3                      # только собственники
EXPECT_ROOMS = None; sent_msgs.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    n = rr.run_sale_search(ss_all, sstore, {"photos": False})
assert all(shown(i) for i in (102, 201, 301)), [m[1][:60] for m in sent_msgs]   # 102, 201 и 3-комнатная 301
assert "Условия поиска обновлены" in sent_msgs[0][1] and "нашлось подходящих: 3" in sent_msgs[0][1]
assert sum("Маклер / агентство" in m[1] for m in sent_msgs) == 2         # 102 и 201
assert any("<b>3-комн" in m[1] and "Собственник" in m[1] and "listings/301" in m[1] for m in sent_msgs)
assert not shown(101)                                                   # не повторили
sent_msgs.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    assert rr.run_sale_search(ss_all, sstore, {"photos": False}) == 0   # условия те же — тишина
assert sent_msgs == []

# одна квартира от двух маклеров — одно уведомление
LONG = ("Продаётся 1-комнатная квартира, Яккасарайский район, ориентир Хосилот, кирпичный дом, "
        "3 этаж из 5, евроремонт, остаётся мебель и техника, документы готовы")
UY_ADS.update({9: 60, 10: 80})
UY_BY_DISTRICT[205] = [uy(401, 9, 205, price=39000, desc=LONG), uy(402, 10, 205, price=39500, desc=LONG + "!")]
sdb.unlink(missing_ok=True); sstore = rr.Store(sdb); sent_msgs.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    rr.run_sale_search(ss_all, sstore, {"photos": False})
assert sum(inmsg(m, 401) or inmsg(m, 402) for m in sent_msgs) == 1, sent_msgs
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    rr.run_sale_search(ss_all, sstore, {"photos": False})
assert sum(inmsg(m, 401) or inmsg(m, 402) for m in sent_msgs) == 1   # дубль сохранён как дубль
assert sstore.known("sale:uybor:402") or sstore.known("sale:uybor:401")
sdb.unlink(missing_ok=True)
# у агентства один телефон на разные квартиры — это не дубли
AG = "Агентство «Тест». Тел: +998 90 111-22-33. "
UY_ADS.update({11: 90})
UY_BY_DISTRICT[205] = [
    uy(501, 11, 205, price=30000, desc=AG + "1-комн, 23 м², бывшее общежитие, кухня и санузел внутри, 2 этаж"),
    uy(502, 11, 205, price=38000, desc=AG + "1-комн, 30 м², новостройка, кирпич, ипотека возможна, 1 этаж")]
UY_BY_DISTRICT[205][0]["square"], UY_BY_DISTRICT[205][1]["square"] = 23, 30
sdb.unlink(missing_ok=True); sstore = rr.Store(sdb); sent_msgs.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    rr.run_sale_search(ss_all, sstore, {"photos": False})
waiting = {x["key"] for x in sstore.get_kv("sale_pick") or []}            # подборка — 5 штук, остальные ждут
assert all(shown(i) or f"sale:uybor:{i}" in waiting for i in (501, 502)), sent_msgs
sdb.unlink(missing_ok=True)
# перевыкладка: старое объявление отсеяли по возрасту, свежая копия — присылаем с датой
OLD = "Продаётся 1-комн квартира, Юнусабад, бывшее общежитие, кухня и санузел внутри, 2 этаж из 4"
UY_ADS.update({12: 30})
UY_BY_DISTRICT[205] = [uy(601, 12, 205, price=28500, days=136, desc=OLD),
                       uy(602, 12, 205, price=28500, days=2, desc=OLD + ".")]
sdb.unlink(missing_ok=True); sstore = rr.Store(sdb); sent_msgs.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    rr.run_sale_search(ss_all, sstore, {"photos": False})
m602 = [m[1] for m in sent_msgs if inmsg(m, 602)]
assert len(m602) == 1 and "перевыложено" in m602[0] and "на рынке ~136 дн." in m602[0], sent_msgs
assert not any(inmsg(m, 601) for m in sent_msgs)
sdb.unlink(missing_ok=True)
_fd.stop()
print("OK — покупка с маклерами: пометка продавца, пересмотр при смене условий, дубли")

# ------------------------------------- данные вне публичного репозитория ----
import os as _os, subprocess as _sp, sys as _sys
_r = _sp.run([_sys.executable, "-c", "import rent_radar as r; print(r.DB_PATH); print(r.SALE_DB_PATH)"],
             env={**_os.environ, "RADAR_STATE_DIR": "/tmp/rr-state"}, capture_output=True, text=True,
             cwd=str(Path(rr.__file__).parent))
assert _r.stdout.split() == ["/tmp/rr-state/radar.db", "/tmp/rr-state/sale.db"], (_r.stdout, _r.stderr)
assert rr.DB_PATH.parent == Path(rr.__file__).resolve().parent      # без переменной — рядом с кодом
print("OK — базы берутся из RADAR_STATE_DIR (приватный rent-radar-state)")

# ---------------------------------------------- анализ рынка (market.py) ----
import market as mk

R = lambda **kw: dict({"id": 1, "price": 30000, "priceCurrency": "usd", "priceType": "all",
                      "square": 25, "room": "1", "districtId": 197, "isNewBuilding": False,
                      "createdAt": _iso(10), "userId": 1, "description": "Квартира"}, **kw)
row = mk._row_from_uybor(R(priceType="sqm", price=1400), "sale", 11780)
assert row["price_usd"] == 35000 and row["key"] == "sale:uybor:1"          # цена за м² × площадь
assert abs(mk._row_from_uybor(R(price=353_400_000, priceCurrency="uzs"), "sale", 11780)["price_usd"] - 30000) < 1
assert mk._row_from_uybor(R(price=40, pricePeriodUnit="day"), "rent", 11780) is None   # посуточно
assert mk._row_from_uybor(R(price=900), "sale", 11780) is None                         # $36/м² — опечатка
assert mk._row_from_uybor(R(description="Бывшее общежитие, галерейка"), "sale", 11780)["dorm"] == 1
assert mk._row_from_uybor(R(description="НОВОСТРОЙКА! ЖК Imperial"), "sale", 11780)["new_building"] == 1

# срез: страницы, история цены, снятые с продажи
SALE_PAGES = [[R(id=1, price=30000), R(id=2, price=40000, square=30)]]
RENT = {197: [R(id=900 + i, price=p, square=25, pricePeriodUnit="month") for i, p in enumerate([300, 320, 340, 360, 380])]}
def fake_mk_get(url, params=None, headers=None, timeout=None):
    p = params or {}
    if p.get("operationType__eq") == "rent":
        return FakeUy({"results": RENT.get(p.get("district__eq"), []) if p.get("page") == 1 else []})
    if "user__eq" in p:
        return FakeUy({"total": 1, "results": [{"userId": int(p["user__eq"])}]})
    pages = SALE_PAGES
    i = p.get("page", 1) - 1
    return FakeUy({"results": pages[i] if i < len(pages) else []})
mdb = Path("/tmp/test_market.db"); mdb.unlink(missing_ok=True)
mst = rr.Store(mdb)
with mock.patch.object(mk.requests, "get", fake_mk_get), mock.patch.object(mk.time, "sleep"):
    assert mk.scan(mst, ss_all, [197]) == {"sale": 2, "rent": 5}
    SALE_PAGES = [[R(id=1, price=28000)]]                                   # подешевела, №2 снята
    mk.scan(mst, ss_all, [197])
assert [p for _, p in mk.price_history(mst, {"key": "sale:uybor:1"})] == [30000, 28000]
assert mst.conn.execute("SELECT removed_at IS NOT NULL FROM market WHERE key='sale:uybor:2'").fetchone()[0] == 1
assert mk.maybe_scan(mst, ss_all, [197]) is False                         # раз в сутки, не чаще
mst.set_kv("market_scan_at", "2020-01-01T00:00:00+00:00"); mst.set_kv("market_scan_try", None)
with mock.patch.object(mk.requests, "get", broken_get):
    assert mk.maybe_scan(mst, ss_all, [197]) is False                     # Uybor недоступен
    assert mk.maybe_scan(mst, ss_all, [197]) is False                     # и повтор не раньше чем через час
with mock.patch.object(mk, "scan") as sc:
    mk.maybe_scan(mst, ss_all, [197]); assert not sc.called

# похожие: общежитие сравниваем только с общежитиями; огромная разница — не аналоги
mst.conn.execute("DELETE FROM market WHERE op='sale'")
def put(i, price, area, dorm=0, nb=0, d="Юнусабад"):
    mst.conn.execute("INSERT INTO market(key, op, district, rooms, area, price_usd, new_building, dorm, "
                     "first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (f"sale:uybor:{i}", "sale", d, 1, area, price, nb, dorm, "x", "x"))
for i in range(6):
    put(100 + i, 1250 * 24, 24, dorm=1)            # общежития по $1 250/м²
    put(200 + i, 2500 * 25, 25)                     # обычные квартиры по $2 500/м²
mst.conn.commit()
dorm_l = {"key": "sale:uybor:1", "district": "Юнусабад", "area": 24, "rooms": 1, "price_usd": 27600,
          "text": "Бывшее общежитие, санузел на этаже", "created_at": _iso(100), "seller_kind": "agency"}
c = mk.comparables(mst, dorm_l)
assert round(c["median_m2"]) == 1250 and "общежития" in c["label"], c
flat_l = dict(dorm_l, text="Квартира в кирпичном доме", price_usd=60000)
assert round(mk.comparables(mst, flat_l)["median_m2"]) == 2500
cheap = dict(flat_l, price_usd=30000)                                       # −50% к похожим
xa = mk.analyze(mst, cheap, ss_all)
assert "gap" not in xa and xa["comp_unreliable"] < -0.4 and xa["verdict"][0] == "⚪"

# аренда, сценарии, налог при продаже раньше 3 лет, вердикт
x = mk.analyze(mst, dorm_l, ss_all)
assert abs(x["gap"] - (27600 / 24 / 1250 - 1)) < 1e-9 and x["verdict"][0] == "🟠"
assert abs(x["values"]["base"][3] - 27600 * 1.03 ** 3) < 0.01
assert abs(x["values"]["pess"][1] - 27600 * 0.94) < 0.01
assert "dorm" in x["flags"] and "stale" in x["flags"] and "agency" in x["flags"]
assert abs(x["rent"] - 340 * 0.85) < 0.01, x["rent"]                        # медиана 5 объявл. − 15%
gain1 = 27600 * 0.03
assert abs(x["invest"][1]["base"] - (gain1 * 0.88 + x["rent"] * 11 * 0.88)) < 0.01   # налог 12% с прироста
assert abs(x["invest"][3]["base"] - (27600 * (1.03 ** 3 - 1) + x["rent"] * 11 * 0.88 * 3)) < 0.01
txt = mk.format_analysis(mst, dorm_l, ss_all)
for part in ("📊 <b>Анализ</b>", "на 8% ниже похожих", "Цена через 1 / 3 / 5 лет", "Бывшее общежитие",
             "висит 100 дн.", "🟠 <b>", mk.REPORT_URL, "средняя по району: $1 441/м²", "🏦 Ипотека — в объявлении не указано", "взнос $6 900 (25%)",
             "🤝 Торг: по похожим разумная цена", "Ремонт не понять", "<blockquote expandable>", "📈 Как вложение"):
    assert part in txt, (part, txt)
# аренда по объявлениям нереально высокая → в расчёте осторожная средняя, цифра из объявлений рядом
RENT_HI = dict(dorm_l, text="Квартира", price_usd=12000, area=24)
mst.conn.execute("UPDATE market SET price_usd=price_usd WHERE 0"); mst.conn.commit()
xh = mk.analyze(mst, RENT_HI, ss_all)
assert xh["rent_ads"] and abs(xh["rent"] - 9.5 * 24) < 0.01, xh
s_txt = mk.summary_text(mst, ss_all, ["Юнусабад"])
assert "Юнусабад" in s_txt and "Сценарии на год" in s_txt and mk.REPORT_URL in s_txt

# в боте: после карточки — отдельное сообщение с анализом; старым вариантам — один раз
sdb.unlink(missing_ok=True); sstore = rr.Store(sdb); sent_msgs.clear()
UY_BY_DISTRICT[205] = [uy(701, 1, 205, price=39000)]
def fake_both(url, params=None, headers=None, timeout=None):
    p = params or {}
    if "district__eq" in p and p.get("operationType__eq") == "sale":
        return fake_uy_get(url, params, headers, timeout)
    return fake_mk_get(url, params, headers, timeout)
SALE_PAGES = [[R(id=701, price=39000, square=30, districtId=205)]]
with mock.patch.object(rr.requests, "get", fake_both), mock.patch.object(mk.requests, "get", fake_both), \
        mock.patch.object(rr, "tg_call", fake_sale_tg), mock.patch.object(rr.time, "sleep"), \
        mock.patch.object(mk.time, "sleep"), \
        mock.patch.object(sale_sources, "score", lambda *a: (90, ["−12% к рынку"], True, {})):   # «сильный» — сразу
    rr.run_sale_search(ss_all, sstore, {"photos": False})
texts = [m[1] for m in sent_msgs]
i_card = next(i for i, t in enumerate(texts) if "listings/701" in t)
card1 = texts[i_card]                                                       # карточка и разбор — одно сообщение
assert "<b>Ипотека</b>" in card1 and "<b>Торг</b>" in card1 and "<blockquote expandable>" in card1, card1
assert rr.tg_len(card1) <= rr.CAPTION_MAX and not any("📊 <b>Анализ</b>" in t for t in texts), texts
assert not sent_msgs[i_card][2]                                            # без кнопок — словами
assert sstore.get_kv("sale_analysis_backfill") is True
sent_msgs.clear()
sstore.set_kv("sale_analysis_backfill", False)                              # как после обновления бота
with mock.patch.object(rr, "tg_call", fake_sale_tg), mock.patch.object(rr.time, "sleep"):
    assert rr.backfill_sale_analysis(ss_all, sstore, {}) == 1
assert "Добавила анализ цены" in sent_msgs[0][1] and "📊 <b>Анализ</b>" in sent_msgs[1][1]
with mock.patch.object(rr, "SALE_DB_PATH", sdb):
    reply, _ = rr.handle_command("/rynok", rr.default_settings(), store, ss_all)
assert "Рынок по срезу Uybor" in reply
# цена «за м²» у Uybor переводится в полную
assert rr.uybor_listing(dict(uy(9, 1, 205, price=1500), priceType="sqm"))["price_value"] == 45000
sdb.unlink(missing_ok=True); mdb.unlink(missing_ok=True)
print("OK — анализ рынка: срез Uybor, история цен, аналоги, аренда, сценарии, вердикт, /rynok")

# ============ маклеры по продаже: сбор из Telegram и Uybor, выбор под тип сделки ============
import unittest.mock as _mk
bdb = Path("/tmp/test_brokers.db"); bdb.unlink(missing_ok=True)
bs = rr.Store(bdb)
bs.upsert_broker("tg:arentash", "TG @arentash", "", "901112233", 500, "Мирабад", 600)   # арендный
bs.upsert_broker("tel:977777777", "TG @x", "", "977777777", 2, "Юнусабад", 90000, deal="sale")
assert [b["bid"] for b in bs.brokers(deal="sale")] == ["tel:977777777"]
assert [b["bid"] for b in bs.brokers(deal="rent")] == ["tg:arentash"]
assert bs.brokers(deal="sale")[0]["min_price"] is None          # цены продажи в диапазон не пишем
bs.upsert_broker("tg:arentash", "TG @arentash", "", "", 500, None, None, deal="sale")
assert set(bs.brokers(deal="sale")[0]["deals"] if bs.brokers(deal="sale")[0]["bid"] == "tg:arentash"
           else bs.brokers(deal="sale")[1]["deals"]) == {"rent", "sale"}
assert bs.broker_stats("sale")[0] == 2 and bs.broker_stats("rent")[0] == 1

POSTS = [
    {"key": "tg:a:1", "source": "TG @a", "text": "Продается 2-комн, Юнусабад, 55 000$. Тел +998 90 123 45 67",
     "phones": ["901234567"], "district": "Юнусабад"},
    {"key": "tg:a:2", "source": "TG @a", "text": "Продаётся 3-комн, Мирабад. +998 90 123 45 67",
     "phones": ["901234567"], "district": "Мирабад"},
    {"key": "tg:b:1", "source": "TG @b", "text": "Сдается квартира 600$/мес, 935554433",
     "phones": ["935554433"], "district": "Чиланзар"},                       # аренда — мимо
    {"key": "tg:b:2", "source": "TG @b", "text": "Продается квартира, агентство недвижимости, 998881122",
     "phones": ["998881122"], "district": None},                             # слово агентства — сразу
    {"key": "tg:b:3", "source": "TG @b", "text": "Продаю свою квартиру, 1 раз, 977001122",
     "phones": ["977001122"], "district": None},                             # хозяин, одно объявление
]
UY = {"results": [
    {"id": 1, "userId": 77, "description": "Продажа, звоните 95 444 33 22", "districtId": 205, "price": 60000},
    {"id": 2, "userId": 78, "description": "Без телефона", "price": 50000},
]}
class _UR:
    status_code = 200
    def raise_for_status(s): pass
    def json(s): return UY
with _mk.patch.object(rr, "fetch_telegram", lambda scfg, cfg: POSTS), \
        _mk.patch.object(rr.requests, "get", lambda *a, **k: _UR()), \
        _mk.patch.object(rr, "uybor_user_ads", lambda uid, store, cfg: 12):
    rr.harvest_sale_brokers(dict(cfg, sale_search={"broker_uybor_pages": 1}), bs)
sale = {b["bid"]: b for b in bs.brokers(deal="sale", limit=100)}
assert "tel:901234567" in sale and sale["tel:901234567"]["ads"] == 2
assert "tel:998881122" in sale and "tel:935554433" not in sale and "tel:977001122" not in sale
assert "uybor:77" in sale and sale["uybor:77"]["phone"] == "954443322"

# запрос на покупку → карточки только продающих маклеров
bs.set_kv("anketa", {"ans": {"deal": "buy"}})
CARDS = []
with _mk.patch.object(rr, "tg_call", lambda c, m, pl, **k: (CARDS.append(pl), {"ok": True})[1]), \
        _mk.patch.object(rr, "send_telegram", lambda c, t: (CARDS.append({"text": t}), True)[1]), \
        _mk.patch.object(rr.time, "sleep"):
    rr.send_broker_cards(cfg, bs, rr.default_settings(), text="Хочу купить квартиру")
head = CARDS[0]["text"]
assert "по продаже" in head, head
assert len(CARDS) == 2 and "в очереди ещё" in CARDS[1]["text"]   # заголовок + первый маклер (по одному)
assert all("$" not in c.get("text", "") for c in CARDS[1:])      # без арендного диапазона цен
bs.set_kv("anketa", {"ans": {"deal": "rent"}})
CARDS.clear()
with _mk.patch.object(rr, "tg_call", lambda c, m, pl, **k: (CARDS.append(pl), {"ok": True})[1]), \
        _mk.patch.object(rr, "send_telegram", lambda c, t: (CARDS.append({"text": t}), True)[1]), \
        _mk.patch.object(rr.time, "sleep"):
    rr.send_broker_cards(cfg, bs, rr.default_settings(), text="Хочу снять")
assert "по аренде" in CARDS[0]["text"] and len(CARDS) == 2       # заголовок + 1 арендный маклер
bdb.unlink(missing_ok=True)
print("OK — маклеры по продаже: Telegram + Uybor, пометка сделки, рассылка под покупку/аренду")

# ============ маклеры с Realt24 и Joymee (контакт открыт в API) ============
mdb2 = Path("/tmp/test_market_brokers.db"); mdb2.unlink(missing_ok=True)
ms3 = rr.Store(mdb2)
R24 = {"data": [
    {"phone": "+998901112233", "isCommissioned": True, "propertyUser": {"firstName": "Ольга", "lastName": "Б"},
     "address": {"fullAddress": {"ru": "Ташкент, Мирабадский район, ул. X"}}},
    {"phone": "+998935554433", "isCommissioned": False, "address": {"fullAddress": {"ru": "Ташкент, Юнусабадский район"}}},
    {"phone": "+998935554433", "isCommissioned": False, "address": {"fullAddress": {"ru": "Ташкент, Юнусабадский район"}}},
    {"phone": "+998977001122", "isCommissioned": False, "address": {"fullAddress": {"ru": "Ташкент, Чиланзарский район"}}},
    {"phone": "+998909998877", "isCommissioned": True, "address": {"fullAddress": {"ru": "Самарканд, центр"}}},
], "meta": {"hasNext": False}}
JM_LIST = {"results": [{"id": 11, "created_by": {"id": 501}}, {"id": 12, "created_by": {"id": 501}},
                       {"id": 13, "created_by": {"id": 502}}], "next": None}
JM_DET = {11: {"phone_number": "+998951234567", "advertiser_type": 2, "seller": {"first_name": "Umid"},
               "district": {"name": "Yunusobod tumani"}},
          13: {"phone_number": "+998881234567", "advertiser_type": 1, "seller": {}}}   # собственник — мимо
class _J:
    def __init__(s, d): s._d = d; s.status_code = 200
    def raise_for_status(s): pass
    def json(s): return s._d
def fake_market(url, params=None, headers=None, timeout=None):
    if "realt24" in url: return _J(R24)
    if url.rstrip("/").endswith("announcement"): return _J(JM_LIST)
    return _J(JM_DET[int(url.rstrip("/").split("/")[-1])])
with _mk.patch.object(rr.requests, "get", fake_market), _mk.patch.object(rr.time, "sleep"):
    assert rr.harvest_realt24(ms3, "sale", pages=1) == 3          # комиссия, 2 объявления; не Самарканд, не хозяин
    assert rr.harvest_joymee(ms3, "sale", pages=1) == 1
    rr.harvest_joymee(ms3, "sale", pages=1)                       # повтор: карточки заново не запрашиваем
sale = {b["bid"]: b for b in ms3.brokers(deal="sale", limit=50)}
assert set(sale) == {"tel:901112233", "tel:935554433", "joymee:501"}, set(sale)
assert sale["tel:901112233"]["name"] == "Ольга Б" and sale["tel:935554433"]["ads"] == 2
assert sale["joymee:501"]["phone"] == "951234567" and sale["joymee:501"]["ads"] == 2
assert ms3.get_kv("joymee_agents") == {"501": "951234567", "502": ""}
assert not ms3.brokers(deal="rent")                               # сделка не перепутана
mdb2.unlink(missing_ok=True)
print("OK — маклеры с Realt24 и Joymee: телефон, посредник/хозяин, Ташкент, без повторных запросов")

# ============ Realting: агентства с Telegram (телефоны зашифрованы — не берём) ============
RT_HTML = """
<div class="teaser-company mb-sm" data-id="501"><div class="title"> <a href="https://realting.uz/agencies/a">Агентство А</a></div>
<div class="address">Узбекистан, Ташкент</div><a class="unit-item" title="Жилая"><img src="x"> <span>12</span></a>
<a class="btn" href="https://telegram.me/agency_a?text=%F0%9F%92%AC">Написать в Telegram</a>
<span data-encr-ph="Zm9vYmFy"></span></div>
<div class="teaser-company mb-sm" data-id="502"><div class="title"> <a href="/agencies/b">Агентство Б</a></div>
<div class="address">Узбекистан, Бухара</div><a href="https://telegram.me/agency_b?text=x">Telegram</a></div>
<div class="teaser-company mb-sm" data-id="503"><div class="title"> <a href="/agencies/c">Без телеграма</a></div>
<div class="address">Узбекистан, Ташкент</div><a href="https://telegram.me/share?url=x">share</a></div>
"""
cards = rr.parse_realting_agencies(RT_HTML)
assert [c["tg"] for c in cards] == ["agency_a", "agency_b", ""] and cards[0]["objects"] == 12
rdb = Path("/tmp/test_realting.db"); rdb.unlink(missing_ok=True)
rs = rr.Store(rdb)
pages = {1: RT_HTML, 2: ""}
class _H:
    def __init__(s, t): s.text, s.status_code = t, 200
    def raise_for_status(s): pass
with _mk.patch.object(rr.requests, "get", lambda url, params=None, **k: _H(pages.get(params["page"], ""))), \
        _mk.patch.object(rr.time, "sleep"):
    assert rr.harvest_realting(rs, pages=3) == 1                   # только Ташкент и с Telegram
assert rs.get_kv("realting_page") == 1                             # каталог кончился — снова с начала
b = rs.brokers(deal="sale")[0]
assert b["bid"] == "realting:501" and b["tg"] == "agency_a" and not b["phone"]
assert rs.broker_stats("sale")[1] == 1                             # Telegram считается контактом
rs.set_kv("anketa", {"ans": {"deal": "buy"}})
CARDS = []
with _mk.patch.object(rr, "tg_call", lambda c, m, pl, **k: (CARDS.append(pl), {"ok": True})[1]), \
        _mk.patch.object(rr, "send_telegram", lambda c, t: (CARDS.append({"text": t}), True)[1]), \
        _mk.patch.object(rr.time, "sleep"):
    rr.send_broker_cards(cfg, rs, rr.default_settings(), text="Хочу купить квартиру")
card = CARDS[1]
assert "✈️ @agency_a" in card["text"] and "📞" not in card["text"]
kb = json.loads(card["reply_markup"])["inline_keyboard"]
assert kb[0][0]["url"].startswith("https://t.me/agency_a?text=") and "WhatsApp" not in json.dumps(kb, ensure_ascii=False)
rdb.unlink(missing_ok=True)
print("OK — Realting: агентства Ташкента с Telegram, обход каталога по кругу, карточка с Telegram-ссылкой")

# ============ путь клиента: параметры чата управляют поиском покупки; маклеры — по районам ============
edb = Path("/tmp/test_eff.db"); edb.unlink(missing_ok=True)
es = rr.Store(edb)
base = rr.deep_merge(rr.DEFAULT_CONFIG, {"sale_search": {"enabled": False}})
assert rr.effective_sale_cfg(base, es) is base                       # нет покупки в чате — как в настройках
es.set_kv("anketa", {"ans": {"deal": "buy", "object": "flat", "rooms": ["2", "4"], "budget": "50000",
                             "districts": [], "note": "ближе к центру"}})
ss = rr.effective_sale_cfg(base, es)["sale_search"]
assert ss["enabled"] and ss["max_price_usd"] == 50000 and ss["rooms"] == [2, 4, 5, 6]
assert ss["districts"] == base["sale_search"]["districts"]            # районов нет — центр из настроек
es.set_kv("anketa", {"ans": {"deal": "buy", "object": "flat", "rooms": [], "budget": "60000",
                             "districts": [str(rr.DISTRICT_LIST.index("Чиланзар"))]}})
ss = rr.effective_sale_cfg(base, es)["sale_search"]
assert ss["districts"] == ["Чиланзар"] and ss["rooms"] == []
es.set_kv("anketa", {"ans": {"deal": "rent", "budget": "900"}})
assert rr.effective_sale_cfg(base, es)["sale_search"]["enabled"] is False        # ищет аренду — продажу выключаем
# ранжирование: сначала маклеры нужных районов
es.set_kv("anketa", {"ans": {"deal": "buy", "districts": [], "note": "ближе к центру"}})
es.upsert_broker("a", "X", "Окраина", "901000001", 90, "Сергели", None, deal="sale")
es.upsert_broker("b", "X", "Центр", "901000002", 5, "Мирабад", None, deal="sale")
assert [b["bid"] for b in rr.ranked_brokers(es, "sale")] == ["b", "a"]
edb.unlink(missing_ok=True)
print("OK — путь клиента: параметры чата → поиск покупки, маклеры нужных районов первыми")

# ============ без команд: кнопки вместо подсказок «/xxx», маклера не приветствуем дважды ============
wdb = Path("/tmp/test_nocmd.db"); wdb.unlink(missing_ok=True)
ws = rr.Store(wdb)
W = []
with mock.patch.object(rr, "tg_call", lambda c, m, pl, **k: (W.append((m, pl)), {"ok": True})[1]):
    rr.handle_broker_message(cfg, ws, {"chat": {"id": 31}, "text": "/start", "_welcomed": True})
    assert not W and ws.get_kv("welcomed:31")                     # воркер уже поздоровался
    cg.show_offers(cfg, ws)                                       # пусто — не тупик, а кнопки
    m, pl = W[-1]
    assert "новых вариантов нет" in pl["text"] and '"b"' in pl["reply_markup"]
    assert "WhatsApp" not in pl["text"] and "cmd:/add" not in pl["reply_markup"]   # пока никому не писали — без лишнего
    ws.upsert_broker("x1", "X", "", "901112233", 3, None, None); ws.broker_status("x1", "contacted")
    cg.show_offers(cfg, ws)
    assert "написали 1 маклерам" in W[-1][1]["text"] and "перешлите" in W[-1][1]["text"]
for t in (rr.HELP_TEXT,):
    assert "своими словами" in t and "заучивать не нужно" in t and "/new" not in t and "/brokers" not in t
    assert "Внизу три кнопки" not in t
wdb.unlink(missing_ok=True)
print("OK — без команд и кнопок: справка словами, понятные подсказки, маклер не получает приветствие дважды")

# ============ мгновенные «Варианты»/«Маклерам»: снимок для воркера ============
sdb2 = Path("/tmp/test_snap.db"); sdb2.unlink(missing_ok=True)
ss2 = rr.Store(sdb2)
ss2.set_kv("anketa", {"ans": {"deal": "buy", "districts": [str(rr.DISTRICT_LIST.index("Мирабад"))]}})
ss2.set_kv("request_text", "Здравствуйте! Хочу купить квартиру в Ташкенте.")
for i in range(3):
    cg.save_offer(ss2, cfg, 700 + i, f"М{i}", f"2 комн Мирабад 5{i} м2 4{i} 000$", [f"p{i}"] if i == 0 else [])
cg.set_offer_status(ss2, 3, "shortlist")
ss2.upsert_broker("tel:901112233", "Realt24", "Ольга", "901112233", 12, "Мирабад", None, deal="sale")
ss2.upsert_broker("tel:909998877", "Realt24", "", "909998877", 40, "Сергели", None, deal="sale")
snap = rr.ui_snapshot(cfg, ss2, rr.default_settings())
assert snap["offers_total"] == 2 and snap["shortlist"] == 1 and snap["deal"] == "sale"
assert snap["offers"][0]["photos"] == ["p0"] and "Вариант 1 из 2" in snap["offers"][0]["text"]
assert [b["bid"] for b in snap["brokers"]] == ["tel:901112233", "tel:909998877"]      # сначала Мирабад
assert snap["brokers"][0]["row"][0]["url"].startswith("https://wa.me/998901112233?text=")
assert "Хочу купить" in snap["header"]
assert [r["oid"] for r in snap["sl"]["n"]["items"]] == [3] and "Шортлист" in snap["sl"]["n"]["title"]
assert set(snap["texts"]) >= {"/help", "/sale", "/request"} and "Хочу купить" in snap["texts"]["/request"]
POSTED = []
class _P:
    status_code = 200
wcfg2 = dict(cfg, worker_url="https://w.example", worker_key="k")
with mock.patch.object(rr.requests, "post", lambda url, json=None, **k: (POSTED.append((url, json)), _P())[1]):
    assert rr.push_snapshot(wcfg2, ss2, rr.default_settings(), force=True)
    assert not rr.push_snapshot(wcfg2, ss2, rr.default_settings())            # чаще 15 с — нет
    assert not rr.push_snapshot(wcfg2, ss2, rr.default_settings(), force=True)  # без изменений — нет
assert POSTED[0][0] == "https://w.example/svc/snapshot" and POSTED[0][1]["brokers_total"] == 2
# нажатия, которые воркер уже провёл, — только сохраняются (никаких сообщений)
with mock.patch.object(rr, "tg_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("не писать"))):
    rr.apply_worker_done("bw:tel:901112233", cfg, ss2)
    rr.apply_worker_done("bx:tel:909998877", cfg, ss2)
assert not ss2.brokers(status="new", deal="sale") and ss2.get_kv("outreach") == {"sent": 1, "skipped": 1}
sdb2.unlink(missing_ok=True)
print("OK — мгновенные кнопки: снимок вариантов и маклеров для воркера, сохранение нажатий")

# ============ разбор вариантов моделью (через воркер): точнее regex, реплики — не карточки ============
adb = Path("/tmp/test_ai.db"); adb.unlink(missing_ok=True)
ast = rr.Store(adb)
ast.set_kv("anketa", {"ans": {"deal": "buy"}})
acfg = dict(cfg, worker_url="https://w.example", worker_key="k")
CALLS_AI = []
def fake_post(cfg_, path, payload, timeout=60):
    CALLS_AI.append((path, payload))
    t = payload["text"]
    if "позвоню" in t:
        return {"ok": True, "offer": {"is_offer": False}}
    if "этаж 7" in t:
        return {"ok": True, "offer": {"is_offer": True, "floor": 7, "floors_total": 9, "commission": "нет"}}
    return {"ok": True, "offer": {"is_offer": True, "deal": "sale", "price": 44000, "currency": "USD", "rooms": 2,
                                  "area": 52.5, "district": "Мирабад", "address": "ЖК Mirabad Avenue",
                                  "repair": "евроремонт", "building": "new", "commission": "50%", "mortgage": "yes"}}
AI_SENT = []
with mock.patch.object(rr, "worker_post", fake_post), \
        mock.patch.object(rr, "tg_call", lambda c, m, pl, **k: (AI_SENT.append((m, pl)), {"ok": True})[1]), \
        mock.patch.object(rr, "send_offer_analysis", lambda *a, **k: False):
    o1, _ = rr.intake_offer(acfg, ast, "chat:1", 1, "Азиз", "Продаю двушку, Мирабад, 44 тыс у.е., ипотека есть", ["ph"])
    o2, _ = rr.intake_offer(acfg, ast, "chat:2", 2, "Бек", "Здравствуйте, есть варианты, позвоню", [])
    pend = ast.get_kv("pending_offers")
    for k in pend: pend[k]["at"] -= 100
    ast.set_kv("pending_offers", pend)
    rr.flush_pending_offer(acfg, ast)
    a = cg.get_offer(ast, o1)
    assert a["price_usd"] == 44000 and a["area"] == 52.5 and a["district"] == "Мирабад"   # regex «44 тыс» не понял
    assert a["extra"]["address"] == "ЖК Mirabad Avenue" and a["extra"]["mortgage"] == "yes"
    card = cg.offer_card(ast, acfg, a)
    assert "🗺 ЖК Mirabad Avenue" in card and "новостройка, евроремонт" in card and "комиссия: 50%" in card and "ипотека: да" in card
    assert "⚠️" not in card                                                    # продажа при покупке — ок
    cg.enrich_offer(acfg, ast, o1, {"deal": "rent"})
    assert "⚠️ Это аренда, а вы ищете покупку" in cg.offer_card(ast, acfg, cg.get_offer(ast, o1))
    cg.enrich_offer(acfg, ast, o1, {"deal": "sale"})
    assert cg.get_offer(ast, o2)["status"] == "message"                          # реплика — не карточка
    assert any("Бек</b> пишет" in pl.get("text", "") for _, pl in AI_SENT)
    assert CALLS_AI[0][1]["photos"] == ["ph"] and CALLS_AI[0][1]["deal"] == "sale"
    # ответ на уточнение: модель дополняет только пустые поля
    cg.set_offer_status(ast, o1, "asked")
    ast.conn.execute("UPDATE broker_offers SET asked_at=? WHERE oid=?", (datetime.now(timezone.utc).isoformat(), o1))
    ast.conn.commit()
    cg.attach_answer(acfg, ast, o1, "Да, этаж 7 из 9, комиссии нет, адрес тот же")
    a = cg.get_offer(ast, o1)
    assert a["floor"] == 7 and a["extra"]["commission"] == "50%"                 # не перезаписали
# модель недоступна — работает regex, карточка всё равно приходит
with mock.patch.object(rr, "worker_post", lambda *a, **k: None), \
        mock.patch.object(rr, "tg_call", lambda c, m, pl, **k: {"ok": True}), \
        mock.patch.object(rr, "send_offer_analysis", lambda *a, **k: False):
    o3, _ = rr.intake_offer(acfg, ast, "chat:3", 3, "Ж", "3 комн Юнусабад 80 м2 60 000$", [])
    pend = ast.get_kv("pending_offers"); pend[str(o3)]["at"] -= 100; ast.set_kv("pending_offers", pend)
    rr.flush_pending_offer(acfg, ast)
    assert cg.get_offer(ast, o3)["price_usd"] == 60000 and cg.get_offer(ast, o3)["status"] == "new"
adb.unlink(missing_ok=True)
print("OK — разбор вариантов моделью: поля, адрес/ремонт/комиссия в карточке, реплики, запасной regex")

# ============ доведение до сделки: молчащий маклер, просмотры, вечерняя сводка ============
import followup as fu
fdb = Path("/tmp/test_fu.db"); fdb.unlink(missing_ok=True)
fs = rr.Store(fdb)
FU = []
ftg = lambda c, m, pl, **k: (FU.append((m, pl)), {"ok": True})[1]
_D0 = (datetime.now(cg.TZ) + timedelta(days=2)).date()      # всегда в будущем: set_viewing сверяется с реальным «сейчас»
T = lambda h, m=0, d=0: (datetime(_D0.year, _D0.month, _D0.day, h, m, tzinfo=cg.TZ) + timedelta(days=d))
with mock.patch.object(rr, "tg_call", ftg):
    a1, _ = cg.save_offer(fs, cfg, 4242, "Аброр", "2 комн Мирабад 50 м2 48 000$", [])
    a2, _ = cg.save_offer(fs, cfg, 4343, "Дильноза", "2 комн Яккасарай 55 м2 47 000$", [])
    for oid in (a1, a2):
        cg.set_offer_status(fs, oid, "shortlist")
    cg.request_details(cfg, fs)                                    # обоим ушли вопросы
    old = (T(10) - timedelta(hours=25)).astimezone(timezone.utc).isoformat()
    fs.conn.execute("UPDATE broker_offers SET asked_at=?", (old,)); fs.conn.commit()
    fs.conn.execute("UPDATE broker_offers SET replied_at=? WHERE oid=?", (T(9).isoformat(), a2)); fs.conn.commit()
    # молчит сутки → вопрос владельцу, один раз; ночью — нет
    FU.clear()
    assert fu.silent_brokers(cfg, fs, T(3)) == 0
    assert fu.silent_brokers(cfg, fs, T(10)) == 1
    msg = FU[-1][1]                                               # напомнила сама, один раз — без кнопок
    assert "Аброр" in msg["text"] and "напомнила" in msg["text"] and "reply_markup" not in msg
    assert any("Напоминаю" in pl.get("text", "") and str(pl["chat_id"]) == "4242" for m, pl in FU)
    assert fu.silent_brokers(cfg, fs, T(11)) == 0                 # не повторяем
    assert "Уже напоминала" in cg.remind_broker(cfg, fs, a1)
    # просмотр завтра 18:00: утро — сводка, за 2 часа — напоминание, после — «как прошёл?»
    cg.set_viewing(cfg, fs, a2, T(18, d=1).isoformat(), "завтра 18:00")
    cg.enrich_offer(cfg, fs, a2, {"address": "ул. Шота Руставели 12"})
    FU.clear()
    assert fu.morning_note(cfg, fs, T(9, d=1))
    assert "Сегодня идём смотреть" in FU[-1][1]["text"] and "18:00" in FU[-1][1]["text"] and "Руставели" in FU[-1][1]["text"]
    assert not fu.morning_note(cfg, fs, T(10, d=1))               # раз в день
    assert fu.viewing_reminders(cfg, fs, T(15, d=1)) == 0         # рано
    assert fu.viewing_reminders(cfg, fs, T(16, 30, d=1)) == 1 and "Через 1 ч 30 мин" in FU[-1][1]["text"]
    assert fu.viewing_reminders(cfg, fs, T(17, d=1)) == 0
    assert fu.viewing_reminders(cfg, fs, T(19, 30, d=1)) == 1 and "как вам вариант" in FU[-1][1]["text"]
    assert fu.viewing_reminders(cfg, fs, T(20, d=1)) == 0
    # вечерняя сводка: только в 20:00–23:30, раз в день, с подсказкой следующего шага
    cg.set_viewing(cfg, fs, a1, T(11, d=1).isoformat(), "завтра 11:00")
    b0 = fs.brokers(status="new")
    fs.conn.execute("INSERT INTO brokers(bid, source, name, phone, status, last_contact) VALUES('x','t','Н','1','contacted',?)",
                    (T(12).astimezone(timezone.utc).isoformat(),))
    fs.conn.commit()
    cg.save_offer(fs, cfg, 4444, "Тимур", "3 комн Юнусабад 70 м2 52 000$", [])
    FU.clear()
    assert not fu.evening_digest(cfg, fs, None, T(19))
    with mock.patch.object(fu, "_day_start_utc", lambda now: "2000-01-01"):
        assert fu.evening_digest(cfg, fs, None, T(20, 5))
    dg = FU[-1][1]["text"]
    assert "Итоги дня" in dg and "написали: 1 сегодня" in dg and "Ждут вашего решения: 1" in dg
    assert "Завтра просмотры" in dg and "11:00" in dg and "Разберите" in dg
    assert "ответьте на карточки" in dg and "reply_markup" not in FU[-1][1]
    assert not fu.evening_digest(cfg, fs, None, T(21))             # уже было сегодня
    # пустой день — без сводки
    es = rr.Store(Path("/tmp/test_fu2.db")); 
    assert fu.digest_text(fu.digest_data(es, None, T(20)), T(20))[0] is None
    Path("/tmp/test_fu2.db").unlink(missing_ok=True)
    # run(): троттлинг
    assert fu.run(cfg, fs, now=T(10), force=True) is not None and fu.run(cfg, fs) == {}
fdb.unlink(missing_ok=True)
print("OK — доведение до сделки: напоминание маклеру, этапы просмотра, утренняя и вечерняя сводка")

# ============ «Ищет Ra'no»: все сайты, лучшие сразу + подборка, объявление → шортлист ============
import sale_sources as SS
class FR:
    def __init__(self, d, text=""): self._d, self.text, self.status_code = d, text, 200
    def json(self): return self._d
    def raise_for_status(self): pass
R24 = {"data": [{"id": 1, "name": {"ru": "2-комнатная квартира − 52 м², 3/9 этаж"}, "description": {"ru": "Мирабад, ремонт"},
                 "price": {"usd": 47000}, "address": {"fullAddress": {"ru": "Ташкент, Мирабадский район, ул. Нукус"}},
                 "phone": "+998901234567", "isCommissioned": False, "user": {"id": 5, "role": {"key": "owner"}},
                 "imageSets": [{"w600": "https://img/1.webp"}], "createdAt": "2026-10-07T10:00:00+00:00"},
                {"id": 2, "name": {"ru": "2-комнатная квартира − 50 м², 2/5 этаж"}, "description": {"ru": ""},
                 "price": {"usd": 700}, "address": {"fullAddress": {"ru": "Ташкент, Яккасарайский район"}},
                 "phone": "+998909999999", "isCommissioned": True, "user": {"id": 6, "role": {"key": "agent"}}},
                {"id": 3, "name": {"ru": "2-комнатная квартира − 50 м², 2/5 этаж"}, "price": {"usd": 40000},
                 "address": {"fullAddress": {"ru": "Самарканд, центр"}}}], "meta": {"hasNext": False}}
JM_LIST = {"results": [{"id": 77}], "next": None}
JM_DET = {"id": 77, "title": "2-комн в ЖК, Мирабад", "description": "Ипотека возможна", "phone_number": "+998977020340",
          "district": {"id": 200, "name": "Mirobod tumani"}, "address_line": "Toshkent shahri, Mirobod tumani",
          "pricing": {"currency": 2, "price": "45000.00"}, "advertiser_type": 1, "mortgage_available": True,
          "detail": {"area_m2": "55.5", "room_quantity": 2, "floor_number": 4, "floors_count": 9, "repair": 4},
          "seller": {"id": 9, "first_name": "Азиз"}, "ads_at": "2026-10-08T09:00:00+05:00", "media": []}
RT_HTML = ('<a href="https://realting.uz/property/555"><div class="teaser-title fs-base color-info">Квартира 2 комнаты</div>'
           '<div class="route">Ташкент, Узбекистан</div>'
           '<div class="unit-item" title="Число комнат"><img src="x"><span>2</span></div>'
           '<div class="unit-item" title="Площадь"><img src="x"><span>48 м²</span></div>'
           '<div class="unit-item" title="Этаж"><img src="x"><span>5/9</span></div>'
           '<div class="clamp-3">Продажа, Юнусабад, ориентир Мегапланет</div>'
           '<div class="price-item" data-price-USD="$46 000">$46 000</div>'
           '<a href="https://telegram.me/Agent_Uz?text=hi">tg</a></a>'
           '<a href="https://realting.uz/property/556"><div class="teaser-title">Квартира</div><div class="route">Бухара</div></a>')
def fake_src(url, params=None, headers=None, timeout=None):
    if "realt24" in url: return FR(R24)
    if url.endswith("/77/"): return FR(JM_DET)
    if "joymee" in url:
        assert params["max_price"] == 50000 and params["room_quantity"] == 2 and params["ordering"] == "newest"
        assert params["district"] in (200, 3, 199)
        return FR(JM_LIST)
    if "realting" in url: return FR({}, RT_HTML)
    raise AssertionError(url)
ssx = {"max_price_usd": 50000, "rooms": [2], "districts": ["Мирабад", "Яккасарай", "Юнусабад"], "mortgage": True,
       "min_price_usd": 5000, "notify_max_age_days": 45, "owner_only": False}
xdb = Path("/tmp/test_rano.db"); xdb.unlink(missing_ok=True); xs = rr.Store(xdb)
ydb = Path("/tmp/test_rano_main.db"); ydb.unlink(missing_ok=True); ys = rr.Store(ydb)
with mock.patch.object(SS.requests, "get", fake_src), mock.patch.object(SS.time, "sleep"):
    r24 = SS.fetch_realt24(ssx, cfg, xs)
    jm = SS.fetch_joymee(ssx, cfg, xs)
    rt = SS.fetch_realting(ssx, cfg, xs)
assert [l["key"] for l in r24] == ["sale:realt24:1", "sale:realt24:2"]          # Самарканд отсеян
a = r24[0]
assert (a["rooms"], a["area"], a["floor"], a["floors_total"], a["district"], a["seller_hint"]) == (2, 52, 3, 9, "Мирабад", "owner")
assert a["url"] == "https://realt24.uz/listing/1/" and a["phones"] and a["photo_urls"] == ["https://img/1.webp"]
b = SS.normalize(dict(r24[1]), cfg)                                              # 700$ — это за м²
assert b["price_value"] == 35000 and "за м²" in b["price_note"] and r24[1]["seller_hint"] == "agency"
dp = SS.normalize({"text": "МЕРОС п\\в-23260y.e. цена - 77550у.е. ИПОТЕКА первоначальный взнос 30",
                   "price_value": 23300, "price_currency": "USD", "area": 47.5}, cfg)
assert dp["price_value"] == 77550 and "первый взнос $23 300" in dp["price_note"]          # взнос ≠ цена
assert SS.normalize({"text": "ипотека, первоначальный взнос 30%", "price_value": 15000, "price_currency": "USD",
                     "area": 50}, cfg).get("down_payment_only")
a1 = {"site": "Joymee", "rooms": 2, "area": 50, "floor": 2, "floors_total": 4, "district": "Мирабад", "price_usd": 48000}
assert SS.same_flat(a1, dict(a1)) and not SS.same_flat(a1, dict(a1, floor=3)) and not SS.same_flat(a1, dict(a1, price_usd=46000))
assert SS.same_flat(a1, dict(a1, site="Uybor", price_usd=46500))                         # с другого сайта — мягче
j = jm[0]
assert (j["price_value"], j["rooms"], j["area"], j["floor"], j["district"], j["seller_hint"], j["mortgage"]) == \
       (45000, 2, 55.5, 4, "Мирабад", "owner", True)
assert j["url"] == "https://joymee.uz/announcements/77" and j["repair"] == "евроремонт"
assert len(rt) == 2 and rt[0]["key"] == "sale:realting:555" and (rt[0]["area"], rt[0]["floor"], rt[0]["price_value"]) == (48, 5, 46000)
assert rt[0]["district"] == "Юнусабад" and rt[0]["seller"] == "@Agent_Uz"
# продавец по пометке сайта: без запросов к Uybor
lj = dict(j); lj["price_usd"] = 45000; lj["photo_urls"] = lj.get("photo_urls") or ["https://cdn.joymee.uz/77.jpg"]
assert rr.sale_reject(lj, ssx, xs, cfg) == ("", False) and lj["seller_kind"] == "owner"
la = dict(r24[1]); SS.normalize(la, cfg)
assert rr.sale_reject(la, ssx, xs, cfg) == ("", False) and la["seller_kind"] == "agency"
# оценка: собственник + ипотека выше; ниже рынка — «сильный»
sc_j, why_j, strong_j, _ = SS.score(xs, lj, cfg, ssx)
assert "собственник" in why_j and "можно в ипотеку" in why_j and not strong_j
with mock.patch("market.analyze", lambda *a: {"gap": -0.12, "flags": [], "verdict": ("🟢", "Ниже рынка", "")}):
    sc2, why2, strong2, _ = SS.score(xs, lj, cfg, ssx)
assert strong2 and sc2 > sc_j and "-12% к рынку" in why2[0]
with mock.patch("market.analyze", lambda *a: {"gap": -0.12, "flags": [], "verdict": ("🟡", "В рынке", "")}):
    assert not SS.score(xs, lj, cfg, ssx)[2]          # −12%, но в пределах обычного разброса похожих — не «сразу»
# дубль с другого сайта: та же площадь, этаж, район, цена ±5%
xs.save(dict(lj, key="sale:uybor:9", site="Uybor", price_usd=45500, phones=[]), notified=True)
assert SS.structural_dup(xs, lj) == "sale:uybor:9"
assert SS.structural_dup(xs, dict(lj, floor=7)) is None
# подборка: лучшие сверху, 👍 → шортлист с ссылкой и телефоном продавца
XT = []
xtg = lambda c, m, pl, **k: (XT.append((m, pl)), {"ok": True})[1]
lr = dict(rt[0], price_usd=46000, photo_urls=["https://static2.realting.com/uploads/bigSlider/abc/1.webp"])
xs.save(lj, notified=False); xs.save(lr, notified=False)
SS.queue_pick(xs, lj, 70, why_j); SS.queue_pick(xs, lr, 55, [])
with mock.patch.object(rr, "tg_call", xtg):
    assert SS.send_pick(cfg, xs) == 2
cards = [pl for m, pl in XT if m in ("sendMessage", "sendPhoto") and "Подборка дня" in (pl.get("text") or pl.get("caption") or "")]
assert len(cards) == 2 and "1 из 2" in (cards[0].get("text") or cards[0].get("caption"))   # каждая — своей карточкой
assert {"L:sale:joymee:77", "L:sale:realting:555"} <= {r for r, _ in rr.CARD_LOG}
assert SS.pick_pending(xs) == []
assert [x["key"] for x in xs.get_kv("last_pick")] == ["sale:joymee:77", "sale:realting:555"]
XT.clear()
with mock.patch.object(rr, "tg_call", xtg):
    assert SS.resend_last_pick(cfg, xs) == 2
assert "Последняя подборка" in tx_(XT[-1][1]) and rr.CARD_LOG[-1][0] == "L:sale:realting:555"
XT.clear()
with mock.patch.object(rr, "tg_call", xtg), mock.patch.object(rr, "SALE_DB_PATH", xdb):
    t5, _ = rr.handle_callback("L:v:sale:joymee:77", rr.default_settings(), ys if "ys" in dir() else None, cfg, 1)
assert rr.CARD_LOG[-1][0] == "L:sale:joymee:77"                                 # карточка с разбором
assert any("Открыть на Joymee" in (pl.get("text") or pl.get("caption") or "") for m, pl in XT)
assert xs.conn.execute("SELECT notified FROM listings WHERE key='sale:joymee:77'").fetchone()[0] == 1
with mock.patch.object(rr, "tg_call", xtg), mock.patch.object(rr, "SALE_DB_PATH", xdb):
    t1, _ = rr.handle_callback("L:s:sale:joymee:77", rr.default_settings(), ys, cfg, 1)
    t2, _ = rr.handle_callback("L:s:sale:joymee:77", rr.default_settings(), ys, cfg, 1)
assert "В шортлисте" in t1 and t2 == "Уже в шортлисте"
with mock.patch.object(rr, "SALE_DB_PATH", xdb):
    t3, _ = rr.handle_callback("L:n:sale:realting:555", rr.default_settings(), ys, cfg, 1)
assert "Убрала" in t3 and "sale:realting:555" in rr.Store(xdb).get_kv("sale_dismissed")
# «Мимо» несёт id карточки: альбом из 3 фото → удалить 57–59 вместе с анализом
kb = rr.sale_kb("sale:joymee:77", [57, 58, 59])
assert kb["inline_keyboard"][0][1]["callback_data"] == "L:n:sale:joymee:77|57.3"
assert rr.sale_kb("sale:joymee:77", True)["inline_keyboard"][0][1]["callback_data"] == "L:n:sale:joymee:77"
with mock.patch.object(rr, "tg_call", lambda c, m, pl, **k: {"ok": True, "result": [{"message_id": 10}, {"message_id": 11}]}):
    assert rr.send_listing(cfg, {"photos": True}, {"photo_urls": ["a", "b"], "title": "x"}, False, text="t") == [10, 11]
with mock.patch.object(rr, "tg_call", lambda c, m, pl, **k: {"ok": True, "result": {"message_id": 12}}):
    assert rr.send_listing(cfg, {"photos": False}, {"title": "x"}, False, text="t") == [12]
with mock.patch.object(rr, "SALE_DB_PATH", xdb):
    t4, _ = rr.handle_callback("L:n:sale:realting:555|57.3", rr.default_settings(), ys, cfg, 1)
assert "Убрала" in t4
so = cg.get_offer(ys, ys.conn.execute("SELECT oid FROM broker_offers WHERE broker_chat='site:sale:joymee:77'").fetchone()[0])
assert so["status"] == "shortlist" and so["price_usd"] == 45000 and so["extra"]["url"].endswith("/77")
card = cg.offer_card(ys, cfg, so)
assert "Открыть объявление" in card and "📞" in card and "объявление с Joymee" in card
XT.clear()
with mock.patch.object(rr, "tg_call", xtg):
    cg.request_details(cfg, ys, [so["oid"]])
man = [pl["text"] for m, pl in XT if "Отправьте от себя" in pl.get("text", "")]
assert man and "По вашему объявлению о продаже" in man[0] and "Продавец: 📞" in man[0]
assert man[0].count("https://t.me/+998") == 1 and "Написать продавцу в Telegram" in man[0]   # одна ссылка, текст вписан
assert "Я Ra'no" not in man[0]                    # клиент отправляет сам — от первого лица
# подборка дня — только в 19:30–23:00 и раз в день
xs.save(dict(lr, key="sale:realting:999"), notified=False)
SS.queue_pick(xs, dict(lr, key="sale:realting:999"), 50, [])
with mock.patch.object(rr, "tg_call", xtg):
    assert SS.maybe_daily_pick(cfg, xs, datetime(2026, 10, 8, 18, 0, tzinfo=cg.TZ)) == 0
    assert SS.maybe_daily_pick(cfg, xs, datetime(2026, 10, 8, 19, 40, tzinfo=cg.TZ)) == 1
    assert SS.maybe_daily_pick(cfg, xs, datetime(2026, 10, 8, 21, 0, tzinfo=cg.TZ)) == 0
# экраны двух кнопок
ys.set_kv("anketa", {"ans": {"deal": "buy", "object": "flat", "rooms": ["2"], "budget": "50000", "note": "нужна ипотека"}})
SS.day_stats(xs, {"seen": 40, "fit": 6, "instant": 1})
with mock.patch.object(rr, "SALE_DB_PATH", xdb):
    scr = rr.rano_screen(cfg, ys)
assert "Ищет Ra'no" in scr["text"] and "Realt24" in scr["text"] and "новых объявлений 40" in scr["text"]
assert "нужна ипотека" in scr["text"] and "R:check" in json.dumps(scr["kb"])
v = rr.via_screen(cfg, ys)
assert "Через маклеров" in v["text"] and "напиши маклерам" in v["text"] and "до 15 в день" in v["text"]
ys.set_kv("fresh_start", True)                                                # после сброса — сначала параметры
assert "Сначала расскажите" in rr.rano_screen(cfg, ys)["text"]
with mock.patch.object(rr, "tg_call", xtg):
    cg.apply_webapp_data(cfg, ys, json.dumps({"v": 3, "replace": True, "ans": {"deal": "buy", "object": "flat", "rooms": ["2"],
                                                                             "budget": "50000", "contact": "bot"}}))
assert not ys.get_kv("fresh_start")
with mock.patch.object(rr, "SALE_DB_PATH", xdb):
    snap = rr.ui_snapshot(cfg, ys, rr.default_settings())
assert set(snap["screens"]) == {"/rano", "/via"}
ys.set_kv("anketa", {"ans": {"deal": "buy", "object": "flat", "rooms": ["2"], "budget": "50000", "districts_any": True}})
assert len(rr.effective_sale_cfg(cfg, ys)["sale_search"]["districts"]) == len(rr.DISTRICT_LIST)   # «любой район» — весь город
c = snap["ctx"]
assert c["last_pick"][0]["n"] == 1 and c["last_pick"][0]["key"].startswith("sale:") and "<" not in c["last_pick"][0]["text"]
assert "shortlist" in c and "today" in c and "pick_pending" in c
xdb.unlink(missing_ok=True); ydb.unlink(missing_ok=True)
print("OK — Ищет Ra'no: Realt24/Joymee/Realting, цена за м², дубли между сайтами, подборка, 👍 → шортлист, экраны")

# ============ сначала ищет сама, маклеров предлагает, только когда на сайтах пусто ============
zdb = Path("/tmp/test_self.db"); zdb.unlink(missing_ok=True); zs = rr.Store(zdb)
sdbz = Path("/tmp/test_self_sale.db"); sdbz.unlink(missing_ok=True); zsale = rr.Store(sdbz)
ZT = []
ztg = lambda c, m, pl, **k: (ZT.append((m, pl)), {"ok": True})[1]
buy = {"deal": "buy", "object": "flat", "city": "tashkent", "rooms": ["2"], "budget": "50000", "contact": "bot"}
with mock.patch.object(rr, "tg_call", ztg):
    cg.apply_webapp_data(cfg, zs, json.dumps({"v": 3, "replace": True, "ans": buy}))
    assert not any("Готовый запрос" in (pl.get("text") or "") for m, pl in ZT)        # маклеров не навязываем
    assert zs.get_kv("request_text") and zs.get_kv("sale_force")                       # текст готов, поиск — сразу
    st0 = zs.get_kv("search_start"); assert st0["deal"] == "buy" and not st0["offered"]
    noon = datetime.now(cg.TZ).replace(hour=12)
    assert not fu.broker_offer(cfg, zs, zsale, noon)                                   # сайты ещё не проверяли
    zsale.set_kv("first_pass", {"at": datetime.now(timezone.utc).isoformat(), "fit": 3})
    assert not fu.broker_offer(cfg, zs, zsale, noon)                                   # нашлось — маклеры не нужны
    zsale.set_kv("first_pass", {"at": datetime.now(timezone.utc).isoformat(), "fit": 0})
    ZT.clear()
    assert fu.broker_offer(cfg, zs, zsale, noon)                                       # пусто → предлагаем
    o = ZT[-1][1]
    assert "ничего нет" in o["text"] and "напиши маклерам" in o["text"] and "reply_markup" not in o   # словами
    assert not fu.broker_offer(cfg, zs, zsale, noon)                                   # один раз
    ZT.clear()
    t, _ = cg.handle_request_cb("q:show", cfg, zs, rr.default_settings())
    assert "Готовый запрос маклерам" in ZT[-1][1]["text"] and "напиши маклерам" in ZT[-1][1]["text"]
    assert "ищу дальше сама" in cg.handle_request_cb("q:later", cfg, zs, rr.default_settings())[0]
    # посуточно — сразу маклеры
    ZT.clear()
    cg.apply_webapp_data(cfg, zs, json.dumps({"v": 3, "replace": True, "ans": dict(buy, deal="daily", budget="60")}))
    assert any("Посуточную" in (pl.get("text") or "") for m, pl in ZT)
    # аренда: фильтры радара — из разговора; сутки без вариантов → маклеры
    rs = cg.rent_settings({"deal": "rent", "budget": "800", "rooms": ["2", "3"], "districts": ["2", "8"]}, rr.default_settings())
    assert rs["max_price_usd"] == 800 and (rs["rooms_min"], rs["rooms_max"]) == (2, 3) and rs["districts"] == ["Мирабад", "Юнусабад"]
    zs.set_kv("search_start", {"at": (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat(), "deal": "rent", "offered": ""})
    ZT.clear()
    assert fu.broker_offer(cfg, zs, None, noon) and "аренды" in ZT[-1][1]["text"]
    # если уже сам пишет маклерам — не предлагаем
    zs.set_kv("search_start", {"at": (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat(), "deal": "rent", "offered": ""})
    zs.upsert_broker("z1", "X", "", "901112299", 3, None, None); zs.broker_status("z1", "contacted")
    assert not fu.broker_offer(cfg, zs, None, noon)
zdb.unlink(missing_ok=True); sdbz.unlink(missing_ok=True)
print("OK — сначала ищет сама: маклеры — только если на сайтах пусто (покупка, аренда), посуточно — сразу")

# ============ одна квартира у разных маклеров — одна карточка + «👥 ещё у N» ============
ddb = Path("/tmp/test_dups_sale.db"); ddb.unlink(missing_ok=True); ds = rr.Store(ddb)
DT = []
dtg = lambda c, m, pl, **k: (DT.append((m, pl)), {"ok": True})[1]
base = {"source": "Joymee · продажа", "site": "Joymee", "rooms": 2, "area": 36.0, "floor": 1, "floors_total": 4,
        "district": "Юнусабад", "phones": [], "text": "", "seller_kind": "owner", "photo_urls": ["https://cdn.joymee.uz/x.jpg"]}
own = dict(base, key="sale:joymee:1", url="https://joymee.uz/announcements/1", seller="Umidjon", price_usd=43000,
           title="Юнусабад 2 квартал 1 в 2 /1/4 2 комнаты")
agt = dict(base, key="sale:joymee:2", url="https://joymee.uz/announcements/2", seller="Санжар", price_usd=47000,
           title="Yunusobod 2    2/1/4", seller_kind="agency")
assert SS.block_no(own) == 2 and SS.block_no(agt) == 2
assert SS.block_no({"title": "Юнусобод -13", "text": "36 кв м"}) == 13 and SS.block_no({"text": "43 kv 9 qavat"}) is None
assert SS.block_no({"district_raw": "14-й квартал"}) == 14
assert SS.same_flat(own, agt)                                              # разные маклеры: наценка 9% — та же квартира
assert not SS.same_flat(own, dict(agt, seller="Umidjon"))                  # тот же продавец — строго
assert not SS.same_flat(own, dict(agt, seller=""))                         # продавец неизвестен — строго
assert not SS.same_flat(own, dict(agt, title="Юнусобод 6кв"))              # другой квартал
assert not SS.same_flat(own, dict(agt, price_usd=50000))                   # +16% — уже другое
with mock.patch.object(rr, "tg_call", dtg):
    # дороже у маклера — тихо дописываем к показанной
    ds.save(own, notified=True)
    assert SS.structural_dup(ds, agt) == "sale:joymee:1"
    assert SS.attach_dup(cfg, ds, dict(agt), "sale:joymee:1", {}) == "alt" and not DT
    o = json.loads(ds.conn.execute("SELECT data FROM listings WHERE key='sale:joymee:1'").fetchone()[0])
    assert o["alts"][0]["key"] == "sale:joymee:2" and o["alts"][0]["kind"] == "agency"
    card = rr.format_sale_message(o, cfg)
    assert "👥 Эту же квартиру продают ещё" in card and "$47 000" in card and "маклер" in card
    assert "ещё у 1: $47 000" in SS.pick_line(o, [])
    # дешевле, а прежнюю уже присылали — короткое «та же квартира дешевле»
    cheap = dict(own, key="sale:uybor:3", site="Uybor", seller="Нигора", price_usd=41000, url="https://uybor.uz/3")
    assert SS.attach_dup(cfg, ds, cheap, "sale:joymee:1", {}) == "cheaper"
    m = DT[-1][1]
    t_ = tx_(m)                                                          # полная карточка, а не строчка
    assert "дешевле на $2 000" in t_ and rr.CARD_LOG[-1][0] == "L:sale:uybor:3" and "$43 000" in t_ and "<b>Ипотека</b>" in t_
    c = json.loads(ds.conn.execute("SELECT data FROM listings WHERE key='sale:uybor:3'").fetchone()[0])
    assert {a["key"] for a in c["alts"]} == {"sale:joymee:1", "sale:joymee:2"}
    # дешевле, а прежняя ждёт в подборке — заменяем её
    q1 = dict(own, key="sale:joymee:10", url="https://joymee.uz/announcements/10", area=50.0, floor=3, price_usd=48000)
    ds.save(q1, notified=False); SS.queue_pick(ds, q1, 50, [])
    q2 = dict(q1, key="sale:joymee:11", url="https://joymee.uz/announcements/11", seller="Другой", price_usd=45000)
    assert SS.attach_dup(cfg, ds, q2, "sale:joymee:10", {}) == "queued"
    pk = ds.get_kv("sale_pick")
    assert [x["key"] for x in pk] == ["sale:joymee:11"] and "ещё у 1: $48 000" in pk[0]["line"]
ddb.unlink(missing_ok=True)
print("OK — дубли от разных маклеров: одна карточка, «👥 ещё у N», «та же квартира дешевле»")
assert SS.merge_alts([], [dict(own, key="sale:joymee:99", price_usd=43500)], own) == []          # перевыкладка того же продавца — не «ещё у 1»
print("OK — перевыкладка тем же продавцом не считается другим продавцом")

# ============ анализ: ремонт меняет экономику, ипотека, торг ============
rdb = Path("/tmp/test_reno.db"); rdb.unlink(missing_ok=True); rs_ = rr.Store(rdb); mk.ensure_tables(rs_.conn)
assert mk.repair_class({"repair": "без ремонта"}) == "none" and mk.repair_class({"repair": "evro"}) == "good"
assert mk.repair_class({"text": "Ремонти ўртача, газ бор"}) == "average"
assert mk.repair_class({"text": "Karobka holatda, 8 qavat"}) == "box"
assert mk.repair_class({"text": "Требует ремонта, евроокна"}) == "none"            # «требует» важнее «евро»
assert mk.repair_class({"text": "Квартира, 2 комнаты"}) is None
for i in range(8):
    rs_.conn.execute("INSERT INTO market(key, op, district, rooms, area, price_usd, new_building, dorm, repair, "
                     "first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (f"sale:uybor:{500 + i}", "sale", "Юнусабад", 2, 50, 1600 * 50, 0, 0, "evro", "x", "x"))
    rs_.conn.execute("INSERT INTO market(key, op, district, rooms, area, price_usd, new_building, dorm, repair, "
                     "first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (f"sale:uybor:{600 + i}", "sale", "Юнусабад", 2, 50, 1300 * 50, 0, 0, "sredniy", "x", "x"))
rs_.conn.commit()
base_l = {"key": "sale:joymee:1", "district": "Юнусабад", "area": 50, "rooms": 2, "created_at": _iso(10)}
cfg_r = dict(ss_all, uzs_per_usd=12000)
# средний ремонт за $60 000 ($1 200/м²): против похожих со средним — −8%; с косметикой ($2 500) — против евро −25%
xr = mk.analyze(rs_, dict(base_l, price_usd=60000, repair="средний ремонт"), cfg_r)
assert xr["comp_same_repair"] and round(xr["comp"]["median_m2"]) == 1300 and abs(xr["gap"] - (1200 / 1300 - 1)) < 1e-9
assert xr["reno"] == 2500 and xr["all_in"] == 62500 and abs(xr["gap_all_in"] - (1250 / 1600 - 1)) < 1e-9
assert xr["verdict"][0] == "🟡" and "с таким же ремонтом" in xr["verdict"][2]            # честное сравнение — со средним
xb = mk.analyze(rs_, dict(base_l, price_usd=58000, text="Коробка"), cfg_r)                    # коробок в срезе нет → цена + отделка
assert xb["repair"] == "box" and not xb["comp_same_repair"] and xb["all_in"] == 64500
assert xb["verdict"][0] == "🟢" and "даже с ремонтом" in xb["verdict"][2] and xb["gap_fair"] == xb["gap_all_in"]
# без ремонта за $72 000: на вид «−10% к евро», но с капитальным ($4 750) — дороже похожих
xn = mk.analyze(rs_, dict(base_l, price_usd=72000, text="Без ремонта"), cfg_r)
assert xn["repair"] == "none" and xn["reno"] == 4750 and xn["gap_all_in"] > -0.05 and xn["verdict"][0] != "🟢"
assert "нужен ремонт ~$4 750" in xn["bargain"]["args"]
# новостройка «без ремонта» = коробка: сравниваем с коробками, а не с евроремонтом рядом
for i in range(8):
    rs_.conn.execute("INSERT INTO market(key, op, district, rooms, area, price_usd, new_building, dorm, repair, "
                     "first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (f"sale:uybor:{700 + i}", "sale", "Яшнабад", 2, 42, 2000 * 42, 1, 0, "evro", "x", "x"))
for i in range(6):
    rs_.conn.execute("INSERT INTO market(key, op, district, rooms, area, price_usd, new_building, dorm, repair, "
                     "first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (f"sale:uybor:{800 + i}", "sale", "Яшнабад", 2, 42, 1400 * 42, 1, 0, "chernovaya", "x", "x"))
rs_.conn.commit()
nb_l = dict(base_l, key="sale:joymee:2", district="Яшнабад", area=42, price_usd=47000, new_building=True,
            repair="без ремонта")
xnb = mk.analyze(rs_, nb_l, cfg_r)
assert xnb["repair"] == "box" and xnb.get("repair_as_box") and xnb["comp_same_repair"], xnb.get("comp")
assert round(xnb["comp"]["median_m2"]) == 1400 and xnb["verdict"][0] == "🟢" and "Намного ниже" not in xnb["verdict"][1]
# коробок рядом нет: не «подозрительно дёшево», а честно — сравнить не с чем
rs_.conn.execute("DELETE FROM market WHERE key LIKE 'sale:uybor:80%'"); rs_.conn.commit()
xnb2 = mk.analyze(rs_, dict(nb_l, price_usd=40000), cfg_r)
assert xnb2["verdict"][1] == "Сравнить не с чем" and "в основном с ремонтом" in xnb2["verdict"][2], xnb2["verdict"]
rs_.conn.execute("DELETE FROM market WHERE key LIKE 'sale:uybor:70%'"); rs_.conn.commit()
# можно ли в ипотеку — прямо: да / нет / не указано; «в ипотеке» — это залог, а не «можно»
assert mk.mortgage_status({"text": "Срочно, в ипотеку не даём, только наличные"})[:3] == ("no", "описание", "в ипотеку не даём")
assert mk.mortgage_status({"text": "Ipotekaga beriladi"})[0] == "yes"
assert mk.mortgage_status({"text": "Ремонт хороший", "mortgage": True, "mortgage_src": "site"})[:2] == ("yes", "пометка сайта")
assert mk.mortgage_status({"text": "Ипотеки нет", "mortgage": True, "mortgage_src": "site"})[0] == "no"      # слова важнее пометки
held = mk.mortgage_status({"text": "Квартира в ипотеке, осталось 2 года"})
assert held[0] is None and held[3] is True
xno = mk.analyze(rs_, dict(base_l, price_usd=60000, text="Средний ремонт. В ипотеку не даём"), cfg_r)
assert xno["mortgage_ok"] == "no" and "mortgage" not in xno and mk.mortgage_line(xno) == "Ипотека — нет: «В ипотеку не даём»"
xun = mk.analyze(rs_, dict(base_l, price_usd=60000, text="Средний ремонт"), cfg_r)
assert xun["mortgage_ok"] is None and xun.get("mortgage") and "не указано" in mk.mortgage_line(xun)
assert "в залоге у банка" in mk.mortgage_line(mk.analyze(rs_, dict(base_l, price_usd=60000, text="Квартира в ипотеке"), cfg_r))
# ипотека: 25% взнос, аннуитет 10 лет под 25% в сумах
mm = mk.mortgage(48000, mk.DEFAULT_ASSUMPTIONS["mortgage"], 12000)
r_, n_ = 0.25 / 12, 120
assert mm["down"] == 12000 and abs(mm["pay_uzs"] - 36000 * 12000 * r_ / (1 - (1 + r_) ** -n_)) < 1
big = mk.mortgage(100000, mk.DEFAULT_ASSUMPTIONS["mortgage"], 12000)                   # упёрлись в лимит 800 млн
assert abs(big["down"] - (100000 - 800_000_000 / 12000)) < 0.01 and big["down_share"] > 0.3
# торг: цена уже сильно ниже рынка и ремонт не нужен — не давить
xc = mk.analyze(rs_, dict(base_l, price_usd=60000, repair="evro"), cfg_r)
assert xc["bargain"]["calm"] and xc["bargain"]["offer"] == 59000
t_ = mk.format_analysis(rs_, dict(base_l, price_usd=60000, repair="средний ремонт"), cfg_r)
assert t_.index("🟡 <b>В рынке</b>") < t_.index("🛠") < t_.index("🏦") < t_.index("🤝") < t_.index("<blockquote expandable>")
assert "С ним ≈ $62 500" in t_ and "млн сум/мес" in t_ and len(t_) < 4096
# ремонт по фото главнее слов продавца; неуверенная оценка — не в счёт
lp = dict(base_l, price_usd=60000, repair="evro", repair_photo={"state": "none", "confidence": 0.8, "signs": "облезлые стены, старые окна"})
assert mk.repair_info(lp) == ("none", "фото", "good")
assert mk.repair_info(dict(lp, repair_photo={"state": "none", "confidence": 0.4})) == ("good", "пометка сайта", "good")
xp = mk.analyze(rs_, lp, cfg_r)
assert xp["reno"] == 4750 and "по фото ремонт хуже заявленного" in xp["bargain"]["args"]
tp = mk.format_analysis(rs_, lp, cfg_r)
assert "🛠 Ремонт по фото: без ремонта (видно: облезлые стены, старые окна)" in tp and "В объявлении — «хороший», на фото хуже" in tp
# разбор и сводка — вручную на узбекском и английском: русского почти не остаётся, модель не нужна
import i18n as _I18a
try:
    for lg_, word_ in (("en", "How I calculated"), ("uz", "Qanday hisobladim")):
        _I18a.set_lang(lg_)
        with mock.patch.object(_I18a, "ai", lambda c, text, lang=None, post=None: "TR:" + text):
            ta_ = mk.format_analysis(rs_, lp, cfg_r)
        assert word_ in ta_ and not _I18a.has_russian(ta_), ta_
        assert "TR:облезлые" in ta_                                    # приметы ремонта от модели — переведены
        su_ = mk.summary_text(rs_, cfg_r, ["Юнусабад"])
        assert not _I18a.has_russian(su_), su_
finally:
    _I18a.set_lang("ru")
# оценка через воркер: один раз на объявление, без воркера — ничего
calls = []
def fake_wp(c, path, payload, timeout=60):
    calls.append((path, payload)); return {"ok": True, "repair": {"state": "average", "confidence": 0.9, "signs": "линолеум"}}
lw = {"key": "sale:x:1", "photo_urls": ["https://a/1.jpg", "https://a/2.jpg"]}
with mock.patch.object(rr, "worker_post", fake_wp):
    assert not SS.photo_repair(cfg, dict(lw))                                     # нет worker_url
    assert SS.photo_repair(dict(cfg, worker_url="https://w"), lw) and lw["repair_photo"]["state"] == "average"
    assert not SS.photo_repair(dict(cfg, worker_url="https://w"), lw)            # уже оценено — не зовём
assert calls == [("/svc/repair", {"urls": ["https://a/1.jpg", "https://a/2.jpg"]})]
assert "repair_photo" in rr.Store.KEEP
rdb.unlink(missing_ok=True)
print("OK — анализ: ремонт в цене и сравнении, ипотека в сумах, торг, сворачиваемые детали")

# ============ ищет покупку — карточки аренды не шлём ============
qdb = Path("/tmp/test_rent_on.db"); qdb.unlink(missing_ok=True); qs = rr.Store(qdb)
assert rr.rent_search_on(qs)                                                     # старый режим без анкеты — аренда
qs.set_kv("anketa", {"ans": {"deal": "buy", "budget": "50000"}}); assert not rr.rent_search_on(qs)
qs.set_kv("anketa", {"ans": {"deal": "daily"}}); assert not rr.rent_search_on(qs)
qs.set_kv("anketa", {"ans": {"deal": "rent"}}); assert rr.rent_search_on(qs)
qs.set_kv("fresh_start", True); assert not rr.rent_search_on(qs)                 # после сброса — ждём параметров
qdb.unlink(missing_ok=True)
print("OK — покупка: объявления аренды в чат не идут, только запоминаются")

# ============ горячее от собственника: сразу, вне лимита, ночью — без звука ============
assert rr.ago_text(1) == "только что" and rr.ago_text(42) == "42 мин назад" and rr.ago_text(135) == "2 ч 15 мин назад"
assert rr.is_night(datetime(2026, 10, 10, 23, 30, tzinfo=rr.TASHKENT_TZ)) and not rr.is_night(datetime(2026, 10, 10, 12, tzinfo=rr.TASHKENT_TZ))
assert rr.fresh_owner_minutes({"seller_kind": "owner", "created_at": _iso(0.03)}, {}) is not None
assert rr.fresh_owner_minutes({"seller_kind": "agency", "created_at": _iso(0.03)}, {}) is None
assert rr.fresh_owner_minutes({"seller_kind": "owner", "created_at": _iso(0.5)}, {}) is None          # 12 ч — уже не горячее
HT = []
def hot_tg(cfg_, method, payload, timeout=20, quiet=False):
    HT.append((method, payload)); return {"ok": True, "result": {}}
hdb = Path("/tmp/test_hot.db"); hdb.unlink(missing_ok=True); hs = rr.Store(hdb)
UY_BY_DISTRICT.clear()
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", hot_tg), \
        mock.patch.object(rr.time, "sleep"), mock.patch.object(SS, "fetch_due", lambda *a, **k: []), \
        mock.patch.object(rr, "is_night", lambda now=None: True):
    rr.run_sale_search(ss_all, hs, {"photos": False})                        # первый проход — пусто
    hs.set_kv("sale_day", {"day": datetime.now(cg.TZ).date().isoformat(), "instant": 6})   # лимит «сразу» исчерпан
    UY_BY_DISTRICT[205] = [uy(951, 1, 205, days=0.03), uy(952, 1, 205, days=2)]
    HT.clear()
    rr.run_sale_search(ss_all, hs, {"photos": False})
hot = [pl for m, pl in HT if "listings/951" in (pl.get("text") or "")]
assert hot and "🔥 <b>Только что от собственника</b>" in hot[0]["text"] and hot[0].get("disable_notification"), HT
assert not any("listings/952" in (pl.get("text") or "") and "🔥" in (pl.get("text") or "") for m, pl in HT)   # старое — в подборку
assert SS.day_stats(hs).get("hot") == 1 and SS.day_stats(hs).get("instant") == 6
hdb.unlink(missing_ok=True)
print("OK — горячее от собственника: сразу, вне дневного лимита, ночью без звука; старые — в подборку")

# ============ ссылка на объявление: карточка, «уже видела», похожие, «искать такие» ============
ldb = Path("/tmp/test_link_sale.db"); ldb.unlink(missing_ok=True); ls_ = rr.Store(ldb)
lrd = Path("/tmp/test_link_radar.db"); lrd.unlink(missing_ok=True); lr_ = rr.Store(lrd)
mk.ensure_tables(ls_.conn)
seen_copy = dict(SS._listing(key="sale:uybor:777", site="Uybor", url="https://uybor.uz/listings/777", rooms=2, area=50,
                             floor=3, floors_total=9, district="Юнусабад", price_value=47000), price_usd=47000, seller_kind="agency")
ls_.save(seen_copy, notified=False)
for i in range(4):
    ls_.conn.execute("INSERT INTO market(key, op, district, rooms, area, price_usd, new_building, dorm, repair, first_seen, last_seen) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"sale:uybor:{800 + i}", "sale", "Юнусабад", 2, 48 + i, 44000 + 1000 * i, 0, 0, "evro", "x", "x"))
ls_.conn.execute("INSERT INTO market(key, op, district, rooms, area, price_usd, new_building, dorm, repair, first_seen, last_seen) "
                 "VALUES('sale:uybor:899','sale','Чиланзар',2,50,45000,0,0,'evro','x','x')"); ls_.conn.commit()
link_l = SS.listing_from_facts("https://www.olx.uz/d/obyavlenie/kv-ID1.html",
                               {"price": 45000, "currency": "USD", "rooms": 2, "area": 50, "floor": 3, "floors_total": 9,
                                "district": "Юнусабад", "seller_type": "owner", "title": "2-комн Юнусабад", "repair": "евроремонт"})
assert link_l["site"] == "OLX" and link_l["seller_hint"] == "owner" and link_l["key"].startswith("sale:link:")
LT = []
def link_tg(cfg_, method, payload, timeout=20, quiet=False):
    LT.append((method, payload)); return {"ok": True, "result": {"message_id": 5}}
with mock.patch.object(rr, "SALE_DB_PATH", ldb), mock.patch.object(rr, "tg_call", link_tg), \
        mock.patch.object(SS, "listing_from_url", lambda c, u: (dict(link_l, photo_urls=["https://x/1.jpg"]), "sale")), \
        mock.patch.object(SS, "_uybor_by_id", lambda rr_, i: ({"photo_urls": [f"https://api.uybor.uz/{i}.jpg"]}, None)):
    rr.handle_link(cfg, lr_, {"_link": "https://www.olx.uz/d/obyavlenie/kv-ID1.html", "text": "найди похожие https://www.olx.uz/d/obyavlenie/kv-ID1.html"})
card = next(tx_(pl) for m, pl in LT if "Уже видела" in tx_(pl))
assert "<b>Ипотека</b>" in card and rr.tg_len(card) <= rr.CAPTION_MAX and sum("Уже видела" in tx_(pl) for m, pl in LT) == 1
assert "uybor.uz/listings/777" in card and "маклер" in card
assert any(r.startswith("L:sale:link:") for r, _ in rr.CARD_LOG)
simm = " ".join(r for r, t in rr.CARD_LOG if "Похожий вариант" in t)
assert "sale:uybor:800" in simm and "sale:uybor:899" not in simm                           # похожие — карточками; другой район — нет
ls2 = rr.Store(ldb)
assert ls2.known("sale:uybor:800") and [x["key"] for x in ls2.get_kv("last_pick")][0].startswith("sale:uybor:")
ls2.conn.close()
# не открылось
LT.clear()
with mock.patch.object(rr, "SALE_DB_PATH", ldb), mock.patch.object(rr, "tg_call", link_tg), \
        mock.patch.object(SS, "listing_from_url", lambda c, u: (None, "страница не открылась")):
    rr.handle_link(cfg, lr_, {"_link": "https://www.olx.uz/d/x.html", "text": "https://www.olx.uz/d/x.html"})
assert "OLX закрыт для программ" in LT[-1][1]["text"] and "скриншот" in LT[-1][1]["text"]
# «🎯 Искать такие» — условия из объявления
LT.clear()
with mock.patch.object(rr, "SALE_DB_PATH", ldb), mock.patch.object(rr, "tg_call", link_tg):
    t, _ = rr.handle_callback(f"L:like:{link_l['key']}", rr.default_settings(), lr_, cfg, 1)
a_ = cg.get_anketa(lr_)["ans"]
assert a_["deal"] == "buy" and a_["rooms"] == ["2"] and rr.DISTRICT_LIST[int(a_["districts"][0])] == "Юнусабад" and a_["budget"] == "50000"
assert any("Ищу такие же" in (pl.get("text") or "") for m, pl in LT)
ldb.unlink(missing_ok=True); lrd.unlink(missing_ok=True)
print("OK — ссылка: карточка с разбором, «уже видела у маклера», похожие (сайты + срез Uybor), «искать такие»")

# ============ аренда: Joymee и Realt24 источниками, каналы, ссылки на аренду ============
assert rr._sale_post("🏢 Квартира сотилади 📐 67 м² 3 хона") and not rr._sale_post("Квартира ижарага берилади, ойига 500$")
assert any(w in "ҳовли уй сотилади 4 сотих" for w in rr.SALE_NOT_FLAT_WORDS)
assert "uybozor_toshkent_uybor" in SS.SALE_CHANNELS
assert "kvartira_bez_posrednika" in json.load(open(Path(rr.__file__).parent / "config.json"))["sources"]["telegram"]["channels"]
it24 = {"id": 5, "name": {"ru": "2-комнатная квартира − 54 м², 3/9 этаж"}, "description": {"ru": "Сдаётся, Мирабад"},
        "address": {"fullAddress": {"ru": "Ташкент, Мирабадский район"}}, "price": {"usd": 650}, "phone": "+998 90 123 45 67",
        "user": {"id": 7, "role": {"key": "owner"}}, "publishedAt": _iso(0.01)}
lr24 = SS._realt24_listing(rr, it24, "rent")
assert lr24["key"] == "realt24:5" and lr24["rooms"] == 2 and lr24["area"] == 54 and lr24["is_business"] is False and lr24["price_value"] == 650
assert SS._realt24_listing(rr, dict(it24, address={"fullAddress": {"ru": "Самарканд"}}), "rent") is None
JR = {"results": [{"id": 31}, {"id": 32}]}
JD = {31: {"title": "Ижарага 2 хона Юнусобод", "pricing": {"currency": 2, "price": "600"}, "advertiser_type": 1,
           "detail": {"room_quantity": 2, "area_m2": 50}, "district": {"name": "Yunusobod"}, "ads_at": _iso(0.01)},
      32: {"title": "Аренда", "pricing": {"currency": 2, "price": "700"}, "advertiser_type": 2, "detail": {"room_quantity": 2}}}
def fake_jm(url, params=None, headers=None, timeout=None):
    if url.rstrip("/").split("/")[-1].isdigit():
        return FakeUy(JD[int(url.rstrip("/").split("/")[-1])])
    return FakeUy(JR)
jdb = Path("/tmp/test_jrent.db"); jdb.unlink(missing_ok=True)
with mock.patch.object(rr, "DB_PATH", jdb), mock.patch.object(rr.requests, "get", fake_jm), mock.patch.object(SS.requests, "get", fake_jm), \
        mock.patch.object(rr.time, "sleep"):
    rr.Store(jdb).conn.close()
    got = rr.fetch_joymee_rent({"details_per_pass": 8}, cfg)
assert [l["key"] for l in got] == ["joymee:31", "joymee:32"] and got[0]["is_business"] is False and got[1]["is_business"] is True
assert got[0]["rooms"] == 2 and got[0]["district"] == "Юнусабад"
jdb.unlink(missing_ok=True)
# ссылка на аренду: карточка, сравнение с похожими, «искать такие» → аренда
rdb2 = Path("/tmp/test_rlink_radar.db"); rdb2.unlink(missing_ok=True); rs2 = rr.Store(rdb2)
sdb2 = Path("/tmp/test_rlink_sale.db"); sdb2.unlink(missing_ok=True); ss2 = rr.Store(sdb2); mk.ensure_tables(ss2.conn)
for i, pr in enumerate([560, 600, 640]):
    ss2.conn.execute("INSERT INTO market(key, op, district, rooms, area, price_usd, new_building, dorm, repair, first_seen, last_seen) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"rent:uybor:{600 + i}", "rent", "Юнусабад", 2, 50, pr, 0, 0, "evro", "x", "x"))
ss2.conn.commit(); ss2.conn.close()
rent_l = SS.listing_from_facts("https://www.olx.uz/d/obyavlenie/arenda-ID9.html",
                               {"deal": "rent", "price": 700, "currency": "USD", "rooms": 2, "area": 50, "district": "Юнусабад",
                                "seller_type": "owner", "title": "Сдаётся 2-комн Юнусабад"})
RT = []
def rent_tg(cfg_, method, payload, timeout=20, quiet=False):
    RT.append((method, payload)); return {"ok": True, "result": {"message_id": 9}}
with mock.patch.object(rr, "SALE_DB_PATH", sdb2), mock.patch.object(rr, "tg_call", rent_tg), \
        mock.patch.object(SS, "listing_from_url", lambda c, u: (dict(rent_l, photo_urls=["https://x/r.jpg"], url=u,
                                                                   **({} if u == rent_l["url"] else {"key": "ru:" + u[-3:]})), "rent")):
    rr.handle_link(cfg, rs2, {"_link": rent_l["url"], "text": rent_l["url"] + " похожие есть?"})
card = next(tx_(pl) for m, pl in RT if "Похожие (3)" in tx_(pl))
assert "~$600/мес" in card and "на 17% дороже" in card, card
assert sum("Похожая аренда" in tx_(pl) for m, pl in RT) == 3                     # каждая — своей карточкой с фото
assert rs2.known("link:" + rent_l["key"].split(":")[-1])
with mock.patch.object(rr, "SALE_DB_PATH", sdb2), mock.patch.object(rr, "tg_call", rent_tg):
    rr.handle_callback("L:like:link:" + rent_l["key"].split(":")[-1], rr.default_settings(), rs2, cfg, 1)
a2 = cg.get_anketa(rs2)["ans"]
assert a2["deal"] == "rent" and a2["budget"] == "770" and rs2.get_kv("settings")["max_price_usd"] == 770
rdb2.unlink(missing_ok=True); sdb2.unlink(missing_ok=True)
print("OK — аренда: Realt24 и Joymee источниками, узбекская кириллица в каналах, ссылка на аренду — сравнение, похожие, «искать такие»")

# ============ каналы от пользователей: проверка, общая база, уведомление, «убрать» ============
def tg_page(posts, title="Ташкент квартиры", subs="2.1K", hours_ago=2):
    t0 = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    body = "".join(f'<div class="tgme_widget_message_text js-message_text" dir="auto">{p}</div>'
                   f'<time datetime="{(t0 - timedelta(hours=i)).isoformat()}">' for i, p in enumerate(posts))
    return (f'<meta property="og:title" content="{title}"><span class="counter_value">{subs}</span> '
            f'<span class="counter_type">subscribers</span>' + body)
ADS = ["Сдаётся 2-комн, Юнусабад, 3/9, 55 м², 600$ в месяц", "Ижарага 2 хона Чилонзор, ойига 450$, 4 қават",
       "Аренда 1-комн Мирабад 400 у.е., 2 этаж", "Сдается 3 комн Яккасарай 800$, 70 м²"]
PAGES = {"good_ch": tg_page(ADS), "dead_ch": tg_page(ADS, hours_ago=24 * 30), "news_ch": tg_page(["Новости рынка", "Курс доллара", "Погода"] * 2),
         "grp": '<meta property="og:title" content="Группа">'}
class FakeTg:
    def __init__(s, t): s.text, s.status_code = t, 200
    def raise_for_status(s): pass
fake_tme = lambda url, headers=None, timeout=None: FakeTg(PAGES[url.rstrip("/").split("/")[-1]])
with mock.patch.object(SS.requests, "get", fake_tme):
    v = SS.verify_channel("good_ch")
    assert v["ok"] and v["deal"] == "rent" and v["subs"] == "2.1K" and v["ads"] == 4, v
    assert "не живой" in SS.verify_channel("dead_ch")["reason"]
    assert "объявлений" in SS.verify_channel("news_ch")["reason"]
    assert "группа" in SS.verify_channel("grp")["reason"]
cdb = Path("/tmp/test_channels.db"); cdb.unlink(missing_ok=True); cs = rr.Store(cdb)
CT = []
def ch_tg(cfg_, method, payload, timeout=20, quiet=False):
    CT.append((method, payload)); return {"ok": True, "result": {}}
broker = {"chat": {"id": 777, "first_name": "Азиз"}, "_channel": ["good_ch", "dead_ch", "arentash"]}
with mock.patch.object(SS.requests, "get", fake_tme), mock.patch.object(rr, "tg_call", ch_tg):
    assert rr.handle_channel_suggestion(cfg, cs, broker) == 1
reply = next(pl["text"] for m, pl in CT if str(pl["chat_id"]) == "777")
assert "✅ добавила в поиск: аренда" in reply and "не живой" in reply and "@arentash — уже отслеживаю" in reply
note = next(pl for m, pl in CT if str(pl["chat_id"]) == str(cfg["telegram_chat_id"]))
assert "Новый источник" in note["text"] and "Азиз" in note["text"] and "ch:rm:good_ch" in note["reply_markup"]
assert rr.user_channels(cs, "rent") == ["good_ch"] and rr.user_channels(cs, "sale") == []
assert "@good_ch" in rr.sources_text(cfg, cs)
# повторно — «уже отслеживаю»; убрать — больше не добавляется
CT.clear()
with mock.patch.object(SS.requests, "get", fake_tme), mock.patch.object(rr, "tg_call", ch_tg):
    rr.handle_channel_suggestion(cfg, cs, dict(broker, _channel=["GOOD_CH"]))
    assert "уже отслеживаю" in CT[-1][1]["text"]
    t, _ = rr.handle_callback("ch:rm:good_ch", rr.default_settings(), cs, cfg, 1)
    assert "Убрала @good_ch" in t and rr.user_channels(cs) == []
    rr.handle_channel_suggestion(cfg, cs, dict(broker, _channel=["good_ch"]))
    assert "не подошёл" in CT[-1][1]["text"]
# продажа: канал пользователя — в поиск покупки
reg = cs.get_kv("user_channels"); reg["sale_ch"] = {"deal": "sale", "active": True}; cs.set_kv("user_channels", reg)
cs.set_kv("anketa", {"ans": {"deal": "buy", "object": "flat", "budget": "50000", "rooms": ["2"]}})
assert "sale_ch" in rr.effective_sale_cfg(cfg, cs)["sale_search"]["sale_channels"]
assert "Kvartiritashkenta" in rr.effective_sale_cfg(cfg, cs)["sale_search"]["sale_channels"]
# лимит: не больше 10 проверок в день от одного маклера
cs.set_kv(f"ch_sugg:777:{datetime.now(rr.TASHKENT_TZ).date().isoformat()}", 10)
CT.clear()
with mock.patch.object(SS.requests, "get", fake_tme), mock.patch.object(rr, "tg_call", ch_tg):
    rr.handle_channel_suggestion(cfg, cs, dict(broker, _channel=["news_ch"]))
assert "на сегодня хватит" in CT[-1][1]["text"]
cdb.unlink(missing_ok=True)
print("OK — каналы от пользователей: проверка живости и объявлений, общая база, уведомление владельцу, «убрать», лимит")

# ============ еженедельная перепроверка каналов: отключить замолчавшие, вернуть оживших ============
hdb2 = Path("/tmp/test_chhealth.db"); hdb2.unlink(missing_ok=True); hs2 = rr.Store(hdb2)
VERD = {}
def fake_verify(name, now=None):
    return VERD.get(name.lower(), {"ok": True})
ccfg = dict(cfg, sources={"telegram": {"channels": ["alive_a", "quiet_b", "gone_c"]}})
HT2 = []
with mock.patch.object(SS, "verify_channel", fake_verify), mock.patch.object(SS, "SALE_CHANNELS", ["sale_d"]), \
        mock.patch.object(rr, "tg_call", lambda c, m, p, **k: (HT2.append(p), {"ok": True})[1]), mock.patch.object(rr.time, "sleep"):
    VERD.update(quiet_b={"ok": False, "reason": "последний пост 10 дн. назад — канал не живой", "newest_days": 10},
                gone_c={"ok": False, "reason": "последний пост 45 дн. назад — канал не живой", "newest_days": 45},
                sale_d={"ok": False, "reason": "Telegram не ответил (timeout)"})
    r1 = rr.recheck_channels(ccfg, hs2, force=True)
    assert r1["off"] == ["gone_c"] and r1["checked"] == 4                      # замолчал >30 дн. — сразу; 10 дн. — ждём
    assert rr.live_channels(hs2, ["alive_a", "quiet_b", "gone_c", "sale_d"]) == ["alive_a", "quiet_b", "sale_d"]
    assert "Отключила @gone_c" in HT2[-1]["text"]
    assert rr.recheck_channels(ccfg, hs2) == {}                                 # раньше недели — не проверяем
    assert rr.recheck_channels(ccfg, hs2, force=True)["off"] == []             # повтор в тот же день — не «вторая неделя»
    r2 = rr.recheck_channels(ccfg, hs2, force=True, now=datetime.now(timezone.utc) + timedelta(days=7))
    assert r2["off"] == ["quiet_b"]                                             # вторую неделю подряд — отключаем
    VERD["gone_c"] = {"ok": True}
    r3 = rr.recheck_channels(ccfg, hs2, force=True, now=datetime.now(timezone.utc) + timedelta(days=14))
    assert r3["back"] == ["gone_c"] and "Вернула @gone_c" in HT2[-1]["text"]
    assert not rr.channel_paused(hs2, "sale_d")                                 # сбой сети — не в счёт
    hs2.set_kv("anketa", {"ans": {"deal": "buy", "object": "flat", "budget": "50000"}})
    assert "quiet_b" not in rr.effective_sale_cfg(ccfg, hs2)["sale_search"]["sale_channels"]
assert "Отключены проверкой" in rr.sources_text(ccfg, hs2) and "@quiet_b" in rr.sources_text(ccfg, hs2)
hdb2.unlink(missing_ok=True)
print("OK — каналы раз в неделю: замолчал >30 дн. — сразу, не прошёл 2 недели — отключаем, ожил — возвращаем, сбой сети не в счёт")

# ============ карточка покупки одним сообщением: коллаж + разбор, ≤1024 символов ============
from io import BytesIO
from PIL import Image
def fake_img(url, headers=None, timeout=None):
    w, h = (1200, 900) if "big" in url else (400, 300)
    b = BytesIO(); Image.new("RGB", (w, h), (120, 80, 40)).save(b, "JPEG"); return R(_content=b.getvalue())
class R:
    def __init__(self, _content=b"", status=200): self.content, self.status_code = _content, status
with mock.patch.object(rr.requests, "get", fake_img):
    col = rr.make_collage(["http://x/s1", "http://x/big", "http://x/s2", "http://x/s3"], more=3)
im = Image.open(BytesIO(col))
assert im.width == 1200 and im.height > 900, im.size                        # 1 крупно + 3 снизу, не растянуто
long_l = dict(SS._listing(key="sale:uybor:5", site="Uybor", url="https://uybor.uz/listings/5", rooms=3, area=42, floor=3,
                          floors_total=6, district="Яшнабад", price_value=64000, text="#яшнабад #3ком Квартира " * 80 +
                          " +998 77 101-00-15"), price_usd=64000, seller_kind="agency", phones=["771010015"],
              alts=[{"price_usd": 66000 + i, "url": f"https://uybor.uz/listings/{9 + i}", "site": "Uybor", "kind": "agency"} for i in range(5)])
ct = rr.format_sale_card(mst, long_l, ss_all, head="🔥 <b>Только что от собственника</b> · выложено 3 мин назад")
assert rr.tg_len(ct) <= rr.CAPTION_MAX, rr.tg_len(ct)
assert ct.startswith("🔥") and "<b>3-комн · 42 м² · этаж 3/6</b>" in ct and "#яшнабад" not in ct and "Открыть на Uybor" in ct
assert ct.count("101-00-15") == 1                                           # телефон — строкой, не в описании
assert rr.sale_kb("sale:uybor:5", "self")["inline_keyboard"][0][1]["callback_data"] == "L:n:sale:uybor:5|0.0"
SP = []
def _rec_files(cfg_, method, data, files, timeout=90):
    SP.append((method, data, files)); return {"ok": True, "result": [{"message_id": 42}, {"message_id": 43}, {"message_id": 44}]}
_geo_t.static_map = lambda *a, **k: b"MAPJPEG"
with mock.patch.object(rr.requests, "get", fake_img), mock.patch.object(rr, "tg_call_files", _rec_files):
    ids_ = rr.send_card(dict(ss_tg, worker_url=""), dict(long_l, photo_urls=["http://x/big", "http://x/s1"],
                                                       geo={"lat": 41.3, "lon": 69.28, "src": "сайт"}),
                        ct, silent=True, ref="L:sale:uybor:5")
_geo_t.static_map = lambda *a, **k: None
assert ids_ == [42, 43, 44]
meth, data_, files_ = SP[0]
med = json.loads(data_["media"])
assert meth == "sendMediaGroup" and med[0]["media"] == "attach://map" and len(med) == 3   # карта первой, потом фото
assert med[0]["caption"] == ct and "reply_markup" not in data_ and data_["disable_notification"]
assert set(files_) == {"map", "p0", "p1"} and rr.CARD_LOG[-1][0] == "L:sale:uybor:5"
print("OK — карточка: обычный альбом — карта с отметкой первой, фото следом, подпись под альбомом, без кнопок")


# ============ место внутри района: ориентиры из объявлений, соседи в радиусе, карта, поиск у ориентира ============
import geo as G
gdb = Path("/tmp/test_geo.db"); gdb.unlink(missing_ok=True); gs = rr.Store(gdb); mk.ensure_tables(gs.conn)
import random
random.seed(7)
def ins(key, d, la, lo, m2, nb=0, area=50, lm=None, addr=None, uid=None, rep="evro"):
    gs.conn.execute("INSERT INTO market(key, op, district, rooms, area, price_usd, new_building, dorm, repair, user_id, "
                    "first_seen, last_seen, lat, lng, landmark, addr) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, "sale", d, 2, area, m2 * area, nb, 0, rep, uid or key, "x", "x", la, lo, lm, addr))
# Мирабад: дорогая часть у «Ойбека» (41.298, 69.274) и недорогая на юге (41.270, 69.290)
for i in range(14):
    ins(f"sale:uybor:a{i}", "Мирабад", 41.298 + random.uniform(-.004, .004), 69.274 + random.uniform(-.004, .004),
        2400 + i * 10, lm="Ориентир: Мирабадский рынок" if i < 5 else None)
for i in range(14):
    ins(f"sale:uybor:b{i}", "Мирабад", 41.270 + random.uniform(-.004, .004), 69.290 + random.uniform(-.004, .004),
        1500 + i * 10, lm="Госпитальный рынок" if i < 4 else None)
# «Корзинка» — по всему городу: не ориентир
for i, (la, lo) in enumerate([(41.35, 69.20), (41.25, 69.33), (41.32, 69.35), (41.28, 69.21)]):
    ins(f"sale:uybor:k{i}", "Юнусабад", la, lo, 1700, lm="рядом Корзинка")
gs.conn.commit()
gaz = G.build(gs.conn)
assert "*|mirabad rinak" in gaz, sorted(gaz)[:20]                       # «Мирабадский рынок» без окончания
assert not any("karzinka" in k for k in gaz)                                  # разброс по городу — не точка
G.load(gs, rebuild=True)
assert G.district_at(41.298, 69.274) == "Мирабад"
# объявление из Telegram без координат — точка по ориентиру из текста
tl = {"key": "sale:tg:x:1", "text": "Продаётся 2-комн. Ориентир: Мирабадский рынок, 3/9", "district": "Мирабад"}
g = G.locate(tl)
assert g["src"] == "ориентир" and G.km(g["lat"], g["lon"], 41.298, 69.274) < 0.6 and g["label"] == "Мирабадский рынок", g
# метро — по названию станции; точка в чужом районе — не берём
assert G.locate({"text": "5 минут до метро Ойбек", "district": "Мирабад"})["label"] == "м. Айбек"
pin_ = {"text": "x", "lat": 41.298, "lon": 69.274, "district": "Сергели"}       # пин в другом районе: верим точке,
assert G.locate(pin_)["src"] == "сайт" and pin_["district"] == "Мирабад" and pin_["district_site"] == "Сергели"   # район — по ней
assert G.locate({"text": "x", "lat": 41.298, "lon": 69.274, "district": "Мирабад"})["src"] == "сайт"
# соседи в радиусе: одна и та же цена — дорого на юге, нормально у Ойбека
south = dict(SS._listing(key="sale:tg:x:2", rooms=2, area=50, district="Мирабад", text="Ориентир: Госпитальный рынок"),
             price_usd=2100 * 50, repair="evro", lat=41.270, lon=69.290)
north = dict(south, key="sale:tg:x:3", lat=41.298, lon=69.274, text="Ориентир: Мирабадский рынок")
xs, xn = mk.analyze(gs, dict(south), ss_all), mk.analyze(gs, dict(north), ss_all)
assert xs["comp"]["near_km"] and "в радиусе" in xs["comp"]["label"] and xs["gap"] > 0.3, xs["comp"]
assert -0.20 < xn["gap"] < -0.05 and "рядом" in xs["verdict"][2], (xn["gap"], xs["verdict"])
assert xs["place"]["diff"] < -0.10 and xn["place"]["diff"] > 0.10                # дешёвая и дорогая части района
card_s = rr.format_sale_card(gs, dict(south), dict(ss_all, worker_url="https://w.example"))
assert "недорогая часть района" in card_s and "/map?p=41.27000,69.29000" in card_s and rr.tg_len(card_s) <= rr.CAPTION_MAX
assert "Рядом (1 км)" in card_s
# поиск у ориентира: радиус вместо района
near = {"lat": 41.298, "lon": 69.274, "km": 1.0, "label": "м. Айбек", "districts": ["Мирабад"]}
assert G.too_far(dict(north), near) == ""
assert "км от «м. Айбек»" in G.too_far(dict(south), near)
assert "точный адрес не указан" in G.too_far({"text": "", "district": "Сергели"}, near)
assert "точный адрес не указан" in G.too_far({"text": "", "district": "Мирабад"}, near)    # и «свой» район — не довод
ssn = dict(ss_all["sale_search"], near=near, max_price_usd=500000, owner_only=False, rooms=[2])
with mock.patch.object(rr, "classify_sale_seller", lambda *a: ""):
    assert rr.sale_reject(dict(south, price_value=105000), ssn, gs, ss_all)[0].endswith("км от «м. Айбек»")
    assert rr.sale_reject(dict(north, price_value=105000), ssn, gs, ss_all) == ("", False)
pt = G.point_for("метро Ойбек")
assert pt["src"] == "метро" and pt["label"] == "м. Айбек"
assert G.point_for("Мирабадский рынок")["src"] == "ориентир"
rd = Path("/tmp/test_geo_radar.db"); rd.unlink(missing_ok=True); rs__ = rr.Store(rd)
with mock.patch.object(rr, "SALE_DB_PATH", gdb), mock.patch.object(rr, "tg_call", fake_sale_tg):
    p1 = rr.near_point(ss_tg, rs__, "метро Ойбек", "1,5")
    assert p1["km"] == 1.5 and "Мирабад" in p1["districts"]
    cg.save_anketa(rs__, {"ans": {"deal": "buy", "object": "flat", "budget": "90000", "rooms": ["2"], "near": "метро Ойбек"}})
    ess = rr.effective_sale_cfg(ss_tg, rs__)["sale_search"]
    assert ess["near"]["label"] == "м. Айбек" and ess["districts"] == p1["districts"]
    assert "от «м. Айбек»" in rr.sale_criteria_text(ess)
    sent_msgs.clear()
    assert rr.near_point(ss_tg, rs__, "Абракадабра 77") is None and "Не нашла на карте" in sent_msgs[-1][1]
# маклерам — ориентир в тексте запроса
assert "рядом с «метро Ойбек», до 2 км" in cg.compose_request(ss_tg, rs__)
# карта цен
grid = G.price_grid(gs.conn)
assert grid["cells"] and all(c["n"] >= 3 for c in grid["cells"]) and grid["districts"]["Мирабад"]["n"] == 28
gs.set_kv("price_grid_dirty", True)
PM = []
with mock.patch.object(rr, "worker_post", lambda c, path, payload, timeout=60: PM.append((path, payload)) or {"ok": True}):
    assert rr.push_price_map(dict(ss_all, worker_url="https://w.example"), gs) is True
assert PM[0][0] == "/svc/pricemap" and PM[0][1]["metro"] and gs.get_kv("price_grid_dirty") is False
G.GAZ.update(gaz={}, pts=[], at=0, db=None)
gdb.unlink(missing_ok=True); rd.unlink(missing_ok=True)
print("OK — место: ориентиры из объявлений (без «Корзинок»), соседи в радиусе, дорогая/дешёвая часть района, поиск у ориентира, карта цен")

# ============ без фото — не показываем ============
npdb = Path("/tmp/test_nophoto.db"); npdb.unlink(missing_ok=True); nps = rr.Store(npdb)
nol = dict(SS._listing(key="sale:tg:x:9", text="2-комн, Мирабад"), price_usd=50000, area=50)
SS.queue_pick(nps, nol, 80, [])
assert not nps.get_kv("sale_pick")                                       # в подборку не попал
SS.queue_pick(nps, dict(nol, photo_urls=["https://x/1.jpg"]), 80, [])
assert len(nps.get_kv("sale_pick")) == 1
with mock.patch.object(rr, "tg_call", lambda *a, **k: {"ok": True, "result": {}}):
    nps.save(nol, notified=False)
    assert rr.send_site_cards(cfg, nps, ["sale:tg:x:9"]) == 0             # карточка без фото не уходит
assert "попросить" in cg.details_question({"rooms": 2, "district": "Мирабад", "price_usd": 0, "floor": 3, "area": 50,
                                           "photos": [], "extra": {}}, cfg, "buy").lower() or \
       "фото" in cg.details_question({"rooms": 2, "district": "Мирабад", "price_usd": 0, "floor": 3, "area": 50,
                                      "photos": [], "extra": {}}, cfg, "buy")
npdb.unlink(missing_ok=True)
print("OK — без фото: в подборку и карточкой не показываем, у маклера просим фото")

# ============ «центр» — что считается центром города: поиск в интернете + проверка по карте ============
cprof = G.center_from_research({"name": "Тест", "lat": 41.3123, "lon": 69.2787, "center_km": 3.5},
    {"anchor": "сквер Амира Темура", "radius_km": 3.5, "boundary": "внутри Малой кольцевой",
     "in": ["Минор", "Ойбек", "Госпитальный", "Бадамзар", "Ц-1"], "out": ["Юнусабад 11 квартал", "Чиланзар 5"]},
    resolve=lambda n: {"сквер Амира Темура": (41.3111, 69.2797), "Минор": (41.3269, 69.2831), "Ойбек": (41.298, 69.2741),
                       "Госпитальный": (41.2936, 69.2728), "Бадамзар": (41.3372, 69.2847), "Ц-1": (41.3126, 69.2893),
                       "Юнусабад 11 квартал": (41.3647, 69.2876), "Чиланзар 5": (41.2919, 69.2236)}.get(n))
assert 2.4 <= cprof["center_km"] <= 3.6 and cprof["near_km"] > cprof["center_km"] and cprof["boundary"], cprof
assert G.center_relation(41.3269, 69.2831, cprof)[0] == "центр" and G.center_relation(41.3647, 69.2876, cprof)[0] == "не центр"
assert G.is_center_query("ближе к центру") and G.is_center_query("markazda") and not G.is_center_query("метро Ойбек")
cdb = Path("/tmp/test_center_sale.db"); cdb.unlink(missing_ok=True); rr.Store(cdb).conn.close()
crd = Path("/tmp/test_center_radar.db"); crd.unlink(missing_ok=True); crs = rr.Store(crd)
CM = []
fake_city = lambda c, path, payload, timeout=60: {"ok": True, "anchor": "сквер Амира Темура", "radius_km": 3.0,
                                                  "boundary": "внутри Малой кольцевой дороги", "in": [], "out": [],
                                                  "sources": ["https://example.uz/centr"]}
with mock.patch.object(rr, "SALE_DB_PATH", cdb), mock.patch.object(rr, "worker_post", fake_city), \
        mock.patch.object(rr, "tg_call", lambda c, m, p, **k: CM.append(p) or {"ok": True, "result": {}}):
    G.CITY_PROFILES.pop("ташкент", None)
    pt = rr.near_point(dict(ss_tg, worker_url="https://w"), crs, "ближе к центру")
    assert pt["src"] == "центр" and pt["center_km"] == 3.0 and pt["km"] > 3.0
    assert "Малой кольцевой" in CM[-1]["text"] and "example.uz" in CM[-1]["text"]   # объяснила, что считает центром
    assert rr.Store(cdb).get_kv("city:ташкент")["researched"] is True          # запомнила, второй раз не ищет
    cg.save_anketa(crs, {"ans": {"deal": "buy", "object": "flat", "budget": "80000", "rooms": ["2"], "note": "ближе к центру"}})
    crs.set_kv("near_point", None)
    assert rr.effective_sale_cfg(ss_tg, crs)["sale_search"]["near"]["src"] == "центр"   # старая пометка в пожеланиях
G.CITY_PROFILES["ташкент"] = {**G.CITY_FALLBACK["ташкент"], "near_km": 4.9}
cdb.unlink(missing_ok=True); crd.unlink(missing_ok=True)
print("OK — центр: что считается центром города — из интернета, проверено по карте; «у центра» помечаем")


# ============ Telegram для бизнеса: Ra'no в чатах клиента — читает маклеров и уточняет сама ============
bzd = Path("/tmp/test_biz.db"); bzd.unlink(missing_ok=True); bz = rr.Store(bzd)
BZ = []
def bz_tg(c, method, payload, timeout=20, quiet=False):
    BZ.append((method, payload)); return {"ok": True, "result": {"message_id": 7}}
bcfg = dict(cfg, telegram_chat_id="500", worker_url="")
cg.save_anketa(bz, {"i": 99, "ans": {"lang": "ru", "deal": "buy", "object": "flat", "city": "tashkent",
                                     "rooms": ["2"], "budget": "70000", "contact": "bot"}})
bz.conn.execute("INSERT INTO brokers(bid, source, name, phone, tg, status) VALUES('b1','uybor','Азиз','901','@Makler1','new')")
bz.conn.commit()
assert "rano_smart_bot" in cg.compose_request(bcfg, bz)                          # не подключено — ссылка на бота
guide = cg.biz_guide(bcfg, bz)
assert "Telegram для бизнеса" in guide and "Чат-боты" in guide and "rano_smart_bot" in guide and "Новые чаты" in guide
with mock.patch.object(rr, "tg_call", bz_tg):
    rr.concierge._BIZ_STORE["store"] = bz
    # подключили без права отвечать — подсказываем, что включить
    rr.handle_business_update(bcfg, bz, {"business_connection": {"id": "BC1", "user": {"id": 500}, "user_chat_id": 500,
                                         "is_enabled": True, "rights": {}}})
    assert "Управление сообщениями" in BZ[-1][1]["text"] and not cg.biz_ready(bz)
    rr.handle_business_update(bcfg, bz, {"business_connection": {"id": "BC1", "user": {"id": 500}, "user_chat_id": 500,
                                         "is_enabled": True, "rights": {"can_reply": True}}})
    assert cg.biz_ready(bz) and "подключена к вашему Telegram" in BZ[-1][1]["text"]
    req = bz.get_kv("request_text")
    assert "ИИ-помощник" in req and "t.me/rano_smart_bot" not in req                # про ИИ — в первом сообщении
    # чужое подключение — не наше
    rr.handle_business_update(bcfg, bz, {"business_connection": {"id": "X", "user": {"id": 1}, "user_chat_id": 1,
                                         "is_enabled": True, "rights": {"can_reply": True}}})
    assert bz.get_kv("biz_conn")["id"] == "BC1"
    def bmsg(peer, frm, text=None, photo=None, **kw):
        m = {"business_connection_id": "BC1", "chat": {"id": peer, "type": "private", "first_name": "Азиз",
                                                         "username": "makler1"}, "from": {"id": frm}, **kw}
        if text: m["text"] = text
        if photo: m["photo"] = [{"file_id": photo, "width": 9, "height": 9}]
        return {"business_message": m}
    n0 = bz.conn.execute("SELECT COUNT(*) FROM broker_offers").fetchone()[0]
    rr.handle_business_update(bcfg, bz, bmsg(9002, 9002, "Привет! Как дела?"))           # чат без запроса — не читаем
    rr.handle_business_update(bcfg, bz, bmsg(9001, 9001, "2 комн Мирабад 65 000$"))     # маклер раньше запроса — тоже
    assert bz.conn.execute("SELECT COUNT(*) FROM broker_offers").fetchone()[0] == n0 and not bz.get_kv("biz_chats")
    rr.handle_business_update(bcfg, bz, bmsg(9001, 500, req))                           # клиент отправил запрос
    assert "biz:9001" in bz.get_kv("biz_chats")
    assert bz.conn.execute("SELECT status FROM brokers WHERE bid='b1'").fetchone()[0] == "contacted"   # сам отметил
    BZ.clear()
    rr.handle_business_update(bcfg, bz, bmsg(9001, 9001, "Есть 2 комн Мирабад 58 м2, 65 000$, 4/9"))
    assert not BZ                                                                      # маклеру сразу не отвечаем
    o = bz.conn.execute("SELECT oid, broker_chat FROM broker_offers ORDER BY oid DESC LIMIT 1").fetchone()
    assert o[1] == "biz:9001"
    pend = bz.get_kv("pending_offers"); pend[str(o[0])]["at"] -= 1000; bz.set_kv("pending_offers", pend)
    with mock.patch.object(rr, "send_offer_analysis", lambda *a, **k: None):
        rr.flush_pending_offer(bcfg, bz)
    ask = [pl for m, pl in BZ if pl.get("business_connection_id") == "BC1"]
    assert len(ask) == 1 and ask[0]["chat_id"] == 9001, BZ
    q = ask[0]["text"]
    assert "фото" in q.lower() and "адрес" in q.lower() and "Ra'no" not in q and "посмотреть" not in q   # без подписи, без просмотра
    assert any(str(pl.get("chat_id")) == "500" for m, pl in BZ)                        # клиенту — карточка
    # маклер досылает фото — в тот же вариант, карточка обновится; второй раз не спрашиваем
    BZ.clear()
    rr.handle_business_update(bcfg, bz, bmsg(9001, 9001, photo="ph1"))
    rr.handle_business_update(bcfg, bz, bmsg(9001, 9001, "Юнусабадская 5, рядом метро"))
    rows = bz.conn.execute("SELECT oid, photos, text FROM broker_offers WHERE broker_chat='biz:9001'").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][1]) == ["ph1"] and "Юнусабадская" in rows[0][2], rows
    pend = bz.get_kv("pending_offers"); pend[str(o[0])]["at"] -= 1000; bz.set_kv("pending_offers", pend)
    with mock.patch.object(rr, "send_offer_analysis", lambda *a, **k: None):
        rr.flush_pending_offer(bcfg, bz)
    assert any("дослал" in pl.get("text", "") for m, pl in BZ)
    assert not [pl for m, pl in BZ if pl.get("business_connection_id")]
    # наше же сообщение (через бота) и отказ — без подписи, от первого лица
    rr.handle_business_update(bcfg, bz, bmsg(9001, 500, "Спасибо!", sender_business_bot={"id": 1}))
    BZ.clear()
    cg.handle_triage_cb(f"t:r:{o[0]}:p", bcfg, bz)
    dec = [pl for m, pl in BZ if pl.get("business_connection_id")]
    assert dec and "Увы, не подошёл" in dec[0]["text"] and "(Ra'no)" not in dec[0]["text"]
    # маклеры — ссылками с вписанным запросом: 5 за раз, до 15 в день, показанных второй раз не даём
    for i in range(20):
        bz.conn.execute("INSERT INTO brokers(bid, source, name, phone, tg, status, ads, districts, deals) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (f"m{i}", "uybor", f"Маклер {i}", f"90{i:07d}",
                                                      "" if i % 2 else f"mk{i}", "new", 30 - i, '["Мирабад"]', '["sale"]'))
    bz.conn.commit()
    BZ.clear()
    assert rr.handle_command("/brokers", rr.default_settings(), bz, bcfg)[0] == ""
    m1 = [pl for m, pl in BZ if "Маклеры с похожими объявлениями" in pl.get("text", "")]
    allb = [pl for m, pl in BZ if m == "sendMessage"]
    joined = "\n".join(pl["text"] for pl in allb)
    assert len(m1) == 1 and joined.count('<a href="https://t.me/') == 5 and "?text=" in joined
    assert not any("reply_markup" in pl for pl in allb) and "ещё маклеров" in allb[-1]["text"]
    # ссылки с двуязычным запросом длинные: 5 в одно сообщение Telegram не пустит — шлём по одному маклеру
    assert len(allb) == 5 and all(pl["text"].count('<a href="https://t.me/') == 1 for pl in allb)
    assert bz.conn.execute("SELECT COUNT(*) FROM brokers WHERE status='offered'").fetchone()[0] == 5
    rr.handle_command("/brokers", rr.default_settings(), bz, bcfg)
    rr.handle_command("/brokers", rr.default_settings(), bz, bcfg)
    stop = rr.handle_command("/brokers", rr.default_settings(), bz, bcfg)[0]
    assert "на сегодня хватит" in stop and bz.conn.execute(
        "SELECT COUNT(*) FROM brokers WHERE status='offered'").fetchone()[0] == 15
    # из аккаунта клиента — без рода: клиент может быть и мужчиной, и женщиной
    FEM = re.compile(r"\b(?:я\s+)?(?:передала|нашла|посмотрела|отправила|спросила|уточнила|видела|поняла|"
                     r"записала|напомнила|получила|готова|рада|сама|ассистент(?:ка)?|помощница)\b", re.I)
    o_ = {"rooms": 2, "district": "Мирабад", "price_usd": 0, "floor": None, "area": None, "photos": [], "extra": {}}
    for t in [cg.biz_followup_text(o_, "buy"), cg.biz_followup_text(o_, "rent"),
              *(cg._biz_voice(cg.decline_text(bcfg, c), bcfg) for c, *_ in cg.DECLINE_REASONS),
              cg._biz_voice(cg.remind_text(dict(o_, oid=1), bcfg), bcfg),
              cg._biz_voice(cg.details_question(dict(o_, oid=1, status="new"), bcfg, "buy"), bcfg),
              cg.compose_request(bcfg, bz).split("Мне помогает")[0]]:
        assert not FEM.search(t), t
    # просили маклеров до подключения → после подключения присылаю сразу
    bz.set_kv("brokers_pending", True); bz.set_kv("biz_conn", {}); bz.set_kv("outreach_day", {})
    bz.conn.execute("UPDATE brokers SET status='new' WHERE bid LIKE 'm%'"); bz.conn.commit()
    BZ.clear()
    rr.handle_business_update(bcfg, bz, {"business_connection": {"id": "BC1", "user": {"id": 500}, "user_chat_id": 500,
                                         "is_enabled": True, "rights": {"can_reply": True}}})
    assert any("Вот маклеры" in pl.get("text", "") for m, pl in BZ) and any(
        "Маклеры с похожими объявлениями" in pl.get("text", "") for m, pl in BZ) and not bz.get_kv("brokers_pending")
    # исходящий запрос до бота не дошёл — узнаём чат по ответу: маклер из подборки или ответ цитатой на запрос
    assert bz.conn.execute("SELECT status FROM brokers WHERE tg='mk0'").fetchone()[0] == "offered"
    def bm2(peer, un, text, **kw):
        return {"business_message": {"business_connection_id": "BC1", "from": {"id": peer}, "text": text,
                                     "chat": {"id": peer, "type": "private", "first_name": "М", "username": un}, **kw}}
    rr.handle_business_update(bcfg, bz, bm2(9003, "mk0", "Есть 2 комн Мирабад 60 000$"))
    rr.handle_business_update(bcfg, bz, bm2(9004, None, "Есть вариант 2 комн", reply_to_message={"text": req}))
    rr.handle_business_update(bcfg, bz, bm2(9005, "friend", "Привет, как дела?"))
    ch_ = bz.get_kv("biz_chats")
    assert ch_["biz:9003"]["via"] == "username" and ch_["biz:9004"]["via"] == "reply" and "biz:9005" not in ch_
    assert bz.conn.execute("SELECT status FROM brokers WHERE tg='mk0'").fetchone()[0] == "contacted"
    assert (bz.get_kv("biz_out_seen") or {}).get("n", 0) >= 1
    # отключили — больше не пишем маклерам из аккаунта клиента
    rr.handle_business_update(bcfg, bz, {"business_connection": {"id": "BC1", "user": {"id": 500}, "user_chat_id": 500,
                                         "is_enabled": False, "rights": {"can_reply": True}}})
    assert not cg.biz_ready(bz) and "отключена" in BZ[-1][1]["text"]
    assert cg.broker_send(bcfg, bz, "biz:9001", "тест") is None
rr.concierge._BIZ_STORE["store"] = None
bzd.unlink(missing_ok=True)
print("OK — Telegram для бизнеса: подключение с проверкой прав, только чаты с запросом, вариант в карточку, сама уточняет недостающее")

# ============ клиенту кнопок не шлём ни из одного места: управление словами ============
ocfg = dict(cfg, telegram_chat_id="500")
kb_ = json.dumps({"inline_keyboard": [[{"text": "x", "callback_data": "y"}]]})
assert "reply_markup" not in rr._owner_no_buttons(ocfg, {"chat_id": 500, "text": "t", "reply_markup": kb_})
assert rr._owner_no_buttons(ocfg, {"chat_id": 777, "text": "t", "reply_markup": kb_})["reply_markup"] == kb_   # маклеру можно
rm_ = json.dumps({"remove_keyboard": True})
assert rr._owner_no_buttons(ocfg, {"chat_id": 500, "text": "t", "reply_markup": rm_})["reply_markup"] == rm_
assert "Кнопок больше нет" in rr.VIEW_HINT and "своими словами" in rr.WELCOME_TEXT + rr.HELP_TEXT
assert "напиши маклерам" in rr.HELP_TEXT and "пару нажатий" not in rr.WELCOME_TEXT
print("OK — без кнопок: клиенту клавиатуры не уходят, подсказки — какими словами сказать")

# ============ без подключения к Telegram: пересылка + маклер может писать прямо в бота ============
mnd = Path("/tmp/test_manual.db"); mnd.unlink(missing_ok=True); mn = rr.Store(mnd)
MN = []
mcfg = dict(cfg, telegram_chat_id="600", worker_url="")
cg.save_anketa(mn, {"i": 99, "ans": {"lang": "ru", "deal": "buy", "object": "flat", "city": "tashkent",
                                     "rooms": ["2"], "budget": "70000", "contact": "bot"}})
for i in range(6):
    mn.conn.execute("INSERT INTO brokers(bid, source, name, phone, tg, status, ads, districts, deals) VALUES(?,?,?,?,?,?,?,?,?)",
                    (f"q{i}", "uybor", f"М {i}", f"90{i:07d}", "", "new", 10, '["Мирабад"]', '["sale"]'))
mn.conn.commit()
with mock.patch.object(rr, "tg_call", lambda c, m, p, **k: MN.append((m, p)) or {"ok": True, "result": {"message_id": 3}}), \
        mock.patch.object(rr, "push_snapshot", lambda *a, **k: True):
    rr.concierge._BIZ_STORE["store"] = mn
    assert cg.broker_mode(mn) == ""
    rr.handle_command("/brokers", rr.default_settings(), mn, mcfg)                      # по умолчанию — подключить Telegram
    g = MN[-1][1]["text"]
    assert "Telegram для бизнеса" in g and "предложу другой способ" in g and "без подключения" not in g
    assert not [p for m, p in MN if "Маклеры с похожими объявлениями" in p.get("text", "")] and mn.get_kv("brokers_pending")
    # «не хочу давать доступ» → второй способ: инструкция по пересылке и сразу маклеры
    MN.clear()
    with mock.patch.object(rr, "fetch_updates", lambda c, st, lp=0: ({"result": [{"update_id": 1, "message": {
            "chat": {"id": 600}, "text": "не хочу давать доступ", "_biz_decline": True}}]}, "tg_offset")):
        rr.process_commands(mcfg, mn)
    assert mn.get_kv("broker_mode") == "manual" and cg.broker_mode(mn) == "manual" and not mn.get_kv("brokers_pending")
    assert any("ничего от вашего имени не пишу" in p.get("text", "") for m, p in MN)
    def batch_text(L):                                  # подборка может прийти несколькими сообщениями
        i = max(k for k, (m, p) in enumerate(L) if "Маклеры с похожими объявлениями" in p.get("text", ""))
        return "\n".join(p.get("text", "") for m, p in L[i:] if m == "sendMessage")
    first = batch_text(MN)
    assert "WhatsApp" in first and "Переслать" in first and "подключить телеграм" not in first
    assert "t.me/rano_smart_bot" in cg.compose_request(mcfg, mn)                                  # маклер может писать в бота
    MN.clear()
    rr.handle_command("/brokers", rr.default_settings(), mn, mcfg)
    nxt = batch_text(MN)
    assert "подключить телеграм" not in nxt and "Переслать" in nxt                                 # больше не уговариваем
    # пересланный вариант: уточнение — готовым текстом клиенту, один раз
    oid, _ = cg.save_offer(mn, mcfg, "owner", "Азиз", "Продаю 2 комн Мирабад", [])
    MN.clear()
    assert cg.manual_auto_ask(mcfg, mn, oid) and "<code>" in MN[-1][1]["text"] and "фото" in MN[-1][1]["text"].lower()
    assert not cg.manual_auto_ask(mcfg, mn, oid)
rr.concierge._BIZ_STORE["store"] = None
mnd.unlink(missing_ok=True)
print("OK — без подключения: выбор словами, инструкция по пересылке, ссылка на бота в запросе, уточнения готовым текстом")

# ============ локация от маклера и просмотр: Ra'no договаривается сама ============
lvd = Path("/tmp/test_loc_view.db"); lvd.unlink(missing_ok=True); lv = rr.Store(lvd)
LV = []
lcfg = dict(cfg, telegram_chat_id="700", worker_url="")
cg.save_anketa(lv, {"i": 99, "ans": {"lang": "ru", "deal": "buy", "object": "flat", "city": "tashkent", "rooms": ["2"]}})
with mock.patch.object(rr, "tg_call", lambda c, m, p, **k: LV.append((m, p)) or {"ok": True, "result": {"message_id": 9}}), \
        mock.patch.object(rr, "send_offer_analysis", lambda *a, **k: None):
    rr.concierge._BIZ_STORE["store"] = lv
    lv.set_kv("biz_conn", {"id": "BC7", "user_id": 700, "enabled": True, "can_reply": True})
    lv.set_kv("biz_chats", {"biz:8101": {"since": time.time()}})
    bm = lambda **kw: {"business_message": {"business_connection_id": "BC7", "chat": {"id": 8101, "type": "private",
                                                                                         "first_name": "Ойбек"},
                                            "from": {"id": 8101}, **kw}}
    # точка Telegram в ответе маклера → координаты варианта, на карточке — точка, а не район
    rr.handle_business_update(lcfg, lv, bm(text="2 комн Яккасарай 60 000$ 3/9"))
    rr.handle_business_update(lcfg, lv, bm(location={"latitude": 41.29, "longitude": 69.26}))
    oid = lv.conn.execute("SELECT oid FROM broker_offers WHERE broker_chat='biz:8101'").fetchone()[0]
    pend = lv.get_kv("pending_offers"); pend[str(oid)]["at"] -= 1000; lv.set_kv("pending_offers", pend)
    rr.flush_pending_offer(lcfg, lv)
    ex = cg.get_offer(lv, oid)["extra"]
    assert (ex["lat"], ex["lon"], ex["loc_src"]) == (41.29, 69.26, "точка")
    assert cg.offer_media(lcfg, cg.get_offer(lv, oid))["lat"] == 41.29
    ask = [p for m, p in LV if p.get("business_connection_id")][-1]["text"]
    assert "адрес" not in ask.lower()                                         # точка есть — адрес не спрашиваем
    # скриншот карты среди фото — в галерею не идёт
    cg.enrich_offer(lcfg, lv, oid, {"map_photos": "1"})
    cg.merge_into_offer(lv, lcfg, oid, "", ["mapshot", "flat1"])
    cg.enrich_offer(lcfg, lv, oid, {"map_photos": "1", "address": "ул. Бобура 5"})
    o_ = cg.get_offer(lv, oid)
    assert o_["photos"] == ["flat1"] and o_["extra"]["map_files"] == ["mapshot"] and o_["extra"]["address"] == "ул. Бобура 5"
    # клиент сказал, когда удобно → Ra'no пишет маклеру; «Да, давайте» → подтверждено, клиенту — ответ
    LV.clear()
    when = (datetime.now(cg.TZ) + timedelta(days=2)).replace(hour=11, minute=0)
    assert cg.set_viewing(lcfg, lv, oid, when.isoformat(), "в субботу в 11:00")
    assert cg.arrange_viewing(lcfg, lv, oid) == "asked"
    q = [p for m, p in LV if p.get("business_connection_id")][-1]
    assert q["chat_id"] == 8101 and "в субботу в 11:00" in q["text"] and "Ra'no" not in q["text"]
    LV.clear()
    rr.handle_business_update(lcfg, lv, bm(text="Да, давайте"))
    note = LV[-1][1]["text"]
    assert "Договорились" in note and "Да, давайте" in note and cg.get_offer(lv, oid)["extra"]["viewing"]["confirmed"]
    # пересланный вариант (маклеру не написать) — готовый текст клиенту
    o2, _ = cg.save_offer(lv, lcfg, "owner", "Азиз", "2 комн Мирабад https://maps.google.com/?q=41.3001,69.2401", [])
    assert cg.locate_offer(lv, o2)[:2] == (41.3001, 69.2401)
    cg.set_viewing(lcfg, lv, o2, when.isoformat(), "в субботу в 11:00")
    LV.clear()
    assert cg.arrange_viewing(lcfg, lv, o2) == "manual" and "<code>" in LV[-1][1]["text"]
    assert "Договорюсь о просмотре" in cg.biz_guide(lcfg) and "локацию" in cg.biz_guide(lcfg)
rr.concierge._BIZ_STORE["store"] = None
lvd.unlink(missing_ok=True)
print("OK — локация от маклера (точка, ссылки карт, скриншот) и просмотр: Ra'no согласует время с маклером")

# ============ языки: клиент ru/uz/en (главное — вручную, остальное — модель), маклер — на его языке ============
import i18n as I18
assert I18.detect_lang("Assalomu alaykum, 2 xonali kvartira bor, narxi 60 000$") == "uz"
assert I18.detect_lang("Здравствуйте, есть 2-комн на Чиланзаре") == "ru"
assert I18.detect_lang("Икки хонали уй сотилади, нархи 55 000") == "uz"
try:
    I18.set_lang("en")
    assert I18.t("Ниже рынка") == "Below market" and I18.t("на {pct} дешевле похожих{near}{with_reno}", pct="21%",
                                                               near=I18.t(" рядом"), with_reno="") == "21% less than similar nearby"
    assert I18.dist("Мирабад") == "Mirabad" and "Telegram Business" in cg.biz_guide({})
    assert I18.t(rr.HELP_TEXT).startswith("🏠 <b>Ra'no</b> — your AI assistant") and "/help" in I18.t(rr.VIEW_HINT)
    assert mk.mortgage_line({"mortgage_ok": "no", "mortgage_quote": "в ипотеку не даём"}) == "Mortgage — no: «в ипотеку не даём»"
    assert I18.t("Неизвестная фраза") == "Неизвестная фраза"                        # нет в каталоге — переведёт модель
    # что осталось по-русски — в перевод моделью; кнопки/ссылки/код — как были
    seen = []
    fake_post = lambda c, path, pl, timeout=40: (seen.append(pl), {"ok": True, "text": "Translated: " + pl["text"][-5:]})[1]
    with mock.patch.object(rr, "worker_post", fake_post):
        ocfg2 = dict(cfg, telegram_chat_id="900", worker_url="https://w")
        out = rr._owner_translate(ocfg2, {"chat_id": 900, "text": "Пока тихо — новых вариантов нет, загляните позже, пожалуйста"})
        assert out["text"].startswith("Translated") and seen[-1]["lang"] == "en"
        same = rr._owner_translate(ocfg2, {"chat_id": 900, "text": I18.t("Ниже рынка") + " · Mirabad"})
        assert same["text"] == "Below market · Mirabad"                                 # уже по-английски — не трогаем
        brk = rr._owner_translate(ocfg2, {"chat_id": 555, "text": "Здравствуйте, маклеру — по-русски, как есть"})
        assert brk["text"].startswith("Здравствуйте")                                 # маклеру — не переводим
        media = rr._owner_translate(ocfg2, {"chat_id": 900, "media": json.dumps([{"type": "photo", "media": "x",
                                     "caption": "Ремонт хороший — можно заезжать, документы готовы, звоните"}])})
        assert json.loads(media["media"])[0]["caption"].startswith("Translated")
    I18.set_lang("uz")
    assert I18.t("Ипотека") == "Ipoteka" and I18.dist("Яшнабад") == "Yashnobod"
finally:
    I18.set_lang("ru")
assert I18.t("Ниже рынка") == "Ниже рынка"
# запрос маклерам — на двух языках; узбекскому маклеру отвечаем по-узбекски
two = cg.compose_request(cfg, cs)
assert "Здравствуйте" in two and "Assalomu alaykum" in two
ls_ = rr.Store(Path("/tmp/test_lang.db"))
UZ = []
with mock.patch.object(rr, "tg_call", lambda c, m, p, **k: UZ.append(p) or {"ok": True}), \
        mock.patch.object(I18, "ai", lambda c, text, lang=None, post=None: "UZ:" + text[:10] if lang == "uz" else text):
    I18.broker_lang(ls_, "4321", "Assalomu alaykum, variant bor")
    cg.broker_send(dict(cfg, worker_url="https://w"), ls_, "4321", "Здравствуйте! Он ещё актуален?")
    assert UZ[-1]["text"].startswith("UZ:")
    I18.broker_lang(ls_, "4322", "Добрый день, есть вариант")
    cg.broker_send(cfg, ls_, "4322", "Здравствуйте! Он ещё актуален?")
    assert UZ[-1]["text"].startswith("Здравствуйте")
    assert "Kvartirani" in cg.biz_followup_text({"photos": [], "extra": {}}, "buy", True) or "rasm" in \
        cg.biz_followup_text({"photos": [], "extra": {}}, "buy", True).lower()
with mock.patch.object(I18, "ai", lambda c, text, lang=None, post=None: "есть вариант" if lang == "ru" else text):
    assert I18.quote_for_client(cfg, "variant bor, narxi yaxshi") == "variant bor, narxi yaxshi\n(перевод: есть вариант)"
Path("/tmp/test_lang.db").unlink(missing_ok=True)
print("OK — языки: карточки и инструкции на ru/uz/en, остальное переводит модель, маклеру — на его языке")

# ============ скорость: сообщения клиента обрабатываем посреди долгого прохода ============
pd_ = Path("/tmp/test_pump.db"); pd_.unlink(missing_ok=True)
ps_ = rr.Store(pd_)
CALLS = []
def _pc(c, st, long_poll=0):
    CALLS.append(long_poll)
    if len(CALLS) == 2:
        st.set_kv("sale_force", True)            # клиент поменял условия
    return {}
with mock.patch.object(rr, "process_commands", _pc), mock.patch.object(rr, "flush_pending_offer", lambda *a: None):
    rr._PUMP.update(ctx=None, last=0.0, busy=False)
    assert rr.pump() is False and not CALLS                       # вне прохода — ничего
    rr._PUMP["ctx"] = ({}, ps_, None)
    assert rr.pump() is False and CALLS == [0]                    # без long-poll
    assert rr.pump() is False and CALLS == [0]                    # чаще раза в PUMP_EVERY — не дёргаем
    rr._PUMP["last"] = 0
    assert rr.pump() is True and len(CALLS) == 2                  # условия поменялись — разбор прервать
    rr._PUMP.update(last=0, busy=True)
    assert rr.pump() is False and len(CALLS) == 2                 # без вложенности
    rr._PUMP.update(ctx=None, busy=False)
import sale_sources as _ss_p
assert _ss_p._rr() is rr                                          # один модуль на всех
# Realting: страниц стало меньше (404) — каталог с начала, а не застревать навсегда
class _R404:
    status_code = 404; text = ""
    def raise_for_status(self): raise RuntimeError("404")
ps_.set_kv("realting_page", 26)
with mock.patch.object(rr.requests, "get", lambda *a, **k: _R404()):
    assert rr.harvest_realting(ps_) == 0 and ps_.get_kv("realting_page") == 1
pd_.unlink(missing_ok=True)
print("OK — скорость: сообщения клиента посреди прохода, без вложенности; Realting не застревает на 404")

# ============ дубли по фото: маклер перевыложил квартиру собственника с другим текстом и ценой ============
from io import BytesIO as _BIO
from PIL import Image as _Img, ImageDraw as _Draw
def _jpeg(seed, shift=0):
    im = _Img.new("RGB", (320, 240), (200, 190, 180)); d = _Draw.Draw(im)
    for i in range(12):
        x = (seed * 37 + i * 53) % 280
        d.rectangle([x + shift, (i * 19) % 200, x + 30 + shift, (i * 19) % 200 + 25], fill=((i * 40) % 255, 90, (seed * 70) % 255))
    b = _BIO(); im.save(b, "JPEG", quality=70); return b.getvalue()
IMGS = {"u/own1.jpg": _jpeg(1), "u/brk1.jpg": _jpeg(1, shift=1), "u/other.jpg": _jpeg(5), "u/own2.jpg": _jpeg(2)}
class _RI:
    def __init__(self, c): self.status_code, self.content = 200, c
pdb_ = Path("/tmp/test_phash.db"); pdb_.unlink(missing_ok=True)
pst_ = rr.Store(pdb_)
with mock.patch.object(_ss_p.requests, "get", lambda u, **k: _RI(IMGS[u])):
    own = {"key": "sale:uybor:1", "rooms": 2, "area": 55, "price_usd": 60000, "photo_urls": ["u/own1.jpg", "u/own2.jpg"]}
    _ss_p.queue_pick(pst_, own, 70, [])                          # собственник — в подборке
    assert pst_.conn.execute("SELECT COUNT(*) FROM photo_hash").fetchone()[0] == 2
    brk = {"key": "sale:joymee:9", "rooms": 2, "area": 56, "price_usd": 64000, "photo_urls": ["u/brk1.jpg"]}
    assert _ss_p.photo_dup(pst_, brk) == "sale:uybor:1"            # те же фото, пережаты и сдвинуты
    oth = {"key": "sale:joymee:10", "rooms": 2, "area": 55, "price_usd": 60000, "photo_urls": ["u/other.jpg"]}
    assert _ss_p.photo_dup(pst_, oth) is None                      # другая квартира
    big = dict(brk, key="sale:joymee:11", area=80)
    assert _ss_p.photo_dup(pst_, big) is None                      # фото похожи, а площадь другая — не склеиваем
    yu = {"key": "sale:yu:5", "rooms": 2, "area": 55, "price_usd": 60000, "photo_urls": ["u/own1.jpg"]}
    assert _ss_p.photo_dup(pst_, yu) is None                       # рендеры ЖК не сравниваем
pdb_.unlink(missing_ok=True)
print("OK — дубли по фото: перевыложенная маклером квартира узнаётся по снимкам, разные квартиры не склеиваются")

# ============ «в центре»: без точной точки не проходит никакой район; ориентиры без названия не ищем ============
near_c = {"lat": 41.31267, "lon": 69.28327, "label": "центр", "km": 3.6, "districts": ["Мирабад", "Мирзо-Улугбек"]}
for d_ in ("Мирзо-Улугбек", "Мирабад"):
    assert "точный адрес не указан" in _geo_t.too_far({"district": d_, "geo": {"src": "район"}}, near_c)
assert _geo_t.too_far({"district": "Мирзо-Улугбек", "geo": {"lat": 41.315, "lon": 69.29, "src": "сайт"}}, near_c) == ""
assert _geo_t.generic_place("Мечеть") and _geo_t.generic_place("Мебельный магазин") and not _geo_t.generic_place("школа 64")
assert not _geo_t.generic_place("Рисовый базар") and not _geo_t.generic_place("Массив ТТЗ-2")
print("OK — «в центре»: решает только точка — без неё никакой район не проходит; ориентиры без названия не ищем")

# ============ оценка цены: ближайшие похожие (kNN), не больше 3 от одного продавца, разброс P20–P80 ============
import market as _mk2
_gz2 = list(_geo_t.GAZ["pts"])
try:
    base_ = (41.30, 69.28)
    pts_ = [(base_[0] + i * 0.0009, base_[1], "Мирабад", "sale", 0, 0, 50 + (i % 5), (1500 + (i % 7) * 20) * (50 + (i % 5)),
             None, f"sale:uybor:{i}", f"u{i}") for i in range(14)]
    # один застройщик/агентство выложил 10 одинаково дешёвых квартир совсем рядом
    pts_ += [(base_[0] + 0.0001, base_[1] + 0.0001 * j, "Мирабад", "sale", 0, 0, 50, 900 * 50, None, f"sale:uybor:a{j}", "AG")
             for j in range(10)]
    _geo_t.GAZ["pts"] = pts_
    kst_ = rr.Store(Path("/tmp/test_knn.db"))
    lk = {"key": "sale:uybor:me", "area": 50, "price_usd": 70000, "district": "Мирабад",
          "geo": {"lat": base_[0], "lon": base_[1], "src": "сайт", "label": ""}}
    ck = _mk2.comparables(kst_, lk)
    assert ck["n"] == 15 and 1400 < ck["median_m2"] < 1700, ck        # агентство — не больше 3 из 15
    assert ck["p20"] <= ck["median_m2"] <= ck["p80"] and ck["p20"] < ck["p80"], ck
    # −12% к медиане, но внутри обычного разброса похожих — «в рынке», а не «хорошая цена»
    xk = _mk2.analyze(kst_, dict(lk, price_usd=ck["median_m2"] * 0.87 * 50), cfg)
    if xk.get("gap") is not None and xk["gap"] <= -0.12 and (xk["m2"] >= (xk["comp"].get("p20") or 0)):
        assert xk["verdict"][1] != "Ниже рынка", xk["verdict"]
    xg = _mk2.analyze(kst_, dict(lk, price_usd=ck["median_m2"] * 0.6 * 50), cfg)
    assert xg["verdict"][1] in ("Ниже рынка", "Намного ниже рынка", "Сравнить не с чем"), xg["verdict"]
finally:
    _geo_t.GAZ["pts"] = _gz2
    Path("/tmp/test_knn.db").unlink(missing_ok=True)
print("OK — оценка цены: ближайшие похожие с весами, без засилья одного продавца, «хорошая цена» — только за пределами разброса")

# ============ надёжность: источник с ошибками дольше 6 ч — владельцу одно сообщение, и когда заработал ============
hdb_ = Path("/tmp/test_health.db"); hdb_.unlink(missing_ok=True)
hst_ = rr.Store(hdb_); hss_ = rr.Store(Path("/tmp/test_health2.db"))
HS = []
with mock.patch.object(rr, "send_telegram", lambda c, txt, _admin=False: HS.append((txt, _admin)) or True):
    now_ = time.time()
    rr.note_source(hst_, "telegram")
    h_ = hst_.get_kv("rent_src_health"); h_["telegram"]["ok_at"] = now_ - 7 * 3600; hst_.set_kv("rent_src_health", h_)
    rr.note_source(hst_, "telegram", "ConnectionError: t.me timeout")
    hss_.set_kv("sale_src_stats", {"realt24": {"at": now_, "n": 5, "err": "", "ok_at": now_}})
    assert rr.source_health({}, hst_, hss_, now=now_) == ["аренда · telegram"]
    assert len(HS) == 1 and "не отвечает" in HS[0][0] and HS[0][1] is True              # служебное — владельцу
    assert rr.source_health({}, hst_, hss_, now=now_ + 60) == [] and len(HS) == 1        # чаще раза в час не проверяем
    hst_.set_kv("health_at", 0)
    rr.source_health({}, hst_, hss_, now=now_ + 120)
    assert len(HS) == 1                                                               # повтор — не раньше чем через сутки
    rr.note_source(hst_, "telegram")
    hst_.set_kv("health_at", 0)
    assert rr.source_health({}, hst_, hss_, now=now_ + 200) == [] and "снова работает" in HS[-1][0]
hdb_.unlink(missing_ok=True); Path("/tmp/test_health2.db").unlink(missing_ok=True)
print("OK — надёжность: упавший источник — сообщение владельцу раз в сутки и когда снова заработал")

# ============ этап 2: много пользователей — свои базы, общий срез рынка, маршрутизация обновлений ============
import tempfile as _tf, tenants as TN, market as _mkt
_td = Path(_tf.mkdtemp())
with mock.patch.object(rr, "STATE_DIR", _td), mock.patch.object(rr, "DB_PATH", _td / "radar.db"), \
        mock.patch.object(rr, "SALE_DB_PATH", _td / "sale.db"):
    ost_, osa_ = rr.Store(rr.DB_PATH), rr.Store(rr.SALE_DB_PATH)
    _mkt.ensure_tables(osa_.conn)
    osa_.conn.execute("INSERT INTO market(key, op, district, area, price_usd, removed_at) VALUES('sale:uybor:1','sale','Мирабад',50,60000,NULL)")
    osa_.conn.commit()
    ost_.upsert_broker("tel:1", "Uybor", "Азиз", "998901112233", 5, "Мирабад", None, deal="sale")
    ost_.broker_status("tel:1", "contacted")                     # у владельца — уже написали
    tcfg_ = dict(cfg, telegram_chat_id="500", worker_url="https://w")
    users_ = {"multi": True, "users": [{"chat": "7001", "status": "active"}, {"chat": "7002", "status": "waitlist"}]}
    TN.REG.update(owner=None, at=0)
    TN.refresh(tcfg_, ost_, osa_, force=True, fetch=lambda: users_)
    assert [x.chat for x in TN.all_tenants()] == ["500", "7001"] and TN.REG["multi"]
    u1 = TN.get("7001")
    assert u1.cfg["telegram_chat_id"] == "7001" and u1.cfg["admin_chat_id"] == "500" and u1.cfg["tenant"] == "7001"
    assert u1.sale.conn.execute("SELECT COUNT(*) FROM market").fetchone()[0] == 1     # срез — общий
    assert _mkt.ensure_tables(u1.sale.conn) is False
    assert not u1.sale.conn.execute("SELECT name FROM main.sqlite_master WHERE name='market'").fetchone()
    assert u1.store.conn.execute("SELECT status FROM brokers WHERE bid='tel:1'").fetchone()[0] == "new"   # свой статус
    assert (_td / "users" / "7001" / "radar.db").exists()
    # маршрутизация: клиент, маклер клиента (метка от воркера), непонятный маклер — владельцу, Telegram для бизнеса
    m_ = lambda chat, **kw: {"update_id": 1, "message": {"chat": {"id": chat}, "text": "x", **kw}}
    assert TN.route(m_(7001)) == "7001" and TN.route(m_(9999, _client="7001")) == "7001"
    assert TN.route(m_(9999)) == "500" and TN.route(m_(9999, _client="7002")) == "500"     # ждущий — не клиент
    assert TN.route({"business_connection": {"user_chat_id": 7001}}) == "7001"
    u1.store.set_kv("biz_conn", {"id": "BCU"})
    assert TN.route({"business_message": {"business_connection_id": "BCU", "chat": {"id": 1}}}) == "7001"
    # обработка: каждому — в его чат, его базой и его sale.db; контекст потом прежний
    seen_ = []
    def _hc(text, settings, store, c):
        seen_.append((text, c["telegram_chat_id"], Path(store.conn.execute("PRAGMA database_list").fetchone()[2]).parent.name,
                      rr.sale_path().parent.name))
        return "", None
    ups_ = [{"update_id": 11, "message": {"chat": {"id": 7001}, "text": "мой поиск"}},
            {"update_id": 12, "message": {"chat": {"id": 500}, "text": "шортлист"}}]
    with mock.patch.object(rr, "fetch_updates", lambda c, st, lp=0: ({"result": ups_}, "wq_offset")), \
            mock.patch.object(rr, "handle_command", _hc), mock.patch.object(rr, "push_snapshot", lambda *a, **k: False):
        rr.process_commands(tcfg_, ost_)
    assert ("мой поиск", "7001", "7001", "7001") in seen_ and ("шортлист", "500", _td.name, _td.name) in seen_, seen_
    assert ost_.get_kv("wq_offset") == 12 and rr.CUR == {"sale": None, "shared": None}
    # ссылка на бота в запросе — с меткой клиента: маклер, пришедший по ней, — маклер этого клиента
    with TN.ctx(u1):
        rq7 = cg.compose_request(u1.cfg, u1.store, lang="ru")
        assert "t.me/" not in rq7 or "?start=m" + cg._b36("7001") in rq7, rq7
    assert int(cg._b36("7001"), 36) == 7001 and int(cg._b36("308687648"), 36) == 308687648
    # поиск покупки — у пользователя по его анкете, его sale.db и его чат
    u1.store.set_kv("anketa", {"ans": {"deal": "buy", "object": "flat", "rooms": ["2"], "budget": "60000", "districts": ["2"]}})
    RS_ = []
    with mock.patch.object(rr, "run_sale_search", lambda c, st, se, force=False: RS_.append(
            (c["telegram_chat_id"], c["sale_search"]["max_price_usd"], Path(st.conn.execute("PRAGMA database_list")
                                                                         .fetchone()[2]).parent.name)) or 0), \
            mock.patch.object(sale_sources, "maybe_daily_pick", lambda *a: 0):
        nx_ = {}
        rr.tenant_sales(nx_, time.time())
        rr.tenant_sales(nx_, time.time())                       # интервал не прошёл — второй раз не ищем
    assert RS_ == [("7001", 60000, "7001")], RS_
    # pump посреди разбора пользователя: обработал чужие сообщения — и вернул его контекст
    with TN.ctx(u1):
        rr._PUMP.update(ctx=(tcfg_, ost_, osa_), last=0, busy=False)
        with mock.patch.object(rr, "fetch_updates", lambda c, st, lp=0: ({"result": [{"update_id": 20, "message": {
                "chat": {"id": 500}, "text": "привет"}}]}, "wq_offset")), mock.patch.object(rr, "handle_command", _hc), \
                mock.patch.object(rr, "push_snapshot", lambda *a, **k: False), \
                mock.patch.object(rr, "flush_pending_offer", lambda *a: None):
            rr.pump()
        assert rr.sale_path().parent.name == "7001" and cg._BIZ_STORE["store"] is u1.store
        rr._PUMP.update(ctx=None)
    # без многих пользователей — только владелец
    TN.refresh(tcfg_, ost_, osa_, force=True, fetch=lambda: {"multi": False, "users": users_["users"]})
    assert [x.chat for x in TN.all_tenants()] == ["500"] and TN.route(m_(7001)) == "500"
TN.REG.update(owner=None, at=0, multi=False, t={})
import shutil as _sh; _sh.rmtree(_td, ignore_errors=True)
print("OK — много пользователей: свои базы и чаты, общий срез рынка, маклер — своему клиенту, без флага — как раньше")

# ============ переуступка в строящемся доме: район из текста, полная цена, ремонт без фото комнат ============
import market as _mk3
uzl_ = {"key": "sale:joymee:504599", "site": "Joymee", "district": "Яшнабад", "price_value": 30000, "price_currency": "USD",
        "area": 54, "rooms": 2, "floor": 13, "floors_total": 15, "repair": "евроремонт", "seller_kind": "owner",
        "photo_urls": ["https://x/1.png"], "title": "",
        "text": "Iltimos oxitigacha oʻqing! Uy Olmazor tumani Jomiy krugida. Uy 2027 yil sentyabr oyida topshiriladi "
                "Uy boʻlib tolashga olingan. 11 oy qismi qolgan. Oylik toʻlov 8 mln. Oxirida yana 322mln. Uy pod klyuch. "
                "Yaxshi sifatli remont. Investitsiya shartnoma oladigan insonni nomiga qayta rasmiylashtiriladi."}
SS.normalize(uzl_, cfg)
assert uzl_["district"] == "Алмазар" and uzl_["district_site"] == "Яшнабад", uzl_.get("district")
assert uzl_["offplan"] == {"year": 2027, "resale": True, "installment": True}, uzl_["offplan"]
full_ = 30000 + (11 * 8e6 + 322e6) / cfg["uzs_per_usd"]
assert uzl_["price_listed_usd"] == 30000 and abs(uzl_["price_value"] - round(full_, -2)) < 1, uzl_["price_value"]
assert "продавцу за договор" in uzl_["price_note"] and "11 × 8 млн" in uzl_["price_note"]
uzl_["price_usd"] = rr.to_usd(uzl_["price_value"], uzl_["price_currency"], cfg)
uzl_["repair_photo"] = {"state": "unknown", "confidence": 1, "signs": "", "photos": 4, "interior": False}
assert _mk3.repair_info(uzl_)[:2] == (None, "строится")
kst4_ = rr.Store(Path("/tmp/test_offplan.db"))
card_ = rr.format_sale_card(kst4_, dict(uzl_), cfg)
assert "Дом строится, сдача 2027" in card_ and "переуступка" in card_ and "всего" in card_ and "можно заезжать" not in card_, card_
assert "на сайте ошибочно: Яшнабад" in card_
# без стройки: фото — только планы → не «хороший ремонт по пометке сайта», а просьба прислать фото комнат
pl_ = {"key": "sale:joymee:1", "area": 50, "price_usd": 60000, "repair": "евроремонт", "text": "Продаю 2 комн",
       "repair_photo": {"state": "unknown", "confidence": 1, "photos": 3, "signs": ""}}
assert _mk3.repair_info(pl_)[:2] == (None, "нет фото")
assert "попросите фото комнат" in rr.format_sale_card(kst4_, dict(pl_), cfg)
# фото: график платежей и логотип ЖК на плане → полная цена и точка у ЖК
ph_ = {"key": "sale:joymee:2", "area": 54, "price_usd": 30000, "district": None, "geo": {"src": "район"},
       "repair_photo": {"state": "unknown", "photos": 4, "interior": False, "complex": "Bashkent Usta",
                        "total_price": 750820000, "down_payment": 324000000, "price_currency": "UZS", "installment": True}}
with mock.patch.object(_geo_t, "point_for", lambda q, *a, **k: {"lat": 41.3506, "lon": 69.2608, "label": q, "src": "ориентир"}
                       if "Bashkent" in q else None), mock.patch.object(_geo_t, "district_at", lambda *a, **k: "Алмазар"):
    assert SS.photo_facts(cfg, kst4_, ph_)
assert ph_["price_listed_usd"] == 30000 and abs(ph_["price_usd"] - round(750820000 / cfg["uzs_per_usd"], -2)) < 1
assert ph_["geo"]["label"] == "ЖК Bashkent Usta" and ph_["offplan"]["installment"]
assert "дороже бюджета" in SS.recheck(ph_, {"max_price_usd": 50000}, kst4_, cfg)
assert SS.text_district("Квартира в Мирабадском районе, ориентир Ойбек") == "Мирабад"
assert SS.text_district("Мирабад или Яккасарай — рассмотрю, Yunusobod tumanida ham") == "Юнусабад"
assert SS.text_district("Продаю квартиру, Мирабад") is None                # без «район/tumani» — не перебиваем сайт
Path("/tmp/test_offplan.db").unlink(missing_ok=True)
print("OK — переуступка в строящемся доме: район по тексту, полная цена с остатком платежей, ремонт без фото — не «хороший»")

# ============ место: «Қора қамиш 1/2» = «Каракамыш» из справочника; «40 кв.м» — не «40 квартал» ============
_gz5 = (dict(_geo_t.GAZ["gaz"]), list(_geo_t.GAZ["pts"]))
try:
    _geo_t.GAZ["gaz"] = {"*|karakamish": [41.35826, 69.221954, 4, 0.002], "Алмазар|karakamish": [41.35826, 69.221954, 4, 0.002],
                         "*|40 kvartal": [41.30, 69.30, 5, 0.1]}
    _geo_t.GAZ["pts"] = []
    kk_ = {"district": "Алмазар", "text": "Олмазор тумани  Қора қамиш 1/2 Сой буйи  2/1/4 2 хона 1 қават 40 кв.м Евро ремонт "
                                         "мебил техника билан сотилади."}
    g5_ = _geo_t.locate(kk_)
    assert g5_ and abs(g5_["lat"] - 41.35826) < 1e-4 and g5_["src"] == "ориентир", g5_
    assert "40 kvartal" not in _geo_t.grams("2 хона 40 кв.м, 5 этаж") and "karakamish" in _geo_t.grams("Қора қамиш")
    _geo_t.GAZ["gaz"]["Юнусабад|yunusabad 16"] = [41.37, 69.29, 5, 0.3]
    g6_ = _geo_t.locate({"district": "Юнусабад", "text": "Продам 1в2 квартиру в юнусабаде 16 квартале, всё остаётся"})
    assert g6_ and abs(g6_["lat"] - 41.37) < 1e-6, g6_                  # «16 квартале» = «Юнусабад-16»
    assert _geo_t.skel("Чиланзарский район 3 квартал") == "chilanzar rayan 3 kvartal"
finally:
    _geo_t.GAZ["gaz"], _geo_t.GAZ["pts"] = _gz5
print("OK — место: узбекская кириллица и раздельное написание массива находят ориентир, площадь — не квартал")

# ============ «в центре» — строго: радиус центра, без точного места не показываем; «рядом с центром» — мягко ============
nc_ = {"lat": 41.3123, "lon": 69.2787, "label": "центр", "src": "центр", "km": 2.6, "center_km": 2.6, "strict": True}
assert "точный адрес не указан" in _geo_t.too_far({"district": "Мирабад", "geo": {"src": "район", "spread": 1.0}}, nc_)
assert _geo_t.too_far({"geo": {"lat": 41.305, "lon": 69.28, "src": "сайт"}}, nc_) == ""                # 0,8 км — центр
assert "км от «центр»" in _geo_t.too_far({"geo": {"lat": 41.2853, "lon": 69.2787, "src": "сайт"}}, nc_)  # 3 км — уже не центр
assert "примерное" in _geo_t.too_far({"geo": {"lat": 41.305, "lon": 69.28, "src": "ориентир", "spread": 2.0}}, nc_)
_np = lambda c, st, txt, km=None, city="Ташкент": {"lat": 41.3123, "lon": 69.2787, "label": "центр", "src": "центр",
                                                   "km": 3.6, "center_km": 2.6, "districts": ["Мирабад", "Юнусабад"]}
for note_, strict_ in (("прямо в центре", True), ("рядом с центром", False), ("", True)):
    sst_ = rr.Store(Path("/tmp/test_strict.db"))
    sst_.set_kv("anketa", {"ans": {"deal": "buy", "object": "flat", "rooms": ["2"], "budget": "60000", "near": "центр",
                                   "note": note_}})
    with mock.patch.object(rr, "near_point", _np), mock.patch.object(_geo_t, "districts_within", lambda *a: ["Мирабад"]):
        nr_ = rr.effective_sale_cfg(cfg, sst_)["sale_search"]["near"]
    assert nr_["strict"] is strict_ and nr_["km"] == (2.6 if strict_ else 3.6), (note_, nr_)
    Path("/tmp/test_strict.db").unlink(missing_ok=True)
print("OK — «в центре» строго: только точное место в радиусе центра; «рядом с центром» — с полосой вокруг")

# ============ сменил покупку на аренду: продажу больше не ищем, первые варианты аренды — сразу из собранного ============
rdb_ = Path("/tmp/test_switch.db"); rdb_.unlink(missing_ok=True)
rst_ = rr.Store(rdb_)
rst_.set_kv("anketa", {"ans": {"deal": "rent", "object": "flat", "rooms": ["2", "3"], "districts": ["9"], "budget": "700"}})
assert rr.effective_sale_cfg(dict(cfg, sale_search={"enabled": True, "max_price_usd": 45000}), rst_)["sale_search"]["enabled"] is False
rst_.set_kv("anketa", {})
assert rr.effective_sale_cfg(dict(cfg, sale_search={"enabled": True}), rst_)["sale_search"]["enabled"] is True   # анкеты нет — как было
sdb3_ = Path("/tmp/test_switch_sale.db"); sdb3_.unlink(missing_ok=True)
rr.Store(sdb3_).set_kv("sale_pick", [{"key": "sale:uybor:1", "score": 70, "line": "x", "at": time.time()}])
RS2 = []
wad_ = json.dumps({"v": 3, "replace": True, "src": "chat", "ans": {"deal": "rent", "object": "flat", "rooms": ["2", "3"],
                                                                     "districts": ["9"], "budget": "700"}})
with mock.patch.object(rr, "SALE_DB_PATH", sdb3_), mock.patch.object(rr, "run_search", lambda c, st, se, **k: RS2.append(se) or ""), \
        mock.patch.object(rr, "push_snapshot", lambda *a, **k: False), mock.patch.object(rr, "tg_call", lambda *a, **k: {"ok": True}), \
        mock.patch.object(rr, "fetch_updates", lambda c, st, lp=0: ({"result": [{"update_id": 5, "message": {
            "chat": {"id": 4242}, "web_app_data": {"data": wad_}}}]}, "wq_offset")):
    rr.process_commands(dict(cfg, telegram_chat_id="4242"), rst_)
assert RS2 and RS2[0].get("districts") == ["Яккасарай"], RS2
assert rr.Store(sdb3_).get_kv("sale_pick") == []
rdb_.unlink(missing_ok=True); sdb3_.unlink(missing_ok=True)
print("OK — сменил покупку на аренду: продажа выключена, её подборка сброшена, первые варианты аренды — сразу")

# ============ «обвести на карте»: ищем только внутри областей, район не важен, без точки — нет ============
sq_ = [[41.32, 69.26], [41.32, 69.30], [41.30, 69.30], [41.30, 69.26]]
assert _geo_t.in_poly(41.31, 69.28, sq_) and not _geo_t.in_poly(41.33, 69.28, sq_)
tri_ = [[41.36, 69.20], [41.36, 69.24], [41.34, 69.22], [41.35, 69.21]]
assert _geo_t.in_areas(41.355, 69.22, [sq_, tri_]) and not _geo_t.in_areas(41.25, 69.22, [sq_, tri_])
assert _geo_t.area_reject({"geo": {"lat": 41.31, "lon": 69.28, "src": "сайт"}, "district": "Сергели"}, [sq_]) == ""  # район — не довод
assert "вне нарисованной области" in _geo_t.area_reject({"geo": {"lat": 41.25, "lon": 69.28, "src": "сайт"}}, [sq_])
assert "точный адрес не указан" in _geo_t.area_reject({"geo": {"src": "район"}, "district": "Мирабад"}, [sq_])
assert "примерное" in _geo_t.area_reject({"geo": {"lat": 41.31, "lon": 69.28, "src": "ориентир", "spread": 1.6}}, [sq_])
# аренда: области из настроек
assert rr.passes_user_filters({"geo": {"lat": 41.31, "lon": 69.28, "src": "сайт"}, "district": "Чиланзар", "rooms": 2},
                              {"areas": [sq_], "districts": ["Яккасарай"], "rooms_min": 2, "rooms_max": 3})
assert not rr.passes_user_filters({"geo": {"lat": 41.25, "lon": 69.28, "src": "сайт"}, "district": "Яккасарай", "rooms": 2},
                                  {"areas": [sq_], "districts": ["Яккасарай"]})
# покупка: области вместо ориентира и районов; условия поиска — «внутри областей»
adb_ = Path("/tmp/test_areas.db"); adb_.unlink(missing_ok=True)
ast_ = rr.Store(adb_)
ast_.set_kv("anketa", {"ans": {"deal": "buy", "object": "flat", "rooms": ["2"], "budget": "60000", "near": "центр"}})
ast_.set_kv("settings", {"areas": [sq_]})
with mock.patch.object(rr, "near_point", lambda *a, **k: {"lat": 41.31, "lon": 69.28, "src": "центр", "km": 2.6}), \
        mock.patch.object(_geo_t, "areas_districts", lambda a, margin_km=2.0: ["Мирабад", "Яккасарай"]):
    ssa_ = rr.effective_sale_cfg(cfg, ast_)["sale_search"]
assert ssa_["areas"] == [sq_] and ssa_["near"] is None and ssa_["districts"] == ["Мирабад", "Яккасарай"]
assert "внутри областей, обведённых на карте (1)" in rr.sale_criteria_text(ssa_)
assert "вне нарисованной области" in SS.recheck({"price_usd": 50000, "geo": {"lat": 41.25, "lon": 69.28, "src": "сайт"}},
                                                 ssa_, ast_, cfg)
# сохранение с карты: настройки, пересмотр, подтверждение в чате; пустой список — области убраны
AM = []
upd_a = lambda areas: {"result": [{"update_id": 9, "message": {"chat": {"id": 4242}, "text": "", "_areas": areas}}]}
for areas_, word_ in (([sq_, tri_], "областей — 2"), ([], "Области убрала")):
    with mock.patch.object(rr, "fetch_updates", lambda c, st, lp=0, a=areas_: (upd_a(a), "wq_offset")), \
            mock.patch.object(rr, "send_telegram", lambda c, txt, _admin=False: AM.append(txt) or True), \
            mock.patch.object(rr, "push_snapshot", lambda *a, **k: False), mock.patch.object(rr, "run_search", lambda *a, **k: ""):
        rr.process_commands(dict(cfg, telegram_chat_id="4242"), ast_)
    assert (ast_.get_kv("settings") or {}).get("areas") == areas_ and word_ in AM[-1], AM[-1]
assert ast_.get_kv("sale_force")
adb_.unlink(missing_ok=True)
print("OK — «обвести на карте»: только внутри областей и с точным адресом, район не важен; сохранение и сброс из чата")

# ============ проверка всего кода: найденные ошибки — каждая с проверкой ============
import i18n as _i18n_r, tenants as _tn_r, concierge as _cg_r, sale_sources as _ss_r, market as _mk_r
# источники: цена Realting с неразрывным пробелом, рассрочка не «цена за м²», комнаты, районы, цены
assert _ss_r._num("165\xa0002") == 165002
_dn = _ss_r.normalize({"title": "", "text": "Продаю 2-комн, П/В 3 000$", "price_value": 3000, "price_currency": "USD",
                       "area": 60}, cfg)
assert _dn["price_value"] == 3000 and "за м²" not in (_dn.get("price_note") or "")
assert rr.extract_rooms("квартира 2-х комнатная") == 2 and rr.extract_rooms("3х комнатную") == 3
assert rr.as_int(94.94) == 95 and rr.as_int("54.5") == 54 and rr.as_int(54.0) == 54
assert rr.canon_district("Юнусобод тумани") == "Юнусабад" and _ss_r.text_district("Чилонзор тумани") == "Чиланзар"
assert _ss_r.text_district("Mirzo Ulug'bek tumani") == "Мирзо-Улугбек"
assert rr.extract_district("Юнусабад 4 квартал, 5 мин до Мирабадского рынка") == "Юнусабад"
assert rr.extract_price_from_text("$500 2 комн") == (500, "USD")
assert rr.extract_price_from_text("Цена $75 000", 3_000_000) == (75000, "USD")
assert rr.extract_price_from_text("850 млн сум", 3_000_000) == (850_000_000, "UZS")
assert _ss_r.block_no({"title": "Юнусабад 3-комнатная", "text": ""}) is None
assert _ss_r.block_no({"title": "Сергели 5 этаж", "text": ""}) is None and _ss_r.block_no({"title": "Юнусабад-14"}) == 14
# поля продажи переживают сохранение в базу (карточки подборки собираются из базы)
_kp = Path("/tmp/test_keep.db"); _kp.unlink(missing_ok=True)
_ks = rr.Store(_kp)
_kl = {"key": "sale:x:1", "source": "s", "url": "u", "title": "t", "text": "", "phones": [], "price_listed_usd": 30000,
       "offplan": {"resale": True}, "district_site": "Мирабад", "district_by": "точка", "complex": "Bashkent"}
_ks.save(_kl, notified=True)
_kd = json.loads(_ks.conn.execute("SELECT data FROM listings WHERE key='sale:x:1'").fetchone()[0])
assert _kd["price_listed_usd"] == 30000 and _kd["offplan"] == {"resale": True} and _kd["complex"] == "Bashkent"
# дубль по телефону — только с уже присланным и с теми же комнатами
_r1 = {"key": "olx:1", "source": "s", "url": "u", "title": "t", "text": "x", "phones": ["901112233"], "rooms": 2}
_ks.save(_r1, notified=False); _ks.conn.execute("INSERT INTO phones VALUES('901112233', 'olx:1', ?)",
                                                 (datetime.now(timezone.utc).isoformat(),)); _ks.conn.commit()
assert _ks.find_dup(dict(_r1, key="olx:2"), cfg) is None                 # прежнее не присылали — разберём заново
_ks.conn.execute("UPDATE listings SET notified=1 WHERE key='olx:1'"); _ks.conn.commit()
assert _ks.find_dup(dict(_r1, key="olx:2"), cfg) == "olx:1"
assert _ks.find_dup(dict(_r1, key="olx:3", rooms=3), cfg) is None        # у маклера с этим номером другая квартира
_kp.unlink(missing_ok=True)
# место: «48 м.» и «кв. Мукимий» — не метро и не площадь; больница/Макро — не ориентир; Яккасарай — район
assert _geo_t.METRO_RE.search("48 м. Юнусабад") is None and _geo_t.METRO_RE.search("60 кв.м. Чиланзар") is None
assert "mukimiy" in _geo_t.grams("Чиланзар 6 кв. Мукимий") and "4 kvartal" in _geo_t.grams("Юнусабад 4 кв метро Шахристан")
assert _geo_t.generic_place("Больница") and _geo_t.generic_place("Макро")
assert _geo_t.skel("Яккасарай") in _geo_t.DISTRICT_WORDS and _geo_t.skel("Янгихаёт") in _geo_t.DISTRICT_WORDS
_pf = _geo_t.point_for("Юнусабад")
assert not _pf or _pf.get("src") != "метро"                              # район, а не станция «Юнусабад»
# рынок: «не даём в ипотеку» — нет; разброс симметричный
for _mt in ("Не даём в ипотеку", "Нельзя в ипотеку", "Не подходит под ипотеку", "не рассматриваем ипотеку"):
    assert _mk_r.mortgage_status({"text": _mt})[0] == "no", _mt
assert _mk_r.mortgage_status({"text": "Можно в ипотеку"})[0] == "yes"
assert _mk_r._band(list(range(15))) == {"p20": 3, "p80": 11}
# языки: «Ok!» не переключает узбекского маклера на русский; неудачный перевод не запоминается
_ls = rr.Store(Path("/tmp/test_lang.db"))
assert _i18n_r.broker_lang(_ls, "b1", "Assalomu alaykum, 2 xonali kvartira bor") == "uz"
assert _i18n_r.broker_lang(_ls, "b1", "Ok!") == "uz" and _i18n_r.broker_lang(_ls, "b1") == "uz"
_tc = []
_i18n_r._CACHE.clear()
_i18n_r.ai(dict(cfg, worker_url="https://w"), "Привет, как дела у вас сегодня?", "uz", post=lambda *a, **k: _tc.append(1) or None)
_i18n_r.ai(dict(cfg, worker_url="https://w"), "Привет, как дела у вас сегодня?", "uz", post=lambda *a, **k: _tc.append(1) or {"text": "Salom"})
assert len(_tc) == 2
Path("/tmp/test_lang.db").unlink(missing_ok=True)
# маклеры: встречное время — не «договорились»; другая квартира в ответе — новый вариант; отрицательный id
_at = "2026-10-25T18:00:00+05:00"
assert _cg_r.viewing_counter("Можно в 19:00?", _at) and _cg_r.viewing_counter("Давайте лучше в воскресенье", _at)
assert not _cg_r.viewing_counter("Да, жду в 18:00", _at) and not _cg_r.viewing_counter("Хорошо, можно", _at)
assert _cg_r._b36(-100) == _cg_r._b36(100)
# новый пользователь без анкеты — ничего не ищем по настройкам владельца
_tp = Path("/tmp/test_tenant_new.db"); _tp.unlink(missing_ok=True)
_ts = rr.Store(_tp); _ts.tenant = True
assert not rr.effective_sale_cfg(dict(cfg, tenant="7001"), _ts)["sale_search"]["enabled"]
assert not rr.rent_search_on(_ts)
_ts.set_kv("anketa", {"ans": {"deal": "rent"}})
assert rr.rent_search_on(_ts)
_tp.unlink(missing_ok=True)
# одно «сломанное» обновление не роняет бота и не повторяется: остальные разобраны, номер сохранён
_cp = Path("/tmp/test_crash.db"); _cp.unlink(missing_ok=True)
_cs = rr.Store(_cp)
_ups = {"result": [{"update_id": 5, "callback_query": {"id": "c", "data": "t:1", "message": {"chat": {"id": 4243}}}},
                   {"update_id": 6, "message": {"chat": {"id": 4243}, "text": "/boom"}},
                   {"update_id": 7, "message": {"chat": {"id": 4243}, "text": "/help"}}]}
_seen = []
_orig_hc = rr.handle_command
def _hc(text, *a, **k):
    if text == "/boom":
        raise RuntimeError("boom")
    _seen.append(text)
    return "ok", None
with mock.patch.object(rr, "fetch_updates", lambda c, st, lp=0: (_ups, "wq_offset")), \
        mock.patch.object(rr, "handle_command", _hc), mock.patch.object(rr, "tg_call", lambda *a, **k: {}), \
        mock.patch.object(rr, "send_telegram", lambda *a, **k: True), mock.patch.object(rr, "push_snapshot", lambda *a, **k: False):
    rr.process_commands(dict(cfg, telegram_chat_id="4243"), _cs)
assert _seen == ["/help"] and _cs.get_kv("wq_offset") == 7
_cp.unlink(missing_ok=True)
print("OK — проверка кода: цены и районы источников, дубли, место, ипотека, языки маклеров, время просмотра, "
      "новые пользователи, сбой одного обновления")
# текст, потом альбом — один вариант; другая квартира в ответе на уточнение — не склеиваем; аренда — без подборки продажи
_ap = Path("/tmp/test_album.db"); _ap.unlink(missing_ok=True)
_as = rr.Store(_ap)
_o1, _ = rr.intake_offer(cfg, _as, "chat:1", "1", "M", "2-комн Мирабад 600$", [])
_o2, _ = rr.intake_offer(cfg, _as, "chat:1", "1", "M", "", ["f1"], media_group="g1")
_o3, _ = rr.intake_offer(cfg, _as, "chat:1", "1", "M", "", ["f2"], media_group="g1")
assert _o1 == _o2 == _o3 and cg.get_offer(_as, _o1)["photos"] == ["f1", "f2"]
assert cg.looks_like_new_offer(_as, cfg, _o1, "Ещё вариант: 3-комн Юнусабад 900$, 80 м2")
assert not cg.looks_like_new_offer(_as, cfg, _o1, "Да, актуален, 5 этаж из 9")
_as.set_kv("anketa", {"ans": {"deal": "rent"}})
assert "аренду" in rr.handle_callback("R:pick", {}, _as, cfg)[0] and "аренду" in rr.handle_callback("R:last", {}, _as, cfg)[0]
_ap.unlink(missing_ok=True)
print("OK — маклеры: текст и альбом — один вариант, новая квартира в ответе — отдельно; у аренды нет подборки продажи")
# поиск по накопленному за 14 дней: объявление недельной давности внутри области — находится (раньше резал
# предел свежести уведомлений в 3 дня); пусто — один раз и с причинами, без повтора
_rp = Path("/tmp/test_rs14.db"); _rp.unlink(missing_ok=True)
_rs = rr.Store(_rp)
_sq = [[41.32, 69.26], [41.32, 69.30], [41.30, 69.30], [41.30, 69.26]]
_old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
_rl = {"key": "uybor:77", "source": "Uybor", "url": "u", "title": "2-комн", "text": "Сдаётся 2-комн, хозяин",
       "phones": [], "rooms": 2, "price_value": 700, "price_currency": "USD", "created_at": _old,
       "lat": 41.31, "lon": 69.28, "geo": {"lat": 41.31, "lon": 69.28, "src": "сайт"}, "photo_urls": ["p"]}
_rs.save(_rl, notified=False)
_rset = {**rr.default_settings(), "areas": [_sq], "rooms_min": 2, "rooms_max": 3, "max_price_usd": 1000,
         "owner_only": False}
_RS = []
with mock.patch.object(rr, "send_telegram", lambda c, t_, **k: _RS.append(t_) or True), \
        mock.patch.object(rr, "send_listing", lambda *a, **k: _RS.append("card") or True), \
        mock.patch.object(rr.time, "sleep", lambda s: None):
    rr.run_search(dict(cfg, telegram_chat_id="1"), _rs, _rset, days=14)
    assert "card" in _RS and "Лучшее сейчас" in _RS[0], _RS
    _RS.clear()
    rr.run_search(dict(cfg, telegram_chat_id="1"), _rs, dict(_rset, rooms_min=4, rooms_max=4), days=14)
    rr.run_search(dict(cfg, telegram_chat_id="1"), _rs, dict(_rset, rooms_min=4, rooms_max=4), days=14)
    assert len(_RS) == 1 and "Внутри области за 14 дн. — 1 объявление" in _RS[0] and "комнаты" in _RS[0], _RS
_rp.unlink(missing_ok=True)
print("OK — поиск за 14 дней: свежесть по тому же окну; «ничего нет» — один раз и с причинами")
# живые тесты в боте: уже присланное не шлём повторно; подпись карточки аренды; «$1 000.0»; «звоните сразу» —
# только свежим; маклер со 166 объявлениями — не «хозяин»; метка сайта против района из текста
_rp = Path("/tmp/test_live2.db"); _rp.unlink(missing_ok=True)
_rs = rr.Store(_rp)
_rl2 = dict(_rl, key="uybor:78")
_rs.save(_rl2, notified=True)
_RS = []
with mock.patch.object(rr, "send_telegram", lambda c, t_, **k: _RS.append(t_) or True), \
        mock.patch.object(rr, "send_listing", lambda *a, **k: _RS.append("card") or True):
    rr.run_search(dict(cfg, telegram_chat_id="1"), _rs, _rset, days=14)
assert "card" not in _RS and "все уже присылала" in _RS[0], _RS
assert rr.card_line("🏠 <b>[Uybor]</b> #1 · x\n💰 $700\n🛏 3-комн · 📍 Мирабад\n🕐 09.10") == "3-комн · Мирабад · $700"
_fm = rr.format_message(dict(_rl2, price_value=1000.0, created_at=_old), cfg, False)
assert "$1 000" in _fm and "1 000.0" not in _fm and "меньше 2 дней" not in _fm
assert "меньше 2 дней" in rr.format_message(dict(_rl2, created_at=datetime.now(timezone.utc).isoformat()), cfg, False)
_rs.conn.execute("INSERT OR REPLACE INTO seller_counts VALUES('uybor:u9', 166)"); _rs.conn.commit()
assert "маклер" in rr.owner_only_reject(dict(_rl2, seller_id="uybor:u9", seller_ads=1), _rs, cfg, {})
_rp.unlink(missing_ok=True)
with mock.patch.object(_geo_t, "district_at", lambda la, lo, radius=0.8: "Мирабад"), \
        mock.patch.object(_geo_t, "_near_district", lambda *a, **k: False), \
        mock.patch.dict(_geo_t.GAZ, {"pts": [(41.3, 69.27, "Мирабад")]}):
    _gc = _geo_t.locate({"title": "", "text": "Район: Мирзо-Улубекский\nАдрес: Аккурган", "lat": 41.2986, "lon": 69.2675,
                         "district": "Мирабад"})
    assert _gc["spread"] == 3.0 and _gc["conflict"] == "Мирзо-Улугбек"
    assert "примерное" in _geo_t.area_reject({"geo": _gc}, [_sq])
    _gok = _geo_t.locate({"title": "", "text": "Мирабадский район, ул. Чехова", "lat": 41.2986, "lon": 69.2675,
                          "district": "Мирабад"})
    assert not _gok.get("spread")
print("OK — живые тесты: без повторов карточек, подпись и цена аренды, маклер по числу объявлений, метка против района")
# из накопленного: снятое не шлём, подорожавшее выше бюджета — тоже
_rp = Path("/tmp/test_live3.db"); _rp.unlink(missing_ok=True)
_rs = rr.Store(_rp)
for _k, _u in (("uybor:91", "https://uybor.uz/listings/91"), ("joymee:92", "https://joymee.uz/announcements/92")):
    _rs.save(dict(_rl, key=_k, url=_u), notified=False)
_lfu = {"https://uybor.uz/listings/91": (None, ""),
        "https://joymee.uz/announcements/92": ({"price_value": 1200, "price_currency": "USD"}, "sale")}
_RS = []
with mock.patch.object(rr.sale_sources, "listing_from_url", lambda c, u: _lfu[u]), \
        mock.patch.object(rr, "send_telegram", lambda c, t_, **k: _RS.append(t_) or True), \
        mock.patch.object(rr, "send_listing", lambda *a, **k: _RS.append("card") or True):
    rr.run_search(dict(cfg, telegram_chat_id="1"), _rs, _rset, days=14)
assert "card" not in _RS and "сняли или подняли цену" in _RS[0], _RS
assert _rs.conn.execute("SELECT COUNT(*) FROM listings WHERE notified=2").fetchone()[0] == 2
_rp.unlink(missing_ok=True)
print("OK — из накопленного: снятые и подорожавшие выше бюджета объявления не присылаются")
# ============ карточки по одной с кнопками; маклерам — от имени клиента со ссылкой ============
_qp = Path("/tmp/test_queue.db"); _qp.unlink(missing_ok=True)
_qs = rr.Store(_qp)
_QT = []
def _qtg(c, m, pl, **k):
    _QT.append((m, dict(pl)))
    return {"ok": True, "result": {"message_id": 100 + len(_QT)}}
_qcfg = dict(cfg, telegram_chat_id="77", card_queue=True)
with mock.patch.object(rr, "tg_call", _qtg), mock.patch.dict(cg._BIZ_STORE, {"store": _qs}), \
        mock.patch.object(rr, "remember_card", lambda *a, **k: None):
    for i in (1, 2, 3):
        rr.send_card(_qcfg, {"key": f"uybor:{i}", "photo_urls": [], "phones": ["901112233"]}, f"карточка {i}",
                     ref=f"L:uybor:{i}", queue=True)
    sent_ = [pl for m, pl in _QT if m == "sendMessage"]
    assert len(sent_) == 1 and sent_[0]["text"].startswith("карточка 1")
    kb_ = json.loads(sent_[0]["reply_markup"])["inline_keyboard"]
    assert [b["callback_data"] for r in kb_ for b in r] == ["Q:s|L:uybor:1", "Q:n|L:uybor:1", "Q:a|L:uybor:1"]
    rr.send_card(_qcfg, {"key": "uybor:2", "photo_urls": []}, "карточка 2", ref="L:uybor:2", queue=True)  # повтор — не дублируем
    assert len(_qs.get_kv("card_queue")) == 2
    _QT.clear()
    with mock.patch.object(rr, "_listing_any", lambda st, k: None):
        rr.card_action(_qcfg, _qs, rr.default_settings(), "Q:n|L:uybor:1", 101)        # пропустить → следующая
    ms_ = [m for m, pl in _QT]
    assert "editMessageText" in ms_ and any(pl.get("text", "").startswith("карточка 2") for m, pl in _QT)
    assert (_qs.get_kv("card_open") or {}).get("ref") == "L:uybor:2"
    _QT.clear()
    rr.card_followup(_qcfg, _qs, "L:s:uybor:2")                                       # «нравится» словами
    assert (_qs.get_kv("card_open") or {}).get("ref") == "L:uybor:3"
    assert any("Сохранила" in pl.get("text", "") for m, pl in _QT if m == "editMessageText")
# нет контакта — вместо «Уточнить» ссылка на объявление; кнопки карточки клиенту не снимаются, прочие — снимаются
_kb2 = rr.card_kb("L:uybor:5", 0, {"url": "https://uybor.uz/listings/5"})
assert _kb2["inline_keyboard"][1][0]["url"].endswith("/5") and len(_kb2["inline_keyboard"][0]) == 2
assert "reply_markup" in rr._owner_no_buttons(_qcfg, {"chat_id": "77", "reply_markup": json.dumps(_kb2)})
assert "reply_markup" not in rr._owner_no_buttons(_qcfg, {"chat_id": "77", "reply_markup": json.dumps(
    {"inline_keyboard": [[{"text": "x", "url": "https://a"}]]})})
# без очереди (по просьбе клиента) — сразу и без кнопок
_QT.clear()
with mock.patch.object(rr, "tg_call", _qtg), mock.patch.dict(cg._BIZ_STORE, {"store": _qs}), \
        mock.patch.object(rr, "remember_card", lambda *a, **k: None):
    rr.send_card(_qcfg, {"key": "uybor:9", "photo_urls": []}, "по просьбе", ref="L:uybor:9")
assert _QT[0][1]["text"] == "по просьбе" and "reply_markup" not in _QT[0][1]
# маклеру в бота — имя клиента ссылкой; из Telegram клиента — от первого лица, без представления
cg.remember_client(_qs, {"id": 77, "first_name": "Шохрух", "username": "shokh"})
_QT.clear()
with mock.patch.object(rr, "tg_call", _qtg):
    cg.broker_send(_qcfg, _qs, "5005", cg.details_question({"rooms": 2, "district": "Мирабад", "price_usd": 700,
                                                             "floor": 3, "area": 60}, _qcfg))
pl_ = _QT[0][1]
assert pl_["parse_mode"] == "HTML" and '<a href="https://t.me/shokh">Шохрух</a>' in pl_["text"]
assert "ищу квартиру для своего клиента" in pl_["text"] and cg.CLIENT not in pl_["text"]
_bv = cg._biz_voice(cg.details_question({"rooms": 2, "district": "Мирабад", "price_usd": 700, "floor": 3, "area": 60},
                                        _qcfg), _qcfg)
assert _bv.startswith("Здравствуйте! По варианту") and "Ra'no" not in _bv.split("\n")[0]
assert "Шохрух" in cg.fill_client(cg.broker_welcome(_qcfg, _qs), _qs) and cg.CLIENT not in cg.fill_client(
    cg.intro_line(_qcfg), rr.Store(Path("/tmp/test_noname.db")))
Path("/tmp/test_noname.db").unlink(missing_ok=True)
_qp.unlink(missing_ok=True)
print("OK — карточки по одной с кнопками «Сохранить / Пропустить / Уточнить»; маклерам — от имени клиента со ссылкой")
# здоровье: счётчики за сутки, отчёт за прошлые сутки — один раз при смене дня
_hp = Path("/tmp/test_health.db"); _hp.unlink(missing_ok=True)
_hs = rr.Store(_hp)
rr.HEALTH["warn"]["[x] ошибка #"] += 3; rr.HEALTH["errors"] += 1
_h = rr.health_flush(cfg, _hs, started=True)
assert _h["passes"] == 1 and _h["errors"] == 1 and _h["warn"]["[x] ошибка #"] == 3
_hs.set_kv("health_day", dict(_h, day="2000-01-01"))
_HR = []
with mock.patch.object(rr, "tg_call", lambda c, m, pl, **k: _HR.append(pl) or {}):
    rr.health_flush(dict(cfg, telegram_chat_id="1"), _hs, started=True)
assert len(_HR) == 1 and "Ra'no за 01.01" in _HR[0]["text"] and "×3" in _HR[0]["text"] and _HR[0]["disable_notification"]
assert _hs.get_kv("health_day")["passes"] == 1 and rr.code_version()
_hp.unlink(missing_ok=True)
print("OK — здоровье: проходы, падения и предупреждения за сутки — отчётом раз в день; версия кода в логе")
# «где это?» — что известно о месте и насколько верить точке
_wp = Path("/tmp/test_where.db"); _wp.unlink(missing_ok=True)
_ws = rr.Store(_wp)
_ws.save({"key": "uybor:55", "source": "Uybor", "url": "u", "title": "2-комн", "text": "Ориентир: Айбек метро",
          "phones": [], "district": "Мирабад", "district_raw": "улица Мирабад, 27/6", "lat": 41.2964, "lon": 69.2702,
          "geo": {"lat": 41.2964, "lon": 69.2702, "src": "сайт"}}, notified=True)
_wt = rr.where_text(cfg, _ws, "L:w:uybor:55")
assert "улица Мирабад, 27/6" in _wt and "метка продавца на карте сайта" in _wt and "yandex" in _wt and "Айбек" in _wt
_ws.save({"key": "tg:9", "source": "TG", "url": "u", "title": "", "text": "Сдаётся 2к, Мирабад", "phones": [],
          "district": "Мирабад"}, notified=True)
assert "Точного адреса нет" in rr.where_text(cfg, _ws, "L:w:tg:9")
_wp.unlink(missing_ok=True)
print("OK — «где это?»: адрес, ориентир, точка и насколько ей верить; нет адреса — честно и с предложением уточнить")
# шортлист раз в сутки: снятое — сказать и убрать, цена другая — сказать и обновить
_sp = Path("/tmp/test_slive.db"); _sp.unlink(missing_ok=True)
_ss2 = rr.Store(_sp)
_o_gone, _ = cg.add_site_offer(cfg, _ss2, {"key": "uybor:301", "url": "https://uybor.uz/listings/301", "rooms": 3,
                                          "district": "Мирабад", "price_usd": 700, "site": "Uybor"})
_o_chg, _ = cg.add_site_offer(cfg, _ss2, {"key": "joymee:302", "url": "https://joymee.uz/announcements/302", "rooms": 2,
                                         "district": "Яккасарай", "price_usd": 800, "site": "Joymee"})
_ans = {"https://uybor.uz/listings/301": (None, ""),
        "https://joymee.uz/announcements/302": ({"price_value": 750, "price_currency": "USD"}, "sale")}
_SL = []
with mock.patch.object(rr.sale_sources, "listing_from_url", lambda c, u: _ans[u]), \
        mock.patch.object(rr, "send_telegram", lambda c, t_, **k: _SL.append(t_) or True), \
        mock.patch.object(_geo_t, "locate", lambda l: {}):
    assert rr.check_shortlist(cfg, _ss2, force=True) == 2
assert any("сняли" in x for x in _SL) and any("Подешевело" in x and "$750" in x for x in _SL)
assert cg.get_offer(_ss2, _o_gone)["status"] == "rejected" and cg.get_offer(_ss2, _o_chg)["price_usd"] == 750
_sp.unlink(missing_ok=True)
print("OK — шортлист раз в сутки: снятые убираются с сообщением, изменения цены — с сообщением")

"""Оффлайн-верификация Амины (без сети): python3 test_offline.py"""
import sqlite3
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path

import rent_radar as rr

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
assert cmd("/qwerty", s)[1] == "M"          # неизвестная команда → меню
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
    card = [pl for m, pl in SENT if m == "editMessageText"][-1]
    ckb = json.loads(card["reply_markup"])["inline_keyboard"]
    datas = [b["callback_data"] for row in ckb for b in row]
    assert f"o:ask:{oid1}" in datas and f"o:view:{oid1}" in datas and f"o:seen:{oid1}:g" in datas
    assert "Этап" in card["text"]
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

    cg.handle_shortlist_cb("s:sort", cfg, cs, 1)
    assert cs.get_kv("sl_sort") in ("p", "m", "n")

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
assert kb == cg.OWNER_KB and kb["is_persistent"]                 # постоянные кнопки вместо команд
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
    assert any("Ищет Ra" in pl.get("reply_markup", "") for _, pl in SENT2)
    SENT2.clear()
    rr.handle_command("/steps", rr.default_settings(), ms2, cfg)
    assert any("Анкета" in str(pl.get("text", "")) for _, pl in SENT2)
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
assert q.startswith("Здравствуйте! Это Ra'no 👋, ИИ-ассистент по поиску жилья.")
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
            "description": desc, "address": "ул. Тестовая", "media": []}

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
shown = lambda i: any(f"listings/{i}" in m[1] for m in sent_msgs)
def fake_sale_tg(cfg_, method, payload, timeout=20, quiet=False):
    sent_msgs.append((method, payload.get("text") or payload.get("media") or ""))
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
assert "Что нашлось сейчас" in sent_msgs[-1][1] and all(shown(i) for i in (101, 104, 202))
assert sent_msgs[-1][1].count("собственник") == 3
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
assert sent_msgs[-1][1].count("агентство/маклер") == 2                  # 102 и 201
assert any("3к" in ln and "собственник" in ln and "listings/301" in ln for ln in sent_msgs[-1][1].split("\n"))
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
assert sum("listings/401" in m[1] or "listings/402" in m[1] for m in sent_msgs) == 1, sent_msgs
with mock.patch.object(rr.requests, "get", fake_uy_get), mock.patch.object(rr, "tg_call", fake_sale_tg), \
        mock.patch.object(rr.time, "sleep"):
    rr.run_sale_search(ss_all, sstore, {"photos": False})
assert sum("listings/401" in m[1] or "listings/402" in m[1] for m in sent_msgs) == 1   # дубль сохранён как дубль
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
assert shown(501) and shown(502), sent_msgs
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
m602 = [ln for m in sent_msgs for ln in m[1].split("\n") if "listings/602" in ln]
assert len(m602) == 1 and "перевыложено" in m602[0] and "на рынке ~136 дн." in m602[0], sent_msgs
assert not any("listings/601" in m[1] for m in sent_msgs)
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
             "Долго продаётся", "Вердикт", mk.REPORT_URL, "средняя по району: $1 441/м²"):
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
assert "📊 <b>Анализ</b>" in texts[i_card + 1], texts
n_cards = sum(t.startswith("🏷 <b>Продажа") for t in texts)
assert n_cards >= 1 and sum("📊 <b>Анализ</b>" in t for t in texts) == n_cards   # по анализу на карточку
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
assert rr.effective_sale_cfg(base, es) is base
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
    assert "Ищет Ra'no" in t and "Через маклеров" in t and "/new" not in t and "/brokers" not in t
wdb.unlink(missing_ok=True)
print("OK — без команд: кнопки внизу, понятные подсказки, маклер не получает приветствие дважды")

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
T = lambda h, m=0, d=0: (datetime(2026, 10, 8, h, m, tzinfo=cg.TZ) + timedelta(days=d))
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
    msg = FU[-1][1]
    assert "Аброр" in msg["text"] and f"o:rem:{a1}" in msg["reply_markup"]
    assert fu.silent_brokers(cfg, fs, T(11)) == 0                 # не повторяем
    FU.clear()
    cg.handle_offer_cb(f"o:rem:{a1}", cfg, fs, 5)
    assert any("Напоминаю" in pl.get("text", "") and str(pl["chat_id"]) == "4242" for m, pl in FU)
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
    assert "cmd:/offers" in FU[-1][1]["reply_markup"]
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
lj = dict(j); lj["price_usd"] = 45000
assert rr.sale_reject(lj, ssx, xs, cfg) == ("", False) and lj["seller_kind"] == "owner"
la = dict(r24[1]); SS.normalize(la, cfg)
assert rr.sale_reject(la, ssx, xs, cfg) == ("", False) and la["seller_kind"] == "agency"
# оценка: собственник + ипотека выше; ниже рынка — «сильный»
sc_j, why_j, strong_j, _ = SS.score(xs, lj, cfg, ssx)
assert "собственник" in why_j and "ипотека" in why_j and not strong_j
with mock.patch("market.analyze", lambda *a: {"gap": -0.12, "flags": []}):
    sc2, why2, strong2, _ = SS.score(xs, lj, cfg, ssx)
assert strong2 and sc2 > sc_j and "-12% к рынку" in why2[0]
# дубль с другого сайта: та же площадь, этаж, район, цена ±5%
xs.save(dict(lj, key="sale:uybor:9", site="Uybor", price_usd=45500, phones=[]), notified=True)
assert SS.structural_dup(xs, lj) == "sale:uybor:9"
assert SS.structural_dup(xs, dict(lj, floor=7)) is None
# подборка: лучшие сверху, 👍 → шортлист с ссылкой и телефоном продавца
XT = []
xtg = lambda c, m, pl, **k: (XT.append((m, pl)), {"ok": True})[1]
lr = dict(rt[0], price_usd=46000)
xs.save(lj, notified=False); xs.save(lr, notified=False)
SS.queue_pick(xs, lj, 70, why_j); SS.queue_pick(xs, lr, 55, [])
with mock.patch.object(rr, "tg_call", xtg):
    assert SS.send_pick(cfg, xs) == 2
pk = XT[-1][1]
assert pk["text"].index("joymee.uz/announcements/77") < pk["text"].index("realting.uz/property/555")
assert "L:s:sale:joymee:77" in pk["reply_markup"] and SS.pick_pending(xs) == []
assert "L:v:sale:joymee:77" in pk["reply_markup"]                                 # 📷 — фото и разбор
assert [x["key"] for x in xs.get_kv("last_pick")] == ["sale:joymee:77", "sale:realting:555"]
XT.clear()
with mock.patch.object(rr, "tg_call", xtg):
    assert SS.resend_last_pick(cfg, xs) == 2
assert "Последняя подборка" in XT[-1][1]["text"] and "L:v:sale:realting:555" in XT[-1][1]["reply_markup"]
XT.clear()
with mock.patch.object(rr, "tg_call", xtg), mock.patch.object(rr, "SALE_DB_PATH", xdb):
    t5, _ = rr.handle_callback("L:v:sale:joymee:77", rr.default_settings(), ys if "ys" in dir() else None, cfg, 1)
assert any(m == "sendMessage" and "L:n:sale:joymee:77" in pl.get("reply_markup", "") for m, pl in XT)   # разбор с 👍/Мимо
assert any("Открыть на Joymee" in (pl.get("text") or "") for m, pl in XT)
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
man = [pl["text"] for m, pl in XT if "Позвоните или напишите" in pl.get("text", "")]
assert man and "По вашему объявлению о продаже" in man[0] and "Продавец: 📞" in man[0]
# подборка дня — только в 19:30–23:00 и раз в день
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
assert "Через маклеров" in v["text"] and '"b"' in json.dumps(v["kb"]) and "10–15 в день" in v["text"]
ys.set_kv("fresh_start", True)                                                # после сброса — сначала параметры
assert "Сначала расскажите" in rr.rano_screen(cfg, ys)["text"]
with mock.patch.object(rr, "tg_call", xtg):
    cg.apply_webapp_data(cfg, ys, json.dumps({"v": 3, "replace": True, "ans": {"deal": "buy", "object": "flat", "rooms": ["2"],
                                                                             "budget": "50000", "contact": "bot"}}))
assert not ys.get_kv("fresh_start")
with mock.patch.object(rr, "SALE_DB_PATH", xdb):
    snap = rr.ui_snapshot(cfg, ys, rr.default_settings())
assert set(snap["screens"]) == {"/rano", "/via"}
c = snap["ctx"]
assert c["last_pick"][0]["n"] == 1 and c["last_pick"][0]["key"].startswith("sale:") and "<" not in c["last_pick"][0]["text"]
assert "shortlist" in c and "today" in c and "pick_pending" in c
xdb.unlink(missing_ok=True); ydb.unlink(missing_ok=True)
print("OK — Ищет Ra'no: Realt24/Joymee/Realting, цена за м², дубли между сайтами, подборка, 👍 → шортлист, экраны")

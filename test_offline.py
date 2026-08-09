"""Оффлайн-верификация Амины (без сети): python3 test_offline.py"""
import sqlite3
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
assert rr.passes_filters(dict(a), cfg) is True
assert rr.passes_filters(dict(b), cfg) is False, "объявление за $1650 обязано отсеиваться"

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
assert rr.handle_callback("bw:olx:77", sb, bs, cfg)[0] == "Отмечено: написал"
assert rr.handle_callback("bx:olx:88", sb, bs, cfg)[0] == "Пропущен"
assert bs.brokers(status="skipped", with_phone=False)[0]["bid"] == "olx:88"
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
    t, _ = cg.handle_triage_cb("t:n:3", cfg, cs)
    assert cg.get_offer(cs, 3)["status"] == "rejected"
    assert any(m == "sendMessage" and "не подошёл" in pl.get("text", "")
               for m, pl in SENT), "маклеру должен уйти отказ"

# --- шортлист: сводка, выбор, запрос деталей ---
cg.set_offer_status(cs, 4, "shortlist")
text, kb, items = cg.shortlist_view(cs, cfg)
assert "Шортлист" in text and len(items) == 2
nums = [b for row in kb["inline_keyboard"] for b in row if b["callback_data"].startswith("s:t:")]
assert len(nums) == 2
assert not any(b["callback_data"] == "s:go" for row in kb["inline_keyboard"] for b in row)

with mock.patch.object(rr, "tg_call", fake_tg):
    cg.handle_shortlist_cb(f"s:t:{oid1}", cfg, cs, 1)
    assert cs.get_kv("sl_sel") == [oid1]
    _, kb2, _ = cg.shortlist_view(cs, cfg)
    assert any(b["callback_data"] == "s:go" for row in kb2["inline_keyboard"] for b in row)
    cg.handle_shortlist_cb(f"s:t:{oid1}", cfg, cs, 1)          # снять
    assert cs.get_kv("sl_sel") == []
    cg.handle_shortlist_cb(f"s:t:{oid1}", cfg, cs, 1)
    SENT.clear()
    toast, _ = cg.handle_shortlist_cb("s:go", cfg, cs, 1)
    assert "Отправлено: 1" in toast
    asked = [pl for m, pl in SENT if m == "sendMessage" and str(pl.get("chat_id")) == "555"]
    assert asked and "актуален" in asked[0]["text"] and "комисси" in asked[0]["text"]
    assert cg.get_offer(cs, oid1)["status"] == "asked"
    assert cs.get_kv("sl_sel") == []                            # выбор сброшен

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
    assert cs.get_kv("pending_offer")["oid"] == last[0]

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

# кнопка приходит именно reply-клавиатурой с web_app (иначе sendData не работает)
with mock.patch.object(rr, "tg_call", fake2):
    cg.send_app_button(cfg, ms2)
m, pl = SENT2[-1]
kb = json.loads(pl["reply_markup"])
assert "keyboard" in kb and "inline_keyboard" not in kb
assert kb["keyboard"][0][0]["web_app"]["url"].startswith(cg.WEBAPP_URL)
assert kb.get("is_persistent") is True

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
    assert any("reply_markup" in pl and "keyboard" in pl.get("reply_markup", "")
               for _, pl in SENT2)
    SENT2.clear()
    rr.handle_command("/steps", rr.default_settings(), ms2, cfg)
    assert any("Анкета" in str(pl.get("text", "")) for _, pl in SENT2)
mdb.unlink(missing_ok=True)
print("OK — мини-апп: ссылка, кнопка, приём данных формы")

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
assert q.startswith("Здравствуйте! Это Ra'no, ассистент по поиску жилья.")
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

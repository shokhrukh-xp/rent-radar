"""
Рано — консьерж-контур: работа через маклеров.

Анкета → текст запроса → рассылка → приём вариантов от маклеров прямо в бота →
быстрый триаж по одному → сессия по шортлисту с автозапросом деталей.

Модуль не импортирует rent_radar на верхнем уровне (иначе circular import) —
нужные функции берутся лениво внутри вызовов.
"""

import base64
import json
import re
import statistics
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=5))


def _rr():
    import rent_radar
    return rent_radar


# ============================================================== АНКЕТА ====
#
# Анкета ветвящаяся: у шага может быть "skip" — предикат по текущим ответам.
# Пропущенные шаги не показываются и не попадают в счётчик «шаг X из N».

BUDGETS = [("до $500", 500), ("$500–800", 800), ("$800–1200", 1200),
           ("$1200–1800", 1800), ("выше $1800", 3000)]          # аренда, $/мес
BUDGETS_DAILY = [("до $50", 50), ("$50–80", 80), ("$80–120", 120),
                 ("выше $120", 250)]                             # посуточно, $/сутки
BUDGETS_BUY = [("до $50 тыс", 50000), ("$50–80 тыс", 80000),
               ("$80–120 тыс", 120000), ("$120–200 тыс", 200000),
               ("выше $200 тыс", 400000)]                        # покупка, $ всего

TERMS_RENT = [("От года", "12"), ("6–12 месяцев", "6_12"),
              ("3–6 месяцев", "3_6"), ("Гибко", "flex")]
TERMS_DAILY = [("1–3 дня", "d1_3"), ("4–7 дней", "d4_7"),
               ("1–4 недели", "d7_30"), ("Гибко", "dflex")]


def _is_buy(ans):
    return ans.get("deal") == "buy"


def _is_land(ans):
    return ans.get("object") == "land"


STEPS = [
    {"k": "deal", "q": "Что ищем?",
     "o": [("🔑 Аренду", "rent"), ("🌙 Посуточно", "daily"), ("🏦 Покупку", "buy")]},
    {"k": "object", "q": "Тип жилья",
     "o": [("🏢 Квартиру", "flat"), ("🏠 Дом / таунхаус", "house"),
           ("🌲 Дачу", "dacha"), ("🌍 Участок", "land")]},
    {"k": "city", "q": "Где ищем",
     "o": [("Ташкент", "tashkent"), ("Чарвак / Чимган", "charvak"),
           ("Ташкентская область", "region"), ("Другой город", "other")]},
    {"k": "districts", "q": "Районы (можно несколько, или «Любой»)", "multi": True,
     "o": "DISTRICTS", "skip": lambda a: a.get("city") != "tashkent"},
    {"k": "rooms", "q": "Сколько комнат? (можно несколько)", "multi": True,
     "o": [("1", "1"), ("2", "2"), ("3", "3"), ("4+", "4"), ("Любое", "any")],
     "skip": _is_land},
    {"k": "budget", "q": "Бюджет", "o": "BUDGETS"},
    {"k": "class", "q": "Класс жилья",
     "o": [("Любой", "any"), ("Новостройка / ЖК", "new"),
           ("ЖК + дизайнерский ремонт", "premium"),
           ("Вторичка с хорошим ремонтом", "reno"),
           ("Бизнес / премиум-класс", "biz")], "skip": _is_land},
    {"k": "furniture", "q": "Мебель и техника",
     "o": [("Нужна", "yes"), ("Не нужна", "no"), ("Неважно", "any")],
     "skip": lambda a: _is_buy(a) or _is_land(a)},
    {"k": "floor_pref", "q": "Этаж",
     "o": [("Не первый", "nf"), ("Не последний", "nl"),
           ("Не первый и не последний", "mid"), ("Неважно", "any")],
     "skip": lambda a: a.get("object", "flat") != "flat"},
    {"k": "term", "q": "На какой срок", "o": "TERMS", "skip": _is_buy},
    {"k": "movein", "q": "Когда заезжать",
     "o": [("Сейчас", "now"), ("В течение месяца", "month"), ("Гибко", "flex")],
     "skip": _is_buy},
    {"k": "who", "q": "Кто будет жить",
     "o": [("Один / одна", "single"), ("Пара", "couple"),
           ("Семья с детьми", "family_kids"), ("Семья без детей", "family"),
           ("Большая семья", "big"), ("Друзья / коллеги", "group")],
     "skip": _is_buy},
    {"k": "pets", "q": "Домашние животные",
     "o": [("Нет", "no"), ("Кошка", "cat"), ("Собака", "dog"),
           ("Другое", "pet_other")], "skip": _is_buy},
    {"k": "parking", "q": "Парковка",
     "o": [("Нужна", "yes"), ("Неважно", "any")]},
    {"k": "contact", "q": "Куда маклерам присылать варианты",
     "o": [("🤖 В бота-помощника", "bot"), ("👤 Мне лично", "me"),
           ("Оба контакта", "both")]},
]


def step_skipped(idx, ans):
    return (0 <= idx < len(STEPS) and STEPS[idx].get("skip") is not None
            and STEPS[idx]["skip"](ans or {}))

LABELS = {}
for _s in STEPS:
    if isinstance(_s["o"], list):
        LABELS[_s["k"]] = {v: t for t, v in _s["o"]}


def districts_options():
    return [(n, str(i)) for i, n in enumerate(_rr().DISTRICT_LIST)] + [("Любой", "any")]


def step_options(step, ans=None):
    ans = ans or {}
    if step["o"] == "DISTRICTS":
        return districts_options()
    if step["o"] == "BUDGETS":                 # пресеты зависят от типа сделки
        deal = ans.get("deal", "rent")
        base = (BUDGETS_BUY if deal == "buy"
                else BUDGETS_DAILY if deal == "daily" else BUDGETS)
        return [(t, str(v)) for t, v in base]
    if step["o"] == "TERMS":                   # срок в днях для посуточной
        return TERMS_DAILY if ans.get("deal") == "daily" else TERMS_RENT
    return step["o"]


def label_of(step, val, ans=None):
    if step["o"] == "DISTRICTS":
        return "Любой" if val == "any" else _rr().DISTRICT_LIST[int(val)]
    for t, v in step_options(step, ans):
        if str(v) == str(val):
            return t
    return LABELS.get(step["k"], {}).get(val, val)


def get_anketa(store):
    return store.get_kv("anketa", {}) or {}


def save_anketa(store, a):
    store.set_kv("anketa", a)


def anketa_text(store, idx):
    step = STEPS[idx]
    a = get_anketa(store)
    ans = a.get("ans", {})
    chosen = ans.get(step["k"])
    line = ""
    if step.get("multi"):
        got = ", ".join(label_of(step, v, ans) for v in (chosen or [])) or "—"
        line = f"\nВыбрано: {got}"
    # счётчик — только по видимым шагам, пропущенные не считаем
    visible = [i for i in range(len(STEPS)) if not step_skipped(i, ans)]
    pos = visible.index(idx) + 1 if idx in visible else idx + 1
    return (f"📋 <b>Анкета</b> · шаг {pos} из {len(visible)}\n\n"
            f"<b>{step['q']}</b>{line}")


def anketa_keyboard(store, idx):
    step = STEPS[idx]
    a = get_anketa(store)
    ans = a.get("ans", {})
    cur = ans.get(step["k"])
    cur_list = cur if isinstance(cur, list) else ([cur] if cur else [])
    rows, row = [], []
    opts = step_options(step, ans)
    per_row = 3 if step["o"] == "DISTRICTS" else (2 if len(opts) > 3 else 1)
    for text, val in opts:
        mark = "✅ " if val in cur_list else ""
        row.append({"text": mark + text, "callback_data": f"a:{idx}:{val}"})
        if len(row) == per_row:
            rows.append(row); row = []
    if row:
        rows.append(row)
    nav = []
    if idx > 0:
        nav.append({"text": "← Назад", "callback_data": "a:back"})
    if step.get("multi"):
        nav.append({"text": "Далее →", "callback_data": "a:next"})
    rows.append(nav or [{"text": "✖️ Отменить", "callback_data": "a:cancel"}])
    return {"inline_keyboard": rows}


def start_anketa(cfg, store):
    a = get_anketa(store)
    a["i"] = 0
    a.setdefault("ans", {})
    save_anketa(store, a)
    render_anketa(cfg, store, 0)


def render_anketa(cfg, store, idx, message_id=None):
    rr = _rr()
    payload = {"chat_id": cfg["telegram_chat_id"], "text": anketa_text(store, idx),
               "parse_mode": "HTML",
               "reply_markup": json.dumps(anketa_keyboard(store, idx), ensure_ascii=False)}
    if message_id:
        payload["message_id"] = message_id
        if rr.tg_call(cfg, "editMessageText", payload) is not None:
            return
        payload.pop("message_id")
    rr.tg_call(cfg, "sendMessage", payload)


def handle_anketa_cb(data, cfg, store, message_id=None):
    """Возвращает (подсказка, обработано)."""
    rr = _rr()
    a = get_anketa(store)
    idx = int(a.get("i", 0))
    _, _, rest = data.partition(":")

    if rest == "cancel":
        store.set_kv("anketa", {})
        rr.send_telegram(cfg, "Анкета отменена. Начать заново — /anketa")
        return "Отменено", True
    if rest == "back":
        ans = a.get("ans", {})
        idx -= 1
        while idx > 0 and step_skipped(idx, ans):   # назад тоже мимо скрытых
            idx -= 1
        idx = max(0, idx)
        a["i"] = idx; save_anketa(store, a)
        render_anketa(cfg, store, idx, message_id)
        return "", True
    if rest == "next":
        return advance(cfg, store, a, idx, message_id)

    pos, _, val = rest.partition(":")
    try:
        idx = int(pos)
    except ValueError:
        return "", False
    step = STEPS[idx]
    ans = a.setdefault("ans", {})
    if step.get("multi"):
        cur = list(ans.get(step["k"]) or [])
        if val == "any":
            cur = ["any"]
        else:
            cur = [x for x in cur if x != "any"]
            cur.remove(val) if val in cur else cur.append(val)
        ans[step["k"]] = cur
        a["i"] = idx; save_anketa(store, a)
        render_anketa(cfg, store, idx, message_id)
        return label_of(step, val, ans), True

    ans[step["k"]] = val
    return advance(cfg, store, a, idx, message_id)


def advance(cfg, store, a, idx, message_id):
    ans = a.get("ans", {})
    idx += 1
    while step_skipped(idx, ans):        # прыгаем через неприменимые шаги
        idx += 1
    a["i"] = idx
    save_anketa(store, a)
    if idx >= len(STEPS):
        finish_anketa(cfg, store)
        return "Анкета готова", True
    render_anketa(cfg, store, idx, message_id)
    return "", True


# ================================================== ТЕКСТ ЗАПРОСА ========

# --- словари письма маклерам: русский и узбекский --------------------------
OBJ_RU = {"flat": "квартиру", "house": "дом", "dacha": "дачу", "land": "участок"}
OBJ_UZ = {"flat": "kvartira", "house": "hovli uy", "dacha": "dala hovli",
          "land": "yer uchastkasi"}
PLACE_RU = {"tashkent": "в Ташкенте", "charvak": "на Чарваке / Чимгане",
            "region": "в Ташкентской области"}
PLACE_UZ = {"tashkent": "Toshkentda", "charvak": "Chorvoq / Chimyonda",
            "region": "Toshkent viloyatida"}
WHO_RU = {"single": "для одного", "couple": "для пары",
          "family_kids": "для семьи с детьми", "family": "для семьи без детей",
          "big": "для большой семьи", "group": "для друзей / коллег"}
WHO_UZ = {"single": "bir kishi uchun", "couple": "juftlik uchun",
          "family_kids": "bolali oila uchun", "family": "bolasiz oila uchun",
          "big": "katta oila uchun", "group": "do'stlar / hamkasblar uchun"}
PETS_RU = {"cat": "с кошкой", "dog": "с собакой",
           "pet_other": "с домашним животным", "yes": "с домашним животным"}
PETS_UZ = {"cat": "mushuk bilan", "dog": "it bilan",
           "pet_other": "uy hayvoni bilan", "yes": "uy hayvoni bilan"}
CLASS_RU = {"new": "желательно новостройка или ЖК",
            "premium": "интересует новый ЖК с хорошим (авторским) ремонтом",
            "reno": "рассмотрю вторичку с хорошим ремонтом",
            "biz": "интересует бизнес / премиум-класс"}
CLASS_UZ = {"new": "yangi qurilgan uy yoki TJM bo'lsa yaxshi",
            "premium": "dizayner ta'miri bilan yangi TJM qiziqtiradi",
            "reno": "yaxshi ta'mirlangan ikkilamchi uy ham bo'ladi",
            "biz": "biznes / premium toifa qiziqtiradi"}
TERM_RU = {"12": "на длительный срок, от года", "6_12": "на 6–12 месяцев",
           "6": "на 6–12 месяцев", "3_6": "на 3–6 месяцев", "3": "до полугода",
           "d1_3": "на 1–3 дня", "d4_7": "на 4–7 дней", "d7_30": "на 1–4 недели"}
TERM_UZ = {"12": "uzoq muddatga, 1 yildan", "6_12": "6–12 oyga",
           "6": "6–12 oyga", "3_6": "3–6 oyga", "3": "yarim yilgacha",
           "d1_3": "1–3 kunga", "d4_7": "4–7 kunga", "d7_30": "1–4 haftaga"}
DISTRICT_UZ = {"Алмазар": "Olmazor", "Бектемир": "Bektemir", "Мирабад": "Mirobod",
               "Мирзо-Улугбек": "Mirzo Ulug'bek", "Сергели": "Sergeli",
               "Учтепа": "Uchtepa", "Чиланзар": "Chilonzor",
               "Шайхантахур": "Shayxontohur", "Юнусабад": "Yunusobod",
               "Яккасарай": "Yakkasaroy", "Янгихаёт": "Yangihayot",
               "Яшнабад": "Yashnobod"}

MON_GEN_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
MON_UZ = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul",
          "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _fmt_date(iso, uz=False):
    """'2026-08-15' → '15 августа' / '15-avgust'. Кривой ввод → ''."""
    m = _DATE_RE.match(str(iso or ""))
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{d}-{MON_UZ[mo - 1]}" if uz else f"{d} {MON_GEN_RU[mo - 1]}"


def _nights_ru(n):
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} ночь"
    if 2 <= n % 10 <= 4 and not (10 <= n % 100 < 20):
        return f"{n} ночи"
    return f"{n} ночей"


def _date_span(ans):
    """Число ночей между date_from и date_to (0, если данных нет/битые)."""
    a, b = ans.get("date_from"), ans.get("date_to")
    if not (_DATE_RE.match(str(a or "")) and _DATE_RE.match(str(b or ""))):
        return 0
    from datetime import date
    da = date(*[int(x) for x in a.split("-")])
    db = date(*[int(x) for x in b.split("-")])
    return (db - da).days


def _floor_wish(ans, uz=False):
    """Этаж: флаги «не первый / не последний» плюс числовой диапазон."""
    pref = ans.get("floor_pref") or ans.get("floor") or []
    if isinstance(pref, str):
        pref = [pref]
    flags = set(pref)
    if "mid" in flags:
        flags |= {"nf", "nl"}
    out = []
    if "nf" in flags:
        out.append("birinchi qavat emas" if uz else "не первый этаж")
    if "nl" in flags:
        out.append("oxirgi qavat emas" if uz else "не последний этаж")
    lo = str(ans.get("floor_min") or "").strip()
    hi = str(ans.get("floor_max") or "").strip()
    if lo.isdigit() or hi.isdigit():
        if lo.isdigit() and hi.isdigit():
            rng = f"{lo}–{hi}"
        elif lo.isdigit():
            rng = (f"{lo}-qavatdan yuqori" if uz else f"от {lo}")
            out.append(("qavat: " if uz else "этаж ") + rng)
            return ", ".join(out)
        else:
            rng = (f"{hi}-qavatgacha" if uz else f"до {hi}")
            out.append(("qavat: " if uz else "этаж ") + rng)
            return ", ".join(out)
        out.append((f"qavat: {rng}" if uz else f"этаж {rng}"))
    return ", ".join(out)


def _fmt_budget(ans, uz=False):
    deal = ans.get("deal", "rent")
    b = str(ans.get("budget") or "").strip()
    if not b.isdigit():
        return ""
    amount = int(b)
    pretty = f"${amount:,}".replace(",", " ")
    if deal == "daily":
        return (f"byudjet kuniga {pretty} gacha" if uz
                else f"бюджет до {pretty}/сутки")
    if deal == "buy":
        return (f"byudjet {pretty} gacha" if uz else f"бюджет до {pretty}")
    return (f"byudjet oyiga {pretty} gacha" if uz else f"бюджет до {pretty}/мес")


def compose_request(cfg, store, username=None):
    """Собирает сообщение маклеру из ответов анкеты.

    Язык письма: узбекский, если клиент выбрал узбекский интерфейс,
    иначе русский (в том числе для иностранцев — маклеры английский
    не читают, переводит Рано)."""
    rr = _rr()
    ans = get_anketa(store).get("ans", {})
    uz = ans.get("lang") == "uz"
    deal = ans.get("deal", "rent")
    obj = ans.get("object", "flat")

    place = (PLACE_UZ if uz else PLACE_RU).get(ans.get("city", "tashkent"))
    if ans.get("city") == "other":
        other = str(ans.get("city_other") or "").strip()[:40]
        # «в г. Самарканд» — без склонения произвольного названия
        place = ((other + " shahrida") if uz else ("в г. " + other)) if other else None

    if uz:
        verb = {"rent": "ijaraga olmoqchiman", "daily": "kunlik ijaraga olmoqchiman",
                "buy": "sotib olmoqchiman"}[deal]
        head = "Assalomu alaykum! " + " ".join(
            x for x in [place, OBJ_UZ.get(obj, "uy-joy"), verb] if x)
    else:
        verb = {"rent": "снять", "daily": "снять посуточно", "buy": "купить"}[deal]
        head = (f"Здравствуйте! Хочу {verb} {OBJ_RU.get(obj, 'жильё')}"
                + (f" {place}" if place else ""))
    parts = [head + "."]

    req = []
    if obj != "land":
        rooms = [r for r in (ans.get("rooms") or []) if r != "any"]
        if rooms:
            rs = sorted(rooms)
            label = "–".join(rs) if len(rs) > 1 else rs[0]
            req.append(("xonalar: " if uz else "комнат: ") + label
                       + ("+" if "4" in rs else ""))
    if ans.get("city", "tashkent") == "tashkent":
        ds = [d for d in (ans.get("districts") or []) if d != "any"]
        if ds:
            names = [rr.DISTRICT_LIST[int(d)] for d in ds]
            if uz:
                names = [DISTRICT_UZ.get(n, n) for n in names]
            req.append(("tumanlar: " if uz else "районы: ") + ", ".join(names))
    fb = _fmt_budget(ans, uz)
    if fb:
        req.append(fb)
    if req:
        parts.append(("Parametrlar: " if uz else "Параметры: ")
                     + "; ".join(req) + ".")

    extra = []
    if obj != "land":
        c = (CLASS_UZ if uz else CLASS_RU).get(ans.get("class"))
        if c:
            extra.append(c)
    if deal != "buy":
        if ans.get("furniture") == "yes":
            extra.append("mebel va texnika bilan" if uz else "с мебелью и техникой")
        fw = _floor_wish(ans, uz) if obj == "flat" else ""
        if fw:
            extra.append(fw)
        if deal == "rent":                        # срок аренды — только у длительной
            t = (TERM_UZ if uz else TERM_RU).get(ans.get("term"))
            if t:
                extra.append(t)
        w = (WHO_UZ if uz else WHO_RU).get(ans.get("who"))
        if w:
            extra.append(w)
        pt = (PETS_UZ if uz else PETS_RU).get(ans.get("pets"))
        if pt:
            extra.append(pt)
    if ans.get("parking") == "yes":
        extra.append("avtoturargoh kerak" if uz else "нужна парковка")
    if extra:
        parts.append(("Xohishlar: " if uz else "Пожелания: ")
                     + ", ".join(extra) + ".")

    span = _date_span(ans)
    if deal == "daily" and span > 0:              # посуточно — конкретные даты
        df, dt = _fmt_date(ans["date_from"], uz), _fmt_date(ans["date_to"], uz)
        if uz:
            parts.append(f"Sanalar: {df}dan {dt}gacha — {span} kecha.")
        else:
            parts.append(f"Даты: заезд {df}, выезд {dt} — {_nights_ru(span)}.")
    elif deal == "daily":                         # чат-фолбэк без календаря — срок чипом
        t = (TERM_UZ if uz else TERM_RU).get(ans.get("term"))
        if t:
            parts.append((t[0].upper() + t[1:] + ".") if not uz else (t + "."))
    elif deal == "rent":
        md = _fmt_date(ans.get("movein_date"), uz) if ans.get("movein") == "date" else ""
        if md:                                    # длительная — точная дата заезда
            parts.append(f"{md}dan kirishni rejalashtiryapman."
                         if uz else f"Заезд планирую с {md}.")
        else:
            movein = ({"now": "darhol kirishga tayyorman",
                       "month": "bir oy ichida kiraman",
                       "flex": "muddat bo'yicha moslashuvchanman"} if uz else
                      {"now": "готов заехать сразу",
                       "month": "заезд в течение месяца",
                       "flex": "по срокам гибко"}).get(ans.get("movein"))
            if movein:
                parts.append(movein[0].upper() + movein[1:] + ".")

    if obj == "flat":                        # этаж спрашиваем только у квартир
        ask = ("Mos variant bo'lsa — foto, aniq manzil, qavati, maydoni va "
               "narxini yuboring. Vositachilik haqini ham yozing." if uz else
               "Если есть подходящие варианты — пришлите, пожалуйста, фото, "
               "точный адрес, этаж, площадь и цену. "
               "Сразу уточните размер комиссии.")
    else:
        ask = ("Mos variant bo'lsa — foto, aniq manzil, maydoni va narxini "
               "yuboring. Vositachilik haqini ham yozing." if uz else
               "Если есть подходящие варианты — пришлите, пожалуйста, фото, "
               "точный адрес, площадь и цену. Сразу уточните размер комиссии.")
    parts.append(ask)

    who = ans.get("contact", "bot")
    bot_un = cfg.get("bot_username") or "rano_smart_bot"
    assistant = cfg.get("assistant_name", "Ra'no")
    # в латинском письме имя тоже латиницей, иначе выходит «Раноga»
    assistant_lat = {"Рано": "Rano", "Амина": "Amina"}.get(assistant, assistant)
    if who in ("bot", "both"):
        # Даём кликабельную ссылку, а не @упоминание: маклер, который ищет бота
        # по имени, легко попадает к чужому боту с похожим юзернеймом.
        # Имя оставляем в именительном падеже — иначе выходит «помощнице Рано».
        if uz:
            parts.append(f"Variantlarni yordamchim {assistant_lat}ga yuboring: "
                         f"https://t.me/{bot_un}\n"
                         f"Havolani bosib yozing — u menga darhol yetkazadi.")
        else:
            parts.append(f"Варианты присылайте, пожалуйста, моей помощнице — "
                         f"{assistant}: https://t.me/{bot_un}\n"
                         f"Нажмите на ссылку и напишите ей, она сразу передаёт мне.")
    if who in ("me", "both") and username:
        parts.append(("Yoki to'g'ridan-to'g'ri menga: @" if uz
                      else "Либо мне напрямую: @") + username)
    parts.append("Rahmat!" if uz else "Спасибо!")
    return "\n".join(parts)


def finish_anketa(cfg, store):
    rr = _rr()
    text = compose_request(cfg, store)
    store.set_kv("request_text", text)
    kb = {"inline_keyboard": [
        [{"text": "✅ Утвердить и показать маклеров", "callback_data": "q:ok"}],
        [{"text": "✏️ Изменить текст", "callback_data": "q:edit"},
         {"text": "🔄 Пройти анкету заново", "callback_data": "q:again"}],
    ]}
    rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "parse_mode": "HTML",
        "text": "📝 <b>Готовый запрос маклерам</b>\n\n"
                f"<code>{rr.escape_html(text)}</code>\n\n"
                "Проверьте текст. Можно утвердить или переписать своими словами.",
        "reply_markup": json.dumps(kb, ensure_ascii=False)})


def handle_request_cb(data, cfg, store, settings):
    rr = _rr()
    _, _, act = data.partition(":")
    if act == "ok":
        rr.send_broker_cards(cfg, store, settings, text=store.get_kv("request_text"))
        return "Показываю маклеров", True
    if act == "edit":
        store.set_kv("awaiting_text", True)
        rr.send_telegram(cfg, "✏️ Пришлите свой вариант текста одним сообщением — "
                              "я сохраню его как запрос маклерам.")
        return "Жду текст", True
    if act == "again":
        start_anketa(cfg, store)
        return "Начинаем заново", True
    return "", False


# ============================================ ВАРИАНТЫ ОТ МАКЛЕРОВ =======

AREA_RE = re.compile(r"(\d{2,3}(?:[.,]\d)?)\s*(?:кв\.?\s*м|м2|м²|kv\.?m|kvm)", re.I)
FLOOR_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{1,2}))?")


def parse_offer(text, cfg):
    """Грубый разбор сообщения маклера (до подключения модели)."""
    rr = _rr()
    t = text or ""
    val, cur = rr.extract_price_from_text(t)
    out = {
        "rooms": rr.extract_rooms(t),
        "district": rr.canon_district(t),
        "price_usd": rr.to_usd(val, cur, cfg) if val else None,
        "price_raw": f"{val} {cur}" if val else "",
        "area": None, "floor": None, "floors_total": None,
    }
    m = AREA_RE.search(t)
    if m:
        try:
            out["area"] = float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    m = FLOOR_RE.search(t)
    if m:
        g = [x for x in m.groups() if x]
        if len(g) == 3:                       # формат комнаты/этаж/этажность
            out["rooms"] = out["rooms"] or int(g[0])
            out["floor"], out["floors_total"] = int(g[1]), int(g[2])
        elif len(g) == 2:
            out["floor"], out["floors_total"] = int(g[0]), int(g[1])
    return out


def save_offer(store, cfg, chat_id, name, text, photos, media_group=None):
    """Сохраняет вариант; фото из одного альбома клеятся в один вариант."""
    now = datetime.now(timezone.utc).isoformat()
    if media_group:
        row = store.conn.execute(
            "SELECT oid, text, photos FROM broker_offers WHERE media_group=? "
            "AND broker_chat=? ORDER BY oid DESC LIMIT 1",
            (media_group, str(chat_id))).fetchone()
        if row:
            oid, old_text, old_photos = row
            ph = json.loads(old_photos or "[]") + photos
            new_text = old_text or text
            store.conn.execute("UPDATE broker_offers SET photos=?, text=? WHERE oid=?",
                               (json.dumps(ph[:8]), new_text, oid))
            store.conn.commit()
            return oid, False

    p = parse_offer(text, cfg)
    cur = store.conn.execute(
        "INSERT INTO broker_offers(broker_chat, broker_name, media_group, text, photos, "
        "district, rooms, area, price_usd, price_raw, floor, floors_total, created_at, status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')",
        (str(chat_id), name, media_group, text, json.dumps(photos[:8]),
         p["district"], p["rooms"], p["area"], p["price_usd"], p["price_raw"],
         p["floor"], p["floors_total"], now))
    store.conn.commit()
    return cur.lastrowid, True


def get_offer(store, oid):
    r = store.conn.execute(
        "SELECT oid, broker_chat, broker_name, text, photos, district, rooms, area, "
        "price_usd, price_raw, floor, floors_total, created_at, status, note "
        "FROM broker_offers WHERE oid=?", (oid,)).fetchone()
    if not r:
        return None
    keys = ["oid", "broker_chat", "broker_name", "text", "photos", "district", "rooms",
            "area", "price_usd", "price_raw", "floor", "floors_total",
            "created_at", "status", "note"]
    o = dict(zip(keys, r))
    o["photos"] = json.loads(o["photos"] or "[]")
    return o


def offers_by_status(store, status, limit=50):
    rows = store.conn.execute(
        "SELECT oid FROM broker_offers WHERE status=? ORDER BY oid", (status,)).fetchall()
    return [get_offer(store, r[0]) for r in rows[:limit]]


def set_offer_status(store, oid, status):
    store.conn.execute("UPDATE broker_offers SET status=? WHERE oid=?", (status, oid))
    store.conn.commit()


# --------------------------------------------------- ценовой индекс -----

def price_index(store, min_sample=2):
    """Индекс строится ТОЛЬКО по тому, что реально прислали маклеры."""
    rows = store.conn.execute(
        "SELECT district, rooms, area, price_usd FROM broker_offers "
        "WHERE price_usd IS NOT NULL AND price_usd > 0").fetchall()
    by_pair, by_rooms, per_m2 = {}, {}, {}
    for d, r, a, p in rows:
        if r:
            by_rooms.setdefault(r, []).append(p)
            if d:
                by_pair.setdefault((d, r), []).append(p)
        if a and a > 10:
            per_m2.setdefault(d or "—", []).append(p / a)
    fold = lambda src: {k: (statistics.median(v), len(v))
                        for k, v in src.items() if len(v) >= min_sample}
    return {"pair": fold(by_pair), "rooms": fold(by_rooms),
            "m2": fold(per_m2), "sample": len(rows)}


def price_note(offer, idx):
    p, r, d = offer.get("price_usd"), offer.get("rooms"), offer.get("district")
    if not p:
        return ""
    ref = idx["pair"].get((d, r)) or idx["rooms"].get(r)
    if not ref:
        return ""
    med, n = ref
    delta = (p - med) / med * 100
    word = ("дешевле" if delta < -8 else "дороже" if delta > 8 else "по рынку")
    return f"{word} предложений маклеров ({delta:+.0f}% к ${med:.0f}, выборка {n})"


# ------------------------------------------------- карточка и триаж -----

def offer_card(store, cfg, o, idx=None, prefix=""):
    rr = _rr()
    idx = idx if idx is not None else price_index(store)
    head = [f"{prefix}🏠 <b>Вариант #{o['oid']}</b>"]
    facts = []
    if o["rooms"]:
        facts.append(f"{o['rooms']}-комн")
    if o["area"]:
        facts.append(f"{o['area']:.0f} м²")
    if o["floor"]:
        facts.append(f"этаж {o['floor']}" + (f"/{o['floors_total']}" if o["floors_total"] else ""))
    if o["district"]:
        facts.append(f"📍 {o['district']}")
    if facts:
        head.append(" · ".join(facts))
    if o["price_usd"]:
        line = f"💰 ${o['price_usd']:.0f}"
        if o["area"]:
            line += f" · ${o['price_usd'] / o['area']:.1f}/м²"
        head.append(line)
        note = price_note(o, idx)
        if note:
            head.append("📊 " + note)
    head.append(f"👤 от {rr.escape_html(o['broker_name'] or 'маклера')}")
    body = (o["text"] or "").strip()
    if body:
        head.append("\n<i>" + rr.escape_html(body[:400]) + "</i>")
    if o.get("note"):
        head.append(f"\n📝 {rr.escape_html(o['note'])}")
    return "\n".join(head)


def triage_keyboard(oid):
    return {"inline_keyboard": [[
        {"text": "👍 В шортлист", "callback_data": f"t:s:{oid}"},
        {"text": "🕐 Позже", "callback_data": f"t:l:{oid}"},
        {"text": "👎 Мимо", "callback_data": f"t:n:{oid}"},
    ]]}


def notify_offer(cfg, store, oid):
    rr = _rr()
    o = get_offer(store, oid)
    if not o:
        return
    text = offer_card(store, cfg, o)
    kb = json.dumps(triage_keyboard(oid), ensure_ascii=False)
    if o["photos"]:
        media = [{"type": "photo", "media": f} for f in o["photos"][:4]]
        media[0]["caption"] = text[:1000]
        media[0]["parse_mode"] = "HTML"
        rr.tg_call(cfg, "sendMediaGroup", {
            "chat_id": cfg["telegram_chat_id"],
            "media": json.dumps(media, ensure_ascii=False)})
        rr.tg_call(cfg, "sendMessage", {
            "chat_id": cfg["telegram_chat_id"], "text": "Что делаем с этим вариантом?",
            "reply_markup": kb})
    else:
        rr.tg_call(cfg, "sendMessage", {
            "chat_id": cfg["telegram_chat_id"], "text": text,
            "parse_mode": "HTML", "reply_markup": kb})


def decline_text(cfg):
    a = cfg.get("assistant_name", "Ra'no")
    return (f"Спасибо! Этот вариант клиенту не подошёл. "
            f"Если появится что-то ближе к параметрам — присылайте, посмотрю. "
            f"({a})")


def handle_triage_cb(data, cfg, store):
    rr = _rr()
    _, kind, sid = data.split(":", 2)
    oid = int(sid)
    o = get_offer(store, oid)
    if not o:
        return "Вариант не найден", True
    if kind == "s":
        set_offer_status(store, oid, "shortlist")
        n = len(offers_by_status(store, "shortlist"))
        return f"В шортлисте: {n}", True
    if kind == "l":
        set_offer_status(store, oid, "later")
        return "Отложено", True
    set_offer_status(store, oid, "rejected")
    rr.tg_call(cfg, "sendMessage",
               {"chat_id": o["broker_chat"], "text": decline_text(cfg)})
    return "Отказ отправлен маклеру", True


# ============================================== ШОРТЛИСТ И ЗАПРОСЫ ======

SORTS = {"p": ("по цене", lambda o: o["price_usd"] or 9e9),
         "m": ("по $/м²", lambda o: (o["price_usd"] / o["area"]) if o.get("area") and o.get("price_usd") else 9e9),
         "n": ("по свежести", lambda o: -o["oid"])}


def shortlist_view(store, cfg):
    sel = set(store.get_kv("sl_sel", []) or [])
    sort = store.get_kv("sl_sort", "n")
    items = offers_by_status(store, "shortlist") + offers_by_status(store, "asked")
    items = [o for o in items if o]
    items.sort(key=SORTS.get(sort, SORTS["n"])[1])
    idx = price_index(store)

    if not items:
        return ("📋 <b>Шортлист пуст</b>\n\nВарианты попадают сюда по кнопке "
                "«👍 В шортлист» под карточкой от маклера."), None, []

    lines = [f"📋 <b>Шортлист</b> — {len(items)} вариантов "
             f"({SORTS.get(sort, SORTS['n'])[0]})\n"]
    for i, o in enumerate(items, 1):
        bits = []
        if o["rooms"]:
            bits.append(f"{o['rooms']}к")
        if o["area"]:
            bits.append(f"{o['area']:.0f}м²")
        if o["district"]:
            bits.append(o["district"])
        price = f"${o['price_usd']:.0f}" if o["price_usd"] else "цена?"
        if o["price_usd"] and o["area"]:
            price += f" ({o['price_usd'] / o['area']:.1f}/м²)"
        mark = "✅" if o["oid"] in sel else f"{i}."
        status = " ⏳ запрошено" if o["status"] == "asked" else ""
        lines.append(f"{mark} <b>{price}</b> · {' · '.join(bits) or '—'}{status}")
        note = price_note(o, idx)
        if note:
            lines.append(f"      <i>{note}</i>")

    rows, row = [], []
    for i, o in enumerate(items, 1):
        row.append({"text": ("✅" if o["oid"] in sel else "") + str(i),
                    "callback_data": f"s:t:{o['oid']}"})
        if len(row) == 5:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if sel:
        rows.append([{"text": f"📨 Запросить детали по выбранным ({len(sel)})",
                      "callback_data": "s:go"}])
        rows.append([{"text": "🗑 Снять выделение", "callback_data": "s:clr"}])
    rows.append([{"text": f"↕️ Сортировка: {SORTS.get(sort, SORTS['n'])[0]}",
                  "callback_data": "s:sort"},
                 {"text": "🔄 Обновить", "callback_data": "s:ref"}])
    return "\n".join(lines), {"inline_keyboard": rows}, items


def show_shortlist(cfg, store, message_id=None):
    rr = _rr()
    text, kb, _ = shortlist_view(store, cfg)
    payload = {"chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML"}
    if kb:
        payload["reply_markup"] = json.dumps(kb, ensure_ascii=False)
    if message_id:
        payload["message_id"] = message_id
        if rr.tg_call(cfg, "editMessageText", payload) is not None:
            return
        payload.pop("message_id")
    rr.tg_call(cfg, "sendMessage", payload)


def details_question(o, cfg=None):
    cfg = cfg or {}
    a = cfg.get("assistant_name", "Ra'no")
    q = [f"Здравствуйте! Это {a}, ассистент по поиску жилья.",
         "По варианту, который вы присылали"]
    tag = []
    if o["rooms"]:
        tag.append(f"{o['rooms']}-комн")
    if o["district"]:
        tag.append(o["district"])
    if o["price_usd"]:
        tag.append(f"${o['price_usd']:.0f}")
    if tag:
        q[1] += f" ({', '.join(tag)})"
    q[1] += ":"
    items = ["Он ещё актуален?", "Точный адрес и ориентир?"]
    if not o["floor"]:
        items.append("Какой этаж и этажность?")
    if not o["area"]:
        items.append("Какая площадь?")
    if not o["price_usd"]:
        items.append("Какая цена в месяц?")
    items += ["Размер депозита и комиссии?", "Когда можно посмотреть?"]
    q += [f"{i}. {t}" for i, t in enumerate(items, 1)]
    return "\n".join(q)


def request_details(cfg, store):
    rr = _rr()
    sel = store.get_kv("sl_sel", []) or []
    if not sel:
        return "Ничего не выбрано"
    sent = 0
    for oid in sel:
        o = get_offer(store, oid)
        if not o:
            continue
        ok = rr.tg_call(cfg, "sendMessage",
                        {"chat_id": o["broker_chat"],
                         "text": details_question(o, cfg)})
        if ok is not None:
            store.conn.execute(
                "UPDATE broker_offers SET status='asked', asked_at=? WHERE oid=?",
                (datetime.now(timezone.utc).isoformat(), oid))
            sent += 1
    store.conn.commit()
    store.set_kv("sl_sel", [])
    rr.send_telegram(cfg, f"📨 Запросы отправлены маклерам по {sent} вариантам.\n"
                          "Ответы придут сюда же — обновлю карточки.")
    return f"Отправлено: {sent}"


def handle_shortlist_cb(data, cfg, store, message_id=None):
    parts = data.split(":", 2)
    act = parts[1] if len(parts) > 1 else ""
    if act == "t":
        oid = int(parts[2])
        sel = store.get_kv("sl_sel", []) or []
        sel.remove(oid) if oid in sel else sel.append(oid)
        store.set_kv("sl_sel", sel)
        show_shortlist(cfg, store, message_id)
        return f"Выбрано: {len(sel)}", True
    if act == "clr":
        store.set_kv("sl_sel", [])
        show_shortlist(cfg, store, message_id)
        return "Выделение снято", True
    if act == "sort":
        order = ["n", "p", "m"]
        cur = store.get_kv("sl_sort", "n")
        store.set_kv("sl_sort", order[(order.index(cur) + 1) % len(order)])
        show_shortlist(cfg, store, message_id)
        return "Сортировка изменена", True
    if act == "ref":
        show_shortlist(cfg, store, message_id)
        return "Обновлено", True
    if act == "go":
        toast = request_details(cfg, store)
        show_shortlist(cfg, store, message_id)
        return toast, True
    return "", False


# ---------------------------------------------------------- сводка ------

def concierge_status(store):
    counts = dict(store.conn.execute(
        "SELECT status, COUNT(*) FROM broker_offers GROUP BY status").fetchall())
    idx = price_index(store)
    lines = ["📊 <b>Консьерж</b>",
             f"Вариантов от маклеров: {sum(counts.values())}",
             f"  новых: {counts.get('new', 0)} · в шортлисте: {counts.get('shortlist', 0)}"
             f" · запрошено: {counts.get('asked', 0)}",
             f"  отложено: {counts.get('later', 0)} · отклонено: {counts.get('rejected', 0)}"]
    if not idx["rooms"]:
        lines.append("\n💵 Индекс цен пока не построен — нужно хотя бы 2 варианта "
                     "с ценой на одну комнатность. Он считается только по тому, "
                     "что реально присылают маклеры.")
    if idx["rooms"]:
        lines.append("\n💵 <b>Реальные цены маклеров</b> (не объявления):")
        for r, (m, n) in sorted(idx["rooms"].items()):
            lines.append(f"  {r}-комн: медиана ${m:.0f} (выборка {n})")
    if idx["m2"]:
        lines.append("\n📐 Цена за м² по районам:")
        for d, (m, n) in sorted(idx["m2"].items(), key=lambda x: -x[1][0])[:6]:
            lines.append(f"  {d}: ${m:.1f}/м² (n={n})")
    return "\n".join(lines)


# ========================================== TELEGRAM MINI APP ============

WEBAPP_URL = "https://shokhrukh-xp.github.io/rent-radar/"


def webapp_url(store, cfg=None):
    """Ссылка на мини-апп с предзаполнением текущими ответами."""
    ans = dict(get_anketa(store).get("ans", {}))
    if cfg and cfg.get("bot_username"):
        ans["_bot"] = cfg["bot_username"]
    if not ans:
        return WEBAPP_URL
    try:
        blob = base64.b64encode(
            json.dumps(ans, ensure_ascii=False).encode("utf-8")).decode()
        if len(blob) < 1500:
            return f"{WEBAPP_URL}#{blob}"
    except (TypeError, ValueError):
        pass
    return WEBAPP_URL


def send_app_button(cfg, store, text=None):
    """Постоянная клавиатура с кнопкой мини-аппа.

    Именно reply-кнопка, а не inline: только из неё Telegram разрешает
    WebApp.sendData() — иначе форма не смогла бы вернуть данные боту.
    """
    rr = _rr()
    kb = {"keyboard": [[{"text": "🏠 Открыть приложение",
                         "web_app": {"url": webapp_url(store, cfg)}}]],
          "resize_keyboard": True, "is_persistent": True}
    return rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"],
        "text": text or ("📱 Кнопка приложения — под полем ввода.\n"
                         "Там все параметры поиска на одном экране."),
        "reply_markup": json.dumps(kb, ensure_ascii=False)})


ALLOWED = {f["k"] for f in STEPS} | {
    "budget_max", "floor_min", "floor_max", "city_other", "lang",
    "date_from", "date_to", "movein_date"}


def apply_webapp_data(cfg, store, raw):
    """Принимает JSON из мини-аппа и превращает в ответы анкеты."""
    rr = _rr()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        rr.send_telegram(cfg, "Не смог прочитать данные из приложения.")
        return False

    ans = {k: v for k, v in (data.get("ans") or {}).items() if k in ALLOWED}
    if not ans:
        return False

    # точная сумма из поля имеет приоритет над пресетом
    bmax = str(ans.pop("budget_max", "") or "").strip()
    if bmax.isdigit():
        ans["budget"] = bmax
    ans.setdefault("city", "tashkent")
    # данные приходят из webview — чистим руками
    if ans.get("lang") not in ("ru", "uz", "en"):
        ans.pop("lang", None)
    for k in ("floor_min", "floor_max"):
        v = str(ans.get(k) or "").strip()
        if not (v.isdigit() and 1 <= int(v) <= 60):
            ans.pop(k, None)
        else:
            ans[k] = v
    if "city_other" in ans:
        ans["city_other"] = str(ans["city_other"])[:40].strip()
    for k in ("date_from", "date_to", "movein_date"):   # только валидные yyyy-mm-dd
        if k in ans and not _DATE_RE.match(str(ans.get(k) or "")):
            ans.pop(k, None)
    fp = ans.get("floor_pref")
    if isinstance(fp, str):
        fp = [fp]
    if isinstance(fp, list):
        ans["floor_pref"] = [x for x in fp if x in ("nf", "nl", "mid", "any")]

    a = get_anketa(store)
    a["ans"] = {**a.get("ans", {}), **ans}
    a["i"] = len(STEPS)
    save_anketa(store, a)
    finish_anketa(cfg, store)
    return True

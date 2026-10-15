"""
Рано — консьерж-контур: работа через маклеров.

Анкета → текст запроса → рассылка → приём вариантов от маклеров прямо в бота →
быстрый триаж по одному → сессия по шортлисту с автозапросом деталей.

Модуль не импортирует rent_radar на верхнем уровне (иначе circular import) —
нужные функции берутся лениво внутри вызовов.
"""

import base64
import datetime as _dt
import json
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=5))


def plural(n, one, few, many):
    """1 вариант, 2 варианта, 5 вариантов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_money(v):
    """$48 000 — с пробелом между тысячами, как в остальных карточках."""
    return "$" + f"{v:,.0f}".replace(",", " ")


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
        rr.send_telegram(cfg, "Хорошо, остановилась. Начать заново — напишите, что ищете, или /new")
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
        if ans.get("near"):                        # ориентир клиента — маклерам понятнее района
            km_ = str(ans.get("near_km") or "2").replace(".0", "").replace(".", ",")
            req.append((f"{ans['near']} atrofida, {km_} km gacha" if uz else
                        f"рядом с «{ans['near']}», до {km_} км"))
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
    note = str(ans.get("note") or "").strip().rstrip(".")
    if note:                                  # свободные пожелания из чата: ипотека, «ближе к центру»…
        extra.append(note[0].lower() + note[1:] if len(note) > 1 and not note[:2].isupper() else note)
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
    if biz_ready_store(store):
        # клиент подключил Ra'no к своему Telegram: маклер отвечает прямо в этот чат, а про ИИ-помощника
        # говорим один раз — здесь (по этой строке Ra'no и узнаёт, что запрос ушёл)
        parts.append(f"Menga {assistant_lat} — sun'iy intellekt yordamchisi yordam beradi, u shu chatda "
                     "tafsilotlarni aniqlashtirishi mumkin. Variantlarni shu yerga yuboring." if uz else
                     f"Мне помогает {assistant} — ИИ-помощник, она может уточнить детали прямо в этом чате. "
                     "Варианты присылайте сюда.")
        who = "none"
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


def request_message(cfg, store):
    """Текст запроса маклерам на проверку — с кнопкой «Утвердить и показать маклеров»."""
    rr = _rr()
    text = store.get_kv("request_text") or compose_request(cfg, store)
    store.set_kv("request_text", text)
    kb = {"inline_keyboard": [
        [{"text": "✅ Утвердить и показать маклеров", "callback_data": "q:ok"}],
        [{"text": "✏️ Изменить текст", "callback_data": "q:edit"},
         {"text": "🔄 Начать заново", "callback_data": "q:again"}],
    ]}
    rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "parse_mode": "HTML",
        "text": "📝 <b>Готовый запрос маклерам</b>\n\n"
                f"<code>{rr.escape_html(text)}</code>\n\n"
                "Гляньте, всё ли так 👀 Поправить — просто скажите, что изменить. Всё верно — скажите "
                "«напиши маклерам», подберу маклеров с этим текстом."})


def offer_brokers(cfg, store, why):
    """Сначала Ra'no ищет сама. Маклеров предлагает, только когда на сайтах пусто (или посуточно)."""
    rr = _rr()
    st = store.get_kv("search_start") or {}
    st["offered"] = datetime.now(timezone.utc).isoformat()
    store.set_kv("search_start", st)
    rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"],
        "text": why + "\n\nДавайте подключим маклеров? Скажите «напиши маклерам» — пришлю 5 маклеров "
                      "под ваш запрос: нажмёте на имя, текст уже вписан, останется «Отправить». "
                      "А на сайтах я продолжу искать сама 👀"})


def finish_anketa(cfg, store):
    """Параметры собраны: Ra'no сразу ищет сама по сайтам. Запрос маклерам готовим, но не навязываем."""
    store.set_kv("request_text", compose_request(cfg, store))
    deal = (get_anketa(store).get("ans") or {}).get("deal", "rent")
    store.set_kv("search_start", {"at": datetime.now(timezone.utc).isoformat(), "deal": deal, "offered": ""})
    if deal == "daily":                       # посуточно на сайтах почти нет — сразу маклеры
        offer_brokers(cfg, store, "🏨 Посуточную аренду на сайтах почти не выкладывают — "
                                  "быстрее всего её находят маклеры.")


def rent_settings(ans, settings, cfg=None, store=None):
    """Аренда из разговора → фильтры радара аренды (бюджет, комнаты, районы, ориентир),
    чтобы «ищу сама» шло по ним."""
    rr = _rr()
    s = dict(settings)
    b = str(ans.get("budget") or "")
    if b.isdigit():
        s["max_price_usd"] = int(b)
    rooms = sorted(int(r) for r in (ans.get("rooms") or []) if str(r).isdigit())
    if rooms:
        s["rooms_min"], s["rooms_max"] = rooms[0], (6 if rooms[-1] >= 4 else rooms[-1])
    elif ans.get("rooms_any"):
        s["rooms_min"] = s["rooms_max"] = None
    ds = [rr.DISTRICT_LIST[int(i)] for i in (ans.get("districts") or [])
          if str(i).isdigit() and int(i) < len(rr.DISTRICT_LIST)]
    s["districts"] = ds
    city = ans.get("city_other") if ans.get("city") == "other" and ans.get("city_other") else "Ташкент"
    s["near"] = rr.near_point(cfg, store, ans.get("near"), ans.get("near_km"), city=city) \
        if ans.get("near") and cfg is not None and store is not None else None
    return s


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
    if act == "show":                          # «Да, показать текст запроса»
        request_message(cfg, store)
        return "", True
    if act == "later":
        return "Хорошо, ищу дальше сама 🙂", True
    return "", False


# ============================================ ВАРИАНТЫ ОТ МАКЛЕРОВ =======

AREA_RE = re.compile(r"(\d{2,3}(?:[.,]\d)?)\s*(?:кв\.?\s*м|м2|м²|kv\.?m|kvm)", re.I)
FLOOR_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{1,2}))?")


def parse_offer(text, cfg):
    """Грубый разбор сообщения маклера (до подключения модели)."""
    rr = _rr()
    t = text or ""
    val, cur = rr.extract_price_from_text(t, max_usd=3_000_000)   # маклеры шлют и продажу
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


def merge_into_offer(store, cfg, oid, text, photos):
    """Маклер прислал вариант по частям (текст, потом фото) — дописываем в тот же."""
    o = get_offer(store, oid)
    if not o:
        return
    new_text = "\n".join(x for x in [(o["text"] or "").strip(), (text or "").strip()] if x)
    ph = (o["photos"] + photos)[:8]
    p = parse_offer(new_text, cfg)
    sets = {"text": new_text, "photos": json.dumps(ph)}
    for k in ("district", "rooms", "area", "price_usd", "price_raw", "floor", "floors_total"):
        if not o.get(k) and p.get(k):
            sets[k] = p[k]
    store.conn.execute("UPDATE broker_offers SET " + ", ".join(f"{k}=?" for k in sets)
                       + " WHERE oid=?", (*sets.values(), oid))
    store.conn.commit()


def pending_answer(store, chat_id, hours=72):
    """Вариант этого маклера, по которому ждём ответ на уточнения."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    r = store.conn.execute(
        "SELECT oid FROM broker_offers WHERE broker_chat=? AND status='asked' "
        "AND asked_at>=? ORDER BY asked_at DESC LIMIT 1", (str(chat_id), since)).fetchone()
    return r[0] if r else None


def attach_answer(cfg, store, oid, text):
    """Ответ маклера на «уточните детали» — к своему варианту, а не новой карточкой."""
    rr = _rr()
    o = get_offer(store, oid)
    if not o:
        return
    note = ((o.get("note") or "") + "\n" if o.get("note") else "") + "💬 " + (text or "").strip()[:600]
    p = parse_offer(text, cfg)
    ai = rr.ai_parse(cfg, store, text, []) if hasattr(rr, "ai_parse") else None
    if ai:
        enrich_offer(cfg, store, oid, ai, fill_only=True)
        o = get_offer(store, oid)
    sets = {"note": note[-1500:], "replied_at": datetime.now(timezone.utc).isoformat(),
            "status": "shortlist"}
    for k in ("area", "floor", "floors_total", "price_usd", "price_raw"):
        if not o.get(k) and p.get(k):
            sets[k] = p[k]
    store.conn.execute("UPDATE broker_offers SET " + ", ".join(f"{k}=?" for k in sets)
                       + " WHERE oid=?", (*sets.values(), oid))
    store.conn.commit()
    o = get_offer(store, oid)
    rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "parse_mode": "HTML",
        "text": f"💬 <b>Маклер ответил по варианту #{oid}</b> — принесла:\n\n"
                f"{rr.escape_html((text or '').strip()[:800])}\n\n"
                + offer_card(store, cfg, o)[:1500],
        "reply_markup": json.dumps({"inline_keyboard": [[
            {"text": "📂 Карточка", "callback_data": f"s:o:{oid}"},
            {"text": "📋 Шортлист", "callback_data": "s:show"}]]}, ensure_ascii=False)})


def request_summary(store) -> str:
    """Суть запроса для маклера, который пришёл в бота сам: что ищем, без просьб и ссылок."""
    text = store.get_kv("request_text") or ""
    keep = []
    for line in text.split("\n"):
        if line.startswith(("Если есть", "Mos variant", "Варианты присылайте", "Variantlarni",
                            "Нажмите на ссылку", "Havolani", "Спасибо", "Rahmat", "Либо мне", "Yoki")):
            continue
        keep.append(line.replace("Здравствуйте! ", "").replace("Assalomu alaykum! ", ""))
    return "\n".join(x for x in keep if x.strip())


def broker_welcome(cfg, store) -> str:
    a = cfg.get("assistant_name", "Ra'no")
    want = request_summary(store)
    return (f"Assalomu alaykum! Я {a} 👋, ИИ-ассистент — ищу жильё для клиента.\n"
            + (f"\nКлиент ищет:\n{want}\n" if want else "")
            + "\nПришлите подходящие варианты: фото, точный адрес, этаж, площадь, цену и "
              "комиссию — одним сообщением или альбомом, я не тороплю. Передам клиенту сразу же.\n"
              "Знаете хороший канал с объявлениями? Пришлите ссылку — добавлю в поиск 📡\n\n"
              f"Assalomu alaykum! Men {a}, AI-yordamchiman. Mos variantlarni yuboring: foto, "
              "manzil, qavat, maydon, narx va vositachilik haqi.")


def get_offer(store, oid):
    r = store.conn.execute(
        "SELECT oid, broker_chat, broker_name, text, photos, district, rooms, area, "
        "price_usd, price_raw, floor, floors_total, created_at, status, note, extra, "
        "asked_at, replied_at FROM broker_offers WHERE oid=?", (oid,)).fetchone()
    if not r:
        return None
    keys = ["oid", "broker_chat", "broker_name", "text", "photos", "district", "rooms",
            "area", "price_usd", "price_raw", "floor", "floors_total",
            "created_at", "status", "note", "extra", "asked_at", "replied_at"]
    o = dict(zip(keys, r))
    o["photos"] = json.loads(o["photos"] or "[]")
    try:
        o["extra"] = json.loads(o["extra"] or "{}")
    except (TypeError, ValueError):
        o["extra"] = {}
    return o


EXTRA_KEYS = ("address", "landmark", "repair", "building", "furniture", "commission",
              "mortgage", "documents", "summary", "deal")


def enrich_offer(cfg, store, oid, ai, fill_only=False):
    """Поля от модели — поверх regex (модель точнее); fill_only — только пустые (ответ на уточнение)."""
    rr = _rr()
    o = get_offer(store, oid)
    if not o or not ai:
        return
    sets = {}
    price = ai.get("price")
    if price:
        usd = rr.to_usd(price, ai.get("currency") or "USD", cfg)
        if usd and (not fill_only or not o.get("price_usd")):
            sets["price_usd"] = round(usd, 2)
            sets["price_raw"] = f"{price:g} {ai.get('currency') or 'USD'}"
    for k in ("rooms", "area", "floor", "floors_total", "district"):
        v = ai.get(k)
        if v and (not fill_only or not o.get(k)):
            sets[k] = v
    extra = dict(o.get("extra") or {})
    for k in EXTRA_KEYS:
        if ai.get(k) and (not fill_only or not extra.get(k)):
            extra[k] = ai[k]
    sets["extra"] = json.dumps(extra, ensure_ascii=False)
    store.conn.execute("UPDATE broker_offers SET " + ", ".join(f"{k}=?" for k in sets)
                       + " WHERE oid=?", (*sets.values(), oid))
    store.conn.commit()


def mark_as_message(cfg, store, oid):
    """Не вариант, а реплика маклера — показываем владельцу как сообщение, без карточки."""
    rr = _rr()
    o = get_offer(store, oid)
    if not o:
        return
    set_offer_status(store, oid, "message")
    rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "parse_mode": "HTML",
        "text": f"💬 <b>{rr.escape_html(o['broker_name'] or 'Маклер')}</b> пишет:\n"
                f"{rr.escape_html((o['text'] or '')[:800])}"})


def is_site_offer(o) -> bool:
    return str(o.get("broker_chat") or "").startswith("site:")


def add_site_offer(cfg, store, l):
    """Объявление с сайта → в шортлист как обычный вариант: этапы, просмотр, заметки — те же."""
    key = l.get("key") or ""
    r = store.conn.execute("SELECT oid, status FROM broker_offers WHERE broker_chat=?",
                           (f"site:{key}",)).fetchone()
    if r:
        if r[1] in ("rejected", "new", "later"):
            set_offer_status(store, r[0], "shortlist")
            return r[0], True
        return r[0], False
    extra = {"url": l.get("url"), "phones": (l.get("phones") or [])[:2], "address": l.get("district_raw"),
             "summary": ", ".join(l.get("why") or []) or None, "site_key": key,
             "building": "new" if l.get("new_building") else None, "repair": l.get("repair"),
             "mortgage": "yes" if l.get("mortgage") else None,
             "deal": "sale" if key.startswith("sale:") else "rent",
             "seller": l.get("seller") or None}
    extra = {k: v for k, v in extra.items() if v}
    text = "\n".join(x for x in (l.get("title"), (l.get("text") or "")[:500]) if x)
    pv = l.get("price_usd")
    cur = store.conn.execute(
        "INSERT INTO broker_offers(broker_chat, broker_name, text, photos, district, rooms, area, "
        "price_usd, price_raw, floor, floors_total, created_at, status, extra) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'shortlist', ?)",
        (f"site:{key}", l.get("site") or l.get("source") or "сайт", text,
         json.dumps((l.get("photo_urls") or [])[:4]), l.get("district"), l.get("rooms"), l.get("area"),
         pv, f"{pv:g} USD" if pv else None, l.get("floor"), l.get("floors_total"),
         datetime.now(timezone.utc).isoformat(), json.dumps(extra, ensure_ascii=False)))
    store.conn.commit()
    return cur.lastrowid, True


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
    return f"{word} предложений маклеров ({delta:+.0f}% к {fmt_money(med)}, выборка {n})"


# ------------------------------------------------- карточка и триаж -----

def offer_card(store, cfg, o, idx=None, prefix="", pos=None, total=None):
    rr = _rr()
    idx = idx if idx is not None else price_index(store)
    # Прогресс «N из M» ориентирует и создаёт ощущение конечного набора
    # (эффект Зейгарник + закон Миллера: видно, сколько осталось).
    if pos and total:
        head = [f"{prefix}🏠 <b>Вариант {pos} из {total}</b>"]
    else:
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
        line = f"💰 {fmt_money(o['price_usd'])}"
        if o["area"]:
            line += f" · {fmt_money(o['price_usd'] / o['area'])}/м²"
        head.append(line)
        note = price_note(o, idx)
        if note:
            head.append("📊 " + note)
    ex = o.get("extra") or {}
    where = ", ".join(x for x in (ex.get("address"), ex.get("landmark")) if x)
    if where:
        head.append(f"🗺 {rr.escape_html(where)}")
    house = ", ".join(x for x in (
        {"new": "новостройка", "secondary": "вторичка"}.get(ex.get("building"), ex.get("building")),
        ex.get("repair"),
        {"yes": "с мебелью", "no": "без мебели"}.get(ex.get("furniture"), ex.get("furniture"))) if x)
    if house:
        head.append(f"🏗 {rr.escape_html(house)}")
    money = []
    if ex.get("commission"):
        money.append("комиссия: " + ex["commission"])
    if ex.get("mortgage"):
        money.append("ипотека: " + {"yes": "да", "no": "нет"}.get(ex["mortgage"], ex["mortgage"]))
    if ex.get("documents"):
        money.append("документы: " + ex["documents"])
    if money:
        head.append("💼 " + rr.escape_html("; ".join(money)))
    if ex.get("summary"):
        head.append("✨ " + rr.escape_html(ex["summary"]))
    if ex.get("phones"):
        head.append("📞 " + ", ".join(rr.fmt_phone(p) for p in ex["phones"][:2]))
    if ex.get("url"):
        head.append(f'🔗 <a href="{rr.escape_html(ex["url"])}">Открыть объявление</a>')
    want = (get_anketa(store).get("ans") or {}).get("deal")
    got = ex.get("deal")
    if want and got and {"buy": "sale"}.get(want, want) != got:   # маклер прислал не то
        what = {"sale": "продажа", "rent": "аренда", "daily": "посуточная аренда"}
        wish = {"buy": "покупку", "rent": "аренду", "daily": "посуточную аренду"}
        head.append(f"⚠️ Это {what.get(got, got)}, а вы ищете {wish.get(want, want)}")
    head.append(f"👤 {'объявление с' if is_site_offer(o) else 'от'} {rr.escape_html(o['broker_name'] or 'маклера')}")
    body = (o["text"] or "").strip()
    if body:
        head.append("\n<i>" + rr.escape_html(body[:300 if ex else 400]) + "</i>")
    if o.get("note"):
        head.append("\n" + rr.escape_html(o["note"]).strip())
    return "\n".join(head)


def triage_keyboard(oid):
    return {"inline_keyboard": [[
        {"text": "👍 В шортлист", "callback_data": f"t:s:{oid}"},
        {"text": "🕐 Позже", "callback_data": f"t:l:{oid}"},
        {"text": "👎 Мимо", "callback_data": f"t:n:{oid}"},
    ]]}


def notify_offer(cfg, store, oid, pos=None, total=None):
    rr = _rr()
    o = get_offer(store, oid)
    if not o:
        return
    oo = dict(o)
    text = offer_card(store, cfg, oo, pos=pos, total=total)
    for cut in (200, 80, 0):                     # подпись к фото — до 1024 символов
        if rr.tg_len(text) <= rr.CAPTION_MAX:
            break
        oo["text"] = (o.get("text") or "")[:cut]
        text = offer_card(store, cfg, oo, pos=pos, total=total)
    if not o["photos"]:
        text += "\n📷 Маклер не прислал фото — без них не сравнить, попрошу при уточнении"
    text += "\n<i>Как вам? Ответьте на сообщение своими словами</i>"
    rr.send_card(cfg, offer_media(cfg, o), text, ref=f"O:{oid}")


def show_offers(cfg, store, batch=None):
    """Показ новых вариантов с «моментом ценности».

    Сначала отдаём N проверенных вариантов бесплатно (принцип взаимности:
    сначала польза, потом просьба), затем — честная подводка к остальным.
    Никаких тёмных паттернов: счётчик реальный, ничего не заблокировано."""
    rr = _rr()
    batch = batch if batch is not None else cfg.get("free_offers", 2)
    pool = offers_by_status(store, "new") + offers_by_status(store, "later")
    total = len(pool)
    if not total:
        sl = len(offers_by_status(store, "shortlist")) + len(offers_by_status(store, "asked"))
        written = store.conn.execute("SELECT COUNT(*) FROM brokers WHERE status='contacted'").fetchone()[0]
        rows = []
        if sl:
            rows.append([{"text": f"📋 Шортлист ({sl})", "callback_data": "s:show"}])
        if not written:                      # ещё никому не писали — главное действие одно
            rows.append([{"text": "📇 Разослать запрос маклерам", "callback_data": "b"}])
            hint = "\nСкажите «напиши маклерам» — подберу маклеров под ваш запрос, текст им уже будет вписан."
        else:                                # писали — ждём; подсказка про WhatsApp только здесь
            rows.append([{"text": "📇 Написать ещё маклерам", "callback_data": "b"}])
            hint = (f"\nВы написали {written} маклерам — как ответят, принесу их варианты сюда карточками. "
                    "Скажите «напиши маклерам» — подберу ещё. Кто-то ответил вам в WhatsApp — перешлите мне, "
                    "сделаю такую же карточку с разбором цены.")
        rr.tg_call(cfg, "sendMessage", {
            "chat_id": cfg["telegram_chat_id"],
            "text": ("Пока тихо — новых вариантов нет 🌙" + (f" В шортлисте — {sl}, скажите «шортлист»." if sl else "")
                     + hint),
            "reply_markup": json.dumps({"inline_keyboard": rows}, ensure_ascii=False)})
        return 0
    for i, o in enumerate(pool[:batch]):
        notify_offer(cfg, store, o["oid"], pos=i + 1, total=total)
    remaining = total - min(batch, total)
    if remaining > 0:
        _more_teaser(cfg, remaining)
    return min(batch, total)


def _more_teaser(cfg, remaining):
    """Подводка к остальным вариантам. Пик радости (первые бесплатно) +
    честная подсказка, что есть ещё — без давления и фальшивого дефицита."""
    rr = _rr()
    word = ("вариант" if remaining % 10 == 1 and remaining % 100 != 11
            else "варианта" if 2 <= remaining % 10 <= 4 and not (10 <= remaining % 100 < 20)
            else "вариантов")
    kb = {"inline_keyboard": [[
        {"text": f"Показать ещё {remaining} →", "callback_data": "off2"}]]}
    rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"],
        "text": (f"✅ Это самые близкие к вашим параметрам.\n\n"
                 f"Маклеры прислали ещё <b>{remaining} {word}</b> — скажите «покажи все», пришлю."),
        "parse_mode": "HTML",
        "reply_markup": json.dumps(kb, ensure_ascii=False)})


DECLINE_REASONS = [
    ("p", "💸 Дорого", "дороговато", "Найдётся что-то дешевле — присылайте"),
    ("d", "📍 Район", "не тот район", "Будет что-то в нужных районах — присылайте"),
    ("c", "🛠 Состояние", "не подошло состояние квартиры", "Будет вариант в лучшем состоянии — присылайте"),
    ("a", "📐 Площадь/планировка", "не подошли площадь или планировка", "Будет другая планировка — присылайте"),
    ("x", "Без причины", "", "Найдётся что-то ближе к параметрам — присылайте"),
]


def decline_text(cfg, reason=""):
    a = cfg.get("assistant_name", "Ra'no")
    r = next((x for x in DECLINE_REASONS if x[0] == reason), DECLINE_REASONS[-1])
    return (f"Rahmat за вариант! 🙏 Клиенту, увы, не подошёл" + (f" — {r[2]}" if r[2] else "") + ". "
            f"{r[3]}, с радостью посмотрю. ({a})")


def can_message_broker(o) -> bool:
    """Писать можно маклерам, которые сами писали в бота, и маклерам в чатах клиента,
    куда Ra'no подключена через Telegram для бизнеса (biz:<чат>). Пересланным — нельзя."""
    c = str(o.get("broker_chat") or "")
    return c.lstrip("-").isdigit() or (c.startswith("biz:") and biz_ready_store(None))


# ============================================== TELEGRAM ДЛЯ БИЗНЕСА ======
# Клиент подключает Ra'no к своему аккаунту (Настройки → Telegram для бизнеса → Чат-боты).
# Тогда маклер отвечает прямо в чате клиента, а Ra'no читает ответ, собирает карточку и сама
# уточняет недостающее. Первое сообщение маклеру отправляет сам клиент — в нём сказано про ИИ-помощника.

_BIZ_STORE = {"store": None}


def biz_conn(store):
    st = store or _BIZ_STORE["store"]
    return (st.get_kv("biz_conn") or {}) if st else {}


def biz_ready(store) -> bool:
    c = biz_conn(store)
    return bool(c.get("id") and c.get("enabled") and c.get("can_reply"))


def biz_ready_store(store) -> bool:
    try:
        return biz_ready(store)
    except Exception:                               # база закрыта/другой поток — считаем, что не подключено
        return False


def broker_mode(store) -> str:
    """Как клиент работает с маклерами: «biz» — Ra'no подключена к его Telegram; «manual» — без доступа:
    ответы маклеров он пересылает сам, маклер может писать и прямо в бота; «» — ещё не выбрал."""
    if biz_ready_store(store):
        return "biz"
    try:
        return (store.get_kv("broker_mode") or "") if store else ""
    except Exception:
        return ""


MANUAL_HOWTO = (
    "Хорошо, без подключения 👍 Так тоже работает:\n"
    "1. Скажите «напиши маклерам» — пришлю 5 маклеров. Нажмёте на имя — откроется чат, текст уже вписан, "
    "останется «Отправить».\n"
    "2. В тексте есть ссылка на меня — маклер может прислать варианты сразу мне.\n"
    "3. Ответил вам — перешлите мне его сообщения: нажмите на сообщение и держите → «Переслать» → Ra'no. "
    "Можно выбрать сразу несколько, с фото. Сделаю карточку с картой и разбором цены.\n"
    "4. Если в ответе чего-то не хватает, пришлю готовый текст уточнения — отправите маклеру сами.\n\n"
    "Ваши чаты я не вижу и ничего от вашего имени не пишу. Передумаете — напишите «подключить телеграм».")


def biz_guide(cfg, store=None) -> str:
    """Инструкция для любого человека: что нажать, что выбрать, что Ra'no будет видеть."""
    bot = cfg.get("bot_username") or "rano_smart_bot"
    a = cfg.get("assistant_name", "Ra'no")
    c = biz_conn(store) if store else {}
    if c.get("enabled") and c.get("can_reply"):
        return (f"✅ {a} уже подключена к вашему Telegram — ответы маклеров вижу и уточняю сама.\n\n"
                "Отключить: Настройки → Telegram для бизнеса → Чат-боты → @" + bot + " → «Отключить бота».")
    return (
        f"🔗 <b>Подключите {a} к своему Telegram — и всю переписку с маклерами я возьму на себя.</b>\n\n"
        "<b>Что вы получаете:</b>\n"
        "• Не нужно отвечать каждому маклеру и разбирать десятки сообщений — я читаю их ответы сама.\n"
        "• Фото, точный адрес, этаж, цену, документы, комиссию — уточняю у маклера сама, пока он на связи.\n"
        "• Из каждого ответа делаю карточку: карта, фото, честный разбор цены — сравнивать варианты легко.\n"
        "• Ничего не теряется и не нужно ничего пересылать: все варианты от всех маклеров — в одном месте.\n"
        "• Вам остаётся только решать: «нравится», «не то», «давай посмотрим».\n\n"
        "<b>Что остаётся за вами:</b> первое сообщение маклеру (нажать «Отправить»), а договориться о просмотре, "
        "торговаться или отказать я могу только после вашего «да».\n\n"
        "<b>Как подключить</b> — один раз, около минуты:\n"
        "<b>1.</b> Откройте <b>Настройки</b> → <b>Telegram для бизнеса</b> (Telegram Business).\n"
        "<b>2.</b> Нажмите <b>Чат-боты</b> (ChatBots).\n"
        f"<b>3.</b> Впишите <code>{bot}</code> (нажмите — скопируется) и нажмите <b>Добавить</b> (Add).\n"
        "<b>4.</b> В «Чаты, к которым у бота есть доступ» выберите <b>Только выбранные чаты</b> "
        "(Only Selected Chats) → <b>Выбрать чаты</b> (Select Chats) → отметьте <b>Новые чаты</b> (New Chats) "
        "и <b>Не контакты</b> (Non-Contacts) → <b>Готово</b>. Старые переписки и ваши контакты я не увижу.\n"
        "<b>5.</b> Ниже, в разрешениях бота, оставьте включённым <b>Управление сообщениями</b> (Manage Messages). "
        "Остальные переключатели не нужны.\n"
        "<b>6.</b> Нажмите <b>Готово</b> (Done) вверху. Если появится сообщение про подарки и звёзды — просто «OK».\n\n"
        "Всё — я сама увижу подключение и напишу вам здесь.\n\n"
        "<b>Безопасность:</b> читаю только чаты, куда вы отправили запрос маклеру; остальные переписки не открываю "
        "и не храню. Пароль и коды не нужны, отключить можно в любой момент.\n"
        "Если названия пунктов чуть другие — ищите «Чат-боты» в разделе для бизнеса.\n\n"
        "Не хотите давать доступ к чатам — так и скажите, предложу другой способ.")


def _biz_voice(text: str, cfg) -> str:
    """Из аккаунта клиента пишем от первого лица, без подписи: про ИИ-помощника сказано в первом сообщении.
    Только формы без рода («спасибо», «посмотрю», «уточните»): клиент может быть и мужчиной, и женщиной."""
    a = (cfg or {}).get("assistant_name", "Ra'no")
    t = re.sub(rf"^Здравствуйте! Это {re.escape(a)} 👋, ИИ-ассистент по поиску жилья\.\n", "", text)
    t = t.replace(f"Здравствуйте! Это снова {a} 🙂 ", "Здравствуйте! ")
    t = t.replace("Клиенту, увы, не подошёл", "Увы, не подошёл").replace("клиенту", "мне")
    return re.sub(rf"\s*\({re.escape(a)}\)\s*$", "", t)


def broker_send(cfg, store, broker_chat, text):
    """Сообщение маклеру: в бота — от бота; в чат клиента — через Telegram для бизнеса."""
    rr = _rr()
    c = str(broker_chat or "")
    if c.startswith("biz:"):
        conn = biz_conn(store)
        if not (conn.get("id") and conn.get("enabled")):
            return None
        return rr.tg_call(cfg, "sendMessage", {"business_connection_id": conn["id"], "chat_id": int(c[4:]),
                                               "text": _biz_voice(text, cfg)}, quiet=True)
    return rr.tg_call(cfg, "sendMessage", {"chat_id": broker_chat, "text": text})


def handle_triage_cb(data, cfg, store):
    rr = _rr()
    _, kind, sid = data.split(":", 2)
    oid = int(sid.partition(":")[0])
    o = get_offer(store, oid)
    if not o:
        return "Вариант не найден", True
    if kind == "s":
        set_offer_status(store, oid, "shortlist")
        n = len(offers_by_status(store, "shortlist"))
        return f"В шортлисте: {n}", True
    if kind == "l":
        set_offer_status(store, oid, "later")
        return "🕐 Отложила", True
    if kind == "n":                          # сначала причина — от неё зависит подсказка маклеру
        rows = [[{"text": t, "callback_data": f"t:r:{oid}:{c}"}] for c, t, *_ in DECLINE_REASONS]
        rr.tg_call(cfg, "sendMessage", {
            "chat_id": cfg["telegram_chat_id"],
            "text": f"Что не так с вариантом #{oid}?"
                    + (" Маклеру отвечу вежливо и подскажу, что искать." if can_message_broker(o) else ""),
            "reply_markup": json.dumps({"inline_keyboard": rows}, ensure_ascii=False)})
        return "Выберите причину", True
    reason = sid.partition(":")[2] if kind == "r" else ""
    set_offer_status(store, oid, "rejected")
    if reason:
        store.conn.execute("UPDATE broker_offers SET note=COALESCE(note,'') || ? WHERE oid=?",
                           (f"\n👎 {next((t for c, t, *_ in DECLINE_REASONS if c == reason), '')}", oid))
        store.conn.commit()
    if can_message_broker(o):
        broker_send(cfg, store, o["broker_chat"], decline_text(cfg, reason))
        return "Маклеру ответила вежливо 🙏", True
    return "👎 Поняла, мимо", True


# ============================================== ШОРТЛИСТ И ЗАПРОСЫ ======

SORTS = {"p": ("по цене", lambda o: o["price_usd"] or 9e9),
         "m": ("по $/м²", lambda o: (o["price_usd"] / o["area"]) if o.get("area") and o.get("price_usd") else 9e9),
         "n": ("по свежести", lambda o: -o["oid"])}


WD_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _when(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(TZ)
    except (TypeError, ValueError):
        return None


def when_label(v, now=None):
    """«сегодня, 18:00» / «завтра, 11:30» / «сб, 12 октября» — для этапа просмотра."""
    at = _when((v or {}).get("at"))
    if not at:
        return (v or {}).get("label") or ""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    dd = (at.date() - now.date()).days
    day = ("сегодня" if dd == 0 else "завтра" if dd == 1 else "вчера" if dd == -1 else
           f"{WD_RU[at.weekday()]}, {at.day} {MON_GEN_RU[at.month - 1]}")
    return day if v.get("notime") else f"{day}, {at:%H:%M}"


def stage_of(o, now=None):
    """Где вариант на пути к сделке: ждём ответа → ответил → просмотр → посмотрели."""
    ex = o.get("extra") or {}
    v = ex.get("viewing") or {}
    seen = ex.get("seen")
    if seen:
        return {"g": "👀 посмотрели — нравится", "m": "👀 посмотрели — думаете"}.get(seen, "👀 посмотрели")
    if v.get("at"):
        at = _when(v["at"])
        now = (now or datetime.now(TZ))
        if at and at < now - timedelta(hours=1):
            return f"📅 просмотр был {when_label(v, now)} — как прошёл?"
        return f"📅 просмотр {when_label(v, now)}"
    if o.get("replied_at"):
        return "💬 маклер ответил"
    if o.get("status") == "asked":
        return "⏳ ждём ответа маклера"
    return ""


def askable(o):
    """Ещё не спрашивали детали и не дошли до просмотра."""
    ex = o.get("extra") or {}
    return (o.get("status") == "shortlist" and not o.get("asked_at") and not o.get("replied_at")
            and not ex.get("seen") and not (ex.get("viewing") or {}).get("at"))


def shortlist_items(store, cfg, sort="n"):
    """Пункты шортлиста: (заголовок, [{oid, line, note}], сколько ещё не спрашивали) —
    общие для Python и снимка воркера."""
    items = offers_by_status(store, "shortlist") + offers_by_status(store, "asked")
    items = [o for o in items if o]
    items.sort(key=SORTS.get(sort, SORTS["n"])[1])
    idx = price_index(store)
    rows = []
    for o in items:
        bits = []
        if o["rooms"]:
            bits.append(f"{o['rooms']}к")
        if o["area"]:
            bits.append(f"{o['area']:.0f}м²")
        if o["district"]:
            bits.append(o["district"])
        price = fmt_money(o["price_usd"]) if o["price_usd"] else "цена?"
        if o["price_usd"] and o["area"]:
            price += f" ({fmt_money(o['price_usd'] / o['area'])}/м²)"
        st = stage_of(o)
        rows.append({"oid": o["oid"], "line": f"<b>{price}</b> · {' · '.join(bits) or '—'}",
                     "stage": st, "note": price_note(o, idx)})
    title = (f"📋 <b>Шортлист</b> — {len(rows)} {plural(len(rows), 'вариант', 'варианта', 'вариантов')} "
             f"({SORTS.get(sort, SORTS['n'])[0]})\n"
             f"<i>Назовите номер — открою карточку: уточнить, назначить просмотр, заметка.</i>\n"
             if rows else "")
    return title, rows, sum(1 for o in items if askable(o))


SL_EMPTY = ("📋 <b>Шортлист пока пуст</b> — но это ненадолго 😉\n\nЧтобы добавить вариант, ответьте на его "
            "карточку: «в шортлист».")


def shortlist_view(store, cfg):
    sort = store.get_kv("sl_sort", "n")
    title, rows, n_ask = shortlist_items(store, cfg, sort)
    if not rows:
        return SL_EMPTY, None, []
    lines = [title]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['line']}")
        if r["stage"]:
            lines.append(f"      {r['stage']}")
        if r["note"]:
            lines.append(f"      <i>{r['note']}</i>")
    kb_rows, row = [], []
    for i, r in enumerate(rows, 1):
        row.append({"text": str(i), "callback_data": f"s:o:{r['oid']}"})
        if len(row) == 5:
            kb_rows.append(row); row = []
    if row:
        kb_rows.append(row)
    if n_ask:
        kb_rows.append([{"text": f"📨 Уточнить у всех, кого ещё не спрашивали ({n_ask})",
                         "callback_data": "s:go"}])
    kb_rows.append([{"text": f"↕️ Сортировка: {SORTS.get(sort, SORTS['n'])[0]}",
                     "callback_data": "s:sort"},
                    {"text": "🔄 Обновить", "callback_data": "s:ref"}])
    items = [get_offer(store, r["oid"]) for r in rows]
    return "\n".join(lines), {"inline_keyboard": kb_rows}, items


def offer_view(store, cfg, o, idx=None):
    """Карточка варианта из шортлиста: этап и действия по нему."""
    rr = _rr()
    ex = o.get("extra") or {}
    v = ex.get("viewing") or {}
    parts = [offer_card(store, cfg, o, idx=idx)[:3000], ""]
    st = stage_of(o)
    parts.append(f"<b>Этап:</b> {st or '👍 в шортлисте'}")
    for n in (ex.get("notes") or [])[-5:]:
        parts.append(f"📝 {rr.escape_html(n.get('text', ''))}")
    oid = o["oid"]
    top = []
    if not o.get("asked_at") and not o.get("replied_at"):
        top.append({"text": "📨 Уточнить", "callback_data": f"o:ask:{oid}"})
    elif o.get("status") == "asked" and not o.get("replied_at") and can_message_broker(o) \
            and not ex.get("reminded"):
        top.append({"text": "🔔 Напомнить", "callback_data": f"o:rem:{oid}"})
    top.append({"text": "📅 Перенести" if v.get("at") else "📅 Просмотр", "callback_data": f"o:view:{oid}"})
    rows = [top, [{"text": "👍 Нравится", "callback_data": f"o:seen:{oid}:g"},
                  {"text": "📝 Заметка", "callback_data": f"o:note:{oid}"},
                  {"text": "👎 Убрать", "callback_data": f"o:seen:{oid}:n"}]]
    return "\n".join(parts), {"inline_keyboard": rows}


def offer_media(cfg, o) -> dict:
    """Для карточки варианта: фото и где он — для карты первым фото.
    Сайт — из объявления (фото свежие: у Joymee ссылки живут 10 минут); маклер — его фото и ориентир."""
    ex = o.get("extra") or {}
    key = ex.get("site_key")
    if key:
        l = _site_listing(key)
        if l:
            return l
    where = ", ".join(x for x in (ex.get("landmark"), ex.get("address")) if x)
    return {"photo_urls": list(o.get("photos") or [])[:6], "district": o.get("district"),
            "text": (f"Ориентир: {where}. " if where else "") + (o.get("text") or "")}


def _site_listing(key):
    rr = _rr()
    if not rr.SALE_DB_PATH.exists():
        return None
    try:
        import sale_sources
        st = rr.Store(rr.SALE_DB_PATH)
        try:
            row = st.conn.execute("SELECT data FROM listings WHERE key=?", (key,)).fetchone()
        finally:
            st.conn.close()
        if not row:
            return None
        l = json.loads(row[0] or "{}")
        sale_sources.refresh_listing(l)
        return l
    except Exception as e:
        rr.log.info("объявление %s для карточки: %s", key, e)
        return None


def offer_photos(cfg, o) -> list:
    """Фото варианта: присланные маклером (file_id) или с сайта (свежие ссылки — у Joymee живут 10 минут)."""
    rr = _rr()
    photos = list(o.get("photos") or [])
    key = (o.get("extra") or {}).get("site_key")
    if key and rr.SALE_DB_PATH.exists():
        try:
            import sale_sources
            st = rr.Store(rr.SALE_DB_PATH)
            try:
                row = st.conn.execute("SELECT data FROM listings WHERE key=?", (key,)).fetchone()
            finally:
                st.conn.close()
            if row:
                l = json.loads(row[0] or "{}")
                sale_sources.refresh_listing(l)
                photos = l.get("photo_urls") or photos
        except Exception as e:
            rr.log.info("фото варианта #%s: %s", o.get("oid"), e)
    return photos[:6]


def offer_caption(store, cfg, o, head=""):
    """Текст карточки варианта под фото (≤1024 символа у Telegram): лишнее из описания обрезаем."""
    rr = _rr()
    oo = dict(o)
    for cut in (None, 200, 80, 0):
        if cut is not None:
            oo["text"] = (o.get("text") or "")[:cut]
            oo["note"] = (o.get("note") or "")[:cut]
        text, kb = offer_view(store, cfg, oo)
        text = (head + "\n\n" if head else "") + text
        if rr.tg_len(text) <= rr.CAPTION_MAX:
            break
    if not o.get("photos") and not (o.get("extra") or {}).get("site_key"):
        text += "\n📷 Фото нет — скажите, и попрошу у маклера"
    return text, kb


def show_offer_view(cfg, store, oid, message_id=None, head=""):
    """Карточка варианта одним сообщением: фото + этап + кнопки. После действия — правим её же."""
    rr = _rr()
    o = get_offer(store, oid)
    if not o:
        return False
    text, kb = offer_caption(store, cfg, o, head)
    kbj = json.dumps(kb, ensure_ascii=False)
    if message_id:
        for method, field in (("editMessageCaption", "caption"), ("editMessageText", "text")):
            if rr.tg_call(cfg, method, {"chat_id": cfg["telegram_chat_id"], "message_id": message_id,
                                        field: text, "parse_mode": "HTML", "reply_markup": kbj}, quiet=True) is not None:
                return True
    rr.send_card(cfg, offer_media(cfg, o), text, ref=f"O:{oid}")
    return True


def _set_extra(store, oid, **kw):
    o = get_offer(store, oid)
    if not o:
        return None
    ex = dict(o.get("extra") or {})
    for k, val in kw.items():
        if val is None:
            ex.pop(k, None)
        else:
            ex[k] = val
    store.conn.execute("UPDATE broker_offers SET extra=? WHERE oid=?",
                       (json.dumps(ex, ensure_ascii=False), oid))
    store.conn.commit()
    return ex


def set_viewing(cfg, store, oid, at, label="", notime=False, text=""):
    """Просмотр назначен (время разобрал воркер). Флаги напоминаний — заново."""
    when = _when(at)
    if not when:
        return False
    v = {"at": when.isoformat(), "label": label, "notime": bool(notime), "text": (text or "")[:200],
         "set_at": datetime.now(TZ).isoformat()}
    if when - datetime.now(TZ) < timedelta(hours=2):   # уже скоро — «за 2 часа» не нужно
        v["pre"] = True
    _set_extra(store, oid, viewing=v)
    o = get_offer(store, oid)
    if o and o["status"] not in ("shortlist", "asked"):
        set_offer_status(store, oid, "shortlist")
    return True


def add_note(store, oid, text):
    o = get_offer(store, oid)
    if not o or not (text or "").strip():
        return False
    notes = list((o.get("extra") or {}).get("notes") or [])
    notes.append({"at": datetime.now(TZ).isoformat(), "text": text.strip()[:500]})
    _set_extra(store, oid, notes=notes[-20:])
    return True


def _offer_tag(o):
    tag = []
    if o.get("rooms"):
        tag.append(f"{o['rooms']}-комн")
    if o.get("district"):
        tag.append(o["district"])
    if o.get("price_usd"):
        tag.append(fmt_money(o["price_usd"]))
    return ", ".join(tag)


def remind_text(o, cfg=None):
    a = (cfg or {}).get("assistant_name", "Ra'no")
    tag = _offer_tag(o)
    return (f"Здравствуйте! Это снова {a} 🙂 Напоминаю про вариант" + (f" ({tag})" if tag else "")
            + " — он ещё актуален? Если уже нет, просто напишите «нет», "
              "больше не побеспокою.")


def remind_broker(cfg, store, oid):
    """Одно вежливое напоминание маклеру, который молчит по уточнению."""
    rr = _rr()
    o = get_offer(store, oid)
    if not o:
        return "Вариант не найден"
    if (o.get("extra") or {}).get("reminded"):
        return "Уже напоминала — второй раз не буду надоедать"
    if not can_message_broker(o):
        rr.send_telegram(cfg, f"✍️ Маклер по варианту #{oid} не в боте — напомните сами, текст готов:\n\n"
                              f"<code>{rr.escape_html(remind_text(o, cfg))}</code>")
        return "Текст напоминания — в чате"
    ok = broker_send(cfg, store, o["broker_chat"], remind_text(o, cfg))
    if ok is None:
        return "Не получилось отправить"
    now = datetime.now(timezone.utc).isoformat()
    store.conn.execute("UPDATE broker_offers SET asked_at=? WHERE oid=?", (now, oid))
    store.conn.commit()
    _set_extra(store, oid, reminded=now)
    return "🔔 Тихонько напомнила маклеру"


def handle_offer_cb(data, cfg, store, message_id=None):
    """Действия с карточкой варианта (o:…): уточнить, просмотр, посмотрел, заметка, убрать."""
    rr = _rr()
    parts = data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    try:
        oid = int(parts[2])
    except (IndexError, ValueError):
        return "", True
    arg = parts[3] if len(parts) > 3 else ""
    o = get_offer(store, oid)
    if not o:
        return "Вариант не найден", True
    toast = ""
    if act == "like":                         # из чата: «в шортлист», «нравится»
        if o["status"] in ("new", "later", "rejected"):
            set_offer_status(store, oid, "shortlist")
            return f"👍 В шортлисте: {len(offers_by_status(store, 'shortlist'))}", True
        act, arg = "seen", "g"
    if act == "skip":                         # из чата: «мимо», «убрать» (+ причина)
        if o["status"] in ("new", "later"):
            return handle_triage_cb(f"t:r:{oid}:{arg or 'x'}", cfg, store)
        act, arg = "seen", "n"
    if act == "ask":
        toast = request_details(cfg, store, [oid])
    elif act == "rem":
        toast = remind_broker(cfg, store, oid)
    elif act == "quiet":
        _set_extra(store, oid, no_remind=True)
        toast = "Хорошо, не дёргаю 🙂"
    elif act == "vclr":
        _set_extra(store, oid, viewing=None)
        toast = "Просмотр отменила"
    elif act == "seen" and arg in ("g", "m", "n"):
        _set_extra(store, oid, seen=arg, seen_at=datetime.now(TZ).isoformat())
        if arg == "n":
            set_offer_status(store, oid, "rejected")
            if can_message_broker(o):
                broker_send(cfg, store, o["broker_chat"], decline_text(cfg))
            done = {"chat_id": cfg["telegram_chat_id"], "parse_mode": "HTML",
                    "text": f"👎 Вариант #{oid} убран из шортлиста"
                            + (" — маклеру ушёл вежливый отказ." if can_message_broker(o) else "."),
                    "reply_markup": json.dumps({"inline_keyboard": [[
                        {"text": "📋 Шортлист", "callback_data": "s:show"}]]}, ensure_ascii=False)}
            if not message_id or (
                    rr.tg_call(cfg, "editMessageCaption", {"chat_id": done["chat_id"], "message_id": message_id,
                                                          "caption": done["text"], "parse_mode": "HTML",
                                                          "reply_markup": done["reply_markup"]}, quiet=True) is None
                    and rr.tg_call(cfg, "editMessageText", {**done, "message_id": message_id}, quiet=True) is None):
                rr.tg_call(cfg, "sendMessage", done)
            return "👎 Убрала — не наше", True
        toast = "👍 Записала: нравится!" if arg == "g" else "🤔 Записала: думаете"
    elif act in ("view", "note"):             # ввод текста ведёт воркер; сюда — только если его нет
        rr.send_telegram(cfg, "Напишите день и время просмотра, например «завтра 18:00»."
                         if act == "view" else "Напишите заметку одним сообщением.")
        return "", True
    else:
        return "", True
    if message_id:                            # нажали кнопку на старой карточке — обновим её; из чата — не дублируем
        show_offer_view(cfg, store, oid, message_id)
    return toast, True


SL_PAGE = 10
SL_HINT = ("Что уточнить, когда смотреть, что понравилось — пишите своими словами, "
           "про конкретную квартиру — ответом на её сообщение")


def show_shortlist(cfg, store, message_id=None, offset=0):
    """Шортлист — карточками: у каждого варианта свои фото, этап и кнопки. Без сводного списка.
    Спросить всех разом — одной кнопкой на первой карточке; больше 5 — «Ещё» на последней."""
    rr = _rr()
    items = [o for o in offers_by_status(store, "shortlist") + offers_by_status(store, "asked") if o]
    items.sort(key=SORTS.get(store.get_kv("sl_sort", "n"), SORTS["n"])[1])
    if not items:
        rr.tg_call(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": SL_EMPTY, "parse_mode": "HTML"})
        return 0
    page = items[offset:offset + SL_PAGE]
    n_ask = sum(1 for o in items if askable(o))
    for i, o in enumerate(page, offset + 1):
        head = f"📋 <b>Шортлист</b> · {i} из {len(items)}"
        if i == offset + 1:                       # кнопок нет — одна подсказка на первой карточке
            head += "\n<i>" + SL_HINT + (f". Могу спросить маклеров сразу по всем ({n_ask})" if n_ask > 1 else "") + "</i>"
        text, _ = offer_caption(store, cfg, o, head=head)
        rr.send_card(cfg, offer_media(cfg, o), text, ref=f"O:{o['oid']}")
        time.sleep(1)
    return len(page)


def biz_followup_text(o, deal="rent", uz=False):
    """Что спросить у маклера сразу после его варианта: только недостающее. О просмотре и торге — нет:
    это решает клиент."""
    ex = o.get("extra") or {}
    items = []
    if not o.get("photos"):
        items.append("Yuborolasizmi rasmlarini (xonalar, oshxona, sanuzel)?" if uz else
                     "Пришлите, пожалуйста, фото (комнаты, кухня, санузел).")
    if not (ex.get("address") or ex.get("landmark")):
        items.append("Aniq manzil yoki mo'ljal?" if uz else "Точный адрес или ориентир?")
    if not o.get("floor"):
        items.append("Qavati va necha qavatli?" if uz else "Какой этаж и этажность?")
    if not o.get("area"):
        items.append("Maydoni qancha?" if uz else "Какая площадь?")
    if not o.get("price_usd"):
        items.append(("Narxi qancha, kelishsa bo'ladimi?" if deal == "buy" else "Oyiga narxi qancha?") if uz else
                     ("Какая цена и есть ли торг?" if deal == "buy" else "Какая цена в месяц?"))
    if deal == "buy" and not ex.get("documents"):
        items.append("Hujjatlari joyidami (kadastr), ipotekaga beriladimi?" if uz else
                     "Документы в порядке (кадастр)? Можно в ипотеку?")
    if not ex.get("commission"):
        items.append("Vositachilik haqi qancha?" if uz else "Какая комиссия?")
    if not items:
        return ""
    head = "Rahmat! Bir nechta savol:" if uz else "Спасибо! Уточните, пожалуйста:"
    return "\n".join([head] + [f"{i}. {t}" for i, t in enumerate(items, 1)])


def manual_auto_ask(cfg, store, oid) -> bool:
    """Вариант пришёл пересылкой (маклеру писать не можем): один раз — готовый текст уточнения клиенту,
    отправит маклеру сам."""
    rr = _rr()
    o = get_offer(store, oid)
    if not o or can_message_broker(o) or is_site_offer(o) or (o.get("extra") or {}).get("auto_asked"):
        return False
    ans = get_anketa(store).get("ans") or {}
    deal = "buy" if ans.get("deal") in ("buy", "sale") else "rent"
    q = biz_followup_text(o, deal, ans.get("lang") == "uz")
    if not q:
        return False
    tag = _offer_tag(o)
    q = q.replace("Спасибо! Уточните, пожалуйста:", "Спасибо за вариант" + (f" ({tag})" if tag else "")
                  + "! Уточните, пожалуйста:").replace("Rahmat! Bir nechta savol:", "Variant uchun rahmat! Bir nechta savol:")
    _set_extra(store, oid, auto_asked=datetime.now(timezone.utc).isoformat())
    return rr.send_telegram(cfg, f"✍️ По варианту #{oid} не хватает деталей — вот текст для маклера, "
                                 "скопируйте и отправьте ему. Ответ перешлите мне — допишу в карточку.\n\n"
                                 f"<code>{rr.escape_html(q)}</code>")


def biz_auto_ask(cfg, store, oid) -> bool:
    """Вариант пришёл в чат клиента — один раз дописываем маклеру, чего не хватает (окно 24 ч открыто:
    маклер только что написал)."""
    o = get_offer(store, oid)
    if not o or not str(o.get("broker_chat") or "").startswith("biz:") or not biz_ready(store):
        return False
    if (o.get("extra") or {}).get("auto_asked"):
        return False
    ans = get_anketa(store).get("ans") or {}
    deal = "buy" if ans.get("deal") in ("buy", "sale") else "rent"
    text = biz_followup_text(o, deal, ans.get("lang") == "uz")
    if not text:
        return False
    if broker_send(cfg, store, o["broker_chat"], text) is None:
        return False
    _set_extra(store, oid, auto_asked=datetime.now(timezone.utc).isoformat())
    asked = store.get_kv("biz_asked") or {}
    asked[str(o["broker_chat"])] = {"oid": oid, "at": time.time()}
    store.set_kv("biz_asked", asked)
    return True


def details_question(o, cfg=None, deal="rent"):
    cfg = cfg or {}
    a = cfg.get("assistant_name", "Ra'no")
    q = [f"Здравствуйте! Это {a} 👋, ИИ-ассистент по поиску жилья.",
         "По вашему объявлению о продаже" if is_site_offer(o) else "По варианту, который вы присылали"]
    tag = []
    if o["rooms"]:
        tag.append(f"{o['rooms']}-комн")
    if o["district"]:
        tag.append(o["district"])
    if o["price_usd"]:
        tag.append(fmt_money(o["price_usd"]))
    if tag:
        q[1] += f" ({', '.join(tag)})"
    q[1] += ":"
    items = ["Он ещё актуален?", "Точный адрес и ориентир?"]
    if not o.get("photos") and not (o.get("extra") or {}).get("site_key"):
        items.append("Пришлите, пожалуйста, фото квартиры (комнаты, кухня, санузел).")
    if not o["floor"]:
        items.append("Какой этаж и этажность?")
    if not o["area"]:
        items.append("Какая площадь?")
    if deal == "buy":
        if not o["price_usd"]:
            items.append("Какая цена и есть ли торг?")
        items += ["Документы в порядке (кадастр, собственник)? Возможна ипотека?",
                  "Размер комиссии?", "Когда можно посмотреть?"]
    else:
        if not o["price_usd"]:
            items.append("Какая цена в месяц?")
        items += ["Размер депозита и комиссии?", "Когда можно посмотреть?"]
    q += [f"{i}. {t}" for i, t in enumerate(items, 1)]
    return "\n".join(q)


def request_details(cfg, store, oids=None):
    """Вопросы маклерам по вариантам. Без списка — всем, кого ещё не спрашивали."""
    rr = _rr()
    if oids is None:
        oids = [o["oid"] for o in offers_by_status(store, "shortlist") if o and askable(o)]
    if not oids:
        return "Всех уже спросила 🙂"
    deal = (get_anketa(store).get("ans") or {}).get("deal", "rent")
    sent = 0
    manual = []
    for oid in oids:
        o = get_offer(store, oid)
        if not o:
            continue
        if not can_message_broker(o):        # пересланный из WhatsApp — уточняете сами
            manual.append(o)
            store.conn.execute("UPDATE broker_offers SET asked_at=? WHERE oid=?",
                               (datetime.now(timezone.utc).isoformat(), oid))
            continue
        ok = broker_send(cfg, store, o["broker_chat"], details_question(o, cfg, deal))
        if ok is not None:
            store.conn.execute(
                "UPDATE broker_offers SET status='asked', asked_at=? WHERE oid=?",
                (datetime.now(timezone.utc).isoformat(), oid))
            sent += 1
    store.conn.commit()
    if sent:
        rr.send_telegram(cfg, f"📨 Спросила маклеров по {sent} {plural(sent, 'варианту', 'вариантам', 'вариантам')}.\n"
                              "Как ответят — прикреплю к карточкам. "
                              "А если кто-то промолчит сутки — подскажу, напомнить ли.")
    for o in manual:                         # готовый текст, чтобы отправить самому
        ex = o.get("extra") or {}
        if is_site_offer(o):
            who = ", ".join(rr.fmt_phone(p) for p in (ex.get("phones") or [])[:2])
            head = (f"✍️ Вариант #{o['oid']} — объявление с {rr.escape_html(o['broker_name'] or 'сайта')}. "
                    + (f"Продавец: 📞 {who}. " if who else "Контакт — в объявлении. ")
                    + "Позвоните или напишите сами, текст готов:")
        else:
            head = f"✍️ Вариант #{o['oid']} пришёл не через бота — уточните сами, текст готов:"
        rr.send_telegram(cfg, f"{head}\n\n<code>{rr.escape_html(details_question(o, cfg, deal))}</code>")
    return f"Отправлено: {sent}" + (f", вручную: {len(manual)}" if manual else "")


def handle_shortlist_cb(data, cfg, store, message_id=None):
    parts = data.split(":", 2)
    act = parts[1] if len(parts) > 1 else ""
    if act in ("o", "t"):                     # номер — карточка варианта («t» — старые кнопки)
        try:
            oid = int(parts[2])
        except (IndexError, ValueError):
            return "", True
        if not show_offer_view(cfg, store, oid):        # новой карточкой с фото, а не поверх списка
            return "Вариант не найден", True
        return "", True
    if act in ("clr", "sort", "ref", "show"):  # шортлист — карточками, новыми сообщениями
        show_shortlist(cfg, store)
        return "", True
    if act == "more":
        try:
            show_shortlist(cfg, store, offset=int(parts[2]))
        except (IndexError, ValueError):
            show_shortlist(cfg, store)
        return "", True
    if act == "go":
        return request_details(cfg, store), True
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


# те же кнопки, что ставит воркер (worker/src/index.js · OWNER_KB)
OWNER_KB = {"remove_keyboard": True}           # кнопок внизу нет — всё словами


def send_app_button(cfg, store, text=None):
    """Раньше — кнопка мини-аппа. Теперь параметры собираются в чате:
    просим описать поиск словами и убираем старую клавиатуру с кнопкой."""
    rr = _rr()
    return rr.tg_call(cfg, "sendMessage", {
        "chat_id": cfg["telegram_chat_id"],
        "text": text or ("💬 Опишите своими словами, что ищете — например: «снять трёшку "
                         "в Мирабаде до $1400, с ремонтом, заезд в ноябре». "
                         "Остальное я уточню сама."),
        "parse_mode": "HTML",
        "reply_markup": json.dumps(OWNER_KB, ensure_ascii=False)})


ALLOWED = {f["k"] for f in STEPS} | {
    "budget_max", "floor_min", "floor_max", "city_other", "lang",
    "date_from", "date_to", "movein_date", "note", "districts_any", "near", "near_km"}


# ---- компактный код параметров для deep link /start p<код> ---------------
# Из мини-аппа, открытого кнопкой меню («Параметры»), Telegram НЕ даёт sendData().
# Поэтому мини-апп упаковывает ответы в ≤64 символов [A-Za-z0-9_-] и открывает
# t.me/<бот>?start=p<код>; клиент сам шлёт боту «/start p<код>».
# Порядок и словари ДОЛЖНЫ совпадать с encodeStart() в docs/index.html.
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_SC_VER = "1"
_SC_EPOCH = _dt.date(2024, 1, 1)
_SC = [  # (ключ, вид, словарь|ширина)
    ("lang", "e", ["ru", "uz", "en"]),
    ("deal", "e", ["rent", "daily", "buy"]),
    ("object", "e", ["flat", "house", "dacha", "land"]),
    ("city", "e", ["tashkent", "charvak", "region", "other"]),
    ("class", "e", ["any", "new", "premium", "reno", "biz"]),
    ("furniture", "e", ["yes", "no", "any"]),
    ("term", "e", ["12", "6_12", "3_6", "flex", "d1_3", "d4_7", "d7_30", "dflex"]),
    ("movein", "e", ["now", "month", "flex", "date"]),
    ("who", "e", ["single", "couple", "family_kids", "family", "big", "group"]),
    ("pets", "e", ["no", "cat", "dog", "pet_other"]),
    ("parking", "e", ["yes", "any"]),
    ("contact", "e", ["bot", "me", "both"]),
    ("districts", "m", [str(i) for i in range(12)] + ["any"]),
    ("rooms", "m", ["1", "2", "3", "4", "any"]),
    ("floor_pref", "m", ["nf", "nl", "mid", "any"]),
    ("budget", "n", 4), ("budget_max", "n", 4),
    ("floor_min", "n", 1), ("floor_max", "n", 1),
    ("movein_date", "d", 2), ("date_from", "d", 2), ("date_to", "d", 2),
]


def _sc_num(chunk):
    n = 0
    for ch in chunk:
        i = _B64.find(ch)
        if i < 0:
            raise ValueError(ch)
        n = n * 64 + i
    return n


def decode_start_code(code):
    """'1....' → dict ответов (как ans из мини-аппа) или None при мусоре."""
    if not code or code[0] != _SC_VER:
        return None
    pos, ans = 1, {}
    try:
        for k, kind, spec in _SC:
            if kind == "e":
                i = _sc_num(code[pos]); pos += 1
                if 1 <= i <= len(spec):
                    ans[k] = spec[i - 1]
            elif kind == "m":
                w = (len(spec) + 5) // 6
                bits = _sc_num(code[pos:pos + w]); pos += w
                ans[k] = [v for j, v in enumerate(spec) if bits >> j & 1]
            elif kind == "n":
                v = _sc_num(code[pos:pos + spec]); pos += spec
                if v:
                    ans[k] = str(v)
            else:  # дата: дни от эпохи, 0 = нет
                v = _sc_num(code[pos:pos + spec]); pos += spec
                if v:
                    ans[k] = (_SC_EPOCH + _dt.timedelta(days=v)).isoformat()
        tail = code[pos:]
        if tail:
            ans["city_other"] = base64.urlsafe_b64decode(
                tail + "=" * (-len(tail) % 4)).decode("utf-8", "ignore").strip("\x00 ")
    except (ValueError, IndexError, TypeError):
        return None
    return ans


def apply_webapp_data(cfg, store, raw):
    """Принимает JSON из мини-аппа и превращает в ответы анкеты."""
    rr = _rr()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        rr.send_telegram(cfg, "Ой, не смогла прочитать данные из приложения 🙈")
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
    if "note" in ans:
        ans["note"] = " ".join(str(ans["note"]).split())[:150]
    if ans.get("near"):
        ans["near"] = " ".join(str(ans["near"]).split())[:60]
    if ans.get("near_km"):
        try:
            ans["near_km"] = str(min(max(float(str(ans["near_km"]).replace(",", ".")), 0.3), 10.0))
        except ValueError:
            ans.pop("near_km")
    for k in ("date_from", "date_to", "movein_date"):   # только валидные yyyy-mm-dd
        if k in ans and not _DATE_RE.match(str(ans.get(k) or "")):
            ans.pop(k, None)
    fp = ans.get("floor_pref")
    if isinstance(fp, str):
        fp = [fp]
    if isinstance(fp, list):
        ans["floor_pref"] = [x for x in fp if x in ("nf", "nl", "mid", "any")]

    a = get_anketa(store)
    # replace: из чата-интервью приходит полный набор — старые ответы не смешиваем
    a["ans"] = ans if data.get("replace") else {**a.get("ans", {}), **ans}
    if data.get("replace"):
        store.set_kv("awaiting_text", False)   # «Изменить текст» отменён новым интервью
    a["i"] = len(STEPS)
    save_anketa(store, a)
    store.set_kv("fresh_start", False)          # после сброса поиск по сайтам ждал этих параметров
    store.set_kv("sale_force", True)            # новые параметры — сразу пробежаться по сайтам
    finish_anketa(cfg, store)
    return True

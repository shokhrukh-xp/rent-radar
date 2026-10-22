"""Доведение поиска до сделки: напоминания и сводки.

- маклер молчит сутки после уточнения → спросить владельца, напомнить ли (одно напоминание);
- просмотр: утром — «сегодня просмотр», за 2 часа — напоминание, после — «как прошёл?»;
- вечером (20:00 по Ташкенту) — итоги дня одним сообщением, если было что-то.

Всё здесь только для владельца, ничего не уходит маклерам без его нажатия."""
import json
from datetime import datetime, timedelta, timezone

import concierge as cg
import i18n
from i18n import t

TZ = cg.TZ
EVERY = 300                       # проверка раз в 5 минут
MAX_PROMPTS = 3                   # за проход — не больше трёх «напомнить?»


def _rr():
    import rent_radar
    return rent_radar


def _offers_with(store, where, args=()):
    rows = store.conn.execute(f"SELECT oid FROM broker_offers WHERE {where}", args).fetchall()
    return [o for o in (cg.get_offer(store, r[0]) for r in rows) if o]


def _day_start_utc(now):
    loc = now.astimezone(TZ)
    return loc.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()


def _send(cfg, text, rows=None, ref=None):
    """Клиенту — без кнопок (rows не используются). ref — о каком варианте сообщение: тогда ответ на него
    словами («понравилась», «перенеси») воркер поймёт как про этот вариант."""
    rr = _rr()
    p = {"chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML"}
    r = rr.tg_call(cfg, "sendMessage", p)
    if r is not None and ref:
        mid = ((r or {}).get("result") or {}).get("message_id")
        if mid:
            rr.remember_card(cfg, [mid], ref, text)
    return r is not None


def _short(o):
    return cg.offer_tag_client(o) or t("вариант #{n}", n=o['oid'])


# ------------------------------------------------------- молчащие маклеры --

def silent_brokers(cfg, store, now):
    """Спросили детали > 24 ч назад, ответа нет → предложить напомнить (один раз)."""
    loc = now.astimezone(TZ)
    if not 9 <= loc.hour < 21:                   # ночью не дёргаем
        return 0
    since = (now - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    n = 0
    for o in _offers_with(store, "status='asked' AND replied_at IS NULL AND asked_at<=?", (since,)):
        ex = o.get("extra") or {}
        if ex.get("remind_prompt") or ex.get("no_remind") or ex.get("reminded"):
            continue
        if n >= MAX_PROMPTS:
            break
        who = _rr().escape_html(o.get("broker_name") or t("Маклер"))
        head = t("⏰ <b>{who}</b> молчит сутки по варианту #{n} ({tag}).", who=who, n=o['oid'], tag=_short(o))
        res = cg.remind_broker(cfg, store, o["oid"])          # одно вежливое напоминание — сама, без кнопок
        if "напомнила" in res:
            text = head + t(" Тихонько напомнила — один раз, надоедать не буду 🙂")
        elif "в чате" in res:
            text = ""                                          # текст напоминания уже прислала — отправить самим
        else:
            text = head + t(" Написать ему ещё раз Telegram мне не даёт — если нужно, напомните сами.")
        if not text or _send(cfg, text, ref=f"O:{o['oid']}"):
            cg._set_extra(store, o["oid"], remind_prompt=now.isoformat())
            n += 1
    return n


# ---------------------------------------------------------------- просмотры --

def viewings(store):
    out = []
    for o in _offers_with(store, "status IN ('shortlist','asked') AND extra LIKE '%\"viewing\"%'"):
        v = (o.get("extra") or {}).get("viewing") or {}
        at = cg._when(v.get("at"))
        if at:
            out.append((at, v, o))
    out.sort(key=lambda x: x[0])
    return out


def _viewing_line(at, v, o):
    rr = _rr()
    ex = o.get("extra") or {}
    where = ", ".join(x for x in (ex.get("address"), ex.get("landmark")) if x)
    hm = t("время уточнить") if v.get("notime") else f"{at:%H:%M}"
    return (f"• <b>{hm}</b> — #{o['oid']} {_short(o)}"
            + (f"\n   🗺 {rr.escape_html(where)}" if where else "")
            + (f"\n   👤 {rr.escape_html(o.get('broker_name') or '')}" if o.get("broker_name") else ""))


def viewing_reminders(cfg, store, now):
    """За 2 часа до просмотра — напоминание; через час после — «как прошёл?»."""
    loc = now.astimezone(TZ)
    n = 0
    for at, v, o in viewings(store):
        oid = o["oid"]
        if (o.get("extra") or {}).get("seen"):
            continue
        if not v.get("notime") and not v.get("pre") and at - timedelta(hours=2) <= now < at:
            mins = max(5, int((at - now).total_seconds() // 60))
            if _send(cfg, t("⏰ Через {h} ч {m} мин просмотр — не опаздываем! 😉", h=mins // 60, m=f"{mins % 60:02d}")
                     + "\n\n" + _viewing_line(at, v, o),
                     ref=f"O:{oid}"):
                cg._set_extra(store, oid, viewing={**v, "pre": True})
                n += 1
            continue
        after = at + (timedelta(hours=8) if v.get("notime") else timedelta(hours=1))
        if not v.get("after") and now >= after and 9 <= loc.hour < 22:
            if _send(cfg, t("🏠 Ну что, как вам вариант #{n} ({tag})? Ответьте на это сообщение своими словами: "
                            "понравилась, думаете или не то. Не состоялся — напишите, на когда перенести.",
                            n=oid, tag=_short(o)),
                     ref=f"O:{oid}"):
                cg._set_extra(store, oid, viewing={**v, "after": True})
                n += 1
    return n


def morning_note(cfg, store, now):
    loc = now.astimezone(TZ)
    today = loc.date().isoformat()
    if not (8 * 60 + 30 <= loc.hour * 60 + loc.minute < 12 * 60) or store.get_kv("fu_morning") == today:
        return False
    store.set_kv("fu_morning", today)
    todays = [(at, v, o) for at, v, o in viewings(store)
              if at.astimezone(TZ).date() == loc.date() and at > now - timedelta(hours=1)]
    if not todays:
        return False
    return _send(cfg, t("☀️ <b>Доброе утро! Сегодня идём смотреть</b>") + "\n\n"
                 + "\n".join(_viewing_line(*x) for x in todays)
                 + "\n\n" + t("За 2 часа напомню ещё раз — я пунктуальная 😉"))


# ------------------------------------------------------------ вечерняя сводка --

def digest_data(store, sale_store, now):
    day = _day_start_utc(now)
    q = lambda sql, *a: store.conn.execute(sql, a).fetchone()[0]
    silent_since = (now - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    loc = now.astimezone(TZ)
    tomorrow = (loc + timedelta(days=1)).date()
    d = {
        "contacted_today": q("SELECT COUNT(*) FROM brokers WHERE status IN ('contacted', 'offered') AND last_contact>=?", day),
        "contacted_total": q("SELECT COUNT(*) FROM brokers WHERE status IN ('contacted', 'offered')"),
        "offers_today": q("SELECT COUNT(*) FROM broker_offers WHERE created_at>=? "
                          "AND status NOT IN ('message') AND broker_chat NOT LIKE 'site:%'", day),
        "pending": q("SELECT COUNT(*) FROM broker_offers WHERE status IN ('new','later')"),
        "answers_today": q("SELECT COUNT(*) FROM broker_offers WHERE replied_at>=?", day),
        "silent": q("SELECT COUNT(*) FROM broker_offers WHERE status='asked' AND replied_at IS NULL "
                    "AND asked_at<=?", silent_since),
        "shortlist": q("SELECT COUNT(*) FROM broker_offers WHERE status IN ('shortlist','asked')"),
        "listings_today": 0,
        "tomorrow": [x for x in viewings(store) if x[0].astimezone(TZ).date() == tomorrow],
    }
    d["site_seen"] = d["site_fit"] = 0
    if sale_store is not None:
        try:
            import sale_sources
            st = sale_sources.day_stats(sale_store)
            d["listings_today"] = st.get("instant", 0) + st.get("picked", 0)
            d["site_seen"], d["site_fit"] = st.get("seen", 0), st.get("fit", 0)
        except Exception:
            pass
    return d


MON_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def day_month(loc) -> str:
    """«15 октября» / «15-oktabr» / «15 October» — на языке клиента."""
    if i18n.LANG == "uz":
        return f"{loc.day}-{cg.MON_UZ[loc.month - 1]}"
    if i18n.LANG == "en":
        return f"{loc.day} {MON_EN[loc.month - 1]}"
    return f"{loc.day} {cg.MON_GEN_RU[loc.month - 1]}"


def digest_text(d, now):
    loc = now.astimezone(TZ)
    activity = (d["contacted_today"] + d["offers_today"] + d["answers_today"] + d["listings_today"]
                + len(d["tomorrow"]) + d["pending"] + d["silent"])
    if not activity:
        return None, None
    lines = [t("🌙 <b>Итоги дня</b> — {date}. Вот что успели:", date=day_month(loc)), ""]
    if d["contacted_today"] or d["contacted_total"]:
        lines.append(t("📇 Маклерам написали: {a} сегодня, всего {b}", a=d['contacted_today'], b=d['contacted_total']))
    if d["offers_today"]:
        lines.append(t("🏠 Новых вариантов от маклеров: {n}", n=d['offers_today']))
    if d["pending"]:
        lines.append(t("👀 Ждут вашего решения: {n}", n=d['pending']))
    if d["answers_today"]:
        lines.append(t("💬 Маклеры ответили на уточнения: {n}", n=d['answers_today']))
    if d["silent"]:
        lines.append(t("⏳ Молчат больше суток: {n}", n=d['silent']))
    if d["shortlist"]:
        lines.append(t("📋 В шортлисте: {n}", n=d['shortlist']))
    if d.get("site_seen"):
        lines.append(t("🔎 Сайты и каналы: новых объявлений {a}, подошло {b}, прислала {c}",
                       a=d['site_seen'], b=d.get('site_fit', 0), c=d['listings_today']))
    elif d["listings_today"]:
        lines.append(t("🔎 С сайтов прислала: {n}", n=d['listings_today']))
    if d["tomorrow"]:
        lines += ["", t("📅 <b>Завтра просмотры</b>")] + [_viewing_line(*x) for x in d["tomorrow"]]
    tip = ""                                   # один следующий шаг — самый полезный
    if d["pending"]:
        tip = t("Разберите {n} {w} — ответьте на карточки своими словами: «нравится», «потом» или «не то», "
                "а дальше я сама.", n=d['pending'], w=cg.plural(d['pending'], 'вариант', 'варианта', 'вариантов'))
    elif not d["contacted_total"]:
        tip = t("Давайте начнём с маклеров — скажите «напиши маклерам», пришлю 5 со вписанным запросом 🙂")
    elif not d["contacted_today"] and d["contacted_total"] < 60:
        tip = t("Завтра можно написать ещё маклерам — скажите «напиши маклерам» (до 15 в день, и номер в безопасности).")
    if tip:
        lines += ["", "👉 " + tip]
    rows = []                                  # кнопок клиенту нет — следующий шаг словами (tip)
    return "\n".join(lines), rows


def evening_digest(cfg, store, sale_store, now):
    loc = now.astimezone(TZ)
    today = loc.date().isoformat()
    if not (20 * 60 <= loc.hour * 60 + loc.minute < 23 * 60 + 30) or store.get_kv("fu_evening") == today:
        return False
    store.set_kv("fu_evening", today)
    if cfg.get("digest") is False:
        return False
    text, rows = digest_text(digest_data(store, sale_store, now), now)
    return bool(text) and _send(cfg, text, rows)


def broker_offer(cfg, store, sale_store, now):
    """Сначала ищет Ra'no сама. Маклеров предлагает, когда на сайтах пусто:
    покупка — первый проход ничего не дал или за 2 дня меньше 3 подходящих; аренда — сутки без вариантов."""
    st = store.get_kv("search_start") or {}
    if not st.get("at") or st.get("offered"):
        return False
    start = datetime.fromisoformat(st["at"])
    if store.conn.execute("SELECT COUNT(*) FROM brokers WHERE status IN ('contacted', 'offered') AND last_contact>=?",
                          (st["at"],)).fetchone()[0]:
        return False                           # уже сам пишет маклерам — не навязываем
    age_h = (now - start).total_seconds() / 3600
    why = ""
    if st.get("deal") == "buy" and sale_store is not None:
        fp = sale_store.get_kv("first_pass") or {}
        shown = sale_store.conn.execute("SELECT COUNT(*) FROM listings WHERE notified=1 AND first_seen>=?",
                                        (st["at"],)).fetchone()[0]
        if fp.get("at", "") >= st["at"] and not fp.get("fit") and not (sale_store.get_kv("sale_pick") or []):
            why = t("🙈 Пробежалась по всем сайтам и каналам — под ваши условия сейчас ничего нет.")
        elif age_h >= 48 and shown < 3:
            why = t("🙈 За 2 дня на сайтах нашлось совсем мало подходящего ({n}).", n=shown)
    elif st.get("deal") == "rent" and age_h >= 24:
        sent = store.conn.execute("SELECT COUNT(*) FROM listings WHERE notified=1 AND first_seen>=?",
                                  (st["at"],)).fetchone()[0]
        if not sent:
            why = t("🙈 За сутки на сайтах не нашлось подходящей аренды.")
    if not why or not 9 <= now.astimezone(TZ).hour < 22:
        return False
    cg.offer_brokers(cfg, store, why)
    return True


def run(cfg, store, sale_store=None, now=None, force=False):
    """Вызывается из главного цикла; сам решает, пора ли."""
    t = datetime.now(timezone.utc).timestamp()
    if not force and t - (store.get_kv("fu_at") or 0) < EVERY:
        return {}
    store.set_kv("fu_at", t)
    now = now or datetime.now(TZ)
    out = {}
    for name, fn in (("silent", lambda: silent_brokers(cfg, store, now)),
                     ("viewing", lambda: viewing_reminders(cfg, store, now)),
                     ("morning", lambda: morning_note(cfg, store, now)),
                     ("digest", lambda: evening_digest(cfg, store, sale_store, now)),
                     ("brokers", lambda: broker_offer(cfg, store, sale_store, now)),
                     ("shortlist", lambda: _rr().check_shortlist(cfg, store))):
        try:
            out[name] = fn()
        except Exception as e:                  # напоминания не должны ронять радар
            _rr().log.warning("followup %s: %s", name, e)
    return out

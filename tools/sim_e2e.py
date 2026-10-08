"""Сквозная симуляция Ra'no: реальные сайты и копия живых баз, Telegram — проверяющая заглушка.

Каждое исходящее сообщение проверяется так, как его проверил бы Telegram:
HTML-разметка (только разрешённые теги, закрыты, нет голых < > &), длина текста и подписи,
callback_data ≤ 64 байт, структура клавиатур. Плюс голос Ra'no: мужской род о себе.
Запуск: python3 sim_e2e.py <папка с кодом> <radar.db> <sale.db>"""
import html, json, os, re, shutil, sys, time, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

CODE, RADAR, SALE = sys.argv[1], sys.argv[2], sys.argv[3]
WORK = Path(__file__).parent / "run"
shutil.rmtree(WORK, ignore_errors=True); WORK.mkdir()
shutil.copy(RADAR, WORK / "radar.db"); shutil.copy(SALE, WORK / "sale.db")
os.environ.update(RADAR_BOT_TOKEN="TEST", RADAR_CHAT_ID="100", RADAR_STATE_DIR=str(WORK))
os.environ.pop("RADAR_WORKER_URL", None)
sys.path.insert(0, CODE)
import rent_radar as rr, concierge as cg, followup as fu, sale_sources as SS  # noqa: E402

OWNER = 100
ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre",
           "tg-spoiler", "span", "blockquote", "tg-emoji"}
TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^<>]*)>")
MASC = re.compile(r"(?:^|[^а-яё])(я\s+)?(понял|нашёл|нашел|записал|сделал|собрал|уточнил|поменял|добавил|"
                  r"сохранил|спросил|напомнил|отправил|убрал|отменил|передал|получил|проверил|смог|рад|готов)"
                  r"(?![а-яё])", re.I)
MSGS, ERRS = [], []
mid = [1000]


def check_html(text, where):
    stack = []
    pos = 0
    for m in TAG.finditer(text):
        chunk = text[pos:m.start()]
        if "<" in chunk or ">" in chunk:
            ERRS.append(f"{where}: голый < или > в тексте: …{chunk[-40:]!r}")
        if re.search(r"&(?!(amp|lt|gt|quot|#\d+|#x[0-9a-f]+);)", chunk, re.I):
            ERRS.append(f"{where}: голый & в тексте: …{chunk[-40:]!r}")
        close, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name not in ALLOWED:
            ERRS.append(f"{where}: тег <{name}> Telegram не поддерживает")
        if close:
            if not stack or stack[-1] != name:
                ERRS.append(f"{where}: закрыт </{name}> без пары (стек {stack})")
            else:
                stack.pop()
        else:
            if name == "a" and "href=" not in attrs:
                ERRS.append(f"{where}: <a> без href")
            stack.append(name)
        pos = m.end()
    tail = text[pos:]
    if "<" in tail or ">" in tail:
        ERRS.append(f"{where}: голый < или > в конце: …{tail[:40]!r}")
    if re.search(r"&(?!(amp|lt|gt|quot|#\d+|#x[0-9a-f]+);)", tail, re.I):
        ERRS.append(f"{where}: голый & в конце: …{tail[:40]!r}")
    if stack:
        ERRS.append(f"{where}: не закрыты теги {stack}")
    plain = html.unescape(TAG.sub("", text))
    return plain


def check_kb(kb, where):
    if not kb:
        return []
    if isinstance(kb, str):
        kb = json.loads(kb)
    datas = []
    for row in kb.get("inline_keyboard", []):
        for b in row:
            if not b.get("text"):
                ERRS.append(f"{where}: кнопка без текста")
            if "callback_data" in b:
                if len(b["callback_data"].encode()) > 64:
                    ERRS.append(f"{where}: callback_data > 64 байт: {b['callback_data']}")
                datas.append(b["callback_data"])
            elif not b.get("url"):
                ERRS.append(f"{where}: кнопка без callback_data и url: {b}")
            elif not re.match(r"^(https?|tg)://", b["url"]):
                ERRS.append(f"{where}: странная ссылка в кнопке: {b['url'][:60]}")
    return datas


def fake_tg(cfg, method, payload, timeout=20, quiet=False):
    where = f"{SCEN}/{method}#{len(MSGS)}"
    text = payload.get("text") or payload.get("caption") or ""
    rec = {"scen": SCEN, "m": method, "chat": str(payload.get("chat_id")), "text": text, "kb": []}
    if method in ("sendMessage", "editMessageText"):
        if not text.strip():
            ERRS.append(f"{where}: пустой текст")
        plain = check_html(text, where) if payload.get("parse_mode") == "HTML" else text
        if len(plain) > 4096:
            ERRS.append(f"{where}: текст {len(plain)} > 4096")
        rec["plain"] = plain
    if method == "sendMediaGroup":
        media = json.loads(payload["media"])
        if not 1 <= len(media) <= 10:
            ERRS.append(f"{where}: альбом из {len(media)}")
        cap = media[0].get("caption", "")
        plain = check_html(cap, where) if media[0].get("parse_mode") == "HTML" else cap
        if len(plain) > 1024:
            ERRS.append(f"{where}: подпись {len(plain)} > 1024")
        rec["text"], rec["plain"] = cap, plain
        ids = []
        for _ in media:
            mid[0] += 1; ids.append({"message_id": mid[0]})
        MSGS.append(rec)
        return {"ok": True, "result": ids}
    if "reply_markup" in payload:
        rec["kb"] = check_kb(payload["reply_markup"], where)
    MSGS.append(rec)
    mid[0] += 1
    return {"ok": True, "result": {"message_id": mid[0]}}


SCEN = "init"
P = [mock.patch.object(rr, "tg_call", fake_tg), mock.patch.object(rr, "SALE_DB_PATH", WORK / "sale.db"),
     mock.patch.object(rr, "send_photo_upload", lambda *a, **k: False),
     mock.patch.object(rr.time, "sleep", lambda *a: None), mock.patch.object(SS.time, "sleep", lambda *a: None)]
for p in P:
    p.start()

cfg = rr.load_config()
cfg["worker_url"] = ""
store = rr.Store(WORK / "radar.db")
sale = rr.Store(WORK / "sale.db")
# как после сброса: чистый поиск, база маклеров и рынок остаются
store.conn.execute("DELETE FROM broker_offers"); store.conn.execute("UPDATE brokers SET status='new', last_contact=NULL")
for k in ("anketa", "request_text", "outreach", "pending_offers", "fu_morning", "fu_evening", "fu_at"):
    store.conn.execute("DELETE FROM kv WHERE key=?", (k,))
store.set_kv("fresh_start", True); store.conn.commit()
sale.conn.execute("DELETE FROM listings"); sale.conn.execute("DELETE FROM phones")
for k in ("sale_pick", "sale_day", "sale_intro_sent", "sale_criteria", "sale_src_next", "sale_dismissed", "sale_pick_day"):
    sale.conn.execute("DELETE FROM kv WHERE key=?", (k,))
sale.conn.commit()
settings = {**rr.default_settings(), **(store.get_kv("settings") or {})}
RESULTS = []


def scen(name):
    def deco(fn):
        global SCEN
        SCEN = name
        n0, e0 = len(MSGS), len(ERRS)
        t = time.time()
        try:
            fn()
            ok = True
        except Exception:
            ERRS.append(f"{name}: ИСКЛЮЧЕНИЕ\n{traceback.format_exc()}")
            ok = False
        RESULTS.append((name, ok, len(MSGS) - n0, len(ERRS) - e0, time.time() - t))
        return fn
    return deco


def last(chat=str(OWNER), m=None):
    for r in reversed(MSGS):
        if r["chat"] == chat and (m is None or r["m"] == m):
            return r
    return None


def expect(cond, msg):
    if not cond:
        ERRS.append(f"{SCEN}: ожидание не выполнено — {msg}")


@scen("1. /start и экран «Ищет Ra'no» до параметров")
def _():
    rr.handle_command("/start", settings, store, cfg)
    expect("Ищу двумя способами" in (last() or {}).get("text", ""), "приветствие")
    scr = rr.rano_screen(cfg, store)
    expect("Сначала расскажите" in scr["text"], "экран ждёт параметров")
    rr.send_screen(cfg, scr); rr.send_screen(cfg, rr.via_screen(cfg, store))


@scen("2. параметры из интервью → текст запроса")
def _():
    ans = {"lang": "ru", "deal": "buy", "object": "flat", "city": "tashkent", "rooms": ["2"], "budget": "50000",
           "note": "ближе к центру, нужна ипотека", "districts": ["2", "8"], "class": "any", "floor_pref": ["nf"],
           "contact": "bot"}
    cg.apply_webapp_data(cfg, store, json.dumps({"v": 3, "replace": True, "src": "chat", "ans": ans}))
    expect(not store.get_kv("fresh_start"), "fresh_start снят")
    expect("Готовый запрос" in (last() or {}).get("text", ""), "текст запроса показан")
    ss = rr.effective_sale_cfg(cfg, store)["sale_search"]
    expect(ss["max_price_usd"] == 50000 and ss["rooms"] == [2] and ss.get("mortgage"), f"поиск покупки: {ss}")
    expect(set(ss["districts"]) == {"Мирабад", "Юнусабад"}, f"районы {ss['districts']}")


@scen("3. поиск по всем сайтам (живые источники) → сразу + подборка")
def _():
    t = time.time()
    n = rr.run_sale_search(rr.effective_sale_cfg(cfg, store), sale, settings, force=True)
    st = SS.day_stats(sale)
    stats = sale.get_kv("sale_src_stats")
    print(f"   источники: { {k: (v.get('n'), v.get('err')[:40] if v.get('err') else '') for k, v in stats.items()} }")
    print(f"   день: {st} · сразу {n} · в подборке {len(SS.pick_pending(sale))} · {time.time() - t:.0f} c")
    expect(st.get("seen", 0) > 20, "мало объявлений с сайтов")
    intro = [r for r in MSGS if r["scen"] == SCEN and "Поиск квартиры для покупки включён" in r["text"]]
    expect(intro, "вступление")


@scen("4. экран «Ищет Ra'no», подборка по кнопке, 👍 и «Мимо»")
def _():
    scr = rr.rano_screen(cfg, store); rr.send_screen(cfg, scr)
    q = SS.pick_pending(sale)
    if q:
        rr.handle_callback("R:pick", settings, store, cfg, 1)
    pick = [r for r in MSGS if r["scen"] == SCEN and "L:s:" in " ".join(r["kb"])]
    # инстант-карточки (если были) и подборка: 👍 первого
    cards = [r for r in MSGS if any(d.startswith("L:n:") for d in r["kb"])]
    target = (pick or cards)
    if not target:
        print("   нечего отмечать — подходящих нет"); return
    data = next(d for d in target[-1]["kb"] if d.startswith("L:s:"))
    toast, _ = rr.handle_callback(data, settings, store, cfg, 1)
    expect("шортлист" in toast.lower(), f"👍 → {toast}")
    toast2, _ = rr.handle_callback(data, settings, store, cfg, 1)
    expect(toast2 == "Уже в шортлисте", "повтор 👍")
    if cards:
        nd = next(d for d in cards[-1]["kb"] if d.startswith("L:n:"))
        t3, _ = rr.handle_callback(nd, settings, store, cfg, 1)
        expect("Убрала" in t3, f"Мимо → {t3}")


@scen("5. рассылка маклерам по одному")
def _():
    rr.send_broker_cards(cfg, store, settings)
    card = last()
    expect(card and any(d.startswith("bw:") for d in card["kb"]), "карточка маклера с кнопками")
    for _ in range(2):
        bw = next(d for d in last()["kb"] if d.startswith("bw:"))
        rr.handle_callback(bw, settings, store, cfg, 1)
    bx = next(d for d in last()["kb"] if d.startswith("bx:"))
    rr.handle_callback(bx, settings, store, cfg, 1)
    expect(store.get_kv("outreach")["sent"] == 2, f"outreach {store.get_kv('outreach')}")


BROKER = 900000001


@scen("6. маклер: /start, вариант частями, карточка владельцу")
def _():
    rr.handle_broker_message(cfg, store, {"chat": {"id": BROKER, "first_name": "Нодира"}, "text": "/start"})
    w = last(str(BROKER))
    expect(w and "Клиент ищет" in w["text"] and "купить" in w["text"], "приветствие маклеру с запросом")
    rr.handle_broker_message(cfg, store, {"chat": {"id": BROKER, "first_name": "Нодира"},
                                          "text": "Продаётся 2 комн Мирабад, ул. Шахрисабз 14, 58 м², 5/9 этаж, евроремонт, 48 000 у.е., ипотека возможна",
                                          "photo": [{"file_id": "PH1", "width": 1280, "height": 960}]})
    rr.handle_broker_message(cfg, store, {"chat": {"id": BROKER}, "photo": [{"file_id": "PH2", "width": 1280, "height": 960}]})
    pend = store.get_kv("pending_offers") or {}
    for k in pend:
        pend[k]["at"] -= 100
    store.set_kv("pending_offers", pend)
    rr.flush_pending_offer(cfg, store, sale)
    o = store.conn.execute("SELECT oid, price_usd, rooms, area, floor FROM broker_offers WHERE broker_chat=?",
                           (str(BROKER),)).fetchone()
    expect(o and o[1] == 48000 and o[2] == 2 and o[3] == 58 and o[4] == 5, f"вариант распознан: {o}")
    expect(any("t:s:" in " ".join(r["kb"]) for r in MSGS if r["scen"] == SCEN), "кнопки триажа")


@scen("7. триаж → шортлист → карточка → уточнение → ответ маклера")
def _():
    oid = store.conn.execute("SELECT oid FROM broker_offers WHERE broker_chat=?", (str(BROKER),)).fetchone()[0]
    rr.handle_callback(f"t:s:{oid}", settings, store, cfg, 1)
    cg.show_shortlist(cfg, store)
    expect("Шортлист" in last()["text"], "шортлист показан")
    rr.handle_callback(f"s:o:{oid}", settings, store, cfg, 5)
    expect("Этап" in last()["text"], "карточка варианта")
    rr.handle_callback(f"o:ask:{oid}", settings, store, cfg, 5)
    q = last(str(BROKER))
    expect(q and "актуален" in q["text"] and "ипотека" in q["text"].lower(), "вопросы маклеру про покупку")
    rr.handle_broker_message(cfg, store, {"chat": {"id": BROKER}, "text": "Да, актуально. Документы готовы, торг 1000$, смотреть можно в субботу"})
    expect(cg.get_offer(store, oid)["replied_at"], "ответ прикреплён")


@scen("8. просмотр, заметка, напоминания, «посмотрели»")
def _():
    oid = store.conn.execute("SELECT oid FROM broker_offers WHERE broker_chat=?", (str(BROKER),)).fetchone()[0]
    TZ = cg.TZ
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
    cg.set_viewing(cfg, store, oid, tomorrow.isoformat(), "завтра, 11:00")
    cg.add_note(store, oid, "Двор хороший, торг 1000$")
    rr.handle_callback(f"s:o:{oid}", settings, store, cfg, 5)
    expect("просмотр завтра" in last()["text"] and "торг 1000" in last()["text"], "этап и заметка в карточке")
    T = lambda h, m=0, d=1: tomorrow.replace(hour=h, minute=m) + timedelta(days=d - 1)
    expect(fu.morning_note(cfg, store, T(9)), "утренняя записка")
    expect(fu.viewing_reminders(cfg, store, T(9, 30)) == 1, "за 2 часа")
    expect(fu.viewing_reminders(cfg, store, T(12, 30)) == 1, "как прошёл?")
    rr.handle_callback(f"o:seen:{oid}:g", settings, store, cfg, 5)
    expect("нравится" in cg.stage_of(cg.get_offer(store, oid)), "этап «нравится»")


@scen("9. «Мимо» с причиной, отказ маклеру")
def _():
    rr.handle_broker_message(cfg, store, {"chat": {"id": BROKER + 1, "first_name": "Азиз"},
                                          "text": "3 комн Юнусабад 80м2 7/9 95000$"})
    pend = store.get_kv("pending_offers") or {}
    for k in pend:
        pend[k]["at"] -= 100
    store.set_kv("pending_offers", pend)
    rr.flush_pending_offer(cfg, store, sale)
    oid = store.conn.execute("SELECT oid FROM broker_offers WHERE broker_chat=?", (str(BROKER + 1),)).fetchone()[0]
    rr.handle_callback(f"t:n:{oid}", settings, store, cfg, 1)
    rr.handle_callback(f"t:r:{oid}:p", settings, store, cfg, 1)
    d = last(str(BROKER + 1))
    expect(d and "не подошёл" in d["text"] and "дешевле" in d["text"], "отказ с подсказкой")


@scen("10. молчащий маклер → напомнить")
def _():
    rr.handle_broker_message(cfg, store, {"chat": {"id": BROKER + 2, "first_name": "Бахтиёр"},
                                          "text": "2 комн Мирабад 52м2 3/4 45000$ срочно"})
    pend = store.get_kv("pending_offers") or {}
    for k in pend:
        pend[k]["at"] -= 100
    store.set_kv("pending_offers", pend)
    rr.flush_pending_offer(cfg, store, sale)
    oid = store.conn.execute("SELECT oid FROM broker_offers WHERE broker_chat=?", (str(BROKER + 2),)).fetchone()[0]
    rr.handle_callback(f"t:s:{oid}", settings, store, cfg, 1)
    cg.request_details(cfg, store, [oid])
    old = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    store.conn.execute("UPDATE broker_offers SET asked_at=? WHERE oid=?", (old, oid)); store.conn.commit()
    noon = datetime.now(cg.TZ).replace(hour=12, minute=0)
    expect(fu.silent_brokers(cfg, store, noon) >= 1, "вопрос «напомнить?»")
    rr.handle_callback(f"o:rem:{oid}", settings, store, cfg, 7)
    expect("Напоминаю" in (last(str(BROKER + 2)) or {}).get("text", ""), "напоминание ушло")


@scen("11. владелец пересылает вариант из WhatsApp")
def _():
    rr.handle_owner_offer(cfg, store, {"chat": {"id": OWNER}, "forward_sender_name": "Маклер Джамшид",
                                       "text": "Яккасарай 2 комн 50 м2 2/5 этаж 49 000$ ремонт свежий"})
    pend = store.get_kv("pending_offers") or {}
    for k in pend:
        pend[k]["at"] -= 100
    store.set_kv("pending_offers", pend)
    rr.flush_pending_offer(cfg, store, sale)
    expect(any("Яккасарай" in r.get("text", "") for r in MSGS if r["scen"] == SCEN), "карточка из пересланного")


@scen("12. справка, /sale, /rynok, /request, экраны, снимок для воркера")
def _():
    for c in ("/help", "/sale", "/rynok", "/request", "/rano", "/via"):
        reply, view = rr.handle_command(c, settings, store, cfg)
        if reply:
            rr.send_telegram(cfg, reply)
    snap = rr.ui_snapshot(cfg, store, settings)
    for name, scr in snap["screens"].items():
        check_html(scr["text"], f"снимок {name}"); check_kb(scr["kb"], f"снимок {name}")
    for oid, c in snap["cards"].items():
        check_html(c["text"], f"снимок карточка {oid}"); check_kb(c["kb"], f"снимок карточка {oid}")
    for o in snap["offers"]:
        check_html(o["text"], f"снимок вариант {o['oid']}")
    for b in snap["brokers"]:
        check_html(b["body"], f"снимок маклер {b['bid']}")
    for k, t in snap["texts"].items():
        check_html(t, f"снимок текст {k}")
    print(f"   снимок: {len(json.dumps(snap, ensure_ascii=False)) // 1024} КБ, карточек {len(snap['cards'])}, маклеров {len(snap['brokers'])}")


@scen("13. вечерние итоги и подборка дня")
def _():
    eve = datetime.now(cg.TZ).replace(hour=20, minute=10)
    store.set_kv("fu_evening", "")
    fu.evening_digest(cfg, store, sale, eve)
    expect(any("Итоги дня" in r["text"] for r in MSGS if r["scen"] == SCEN), "итоги дня")
    sale.set_kv("sale_pick_day", "")
    SS.maybe_daily_pick(cfg, sale, eve.replace(hour=19, minute=40))


for p in P:
    p.stop()

# голос: мужской род о себе — только в сообщениях бота (не в запросе от лица клиента и не в чужом тексте)
voice = []
for r in MSGS:
    t = r.get("plain") or r.get("text") or ""
    t = re.sub(r"<code>.*?</code>", "", t, flags=re.S)
    if "Готовый запрос" in t or r["kb"] and any(d.startswith(("bw:", "bx:")) for d in r["kb"]):
        continue                                      # текст запроса — голос клиента
    for line in t.split("\n"):
        if line.strip().startswith(("<i>", "«")) or "пишет:" in line:
            continue
        if MASC.search(line) and not re.search(r"(клиент|маклер|вы|продавец|собственник|он|она)\s+\S*\s*$", line[:MASC.search(line).start()].lower()):
            voice.append(f"{r['scen']}: «{line.strip()[:110]}»")

print("\n================ ИТОГ ================")
for name, ok, n, e, sec in RESULTS:
    print(f"{'✅' if ok and not e else '❌'} {name}: сообщений {n}, ошибок {e}, {sec:.0f} c")
print(f"\nВсего сообщений: {len(MSGS)}, ошибок формата/логики: {len(ERRS)}")
for e in ERRS:
    print("  ✗", e[:600])
print(f"\nМужской род о себе (проверить вручную): {len(voice)}")
for v in voice[:40]:
    print("  ?", v)
json.dump(MSGS, open(WORK / "messages.json", "w"), ensure_ascii=False, indent=1)

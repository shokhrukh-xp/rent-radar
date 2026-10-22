"""Ra'no пишет продавцу первой — со своего Telegram-аккаунта, а если продавца в Telegram нет — SMS со ссылкой на бота.

Бот Telegram не может начать чат с незнакомым человеком, поэтому у Ra'no есть отдельный аккаунт
(«Ra'no · AI-помощник», своя SIM). Клиент нажал «Уточнить» по объявлению — это и есть его просьба написать продавцу:
Ra'no находит продавца по номеру из объявления, пишет ему от себя (представляется ИИ-помощником клиента), читает
ответы и приносит их к карточке варианта. Продавца в Telegram нет — SMS (Eskiz) со ссылкой «ответить в боте».

Бережно, чтобы аккаунт не сочли спамом: только по нажатию клиента, одно сообщение на объявление,
не больше NEW_PER_DAY новых чатов в день на всех, пауза GAP между ними, только с 9 до 21 по Ташкенту.
Telegram ограничил аккаунт (PeerFlood) — пауза на сутки, владельцу сообщение, клиентам — прежний путь «отправьте сами».

Ключи (сессия аккаунта, пароль Eskiz) — только в STATE_DIR/agent/*.json (права 600, в бэкап не попадают).
Записывает их владелец сам: tools/agent_setup.py. Без них модуль молчит и всё работает как раньше.
"""
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("rent-radar")

TZ = timezone(timedelta(hours=5))
NEW_PER_DAY = 15            # новых чатов в день с аккаунта Ra'no — на всех клиентов
PER_CLIENT_DAY = 5          # и на одного клиента
SMS_PER_DAY = 10
GAP = 90                    # секунд между первыми сообщениями незнакомым
HOURS = (9, 21)             # пишем незнакомым только днём
POLL_EVERY = 20             # как часто смотреть новые ответы
PEER_DAYS = 30              # сколько помним, чей это продавец
UNKNOWN_NOTE_DAYS = 30
SMS_TEXT = ("Ra'no (AI-yordamchi): e'loningiz bo'yicha mijozimning savoli bor / vopros po vashemu obyavleniyu. "
            "Javob: t.me/{bot}?start={code}")

ADAPTER = {"tg": None, "sms": None}       # тесты подставляют свои
_STATE = {"tg": None, "tg_err": "", "tg_next": 0.0, "poll_at": 0.0, "sms": None}


def _rr():
    import rent_radar
    return rent_radar


def agent_dir():
    return _rr().STATE_DIR / "agent"


def _conf(name):
    try:
        return json.loads((agent_dir() / f"{name}.json").read_text())
    except (OSError, ValueError):
        return None


def save_conf(name, data):
    d = agent_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = d / f"{name}.json"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(p, 0o600)


def tg_ready() -> bool:
    if ADAPTER["tg"] is not None:
        return True
    c = _conf("telegram") or {}
    return bool(c.get("session") and c.get("api_id") and c.get("api_hash"))


def sms_ready() -> bool:
    if ADAPTER["sms"] is not None:
        return True
    c = _conf("sms") or {}
    return bool(c.get("email") and c.get("password"))


def _shared(store):
    import tenants
    return tenants.shared_store(store)


def _now_local(now=None):
    return datetime.fromtimestamp(now or time.time(), TZ)


def daytime(now=None) -> bool:
    return HOURS[0] <= _now_local(now).hour < HOURS[1]


def _day(sh, now=None):
    d = _now_local(now).strftime("%Y-%m-%d")
    b = sh.get_kv("ra_day") or {}
    if b.get("d") != d:
        b = {"d": d, "n": 0, "sms": 0, "per": {}}
    return b


def paused(sh, now=None) -> float:
    return max(0.0, float(sh.get_kv("ra_pause_until") or 0) - (now or time.time()))


def phones_of(o):
    out = []
    for p in ((o.get("extra") or {}).get("phones") or []):
        d = re.sub(r"\D", "", str(p or ""))
        if len(d) >= 9:
            d = d if d.startswith("998") else "998" + d[-9:]
            if d not in out:
                out.append(d)
    return out[:2]


def can_take(o) -> bool:
    """Ra'no может написать продавцу сама: есть номер и есть чем писать."""
    return bool(phones_of(o)) and (tg_ready() or sms_ready())


# ----------------------------------------------------------------- очередь --

def enqueue(cfg, store, o, text) -> str:
    """Клиент попросил уточнить объявление — ставим «написать продавцу» в очередь. Ответ — для клиента."""
    sh = _shared(store)
    chat = str(cfg["telegram_chat_id"])
    q = sh.get_kv("ra_queue") or []
    if any(x.get("chat") == chat and x.get("oid") == o["oid"] for x in q):
        return "Уже пишу этому продавцу — жду очереди 🙂"
    q.append({"chat": chat, "oid": o["oid"], "phones": phones_of(o), "text": text, "at": time.time(),
              "tries": 0})
    sh.set_kv("ra_queue", q)
    rr = _rr()
    when = ""
    b = _day(sh)
    if not daytime():
        when = " Сейчас поздно — напишу утром, после 9:00."
    elif paused(sh) or b["n"] >= NEW_PER_DAY or b["per"].get(chat, 0) >= PER_CLIENT_DAY:
        when = " Сегодня уже много написала — продавцу напишу завтра утром."
    rr.send_telegram(cfg, f"📨 Напишу продавцу варианта #{o['oid']} сама — представлюсь вашим AI-помощником."
                          f"{when} Как ответит — принесу ответ сюда, к карточке.")
    return "📨 Пишу продавцу"


def _tenant(cfg, store, chat):
    """cfg/store и контекст клиента, которому принадлежит задача."""
    import tenants
    from contextlib import nullcontext
    t_ = tenants.get(chat)
    if t_ is None or t_.owner or str(chat) == str(cfg["telegram_chat_id"]):
        return cfg, store, nullcontext()
    return t_.cfg, t_.store, tenants.ctx(t_)


def _alert(cfg, sh, key, text, every=6 * 3600):
    k = f"ra_alert:{key}"
    if time.time() - float(sh.get_kv(k) or 0) < every:
        return
    sh.set_kv(k, time.time())
    _rr().tg_call(cfg, "sendMessage", {"chat_id": cfg.get("admin_chat_id") or cfg["telegram_chat_id"],
                                       "_noTr": True, "text": text})


def _fallback(cfg, store, job, why=""):
    """Написать сама не вышло — как раньше: готовый текст, клиент отправит сам."""
    import concierge
    o = concierge.get_offer(store, job["oid"])
    if not o:
        return
    deal = (concierge.get_anketa(store).get("ans") or {}).get("deal", "rent")
    if why:
        _rr().send_telegram(cfg, f"🙈 Продавцу варианта #{o['oid']} написать не получилось ({why}).")
    concierge.manual_ask(cfg, store, o, deal)


# ----------------------------------------------------------------- адаптеры --

class NotAuthorized(Exception):
    pass


def _ensure_telethon():
    try:
        import telethon  # noqa: F401
        return True
    except ImportError:
        pass
    import subprocess
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", "telethon>=1.36,<2"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        log.warning("[ra] telethon не установился: %s", (r.stderr or "")[-300:])
        return False
    import importlib
    importlib.invalidate_caches()
    return True


class TelethonAdapter:
    """Аккаунт Ra'no через Telethon (синхронный режим): найти по номеру, написать, прочитать ответы."""

    def __init__(self, conf):
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        self.c = TelegramClient(StringSession(conf["session"]), int(conf["api_id"]), conf["api_hash"],
                                device_model="Ra'no server", app_version="1.0", system_version="Linux")
        self.c.connect()
        if not self.c.is_user_authorized():
            raise NotAuthorized()

    def _peer(self, pid, h):
        from telethon.tl.types import InputPeerUser
        return InputPeerUser(int(pid), int(h or 0))

    def resolve(self, phone, label):
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact
        r = self.c(ImportContactsRequest([InputPhoneContact(client_id=random.randrange(1, 1 << 62),
                                                            phone="+" + phone, first_name=label, last_name="")]))
        u = next((x for x in r.users if not getattr(x, "bot", False)), None)
        return (str(u.id), str(u.access_hash)) if u else None

    def send(self, pid, h, text):
        m = self.c.send_message(self._peer(pid, h), text, parse_mode="html", link_preview=False)
        return m.id

    def fetch(self, last_ids):
        out = []
        for d in self.c.iter_dialogs(limit=40):
            e = d.entity
            if not d.is_user or getattr(e, "bot", False) or getattr(e, "is_self", False) or d.unread_count <= 0:
                continue
            pid = str(e.id)
            name = " ".join(x for x in (getattr(e, "first_name", ""), getattr(e, "last_name", "")) if x)
            for m in reversed(self.c.get_messages(e, limit=min(d.unread_count, 20))):
                if m.out or m.id <= int(last_ids.get(pid) or 0):
                    continue
                text = m.message or ""
                if getattr(m, "geo", None):
                    text = "\n".join(x for x in (text, f"📍 Локация: {m.geo.lat:.6f}, {m.geo.long:.6f}") if x)
                photos = []
                if getattr(m, "photo", None):
                    try:
                        photos.append(self.c.download_media(m, file=bytes))
                    except Exception as ex:
                        log.info("[ra] фото не скачалось: %s", ex)
                out.append({"peer": pid, "hash": str(e.access_hash), "id": m.id, "text": text,
                            "photos": photos, "name": name or "продавец"})
            try:
                self.c.send_read_acknowledge(e)
            except Exception:
                pass
        return out


class EskizSMS:
    BASE = "https://notify.eskiz.uz/api"

    def __init__(self, conf):
        self.conf, self.token = conf, None

    def _login(self):
        import requests
        r = requests.post(self.BASE + "/auth/login", data={"email": self.conf["email"],
                                                           "password": self.conf["password"]}, timeout=20)
        r.raise_for_status()
        self.token = (r.json().get("data") or {}).get("token")
        if not self.token:
            raise RuntimeError("Eskiz: нет токена")

    def send(self, phone, text):
        import requests
        for attempt in (0, 1):
            if not self.token:
                self._login()
            r = requests.post(self.BASE + "/message/sms/send", timeout=20,
                              headers={"Authorization": f"Bearer {self.token}"},
                              data={"mobile_phone": phone, "message": text, "from": self.conf.get("from") or "4546"})
            if r.status_code == 401 and attempt == 0:
                self.token = None
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Eskiz {r.status_code}: {r.text[:200]}")
            return (r.json() or {}).get("id") or True
        return None


def _tg(cfg, sh):
    if ADAPTER["tg"] is not None:
        return ADAPTER["tg"]
    if _STATE["tg"] is not None:
        return _STATE["tg"]
    if not tg_ready() or time.time() < _STATE["tg_next"]:
        return None
    try:
        if not _ensure_telethon():
            raise RuntimeError("нет telethon")
        _STATE["tg"] = TelethonAdapter(_conf("telegram"))
        log.info("[ra] аккаунт Ra'no на связи")
        return _STATE["tg"]
    except NotAuthorized:
        _STATE["tg_next"] = time.time() + 3600
        _alert(cfg, sh, "auth", "⚠️ Аккаунт Ra'no (пишет продавцам) не авторизован — войдите заново: "
                                "tools/agent_setup.py telegram. Пока клиенты отправляют вопросы сами.", 24 * 3600)
    except Exception as e:
        _STATE["tg_next"] = time.time() + 600
        log.warning("[ra] аккаунт не подключился: %s", e)
    return None


def _sms():
    if ADAPTER["sms"] is not None:
        return ADAPTER["sms"]
    if _STATE["sms"] is None and sms_ready():
        _STATE["sms"] = EskizSMS(_conf("sms"))
    return _STATE["sms"]


def _ban_like(e) -> str:
    n = type(e).__name__
    if n in ("PeerFloodError",):
        return "flood"
    if n in ("FloodWaitError", "SlowModeWaitError"):
        return "wait"
    if n in ("UserDeactivatedBanError", "UserDeactivatedError", "AuthKeyUnregisteredError",
             "SessionRevokedError", "AuthKeyDuplicatedError"):
        return "dead"
    return ""


def _drop_tg():
    try:
        if _STATE["tg"] is not None:
            _STATE["tg"].c.disconnect()
    except Exception:
        pass
    _STATE["tg"] = None


# ----------------------------------------------------------------- такт --

def tick(cfg, store, now=None):
    """Из основного цикла: отправить очередное первое сообщение и забрать ответы продавцов."""
    if not (tg_ready() or sms_ready()):
        return
    now = now or time.time()
    sh = _shared(store)
    try:
        _send_next(cfg, sh, now)
    except Exception as e:
        log.warning("[ra] отправка: %s", e)
    if now - _STATE["poll_at"] >= POLL_EVERY:
        _STATE["poll_at"] = now
        try:
            poll(cfg, store, sh)
        except Exception as e:
            log.warning("[ra] ответы: %s", e)
            if _ban_like(e) == "dead":
                _drop_tg()


def _send_next(cfg, sh, now):
    q = sh.get_kv("ra_queue") or []
    if not q or not daytime(now) or paused(sh, now):
        return
    if now - float(sh.get_kv("ra_last_new") or 0) < GAP:
        return
    b = _day(sh, now)
    if b["n"] >= NEW_PER_DAY:
        return
    job = next((j for j in q if b["per"].get(j["chat"], 0) < PER_CLIENT_DAY), None)
    if job is None:
        return
    q = [j for j in q if j is not job]
    tcfg, tstore, ctx = _tenant(cfg, sh, job["chat"])
    with ctx:
        res = _deliver(tcfg, tstore, sh, job, b, now)
    if res == "retry":
        job["tries"] = job.get("tries", 0) + 1
        if job["tries"] < 3:
            q.append(job)
        else:
            with ctx:
                _fallback(tcfg, tstore, job, "не получилось связаться")
    elif res == "pause":
        for j in [job] + q:                  # аккаунт ограничили — всем, кто ждёт, прежний путь
            c2, s2, x2 = _tenant(cfg, sh, j["chat"])
            with x2:
                _fallback(c2, s2, j)
        q = []
    sh.set_kv("ra_queue", q)
    sh.set_kv("ra_day", b)


def _deliver(cfg, store, sh, job, b, now):
    """Одна задача: Telegram по номеру → SMS → «отправьте сами». Возвращает ok / retry / pause / manual."""
    import concierge
    rr = _rr()
    o = concierge.get_offer(store, job["oid"])
    if not o:
        return "manual"
    tg = _tg(cfg, sh)
    if tg is not None:
        for ph in job["phones"]:
            try:
                peer = tg.resolve(ph, f"Продавец #{o['oid']}")
                sh.set_kv("ra_last_new", now)
                if not peer:
                    continue
                mid = tg.send(peer[0], peer[1], job["text"])
            except Exception as e:
                kind = _ban_like(e)
                log.warning("[ra] продавцу #%s не ушло: %s %s", o["oid"], type(e).__name__, str(e)[:120])
                if kind == "flood":
                    sh.set_kv("ra_pause_until", now + 24 * 3600)
                    _alert(cfg, sh, "flood", "⚠️ Telegram ограничил аккаунт Ra'no на новые чаты (жалобы на спам?). "
                                             "Пауза на сутки; клиенты пока отправляют вопросы сами.")
                    return "pause"
                if kind == "wait":
                    sh.set_kv("ra_pause_until", now + min(int(getattr(e, "seconds", 600) or 600), 24 * 3600))
                    return "retry"
                if kind == "dead":
                    _drop_tg()
                    _alert(cfg, sh, "dead", f"⚠️ Аккаунт Ra'no недоступен ({type(e).__name__}). "
                                            "Клиенты пока отправляют вопросы сами.", 24 * 3600)
                    return "pause"
                return "retry"
            key = f"ra:{peer[0]}"
            peers = sh.get_kv("ra_peers") or {}
            peers[peer[0]] = {"chat": job["chat"], "oid": o["oid"], "at": now, "hash": peer[1]}
            sh.set_kv("ra_peers", peers)
            last = sh.get_kv("ra_last_ids") or {}
            last[peer[0]] = max(int(last.get(peer[0]) or 0), int(mid or 0))
            sh.set_kv("ra_last_ids", last)
            concierge.link_manual_ask(store, o["oid"], key)
            b["n"] += 1
            b["per"][job["chat"]] = b["per"].get(job["chat"], 0) + 1
            rr.send_telegram(cfg, f"📨 Написала продавцу варианта #{o['oid']} в Telegram. Как ответит — "
                                  "принесу сюда.")
            log.info("[ra] написала продавцу #%s (клиент %s)", o["oid"], job["chat"])
            return "ok"
    sms = _sms()
    bd = _day(sh, now)
    if sms is not None and job["phones"] and bd.get("sms", 0) < SMS_PER_DAY:
        code = "s" + _b36(int(job["chat"])) + "-" + _b36(int(o["oid"]))
        text = SMS_TEXT.format(bot=cfg.get("bot_username") or "rano_smart_bot", code=code)
        try:
            sms.send(job["phones"][0], text)
        except Exception as e:
            log.warning("[ra] SMS продавцу #%s: %s", o["oid"], e)
            _alert(cfg, sh, "sms", f"⚠️ SMS продавцу не ушло: {str(e)[:200]}", 24 * 3600)
        else:
            b["sms"] = b.get("sms", 0) + 1
            concierge._set_extra(store, o["oid"], sms_at=datetime.now(timezone.utc).isoformat())
            store.conn.execute("UPDATE broker_offers SET status='asked', asked_at=? WHERE oid=?",
                               (datetime.now(timezone.utc).isoformat(), o["oid"]))
            store.conn.commit()
            who = rr.fmt_phone("+" + job["phones"][0])
            rr.send_telegram(cfg, f"📨 Продавца варианта #{o['oid']} нет в Telegram — отправила ему SMS со ссылкой "
                                  f"на меня. Ответит — принесу сюда. Если не ответит до завтра, проще позвонить: 📞 {who}")
            return "ok"
    _fallback(cfg, store, job, "его нет в Telegram" if tg is not None else "")
    return "manual"


def _b36(n: int) -> str:
    s, n = "", int(n)
    if n == 0:
        return "0"
    while n:
        n, r = divmod(n, 36)
        s = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + s
    return s


# ----------------------------------------------------------------- ответы --

def poll(cfg, store, sh=None):
    sh = sh or _shared(store)
    tg = _tg(cfg, sh)
    if tg is None:
        return 0
    last = sh.get_kv("ra_last_ids") or {}
    msgs = tg.fetch(last)
    if not msgs:
        return 0
    peers = sh.get_kv("ra_peers") or {}
    since = time.time() - PEER_DAYS * 86400
    n = 0
    for m in msgs:
        last[m["peer"]] = max(int(last.get(m["peer"]) or 0), int(m["id"]))
        p = peers.get(m["peer"])
        if not p or p.get("at", 0) < since:
            _unknown(cfg, sh, tg, m)
            continue
        tcfg, tstore, ctx = _tenant(cfg, sh, p["chat"])
        with ctx:
            try:
                handle_reply(tcfg, tstore, m, int(p["oid"]))
                n += 1
            except Exception as e:
                log.warning("[ra] ответ продавца #%s не разобрала: %s", p.get("oid"), e)
    sh.set_kv("ra_last_ids", last)
    return n


def _unknown(cfg, sh, tg, m):
    """Пишет кто-то, кому Ra'no не писала (маклер нашёл аккаунт) — один раз вежливо в бота."""
    seen = sh.get_kv("ra_unknown") or {}
    if time.time() - float(seen.get(m["peer"]) or 0) < UNKNOWN_NOTE_DAYS * 86400:
        return
    seen[m["peer"]] = time.time()
    sh.set_kv("ra_unknown", seen)
    bot = cfg.get("bot_username") or "rano_smart_bot"
    try:
        tg.send(m["peer"], m.get("hash"), f"Здравствуйте! Я Ra'no — AI-помощник по поиску жилья, пишу продавцам "
                                          f"по просьбе клиентов. Есть квартира — пришлите её в бот @{bot} 🙂\n\n"
                                          f"Assalomu alaykum! Men Ra'no, AI-yordamchiman. Variantlarni @{bot} "
                                          "botiga yuboring.")
    except Exception as e:
        log.info("[ra] незнакомому не ответила: %s", e)


def handle_reply(cfg, store, m, oid):
    """Ответ продавца в аккаунт Ra'no — к карточке варианта (как ответ в чате клиента)."""
    rr = _rr()
    import concierge
    key = f"ra:{m['peer']}"
    ba = store.get_kv("biz_asked") or {}
    if (ba.get(key) or {}).get("oid") != oid or time.time() - (ba.get(key) or {}).get("at", 0) > 48 * 3600:
        ba[key] = {"oid": oid, "at": time.time()}       # этот чат — только про это объявление
        store.set_kv("biz_asked", ba)
    if m.get("photos"):
        ids = _upload_photos(cfg, m["photos"], oid)
        if ids:
            o = concierge.get_offer(store, oid) or {}
            photos = list(o.get("photos") or []) + [x for x in ids if x not in (o.get("photos") or [])]
            store.conn.execute("UPDATE broker_offers SET photos=? WHERE oid=?", (json.dumps(photos[:20]), oid))
            store.conn.commit()
    if (m.get("text") or "").strip():
        rr.handle_broker_message(cfg, store, {"chat": {"id": key, "first_name": m.get("name") or "продавец"},
                                              "text": m["text"]}, key=key, quiet=True)


def _upload_photos(cfg, blobs, oid):
    """Фото от продавца — клиенту альбомом; file_id — в вариант."""
    rr = _rr()
    blobs = [b for b in blobs if b][:6]
    if not blobs:
        return []
    cap = f"📷 Продавец прислал фото — вариант #{oid}"
    if len(blobs) == 1:
        r = rr.tg_call_files(cfg, "sendPhoto", {"chat_id": cfg["telegram_chat_id"], "caption": cap},
                             {"photo": ("p.jpg", blobs[0])})
        res = [(r or {}).get("result") or {}]
    else:
        media = [{"type": "photo", "media": f"attach://p{i}", **({"caption": cap} if i == 0 else {})}
                 for i in range(len(blobs))]
        r = rr.tg_call_files(cfg, "sendMediaGroup", {"chat_id": cfg["telegram_chat_id"],
                                                     "media": json.dumps(media, ensure_ascii=False)},
                             {f"p{i}": (f"p{i}.jpg", b) for i, b in enumerate(blobs)})
        res = (r or {}).get("result") or []
    ids = []
    for x in res:
        ph = x.get("photo") or []
        if ph:
            ids.append(max(ph, key=lambda p: (p.get("width") or 0) * (p.get("height") or 0))["file_id"])
    return ids


def send_now(cfg, store, key, text):
    """Следующие сообщения в уже открытый чат (просмотр, напоминание, вежливый отказ) — без очереди и лимитов
    новых чатов. None — не ушло."""
    sh = _shared(store)
    pid = str(key)[3:]
    p = (sh.get_kv("ra_peers") or {}).get(pid) or {}
    tg = _tg(cfg, sh)
    if tg is None:
        return None
    try:
        return tg.send(pid, p.get("hash"), text)
    except Exception as e:
        log.warning("[ra] в чат продавца не ушло: %s", e)
        if _ban_like(e) == "dead":
            _drop_tg()
        return None


def status(store) -> str:
    sh = _shared(store)
    b = _day(sh)
    parts = [f"аккаунт: {'есть' if tg_ready() else 'нет'}", f"SMS: {'есть' if sms_ready() else 'нет'}",
             f"сегодня новых чатов {b['n']}/{NEW_PER_DAY}, SMS {b.get('sms', 0)}/{SMS_PER_DAY}",
             f"в очереди {len(sh.get_kv('ra_queue') or [])}"]
    if paused(sh):
        parts.append(f"пауза ещё {int(paused(sh) // 60)} мин")
    return "; ".join(parts)

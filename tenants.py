"""Этап 2: много пользователей.

Каждый пользователь — «арендатор» (tenant): свой чат, свои базы в STATE_DIR/users/<chat>/ (radar.db — анкета,
варианты маклеров, маклеры со своими статусами; sale.db — что видел, подборка), язык, подключение Telegram для
бизнеса. Общее — срез рынка (из sale.db владельца, подключён как «shared»), справочник ориентиров, скачанные
страницы сайтов (кэш fetch на минуту) и каталог маклеров (копия раз в 12 часов).

Владелец (OWNER_CHAT) — тоже арендатор, со старыми путями: его данные никуда не переезжают.
Включается cfg["multi_user"] (RADAR_MULTI_USER=on); выключено — всё как раньше, один клиент.
Кто пользователь, а кто в листе ожидания (лимит, по умолчанию 20), решает воркер: /svc/users.
"""
import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("rent-radar")

REG = {"multi": False, "at": 0.0, "t": {}, "owner": None}
REFRESH_EVERY = 60
BROKER_SYNC_EVERY = 12 * 3600
BROKER_COLS = "bid, source, name, phone, ads, districts, min_price, max_price, first_seen, deals, tg"


def _rr():
    import rent_radar
    return rent_radar


class Tenant:
    def __init__(self, chat, cfg, owner=False, store=None, sale=None, info=None):
        rr = _rr()
        self.chat, self.owner, self.info = str(chat), owner, info or {}
        self.dir = rr.STATE_DIR if owner else rr.STATE_DIR / "users" / self.chat
        if not owner:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg if owner else dict(cfg, telegram_chat_id=self.chat, tenant=self.chat,
                                          admin_chat_id=cfg.get("admin_chat_id") or cfg["telegram_chat_id"])
        self._store, self._sale = store, sale

    @property
    def sale_path(self) -> Path:
        return self.dir / "sale.db"

    @property
    def store(self):
        if self._store is None:
            rr = _rr()
            self._store = rr.Store(self.dir / "radar.db")
            if not self.owner:
                sync_brokers(self._store, rr.DB_PATH)
        return self._store

    @property
    def sale(self):
        if self._sale is None:
            rr = _rr()
            self._sale = rr.Store(self.sale_path)
            if not self.owner:
                self._sale.attach_shared(rr.SALE_DB_PATH)
        return self._sale

    def settings(self):
        rr = _rr()
        return {**rr.default_settings(), **(self.store.get_kv("settings") or {})}


def sync_brokers(store, owner_db, now=None):
    """Каталог маклеров — копия из базы владельца (новые маклеры; статусы «написали/ответил» у каждого свои)."""
    now = now or time.time()
    if now - float(store.get_kv("brokers_synced_at") or 0) < BROKER_SYNC_EVERY or not Path(owner_db).exists():
        return 0
    if Path(owner_db).resolve() == Path(store.conn.execute("PRAGMA database_list").fetchone()[2]).resolve():
        return 0
    try:
        store.conn.execute("ATTACH DATABASE ? AS o", (str(owner_db),))
        cur = store.conn.execute(f"INSERT OR IGNORE INTO brokers({BROKER_COLS}, status) "
                                 f"SELECT {BROKER_COLS}, 'new' FROM o.brokers")
        n = cur.rowcount
        store.conn.commit()
        store.conn.execute("DETACH DATABASE o")
    except Exception as e:
        log.warning("[пользователи] каталог маклеров не скопировался: %s", e)
        return 0
    store.set_kv("brokers_synced_at", now)
    return n


def refresh(cfg, store, sale, force=False, fetch=None):
    """Список арендаторов: владелец + активные пользователи с воркера. Без multi_user — только владелец."""
    owner = str(cfg["telegram_chat_id"])
    if REG["owner"] is None or REG["owner"].chat != owner or REG["owner"]._store is not store:
        REG["owner"] = Tenant(owner, cfg, owner=True, store=store, sale=sale)
        REG["t"] = {owner: REG["owner"]}
    REG["owner"]._sale = sale
    if not cfg.get("worker_url") and fetch is None:
        REG["multi"], REG["t"] = False, {owner: REG["owner"]}
        return list(REG["t"].values())
    if not force and time.time() - REG["at"] < REFRESH_EVERY:
        return list(REG["t"].values())
    REG["at"] = time.time()
    rr = _rr()
    users = (fetch or (lambda: rr.worker_call(cfg, "/svc/users", timeout=10)))()
    if users is None:                       # воркер не ответил — оставляем прежний список
        return list(REG["t"].values())
    REG["multi"] = bool(users.get("multi") or cfg.get("multi_user"))   # включает воркер (MULTI_USER=on)
    if not REG["multi"]:
        REG["t"] = {owner: REG["owner"]}
        return list(REG["t"].values())
    active = {str(u["chat"]): u for u in users.get("users") or [] if u.get("status") == "active"}
    keep = {owner: REG["owner"]}
    for chat, u in active.items():
        if chat == owner:
            continue
        keep[chat] = REG["t"].get(chat) or Tenant(chat, cfg, info=u)
        keep[chat].info = u
    REG["t"] = keep
    return list(keep.values())


def get(chat):
    return REG["t"].get(str(chat))


def all_tenants():
    return list(REG["t"].values())


def others():
    return [x for x in REG["t"].values() if not x.owner]


@contextmanager
def ctx(t):
    """Работаем от имени арендатора: его sale.db для обработчиков, язык, Telegram для бизнеса.
    Вложенный вызов (pump посреди разбора другого) возвращает прежнее как было."""
    rr = _rr()
    import concierge
    import i18n
    saved = (dict(rr.CUR), i18n.LANG, concierge._BIZ_STORE.get("store"))
    try:
        rr.CUR.update(sale=None if t.owner else t.sale_path, shared=None if t.owner else rr.SALE_DB_PATH)
        concierge._BIZ_STORE["store"] = t.store
        rr.sync_lang(t.store)
        yield t
    finally:
        rr.CUR.clear()
        rr.CUR.update(saved[0])
        i18n.LANG = saved[1]
        concierge._BIZ_STORE["store"] = saved[2]


def _biz_owner(conn_id):
    for t in REG["t"].values():
        try:
            if ((t.store.get_kv("biz_conn") or {}).get("id")) == conn_id:
                return t
        except Exception:
            continue
    return None


def route(upd) -> str:
    """Чей это апдейт: чат арендатора. Маклер — того клиента, чей запрос он получил (воркер кладёт _client);
    непонятно чей — владельца (как было до многих пользователей)."""
    owner = REG["owner"].chat if REG["owner"] else ""
    if not REG["multi"]:
        return owner
    bc = upd.get("business_connection")
    if bc:
        c = str(bc.get("user_chat_id") or "")
        return c if c in REG["t"] else owner
    bm = upd.get("business_message") or upd.get("edited_business_message") or upd.get("deleted_business_messages")
    if bm:
        t = _biz_owner(bm.get("business_connection_id"))
        return t.chat if t else owner
    cb = upd.get("callback_query")
    msg = ((cb or {}).get("message") if cb else None) or upd.get("message") or upd.get("edited_message") or {}
    chat = str((msg.get("chat") or {}).get("id") or "")
    if chat in REG["t"]:
        return chat
    client = str(msg.get("_client") or "")
    return client if client in REG["t"] else owner


# ------------------------------------------------------------- общий кэш скачанного --
_FETCH = {}
FETCH_TTL = 50


def shared_fetch(name, fn):
    """Одинаковый запрос к сайту от разных пользователей за минуту — один раз. Только для функций (scfg, cfg):
    результат не зависит от базы пользователя."""
    def wrap(scfg, cfg, *a, **k):
        if not REG["multi"] or a or k:
            return fn(scfg, cfg, *a, **k)
        key = (name, json.dumps(scfg, sort_keys=True, ensure_ascii=False, default=str))
        hit = _FETCH.get(key)
        if hit and time.time() - hit[0] < FETCH_TTL:
            return [dict(x) for x in hit[1]]
        out = fn(scfg, cfg)
        if len(_FETCH) > 200:
            _FETCH.clear()
        _FETCH[key] = (time.time(), [dict(x) for x in out])
        return out
    wrap.__name__ = getattr(fn, "__name__", name)
    wrap._shared = True
    return wrap

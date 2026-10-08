// Оффлайн-тесты воркера: node --experimental-sqlite worker/test/test.mjs
// D1 эмулируется на node:sqlite, Telegram / Gemini / GitHub — подменой fetch.
import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import worker, { applyPatch, essentialsOk, finalAns, handleUpdate, summary, pairsToSet, OWNER_KB, BROKER_KB, BTN } from "../src/index.js";

function d1() {
  const s = new DatabaseSync(":memory:");
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    run: async () => { s.prepare(sql).run(...args); return { success: true }; },
    all: async () => ({ results: s.prepare(sql).all(...args) }),
    first: async () => s.prepare(sql).get(...args) || null,
  });
  return { prepare: sql => stmt(sql), batch: async list => Promise.all(list.map(x => x.run())) };
}

const sent = [], gh = [];
let geminiQueue = [];
globalThis.fetch = async (url, opt = {}) => {
  url = String(url);
  const body = opt.body ? JSON.parse(opt.body) : {};
  if (url.includes("api.telegram.org")) {
    sent.push({ m: url.split("/").pop(), ...body });
    return new Response(JSON.stringify({ ok: true, result: {} }));
  }
  if (url.includes("generativelanguage")) {
    const next = geminiQueue.shift();
    if (!next) return new Response(JSON.stringify({ error: { message: "no mock" } }), { status: 500 });
    return new Response(JSON.stringify({ candidates: [{ content: { parts: [{ text: JSON.stringify(next) }] } }] }));
  }
  if (url.includes("api.github.com")) { gh.push(url); return new Response(null, { status: 204 }); }
  throw new Error("unexpected fetch " + url);
};

const OWNER = "308687648";
const env = { DB: d1(), OWNER_CHAT: OWNER, BOT_TOKEN: "T", GEMINI_KEY: "G", SVC_KEY: "svc", TG_SECRET: "sec",
  GH_TOKEN: "gh", GH_REPO: "shokhrukh-xp/rent-radar", GH_WORKFLOW: "radar.yml" };
let uid = 100;
const msg = (text, chat = OWNER, extra = {}) => ({ update_id: ++uid,
  message: { message_id: uid, chat: { id: +chat }, from: { id: +chat, language_code: "ru" }, text, ...extra } });
const texts = () => sent.filter(x => x.m === "sendMessage").map(x => x.text);
const svc = (path) => worker.fetch(new Request("https://w.example" + path, { headers: { "x-svc": "svc" } }), env, { waitUntil() {} });

// ── нормализация ответа модели
let a = applyPatch({}, { deal: "rent", rooms: ["3", "x"], districts: ["Мирабад", "yunusobod", "Марс"],
  budget: "1 400", class: "premium", floor_min: 99, movein_date: "2026-11-15", term: "bad" });
assert.deepEqual(a.rooms, ["3"]);
assert.deepEqual(a.districts, ["2", "8"]);                 // Мирабад, Юнусабад; мусор отброшен
assert.equal(a.budget, "1400"); assert.equal(a.class, "premium");
assert.equal(a.floor_min, undefined); assert.equal(a.term, undefined);
assert.equal(a.movein, "date");                             // дата заезда → movein=date
a = applyPatch(a, { districts: ["any"] }, ["class"]);
assert.deepEqual(a.districts, []); assert.equal(a.class, undefined);
assert.equal(essentialsOk({ deal: "rent", city: "tashkent", budget: "1" }), false);
assert.equal(essentialsOk({ deal: "rent", city: "tashkent", budget: "1", rooms: ["2"] }), true);
assert.equal(essentialsOk({ deal: "buy", object: "land", city: "region", budget: "9" }), true);
assert.equal(finalAns({ rooms_any: true }).rooms_any, undefined);
let f = finalAns({ deal: "rent", date_from: "2026-11-15", date_to: "2027-11-15", term: "12", movein: "date", movein_date: "2026-11-15" });
assert.equal(f.date_from, undefined); assert.equal(f.term, "12"); assert.equal(f.movein_date, "2026-11-15");
f = finalAns({ deal: "daily", term: "12", date_from: "2026-10-20", movein: "now" });
assert.equal(f.term, undefined); assert.equal(f.movein, undefined); assert.equal(f.date_from, "2026-10-20");
f = finalAns({ deal: "buy", who: "single", pets: "cat", city: "region", districts: ["2"] });
assert.equal(f.who, undefined); assert.deepEqual(f.districts, []);
assert.match(summary({ deal: "rent", object: "flat", rooms: ["3"], districts: ["2"], budget: "1400" }), /аренда · квартира · 3-комн\. · Мирабад · до \$1400\/мес/);

// ── список пар от модели → поля
assert.deepEqual(pairsToSet([{ k: "who", v: "couple" }, { k: "districts", v: "Мирабад, Юнусабад" }, { k: "rooms", v: "2,3" }, { v: "x" }]),
  { who: "couple", districts: ["Мирабад", "Юнусабад"], rooms: ["2", "3"] });
assert.deepEqual(applyPatch({}, pairsToSet([{ k: "districts", v: "Мирабад, Юнусабад" }])).districts, ["2", "8"]);

assert.equal(applyPatch({}, { note: "  нужна   ипотека " }).note, "нужна ипотека");

// ── webhook: без секрета — 403
let r = await worker.fetch(new Request("https://w.example/tg", { method: "POST", body: "{}" }), env, { waitUntil() {} });
assert.equal(r.status, 403);

// ── /start → приветствие и снятие старой клавиатуры
await handleUpdate(env, msg("/start"));
assert.match(texts().at(-1), /Ra'no, ИИ-ассистент/);
assert.deepEqual(sent.at(-1).reply_markup, OWNER_KB);              // постоянные кнопки внизу
assert.doesNotMatch(texts().at(-1), /\/new/);                       // команды учить не нужно

// ── ход 1: модель разобрала часть, спрашивает дальше — в очередь ничего
geminiQueue.push({ reply: "Отлично! Какой бюджет в месяц?", ready: false,
  set: { deal: "rent", object: "flat", city: "tashkent", rooms: ["3"], districts: ["Мирабад"] } });
await handleUpdate(env, msg("хочу снять трёшку в Мирабаде"));
assert.equal(texts().at(-1), "Отлично! Какой бюджет в месяц?");
let q = await (await svc("/svc/updates?after=0&until=0")).json();
assert.equal(q.result.length, 0);

// Python «уснул»: сбросим сердцебиение
await env.DB.prepare("DELETE FROM kv WHERE k='py_alive'").run();

// ── ход 2: модель говорит ready, но главного не хватает (нет бюджета) → не отправляем
geminiQueue.push({ reply: "Готово!", ready: true, set: {} });
await handleUpdate(env, msg("ну всё"));
q = await (await svc("/svc/updates?after=0")).json();
assert.equal(q.result.length, 0);
await env.DB.prepare("DELETE FROM kv WHERE k='py_alive'").run();

// ── ход 3: бюджет есть, ready → в очереди web_app_data с полным набором, Actions разбужен
geminiQueue.push({ reply: "Собрала: аренда, 3-комн., Мирабад, до $1400.", ready: true,
  set: { budget: 1400, class: "premium", pets: "cat" } });
await handleUpdate(env, msg("до 1400, ремонт дизайнерский, есть кошка"));
assert.match(texts().at(-1), /Собрала.*\n\n📝 Текст запроса пришлю на проверку через 1–2 минуты\./s);
assert.equal(gh.length, 1);
assert.match(gh[0], /actions\/workflows\/radar\.yml\/dispatches/);
q = await (await svc("/svc/updates?after=0&until=" + Math.floor(Date.now() / 1000 + 600))).json();
assert.equal(q.result.length, 1);
const wad = JSON.parse(q.result[0].message.web_app_data.data);
assert.equal(wad.replace, true);
assert.deepEqual(wad.ans.rooms, ["3"]); assert.equal(wad.ans.budget, "1400");
assert.equal(wad.ans.contact, "bot"); assert.equal(wad.ans.lang, "ru");
assert.equal(String(q.result[0].message.chat.id), OWNER);
const id1 = q.result[0].update_id;

// ── та же модель повторно ready без изменений → дубль не шлём
geminiQueue.push({ reply: "Всё так же.", ready: true, set: {} });
await handleUpdate(env, msg("ок"));
q = await (await svc(`/svc/updates?after=${id1}&until=${Math.floor(Date.now() / 1000 + 600)}`)).json();
assert.equal(q.result.length, 0);
assert.equal(texts().at(-1), "Всё так же.");

// ── правка словами → новый набор; Python жив → не будим, текст без «1–2 минуты»
geminiQueue.push({ reply: "Поменяла бюджет на $1200.", ready: true, set: { budget: 1200 } });
await handleUpdate(env, msg("бюджет 1200"));
assert.match(texts().at(-1), /Поменяла бюджет на \$1200\.\n\n📝 Сейчас пришлю текст запроса на проверку…$/);
assert.equal(gh.length, 1);
q = await (await svc(`/svc/updates?after=${id1}`)).json();
assert.equal(JSON.parse(q.result[0].message.web_app_data.data).ans.budget, "1200");
const id2 = q.result[0].update_id;

// ── маклер пишет → в очередь
await handleUpdate(env, msg("3 комн Мирабад 1300$", "555"));
q = await (await svc(`/svc/updates?after=${id2}`)).json();
assert.equal(q.result[0].message.chat.id, 555);
const id3 = q.result[0].update_id;

// ── кнопки: q:edit → следующий текст уходит Python, а не в ИИ; q:again → интервью заново
await handleUpdate(env, { update_id: ++uid, callback_query: { id: "c1", data: "q:edit",
  message: { message_id: 9, chat: { id: +OWNER } } } });
await handleUpdate(env, msg("Мой собственный текст запроса"));
q = await (await svc(`/svc/updates?after=${id3}`)).json();
assert.equal(q.result.length, 2);
assert.equal(q.result[0].callback_query.data, "q:edit");
assert.equal(q.result[1].message.text, "Мой собственный текст запроса");
const id4 = q.result[1].update_id;
await handleUpdate(env, { update_id: ++uid, callback_query: { id: "c2", data: "q:again",
  message: { message_id: 9, chat: { id: +OWNER } } } });
assert.match(texts().at(-1), /Как это работает/);
q = await (await svc(`/svc/updates?after=${id4}`)).json();
assert.equal(q.result.length, 0);

// ── прочие команды → в очередь; повтор того же update_id игнорируется
const u = msg("/shortlist");
await handleUpdate(env, u);
assert.equal(await handleUpdate(env, u), "dup");
q = await (await svc(`/svc/updates?after=${id4}`)).json();
assert.equal(q.result.length, 1);
assert.equal(q.result[0].message.text, "/shortlist");

// ── /start с параметрами старого мини-аппа уходит Python
await handleUpdate(env, msg("/start p1DBB"));
q = await (await svc(`/svc/updates?after=${q.result[0].update_id}`)).json();
assert.equal(q.result[0].message.text, "/start p1DBB");

// ── ошибка Gemini → вежливое сообщение, без падения
await handleUpdate(env, msg("ещё что-то"));
assert.match(texts().at(-1), /попробуйте ещё раз/);

// ── маклер: мгновенный ответ (не на /start), не чаще раза в 20 минут; пометка _acked для Python
let qq = await (await svc("/svc/updates?after=0")).json();
let lastId = qq.result.length ? qq.result.at(-1).update_id : 0;
sent.length = 0;
await handleUpdate(env, msg("/start", "4242"));
let to4242 = sent.filter(x => x.m === "sendMessage" && String(x.chat_id) === "4242");
assert.equal(to4242.length, 1); assert.match(to4242[0].text, /Пришлите подходящие варианты/);
assert.deepEqual(to4242[0].reply_markup, BROKER_KB);              // одна кнопка «Что ищет клиент»
await handleUpdate(env, msg(BTN.what, "4242"));                   // кнопка — ответ сразу, в очередь не идёт
await handleUpdate(env, msg("Продаю 2 комн Мирабад 44 000$", "4242"));
await handleUpdate(env, { update_id: ++uid, message: { message_id: uid, chat: { id: 4242 }, photo: [{ file_id: "x" }] } });
to4242 = sent.filter(x => x.m === "sendMessage" && String(x.chat_id) === "4242");
const acks = to4242.filter(x => /получила/.test(x.text));
assert.equal(to4242.length, 3); assert.equal(acks.length, 1);
qq = await (await svc(`/svc/updates?after=${lastId}`)).json();
assert.equal(qq.result.length, 3);
assert.equal(qq.result[0].message._welcomed, true);              // /start — Python не здоровается второй раз
assert.equal(qq.result[1].message._acked, true);
assert.equal(qq.result[2].message._acked, true);                 // второе — в окне 20 минут
lastId = qq.result.at(-1).update_id;

// ── владелец пересылает вариант / фото / режим /add → в очередь как _owner_offer, не в интервью
await handleUpdate(env, msg("2-комн Яккасарай 41 000$", OWNER, { forward_origin: { type: "user", sender_user: { first_name: "Б" } } }));
await handleUpdate(env, { update_id: ++uid, message: { message_id: uid, chat: { id: +OWNER }, from: { id: +OWNER }, photo: [{ file_id: "p" }] } });
await handleUpdate(env, msg("/add"));
assert.match(texts().at(-1), /Перешлите или вставьте сюда/);
await handleUpdate(env, msg("Вот ещё вариант от маклера: 3/9, 55 м², 47 000$"));
await handleUpdate(env, msg("/done"));
qq = await (await svc(`/svc/updates?after=${lastId}`)).json();
assert.equal(qq.result.length, 3);
assert.ok(qq.result.every(u => u.message._owner_offer === true));
lastId = qq.result.at(-1).update_id;
geminiQueue.push({ reply: "Поняла.", ready: false, set: [] });
await handleUpdate(env, msg("а бюджет можно до 50"));            // после /done — снова интервью
assert.equal(texts().at(-1), "Поняла.");
qq = await (await svc(`/svc/updates?after=${lastId}`)).json();
assert.equal(qq.result.length, 0);

// ── кнопки внизу и обычные фразы вместо команд
lastId = (await (await svc(`/svc/updates?after=${lastId}`)).json()).result.at(-1)?.update_id || lastId;
const drain = async () => { const r = await (await svc(`/svc/updates?after=${lastId}`)).json(); if (r.result.length) lastId = r.result.at(-1).update_id; return r.result; };
await drain();
await handleUpdate(env, msg(BTN.offers));
await handleUpdate(env, msg(BTN.brokers));
let got = await drain();
assert.deepEqual(got.map(u => u.message.text), ["/offers", "/brokers"]);
sent.length = 0;
await handleUpdate(env, msg(BTN.more));
assert.ok(JSON.stringify(sent.at(-1).reply_markup).includes("cmd:/add"));
await handleUpdate(env, msg(BTN.search));
assert.match(texts().at(-1), /Сейчас ищем|Как это работает/);
// «⋯ Ещё» → шортлист: колбэк превращается в команду для Python
await handleUpdate(env, { update_id: ++uid, callback_query: { id: "c9", data: "cmd:/shortlist", message: { message_id: 3, chat: { id: +OWNER } } } });
got = await drain();
assert.equal(got.at(-1).message.text, "/shortlist");
// обычной фразой: «что прислали маклеры?» → /offers; «давай заново» → новый поиск; «мне скинули квартиру» → режим добавления
geminiQueue.push({ reply: "Сейчас покажу.", ready: false, set: [], intent: "show_offers" });
await handleUpdate(env, msg("что там прислали маклеры?"));
got = await drain();
assert.equal(got.at(-1).message.text, "/offers");
geminiQueue.push({ reply: "Хорошо.", ready: false, set: [], intent: "restart" });
await handleUpdate(env, msg("давай начнём заново"));
assert.match(texts().at(-1), /Как это работает/);                 // без деталей — приветствие
geminiQueue.push({ reply: "Ищем аренду. В каком районе?", ready: false, set: [{ k: "deal", v: "rent" }], intent: "restart" });
await handleUpdate(env, msg("давай заново, теперь аренда"));
assert.match(texts().at(-1), /Начинаем новый поиск\.\n\nИщем аренду/);
const ivR = JSON.parse((await env.DB.prepare("SELECT v FROM kv WHERE k=?").bind("iv:" + OWNER).first()).v);
assert.deepEqual(ivR.ans, { deal: "rent" });                       // старое забыто, новое сохранено
geminiQueue.push({ reply: "Ок.", ready: false, set: [], intent: "add_offer" });
await handleUpdate(env, msg("мне в ватсапе скинули квартиру, добавь"));
assert.match(texts().at(-1), /Перешлите или вставьте сюда/);
await handleUpdate(env, msg("Скопированный текст: 2/5, 50 м², 43 000$"));
got = await drain();
assert.equal(got.at(-1).message._owner_offer, true);
await handleUpdate(env, { update_id: ++uid, callback_query: { id: "c10", data: "cmd:/done", message: { message_id: 4, chat: { id: +OWNER } } } });
assert.match(texts().at(-1), /Готово/);

// ── снимок от Python: «Варианты» и «Маклерам» отвечают сразу, без очереди
const post = (path, body) => worker.fetch(new Request("https://w.example" + path, { method: "POST",
  headers: { "x-svc": "svc", "content-type": "application/json" }, body: JSON.stringify(body) }), env, { waitUntil() {} });
await drain();
// снимок свежее последнего запроса: ставим sentAt в прошлое
{ const row = await env.DB.prepare("SELECT v FROM kv WHERE k=?").bind("iv:" + OWNER).first();
  const ivx = JSON.parse(row.v); ivx.sentAt = Date.now() - 60e3;
  await env.DB.prepare("UPDATE kv SET v=? WHERE k=?").bind(JSON.stringify(ivx), "iv:" + OWNER).run(); }
assert.equal((await post("/svc/snapshot", { offers: [
  { oid: 7, photos: ["ph7"], text: "🏠 <b>Вариант 1 из 3</b>" }, { oid: 8, photos: [], text: "🏠 Вариант 2 из 3" },
  { oid: 9, photos: [], text: "🏠 Вариант 3 из 3" }], offers_total: 3, shortlist: 1, written: 2, free: 2, deal: "sale",
  brokers: [{ bid: "tel:1", body: "📇 <b>А</b>", row: [{ text: "📱 WhatsApp с текстом", url: "https://wa.me/1" }] },
            { bid: "tel:2", body: "📇 <b>Б</b>", row: [{ text: "📱 WhatsApp с текстом", url: "https://wa.me/2" }] }],
  brokers_total: 2, header: "📇 <b>Рассылка</b>", brokers_empty: "" })).status, 200);
await env.DB.prepare("DELETE FROM kv WHERE k='py_alive'").run();       // Python спит
sent.length = 0;
await handleUpdate(env, msg(BTN.offers));
assert.equal(sent.filter(x => x.m === "sendMediaGroup").length, 1);       // вариант с фото — альбомом
assert.ok(sent.some(x => x.text === "🏠 Вариант 2 из 3" && x.reply_markup.inline_keyboard[0][2].callback_data === "t:n:8"));
assert.ok(sent.some(x => /ещё <b>1<\/b>/.test(x.text || "")));          // честная подводка к третьему
assert.equal((await drain()).length, 0);                                  // Python не понадобился
sent.length = 0;
await handleUpdate(env, { update_id: ++uid, callback_query: { id: "o2", data: "off2", message: { message_id: 5, chat: { id: +OWNER } } } });
assert.equal(sent.filter(x => x.m === "sendMessage" && /Вариант/.test(x.text || "")).length, 2);
// «Мимо» → причины сразу; выбор причины уходит Python
sent.length = 0;
await handleUpdate(env, { update_id: ++uid, callback_query: { id: "n1", data: "t:n:8", message: { message_id: 6, chat: { id: +OWNER } } } });
assert.ok(JSON.stringify(sent.at(-1).reply_markup).includes("t:r:8:p"));
await handleUpdate(env, { update_id: ++uid, callback_query: { id: "n2", data: "t:r:8:p", message: { message_id: 7, chat: { id: +OWNER } } } });
assert.equal((await drain()).at(-1).callback_query.data, "t:r:8:p");
// рассылка по одному: следующий маклер — сразу, статус уходит Python с пометкой _worker_done
sent.length = 0;
await handleUpdate(env, msg(BTN.brokers));
assert.match(texts().at(-2), /Рассылка/); assert.match(texts().at(-1), /📇 <b>А<\/b>[\s\S]*в очереди ещё 1/);
await handleUpdate(env, { update_id: ++uid, callback_query: { id: "w1", data: "bw:tel:1", message: { message_id: 8, chat: { id: +OWNER } } } });
assert.match(texts().at(-1), /📇 <b>Б<\/b>[\s\S]*Написано 1/);
assert.ok(sent.some(x => x.m === "editMessageReplyMarkup" && x.message_id === 8));
await handleUpdate(env, { update_id: ++uid, callback_query: { id: "w2", data: "bx:tel:2", message: { message_id: 9, chat: { id: +OWNER } } } });
assert.match(texts().at(-1), /Рассылка закончена: написано 1, пропущено 1/);
got = await drain();
assert.deepEqual(got.map(u => [u.callback_query.data, u.callback_query._worker_done]), [["bw:tel:1", true], ["bx:tel:2", true]]);
// запрос поменялся после снимка — к Python, чтобы ссылки не ушли со старым текстом
{ const row = await env.DB.prepare("SELECT v FROM kv WHERE k=?").bind("iv:" + OWNER).first();
  const ivx = JSON.parse(row.v); ivx.sentAt = Date.now() + 60e3;
  await env.DB.prepare("UPDATE kv SET v=? WHERE k=?").bind(JSON.stringify(ivx), "iv:" + OWNER).run(); }
await handleUpdate(env, msg(BTN.brokers));
assert.equal((await drain()).at(-1).message.text, "/brokers");

// ── /svc без ключа — 401
r = await worker.fetch(new Request("https://w.example/svc/updates"), env, { waitUntil() {} });
assert.equal(r.status, 401);

console.log("OK — воркер: интервью, нормализация, очередь, будильник, кнопки, дубли, ошибки");

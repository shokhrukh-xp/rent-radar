// Оффлайн-тесты воркера: node --experimental-sqlite worker/test/test.mjs
// D1 эмулируется на node:sqlite, Telegram / Gemini / GitHub — подменой fetch.
import { DatabaseSync } from "node:sqlite";
import assert from "node:assert/strict";
import worker, { applyPatch, essentialsOk, finalAns, handleUpdate, summary, pairsToSet } from "../src/index.js";

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

// ── webhook: без секрета — 403
let r = await worker.fetch(new Request("https://w.example/tg", { method: "POST", body: "{}" }), env, { waitUntil() {} });
assert.equal(r.status, 403);

// ── /start → приветствие и снятие старой клавиатуры
await handleUpdate(env, msg("/start"));
assert.match(texts().at(-1), /Ra'no, ИИ-ассистент/);
assert.deepEqual(sent.at(-1).reply_markup, { remove_keyboard: true });

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
assert.match(texts().at(-1), /Расскажите своими словами/);
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

// ── /svc без ключа — 401
r = await worker.fetch(new Request("https://w.example/svc/updates"), env, { waitUntil() {} });
assert.equal(r.status, 401);

console.log("OK — воркер: интервью, нормализация, очередь, будильник, кнопки, дубли, ошибки");

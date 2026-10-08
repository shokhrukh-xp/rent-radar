/* Ra'no — «лицо» бота на Cloudflare Worker.
 *
 * Зачем: Python-бот живёт на GitHub Actions и онлайн лишь ~50 минут из 4–7 часов.
 * Чату так нельзя. Поэтому Telegram шлёт ВСЕ обновления сюда (webhook):
 *   • владелец пишет текстом → ИИ-интервью (Gemini) отвечает сразу и собирает
 *     параметры поиска; когда их хватает — кладём их в очередь для Python
 *     (как раньше web_app_data из мини-аппа) и будим Actions;
 *   • всё остальное (маклеры, команды, кнопки) — в очередь D1 + будим Actions.
 * Python вместо getUpdates забирает очередь: GET /svc/updates.
 *
 * Секреты (wrangler secret put): BOT_TOKEN, GEMINI_KEY, TG_SECRET, SVC_KEY,
 *   GH_TOKEN (fine-grained, rent-radar: Actions read/write) — без него просто не будим.
 * Переменные (wrangler.toml): OWNER_CHAT, GH_REPO, GH_WORKFLOW, AI_MODEL.
 */

// ───────────────────────────── справочники ─────────────────────────────
export const DISTRICTS = ["Алмазар", "Бектемир", "Мирабад", "Мирзо-Улугбек", "Сергели", "Учтепа",
  "Чиланзар", "Шайхантахур", "Юнусабад", "Яккасарай", "Янгихаёт", "Яшнабад"];
const DISTRICTS_UZ = ["Olmazor", "Bektemir", "Mirobod", "Mirzo Ulug'bek", "Sergeli", "Uchtepa",
  "Chilonzor", "Shayxontohur", "Yunusobod", "Yakkasaroy", "Yangihayot", "Yashnobod"];
const E = {
  lang: ["ru", "uz", "en"], deal: ["rent", "daily", "buy"],
  object: ["flat", "house", "dacha", "land"], city: ["tashkent", "charvak", "region", "other"],
  class: ["any", "new", "premium", "reno", "biz"], furniture: ["yes", "no", "any"],
  term: ["12", "6_12", "3_6", "flex", "d1_3", "d4_7", "d7_30", "dflex"],
  movein: ["now", "month", "flex", "date"],
  who: ["single", "couple", "family_kids", "family", "big", "group"],
  pets: ["no", "cat", "dog", "pet_other"], parking: ["yes", "any"], contact: ["bot", "me", "both"],
};
const M = { rooms: ["1", "2", "3", "4", "any"], floor_pref: ["nf", "nl", "mid", "any"] };
const DATES = ["movein_date", "date_from", "date_to"];
const ALL_KEYS = [...Object.keys(E), ...Object.keys(M), "districts", "city_other", "note",
  "budget", "floor_min", "floor_max", ...DATES];
const START_CMDS = ["/start", "/new", "/app", "/mini", "/anketa", "/steps", "/search",
  "/start_search", "/profile", "/params"];

// ───────────────────────────── нормализация ответа модели ─────────────────────────────
const norm = s => String(s || "").toLowerCase().replace(/ё/g, "е").replace(/[^a-zа-я0-9]/g, "");
function districtIdx(name) {
  const n = norm(name);
  if (!n) return null;
  if (n === "any" || n === "любой" || n === "все") return "any";
  for (let i = 0; i < DISTRICTS.length; i++) {
    const a = norm(DISTRICTS[i]), b = norm(DISTRICTS_UZ[i]);
    if (n === a || n === b || (n.length >= 4 && (a.startsWith(n) || b.startsWith(n) || n.startsWith(a.slice(0, 5)))))
      return String(i);
  }
  return null;
}
const isoOk = s => /^\d{4}-\d{2}-\d{2}$/.test(String(s || "")) && !isNaN(Date.parse(s));

/** Применяет {set, clear} модели к ans с жёсткой проверкой значений. */
export function applyPatch(ans, set, clear) {
  const out = { ...ans };
  for (const k of (Array.isArray(clear) ? clear : [])) if (ALL_KEYS.includes(k)) delete out[k];
  for (const [k, v] of Object.entries(set || {})) {
    if (v === null || v === undefined || v === "") continue;
    if (E[k]) { const s = String(v); if (E[k].includes(s)) out[k] = s; continue; }
    if (M[k]) {
      const arr = (Array.isArray(v) ? v : [v]).map(String).filter(x => M[k].includes(x));
      out[k] = arr.includes("any") ? [] : [...new Set(arr)];
      continue;
    }
    if (k === "districts") {
      const arr = (Array.isArray(v) ? v : [v]).map(districtIdx).filter(Boolean);
      out.districts = arr.includes("any") ? [] : [...new Set(arr)].sort((a, b) => a - b);
      continue;
    }
    if (k === "budget") { const n = Math.round(+String(v).replace(/[^\d.]/g, "")); if (n > 0 && n < 1e8) out.budget = String(n); continue; }
    if (k === "floor_min" || k === "floor_max") { const n = parseInt(v, 10); if (n >= 1 && n <= 60) out[k] = String(n); continue; }
    if (DATES.includes(k)) { if (isoOk(v)) out[k] = String(v); continue; }
    if (k === "city_other") { out.city_other = String(v).trim().slice(0, 40); continue; }
    if (k === "note") { out.note = String(v).replace(/\s+/g, " ").trim().slice(0, 150); continue; }
  }
  const md = String((set || {}).movein_date || "").toLowerCase();
  if (["now", "month", "flex"].includes(md)) out.movein = md;
  if (out.movein_date && !out.movein) out.movein = "date";
  return out;
}

/** Хватает ли параметров, чтобы собрать запрос маклерам. */
export function essentialsOk(a) {
  return !!(a.deal && a.city && a.budget && (a.object === "land" || (a.rooms && a.rooms.length) || a.rooms_any));
}

/** Полный набор для Python: недостающее — значениями по умолчанию. */
export function finalAns(a) {
  const out = { lang: "ru", deal: "rent", object: "flat", city: "tashkent", contact: "bot", ...a };
  delete out.rooms_any;
  // поля, которые к этому типу сделки не относятся, не отдаём — иначе попадут в письмо
  if (out.deal !== "daily") { delete out.date_from; delete out.date_to; }
  if (out.deal !== "rent") { delete out.movein; delete out.movein_date; }
  if (out.deal === "rent" && /^d/.test(out.term || "")) delete out.term;
  if (out.deal === "daily" && out.term && !/^d/.test(out.term)) delete out.term;
  if (out.deal === "buy") { delete out.term; delete out.who; delete out.pets; delete out.furniture; }
  if (out.city !== "tashkent") out.districts = [];
  if (out.city !== "other") delete out.city_other;
  return out;
}

// ───────────────────────────── промпт и схема ─────────────────────────────
const SYSTEM = `Ты — Ra'no, ИИ-ассистент по подбору жилья в Узбекистане (в основном Ташкент).
Ты всегда ИИ-ассистент, никогда не выдаёшь себя за человека. В этом чате ты коротким
дружелюбным разговором выясняешь, что ищет клиент, и заполняешь параметры поиска.
По ним потом автоматически соберётся запрос маклерам.

Как вести разговор:
- Пиши коротко и тепло, без канцелярита: 1–3 предложения, не больше двух вопросов за раз.
- Отвечай на языке клиента (русский; узбекский — латиницей; английский) и ставь lang.
- Сразу забирай из сообщения всё, что можно. Не переспрашивай уже известное.
- Сначала главное: что ищем (аренда на длительный срок / посуточно / покупка), тип жилья,
  город и районы (для Ташкента), сколько комнат, бюджет в долларах.
- Затем ОДНИМ сообщением спроси про пожелания: ремонт/класс дома, мебель, этаж,
  сроки (аренда — на сколько и когда заезд; посуточно — даты заезда и выезда),
  кто будет жить, животные, парковка. «Неважно»/пропуск — не заполняй или ставь any.
- Бюджет: аренда — $ в месяц, посуточно — $ в сутки, покупка — $ за объект.
  Если назвали сумму в сумах — переведи по ~12 700 сум за $ и скажи, что перевела.
- «Трёшка» = 3 комнаты, «однушка» = 1. «Любой район» → districts ["any"].
  «Любое количество комнат» → rooms ["any"].
- Даты — в формате YYYY-MM-DD, считай от сегодняшней даты (она дана ниже).
- Длительная аренда: срок — ТОЛЬКО term («на год» → 12, «на полгода» → 6_12), дата заезда —
  movein="date" + movein_date. date_from/date_to для длительной аренды НЕ заполняй.
- Посуточно: date_from и date_to (заезд и выезд); если дат нет — term d1_3/d4_7/d7_30/dflex.
- Покупка: term, movein, who, pets, furniture не нужны — не спрашивай о них.
- Ничего не выдумывай, не обещай квартир и цен, не дави и не торопи.
- ready=true, когда известны deal, city, budget и (кроме участка) rooms, И ты уже спросил
  про пожелания (или клиент сам сказал, что остальное неважно / «ищи» / «хватит»).
  Тогда в reply одной-двумя строками перечисли собранное и скажи, что сейчас пришлёшь текст
  запроса на проверку. НЕ пиши, что запрос уже отправлен или передан маклерам: клиент сначала
  утверждает текст, а маклерам его отправляет сам, одним нажатием из карточек.
- Клиент может потом менять что угодно словами («бюджет 1200», «добавь Юнусабад»,
  «парковка не нужна»). Обнови поля и снова верни ready=true, если главное известно.
- Если сообщение не про жильё — ответь коротко и мягко верни к поиску.

Ответ — JSON: reply (текст клиенту), ready, set — СПИСОК пар {k, v} (поле и код значения)
по КАЖДОМУ факту из нового сообщения, clear — имена полей, которые клиент попросил сбросить.
ВАЖНО: сохраняется только set — reply лишь пересказывает. Ничего из сказанного не пропускай.
Пример: «пара, без животных, заезжаем сразу, на год, ремонт неважен» → set
[{k:"who",v:"couple"},{k:"pets",v:"no"},{k:"movein",v:"now"},{k:"term",v:"12"},{k:"class",v:"any"}].
Районы и комнаты — через запятую: {k:"districts",v:"Мирабад, Юнусабад"}, {k:"rooms",v:"2,3"}.
На любом языке клиента значения — коды из списка ниже (районы — по-русски).

Поля set:
lang: ru|uz|en
deal: rent (длительная аренда) | daily (посуточно) | buy (покупка)
object: flat (квартира) | house (дом/таунхаус) | dacha | land (участок)
city: tashkent | charvak (Чарвак/Чимган) | region (Ташкентская обл.) | other (тогда city_other — название)
districts: массив из ${DISTRICTS.join(", ")} или ["any"]
rooms: массив из "1","2","3","4" (4 = 4 и больше) или ["any"]
budget: целое число $ (верхняя граница)
class: any | new (новостройка/ЖК) | premium (ЖК + дизайнерский ремонт) | reno (вторичка с хорошим ремонтом) | biz (бизнес/премиум-класс)
furniture: yes | no | any
floor_pref: массив из nf (не первый), nl (не последний), mid (не первый и не последний) или ["any"]; floor_min, floor_max — числа
term (аренда): 12 (от года) | 6_12 | 3_6 | flex; (посуточно): d1_3 | d4_7 | d7_30 | dflex
movein (аренда): now | month | flex | date (+ movein_date)
date_from, date_to (посуточно)
who: single | couple | family_kids | family (без детей) | big (большая семья) | group (друзья/коллеги)
pets: no | cat | dog | pet_other
parking: yes (нужна) | any
contact: bot (маклеры пишут ассистенту — по умолчанию) | me (лично клиенту) | both
note: короткая фраза для маклеров о том, для чего нет своего поля: «нужна ипотека»,
  «рассрочка», «ближе к центру», «рядом со школой». Пиши её целиком заново (с прежним содержимым),
  на языке письма маклерам (русский, для lang=uz — узбекский).

intent — что клиент хочет сделать этим сообщением:
  search — описывает или меняет поиск (по умолчанию);
  restart — начать новый поиск с нуля («давай заново», «теперь ищу аренду, забудь прошлое»);
  show_offers — посмотреть, что прислали маклеры («что прислали?», «покажи варианты»);
  shortlist — отобранные варианты; brokers — разослать запрос / написать маклерам;
  add_offer — добавить вариант, который ему прислали в WhatsApp («мне скинули квартиру, добавь»);
  market — цены рынка; request_text — показать текст запроса; sale_search — что нашлось на Uybor;
  help — что ты умеешь / как пользоваться.
  Для всего, кроме search и restart, set оставь пустым, а reply — одной короткой фразой.

Честность: если ты ничего не записал в set — не пиши «учла», «обновила запрос».
«Центр» без названий районов — не выдумывай districts, а положи «ближе к центру» в note.`;

// Перечисления в схеме Gemini воспринимает как «заполнять только если уверен» и молча
// пропускает поля — поэтому в схеме просто строки, а допустимые значения проверяет applyPatch.
// Схема: set — СПИСОК пар {k, v}. С объектом из ~25 необязательных полей Gemini
// молча пропускает часть (проверено вживую: who/pets/term/movein терялись),
// а список фактов перечисляет полно. Значения проверяет applyPatch.
const KEYS = [...Object.keys(E), "districts", "rooms", "floor_pref", "budget", "floor_min", "floor_max",
  "movein_date", "date_from", "date_to", "city_other", "note"];
export const SCHEMA = {
  type: "object",
  properties: {
    reply: { type: "string" },
    ready: { type: "boolean" },
    set: {
      type: "array",
      description: "по одной паре на КАЖДЫЙ факт из нового сообщения клиента",
      items: {
        type: "object",
        properties: {
          k: { type: "string", description: KEYS.join(" | ") },
          v: { type: "string", description: "код значения; для districts/rooms/floor_pref — через запятую" },
        },
        required: ["k", "v"],
      },
    },
    clear: { type: "array", items: { type: "string" } },
    intent: { type: "string", description: "search | restart | show_offers | shortlist | brokers | add_offer | market | help | request_text | sale_search" },
  },
  required: ["reply", "ready", "set"],
};
const MULTI = ["districts", "rooms", "floor_pref"];
/** set модели (список пар или объект) → объект полей. */
export function pairsToSet(set) {
  if (!Array.isArray(set)) return set && typeof set === "object" ? set : {};
  const o = {};
  for (const p of set) {
    if (!p || !p.k) continue;
    const k = String(p.k).trim(), v = p.v;
    o[k] = MULTI.includes(k) ? (Array.isArray(v) ? v : String(v ?? "").split(/\s*[,;]\s*/).filter(Boolean)) : v;
  }
  return o;
}

// ───────────────────────────── Gemini ─────────────────────────────
export async function gemini(env, system, userText) {
  if (!env.GEMINI_KEY) throw new Error("GEMINI_KEY не задан");
  const first = env.AI_MODEL || "gemini-3.8-flash";
  const models = [first, ...["gemini-3.6-flash", "gemini-3.5-flash"].filter(m => m !== first)];
  const variants = [
    { responseMimeType: "application/json", responseJsonSchema: SCHEMA },
    { responseMimeType: "application/json", responseSchema: SCHEMA },
    { responseMimeType: "application/json" },
  ];
  let lastErr = "";
  for (const model of models) {
    for (let i = 0; i < variants.length; i++) {
      const sys = system + (i === 2 ? "\n\nФормат ответа — строго JSON по схеме: " + JSON.stringify(SCHEMA) : "");
      const gen = { temperature: 0.4, maxOutputTokens: 2048, ...variants[i] };
      let r, d;
      for (const g of [{ ...gen, thinkingConfig: { thinkingLevel: "low" } }, gen]) {
        r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
          method: "POST",
          headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_KEY },
          body: JSON.stringify({ systemInstruction: { parts: [{ text: sys }] },
            contents: [{ role: "user", parts: [{ text: userText }] }], generationConfig: g }),
        });
        d = await r.json().catch(() => ({}));
        if (r.ok || !(r.status === 400 && /think/i.test(d?.error?.message || ""))) break;
      }
      if (r.ok) {
        const raw = (d.candidates?.[0]?.content?.parts || []).filter(p => !p.thought).map(p => p.text || "").join("")
          .trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
        try { const o = JSON.parse(raw); if (o && typeof o === "object") Object.defineProperty(o, "_via", { value: model + "#" + i }); return o; } catch (e) {
          const a = raw.indexOf("{"), z = raw.lastIndexOf("}");
          if (a >= 0 && z > a) try { return JSON.parse(raw.slice(a, z + 1)); } catch (e2) {}
          lastErr = "не JSON"; continue;
        }
      }
      lastErr = `${model} ${r.status}: ${d?.error?.message || ""}`;
      if ([429, 503, 404, 500].includes(r.status)) break;   // следующая модель
      if (r.status !== 400) break;                          // 400 — пробуем другой формат схемы
    }
  }
  throw new Error(lastErr || "Gemini недоступен");
}

// ───────────────────────────── хранилище (D1) ─────────────────────────────
let schemaReady = false;
async function db(env) {
  if (!schemaReady) {
    await env.DB.batch([
      env.DB.prepare("CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, upd TEXT NOT NULL, at INTEGER NOT NULL)"),
      env.DB.prepare("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)"),
    ]);
    schemaReady = true;
  }
  return env.DB;
}
async function kvGet(env, k, dflt = null) {
  const r = await (await db(env)).prepare("SELECT v FROM kv WHERE k=?").bind(k).first();
  if (!r) return dflt;
  try { return JSON.parse(r.v); } catch (e) { return dflt; }
}
async function kvSet(env, k, v) {
  await (await db(env)).prepare("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v")
    .bind(k, JSON.stringify(v)).run();
}
async function enqueue(env, upd) {
  await (await db(env)).prepare("INSERT INTO queue (upd, at) VALUES (?, ?)").bind(JSON.stringify(upd), Date.now()).run();
}

// ───────────────────────────── Telegram ─────────────────────────────
// Токен бота: секрет BOT_TOKEN, а если его нет — переданный из GitHub Secrets
// разовым workflow (.github/workflows/worker-token.yml → POST /svc/bot-token).
let tokenCache = null;
async function botToken(env) {
  if (env.BOT_TOKEN) return env.BOT_TOKEN;
  if (!tokenCache) tokenCache = await kvGet(env, "bot_token", null);
  return tokenCache;
}
export async function tg(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${await botToken(env)}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  return r.json().catch(() => ({ ok: false }));
}
const say = (env, chat, text, extra = {}) => tg(env, "sendMessage", { chat_id: chat, text, ...extra });

// ── Управление без команд: постоянные кнопки внизу + обычные фразы ──
export const BTN = { search: "🔎 Мой поиск", offers: "🏠 Варианты", brokers: "📇 Маклерам", more: "⋯ Ещё",
  what: "🏠 Что ищет клиент" };
export const OWNER_KB = { keyboard: [[{ text: BTN.search }, { text: BTN.offers }], [{ text: BTN.brokers }, { text: BTN.more }]],
  resize_keyboard: true, is_persistent: true,
  input_field_placeholder: "Напишите, что ищете, или перешлите вариант" };
export const BROKER_KB = { keyboard: [[{ text: BTN.what }]], resize_keyboard: true, is_persistent: true,
  input_field_placeholder: "Пришлите вариант: фото, адрес, этаж, цена" };
const MORE_MENU = { inline_keyboard: [
  [{ text: "📥 Добавить вариант из WhatsApp", callback_data: "cmd:/add" }],
  [{ text: "📋 Шортлист", callback_data: "cmd:/shortlist" }, { text: "📊 Цены рынка", callback_data: "cmd:/rynok" }],
  [{ text: "🏷 Поиск на Uybor", callback_data: "cmd:/sale" }, { text: "📝 Текст запроса", callback_data: "cmd:/request" }],
  [{ text: "🔄 Начать поиск заново", callback_data: "q:again" }],
  [{ text: "❓ Как это работает", callback_data: "cmd:/help" }],
] };
// намерения из обычных фраз → что делает Python
const INTENT_CMD = { show_offers: "/offers", shortlist: "/shortlist", brokers: "/brokers",
  market: "/rynok", help: "/help", request_text: "/request", sale_search: "/sale" };

async function asCommand(env, chat, cmd, L = "ru") {
  // синтетическое сообщение-команда от владельца — Python обработает как набранную
  await queueAndWake(env, { message: { message_id: 0, chat: { id: +chat || chat, type: "private" },
    from: { id: +chat || chat }, date: Math.floor(Date.now() / 1000), text: cmd } }, chat, WAIT[L]);
}

async function showMySearch(env, chat, L) {
  const iv = await kvGet(env, ivKey(chat), null);
  if (!iv || !essentialsOk(iv.ans || {})) return startInterview(env, chat, false, L);
  const note = iv.ans.note ? `\nПожелания: ${iv.ans.note}` : "";
  await say(env, chat, `🔎 Сейчас ищем: ${summary(iv.ans)}.${note}\n\n` +
    "Чтобы что-то поменять — просто напишите, например: «бюджет 60 тысяч» или «добавь Юнусабад».", {
    reply_markup: { inline_keyboard: [
      [{ text: "📇 Разослать маклерам", callback_data: "cmd:/brokers" }, { text: "📝 Текст запроса", callback_data: "cmd:/request" }],
      [{ text: "🔄 Начать поиск заново", callback_data: "q:again" }]] } });
}

async function startAddMode(env, chat) {
  const iv = (await kvGet(env, ivKey(chat), null)) || emptyIv();
  iv.mode = "add"; iv.addAt = Date.now();
  await kvSet(env, ivKey(chat), iv);
  await say(env, chat, "📥 Перешлите или вставьте вариант от маклера — текст и фото, можно несколькими " +
    "сообщениями. Пересланное я и так узнаю; этот режим — для скопированного текста. Выключится сам через 15 минут.",
    { reply_markup: { inline_keyboard: [[{ text: "✅ Готово", callback_data: "cmd:/done" }]] } });
}

// ───────────────────────────── «будильник» для Python ─────────────────────────────
async function pythonAlive(env) {
  const hb = await kvGet(env, "py_alive", null);
  return !!(hb && Date.now() < hb.until && Date.now() - hb.at < 15 * 60e3);
}
/** Будит GitHub Actions, если Python сейчас не работает. Возвращает true, если он уже жив. */
export async function wake(env) {
  if (await pythonAlive(env)) return true;
  const last = await kvGet(env, "last_wake", 0);
  if (Date.now() - last < 4 * 60e3) return false;     // уже будили — запуск в пути
  await kvSet(env, "last_wake", Date.now());
  if (!env.GH_TOKEN || !env.GH_REPO) return false;
  const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW || "radar.yml"}/dispatches`, {
    method: "POST",
    headers: { authorization: `Bearer ${env.GH_TOKEN}`, accept: "application/vnd.github+json",
      "user-agent": "rano-worker", "x-github-api-version": "2022-11-28" },
    body: JSON.stringify({ ref: "main" }),
  });
  const st = { at: Date.now(), status: r.status, body: r.status === 204 ? "" : (await r.text()).slice(0, 300) };
  await kvSet(env, "last_wake_status", st);
  if (r.status !== 204) console.log("wake failed", st.status, st.body);
  return false;
}
async function queueAndWake(env, upd, chat, note) {
  await enqueue(env, upd);
  const alive = await wake(env);
  if (!alive && chat && note) {
    const w = await kvGet(env, "wait_note", 0);              // не чаще раза в 5 минут
    if (Date.now() - w > 5 * 60e3) { await kvSet(env, "wait_note", Date.now()); await say(env, chat, note); }
  }
  return alive;
}

// ───────────────────────────── интервью ─────────────────────────────
const GREET = {
  ru: "Привет! Я Ra'no, ИИ-ассистент по поиску жилья.\n\n" +
    "Как это работает:\n1. Вы своими словами говорите, что ищете — я уточню детали.\n" +
    "2. Я составлю запрос, а вы в пару нажатий отправите его маклерам.\n" +
    "3. Варианты маклеров приходят сюда карточками — с анализом цены.\n\n" +
    "Начнём? Напишите, например: «купить двушку в центре до $50 000, с ремонтом». " +
    "Всё остальное — кнопками внизу, команды запоминать не нужно.",
  uz: "Salom! Men Ra'no, uy-joy qidirish bo'yicha AI-yordamchiman. Nima qidirayotganingizni o'z so'zlaringiz bilan yozing — " +
    "masalan: «markazda 2 xonali, $50 000 gacha, remont bilan». Qolgani — pastdagi tugmalar orqali.",
  en: "Hi! I'm Ra'no, an AI assistant for finding a home. Tell me in your own words what you're looking for — " +
    "e.g. \"buy a 2-room flat in the centre up to $50,000, renovated\". Everything else is in the buttons below.",
};
const WAIT = {
  ru: "⏳ Запускаю основной модуль — ответ придёт через 1–2 минуты.",
  uz: "⏳ Asosiy modulni ishga tushiryapman — javob 1–2 daqiqada keladi.",
  en: "⏳ Starting the main module — the reply will come in 1–2 minutes.",
};
const ivKey = chat => "iv:" + chat;

// Маклеру — ответ сразу (раньше ждал, пока проснётся Python). Не на /start и не на «здравствуйте»:
// там Python присылает знакомство с запросом клиента. Не чаще раза в 20 минут на маклера.
export const BROKER_ACK = "Здравствуйте! Я Ra'no, ИИ-ассистент — веду поиск жилья для клиента и передаю ему варианты.\n" +
  "Спасибо, получила! Если подойдёт, вернусь с уточнениями. Присылайте ещё, что есть по параметрам.\n\n" +
  "Assalomu alaykum! Men Ra'no, AI-yordamchiman — mijoz uchun uy-joy qidiryapman. Rahmat, qabul qilindi! " +
  "Mos kelsa, aniqlik kiritish uchun yozaman. Parametrlarga mos variantlar bo'lsa, yuboravering.";
/** Маклеру, который пришёл по ссылке / поздоровался / нажал «Что ищет клиент» — знакомство и суть запроса. */
async function brokerWelcome(env, msg, owner) {
  const text = String(msg.text || msg.caption || "").trim();
  const asks = text === BTN.what;
  const greeting = !msg.photo && (text.startsWith("/") || (text.length < 25 && !/\d/.test(text)));
  if (!asks && !greeting) return false;
  const k = "welcome:" + msg.chat.id;
  if (!asks && !text.startsWith("/") && Date.now() - (await kvGet(env, k, 0)) < 6 * 3600e3) return true;
  await kvSet(env, k, Date.now());
  const iv = await kvGet(env, ivKey(owner), null);
  const want = iv && essentialsOk(iv.ans || {}) ? summary(iv.ans) + (iv.ans.note ? `; ${iv.ans.note}` : "") : "";
  await tg(env, "sendMessage", { chat_id: msg.chat.id, reply_markup: BROKER_KB, text:
    "Здравствуйте! Я Ra'no, ИИ-ассистент — веду поиск жилья для клиента.\n" +
    (want ? `\nКлиент ищет: ${want}.\n` : "") +
    "\nПришлите подходящие варианты: фото, точный адрес, этаж, площадь, цену и комиссию — " +
    "одним сообщением или по частям. Я сразу передам клиенту.\n\n" +
    "Assalomu alaykum! Men Ra'no, AI-yordamchiman. Mos variantlarni yuboring: foto, manzil, qavat, maydon, narx va vositachilik haqi." });
  return true;
}

async function brokerAck(env, msg) {
  const text = String(msg.text || msg.caption || "").trim();
  const looksOffer = !!msg.photo || text.length >= 25 || /\d/.test(text);
  if (text.startsWith("/") || !looksOffer) return false;
  const k = "ack:" + msg.chat.id;
  if (Date.now() - (await kvGet(env, k, 0)) < 20 * 60e3) { msg._acked = true; return false; }
  await kvSet(env, k, Date.now());
  const r = await tg(env, "sendMessage", { chat_id: msg.chat.id, text: BROKER_ACK });
  if (r && r.ok !== false) msg._acked = true;
  return true;
}
const emptyIv = () => ({ ans: {}, hist: [], sent: "", mode: "" });

export function summary(a) {
  const deal = { rent: "аренда", daily: "посуточно", buy: "покупка" }[a.deal] || "";
  const obj = { flat: "квартира", house: "дом", dacha: "дача", land: "участок" }[a.object] || "";
  const rooms = (a.rooms || []).length ? a.rooms.map(r => r === "4" ? "4+" : r).join("/") + "-комн." : "";
  const ds = (a.districts || []).map(i => DISTRICTS[+i]).filter(Boolean).join(", ");
  const b = a.budget ? `до $${a.budget}${a.deal === "daily" ? "/сутки" : a.deal === "buy" ? "" : "/мес"}` : "";
  return [deal, obj, rooms, ds || (a.city === "tashkent" ? "Ташкент" : a.city_other || ""), b].filter(Boolean).join(" · ");
}

function buildPrompt(iv, text, today) {
  const hist = iv.hist.slice(-16).map(h => (h.r === "u" ? "Клиент: " : "Ra'no: ") + h.t).join("\n");
  return `Сегодня: ${today}.\nТекущие параметры (JSON): ${JSON.stringify(iv.ans)}\n` +
    (hist ? `История диалога:\n${hist}\n` : "") + `Новое сообщение клиента: ${text}`;
}

/** Один ход интервью без отправки в Telegram: модель → проверка → новое состояние. */
export async function interviewCore(env, iv, text) {
  const out = await gemini(env, SYSTEM, buildPrompt(iv, text, new Date().toISOString().slice(0, 10)));
  const set = pairsToSet(out.set);
  const roomsAny = Array.isArray(set.rooms) && set.rooms.includes("any");
  iv.ans = applyPatch(iv.ans, set, out.clear);
  if (roomsAny) iv.ans.rooms_any = true; else if (set.rooms) delete iv.ans.rooms_any;
  const reply = String(out.reply || "").trim().slice(0, 3500) || "Расскажите, пожалуйста, что ищете?";
  iv.hist.push({ r: "u", t: String(text).slice(0, 1000) }, { r: "a", t: reply });
  iv.hist = iv.hist.slice(-20);
  const fin = finalAns(iv.ans);
  const sig = JSON.stringify(fin);
  let ready = false;
  if (out.ready && essentialsOk(iv.ans) && sig !== iv.sent) { iv.sent = sig; ready = true; }
  return { reply, ready, fin, raw: out, via: out._via };
}

export async function interviewTurn(env, chat, text) {
  const iv = await kvGet(env, ivKey(chat), null) || emptyIv();
  tg(env, "sendChatAction", { chat_id: chat, action: "typing" }).catch(() => {});
  let res;
  try {
    res = await interviewCore(env, iv, text);
  } catch (e) {
    console.log("gemini error", e.message);
    await say(env, chat, "Не получилось обработать сообщение — попробуйте ещё раз через минуту.");
    return;
  }
  let { reply, ready, fin } = res;
  const intent = String(res.raw?.intent || "search");
  if (intent === "restart") {
    // «давай заново, теперь аренда» — начинаем с чистого листа, но сказанное сейчас не теряем
    const fresh = emptyIv();
    fresh.ans = applyPatch({}, pairsToSet(res.raw?.set));
    if (!Object.keys(fresh.ans).length) { await kvSet(env, ivKey(chat), fresh); return startInterview(env, chat, true, fin.lang || "ru"); }
    fresh.hist = [{ r: "u", t: String(text).slice(0, 1000) }, { r: "a", t: reply }];
    await kvSet(env, ivKey(chat), fresh);
    await say(env, chat, "🔄 Начинаем новый поиск.\n\n" + reply, { reply_markup: OWNER_KB });
    return;
  }
  if (intent === "add_offer") return startAddMode(env, chat);
  if (INTENT_CMD[intent]) {
    await kvSet(env, ivKey(chat), iv);
    await asCommand(env, chat, INTENT_CMD[intent], fin.lang === "uz" ? "uz" : "ru");
    return;
  }
  await kvSet(env, ivKey(chat), iv);
  if (ready) {
    await enqueue(env, { message: { chat: { id: +chat || chat, type: "private" }, from: { id: +chat || chat },
      date: Math.floor(Date.now() / 1000),
      web_app_data: { data: JSON.stringify({ v: 3, replace: true, src: "chat", ans: fin }) } } });
    const alive = await wake(env);
    const lang = fin.lang || "ru";
    reply += "\n\n" + (alive
      ? { ru: "📝 Сейчас пришлю текст запроса на проверку…", uz: "📝 So'rov matnini tekshirish uchun hozir yuboraman…", en: "📝 Sending the request text for your review…" }[lang]
      : { ru: "📝 Текст запроса пришлю на проверку через 1–2 минуты.", uz: "📝 So'rov matnini 1–2 daqiqada tekshirish uchun yuboraman.", en: "📝 The request text will come for your review in 1–2 minutes." }[lang]);
  }
  await say(env, chat, reply, { reply_markup: OWNER_KB });
}

async function startInterview(env, chat, fresh, lang = "ru") {
  let iv = await kvGet(env, ivKey(chat), null);
  if (fresh || !iv || !essentialsOk(iv.ans || {})) {
    iv = emptyIv();
    await kvSet(env, ivKey(chat), iv);
    await say(env, chat, GREET[lang] || GREET.ru, { reply_markup: OWNER_KB });
    return;
  }
  await say(env, chat, `С возвращением! Сейчас ищем: ${summary(iv.ans)}.\n` +
    "Напишите, что поменять, — или нажмите «🔎 Мой поиск» → «Начать заново».", { reply_markup: OWNER_KB });
}

// ───────────────────────────── разбор обновления ─────────────────────────────
export async function handleUpdate(env, upd) {
  const owner = String(env.OWNER_CHAT);
  const last = await kvGet(env, "last_upd", 0);
  if (upd.update_id && upd.update_id <= last) return "dup";        // повтор от Telegram
  if (upd.update_id) await kvSet(env, "last_upd", upd.update_id);

  const cb = upd.callback_query;
  if (cb) {
    const chat = String(cb.message?.chat?.id ?? "");
    if (chat !== owner) return "skip";
    const data = cb.data || "";
    if (data.startsWith("cmd:")) {               // кнопки меню «⋯ Ещё» и «Мой поиск»
      const cmd = data.slice(4);
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
      if (cmd === "/add") return (await startAddMode(env, chat), "add_mode");
      if (cmd === "/done") {
        const iv = (await kvGet(env, ivKey(chat), null)) || emptyIv();
        iv.mode = ""; await kvSet(env, ivKey(chat), iv);
        await say(env, chat, "✅ Готово. Варианты появятся карточками.", { reply_markup: OWNER_KB });
        return "add_done";
      }
      await asCommand(env, chat, cmd);
      return "queued";
    }
    if (data === "q:again") {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Начинаем заново" });
      await startInterview(env, chat, true, (await kvGet(env, ivKey(chat), {}))?.ans?.lang);
      return "interview";
    }
    if (data === "q:edit") {
      const iv = await kvGet(env, ivKey(chat), null) || emptyIv();
      iv.mode = "await_text"; await kvSet(env, ivKey(chat), iv);
    }
    const alive = await pythonAlive(env);
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id,
      text: alive ? "" : "Принято — выполню через 1–2 минуты" });
    await queueAndWake(env, upd);
    return "queued";
  }

  const msg = upd.message || upd.edited_message;
  if (!msg) { await enqueue(env, upd); return "queued"; }
  const chat = String(msg.chat?.id ?? "");
  if (chat !== owner) {                         // маклер
    if (await brokerWelcome(env, msg, owner)) {
      if (String(msg.text || "").trim() === BTN.what) return "broker_info";   // кнопка — Python не нужен
      msg._welcomed = true;
    } else {
      await brokerAck(env, msg);                // ответ на вариант — сразу, даже если Python спит
    }
    await queueAndWake(env, upd);
    return "queued";
  }
  const text = (msg.text || "").trim();
  const lang = (msg.from?.language_code || "").slice(0, 2);
  const L = lang === "uz" ? "uz" : "ru";     // язык интерфейса Telegram часто английский — это не язык клиента

  // кнопки внизу — обычный текст с подписью кнопки
  if (text === BTN.search) return (await showMySearch(env, chat, L), "my_search");
  if (text === BTN.offers) return (await asCommand(env, chat, "/offers", L), "queued");
  if (text === BTN.brokers) return (await asCommand(env, chat, "/brokers", L), "queued");
  if (text === BTN.more) {
    await say(env, chat, "Что ещё могу:", { reply_markup: MORE_MENU });
    return "more";
  }

  // вариант, который владелец пересылает или вставляет (из WhatsApp и других чатов)
  const forwarded = !!(msg.forward_origin || msg.forward_from || msg.forward_sender_name || msg.forward_from_chat);
  const ivNow = await kvGet(env, ivKey(chat), null);
  const adding = ivNow && ivNow.mode === "add" && Date.now() - (ivNow.addAt || 0) < 15 * 60e3;
  if (!text.startsWith("/") && (forwarded || msg.photo || msg.document || adding)) {
    if (adding) { ivNow.addAt = Date.now(); await kvSet(env, ivKey(chat), ivNow); }
    upd.message = { ...msg, _owner_offer: true };
    await queueAndWake(env, upd, chat, WAIT[L]);
    return "owner_offer";
  }

  if (text.startsWith("/")) {
    const [c0, ...rest] = text.split(/\s+/);
    const cmd = c0.toLowerCase().split("@")[0];
    const arg = rest.join(" ");
    if (cmd === "/add") return (await startAddMode(env, chat), "add_mode");
    if (cmd === "/done") {
      const iv = ivNow || emptyIv();
      iv.mode = ""; await kvSet(env, ivKey(chat), iv);
      await say(env, chat, "✅ Готово. Пишите, если что-то поменять в поиске.", { reply_markup: OWNER_KB });
      return "add_mode";
    }
    if (START_CMDS.includes(cmd) && !(cmd === "/start" && /^p/.test(arg))) {
      await startInterview(env, chat, cmd !== "/start" && cmd !== "/params", L);
      return "interview";
    }
    await queueAndWake(env, upd, chat, WAIT[L]);
    return "queued";
  }
  if (!text || msg.web_app_data || upd.edited_message) {
    await queueAndWake(env, upd);
    return "queued";
  }
  const iv = await kvGet(env, ivKey(chat), null);
  if (iv && iv.mode === "await_text") {          // «✏️ Изменить текст» — это для Python
    iv.mode = ""; await kvSet(env, ivKey(chat), iv);
    await queueAndWake(env, upd, chat, WAIT[L]);
    return "queued";
  }
  await interviewTurn(env, chat, text);
  return "interview";
}

// ───────────────────────────── HTTP ─────────────────────────────
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json" } });

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const p = url.pathname;
    if (p === "/tg" && req.method === "POST") {
      if (env.TG_SECRET && req.headers.get("x-telegram-bot-api-secret-token") !== env.TG_SECRET)
        return new Response("forbidden", { status: 403 });
      const upd = await req.json().catch(() => null);
      if (upd) ctx.waitUntil(handleUpdate(env, upd).catch(e => console.log("update error", e.stack || e)));
      return new Response("ok");
    }
    if (p.startsWith("/svc/")) {
      if (!env.SVC_KEY || req.headers.get("x-svc") !== env.SVC_KEY) return json({ error: "нет ключа" }, 401);
      if (p === "/svc/updates") {
        const after = parseInt(url.searchParams.get("after") || "0", 10) || 0;
        const until = parseInt(url.searchParams.get("until") || "0", 10) || 0;
        await kvSet(env, "py_alive", { at: Date.now(), until: until ? until * 1000 : Date.now() + 120e3 });
        const d = await db(env);
        if (after) await d.prepare("DELETE FROM queue WHERE id<=?").bind(after).run();
        const r = await d.prepare("SELECT id, upd FROM queue WHERE id>? ORDER BY id LIMIT 100").bind(after).all();
        const updates = (r.results || []).map(x => ({ ...JSON.parse(x.upd), update_id: x.id }));
        return json({ ok: true, result: updates });
      }
      if (p === "/svc/bot-token" && req.method === "POST") {
        const b = await req.json().catch(() => ({}));
        const t = String(b.token || "").trim();
        const me = await (await fetch(`https://api.telegram.org/bot${t}/getMe`)).json().catch(() => ({}));
        if (!me.ok) return json({ ok: false, error: "токен не принят Telegram" }, 400);
        await kvSet(env, "bot_token", t); tokenCache = t;
        return json({ ok: true, username: me.result.username });
      }
      if (p === "/svc/wake") {           // проверка «будильника» вручную
        await kvSet(env, "last_wake", 0);
        const alive = await wake(env);
        return json({ alive, last_wake: await kvGet(env, "last_wake", 0), last_wake_status: await kvGet(env, "last_wake_status", null) });
      }
      if (p === "/svc/bye") { await kvSet(env, "py_alive", null); return json({ ok: true }); }
      if (p === "/svc/setup") {
        const hook = await tg(env, "setWebhook", { url: url.origin + "/tg", secret_token: env.TG_SECRET || undefined,
          allowed_updates: ["message", "edited_message", "callback_query"], drop_pending_updates: false });
        // кнопка меню «Параметры» (мини-апп) больше не нужна — возвращаем список команд
        const menu = await tg(env, "setChatMenuButton", { menu_button: { type: "commands" } });
        const menuOwner = await tg(env, "setChatMenuButton", { chat_id: +env.OWNER_CHAT, menu_button: { type: "commands" } });
        const ownerCmds = [
          { command: "new", description: "🔎 Новый поиск" }, { command: "offers", description: "🏠 Варианты от маклеров" },
          { command: "brokers", description: "📇 Разослать маклерам" }, { command: "add", description: "📥 Добавить вариант из WhatsApp" },
          { command: "shortlist", description: "📋 Шортлист" }, { command: "help", description: "❓ Как это работает" }];
        const cmds = await tg(env, "setMyCommands", { commands: ownerCmds, scope: { type: "chat", chat_id: +env.OWNER_CHAT } });
        const cmdsAll = await tg(env, "setMyCommands", { commands: [{ command: "start", description: "Как прислать вариант" }] });
        await tg(env, "sendMessage", { chat_id: +env.OWNER_CHAT, reply_markup: OWNER_KB,
          text: "Кнопки — внизу: «🔎 Мой поиск», «🏠 Варианты», «📇 Маклерам», «⋯ Ещё». " +
                "Команды запоминать не нужно — можно и просто написать, что хотите сделать." });
        const info = await tg(env, "getWebhookInfo", {});
        return json({ hook, menu, menuOwner, cmds, cmdsAll, info: info.result });
      }
      if (p === "/svc/try") {            // проверка промпта вживую, без Telegram и очереди
        if (url.searchParams.get("reset")) await kvSet(env, "iv:test", emptyIv());
        const iv = await kvGet(env, "iv:test", null) || emptyIv();
        const res = await interviewCore(env, iv, url.searchParams.get("text") || "");
        await kvSet(env, "iv:test", iv);
        return json({ ...res, ans: iv.ans });
      }
      if (p === "/svc/state") {
        return json({ alive: await pythonAlive(env), iv: await kvGet(env, ivKey(env.OWNER_CHAT), null),
          queue: (await (await db(env)).prepare("SELECT COUNT(*) n FROM queue").first())?.n });
      }
      return json({ error: "нет такого" }, 404);
    }
    return new Response("Ra'no worker ok");
  },
};

import { MAP_HTML } from "./map.js";
import { DRAW_HTML } from "./draw.js";
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
  "budget", "floor_min", "floor_max", "near", "near_km", ...DATES];
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
    if (k === "near") {                          // ориентир: «метро Ойбек», «Ц-1», «Госпитальный рынок»
      const t = String(v).replace(/\s+/g, " ").trim().slice(0, 60);
      if (t.length >= 2 && !/^(any|нет|неважно)$/i.test(t)) out.near = t;
      continue;
    }
    if (k === "near_km") { const n = parseFloat(String(v).replace(",", ".")); if (n >= 0.3 && n <= 10) out.near_km = String(Math.round(n * 10) / 10); continue; }
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
  if (out.city !== "tashkent") { out.districts = []; delete out.districts_any; delete out.near; delete out.near_km; }
  if (out.city !== "other") delete out.city_other;
  return out;
}

// ───────────────────────────── промпт и схема ─────────────────────────────
const SYSTEM = `Ты — Ra'no, ИИ-ассистент по подбору жилья в Узбекистане (в основном Ташкент).
Ты всегда ИИ-ассистент, никогда не выдаёшь себя за человека. В этом чате ты коротким
дружелюбным разговором выясняешь, что ищет клиент, и заполняешь параметры поиска.
По ним ты СНАЧАЛА ищешь сама — на сайтах и в Telegram-каналах. Маклеров подключаешь, только
если на сайтах пусто (это предложит система) или клиент сам попросит. Не предлагай маклеров первой.

Твой характер: живая, весёлая девушка из Ташкента с лёгким юмором, которая обожает разбираться
в квартирах. О себе — только в женском роде («нашла», «поняла», «записала»). К клиенту — на «вы».
Можно 1–2 эмодзи и лёгкую шутку на сообщение, но по делу. К месту — узбекские словечки
(Assalomu alaykum, rahmat, zo'r, xo'p). Не шути про деньги клиента, риски, документы и отказы —
там спокойно и ясно. Без сарказма над клиентом, маклерами и районами.

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
- ready=true, когда известны deal, city, budget и (кроме участка) rooms, И ты уже спросила
  про пожелания (или клиент сам сказал, что остальное неважно / «ищи» / «хватит»).
  Тогда в reply одной-двумя строками перечисли собранное и скажи, что начинаешь искать сама.
  Про маклеров и текст запроса НЕ говори (кроме посуточной аренды: её на сайтах почти нет —
  скажи, что быстрее найдут маклеры, и система покажет текст запроса).
- Клиент может потом менять что угодно словами («бюджет 1200», «добавь Юнусабад»,
  «парковка не нужна»). Обнови поля и снова верни ready=true, если главное известно.
- Если сообщение не про жильё — ответь коротко и мягко верни к поиску. Не выдумывай факты, которых
  не знаешь (погоду, новости, курсы): честно скажи, что этого не знаешь, и верни к поиску.
- Не начинай каждый ответ одинаково («Отлично», «Отличный выбор») — меняй начало или сразу к делу.
- «Со следующей недели», «через неделю», «с понедельника» — movein="date" и movein_date на эту дату.

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
near: ориентир, рядом с которым искать, — как назвал клиент: «метро Ойбек», «Ц-1», «Госпитальный рынок»,
  «Мегапланет», «улица Бабура». «Центр», «в центре», «ближе к центру», «markazda» — near = "центр"
  (что считается центром именно этого города, система узнает сама).
near_km: радиус в км, если клиент назвал («не дальше 2 км», «пешком до метро» → 1); иначе не ставь.
  Ориентир и районы можно вместе: районы тогда не нужны, ищу по радиусу.
note: короткая фраза для маклеров о том, для чего нет своего поля: «нужна ипотека»,
  «рассрочка», «рядом со школой», «тихий двор». Пиши её целиком заново (с прежним содержимым),
  на языке письма маклерам (русский, для lang=uz — узбекский).

intent — что клиент хочет сделать этим сообщением (выбери ОДНО):
  search — описывает или меняет поиск (по умолчанию);
  restart — начать новый поиск с нуля («давай заново», «теперь ищу аренду, забудь прошлое»);
  search_now — «ищи сама», «поищи на сайтах», «проверь, что нового» — пробежаться по сайтам прямо сейчас;
  show_pick — показать подборку с сайтов («что нашла?», «покажи подборку», «покажи все», «ещё раз список»);
  show_item — показать конкретные объявления из ПОСЛЕДНЕЙ ПОДБОРКИ с фото и разбором цены
    («покажи 2 и 4», «есть фото у первой?», «покажи все 5 с фото») — номера в items
    (все — перечисли все номера из last_pick; не больше 5);
  rano_status — как идёт поиск по сайтам, сколько нашла; via_status — как дела с маклерами;
  show_offers — что прислали маклеры; shortlist — отобранные варианты (шортлист);
  sl_item — открыть вариант из шортлиста по номеру (номер в items);
  brokers — разослать запрос / написать маклерам;
  add_offer — добавить вариант, который ему прислали в WhatsApp («мне скинули квартиру, добавь»);
  market — цены рынка; request_text — показать текст запроса маклерам; help — что ты умеешь;
  card — клиент говорит о конкретной квартире из присланных: нравится / не подходит / уточнить / посмотреть
    вживую / заметка / найти похожие. Он ответил на её сообщение (в данных есть card) или говорит о ней иначе.
    Клиент пишет как удобно, своими словами — никаких «правильных» фраз нет, понимай по смыслу:
    «норм вариант» / «вот эта ничего» / «оставь её» → like; «не, не то» / «дороговато» / «первый этаж не хочу» → skip;
    «а документы там чистые?» / «спроси, торг есть?» → ask; «давай глянем её в субботу» → view; «потом решу» → later.
    Поля: card_act — одно или несколько через запятую, по порядку: like | skip | later | ask | view | note | similar | search_like
      («нравится, узнай про документы» → "like,ask»; «посмотрим в субботу в 11» → "view");
    note_text — для view: когда смотреть как сказал клиент («суббота 11:00»); для note: что записать;
      для ask: что именно спросить, если сказал;
    reason для skip, если понятно почему: p — дорого, d — место, c — состояние/ремонт, a — площадь/планировка;
    items — какой вариант из recent_cards имеется в виду, если card нет: по номеру («вторая»), по порядку
      («последняя», «предыдущая») или по описанию («та, что на Чиланзаре», «за 64 тысячи») — сравни с line.
      Если card есть — items не нужен. Если не понятно, о какой квартире речь, — items пустой.
  Для всего, кроме search и restart, set оставь пустым, а reply — одной короткой фразой-подводкой
  («Показываю 👇», «Бегу проверять сайты 🏃‍♀️»): само действие выполнит система сразу после твоего ответа.

Что у тебя есть (блок «Сейчас у бота» в запросе) — это правда, опирайся ТОЛЬКО на неё:
  last_pick — последняя подборка с сайтов с номерами, как их видел клиент; shortlist — шортлист с номерами;
  card — карточка варианта, на которую клиент ответил (если ответил); recent_cards — последние карточки
  в чате с номерами n по порядку (последняя — с наибольшим n);
  pick_pending — сколько новых ждут подборки; today — сколько объявлений просмотрела и подошло сегодня.
  rent_search — что сейчас ищу в аренде на самом деле (комнаты, бюджет, область на карте или районы, хозяева
  или маклеры); rent_today — за сутки новых и присланных. На «что ищешь?» отвечай по rent_search / site_search:
  если там «внутри областей, обведённых на карте» — говори про область на карте, а не про районы из анкеты.
  Никогда не говори, что что-то показала, отправила, нашла или «открываю», если этого нет в данных или
  если это не делает выбранный intent. Если клиент ссылается на то, чего нет (номер больше списка,
  подборок ещё не было) — честно скажи и предложи, что можно сделать.
  Фото есть у объявлений с сайтов: их показывает show_item. Ты сама картинки не видишь.

Честность: если ты ничего не записал в set — не пиши «учла», «обновила запрос».
«Центр» без названий районов — не выдумывай districts, а поставь near = "центр".
Ориентир («рядом с метро Ойбек», «у Ц-1», «в 2 км от Мегапланета») — в near (и near_km, если назван радиус).`;

// Перечисления в схеме Gemini воспринимает как «заполнять только если уверен» и молча
// пропускает поля — поэтому в схеме просто строки, а допустимые значения проверяет applyPatch.
// Схема: set — СПИСОК пар {k, v}. С объектом из ~25 необязательных полей Gemini
// молча пропускает часть (проверено вживую: who/pets/term/movein терялись),
// а список фактов перечисляет полно. Значения проверяет applyPatch.
const KEYS = [...Object.keys(E), "districts", "rooms", "floor_pref", "budget", "floor_min", "floor_max",
  "movein_date", "date_from", "date_to", "city_other", "note", "near", "near_km"];
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
    intent: { type: "string", description: "search | restart | search_now | show_pick | show_item | rano_status | via_status | show_offers | shortlist | sl_item | brokers | add_offer | market | help | request_text | card" },
    card_act: { type: "string", description: "для intent=card: like | skip | later | ask | view | note | similar | search_like" },
    reason: { type: "string", description: "для card_act=skip: p | d | c | a" },
    note_text: { type: "string", description: "для card_act=note — текст заметки; для view — когда смотреть" },
    items: { type: "array", items: { type: "string" }, description: "номера пунктов для show_item / sl_item" },
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
export async function gemini(env, system, userText, opts = {}) {
  if (!env.GEMINI_KEY) throw new Error("GEMINI_KEY не задан");
  const SCHEMA_ = opts.schema || SCHEMA;
  const images = opts.images || [];
  const first = env.AI_MODEL || "gemini-3.8-flash";
  const models = [first, ...["gemini-3.6-flash", "gemini-3.5-flash"].filter(m => m !== first)];
  const variants = [
    { responseMimeType: "application/json", responseJsonSchema: SCHEMA_ },
    { responseMimeType: "application/json", responseSchema: SCHEMA_ },
    { responseMimeType: "application/json" },
  ];
  let lastErr = "";
  for (const model of models) {
    for (let i = 0; i < variants.length; i++) {
      const sys = system + (i === 2 ? "\n\nФормат ответа — строго JSON по схеме: " + JSON.stringify(SCHEMA_) : "");
      const gen = { temperature: opts.temperature ?? 0.4, maxOutputTokens: 2048, ...variants[i] };
      let r, d;
      for (const g of [{ ...gen, thinkingConfig: { thinkingLevel: "low" } }, gen]) {
        r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
          method: "POST",
          headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_KEY },
          body: JSON.stringify({ systemInstruction: { parts: [{ text: sys }] },
            contents: [{ role: "user", parts: [...images.map(im => ({ inlineData: { mimeType: im.mime, data: im.data } })),
              { text: userText }] }], generationConfig: g }),
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

// ───────────────────────────── перевод для клиента (uz/en) ─────────────────────────────
// Готовые тексты бота написаны по-русски; клиенту на узбекском/английском их переводит модель.
// Без изменений остаются: HTML-разметка, ссылки, всё внутри <code> (это текст маклерам), числа и суммы.
const TR_SCHEMA = { type: "object", properties: { text: { type: "string" } }, required: ["text"] };
const TR_LANG = { uz: "узбекский язык (латиница, как пишут в Ташкенте)", en: "английский язык", ru: "русский язык" };
const TR_SYSTEM = (lang) => `Переведи сообщение Telegram-бота Ra'no (помощник по поиску квартир в Ташкенте) ${lang === "ru" ? "с узбекского (или любого другого языка)" : "с русского"} на ${TR_LANG[lang]}.
Правила:
- Метки вида ⟦0⟧, ⟦1⟧ и HTML-теги (<b>, <i>, <a …>, </a>, <blockquote expandable>, <code>) оставь ровно как есть, на своих местах.
- Числа, суммы ($, сум, млн), проценты, даты, время, телефоны, эмодзи — без изменений.
- Названия районов, улиц, ЖК, сайтов — как принято писать на этом языке, без пояснений.
- Ra'no говорит о себе в женском роде (для английского — просто I). Тон дружелюбный и короткий, как в оригинале.
- Ничего не добавляй и не сокращай. Ответ: {"text": "перевод"}`;

export function hasRussian(text) {
  const t = String(text || "").replace(/<code>[\s\S]*?<\/code>/g, " ").replace(/<[^>]+>/g, " ")
    .replace(/https?:\/\/\S+/g, " ").replace(/«[^»]{0,300}»/g, " ");   // цитаты маклера переводим, но не считаем
  const cyr = (t.match(/[а-яё]/gi) || []).length, lat = (t.match(/[a-z]/gi) || []).length;
  return cyr >= 12 && cyr > lat * 0.6;
}

function digitsOf(t) {
  return String(t).replace(/<[^>]+>/g, " ").match(/\d+/g)?.sort().join(",") || "";
}

async function sha1(s) {
  const b = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, "0")).join("");
}

export async function translateText(env, text, lang) {
  if (!text || !TR_LANG[lang] || (lang !== "ru" && !hasRussian(text))) return text;
  const key = "tr:" + lang + ":" + (await sha1(text)).slice(0, 20);
  const hit = await kvGet(env, key, null);
  if (hit) return hit;
  const keep = [];
  const masked = String(text).replace(/<code>[\s\S]*?<\/code>|href="[^"]*"|https?:\/\/[^\s<>"]+/g,
    m => { keep.push(m); return `⟦${keep.length - 1}⟧`; });
  try {
    const out = await gemini(env, TR_SYSTEM(lang), masked, { schema: TR_SCHEMA, temperature: 0.2 });
    let t = String(out?.text || "");
    if (!t || keep.some((_, i) => !t.includes(`⟦${i}⟧`))) return text;          // метки потерялись — не рискуем
    t = t.replace(/⟦(\d+)⟧/g, (_, i) => keep[+i] ?? "");
    if (digitsOf(t) !== digitsOf(text)) return text;                            // цифры поменялись — оригинал
    await kvSet(env, key, t);
    return t;
  } catch (e) { return text; }
}

let langCache = {};
export function resetLangCache() { langCache = {}; }
async function ownerLang(env, chat = env.OWNER_CHAT) {        // язык клиента (любого пользователя)
  const c = langCache[chat];
  if (c && Date.now() - c.at < 60e3) return c.v;
  const iv = await kvGet(env, "iv:" + chat, null);
  langCache[chat] = { at: Date.now(), v: iv?.ans?.lang || "ru" };
  return langCache[chat].v;
}

// ───────────────────────────── много пользователей (этап 2) ─────────────────────────────
// Владелец (OWNER_CHAT) — клиент всегда. Остальные — из таблицы users: active / waitlist / removed.
// MULTI_USER=on — новые люди по /start становятся клиентами (до лимита, дальше — лист ожидания);
// выключено — как раньше: любой чат, кроме владельца, — маклер.
export const isMulti = env => env.MULTI_USER === "on";
const uiKey = (env, chat) => (!chat || String(chat) === String(env.OWNER_CHAT)) ? "ui" : "ui:" + chat;
const cardsKey = (env, chat) => (!chat || String(chat) === String(env.OWNER_CHAT)) ? "cards_recent" : "cards:" + chat;
let usersCache = { at: 0, m: new Map() };
export function resetUsersCache() { usersCache = { at: 0, m: new Map() }; }
async function usersMap(env) {
  if (Date.now() - usersCache.at < 30e3) return usersCache.m;
  const r = await (await db(env)).prepare("SELECT chat, status, name, username, lang, at FROM users").all();
  usersCache = { at: Date.now(), m: new Map((r.results || []).map(u => [String(u.chat), u])) };
  return usersCache.m;
}
export async function userStatus(env, chat) {
  if (String(chat) === String(env.OWNER_CHAT)) return "owner";
  return (await usersMap(env)).get(String(chat))?.status || null;
}
export async function isClient(env, chat) {
  if (String(chat) === String(env.OWNER_CHAT)) return true;
  if (!isMulti(env)) return false;
  return (await userStatus(env, chat)) === "active";
}
async function userLimit(env) { return +(await kvGet(env, "user_limit", 0)) || +(env.USER_LIMIT || 20); }
async function activeCount(env) {
  return [...(await usersMap(env)).values()].filter(u => u.status === "active").length;
}
async function setUser(env, chat, status, from = {}) {
  const lang = (from.language_code || "").slice(0, 2);
  await (await db(env)).prepare("INSERT INTO users (chat, status, name, username, lang, at) VALUES (?, ?, ?, ?, ?, ?) " +
    "ON CONFLICT(chat) DO UPDATE SET status=excluded.status, name=COALESCE(excluded.name, users.name), " +
    "username=COALESCE(excluded.username, users.username)")
    .bind(String(chat), status, [from.first_name, from.last_name].filter(Boolean).join(" ") || null,
          from.username || null, lang || null, Date.now()).run();
  resetUsersCache();
}
const adminSay = (env, text) => env.OWNER_CHAT
  ? tg(env, "sendMessage", { chat_id: env.OWNER_CHAT, text, parse_mode: "HTML", _noTr: true }) : null;
const who = (from = {}, chat = "") => esc([from.first_name, from.last_name].filter(Boolean).join(" ") || "без имени") +
  (from.username ? ` @${esc(from.username)}` : "") + ` · <code>${chat}</code>`;
function esc(s) { return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
export const WAITLIST = {
  ru: "Привет! Я Ra'no 👋 — ИИ-ассистент по поиску квартир в Ташкенте.\n\nСейчас я работаю с небольшой группой " +
    "людей, и все места заняты. Я записала вас в лист ожидания — как освободится место, напишу сюда сама 🙌",
  uz: "Salom! Men Ra'no 👋 — Toshkentda kvartira qidirishda AI-yordamchiman.\n\nHozir kichik guruh bilan ishlayapman, " +
    "barcha joylar band. Sizni kutish ro'yxatiga yozdim — joy bo'shashi bilan o'zim shu yerga yozaman 🙌",
  en: "Hi! I'm Ra'no 👋 — an AI assistant for finding flats in Tashkent.\n\nRight now I work with a small group and " +
    "all spots are taken. I've put you on the waitlist — I'll message you here as soon as a spot opens 🙌",
};
export const APPROVED = {
  ru: "🎉 Место освободилось — начинаем! Напишите своими словами, что ищете: например, «купить двушку в центре до $60 000».",
  uz: "🎉 Joy bo'shadi — boshlaymiz! Nima qidirayotganingizni yozing: masalan, «markazda 2 xonali, $60 000 gacha».",
  en: "🎉 A spot opened up — let's start! Tell me what you're looking for, e.g. \"buy a 2-room flat in the centre up to $60,000\".",
};
const langOf = from => { const l = (from?.language_code || "").slice(0, 2); return l === "uz" ? "uz" : l === "ru" ? "ru" : (l ? "en" : "ru"); };
/** Новый человек нажал Start (без метки маклера): клиент, если есть место, иначе — лист ожидания. */
export async function signup(env, msg) {
  const chat = String(msg.chat.id), from = msg.from || {};
  const st = await userStatus(env, chat);
  if (st === "active" || st === "owner") return "active";
  if (st === "removed") return "removed";
  const n = await activeCount(env), lim = await userLimit(env);
  if (n < lim) {
    await setUser(env, chat, "active", from);
    await setMapMenu(env, chat).catch(() => null);
    await adminSay(env, `🆕 Новый пользователь: ${who(from, chat)} — ${n + 1}/${lim}`);
    return "active";
  }
  if (st !== "waitlist") {
    await setUser(env, chat, "waitlist", from);
    await adminSay(env, `⏳ В лист ожидания: ${who(from, chat)} (мест ${lim}/${lim}). Пустить: <code>/approve ${chat}</code>`);
  }
  return "waitlist";
}
/** Команды владельца: /users, /approve <chat|@username>, /remove <chat>, /limit <N>. */
export async function adminCommand(env, chat, text) {
  if (String(chat) !== String(env.OWNER_CHAT)) return null;
  const [c0, arg = ""] = text.trim().split(/\s+/, 2);
  const cmd = c0.toLowerCase().split("@")[0];
  if (!["/users", "/approve", "/remove", "/limit"].includes(cmd)) return null;
  const m = await usersMap(env);
  const find = a => [...m.values()].find(u => String(u.chat) === a || (u.username && "@" + u.username.toLowerCase() === a.toLowerCase()));
  if (cmd === "/limit") {
    const n = parseInt(arg, 10);
    if (n > 0) await kvSet(env, "user_limit", n);
    return `Лимит пользователей: ${await userLimit(env)} (сейчас ${await activeCount(env)}).`;
  }
  if (cmd === "/users") {
    const rows = [...m.values()].sort((a, b) => (a.at || 0) - (b.at || 0));
    const line = u => `• ${who({ first_name: u.name, username: u.username }, u.chat)}`;
    const act = rows.filter(u => u.status === "active"), wl = rows.filter(u => u.status === "waitlist");
    return `👥 <b>Пользователи</b>: ${act.length}/${await userLimit(env)}` + (isMulti(env) ? "" : " (MULTI_USER выключен)") +
      (act.length ? "\n" + act.map(line).join("\n") : "") +
      (wl.length ? `\n\n⏳ <b>Лист ожидания</b>: ${wl.length}\n` + wl.map(line).join("\n") : "") +
      "\n\nПустить: <code>/approve id</code> · убрать: <code>/remove id</code> · лимит: <code>/limit 30</code>";
  }
  const u = find(arg);
  if (!u) return "Не нашла такого пользователя — посмотрите <code>/users</code>.";
  if (cmd === "/approve") {
    await setUser(env, u.chat, "active", {});
    await setMapMenu(env, u.chat).catch(() => null);
    const L = u.lang === "uz" ? "uz" : u.lang === "ru" || !u.lang ? "ru" : "en";
    await tg(env, "sendMessage", { chat_id: u.chat, text: APPROVED[L], _noTr: true });
    return `✅ Пустила: ${who({ first_name: u.name, username: u.username }, u.chat)}`;
  }
  await setUser(env, u.chat, "removed", {});
  return `🚫 Убрала: ${who({ first_name: u.name, username: u.username }, u.chat)} — сообщения от него больше не обрабатываю.`;
}

// ───────────────────────────── разбор варианта маклера ─────────────────────────────
export const OFFER_KEYS = ["is_offer", "deal", "price", "currency", "price_period", "rooms", "area", "floor",
  "floors_total", "district", "address", "landmark", "repair", "building", "furniture", "commission",
  "mortgage", "documents", "summary", "map_photos"];
const OFFER_SCHEMA = {
  type: "object",
  properties: {
    facts: { type: "array", description: "по паре на каждый найденный факт", items: { type: "object",
      properties: { k: { type: "string", description: OFFER_KEYS.join(" | ") }, v: { type: "string" } }, required: ["k", "v"] } },
  },
  required: ["facts"],
};
const OFFER_SYSTEM = `Ты разбираешь сообщение маклера (риелтора) о квартире в Ташкенте — текст и, если есть, фото
(часто это скриншот объявления с ценой и параметрами). Извлеки ТОЛЬКО то, что написано или видно.
Ничего не выдумывай и не угадывай. Пиши факты списком пар {k, v}.

Поля:
is_offer: yes — это предложение конкретного жилья; no — приветствие, вопрос, «позвоню», «есть варианты» без конкретики
deal: rent (аренда помесячно) | daily (посуточно) | sale (продажа)
price: число без пробелов. «44 тыс», «44к», «44.000» → 44000; «1,2 млн у.е.» → 1200000
currency: USD (также $, у.е., уе, y.e., доллар) | UZS (сум, so'm, сўм)
price_period: month | day | total (для продажи — total)
rooms: число комнат. Формат «2/5/9» = 2 комнаты, 5 этаж, 9 этажей
area: общая площадь, м², число
floor, floors_total: этаж и этажность, числа
district: только если назван или однозначен — один из: ${DISTRICTS.join(", ")}
address: улица/массив/ЖК/дом, как написано; landmark: ориентир (метро, школа, ТЦ).
Если одно из фото — скриншот карты или локации (Яндекс, Google, 2ГИС, Telegram): прочитай с него улицу, дом, ЖК
или ориентир рядом с меткой и запиши в address/landmark
map_photos: номера таких фото-карт (с 1, по порядку присланных), через запятую — это не фото квартиры
repair: коротко, как в тексте (евроремонт, дизайнерский, требует ремонта, без ремонта)
building: new (новостройка/ЖК) | secondary (вторичка); можно с материалом: «новостройка, монолит»
furniture: yes | no | частично; commission: как написано («50%», «нет», «за счёт продавца»)
mortgage: yes | no — только если сказано про ипотеку/кредит; documents: как написано («кадастр готов»)
summary: одна короткая строка по-русски — важное, чего нет в полях (до 120 символов), иначе не пиши`;

/** «44 тыс» → 44000, «1,2 млн» → 1200000, «52,5» → 52.5, «44,000» / «44 000» → 44000. */
export function parseNum(v) {
  let t = String(v || "").toLowerCase().replace(/\u00a0/g, " ");
  const mult = /млн|mln|million|миллион/.test(t) ? 1e6 : /тыс|ming|\d\s*(k|к)(?![a-zа-яё])|thousand/.test(t) ? 1e3 : 1;
  t = t.replace(/(\d)[\s'](?=\d{3}\b)/g, "$1");             // пробелы-разделители тысяч
  if (/(^|[^\d.])\d{1,3}(\.\d{3})+(?![\d.])/.test(t)) t = t.replace(/(\d)\.(?=\d{3}(?!\d))/g, "$1");   // «44.000$», «1.200.000»
  t = /\d,\d{3}(\D|$)/.test(t) ? t.replace(/,(?=\d{3}(\D|$))/g, "") : t.replace(",", ".");
  const m = t.match(/\d+(?:\.\d+)?/);
  return m ? +m[0] * mult : NaN;
}

function normOfferFacts(facts, keys = OFFER_KEYS) {
  const o = {};
  for (const f of Array.isArray(facts) ? facts : []) {
    const k = String(f?.k || "").trim(), v = String(f?.v ?? "").trim();
    if (!keys.includes(k) || !v || /^(null|none|нет данных|-)$/i.test(v)) continue;
    if (["price", "area"].includes(k)) { const n = parseNum(v); if (n > 0) o[k] = Math.round(n * 100) / 100; continue; }
    if (["rooms", "floor", "floors_total"].includes(k)) { const n = parseInt(v, 10); if (n > 0 && n < 100) o[k] = n; continue; }
    if (k === "district") { const i = districtIdx(v); if (i && i !== "any") o.district = DISTRICTS[+i]; continue; }
    if (k === "is_offer") { o.is_offer = !/^(no|нет|false)$/i.test(v); continue; }
    if (k === "currency") { o.currency = /uzs|сум|so.?m|сўм/i.test(v) ? "UZS" : "USD"; continue; }
    o[k] = v.slice(0, k === "summary" ? 160 : 120);
  }
  if (o.floor && o.floors_total && o.floor > o.floors_total) delete o.floor;
  return o;
}

async function tgPhoto(env, fileId) {
  const f = await tg(env, "getFile", { file_id: fileId });
  const path = f?.result?.file_path;
  if (!path) return null;
  const r = await fetch(`https://api.telegram.org/file/bot${await botToken(env)}/${path}`);
  if (!r.ok) return null;
  const buf = new Uint8Array(await r.arrayBuffer());
  if (buf.length > 4e6) return null;
  let bin = "";
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return { mime: /\.png$/i.test(path) ? "image/png" : "image/jpeg", data: btoa(bin) };
}

export async function parseOffer(env, { text = "", photos = [], deal = "" }) {
  const images = [];
  for (const id of photos.slice(0, 4)) { try { const im = await tgPhoto(env, id); if (im) images.push(im); } catch (e) {} }
  if (!String(text).trim() && !images.length) return { is_offer: false };
  const out = await gemini(env, OFFER_SYSTEM,
    (deal ? `Клиент ищет: ${deal === "sale" ? "покупку" : "аренду"}.\n` : "") + `Сообщение маклера:\n${String(text).slice(0, 3000) || "(только фото)"}`,
    { schema: OFFER_SCHEMA, images, temperature: 0.1 });
  return normOfferFacts(out.facts);
}

// ───────────────────────────── объявление по ссылке ─────────────────────────────
// Страницу открывает сама модель (инструмент url_context Gemini) — как превью ссылки:
// одна страница, которую прислал пользователь. Не открылась — честно говорим и просим скриншот.
export const LINK_KEYS = [...OFFER_KEYS, "seller_type", "phone", "posted", "title"];
const LINK_SYSTEM = `Тебе дают ссылку на объявление о недвижимости (OLX, Uybor, Realt24, Telegram-канал и т. п.).
Открой страницу и извлеки ТОЛЬКО то, что на ней написано. Ничего не выдумывай.
Ответь строго JSON: {"opened": true|false, "facts": [{"k": "...", "v": "..."}]}.
opened=false — если страница не открылась, требует входа, капчу или это не объявление.
Ключи фактов: ${OFFER_KEYS.join(", ")}, а также:
seller_type: owner (частное лицо, собственник, «без маклера») | agency (агентство, риелтор, маклер) | developer (застройщик)
phone: телефон продавца, если виден; posted: дата публикации как написано; title: заголовок объявления.
Остальные поля — как в объявлении: deal (sale | rent | daily), price (число), currency (USD | UZS), rooms, area, floor,
floors_total, district (район Ташкента), address, landmark, repair, building, furniture, mortgage, documents, summary (1 строка).`;

export async function linkFacts(env, url) {
  if (!env.GEMINI_KEY) throw new Error("GEMINI_KEY не задан");
  const first = env.AI_MODEL || "gemini-3.8-flash";
  let last = "";
  for (const model of [first, ...["gemini-3.6-flash", "gemini-3.5-flash"].filter(m => m !== first)]) {
    const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
      method: "POST", headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_KEY },
      body: JSON.stringify({ systemInstruction: { parts: [{ text: LINK_SYSTEM }] },
        contents: [{ role: "user", parts: [{ text: `Ссылка на объявление: ${url}` }] }],
        tools: [{ url_context: {} }], generationConfig: { temperature: 0.1, maxOutputTokens: 2048 } }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { last = `${model} ${r.status}: ${d?.error?.message || ""}`; if ([429, 500, 503, 404].includes(r.status)) continue; break; }
    const c = d.candidates?.[0] || {};
    const meta = (c.urlContextMetadata || c.url_context_metadata || {}).urlMetadata || [];
    const fetched = meta.some(m => /SUCCESS/i.test(m.urlRetrievalStatus || m.url_retrieval_status || ""));
    const raw = (c.content?.parts || []).filter(p => !p.thought).map(p => p.text || "").join("");
    const a = raw.indexOf("{"), z = raw.lastIndexOf("}");
    let o = {};
    try { o = JSON.parse(raw.slice(a, z + 1)); } catch (e) { o = {}; }
    const facts = normOfferFacts(o.facts, LINK_KEYS);
    const opened = (fetched || !meta.length) && o.opened !== false && Object.keys(facts).length >= 2;
    return { opened, facts: opened ? facts : {}, status: meta.map(m => m.urlRetrievalStatus || m.url_retrieval_status).join(",") };
  }
  throw new Error(last || "Gemini недоступен");
}

// ───────────────────────── что считается центром города ─────────────────────────
// Модель ищет в интернете (инструмент google_search Gemini), что жители и риелторы называют центром.
// Координаты и радиус Python потом проверяет по карте — сюда только смысл и названия.
const CITY_SYSTEM = `Ты помогаешь сервису поиска квартир понять, что в городе считается центром.
Найди в интернете, что жители, риелторы и сайты недвижимости этого города называют центром.
Ответь строго JSON без пояснений:
{"anchor": "главная центральная точка, от которой меряют центр (площадь, сквер), как она называется на карте",
 "boundary": "чем ограничен центр — улицы, кольцевая дорога, река; одна короткая фраза по-русски",
 "radius_km": число — примерный радиус центра от anchor в км,
 "in": ["8–15 массивов, кварталов, улиц и ориентиров, которые точно ВНУТРИ центра"],
 "near": ["5–10 мест рядом с центром, но уже не центр"],
 "out": ["5–10 известных районов и массивов города, которые точно НЕ центр"]}
Названия — как их пишут на картах и в объявлениях, по-русски. Ничего не выдумывай: если не нашла — пустой список.`;

export async function cityCenterFacts(env, city) {
  if (!env.GEMINI_KEY) throw new Error("GEMINI_KEY не задан");
  const first = env.AI_MODEL || "gemini-3.8-flash";
  let last = "";
  for (const model of [first, ...["gemini-3.6-flash", "gemini-3.5-flash"].filter(m => m !== first)]) {
    const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
      method: "POST", headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_KEY },
      body: JSON.stringify({ systemInstruction: { parts: [{ text: CITY_SYSTEM }] },
        contents: [{ role: "user", parts: [{ text: `Город: ${city}, Узбекистан. Что здесь считается центром города?` }] }],
        tools: [{ google_search: {} }], generationConfig: { temperature: 0.1, maxOutputTokens: 2048 } }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { last = `${model} ${r.status}: ${d?.error?.message || ""}`; if ([429, 500, 503, 404].includes(r.status)) continue; break; }
    const c = d.candidates?.[0] || {};
    const raw = (c.content?.parts || []).filter(p => !p.thought).map(p => p.text || "").join("");
    const a = raw.indexOf("{"), z = raw.lastIndexOf("}");
    let o = {};
    try { o = JSON.parse(raw.slice(a, z + 1)); } catch (e) { last = "не JSON"; continue; }
    const list = v => (Array.isArray(v) ? v : []).map(x => String(x).trim()).filter(x => x && x.length <= 60).slice(0, 15);
    const gm = c.groundingMetadata || c.grounding_metadata || {};
    const sources = (gm.groundingChunks || gm.grounding_chunks || []).map(g => g.web?.uri).filter(Boolean).slice(0, 5);
    const rk = parseFloat(o.radius_km);
    return { anchor: String(o.anchor || "").slice(0, 80), boundary: String(o.boundary || "").slice(0, 160),
      radius_km: rk > 0 && rk < 15 ? rk : null, in: list(o.in), near: list(o.near), out: list(o.out),
      sources, via: model };
  }
  throw new Error(last || "Gemini недоступен");
}

// ───────────────────────────── ремонт по фото ─────────────────────────────
export const REPAIR_STATES = ["good", "average", "none", "box", "unknown"];
const REPAIR_SCHEMA = {
  type: "object",
  properties: {
    kinds: { type: "string", description: "что на каждом фото по порядку, через запятую: interior | plan | render | document | exterior | map | other" },
    interior: { type: "boolean", description: "есть ли хоть одно настоящее фото комнат этой квартиры (не план, не рендер, не документ)" },
    signs: { type: "string", description: "что видно на фото интерьера, 3–8 признаков через запятую, по-русски; нет интерьера — пусто" },
    state: { type: "string", description: REPAIR_STATES.join(" | ") },
    confidence: { type: "number", description: "0…1 — насколько уверена по этим фото" },
    complex: { type: "string", description: "название ЖК или застройщика, если написано на фото (логотип, шапка плана, договор), как написано; иначе пусто" },
    address: { type: "string", description: "адрес или ориентир, если он написан на фото; иначе пусто" },
    total_price: { type: "number", description: "полная стоимость квартиры, если она есть на фото (график платежей, договор, прайс); иначе 0" },
    down_payment: { type: "number", description: "первоначальный взнос с фото; иначе 0" },
    price_currency: { type: "string", description: "UZS | USD | пусто — валюта цен с фото" },
    installment: { type: "boolean", description: "на фото график рассрочки/платежей от застройщика" },
  },
  required: ["kinds", "interior", "signs", "state", "confidence"],
};
const REPAIR_SYSTEM = `Ты смотришь фотографии из объявления о продаже квартиры в Ташкенте: что на них и в каком состоянии ремонт.
Сначала для каждого фото — что это: interior (комнаты этой квартиры), plan (планировка, схема этажа), render (3D-визуализация),
document (график платежей, договор, прайс, скриншот приложения), exterior (дом, двор), map, other.
Ремонт оцениваешь ТОЛЬКО по фото interior, а не по словам продавца. Категории:
good — свежий современный ремонт: ровные стены, современные пол и плитка, новая сантехника и двери, пластиковые окна; можно заезжать без вложений.
average — ремонт не новый, но из современных материалов: ламинат, пластиковые окна, кафель не советский, потёртости, немодные обои; заехать можно, хватит косметики.
none — советский или очень старый ремонт, либо разруха: старые обои и ковры на стенах, советская плитка и сантехника, деревянные окна, старый линолеум или дощатый пол, облезлые стены, следы протечек; по-хорошему менять всё.
box — коробка или черновая: голый бетон, стяжка, штукатурка без отделки.
unknown — нет ни одного фото interior (только планы, рендеры, документы, фасад) или по фото не понять. Тогда interior=false и signs пусто.
Мебель и вещи не путай с ремонтом. confidence ниже 0.5 — если комнат почти не видно или фото противоречат друг другу.
Сначала выпиши признаки, потом выбери категорию: два и больше признаков из описания none — это none.
Если на плане, логотипе или документе написано название ЖК или застройщика — перепиши его в complex как есть (например «Bashkent», «Gardens Residence»).
Если на фото график платежей, договор или прайс — выпиши полную стоимость (total_price), первоначальный взнос (down_payment) числами
без пробелов и валюту; installment=true, если это рассрочка застройщика. Ничего не придумывай: не видно — 0 и пусто.`;

const SOVIET_SIGNS = /советск|ковр|деревянн\S* окн|дощат|протеч|облез|старая сантех|старые труб|старая ванн|пожелтевш/g;

async function urlPhoto(url) {
  const r = await fetch(url, { headers: { "user-agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    accept: "image/*" } });
  if (!r.ok) return null;
  const mime = (r.headers.get("content-type") || "image/jpeg").split(";")[0].trim();
  if (!/^image\/(jpeg|png|webp)$/.test(mime)) return null;
  const buf = new Uint8Array(await r.arrayBuffer());
  if (!buf.length || buf.length > 4e6) return null;
  let bin = "";
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return { mime, data: btoa(bin) };
}

export async function repairFromPhotos(env, { urls = [], file_ids = [] }) {
  const images = [];
  for (const u of urls.slice(0, 4)) { try { const im = await urlPhoto(u); if (im) images.push(im); } catch (e) {} }
  for (const id of file_ids.slice(0, Math.max(0, 4 - images.length))) {
    try { const im = await tgPhoto(env, id); if (im) images.push(im); } catch (e) {}
  }
  if (!images.length) return { state: "unknown", confidence: 0, signs: "", photos: 0 };
  const o = await gemini(env, REPAIR_SYSTEM, `Фото квартиры: ${images.length} шт. Оцени состояние ремонта.`,
    { schema: REPAIR_SCHEMA, images, temperature: 0.1 });
  let state = REPAIR_STATES.includes(o.state) ? o.state : "unknown";
  // страховка: модель перечислила признаки капитального, а поставила «средний»
  const hard = (String(o.signs || "").toLowerCase().match(SOVIET_SIGNS) || []).length;
  if (state === "average" && hard >= 2) state = "none";
  const interior = o.interior !== false && /interior/.test(String(o.kinds || "interior"));
  if (!interior) state = "unknown";                 // по планам и документам ремонт не оценить
  const conf = interior ? Math.max(0, Math.min(1, Number(o.confidence) || 0)) : 0;
  const num = v => { const x = Number(String(v ?? "").replace(/[^\d.]/g, "")); return Number.isFinite(x) && x > 0 ? x : 0; };
  const cur = /usd|\$|у\.?е/i.test(String(o.price_currency || "")) ? "USD" : /uzs|сум|so.?m/i.test(String(o.price_currency || "")) ? "UZS" : "";
  return { state, confidence: Math.round(conf * 100) / 100, signs: interior ? String(o.signs || "").slice(0, 120) : "",
    photos: images.length, interior, kinds: String(o.kinds || "").slice(0, 80),
    complex: String(o.complex || "").trim().slice(0, 60), address: String(o.address || "").trim().slice(0, 100),
    total_price: num(o.total_price), down_payment: num(o.down_payment), price_currency: cur, installment: !!o.installment };
}

// ───────────────────────────── хранилище (D1) ─────────────────────────────
let schemaReady = false;
async function db(env) {
  if (!schemaReady) {
    await env.DB.batch([
      env.DB.prepare("CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, upd TEXT NOT NULL, at INTEGER NOT NULL)"),
      env.DB.prepare("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)"),
      env.DB.prepare("CREATE TABLE IF NOT EXISTS users (chat TEXT PRIMARY KEY, status TEXT, name TEXT, " +
        "username TEXT, lang TEXT, at INTEGER)"),
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
  const client = body?.chat_id != null && env.OWNER_CHAT && await isClient(env, body.chat_id).catch(() => false);
  // клиенту кнопок не шлём: управление — словами (ссылки в тексте остаются)
  if (body && method !== "editMessageReplyMarkup" && client && body.reply_markup?.inline_keyboard?.length) {
    body = { ...body }; delete body.reply_markup;
  }
  // клиент на узбекском/английском: готовые русские тексты — в перевод
  if (body?.text && client && /^(sendMessage|editMessageText)$/.test(method) && !body._noTr) {
    const lang = await ownerLang(env, String(body.chat_id));
    if (lang !== "ru" && hasRussian(body.text)) body = { ...body, text: await translateText(env, body.text, lang) };
  }
  if (body?._noTr) { body = { ...body }; delete body._noTr; }
  const r = await fetch(`https://api.telegram.org/bot${await botToken(env)}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  return r.json().catch(() => ({ ok: false }));
}
const say = (env, chat, text, extra = {}) => tg(env, "sendMessage", { chat_id: chat, text, ...extra });

// ── Управление без команд: постоянные кнопки внизу + обычные фразы ──
export const BTN = { rano: "🔎 Ищет Ra'no", via: "📇 Через маклеров", more: "⋯ Ещё",
  search: "🔎 Мой поиск", offers: "🏠 Варианты", brokers: "📇 Маклерам",      // старые кнопки — тоже понимаем
  what: "🏠 Что ищет клиент" };
// кнопок внизу больше нет — всё словами (старые кнопки у клиента пропадут с первым же ответом)
export const OWNER_KB = { remove_keyboard: true };
export const BROKER_KB = { keyboard: [[{ text: BTN.what }]], resize_keyboard: true, is_persistent: true,
  input_field_placeholder: "Пришлите вариант: фото, адрес, этаж, цена" };
let ORIGIN = "";                         // адрес воркера — для ссылки на карту цен
const MORE_MENU_ROWS = () => [
  [{ text: "✏️ Что ищем — посмотреть и поменять", callback_data: "cmd:/mysearch" }],
  [{ text: "💬 Маклер ответил мне в WhatsApp", callback_data: "cmd:/add" }],
  [{ text: "📋 Шортлист", callback_data: "cmd:/shortlist" }, { text: "📊 Цены рынка", callback_data: "cmd:/rynok" }],
  [{ text: "🏷 Поиск по сайтам", callback_data: "cmd:/rano" }, { text: "📝 Текст запроса", callback_data: "cmd:/request" }],
  [{ text: "📡 Источники: добавить свой канал", callback_data: "cmd:/sources" }],
  [{ text: "🔄 Начать поиск заново", callback_data: "q:again" }],
  ...(ORIGIN ? [[{ text: "🗺 Карта цен по городу", url: ORIGIN + "/map" }]] : []),
  [{ text: "❓ Как это работает", callback_data: "cmd:/help" }],
];
// намерения из обычных фраз → что делает Python
const INTENT_CMD = { show_offers: "/offers", shortlist: "/shortlist", brokers: "/brokers",
  market: "/rynok", help: "/help", request_text: "/request", sale_search: "/rano" };

// Готовые тексты из снимка (цены рынка, поиск на Uybor, текст запроса, справка) — сразу.
async function instantText(env, chat, cmd) {
  const ui = await kvGet(env, uiKey(env, chat), null);
  const t = ui && ui.texts && ui.texts[cmd];
  if (!t) return false;
  if (cmd === "/request") {                       // запрос поменялся после снимка — пусть ответит Python
    const iv = await kvGet(env, ivKey(chat), null);
    if (iv && iv.sentAt && iv.sentAt > (ui.at || 0)) return false;
  }
  await say(env, chat, t, { parse_mode: "HTML", disable_web_page_preview: true });
  return true;
}

// ── Экраны двух кнопок поиска — из снимка; без снимка ответит Python ──
async function showScreen(env, chat, cmd) {
  const scr = ((await kvGet(env, uiKey(env, chat), null)) || {}).screens?.[cmd];
  if (!scr) return asCommand(env, chat, cmd);
  await say(env, chat, scr.text, { parse_mode: "HTML", disable_web_page_preview: true, reply_markup: scr.kb });
}

// ── Шортлист из снимка: номер открывает карточку варианта, сортировку ведёт воркер ──
const SL_ORDER = ["n", "p", "m"];
function renderShortlist(ui, st) {
  const v = (ui.sl || {})[st.sort] || (ui.sl || {}).n;
  if (!v || !v.items || !v.items.length) return { text: ui.sl_empty || "📋 Шортлист пуст", kb: null };
  const lines = [v.title];
  v.items.forEach((r, i) => {
    lines.push(`${i + 1}. ${r.line}`);
    if (r.stage) lines.push(`      ${r.stage}`);
    if (r.note) lines.push(`      <i>${r.note}</i>`);
  });
  const rows = []; let row = [];
  v.items.forEach((r, i) => {
    row.push({ text: String(i + 1), callback_data: `s:o:${r.oid}` });
    if (row.length === 5) { rows.push(row); row = []; }
  });
  if (row.length) rows.push(row);
  if (v.askable) rows.push([{ text: `📨 Уточнить у всех, кого ещё не спрашивали (${v.askable})`, callback_data: "s:go" }]);
  rows.push([{ text: `↕️ Сортировка: ${v.sort_label}`, callback_data: "s:sort" }, { text: "🔄 Обновить", callback_data: "s:ref" }]);
  return { text: lines.join("\n"), kb: { inline_keyboard: rows } };
}

// ── дата просмотра из обычной фразы: «завтра 18:00», «сб 11», «12 октября в 15», «в 19» ──
const WD = [["вс", "воскр"], ["пн", "понед"], ["вт", "вторн"], ["ср", "сред"], ["чт", "четв"], ["пт", "пятн"], ["сб", "суббот"]];
const WD_SHORT = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
const MON = ["январ", "феврал", "март", "апрел", "ма[яй]", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"];
const MON_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const NB = "(?:^|[^а-яa-z0-9])";            // «границу слова» \b JS для кириллицы не знает
export function parseWhen(text, nowMs = Date.now()) {
  let t = " " + String(text || "").toLowerCase().replace(/ё/g, "е") + " ";
  const loc = new Date(nowMs + 5 * 3600e3);                          // Ташкент, UTC+5, без перехода на летнее
  let y = loc.getUTCFullYear(), mo = loc.getUTCMonth(), d = loc.getUTCDate();
  let day = null, hh = null, mm = 0, wd = null;
  let m;
  if ((m = t.match(/(\d{1,2})\s*(январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*/))) {
    const mi = MON.findIndex(x => new RegExp("^" + x).test(m[2]));
    day = { y, mo: mi, d: +m[1] }; t = t.replace(m[0], " ");
  } else if ((m = t.match(/(\d{1,2})([.\/])(\d{1,2})(?:[.\/](\d{2,4}))?/))) {
    // «9.10» — дата; «18.30», «завтра 10.10», «в 11.00» — время
    const before = t.slice(0, t.indexOf(m[0]));
    const rest = t.replace(m[0], " ");
    const otherTime = /\d{1,2}:\d{2}/.test(rest) || new RegExp(NB + "в\\s*\\d").test(rest);
    const relDay = /сегодня|завтра/.test(t) || WD.some(([a, b]) => new RegExp(NB + "(" + a + "(?![а-я])|" + b + ")").test(t));
    const asTime = m[2] === "." && !m[4] && m[3].length === 2 && +m[1] < 24 && +m[3] < 60 &&
      (+m[3] > 12 || +m[3] === 0 || /(^|[^а-я])в\s*$/.test(before) || (relDay && !otherTime));
    if (!asTime && +m[3] >= 1 && +m[3] <= 12 && +m[1] >= 1 && +m[1] <= 31) {
      day = { y: m[4] ? (+m[4] < 100 ? 2000 + +m[4] : +m[4]) : y, mo: +m[3] - 1, d: +m[1] }; t = rest;
    }
  }
  if (!day) {
    if (/послезавтра/.test(t)) day = { rel: 2 };
    else if (/завтра/.test(t)) day = { rel: 1 };
    else if (/сегодня/.test(t)) day = { rel: 0 };
    else {
      for (let i = 0; i < 7 && wd === null; i++)
        if (new RegExp(NB + "(" + WD[i][0] + "(?![а-я])|" + WD[i][1] + ")").test(t)) wd = i;
      if (wd !== null) day = { wd };
    }
  }
  if ((m = t.match(/(\d{1,2})[:.](\d{2})/)) && +m[1] < 24 && +m[2] < 60) { hh = +m[1]; mm = +m[2]; }
  else if ((m = t.match(new RegExp(NB + "в\\s*(\\d{1,2})(?![\\d.:])"))) && +m[1] < 24) hh = +m[1];
  else if ((m = t.match(/(\d{1,2})\s*(?:ч|час)/)) && +m[1] < 24) hh = +m[1];
  else if (day && (m = t.match(new RegExp(NB + "(\\d{1,2})(?![\\d.:/])"))) && +m[1] < 24 && +m[1] >= 7) hh = +m[1];
  else if (/утр/.test(t)) hh = 10; else if (/вечер/.test(t)) hh = 19; else if (/дн[её]м|обед/.test(t)) hh = 14;
  if (hh !== null && hh < 7 && !/утр|ноч/.test(t)) hh += 12;          // «в 3» — это 15:00
  if (!day && hh === null) return null;
  const notime = hh === null;
  const H = notime ? 12 : hh, Mi = notime ? 0 : mm;
  const at = (yy, mm0, dd) => Date.UTC(yy, mm0, dd, H, Mi) - 5 * 3600e3;
  let ms;
  if (!day) { ms = at(y, mo, d); if (ms < nowMs) ms = at(y, mo, d + 1); }
  else if (day.rel !== undefined) ms = at(y, mo, d + day.rel);
  else if (day.wd !== undefined) {
    let delta = (day.wd - loc.getUTCDay() + 7) % 7;
    ms = at(y, mo, d + delta);
    if (ms < nowMs) ms = at(y, mo, d + delta + 7);
  } else {
    ms = at(day.y, day.mo, day.d);
    if (ms < nowMs - 86400e3) ms = at(day.y + 1, day.mo, day.d);
  }
  const L = new Date(ms + 5 * 3600e3);
  const p2 = n => String(n).padStart(2, "0");
  const iso = `${L.getUTCFullYear()}-${p2(L.getUTCMonth() + 1)}-${p2(L.getUTCDate())}T${p2(L.getUTCHours())}:${p2(L.getUTCMinutes())}:00+05:00`;
  const dd = Math.round((Date.UTC(L.getUTCFullYear(), L.getUTCMonth(), L.getUTCDate()) - Date.UTC(y, mo, d)) / 86400e3);
  const dayLbl = dd === 0 ? "сегодня" : dd === 1 ? "завтра" : `${WD_SHORT[L.getUTCDay()]}, ${L.getUTCDate()} ${MON_GEN[L.getUTCMonth()]}`;
  return { at: iso, ms, notime, label: notime ? `${dayLbl} (время уточнить)` : `${dayLbl}, ${p2(L.getUTCHours())}:${p2(L.getUTCMinutes())}` };
}

const OFFER_TOAST = { ask: "📨 Бегу спрашивать маклера", rem: "🔔 Тихонько напомню маклеру", quiet: "Хорошо, не дёргаю 🙂",
  vclr: "Просмотр отменила", "seen:g": "👍 Записала: нравится!", "seen:m": "🤔 Записала: думаете", "seen:n": "👎 Убираю — не наше" };

async function pendingChanges(env) {
  const r = await (await db(env)).prepare("SELECT upd FROM queue").all();
  return (r.results || []).filter(x => { try { return /^t:[slr]:/.test(JSON.parse(x.upd).callback_query?.data || ""); } catch (e) { return false; } }).length;
}

export async function showShortlist(env, chat, messageId = null) {
  // карточки с фото собирает Python — список из снимка больше не показываем
  return asCommand(env, chat, "/shortlist");
}

export async function showShortlistList(env, chat, messageId = null) {   // прежний список (не используется)
  const ui = await kvGet(env, uiKey(env, chat), null);
  if (!ui || !ui.sl) return asCommand(env, chat, "/shortlist");
  const st = await kvGet(env, "sl", null) || { sort: "n" };
  const { text, kb } = renderShortlist(ui, st);
  const pend = await pendingChanges(env);
  const full = text + (pend ? `\n\n<i>⏳ Ещё ${pend} отметок дописываю — через минутку обновлю.</i>` : "");
  if (pend) await wake(env);
  const extra = { parse_mode: "HTML", ...(kb ? { reply_markup: kb } : {}) };
  if (messageId) {
    const r = await tg(env, "editMessageText", { chat_id: chat, message_id: messageId, text: full, ...extra });
    if (r && r.ok !== false) return;
  }
  await say(env, chat, full, extra);
}

async function asCommand(env, chat, cmd, L = "ru") {
  if (await instantText(env, chat, cmd)) return;
  // синтетическое сообщение-команда от владельца — Python обработает как набранную
  await queueAndWake(env, { message: { message_id: 0, chat: { id: +chat || chat, type: "private" },
    from: { id: +chat || chat }, date: Math.floor(Date.now() / 1000), text: cmd } }, chat, WAIT[L]);
}

async function showMySearch(env, chat, L) {
  const iv = await kvGet(env, ivKey(chat), null);
  if (!iv || !essentialsOk(iv.ans || {})) return startInterview(env, chat, false, L);
  const note = iv.ans.note ? `\nПожелания: ${iv.ans.note}` : "";
  const ui = await kvGet(env, uiKey(env, chat), null);
  const st = ui?.screens?.["/rano"]?.text ? "\n\n" + ui.screens["/rano"].text : "";
  await say(env, chat, `🔎 <b>Мой поиск</b>: ${summary(iv.ans)}.${note}${st}\n\n` +
    "Поменять — просто напишите: «бюджет 60 тысяч», «добавь Юнусабад», «рядом с метро Ойбек». " +
    "Ещё можно: «покажи шортлист», «напиши маклерам», «начать заново».", { parse_mode: "HTML", reply_markup: OWNER_KB });
}

// ── «Варианты» и «Маклерам» — сразу, из снимка, который присылает Python ──
const DECLINE = [["p", "💸 Дорого"], ["d", "📍 Район"], ["c", "🛠 Состояние"], ["a", "📐 Площадь/планировка"], ["x", "Без причины"]];
const triageKb = oid => ({ inline_keyboard: [[
  { text: "👍 В шортлист", callback_data: `t:s:${oid}` }, { text: "🕐 Позже", callback_data: `t:l:${oid}` },
  { text: "👎 Мимо", callback_data: `t:n:${oid}` }]] });

async function pendingBrokerMsgs(env, chat = env.OWNER_CHAT) {
  const r = await (await db(env)).prepare("SELECT upd FROM queue").all();
  const owner = String(env.OWNER_CHAT);
  return (r.results || []).filter(x => {
    try {
      const u = JSON.parse(x.upd); const m = u.message || u.edited_message;
      return m && !m._welcomed && String(m.chat?.id) !== String(chat) && String(m._client || owner) === String(chat);
    } catch (e) { return false; }
  }).length;
}

export async function showOffers(env, chat, all = false) {
  const ui = await kvGet(env, uiKey(env, chat), null);
  if (!ui) return asCommand(env, chat, all ? "/offers" : "/offers");
  const waiting = await pendingBrokerMsgs(env, chat);
  const waitNote = waiting ? `\n\n⏳ Ещё ${waiting} сообщ. от маклеров разбираю — карточки будут через минуту-две.` : "";
  if (waiting) await wake(env);
  if (!ui.offers_total) {
    const hint = ui.written
      ? `\nВы написали ${ui.written} маклерам — как ответят, принесу их варианты сюда карточками. ` +
        "Скажите «напиши маклерам» — подберу ещё."
      : "\nСкажите «напиши маклерам» — подберу маклеров под ваш запрос, текст им уже будет вписан.";
    const biz = ui.biz ? "" : ui.broker_mode === "manual"
      ? "\nМаклер ответил вам — перешлите мне его сообщения, сделаю карточку."
      : "\nЧтобы я сама читала ответы маклеров в ваших чатах — «подключить телеграм».";
    await say(env, chat, (waiting ? "Пока тихо — новых карточек нет 🌙" : "Пока тихо — новых вариантов нет 🌙") +
      (ui.shortlist ? ` В шортлисте — ${ui.shortlist}, скажите «шортлист».` : "") + hint + biz + waitNote);
    return;
  }
  // карточки вариантов — альбомом (карта + фото маклера), их собирает Python
  return asCommand(env, chat, all ? "/offers all" : "/offers");
}

export async function startOutreach(env, chat) {
  // подбор маклеров и ссылки с вписанным запросом — в Python, одним сообщением, без кнопок
  return asCommand(env, chat, "/brokers");
}

async function nextBroker(env, chat) {
  const ui = await kvGet(env, uiKey(env, chat), null) || {};
  const st = await kvGet(env, "out", null) || { sent: 0, skipped: 0, done: [] };
  const left = (ui.brokers || []).filter(b => !st.done.includes(b.bid));
  if (!left.length) {
    const more = (ui.brokers_total || 0) - st.done.length;
    if (more > 0) {                                   // в снимке кончились — Python пришлёт ещё
      await wake(env);
      await say(env, chat, `Ещё ${more} маклеров — подгружаю следующую порцию, это минута-две. Напишите «напиши маклерам» чуть позже.`);
    } else {
      await say(env, chat, `✅ Всех прошли: написали ${st.sent}, пропустили ${st.skipped}. ` +
        "Новые маклеры появляются сами — загляните через пару часов, подкину ещё.");
    }
    return;
  }
  const b = left[0];
  const progress = `\n\n<i>Написано ${st.sent} · пропущено ${st.skipped} · в очереди ещё ${Math.max(0, (ui.brokers_total || left.length) - st.done.length - 1)}</i>`;
  await say(env, chat, b.body + progress, { parse_mode: "HTML", reply_markup: { inline_keyboard: [b.row,
    [{ text: "✅ Отправил → следующий", callback_data: `bw:${b.bid}` }, { text: "⏭ Пропустить", callback_data: `bx:${b.bid}` }]] } });
}

async function startAddMode(env, chat) {
  const iv = (await kvGet(env, ivKey(chat), null)) || emptyIv();
  iv.mode = "add"; iv.addAt = Date.now();
  await kvSet(env, ivKey(chat), iv);
  await say(env, chat, "💬 Маклер написал вам в WhatsApp, а не мне? Не ревную 😄 Перешлите или вставьте сюда " +
    "его сообщение — текст и фото, можно по частям. Сделаю карточку, как для остальных: " +
    "карта, фото, цена и разбор — всё в одном месте. Когда всё перешлёте — напишите «готово».\n\n" +
    "А чтобы маклеры отвечали в Telegram и я читала их сама — напишите «подключить телеграм».");
}

// ───────────────────────────── «будильник» для Python ─────────────────────────────
async function pythonAlive(env) {
  const hb = await kvGet(env, "py_alive", null);
  if (hb && Date.now() < hb.until && Date.now() - hb.at < 15 * 60e3) return true;
  // свой сервер: между 10-минутными проходами пауза в секунды (git pull, бэкап) — это не «сплю», не пугаем клиента
  if (env.WAKE_GH === "off" && Date.now() - (await kvGet(env, "py_seen", 0)) < 3 * 60e3) return true;
  return false;
}
/** Будит GitHub Actions, если Python сейчас не работает. Возвращает true, если он уже жив. */
export async function wake(env) {
  if (await pythonAlive(env)) return true;
  if (env.WAKE_GH === "off") return false;            // Python живёт на своём сервере — GitHub не будим
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
/** Сторож: Python на сервере перестал забирать очередь (сервер упал, контейнер встал) — сказать владельцу.
 *  Раз в 6 часов, пока молчит; и одно сообщение, когда снова на связи. Вызывается cron'ом каждые 15 минут. */
export const WATCH_QUIET_MIN = 25;
export async function watchdog(env, now = Date.now()) {
  if (env.WAKE_GH !== "off" || !env.OWNER_CHAT) return "skip";       // Python не на своём сервере — будим GitHub
  const seen = await kvGet(env, "py_seen", 0);
  if (!seen) return "never";
  const st = await kvGet(env, "py_watch", { down: false, at: 0 });
  const quiet = (now - seen) / 60e3;
  if (quiet >= WATCH_QUIET_MIN && (!st.down || now - st.at > 6 * 3600e3)) {
    await tg(env, "sendMessage", { chat_id: env.OWNER_CHAT, parse_mode: "HTML", _noTr: true,
      text: `⚠️ <b>Сервер Ra'no молчит ${Math.round(quiet)} мин</b> — поиск и ответы, которым нужен сервер, стоят.\n` +
        "Проверьте: <code>ssh sonar docker ps</code> и <code>docker logs rano --tail 50</code>. Как оживёт — напишу." });
    await kvSet(env, "py_watch", { down: true, at: now });
    return "alert";
  }
  if (st.down && quiet < 5) {
    await tg(env, "sendMessage", { chat_id: env.OWNER_CHAT, _noTr: true, text: "✅ Сервер Ra'no снова на связи." });
    await kvSet(env, "py_watch", { down: false, at: now });
    return "recovered";
  }
  return "ok";
}
let lastWatch = 0;                         // в памяти изолята: не чаще раза в 5 минут
function maybeWatch(env, ctx) {
  if (Date.now() - lastWatch < 5 * 60e3) return;
  lastWatch = Date.now();
  ctx.waitUntil(watchdog(env).catch(e => console.log("watchdog error", e.stack || e)));
}
// ───────────────────────────── «Обвести на карте» (Mini App из кнопки «Меню») ─────────────────────────────
const hexOf = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
/** Подпись Telegram WebApp (initData): HMAC-SHA256 ключом от токена бота. Возвращает user или null. */
export async function checkInitData(env, initData, maxAgeSec = 86400) {
  const p = new URLSearchParams(String(initData || ""));
  const hash = p.get("hash");
  if (!hash) return null;
  p.delete("hash");
  const dcs = [...p.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([k, v]) => `${k}=${v}`).join("\n");
  const enc = new TextEncoder();
  const k1 = await crypto.subtle.importKey("raw", enc.encode("WebAppData"), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const secret = await crypto.subtle.sign("HMAC", k1, enc.encode(await botToken(env)));
  const k2 = await crypto.subtle.importKey("raw", secret, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  if (hexOf(await crypto.subtle.sign("HMAC", k2, enc.encode(dcs))) !== hash) return null;
  if (Date.now() / 1000 - (+p.get("auth_date") || 0) > maxAgeSec) return null;
  try { const u = JSON.parse(p.get("user") || "null"); return u && u.id ? u : null; } catch (e) { return null; }
}
/** Области с карты: до 6 многоугольников по 4–300 точек, только Ташкент и окрестности. */
export function cleanAreas(raw) {
  if (!Array.isArray(raw)) return null;
  const out = [];
  for (const a of raw.slice(0, 6)) {
    if (!Array.isArray(a)) return null;
    const pts = a.slice(0, 300).filter(q => Array.isArray(q) && q.length >= 2)
      .map(q => [Math.round(+q[0] * 1e5) / 1e5, Math.round(+q[1] * 1e5) / 1e5])
      .filter(([la, lo]) => la > 40.9 && la < 41.7 && lo > 68.9 && lo < 69.8);
    if (pts.length >= 4) out.push(pts);
  }
  return out;
}
export const DRAW_ASK = /^\/(draw|area|areas|karta)(@\w+)?$|(нарис|обвест|обвед|очерт)\w*[^.!?\n]{0,40}(карт(?!оч)|облаc|облас)|выдел\w*[^.!?\n]{0,30}карт(?!оч)|(карт\w*)[^.!?\n]{0,20}(нарис|обвест|обвед|очерт)|xaritada\s+(chiz|belgila)|draw\s+(an?\s+)?(area|on the map)/i;
export const DRAW_HINT = {
  ru: "🗺 Нажмите «Меню» слева внизу → «Карта». Там нажмите «Обвести» и проведите пальцем по краю нужного места — " +
    "можно несколько областей. Потом «Сохранить»: буду искать только внутри них и только объявления с точным адресом. " +
    "Убрать области — там же, 🗑 и «Сохранить».",
  uz: "🗺 Pastdagi «Menu» → «Karta» ni bosing. «Chizish» ni bosing va kerakli joy chegarasi bo'ylab barmog'ingizni yurgizing — " +
    "bir nechta hudud mumkin. Keyin «Saqlash»: faqat shu hududlarda, aniq manzilli e'lonlarni qidiraman.",
  en: "🗺 Tap «Menu» at the bottom left → «Map». Tap «Draw» and trace the edge of the place with your finger — several " +
    "areas are fine. Then «Save»: I'll search only inside them, and only listings with an exact address.",
};
export const MAP_MENU = { type: "web_app", text: "Карта", web_app: { url: "" } };
export async function setMapMenu(env, chat) {
  if (!ORIGIN) return null;
  return tg(env, "setChatMenuButton", { chat_id: +chat, menu_button: { ...MAP_MENU, web_app: { url: ORIGIN + "/draw" } } });
}
export async function drawApi(env, path, body) {
  const user = await checkInitData(env, body?.initData);
  if (!user) return { ok: false, error: "auth" };
  const chat = String(user.id);
  if (!(await isClient(env, chat))) return { ok: false, error: "not_client" };
  if (path === "/draw/load") return { ok: true, areas: (await kvGet(env, "areas:" + chat, null)) || [],
    lang: await ownerLang(env, chat).catch(() => null) };
  const areas = cleanAreas(body?.areas);
  if (!areas) return { ok: false, error: "bad_areas" };
  await kvSet(env, "areas:" + chat, areas);
  // Python пересчитает поиск и сам подтвердит в чате (с числом областей и что ищет внутри)
  await queueAndWake(env, { message: { message_id: 0, chat: { id: +chat, type: "private" }, from: { id: +chat },
    date: Math.floor(Date.now() / 1000), text: "", _areas: areas } });
  return { ok: true, n: areas.length };
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
  ru: "Привет! Я Ra'no 👋 — ИИ-ассистент, которая обожает квартиры в Ташкенте: аренда и покупка.\n\n" +
    "Ищу двумя способами:\n🔎 Сначала сама — каждый день прочёсываю сайты и Telegram-каналы, выгодное приношу сразу.\n" +
    "📇 Если на сайтах пусто — подберу маклеров: нажмёте на имя, запрос уже вписан, останется «Отправить».\n" +
    "К каждому варианту — честный разбор цены: дешевле рынка или кто-то загнул 😉\n\n" +
    "С чего начнём? Напишите своими словами, что ищете, — например: «купить двушку в центре до $50 000, нужна ипотека».",
  uz: "Salom! Men Ra'no 👋 — Toshkentdagi kvartiralarni juda yaxshi ko'radigan AI-yordamchiman: ijara va sotib olish.\n\n" +
    "🔎 O'zim har kuni saytlar va Telegram-kanallarni ko'rib chiqaman, zo'r variantlarni darhol olib kelaman.\n" +
    "📇 Saytlarda bo'lmasa — maklerlarni tanlayman: ismini bosasiz, so'rov tayyor yozilgan, faqat «Yuborish»ni bosasiz.\n" +
    "Har bir variantga — halol narx tahlili 😉\n\n" +
    "Nima qidirayotganingizni yozing — masalan: «markazda 2 xonali, $50 000 gacha, ipoteka kerak».",
  en: "Hi! I'm Ra'no, an AI assistant for finding a home in Tashkent. Tell me in your own words what you're looking for — " +
    "e.g. \"buy a 2-room flat in the centre up to $50,000, mortgage needed\". No buttons to learn — just write to me.",
};
// Профиль бота: «О боте» (до 120 символов) и экран до Start (до 512) — русский по умолчанию, узбекский отдельно
export const AVATAR_URL = "https://raw.githubusercontent.com/shokhrukh-xp/rent-radar/main/docs/brand/rano_avatar_navy.jpg";
export const PROFILE = {
  name: { "": "Ra'no", uz: "Ra'no", en: "Ra'no" },
  short: {
    "": "Ra'no 👋 сама ищет квартиры в Ташкенте на сайтах и в Telegram-каналах, а если пусто — подключит маклеров.",
    uz: "Ra'no 👋 Toshkentda kvartirani saytlar va Telegram-kanallardan o'zi qidiradi, topilmasa — maklerlarni ulaydi.",
    en: "Ra'no 👋 finds flats in Tashkent herself on sites and Telegram channels, and brings in brokers if it's empty.",
  },
  long: {
    "": "Привет! Я Ra'no 👋 — ИИ-ассистент, которая обожает квартиры в Ташкенте: аренда и покупка.\n\n" +
      "🔎 Сначала ищу сама — каждый день смотрю Uybor, Realt24, Joymee, Realting, Yangiuylar и Telegram-каналы. " +
      "Выгодное приношу сразу, остальное — вечером, без повторов.\n" +
      "📇 На сайтах пусто — подберу маклеров: нажмёте на имя, запрос уже вписан, останется «Отправить».\n" +
      "📊 К каждому варианту — честный разбор цены: дешевле рынка или кто-то загнул 😉\n\n" +
      "Маклерам: жмите Start и присылайте варианты — передам клиенту сразу 🙌",
    uz: "Salom! Men Ra'no 👋 — Toshkentdagi kvartiralarni juda yaxshi ko'radigan AI-yordamchiman: ijara va sotib olish.\n\n" +
      "🔎 Avval o'zim qidiraman — har kuni Uybor, Realt24, Joymee, Realting, Yangiuylar va Telegram-kanallarni ko'raman. " +
      "Zo'rini darhol, qolganini to'plamda yuboraman.\n" +
      "📇 Saytlarda topilmasa — maklerlarni ulayman: so'rovni men tayyorlayman.\n" +
      "📊 Har bir variantga — halol narx tahlili 😉\n\n" +
      "Maklerlar uchun: Start ni bosing va variantlarni yuboring — mijozga darhol yetkazaman 🙌",
    en: "Hi! I'm Ra'no 👋 — an AI assistant who loves flats in Tashkent: renting and buying.\n\n" +
      "🔎 I search myself first — every day on Uybor, Realt24, Joymee, Realting, Yangiuylar and Telegram channels. " +
      "Great deals right away, the rest in the evening, no repeats.\n" +
      "📇 Sites empty? I'll pick brokers: tap a name, the request is already typed, just press «Send».\n" +
      "📊 Every option comes with an honest price analysis 😉\n\n" +
      "Brokers: press Start and send your options — I'll pass them to the client right away 🙌",
  },
};
const WAIT = {
  ru: "⏳ Секундочку, бужу свой «мозг» — отвечу через минуту-две.",
  uz: "⏳ Bir daqiqa, asosiy modulimni uyg'otyapman — 1–2 daqiqada javob beraman.",
  en: "⏳ Starting the main module — the reply will come in 1–2 minutes.",
};
const ivKey = chat => "iv:" + chat;

// Маклеру — ответ сразу (раньше ждал, пока проснётся Python). Не на /start и не на «здравствуйте»:
// там Python присылает знакомство с запросом клиента. Не чаще раза в 20 минут на маклера.
export const BROKER_ACK = "Здравствуйте! Я Ra'no, ИИ-ассистент — веду поиск жилья для клиента и передаю ему варианты.\n" +
  "Rahmat, получила! 🙌 Если зацепит — вернусь с вопросами. Есть ещё что-то по параметрам — присылайте.\n\n" +
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
    "Assalomu alaykum! Я Ra'no 👋, ИИ-ассистент — ищу жильё для клиента.\n" +
    (want ? `\nКлиент ищет: ${want}.\n` : "") +
    "\nПришлите подходящие варианты: фото, точный адрес, этаж, площадь, цену и комиссию — " +
    "одним сообщением или по частям, я не тороплю. Передам клиенту сразу же.\n\n" +
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
// предложили канал: ссылка t.me/имя (без номера поста) или @имя с пометкой «канал», или просто @имя
const TG_CH_LINK = /(?:https?:\/\/)?(?:t\.me|telegram\.me)\/(?:s\/)?([A-Za-z][\w]{3,31})\/?(?=$|[\s?#,;!)])/gi;
const TG_CH_AT = /(?:^|[\s(])@([A-Za-z][\w]{3,31})\b/g;
export function suggestedChannels(text, botName = "rano_smart_bot") {
  const t = String(text || "");
  if (!t || t.length > 400) return [];
  const names = [...t.matchAll(TG_CH_LINK)].map(m => m[1]);
  const ats = [...t.matchAll(TG_CH_AT)].map(m => m[1]);
  const rest = t.replace(TG_CH_AT, " ").replace(TG_CH_LINK, " ").trim();
  if (ats.length && (/канал|kanal|группа|guruh|источник|manba|channel/i.test(t) || rest.length < 3)) names.push(...ats);
  const bad = new Set([botName.toLowerCase(), "joinchat", "share", "addstickers", "proxy", "iv", "s"]);
  return [...new Set(names.filter(n => !bad.has(n.toLowerCase())))].slice(0, 5);
}
const emptyIv = () => ({ ans: {}, hist: [], sent: "", mode: "" });

export function summary(a) {
  const deal = { rent: "аренда", daily: "посуточно", buy: "покупка" }[a.deal] || "";
  const obj = { flat: "квартира", house: "дом", dacha: "дача", land: "участок" }[a.object] || "";
  const rooms = (a.rooms || []).length ? a.rooms.map(r => r === "4" ? "4+" : r).join("/") + "-комн." : "";
  const ds = a.near && /центр|markaz|center/i.test(a.near) ? "центр и рядом с ним"
    : a.near ? `до ${(a.near_km || "2").replace(".", ",")} км от «${a.near}»`
    : (a.districts || []).map(i => DISTRICTS[+i]).filter(Boolean).join(", ");
  const b = a.budget ? `до $${a.budget}${a.deal === "daily" ? "/сутки" : a.deal === "buy" ? "" : "/мес"}` : "";
  return [deal, obj, rooms, ds || (a.city === "tashkent" ? "Ташкент" : a.city_other || ""), b].filter(Boolean).join(" · ");
}

function buildPrompt(iv, text, today, ctx = null) {
  const hist = iv.hist.slice(-16).map(h => (h.r === "u" ? "Клиент: " : h.r === "s" ? "[система] " : "Ra'no: ") + h.t).join("\n");
  return `Сегодня: ${today}.\nТекущие параметры (JSON): ${JSON.stringify(iv.ans)}\n` +
    (ctx ? `Сейчас у бота (JSON): ${JSON.stringify(ctx).slice(0, 3500)}\n` : "") +
    (hist ? `История диалога:\n${hist}\n` : "") + `Новое сообщение клиента: ${text}`;
}

/** Один ход интервью без отправки в Telegram: модель → проверка → новое состояние. */
export async function interviewCore(env, iv, text, ctx = null) {
  const out = await gemini(env, SYSTEM, buildPrompt(iv, text, new Date().toISOString().slice(0, 10), ctx));
  const set = pairsToSet(out.set);
  const roomsAny = Array.isArray(set.rooms) && set.rooms.includes("any");
  iv.ans = applyPatch(iv.ans, set, out.clear);
  if (roomsAny) iv.ans.rooms_any = true; else if (set.rooms) delete iv.ans.rooms_any;
  const distAny = Array.isArray(set.districts) && set.districts.some(d => String(d).toLowerCase() === "any");
  if (distAny) iv.ans.districts_any = true; else if (set.districts) delete iv.ans.districts_any;
  const reply = String(out.reply || "").trim().slice(0, 3500) || "Расскажите, что ищем? 🙂";
  iv.hist.push({ r: "u", t: String(text).slice(0, 1000) }, { r: "a", t: reply });
  iv.hist = iv.hist.slice(-20);
  const fin = finalAns(iv.ans);
  const sig = JSON.stringify(fin);
  let ready = false;
  // iv.sent ставит тот, кто реально отправил поиск в Python (interviewTurn) — иначе готовый поиск теряется
  if (out.ready && essentialsOk(iv.ans) && sig !== iv.sent) ready = true;
  return { reply, ready, fin, sig, raw: out, via: out._via };
}

// Действия из разговора: модель выбрала intent — делаем сразу, а в историю пишем, что сделали
async function chatAction(env, chat, intent, items, ui, iv, reply) {
  const ctx = ui?.ctx || {};
  const nums = [...new Set((items || []).map(x => parseInt(x, 10)).filter(n => n > 0))].slice(0, 5);
  const note = t => { iv.hist.push({ r: "s", t }); iv.hist = iv.hist.slice(-20); };
  const queue = async data => queueAndWake(env, { callback_query: { id: "chat", data, _toast_done: true, _from_chat: true,
    message: { message_id: 0, chat: { id: +chat || chat } } } });
  if (intent === "search_now") {
    await say(env, chat, reply || "Бегу проверять сайты 🏃‍♀️ Новое пришлю через минуту-две.");
    await queue("R:check"); note("запущена проверка всех сайтов");
  } else if (intent === "show_pick") {
    await say(env, chat, reply || "Показываю 👇");
    if (ctx.pick_pending) { await queue("R:pick"); note("отправлена новая подборка"); }
    else if ((ctx.last_pick || []).length) { await queue("R:last"); note("повторно показана последняя подборка"); }
    else { await showScreen(env, chat, "/rano"); note("подборок ещё не было — показан статус поиска"); }
  } else if (intent === "show_item") {
    const list = ctx.last_pick || [];
    const pick = (nums.length ? nums : list.map(x => x.n)).filter(n => n <= list.length).slice(0, 5);
    if (!pick.length) {
      await say(env, chat, list.length ? `В последней подборке только ${list.length} — назовите номер от 1 до ${list.length} 🙂`
        : "Подборок с сайтов пока не было — как только найду, пришлю с номерами 🙂");
      note("номер не найден в подборке");
    } else {
      await say(env, chat, reply || `Показываю ${pick.join(", ")} 👇`);
      for (const n of pick) await queue(`L:v:${list[n - 1].key}`.slice(0, 64));
      note(`показаны объявления ${pick.join(", ")} из подборки с фото и разбором`);
    }
  } else if (intent === "sl_item") {
    const list = ctx.shortlist || [];
    const n = nums[0];
    if (!n || n > list.length) {
      await say(env, chat, list.length ? `В шортлисте ${list.length} — назовите номер от 1 до ${list.length} 🙂` : "Шортлист пока пуст 🙂");
    } else {
      const card = ui?.cards?.[String(list[n - 1].oid)];
      if (card) await say(env, chat, card.text, { parse_mode: "HTML", reply_markup: card.kb });
      else await queue(`s:o:${list[n - 1].oid}`);
      note(`открыт вариант ${n} из шортлиста`);
    }
  } else if (intent === "rano_status" || intent === "via_status") {
    await showScreen(env, chat, intent === "rano_status" ? "/rano" : "/via");
    note(intent === "rano_status" ? "показан статус поиска по сайтам" : "показан статус маклеров");
  } else if (intent === "shortlist") {
    await showShortlist(env, chat); note("показан шортлист");
  } else return null;
  await kvSet(env, ivKey(chat), iv);
  return intent;
}

/** Карточка, на которую ответили: по id любого сообщения альбома. */
export async function cardByMessage(env, mid, chat = null) {
  if (!mid) return null;
  const list = (await kvGet(env, cardsKey(env, chat), null)) || [];
  return [...list].reverse().find(c => (c.ids || []).includes(+mid)) || null;
}

/** Клиент своими словами говорит о квартире из присланных: ответом на её сообщение, по номеру,
 *  по описанию или сразу после неё. Одно сообщение — можно несколько действий («нравится, узнай про документы»). */
export async function cardAction(env, chat, raw, card, recent, iv, text) {
  const nums = (raw.items || []).map(x => parseInt(x, 10)).filter(n => n > 0 && n <= recent.length);
  const fresh = recent.length && Date.now() - (recent.at(-1).at || 0) < 30 * 60e3;
  // кто о какой: ответ на сообщение → номер/описание → единственная свежая карточка
  const lone = fresh && recent.filter(c => Date.now() - (c.at || 0) < 30 * 60e3).length === 1;
  const target = card || (nums[0] ? recent[nums[0] - 1] : null) || (lone ? recent.at(-1) : null);
  const note = t => { iv.hist.push({ r: "u", t: String(text).slice(0, 500) }, { r: "s", t }); iv.hist = iv.hist.slice(-20); };
  if (!target) {
    const opts = recent.slice(-3).reverse().map(c => `• ${c.line}`).join("\n");
    await say(env, chat, recent.length
      ? `Про какую квартиру? 🙂 Можно ответить на её сообщение или просто назвать — например:\n${opts}`
      : "Пока не присылала вариантов — как найду, принесу 🙂", { reply_markup: OWNER_KB });
    iv.hist.push({ r: "u", t: String(text).slice(0, 500) }, { r: "s", t: "переспросила, о какой квартире речь" });
    iv.hist = iv.hist.slice(-20); await kvSet(env, ivKey(chat), iv);
    return "card";
  }
  const [kind, ...rest] = target.ref.split(":");
  const ref = rest.join(":");
  const acts = String(raw.card_act || "").toLowerCase().split(/[,\s]+/).filter(Boolean);
  const reason = /^[pdca]$/.test(raw.reason || "") ? raw.reason : "x";
  const what = target.line ? `«${target.line}»` : "вариант";
  const queueCb = data => queueAndWake(env, { callback_query: { id: "chat", data, _toast_done: true, _from_chat: true,
    message: { message_id: 0, chat: { id: +chat || chat } } } });
  const queueMsg = extra => queueAndWake(env, { message: { message_id: 0, chat: { id: +chat || chat, type: "private" },
    from: { id: +chat || chat }, date: Math.floor(Date.now() / 1000), text: String(text).slice(0, 500), ...extra } });
  const L = kind === "L", O = kind === "O";
  const outs = [];
  for (const act of (acts.length ? acts : [""])) {
  let out = "";
  if (act === "like") { await queueCb(L ? `L:s:${ref}` : `o:like:${ref}`); out = `👍 Добавила в шортлист ${what}`; }
  else if (act === "skip") {
    await tg(env, "deleteMessages", { chat_id: chat, message_ids: target.ids });
    await queueCb(L ? `L:n:${ref}` : `o:skip:${ref}:${reason}`);
    out = `👎 Убрала ${what}` + (L ? " — похожие больше не покажу" : " — маклеру отвечу вежливо");
  } else if (act === "later") { if (O) await queueCb(`t:l:${ref}`); out = `🕐 Хорошо, ${what} — на потом`; }
  else if (act === "ask") { await queueCb(L ? `L:a:${ref}` : `o:ask:${ref}`); out = `📨 Уточняю по ${what}` + (raw.note_text ? ` (${String(raw.note_text).slice(0, 80)})` : "") + " — ответ принесу сюда"; }
  else if (act === "view") {
    const w = parseWhen(raw.note_text || text);
    if (!w) out = `Когда хотите смотреть ${what}? Напишите день и время, как удобно 🙂`;
    else {
      await queueMsg({ _view: { ...(L ? { key: ref } : { oid: +ref }), at: w.at, label: w.label, notime: w.notime } });
      out = `📅 Записала просмотр ${what}: <b>${w.label}</b>. Согласую время с маклером и передам его ответ. ` +
        `Утром в этот день напомню` + (w.notime ? "." : ", а за 2 часа — ещё раз.");
    }
  } else if (act === "note") {
    const t = String(raw.note_text || text).trim();
    await queueMsg({ _note: { ...(L ? { key: ref } : { oid: +ref }), text: t } });
    out = `📝 Записала к ${what}: ${t.slice(0, 120)}`;
  } else if ((act === "similar" || act === "search_like") && L) {
    await queueCb(`L:${act === "similar" ? "sim" : "like"}:${ref}`);
    out = act === "similar" ? "🔎 Ищу похожие — пришлю карточками" : "🎯 Настраиваю поиск на такие же";
  } else out = "Поняла, что это про " + what + " — а что с ней сделать? Скажите как есть: нравится, не то, что уточнить или когда смотреть 🙂";
  outs.push(out);
  }
  note(`с вариантом ${target.line || target.ref}: ${acts.join(", ") || "?"}`);
  await kvSet(env, ivKey(chat), iv);
  await say(env, chat, outs.join("\n"), { parse_mode: "HTML", reply_markup: OWNER_KB });
  return "card";
}

export async function interviewTurn(env, chat, text, card = null) {
  const iv = await kvGet(env, ivKey(chat), null) || emptyIv();
  tg(env, "sendChatAction", { chat_id: chat, action: "typing" }).catch(() => {});
  let res;
  const ui = await kvGet(env, uiKey(env, chat), null);
  const recent = ((await kvGet(env, cardsKey(env, chat), null)) || []).slice(-6);
  const ctx = { ...(ui?.ctx || {}), recent_cards: recent.map((c, i) => ({ n: i + 1, line: c.line })),
    ...(card ? { card: { line: card.line } } : {}) };
  try {
    res = await interviewCore(env, iv, text, ctx);
  } catch (e) {
    console.log("gemini error", e.message);
    await say(env, chat, "Ой, я запнулась 🙈 Попробуйте ещё раз через минуту.");
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
    await say(env, chat, "🔄 С чистого листа!\n\n" + reply, { reply_markup: OWNER_KB });
    return;
  }
  if (ready) {                                   // параметры собраны — поиск в Python при любом intent
    iv.sent = res.sig; iv.sentAt = Date.now();
    await kvSet(env, ivKey(chat), iv);
    await enqueue(env, { message: { chat: { id: +chat || chat, type: "private" }, from: { id: +chat || chat },
      date: Math.floor(Date.now() / 1000),
      web_app_data: { data: JSON.stringify({ v: 3, replace: true, src: "chat", ans: fin }) } } });
    if (intent !== "search") await wake(env);
  }
  if (intent === "card") return cardAction(env, chat, res.raw || {}, card, recent, iv, text);
  const act = await chatAction(env, chat, intent, res.raw?.items, ui, iv, reply);
  if (act) return act;
  if (intent === "add_offer") return startAddMode(env, chat);
  if (intent === "show_offers") {                // «покажи все / ещё» — остальные варианты маклеров
    await kvSet(env, ivKey(chat), iv);
    return showOffers(env, chat, /(все|всё|ещё|еще|остальн|hammasi|yana)/i.test(text));
  }
  if (intent === "brokers") { await kvSet(env, ivKey(chat), iv); return startOutreach(env, chat); }
  if (INTENT_CMD[intent]) {
    await kvSet(env, ivKey(chat), iv);
    await asCommand(env, chat, INTENT_CMD[intent], fin.lang === "uz" ? "uz" : "ru");
    return;
  }
  await kvSet(env, ivKey(chat), iv);
  if (ready) {
    await wake(env);
    const lang = fin.lang || "ru";
    reply += "\n\n" + (fin.deal === "daily"
      ? { ru: "📇 Посуточно быстрее всего находят маклеры — сейчас покажу текст запроса.", uz: "📇 Kunlik ijarani maklerlar tezroq topadi — so'rov matnini hozir ko'rsataman.", en: "📇 Daily rentals are fastest via brokers — I'll show the request text now." }[lang]
      : { ru: "🔎 Уже ищу сама по сайтам и каналам — первые варианты пришлю через пару минут.", uz: "🔎 Saytlar va kanallarda o'zim qidiryapman — birinchi variantlarni bir-ikki daqiqada yuboraman.", en: "🔎 Already searching sites and channels myself — first options in a couple of minutes." }[lang]);
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
  await say(env, chat, `С возвращением! 🙌 Сейчас ищем: ${summary(iv.ans)}.\n` +
    "Скажите, что поменять, — или «начать заново».", { reply_markup: OWNER_KB });
}

// ───────────────────────────── разбор обновления ─────────────────────────────
// «подключить к телеграму», «как ты будешь читать маклеров», «telegram business» — без слов про поиск квартиры
export const BIZ_ASK = /(подключ\w*|привяз\w*|привяж\w*|настро\w*|ulash|ulang)[^.!?\n]{0,40}(телеграм\w*|telegram|аккаунт\w*|akkaunt)|telegram\s*(business|для\s*бизнеса|biznes)|бизнес[- ]?(режим|аккаунт)|(читать|отвечать)[^.!?\n]{0,25}маклер\w*\s+(в|из)\s+(моих|моём|моем|мой)/i;

// «не хочу давать доступ», «без подключения» — работа через пересылку
export const BIZ_NO = /без\s+(подключени[а-яё]*\s+(к\s+)?(телеграм|аккаунт|тебя|вас)|доступа\s+(к\s+)?(телеграм|аккаунт|чат|переписк))|не\s+(хочу|буду|надо|нужно)\s+(давать\s+)?(подключ[а-яё]*|доступ[а-яё]*|давать\s+доступ)|(отключ[а-яё]*|выключ[а-яё]*)\s+(тебя\s+)?(от\s+)?(моего\s+)?(телеграм[а-яё]*|чат[а-яё]*)|ulamasdan|ulamayman/i;

export async function handleUpdate(env, upd) {
  const owner = String(env.OWNER_CHAT);
  // повтор от Telegram — по самому номеру, не «не больше последнего»: вебхук шлёт в несколько соединений,
  // и фото альбома приходят не по порядку
  if (upd.update_id) {
    const d = await db(env);
    const fresh = await d.prepare("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO NOTHING RETURNING k")
      .bind("upd:" + upd.update_id, String(Date.now())).first();
    if (!fresh) return "dup";
    if (upd.update_id % 50 === 0)
      await d.prepare("DELETE FROM kv WHERE k LIKE 'upd:%' AND CAST(v AS INTEGER) < ?").bind(Date.now() - 2 * 864e5).run();
  }

  const cb = upd.callback_query;
  if (cb) {
    const chat = String(cb.message?.chat?.id ?? "");
    if (!(await isClient(env, chat))) return "skip";
    const data = cb.data || "";
    if (data === "b" || data === "off2" || data === "q:ok") {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
      if (data === "off2") await showOffers(env, chat, true); else await startOutreach(env, chat);
      return "ui";
    }
    if (/^b[wx]:/.test(data)) {                  // рассылка по одному — следующий сразу, сохранит Python
      const st = await kvGet(env, "out", null) || { sent: 0, skipped: 0, done: [] };
      const bid = data.slice(3);
      if (!st.done.includes(bid)) { st.done.push(bid); st[data[1] === "w" ? "sent" : "skipped"] += 1; }
      await kvSet(env, "out", st);
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: data[1] === "w" ? "✅ Отмечено" : "Пропущен" });
      await tg(env, "editMessageReplyMarkup", { chat_id: chat, message_id: cb.message.message_id, reply_markup: { inline_keyboard: [] } });
      await nextBroker(env, chat);
      upd.callback_query = { ...cb, _worker_done: true };
      await queueAndWake(env, upd);
      return "outreach";
    }
    if (/^s:(o|t):\d+$/.test(data)) {           // номер в шортлисте — карточка с фото собирает Python
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "📂 Открываю…" });
      upd.callback_query = { ...cb, _toast_done: true };
      await queueAndWake(env, upd);
      return "card";
    }
    if (/^s:(clr|sort|ref|go|show|more)/.test(data)) {   // шортлист — карточками с фото, их собирает Python
      const act = data.split(":")[1];
      let toast = "📋 Открываю шортлист…";
      if (act === "go") {
        const ui = await kvGet(env, uiKey(env, chat), null);
        const n = ((ui?.sl || {}).n || {}).askable || 0;
        toast = ui?.sl && !n ? "Всех уже спросила 🙂" : `📨 Спрашиваю маклеров — ответы принесу сюда`;
        if (ui?.sl && !n) { await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: toast }); return "shortlist"; }
      }
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: toast });
      upd.callback_query = { ...cb, _toast_done: true };
      await queueAndWake(env, upd);
      return "shortlist";
    }
    if (/^o:(view|note):\d+$/.test(data)) {      // дальше — ввод текста: время просмотра или заметка
      const [, kind, oid] = data.split(":");
      const iv = (await kvGet(env, ivKey(chat), null)) || emptyIv();
      iv.mode = kind === "view" ? "await_view" : "await_note"; iv.oid = +oid; iv.modeAt = Date.now();
      await kvSet(env, ivKey(chat), iv);
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
      await say(env, chat, (kind === "view"
        ? `📅 Когда идём смотреть вариант #${oid}? Напишите день и время — например «завтра 18:00», «сб 11:30» или «12 октября в 15».`
        : `📝 Что запомнить про вариант #${oid}? Что понравилось, что смутило, о чём договорились — пишите, всё сохраню.`)
        + " Передумали — напишите «отмена».");
      return "await";
    }
    if (data === "o:cancel:0") {
      const iv = (await kvGet(env, ivKey(chat), null)) || emptyIv();
      iv.mode = ""; await kvSet(env, ivKey(chat), iv);
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Отменено" });
      await tg(env, "editMessageReplyMarkup", { chat_id: chat, message_id: cb.message.message_id, reply_markup: { inline_keyboard: [] } });
      return "cancel";
    }
    if (data === "L:x") {                        // уже нажато — кнопка-отметка
      const btns = (cb.message?.reply_markup?.inline_keyboard || []).flat();
      if (btns.some(b => /Не подходит/.test(b.text || ""))) {   // «Мимо» до обновления — убрать из чата сейчас
        const mid = cb.message.message_id;
        const r = await tg(env, "deleteMessages", { chat_id: chat, message_ids: [mid, mid - 1] });
        await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: r && r.ok ? "👎 Убрала" : "Это сообщение уже старое — Telegram не даёт мне его удалить, уберите вручную 🙏" });
        return "removed";
      }
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Уже отмечено" });
      return "noop";
    }
    if (/^L:(sim|like):/.test(data)) {                     // похожие / искать такие же — Python
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: data[2] === "s" ? "🔎 Ищу похожие…" : "🎯 Настраиваю поиск…" });
      upd.callback_query = { ...cb, _toast_done: true };
      await queueAndWake(env, upd);
      return "site";
    }
    if (/^L:v:/.test(data) || data === "R:last") {          // фото и разбор / последняя подборка — Python
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: data === "R:last" ? "Показываю 👇" : "📷 Открываю…" });
      upd.callback_query = { ...cb, _toast_done: true };
      await queueAndWake(env, upd);
      return "site";
    }
    if (/^L:[sn]:/.test(data) || data === "R:check") {     // объявление с сайта / «проверить сайты»
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id,
        text: data === "R:check" ? "🔄 Пробегусь по всем сайтам — минута-две" : data[2] === "s" ? "👍 Добавила в шортлист" : "👎 Убрала — больше не покажу" });
      let removed = false;
      if (data[2] === "n") {                     // «Мимо» — убрать из чата и анализ, и карточку над ним
        const [, ref] = data.slice(4).split("|");
        const mid = cb.message.message_id;
        let ids = [mid];
        if (ref) { const [first, n] = ref.split(".").map(Number); for (let i = 0; i < n; i++) ids.push(first + i); }
        else ids.push(mid - 1);                  // старые сообщения: карточка — сразу над анализом
        const r = await tg(env, "deleteMessages", { chat_id: chat, message_ids: ids });
        removed = !!(r && r.ok);
      }
      if (data !== "R:check" && !removed) {      // удалить нельзя (старше 48 ч) — хотя бы видимый след
        const key = data.slice(4).split("|")[0];
        const old = cb.message?.reply_markup?.inline_keyboard || [];
        let rows;
        if (data[2] === "n") rows = [[{ text: "👎 Не подходит — убрала", callback_data: "L:x" }]];
        else {
          rows = old.map(r => r.flatMap(b => {
            if (b.callback_data === data) return [{ text: /шортлист/i.test(b.text) ? "✅ В шортлисте" : b.text.replace("👍", "✅"), callback_data: "L:x" }];
            if ((b.callback_data || "").split("|")[0] === `L:n:${key}`) return [];
            return [b];
          })).filter(r => r.length);
          if (!rows.some(r => r.some(b => b.callback_data === "s:show"))) rows.push([{ text: "📋 Открыть шортлист", callback_data: "s:show" }]);
        }
        await tg(env, "editMessageReplyMarkup", { chat_id: chat, message_id: cb.message.message_id, reply_markup: { inline_keyboard: rows } });
      }
      upd.callback_query = { ...cb, _toast_done: true };
      await queueAndWake(env, upd);
      return "site";
    }
    if (/^o:(ask|rem|quiet|vclr|seen):\d+/.test(data)) {
      const p = data.split(":");
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id,
        text: OFFER_TOAST[p[1] === "seen" ? `seen:${p[3]}` : p[1]] || "Принято" });
      upd.callback_query = { ...cb, _toast_done: true };
      await queueAndWake(env, upd);
      return "offer";
    }
    if (/^t:n:\d+$/.test(data)) {               // «Мимо» — сразу спросить причину
      const oid = data.split(":")[2];
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Выберите причину" });
      await say(env, chat, `Что не так с вариантом #${oid}? Маклеру отвечу вежливо и подскажу, что искать.`,
        { reply_markup: { inline_keyboard: DECLINE.map(([c, t]) => [{ text: t, callback_data: `t:r:${oid}:${c}` }]) } });
      return "triage";
    }
    if (/^t:[slr]:/.test(data)) {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id,
        text: data[2] === "s" ? "👍 В шортлисте" : data[2] === "l" ? "🕐 Отложила" : "👎 Поняла, мимо" });
      if (data[2] !== "s") await tg(env, "editMessageReplyMarkup", { chat_id: chat, message_id: cb.message.message_id, reply_markup: { inline_keyboard: [] } });
      upd.callback_query = { ...cb, _toast_done: true };
      await queueAndWake(env, upd);
      return "triage";
    }
    if (data.startsWith("cmd:")) {               // кнопки меню «⋯ Ещё» и «Мой поиск»
      const cmd = data.slice(4);
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
      if (cmd === "/add") return (await startAddMode(env, chat), "add_mode");
      if (cmd === "/mysearch") return (await showMySearch(env, chat, "ru"), "my_search");
      if (cmd === "/rano" || cmd === "/via") return (await showScreen(env, chat, cmd), "screen");
      if (cmd === "/brokers") return (await startOutreach(env, chat), "ui");
      if (cmd === "/offers") return (await showOffers(env, chat), "ui");
      if (cmd === "/done") {
        const iv = (await kvGet(env, ivKey(chat), null)) || emptyIv();
        iv.mode = ""; await kvSet(env, ivKey(chat), iv);
        await say(env, chat, "✅ Готово! Варианты будут тут карточками.", { reply_markup: OWNER_KB });
        return "add_done";
      }
      await asCommand(env, chat, cmd);
      return "queued";
    }
    if (data === "q:again") {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "С чистого листа!" });
      await startInterview(env, chat, true, (await kvGet(env, ivKey(chat), {}))?.ans?.lang);
      return "interview";
    }
    if (data === "q:edit") {
      const iv = await kvGet(env, ivKey(chat), null) || emptyIv();
      iv.mode = "await_text"; await kvSet(env, ivKey(chat), iv);
    }
    const alive = await pythonAlive(env);
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id,
      text: alive ? "" : "Приняла — сделаю через минуту-две" });
    await queueAndWake(env, upd);
    return "queued";
  }

  // Telegram для бизнеса: клиент подключил Ra'no к своему аккаунту — чаты с маклерами разбирает Python
  if (upd.business_connection || upd.business_message || upd.edited_business_message
      || upd.deleted_business_messages) {
    await queueAndWake(env, upd);
    return "business";
  }
  const msg = upd.message || upd.edited_message;
  if (!msg) { await enqueue(env, upd); return "queued"; }
  const chat = String(msg.chat?.id ?? "");
  let client = chat === owner || await isClient(env, chat);
  if (!client && isMulti(env)) {
    // много пользователей: Start без метки маклера — новый клиент (или лист ожидания); с меткой m<id> — маклер
    // клиента <id> (ссылка из его запроса). Убранных и ждущих не обслуживаем.
    const st0 = await userStatus(env, chat);
    const sm = /^\/start(?:@\w+)?(?:\s+(\S+))?/i.exec(String(msg.text || "").trim());
    const payload = sm ? (sm[1] || "") : null;
    if (payload !== null && /^m[0-9a-z]+$/i.test(payload)) {
      const cid = String(parseInt(payload.slice(1), 36));
      if (cid && (cid === owner || (await userStatus(env, cid)) === "active")) await kvSet(env, "broker_of:" + chat, cid);
    } else if ((payload !== null && !(payload === "" && await kvGet(env, "broker_of:" + chat, null)))
               || st0 === "waitlist") {             // маклер нажал «Start» ещё раз — он маклер, не клиент
      const st = st0 === "waitlist" && payload === null ? "waitlist" : await signup(env, msg);
      if (st === "waitlist") {
        const k = "wl_note:" + chat;                   // не чаще раза в сутки
        if (Date.now() - (await kvGet(env, k, 0)) > 864e5) { await kvSet(env, k, Date.now()); await say(env, chat, WAITLIST[langOf(msg.from)]); }
        return "waitlist";
      }
      if (st === "removed") return "removed";
      client = true;
    }
    if (st0 === "removed") return "removed";
  }
  if (!client) {                                // маклер
    const forClient = isMulti(env) ? (await kvGet(env, "broker_of:" + chat, null)) || owner : owner;
    msg._client = forClient;                      // Python: чей это маклер
    const chans = !msg.photo ? suggestedChannels(msg.text) : [];
    if (chans.length) {
      await say(env, chat, `📡 Спасибо! Проверяю ${chans.map(c => "@" + c).join(", ")} — пару минут.`);
      upd.message = { ...msg, _channel: chans };
      await queueAndWake(env, upd);
      return "channel";
    }
    if (await brokerWelcome(env, msg, forClient)) {
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

  if (Object.values(BTN).includes(text) || text.startsWith("/")) {   // нажали другое — ввод даты/заметки бросили
    const ivA = await kvGet(env, ivKey(chat), null);
    if (ivA && /^await_(view|note)$/.test(ivA.mode)) { ivA.mode = ""; await kvSet(env, ivKey(chat), ivA); }
  }
  // кнопки внизу — обычный текст с подписью кнопки
  if (text === BTN.rano || /^\/(rano|sites)(@\w+)?$/i.test(text)) return (await showScreen(env, chat, "/rano"), "rano");
  if (text === BTN.via || /^\/via(@\w+)?$/i.test(text)) return (await showScreen(env, chat, "/via"), "via");
  if (text === BTN.search) return (await showMySearch(env, chat, L), "my_search");
  if (text === BTN.offers || /^\/(offers|varianty)(@\w+)?$/i.test(text)) return (await showOffers(env, chat), "ui");
  if (text === BTN.brokers || /^\/(brokers|makler|outreach)(@\w+)?$/i.test(text)) return (await startOutreach(env, chat), "ui");
  if (text === BTN.more) {
    await say(env, chat, "Кнопок больше нет — просто скажите, что нужно: «мой поиск», «шортлист», «напиши маклерам», " +
      "«цены рынка», «маклер ответил в WhatsApp», «подключить телеграм». Полный список — /help.");
    return "more";
  }

  // «хочу нарисовать на карте, где искать» — карта в кнопке «Меню»
  if (DRAW_ASK.test(text)) {
    await setMapMenu(env, chat).catch(() => null);
    await say(env, chat, DRAW_HINT[L] || DRAW_HINT.ru);
    return "draw_hint";
  }
  // «подключи к моему телеграму» — пошаговая инструкция Telegram для бизнеса (текст и проверка — в Python)
  // короткое «нет / не хочу» сразу после предложения подключить Telegram — тоже отказ от доступа
  const uiB = await kvGet(env, uiKey(env, chat), null);
  // «нет» — ответ на предложение, только если это первое сообщение после него (а не ответ на вопрос анкеты)
  const offAt = uiB?.biz_offered_at || 0;
  const justOffered = offAt && Date.now() / 1000 - offAt < 3600 && offAt > (await kvGet(env, "biz_seen:" + chat, 0));
  if (justOffered) await kvSet(env, "biz_seen:" + chat, offAt);
  if (BIZ_NO.test(text) || (justOffered && /^(нет|не|не хочу|не надо|не буду|не стоит|пока нет|не готов[а-я]*|yo['ʻ’]?q|kerak emas|xohlamayman)[.!,)]*$/i.test(text))) {
    upd.message = { ...msg, _biz_decline: true };
    await queueAndWake(env, upd);
    return "biz_decline";
  }
  if (/^\/connect(@\w+)?$/i.test(text) || BIZ_ASK.test(text)) {
    upd.message = { ...msg, _biz_guide: true };
    await queueAndWake(env, upd);
    return "biz_guide";
  }

  // вариант, который владелец пересылает или вставляет (из WhatsApp и других чатов)
  const forwarded = !!(msg.forward_origin || msg.forward_from || msg.forward_sender_name || msg.forward_from_chat);
  const ivNow = await kvGet(env, ivKey(chat), null);
  const adding = ivNow && ivNow.mode === "add" && Date.now() - (ivNow.addAt || 0) < 15 * 60e3;
  if (adding && /^(готово|всё|все|done|tayyor|bo['ʻ’]?ldi)[.!]?$/i.test(text)) {   // кнопки «Готово» больше нет
    ivNow.mode = ""; await kvSet(env, ivKey(chat), ivNow);
    await say(env, chat, "✅ Готово! Карточки соберу через минуту. Захотите что-то поменять в поиске — просто напишите.");
    return "add_mode";
  }
  if (!text.startsWith("/") && (forwarded || msg.photo || msg.document || adding)) {
    if (adding) { ivNow.addAt = Date.now(); await kvSet(env, ivKey(chat), ivNow); }
    upd.message = { ...msg, _owner_offer: true };
    await queueAndWake(env, upd, chat, WAIT[L]);
    return "owner_offer";
  }

  // предложенный канал — проверить и добавить в общую базу источников
  const chans = !forwarded && !text.startsWith("/") ? suggestedChannels(text) : [];
  if (chans.length) {
    await say(env, chat, `📡 Проверяю ${chans.map(c => "@" + c).join(", ")} — живой ли, есть ли объявления по Ташкенту…`);
    upd.message = { ...msg, _channel: chans };
    await queueAndWake(env, upd);
    return "channel";
  }
  // ссылка на объявление: открыть, разобрать, найти похожие — это делает Python
  const link = !forwarded && !text.startsWith("/") ? (text.match(/https?:\/\/[^\s<>"]+/i) || [])[0] : "";
  if (link) {
    upd.message = { ...msg, _link: link };
    await say(env, chat, "🔗 Открываю объявление — минутку…");
    await queueAndWake(env, upd);
    return "link";
  }

  if (text.startsWith("/") && chat === owner) {
    const adm = await adminCommand(env, chat, text);
    if (adm) { await say(env, chat, adm, { parse_mode: "HTML", _noTr: true }); return "admin"; }
  }
  if (text.startsWith("/")) {
    const [c0, ...rest] = text.split(/\s+/);
    const cmd = c0.toLowerCase().split("@")[0];
    const arg = rest.join(" ");
    if (cmd === "/add") return (await startAddMode(env, chat), "add_mode");
    if (cmd === "/mysearch") return (await showMySearch(env, chat, L), "my_search");
    if (cmd === "/done") {
      const iv = ivNow || emptyIv();
      iv.mode = ""; await kvSet(env, ivKey(chat), iv);
      await say(env, chat, "✅ Готово! Захотите что-то поменять в поиске — просто напишите.", { reply_markup: OWNER_KB });
      return "add_mode";
    }
    if (START_CMDS.includes(cmd) && !(cmd === "/start" && /^p/.test(arg))) {
      await startInterview(env, chat, cmd !== "/start" && cmd !== "/params", L);
      return "interview";
    }
    if (await instantText(env, chat, cmd)) return "instant";
    if (/^\/(shortlist|short)$/.test(cmd)) return (await showShortlist(env, chat), "shortlist");
    await queueAndWake(env, upd, chat, WAIT[L]);
    return "queued";
  }
  if (!text || msg.web_app_data || upd.edited_message) {
    await queueAndWake(env, upd);
    return "queued";
  }
  const iv = await kvGet(env, ivKey(chat), null);
  if (iv && /^await_(view|note)$/.test(iv.mode) && Date.now() - (iv.modeAt || 0) < 15 * 60e3) {
    const oid = iv.oid;
    if (/^(отмена|стоп|не надо)$/i.test(text)) {
      iv.mode = ""; await kvSet(env, ivKey(chat), iv);
      await say(env, chat, "Хорошо, отменила.");
      return "cancel";
    }
    if (iv.mode === "await_note") {
      iv.mode = ""; await kvSet(env, ivKey(chat), iv);
      upd.message = { ...msg, _note: { oid } };
      await queueAndWake(env, upd);
      await say(env, chat, `📝 Записала в заметки к варианту #${oid} ✍️`,
        { reply_markup: { inline_keyboard: [[{ text: "📂 Карточка", callback_data: `s:o:${oid}` }, { text: "📋 Шортлист", callback_data: "s:show" }]] } });
      return "note";
    }
    const w = parseWhen(text);
    if (!w && !/\d/.test(text) && text.split(/\s+/).length > 3) {   // это уже не про дату — обычный разговор
      iv.mode = ""; await kvSet(env, ivKey(chat), iv);
      await interviewTurn(env, chat, text);
      return "interview";
    }
    if (!w) {
      iv.modeAt = Date.now(); await kvSet(env, ivKey(chat), iv);
      await say(env, chat, "Не поняла дату 🙈 Напишите, например: «завтра 18:00», «сб 11:30» или «12 октября в 15». " +
        "Передумали — напишите «отмена».");
      return "await";
    }
    iv.mode = ""; await kvSet(env, ivKey(chat), iv);
    upd.message = { ...msg, _view: { oid, at: w.at, label: w.label, notime: w.notime } };
    await queueAndWake(env, upd);
    await say(env, chat, `📅 Записала просмотр варианта #${oid}: <b>${w.label}</b>. Согласую время с маклером и передам его ответ.\n`
      + (w.notime ? "Утром в этот день напомню." : "Утром в этот день напомню, а за 2 часа — ещё раз, чтобы точно не забыли 😉")
      + "\nПотом расскажете, как вам?", { parse_mode: "HTML" });
    return "viewing";
  }
  if (iv && iv.mode === "await_text") {          // «✏️ Изменить текст» — это для Python
    iv.mode = ""; await kvSet(env, ivKey(chat), iv);
    await queueAndWake(env, upd, chat, WAIT[L]);
    return "queued";
  }
  const card = await cardByMessage(env, msg.reply_to_message?.message_id, chat);
  await interviewTurn(env, chat, text, card);
  return card ? "card" : "interview";
}

// ───────────────────────────── HTTP ─────────────────────────────
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json" } });

export default {
  // 08:30 и 20:00 по Ташкенту — разбудить Python к утренней сводке просмотров и вечерним итогам
  async scheduled(event, env, ctx) {
    ctx.waitUntil(wake(env).catch(e => console.log("cron wake error", e.stack || e)));
    ctx.waitUntil(watchdog(env).catch(e => console.log("watchdog error", e.stack || e)));
  },
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const p = url.pathname;
    if (url.origin.startsWith("https://")) ORIGIN = url.origin;
    if (p === "/watch") {                      // GitHub Actions раз в 30 минут; ничего не раскрывает
      lastWatch = Date.now();
      return json({ ok: true, state: await watchdog(env) });
    }
    if (p !== "/svc/updates") maybeWatch(env, ctx);   // Python опрашивает очередь сам — это и есть «жив»
    if (p === "/draw" || p === "/draw/") {
      return new Response(DRAW_HTML, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-cache" } });
    }
    if ((p === "/draw/load" || p === "/draw/save") && req.method === "POST") {
      const body = await req.json().catch(() => null);
      return json(await drawApi(env, p, body));
    }
    if (p === "/map" || p === "/map/") {
      return new Response(MAP_HTML, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=600" } });
    }
    if (p === "/map.json") {
      const d = await kvGet(env, "pricemap", null);
      return new Response(JSON.stringify(d || {}), { headers: { "content-type": "application/json",
        "cache-control": "public, max-age=600", "access-control-allow-origin": "*" } });
    }
    if (p === "/tg" && req.method === "POST") {
      if (!env.TG_SECRET || req.headers.get("x-telegram-bot-api-secret-token") !== env.TG_SECRET)
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
        await kvSet(env, "py_seen", Date.now());                    // для сторожа: когда сервер был на связи
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
      if (p === "/svc/parse" && req.method === "POST") {   // Python: разобрать вариант маклера
        const body = await req.json().catch(() => ({}));
        try { return json({ ok: true, offer: await parseOffer(env, body) }); }
        catch (e) { return json({ ok: false, error: String(e.message || e) }, 502); }
      }
      if (p === "/svc/owner") return json({ chat: env.OWNER_CHAT || null });   // для .env сервера: не секрет
      if (p === "/svc/users") {                                // Python: кто клиент (этап 2)
        const users = [...(await usersMap(env)).values()].map(u => ({ chat: u.chat, status: u.status, lang: u.lang, name: u.name }));
        return json({ multi: isMulti(env), limit: await userLimit(env), users });
      }
      if (p === "/svc/link" && req.method === "POST") {    // Python: прочитать объявление по ссылке
        const body = await req.json().catch(() => ({}));
        try { return json({ ok: true, ...(await linkFacts(env, String(body.url || ""))) }); }
        catch (e) { return json({ ok: false, error: String(e.message || e) }, 502); }
      }
      if (p === "/svc/city" && req.method === "POST") {    // Python: что считается центром города (поиск в интернете)
        const body = await req.json().catch(() => ({}));
        try { return json({ ok: true, ...(await cityCenterFacts(env, String(body.city || "Ташкент").slice(0, 40))) }); }
        catch (e) { return json({ ok: false, error: String(e.message || e) }, 502); }
      }
      if (p === "/svc/repair" && req.method === "POST") {  // Python: ремонт по фото объявления
        const body = await req.json().catch(() => ({}));
        try { return json({ ok: true, repair: await repairFromPhotos(env, body) }); }
        catch (e) { return json({ ok: false, error: String(e.message || e) }, 502); }
      }
      if (p === "/svc/snapshot" && req.method === "POST") {
        const snap = await req.json().catch(() => null);
        if (!snap || !Array.isArray(snap.offers)) return json({ ok: false }, 400);
        snap.at = Date.now();
        await kvSet(env, uiKey(env, url.searchParams.get("chat") || snap.chat), snap);
        return json({ ok: true });
      }
      if (p === "/svc/cards" && req.method === "POST") {      // Python: отправил карточку-альбом
        const b = await req.json().catch(() => null);
        if (!b || !Array.isArray(b.ids) || !b.ref) return json({ ok: false }, 400);
        const ck = cardsKey(env, b.chat);
        const list = (await kvGet(env, ck, null)) || [];
        list.push({ ids: b.ids.map(Number).filter(Boolean).slice(0, 12), ref: String(b.ref).slice(0, 80),
                    line: String(b.line || "").slice(0, 140), at: Date.now() });
        await kvSet(env, ck, list.slice(-40));
        return json({ ok: true });
      }
      if (p === "/svc/pricemap" && req.method === "POST") {   // Python: сетка цен после среза рынка
        const d = await req.json().catch(() => null);
        if (!d || !Array.isArray(d.cells)) return json({ ok: false }, 400);
        await kvSet(env, "pricemap", d);
        return json({ ok: true, cells: d.cells.length });
      }
      if (p === "/svc/translate" && req.method === "POST") {   // Python: перевод текста для клиента
        const b = await req.json().catch(() => null);
        if (!b || !b.text || !TR_LANG[b.lang]) return json({ ok: false }, 400);
        return json({ ok: true, text: await translateText(env, String(b.text).slice(0, 6000), b.lang) });
      }
      if (p === "/svc/bye") { await kvSet(env, "py_alive", null); return json({ ok: true }); }
      if (p === "/svc/setup") {
        const hook = await tg(env, "setWebhook", { url: url.origin + "/tg", secret_token: env.TG_SECRET || undefined,
          allowed_updates: ["message", "edited_message", "callback_query", "business_connection",
                            "business_message", "edited_business_message", "deleted_business_messages"],
          drop_pending_updates: false });
        const me = await tg(env, "getMe", {});           // включён ли бизнес-режим в BotFather
        // кнопка меню «Параметры» (мини-апп) больше не нужна — возвращаем список команд
        const menu = await tg(env, "setChatMenuButton", { menu_button: { type: "commands" } });
        // клиенту в «Меню» — карта «где искать» (обвести пальцем); маклерам — по-прежнему список команд
        const menuOwner = await setMapMenu(env, env.OWNER_CHAT);
        const ownerCmds = [
          { command: "new", description: "🔎 Новый поиск" }, { command: "mysearch", description: "✏️ Мой поиск" },
          { command: "shortlist", description: "📋 Шортлист" }, { command: "help", description: "❓ Помощь" }];
        const cmds = await tg(env, "setMyCommands", { commands: ownerCmds, scope: { type: "chat", chat_id: +env.OWNER_CHAT } });
        const cmdsAll = await tg(env, "setMyCommands", { commands: [{ command: "start", description: "Как прислать вариант" }] });
        if (url.searchParams.get("kb")) await tg(env, "sendMessage", { chat_id: +env.OWNER_CHAT, reply_markup: OWNER_KB,
          text: "Кнопок больше нет — просто пишите. Меню ☰: новый поиск, мой поиск, шортлист, помощь. " +
                "Команды запоминать не нужно — можно и просто написать, что хотите сделать." });
        const profile = {};
        // и для ru/en явно: старые языковые описания из BotFather перекрывают общее
        for (const lang of ["", "ru", "en", "uz"]) {
          const lc = lang ? { language_code: lang } : {};
          const t = lang === "uz" ? "uz" : lang === "en" ? "en" : "";
          profile[lang || "default"] = {
            name: (await tg(env, "setMyName", { name: PROFILE.name[t], ...lc })).ok,
            short: (await tg(env, "setMyShortDescription", { short_description: PROFILE.short[t], ...lc })).ok,
            long: (await tg(env, "setMyDescription", { description: PROFILE.long[t], ...lc })).ok,
            now: (await tg(env, "getMyShortDescription", lc)).result?.short_description,
          };
        }
        const info = await tg(env, "getWebhookInfo", {});
        return json({ hook, menu, menuOwner, cmds, cmdsAll, profile, info: info.result,
                     business_mode: !!me.result?.can_connect_to_business });
      }
      if (p === "/svc/avatar") {         // аватар бота (Bot API 9.4 setMyProfilePhoto) из публичного репо
        const src = url.searchParams.get("src") || AVATAR_URL;
        const img = await fetch(src);
        if (!img.ok) return json({ ok: false, error: `картинка не скачалась: ${img.status}` });
        const form = new FormData();
        form.append("photo", JSON.stringify({ type: "static", photo: "attach://avatar" }));
        form.append("avatar", new Blob([await img.arrayBuffer()], { type: "image/jpeg" }), "avatar.jpg");
        const r = await fetch(`https://api.telegram.org/bot${await botToken(env)}/setMyProfilePhoto`, { method: "POST", body: form });
        const res = await r.json().catch(() => ({ ok: false }));
        return json({ ok: res.ok, description: res.description });
      }
      if (p === "/svc/try") {            // проверка промпта вживую, без Telegram и очереди
        if (url.searchParams.get("reset")) await kvSet(env, "iv:test", emptyIv());
        const iv = await kvGet(env, "iv:test", null) || emptyIv();
        const ctx = url.searchParams.get("ctx") ? JSON.parse(url.searchParams.get("ctx")) : ((await kvGet(env, uiKey(env, null), null))?.ctx || null);
        const res = await interviewCore(env, iv, url.searchParams.get("text") || "", ctx);
        await kvSet(env, "iv:test", iv);
        return json({ ...res, intent: res.raw?.intent, items: res.raw?.items, ans: iv.ans });
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

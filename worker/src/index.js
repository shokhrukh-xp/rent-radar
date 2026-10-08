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
  if (out.city !== "tashkent") { out.districts = []; delete out.districts_any; }
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
note: короткая фраза для маклеров о том, для чего нет своего поля: «нужна ипотека»,
  «рассрочка», «ближе к центру», «рядом со школой». Пиши её целиком заново (с прежним содержимым),
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
  market — цены рынка; request_text — показать текст запроса маклерам; help — что ты умеешь.
  Для всего, кроме search и restart, set оставь пустым, а reply — одной короткой фразой-подводкой
  («Показываю 👇», «Бегу проверять сайты 🏃‍♀️»): само действие выполнит система сразу после твоего ответа.

Что у тебя есть (блок «Сейчас у бота» в запросе) — это правда, опирайся ТОЛЬКО на неё:
  last_pick — последняя подборка с сайтов с номерами, как их видел клиент; shortlist — шортлист с номерами;
  pick_pending — сколько новых ждут подборки; today — сколько объявлений просмотрела и подошло сегодня.
  Никогда не говори, что что-то показала, отправила, нашла или «открываю», если этого нет в данных или
  если это не делает выбранный intent. Если клиент ссылается на то, чего нет (номер больше списка,
  подборок ещё не было) — честно скажи и предложи, что можно сделать.
  Фото есть у объявлений с сайтов: их показывает show_item. Ты сама картинки не видишь.

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
    intent: { type: "string", description: "search | restart | search_now | show_pick | show_item | rano_status | via_status | show_offers | shortlist | sl_item | brokers | add_offer | market | help | request_text" },
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

// ───────────────────────────── разбор варианта маклера ─────────────────────────────
export const OFFER_KEYS = ["is_offer", "deal", "price", "currency", "price_period", "rooms", "area", "floor",
  "floors_total", "district", "address", "landmark", "repair", "building", "furniture", "commission",
  "mortgage", "documents", "summary"];
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
address: улица/массив/ЖК/дом, как написано; landmark: ориентир (метро, школа, ТЦ)
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
  t = /\d,\d{3}(\D|$)/.test(t) ? t.replace(/,(?=\d{3}(\D|$))/g, "") : t.replace(",", ".");
  const m = t.match(/\d+(?:\.\d+)?/);
  return m ? +m[0] * mult : NaN;
}

function normOfferFacts(facts) {
  const o = {};
  for (const f of Array.isArray(facts) ? facts : []) {
    const k = String(f?.k || "").trim(), v = String(f?.v ?? "").trim();
    if (!OFFER_KEYS.includes(k) || !v || /^(null|none|нет данных|-)$/i.test(v)) continue;
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
  for (const id of photos.slice(0, 2)) { try { const im = await tgPhoto(env, id); if (im) images.push(im); } catch (e) {} }
  if (!String(text).trim() && !images.length) return { is_offer: false };
  const out = await gemini(env, OFFER_SYSTEM,
    (deal ? `Клиент ищет: ${deal === "sale" ? "покупку" : "аренду"}.\n` : "") + `Сообщение маклера:\n${String(text).slice(0, 3000) || "(только фото)"}`,
    { schema: OFFER_SCHEMA, images, temperature: 0.1 });
  return normOfferFacts(out.facts);
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
export const BTN = { rano: "🔎 Ищет Ra'no", via: "📇 Через маклеров", more: "⋯ Ещё",
  search: "🔎 Мой поиск", offers: "🏠 Варианты", brokers: "📇 Маклерам",      // старые кнопки — тоже понимаем
  what: "🏠 Что ищет клиент" };
export const OWNER_KB = { keyboard: [[{ text: BTN.rano }, { text: BTN.via }], [{ text: BTN.more }]],
  resize_keyboard: true, is_persistent: true,
  input_field_placeholder: "Напишите, что ищете, или перешлите вариант" };
export const BROKER_KB = { keyboard: [[{ text: BTN.what }]], resize_keyboard: true, is_persistent: true,
  input_field_placeholder: "Пришлите вариант: фото, адрес, этаж, цена" };
const MORE_MENU = { inline_keyboard: [
  [{ text: "✏️ Что ищем — посмотреть и поменять", callback_data: "cmd:/mysearch" }],
  [{ text: "💬 Маклер ответил мне в WhatsApp", callback_data: "cmd:/add" }],
  [{ text: "📋 Шортлист", callback_data: "cmd:/shortlist" }, { text: "📊 Цены рынка", callback_data: "cmd:/rynok" }],
  [{ text: "🏷 Поиск по сайтам", callback_data: "cmd:/rano" }, { text: "📝 Текст запроса", callback_data: "cmd:/request" }],
  [{ text: "🔄 Начать поиск заново", callback_data: "q:again" }],
  [{ text: "❓ Как это работает", callback_data: "cmd:/help" }],
] };
// намерения из обычных фраз → что делает Python
const INTENT_CMD = { show_offers: "/offers", shortlist: "/shortlist", brokers: "/brokers",
  market: "/rynok", help: "/help", request_text: "/request", sale_search: "/rano" };

// Готовые тексты из снимка (цены рынка, поиск на Uybor, текст запроса, справка) — сразу.
async function instantText(env, chat, cmd) {
  const ui = await kvGet(env, "ui", null);
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
  const scr = ((await kvGet(env, "ui", null)) || {}).screens?.[cmd];
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
  const ui = await kvGet(env, "ui", null);
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
  if (cmd === "/shortlist") { const ui = await kvGet(env, "ui", null); if (ui && ui.sl) return showShortlist(env, chat); }
  // синтетическое сообщение-команда от владельца — Python обработает как набранную
  await queueAndWake(env, { message: { message_id: 0, chat: { id: +chat || chat, type: "private" },
    from: { id: +chat || chat }, date: Math.floor(Date.now() / 1000), text: cmd } }, chat, WAIT[L]);
}

async function showMySearch(env, chat, L) {
  const iv = await kvGet(env, ivKey(chat), null);
  if (!iv || !essentialsOk(iv.ans || {})) return startInterview(env, chat, false, L);
  const note = iv.ans.note ? `\nПожелания: ${iv.ans.note}` : "";
  await say(env, chat, `🔎 Сейчас ищем: ${summary(iv.ans)}.${note}\n\n` +
    "Хотите что-то поменять — просто скажите, например: «бюджет 60 тысяч» или «добавь Юнусабад». Я не обижусь 😉", {
    reply_markup: { inline_keyboard: [
      [{ text: "📇 Разослать маклерам", callback_data: "cmd:/brokers" }, { text: "📝 Текст запроса", callback_data: "cmd:/request" }],
      [{ text: "🔄 Начать поиск заново", callback_data: "q:again" }]] } });
}

// ── «Варианты» и «Маклерам» — сразу, из снимка, который присылает Python ──
const DECLINE = [["p", "💸 Дорого"], ["d", "📍 Район"], ["c", "🛠 Состояние"], ["a", "📐 Площадь/планировка"], ["x", "Без причины"]];
const triageKb = oid => ({ inline_keyboard: [[
  { text: "👍 В шортлист", callback_data: `t:s:${oid}` }, { text: "🕐 Позже", callback_data: `t:l:${oid}` },
  { text: "👎 Мимо", callback_data: `t:n:${oid}` }]] });

async function pendingBrokerMsgs(env) {
  const r = await (await db(env)).prepare("SELECT upd FROM queue").all();
  const owner = String(env.OWNER_CHAT);
  return (r.results || []).filter(x => {
    try { const u = JSON.parse(x.upd); const m = u.message || u.edited_message; return m && String(m.chat?.id) !== owner && !m._welcomed; }
    catch (e) { return false; }
  }).length;
}

export async function showOffers(env, chat, all = false) {
  const ui = await kvGet(env, "ui", null);
  if (!ui) return asCommand(env, chat, all ? "/offers" : "/offers");
  const waiting = await pendingBrokerMsgs(env);
  const waitNote = waiting ? `\n\n⏳ Ещё ${waiting} сообщ. от маклеров разбираю — карточки будут через минуту-две.` : "";
  if (waiting) await wake(env);
  if (!ui.offers_total) {
    const rows = [];
    if (ui.shortlist) rows.push([{ text: `📋 Шортлист (${ui.shortlist})`, callback_data: "s:show" }]);
    rows.push([{ text: ui.written ? "📇 Написать ещё маклерам" : "📇 Разослать запрос маклерам", callback_data: "b" }]);
    const hint = ui.written
      ? `\nВы написали ${ui.written} маклерам — как ответят, принесу их варианты сюда карточками.\n` +
        "Если кто-то ответил вам в WhatsApp — перешлите мне, сделаю такую же карточку с разбором цены."
      : "\nЧтобы они появились, давайте разошлём запрос маклерам — это пара нажатий.";
    await say(env, chat, (waiting ? "Пока тихо — новых карточек нет 🌙" : "Пока тихо — новых вариантов нет 🌙") +
      (ui.shortlist ? ` В шортлисте — ${ui.shortlist}.` : "") + hint + waitNote, { reply_markup: { inline_keyboard: rows } });
    return;
  }
  const batch = all ? ui.offers.length : Math.max(1, ui.free || 2);
  for (const o of ui.offers.slice(0, batch)) {
    if (o.photos && o.photos.length) {
      const media = o.photos.map((f, i) => ({ type: "photo", media: f, ...(i === 0 ? { caption: o.text.slice(0, 1000), parse_mode: "HTML" } : {}) }));
      await tg(env, "sendMediaGroup", { chat_id: chat, media });
      await say(env, chat, "Ну как вам? 👀", { reply_markup: triageKb(o.oid) });
    } else {
      await say(env, chat, o.text, { parse_mode: "HTML", reply_markup: triageKb(o.oid) });
    }
  }
  const rest = ui.offers_total - Math.min(batch, ui.offers_total);
  if (rest > 0) await say(env, chat, `Маклеры прислали ещё <b>${rest}</b> — показываю?`, { parse_mode: "HTML",
    reply_markup: { inline_keyboard: [[{ text: `Показать ещё ${rest} →`, callback_data: "off2" }]] } });
  else if (waitNote) await say(env, chat, waitNote.trim());
}

export async function startOutreach(env, chat) {
  const ui = await kvGet(env, "ui", null);
  const iv = await kvGet(env, ivKey(chat), null);
  // нет снимка или запрос поменялся после него — ссылки со старым текстом слать нельзя
  if (!ui || (iv && iv.sentAt && iv.sentAt > (ui.at || 0))) return asCommand(env, chat, "/brokers");
  if (!ui.brokers || !ui.brokers.length) { await say(env, chat, ui.brokers_empty || "Маклеров под ваш запрос пока не нашла — ищу дальше."); return; }
  await kvSet(env, "out", { sent: 0, skipped: 0, done: [], at: Date.now() });
  await say(env, chat, ui.header, { parse_mode: "HTML" });
  await nextBroker(env, chat);
}

async function nextBroker(env, chat) {
  const ui = await kvGet(env, "ui", null) || {};
  const st = await kvGet(env, "out", null) || { sent: 0, skipped: 0, done: [] };
  const left = (ui.brokers || []).filter(b => !st.done.includes(b.bid));
  if (!left.length) {
    const more = (ui.brokers_total || 0) - st.done.length;
    if (more > 0) {                                   // в снимке кончились — Python пришлёт ещё
      await wake(env);
      await say(env, chat, `Ещё ${more} маклеров — подгружаю следующую порцию, это минута-две. Загляните в «📇 Через маклеров» чуть позже.`);
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
    "цена, район, разбор, кнопки «В шортлист / Мимо» — всё в одном месте.",
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
  ru: "Привет! Я Ra'no 👋 — ИИ-ассистент, которая обожает квартиры в Ташкенте: аренда и покупка.\n\n" +
    "Ищу двумя способами:\n🔎 Сначала сама — каждый день прочёсываю сайты и Telegram-каналы, выгодное приношу сразу.\n" +
    "📇 Если на сайтах пусто — подключу маклеров: составлю запрос, вы отправите его в пару нажатий.\n" +
    "К каждому варианту — честный разбор цены: дешевле рынка или кто-то загнул 😉\n\n" +
    "С чего начнём? Напишите своими словами, что ищете, — например: «купить двушку в центре до $50 000, нужна ипотека».",
  uz: "Salom! Men Ra'no 👋 — Toshkentdagi kvartiralarni juda yaxshi ko'radigan AI-yordamchiman: ijara va sotib olish.\n\n" +
    "🔎 O'zim har kuni saytlar va Telegram-kanallarni ko'rib chiqaman, zo'r variantlarni darhol olib kelaman.\n" +
    "📇 Saytlarda bo'lmasa — maklerlarni ulayman: so'rov tuzaman, siz uni bir-ikki bosishda yuborasiz.\n" +
    "Har bir variantga — halol narx tahlili 😉\n\n" +
    "Nima qidirayotganingizni yozing — masalan: «markazda 2 xonali, $50 000 gacha, ipoteka kerak».",
  en: "Hi! I'm Ra'no, an AI assistant for finding a home in Tashkent. Tell me in your own words what you're looking for — " +
    "e.g. \"buy a 2-room flat in the centre up to $50,000, mortgage needed\". Everything else is in the buttons below.",
};
// Профиль бота: «О боте» (до 120 символов) и экран до Start (до 512) — русский по умолчанию, узбекский отдельно
export const AVATAR_URL = "https://raw.githubusercontent.com/shokhrukh-xp/rent-radar/main/docs/brand/rano_avatar_navy.jpg";
export const PROFILE = {
  name: { "": "Ra'no · поиск жилья", uz: "Ra'no · uy qidirish" },
  short: {
    "": "Ra'no 👋 сама ищет квартиры в Ташкенте на сайтах и в Telegram-каналах, а если пусто — подключит маклеров.",
    uz: "Ra'no 👋 Toshkentda kvartirani saytlar va Telegram-kanallardan o'zi qidiradi, topilmasa — maklerlarni ulaydi.",
  },
  long: {
    "": "Привет! Я Ra'no 👋 — ИИ-ассистент, которая обожает квартиры в Ташкенте: аренда и покупка.\n\n" +
      "🔎 Сначала ищу сама — каждый день смотрю Uybor, Realt24, Joymee, Realting, Yangiuylar и Telegram-каналы. " +
      "Выгодное приношу сразу, остальное — подборкой, без повторов.\n" +
      "📇 На сайтах пусто — подключу маклеров: запрос готовлю я, вы отправляете в пару нажатий.\n" +
      "📊 К каждому варианту — честный разбор цены: дешевле рынка или кто-то загнул 😉\n\n" +
      "Маклерам: жмите Start и присылайте варианты — передам клиенту сразу 🙌",
    uz: "Salom! Men Ra'no 👋 — Toshkentdagi kvartiralarni juda yaxshi ko'radigan AI-yordamchiman: ijara va sotib olish.\n\n" +
      "🔎 Avval o'zim qidiraman — har kuni Uybor, Realt24, Joymee, Realting, Yangiuylar va Telegram-kanallarni ko'raman. " +
      "Zo'rini darhol, qolganini to'plamda yuboraman.\n" +
      "📇 Saytlarda topilmasa — maklerlarni ulayman: so'rovni men tayyorlayman.\n" +
      "📊 Har bir variantga — halol narx tahlili 😉\n\n" +
      "Maklerlar uchun: Start ni bosing va variantlarni yuboring — mijozga darhol yetkazaman 🙌",
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
const emptyIv = () => ({ ans: {}, hist: [], sent: "", mode: "" });

export function summary(a) {
  const deal = { rent: "аренда", daily: "посуточно", buy: "покупка" }[a.deal] || "";
  const obj = { flat: "квартира", house: "дом", dacha: "дача", land: "участок" }[a.object] || "";
  const rooms = (a.rooms || []).length ? a.rooms.map(r => r === "4" ? "4+" : r).join("/") + "-комн." : "";
  const ds = (a.districts || []).map(i => DISTRICTS[+i]).filter(Boolean).join(", ");
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
  if (out.ready && essentialsOk(iv.ans) && sig !== iv.sent) { iv.sent = sig; ready = true; }
  return { reply, ready, fin, raw: out, via: out._via };
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

export async function interviewTurn(env, chat, text) {
  const iv = await kvGet(env, ivKey(chat), null) || emptyIv();
  tg(env, "sendChatAction", { chat_id: chat, action: "typing" }).catch(() => {});
  let res;
  const ui = await kvGet(env, "ui", null);
  try {
    res = await interviewCore(env, iv, text, ui?.ctx || null);
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
  const act = await chatAction(env, chat, intent, res.raw?.items, ui, iv, reply);
  if (act) return act;
  if (intent === "add_offer") return startAddMode(env, chat);
  if (intent === "show_offers") { await kvSet(env, ivKey(chat), iv); return showOffers(env, chat); }
  if (intent === "brokers") { await kvSet(env, ivKey(chat), iv); return startOutreach(env, chat); }
  if (INTENT_CMD[intent]) {
    await kvSet(env, ivKey(chat), iv);
    await asCommand(env, chat, INTENT_CMD[intent], fin.lang === "uz" ? "uz" : "ru");
    return;
  }
  await kvSet(env, ivKey(chat), iv);
  if (ready) {
    iv.sentAt = Date.now(); await kvSet(env, ivKey(chat), iv);
    await enqueue(env, { message: { chat: { id: +chat || chat, type: "private" }, from: { id: +chat || chat },
      date: Math.floor(Date.now() / 1000),
      web_app_data: { data: JSON.stringify({ v: 3, replace: true, src: "chat", ans: fin }) } } });
    const alive = await wake(env);
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
    "Скажите, что поменять, — или «⋯ Ещё» → «Начать поиск заново».", { reply_markup: OWNER_KB });
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
    if (/^s:(o|t):\d+$/.test(data)) {           // номер в шортлисте — карточка варианта из снимка
      const oid = data.split(":")[2];
      const card = ((await kvGet(env, "ui", null)) || {}).cards?.[oid];
      if (card) {
        await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
        const r = await tg(env, "editMessageText", { chat_id: chat, message_id: cb.message.message_id, text: card.text,
          parse_mode: "HTML", reply_markup: card.kb });
        if (!r || r.ok === false) await say(env, chat, card.text, { parse_mode: "HTML", reply_markup: card.kb });
        return "card";
      }
    }
    if (/^s:(clr|sort|ref|go|show)/.test(data) && (await kvGet(env, "ui", null))?.sl) {
      const st = await kvGet(env, "sl", null) || { sort: "n" };
      const act = data.split(":")[1];
      let toast = "";
      if (act === "sort") { st.sort = SL_ORDER[(SL_ORDER.indexOf(st.sort) + 1) % 3]; toast = "Сортировка изменена"; }
      else if (act === "go") {
        const ui = await kvGet(env, "ui", null);
        const n = (ui.sl[st.sort] || ui.sl.n).askable || 0;
        if (!n) toast = "Всех уже спросила 🙂";
        else {
          upd.callback_query = { ...cb, _toast_done: true };
          await queueAndWake(env, upd);
          toast = `📨 Спрашиваю ${n} маклеров — ответы принесу сюда`;
        }
      } else toast = act === "show" ? "" : "Обновлено";
      await kvSet(env, "sl", { sort: st.sort || "n" });
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: toast });
      if (act !== "go") await showShortlist(env, chat, act === "show" ? null : cb.message.message_id);
      return "shortlist";
    }
    if (/^o:(view|note):\d+$/.test(data)) {      // дальше — ввод текста: время просмотра или заметка
      const [, kind, oid] = data.split(":");
      const iv = (await kvGet(env, ivKey(chat), null)) || emptyIv();
      iv.mode = kind === "view" ? "await_view" : "await_note"; iv.oid = +oid; iv.modeAt = Date.now();
      await kvSet(env, ivKey(chat), iv);
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
      await say(env, chat, kind === "view"
        ? `📅 Когда идём смотреть вариант #${oid}? Напишите день и время — например «завтра 18:00», «сб 11:30» или «12 октября в 15».`
        : `📝 Что запомнить про вариант #${oid}? Что понравилось, что смутило, о чём договорились — пишите, всё сохраню.`,
        { reply_markup: { inline_keyboard: [[{ text: "Отмена", callback_data: "o:cancel:0" }]] } });
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
    await say(env, chat, "Что ещё умею:", { reply_markup: MORE_MENU });
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
      await say(env, chat, "✅ Готово! Захотите что-то поменять в поиске — просто напишите.", { reply_markup: OWNER_KB });
      return "add_mode";
    }
    if (START_CMDS.includes(cmd) && !(cmd === "/start" && /^p/.test(arg))) {
      await startInterview(env, chat, cmd !== "/start" && cmd !== "/params", L);
      return "interview";
    }
    if (await instantText(env, chat, cmd)) return "instant";
    if (/^\/(shortlist|short)$/.test(cmd) && (await kvGet(env, "ui", null))?.sl) return (await showShortlist(env, chat), "shortlist");
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
      await say(env, chat, "Не поняла дату 🙈 Напишите, например: «завтра 18:00», «сб 11:30» или «12 октября в 15».",
        { reply_markup: { inline_keyboard: [[{ text: "Отмена", callback_data: "o:cancel:0" }]] } });
      return "await";
    }
    iv.mode = ""; await kvSet(env, ivKey(chat), iv);
    upd.message = { ...msg, _view: { oid, at: w.at, label: w.label, notime: w.notime } };
    await queueAndWake(env, upd);
    await say(env, chat, `📅 Записала просмотр варианта #${oid}: <b>${w.label}</b>.\n`
      + (w.notime ? "Утром в этот день напомню." : "Утром в этот день напомню, а за 2 часа — ещё раз, чтобы точно не забыли 😉")
      + "\nПотом расскажете, как вам?",
      { parse_mode: "HTML", reply_markup: { inline_keyboard: [[{ text: "📂 Карточка", callback_data: `s:o:${oid}` }, { text: "📋 Шортлист", callback_data: "s:show" }]] } });
    return "viewing";
  }
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
  // 08:30 и 20:00 по Ташкенту — разбудить Python к утренней сводке просмотров и вечерним итогам
  async scheduled(event, env, ctx) {
    ctx.waitUntil(wake(env).catch(e => console.log("cron wake error", e.stack || e)));
  },
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
      if (p === "/svc/parse" && req.method === "POST") {   // Python: разобрать вариант маклера
        const body = await req.json().catch(() => ({}));
        try { return json({ ok: true, offer: await parseOffer(env, body) }); }
        catch (e) { return json({ ok: false, error: String(e.message || e) }, 502); }
      }
      if (p === "/svc/snapshot" && req.method === "POST") {
        const snap = await req.json().catch(() => null);
        if (!snap || !Array.isArray(snap.offers)) return json({ ok: false }, 400);
        snap.at = Date.now();
        await kvSet(env, "ui", snap);
        return json({ ok: true });
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
        if (url.searchParams.get("kb")) await tg(env, "sendMessage", { chat_id: +env.OWNER_CHAT, reply_markup: OWNER_KB,
          text: "Кнопки — внизу: «🔎 Ищет Ra'no», «📇 Через маклеров», «⋯ Ещё». " +
                "Команды запоминать не нужно — можно и просто написать, что хотите сделать." });
        const profile = {};
        // и для ru/en явно: старые языковые описания из BotFather перекрывают общее
        for (const lang of ["", "ru", "en", "uz"]) {
          const lc = lang ? { language_code: lang } : {};
          const t = lang === "uz" ? "uz" : "";
          profile[lang || "default"] = {
            name: (await tg(env, "setMyName", { name: PROFILE.name[t], ...lc })).ok,
            short: (await tg(env, "setMyShortDescription", { short_description: PROFILE.short[t], ...lc })).ok,
            long: (await tg(env, "setMyDescription", { description: PROFILE.long[t], ...lc })).ok,
            now: (await tg(env, "getMyShortDescription", lc)).result?.short_description,
          };
        }
        const info = await tg(env, "getWebhookInfo", {});
        return json({ hook, menu, menuOwner, cmds, cmdsAll, profile, info: info.result });
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
        const ctx = url.searchParams.get("ctx") ? JSON.parse(url.searchParams.get("ctx")) : ((await kvGet(env, "ui", null))?.ctx || null);
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

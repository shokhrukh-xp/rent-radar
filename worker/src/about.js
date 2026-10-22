// Ra'no · «Кому служит Ra'no»: публичная страница — что делает помощник, чего не делает, кто платит.
// Для клиентов, маклеров и для проверки регулятором. Открывается по адресу воркера /about (и /about?lang=uz, /about?lang=en).

const RU = {
  title: "Ra'no — личный ИИ-помощник для поиска жилья",
  lead: "Ra'no работает только на вас — на того, кто ищет квартиру, чтобы снять или купить. Она не риэлтор и не заменяет риэлтора: это помощник, который делает за вас рутину — ищет, сравнивает, пишет и напоминает. Решения принимаете вы.",
  does: ["Просматривает открытые объявления на сайтах и в Telegram-каналах по вашим условиям — районам, цене, комнатам или области, которую вы обвели на карте.",
    "Показывает факты: где квартира и насколько точен адрес, сколько стоят похожие рядом, как давно висит объявление, сколько объявлений у продавца.",
    "По вашей просьбе пишет продавцам и маклерам — и всегда представляется: «ИИ-ассистент, ищу квартиру для своего клиента» с вашим именем. Из вашего Telegram пишет от вашего имени.",
    "Ведёт ваш список понравившихся вариантов: что ответил продавец, когда просмотр, не сняли ли объявление."],
  doesnt: ["Не оказывает риэлторских услуг и не посредничает в сделках.",
    "Не представляет продавцов, арендодателей, маклеров и риэлторов, не получает от них денег и никого не продвигает — в выдаче нет платных мест.",
    "Не имеет своих объявлений и эксклюзивов.",
    "Не участвует в сделке: не подписывает договоры, не принимает и не передаёт деньги.",
    "Не притворяется человеком: всегда говорит, что она ИИ-ассистент."],
  money: "Сейчас Ra'no бесплатна. Если станет платной — платить будете только вы и только за помощника (фиксированная сумма), никогда не процент от сделки и никогда не продавец или маклер.",
  law: "С 8 ноября 2026 года риэлторские услуги в Узбекистане вправе оказывать только организации из Единого реестра участников рынка, а риэлторы — с квалификационным сертификатом. Если работаете с маклером или риэлтором, спросите сертификат и договор; Ra'no подскажет, что проверить.",
  data: "Ra'no хранит ваши условия поиска, список вариантов и переписку с продавцами, которую вы ей поручили. Данные не продаются и не передаются третьим лицам. Чтобы удалить свои данные, напишите в бота «удали мои данные».",
  who: "Сервис разрабатывает Xplorence (Ташкент).",
  h: ["Что делает", "Чего не делает", "Кто платит", "Маклеры и риэлторы", "Ваши данные"], open: "Открыть Ra'no в Telegram",
};
const UZ = {
  title: "Ra'no — uy-joy qidirish uchun shaxsiy sun'iy intellekt yordamchisi",
  lead: "Ra'no faqat siz uchun ishlaydi — kvartira ijaraga olmoqchi yoki sotib olmoqchi bo'lgan odam uchun. U rieltor emas va rieltorning o'rnini bosmaydi: bu sizning o'rningizga qidiradigan, solishtiradigan, yozadigan va eslatadigan yordamchi. Qarorni siz qabul qilasiz.",
  does: ["Saytlar va Telegram kanallaridagi ochiq e'lonlarni sizning shartlaringiz bo'yicha ko'rib chiqadi.",
    "Faktlarni ko'rsatadi: kvartira qayerda, manzil qanchalik aniq, yaqindagi o'xshashlari qancha turadi, e'lon qachondan beri turibdi.",
    "Iltimosingiz bilan sotuvchi va maklerlarga yozadi va doim o'zini tanishtiradi: «sun'iy intellekt yordamchisiman, mijozim uchun kvartira qidiryapman» — ismingiz bilan.",
    "Yoqqan variantlar ro'yxatini yuritadi: sotuvchi nima dedi, ko'rish qachon, e'lon olib tashlanmadimi."],
  doesnt: ["Rieltorlik xizmatlarini ko'rsatmaydi va bitimlarda vositachilik qilmaydi.",
    "Sotuvchi, ijaraga beruvchi, makler va rieltorlarni vakillik qilmaydi, ulardan pul olmaydi va hech kimni targ'ib qilmaydi.",
    "O'z e'lonlari va eksklyuzivlari yo'q.",
    "Bitimda qatnashmaydi: shartnoma imzolamaydi, pul qabul qilmaydi va o'tkazmaydi.",
    "O'zini odam deb ko'rsatmaydi."],
  money: "Hozir Ra'no bepul. Pullik bo'lsa — faqat siz va faqat yordamchi uchun to'laysiz (qat'iy summa), hech qachon bitimdan foiz emas.",
  law: "2026-yil 8-noyabrdan rieltorlik xizmatlarini faqat Yagona reestrdagi tashkilotlar ko'rsatishi mumkin. Makler yoki rieltor bilan ishlasangiz, sertifikat va shartnomani so'rang.",
  data: "Ra'no qidiruv shartlaringiz, variantlar ro'yxati va topshirgan yozishmalaringizni saqlaydi. Ma'lumotlar sotilmaydi. O'chirish uchun botga «ma'lumotlarimni o'chir» deb yozing.",
  who: "Xizmatni Xplorence (Toshkent) ishlab chiqadi.",
  h: ["Nima qiladi", "Nima qilmaydi", "Kim to'laydi", "Makler va rieltorlar", "Ma'lumotlaringiz"], open: "Ra'noni Telegramda ochish",
};
const EN = {
  title: "Ra'no — a personal AI assistant for finding a home",
  lead: "Ra'no works only for you — the person looking for a flat to rent or buy. She is not a realtor and does not replace one: she is an assistant who does the routine for you — searching, comparing, writing and reminding. You make the decisions.",
  does: ["Checks open listings on websites and Telegram channels against your terms — districts, price, rooms or an area you drew on the map.",
    "Shows facts: where the flat is and how exact the address is, what similar flats nearby cost, how long the listing has been up, how many listings the seller has.",
    "On your request writes to sellers and brokers in Russian or Uzbek — and always introduces herself as an AI assistant searching for her client, with your name.",
    "Keeps your list of liked options: what the seller replied, when the viewing is, whether the listing is still up."],
  doesnt: ["Does not provide realtor services and does not mediate deals.",
    "Does not represent sellers, landlords, brokers or realtors, takes no money from them and promotes no one — there are no paid placements.",
    "Has no listings or exclusives of her own.",
    "Takes no part in the deal: signs no contracts, receives or passes on no money.",
    "Never pretends to be a human."],
  money: "Ra'no is free during the beta. If it becomes paid, only you will pay, and only for the assistant (a fixed amount) — never a percentage of the deal, never the seller or broker.",
  law: "From 8 November 2026 realtor services in Uzbekistan may only be provided by organisations in the Unified Register, and realtors need a qualification certificate. If you work with a broker or realtor, ask for the certificate and a contract.",
  data: "Ra'no stores your search terms, your list of options and the correspondence with sellers you asked her to handle. The data is not sold or shared with third parties. To delete your data, write \"delete my data\" to the bot.",
  who: "The service is developed by Xplorence (Tashkent).",
  h: ["What she does", "What she doesn't do", "Who pays", "Brokers and realtors", "Your data"], open: "Open Ra'no in Telegram",
};
const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
export function aboutHtml(lang = "ru", bot = "rano_smart_bot") {
  const T = lang === "uz" ? UZ : lang === "en" ? EN : RU;
  const ul = xs => "<ul>" + xs.map(x => `<li>${esc(x)}</li>`).join("") + "</ul>";
  return `<!doctype html><html lang="${lang === "uz" ? "uz" : lang === "en" ? "en" : "ru"}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(T.title)}</title>
<style>:root{--bg:#faf7f2;--ink:#231a1d;--muted:#6b5f62;--accent:#C21F45;--line:#eadfd6}
@media (prefers-color-scheme:dark){:root{--bg:#17120f;--ink:#f1e9e4;--muted:#b3a69f;--accent:#ff6f8c;--line:#3a2f2a}}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:28px 18px 48px}h1{font-size:26px;line-height:1.25;margin:0 0 12px}
h2{font-size:18px;margin:28px 0 8px;color:var(--accent)}p,li{color:var(--ink)}ul{padding-left:20px}li{margin:6px 0}
.lead{font-size:18px}.muted{color:var(--muted);font-size:14px;margin-top:32px;border-top:1px solid var(--line);padding-top:12px}
a.btn{display:inline-block;margin-top:18px;background:var(--accent);color:#fff;text-decoration:none;padding:12px 18px;border-radius:12px;font-weight:600}
.lang{float:right;font-size:14px}.lang a{color:var(--muted)}</style></head><body><main>
<div class="lang"><a href="?lang=ru">Русский</a> · <a href="?lang=uz">O'zbekcha</a> · <a href="?lang=en">English</a></div>
<h1>${esc(T.title)}</h1><p class="lead">${esc(T.lead)}</p>
<h2>${T.h[0]}</h2>${ul(T.does)}<h2>${T.h[1]}</h2>${ul(T.doesnt)}<h2>${T.h[2]}</h2><p>${esc(T.money)}</p>
<h2>${T.h[3]}</h2><p>${esc(T.law)}</p><h2>${T.h[4]}</h2><p>${esc(T.data)}</p>
<a class="btn" href="https://t.me/${bot}">${esc(T.open)}</a><p class="muted">${esc(T.who)}</p></main></body></html>`;
}

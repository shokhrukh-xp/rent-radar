"""Длинные тексты Ra'no на трёх языках (переведены вручную). Русский — источник; код берёт ru,
а t() отдаёт перевод на язык клиента. Поменяли русский и забыли перевод — переведёт модель."""

HELP = {
    "ru": "🏠 <b>Ra'no</b> — ваш ИИ-ассистент по поиску жилья.\n\n"
          "Пишите мне как человеку — своими словами, как удобно. Ничего заучивать не нужно: я пойму и сделаю.\n"
          "Например: «хочу двушку в Мирабаде, тысяч до 60», «эта нравится, узнай про документы», «давай посмотрим её в "
          "субботу», «что-то дорого, не то».\n\n"
          "Про конкретную квартиру удобнее всего ответить на её сообщение — тогда я точно пойму, о какой речь. Но можно "
          "и просто описать: «та, что на Чиланзаре».\n\n"
          "Каждый вариант приходит альбомом: первым — карта, где квартира, дальше фото, под ними — разбор цены.\n"
          "Могу ещё: разобрать ссылку на объявление, сделать карточку из пересланного сообщения маклера, написать "
          "маклерам, показать цены рынка. Напоминаю сама — о просмотрах и если маклер молчит.\n\n"
          "Маклерам: скажите «напиши маклерам» — пришлю 5 маклеров под ваш запрос. Нажмёте на имя — откроется чат с "
          "ним, текст уже вписан, останется «Отправить».\n\n"
          "Хотите, чтобы маклеры отвечали прямо вам в Telegram, а я читала их ответы и сама уточняла детали? Напишите "
          "«подключить телеграм» — пришлю инструкцию на минуту. Не хотите давать доступ к чатам — так и скажите, "
          "предложу другой способ.",
    "uz": "🏠 <b>Ra'no</b> — uy-joy qidirish bo'yicha sun'iy intellekt yordamchingiz.\n\n"
          "Menga odamga yozgandek, o'z so'zlaringiz bilan yozing. Hech narsani yodlash shart emas: tushunaman va "
          "bajaraman.\n"
          "Masalan: «Mirobodda 2 xonali, 60 minggacha», «bu yoqdi, hujjatlarini bil», «shanba kuni ko'raylik», "
          "«qimmat, bu emas».\n\n"
          "Aniq bir kvartira haqida uning xabariga javob yozish qulay — shunda qaysi biri haqida ekanini aniq "
          "tushunaman. Yoki shunchaki tasvirlang: «Chilonzordagisi».\n\n"
          "Har bir variant albom bo'lib keladi: birinchi — kvartira joylashgan xarita, keyin rasmlar, ostida — narx "
          "tahlili.\n"
          "Yana: e'lon havolasini tahlil qilaman, maklerdan uzatilgan xabardan kartochka yasayman, maklerlarga "
          "yozaman, bozor narxlarini ko'rsataman. Ko'rishlar va makler jim qolsa — o'zim eslataman.\n\n"
          "Maklerlar: «maklerlarga yoz» deng — so'rovingizga mos 5 ta makler yuboraman. Ismini bossangiz — u bilan "
          "chat ochiladi, matn tayyor yozilgan, faqat «Yuborish»ni bosasiz.\n\n"
          "Maklerlar to'g'ridan-to'g'ri sizga Telegram'da javob berishini, men esa ularni o'zim o'qib, tafsilotlarni "
          "aniqlashtirishimni xohlaysizmi? «telegramni ulash» deb yozing — bir daqiqalik ko'rsatma yuboraman. "
          "Chatlarga ruxsat bermoqchi bo'lmasangiz — shunday deng, boshqa usul taklif qilaman.",
    "en": "🏠 <b>Ra'no</b> — your AI assistant for finding a home.\n\n"
          "Write to me like you would to a person, in your own words. Nothing to learn by heart: I'll understand and "
          "do it.\n"
          "For example: «2-room flat in Mirabad up to 60k», «I like this one, ask about the documents», «let's view it "
          "on Saturday», «too expensive, not this one».\n\n"
          "About a specific flat, it's easiest to reply to its message — then I know exactly which one you mean. Or "
          "just describe it: «the one in Chilanzar».\n\n"
          "Each option comes as an album: first a map of where the flat is, then photos, and a price analysis "
          "underneath.\n"
          "I can also: analyse a listing link, make a card from a forwarded broker message, write to brokers, show "
          "market prices. I remind you myself — about viewings and when a broker goes quiet.\n\n"
          "Brokers: say «write to brokers» — I'll send 5 brokers matching your request. Tap a name — a chat with them "
          "opens with the text already typed, just press «Send».\n\n"
          "Want brokers to reply right in your Telegram while I read their answers and clarify details myself? Write "
          "«connect telegram» — I'll send a one-minute guide. Don't want to give access to your chats — just say so, "
          "I'll offer another way.",
}

WELCOME = {
    "ru": "Привет! Я <b>Ra'no</b> 👋 — ИИ-ассистент, которая обожает квартиры в Ташкенте: аренда и покупка.\n\n"
          "Ищу двумя способами:\n"
          "🔎 Сначала сама — каждый день прочёсываю сайты и Telegram-каналы, выгодное приношу сразу.\n"
          "📇 Если на сайтах пусто — подберу маклеров: нажмёте на имя, запрос уже вписан, останется «Отправить».\n"
          "К каждому варианту — честный разбор цены: дешевле рынка или кто-то загнул 😉\n\n"
          "С чего начнём? Напишите своими словами, что ищете, — например: «купить двушку в центре до $50 000, нужна "
          "ипотека».",
    "uz": "Salom! Men <b>Ra'no</b> 👋 — Toshkentdagi kvartiralarni juda yaxshi ko'radigan sun'iy intellekt "
          "yordamchisiman: ijara va sotib olish.\n\n"
          "Ikki usulda qidiraman:\n"
          "🔎 Avval o'zim — har kuni saytlar va Telegram-kanallarni ko'rib chiqaman, foydalisini darhol olib kelaman.\n"
          "📇 Saytlarda bo'lmasa — maklerlarni tanlayman: ismini bosasiz, so'rov tayyor yozilgan, faqat «Yuborish»ni "
          "bosasiz.\n"
          "Har bir variantga — halol narx tahlili: bozordan arzonmi yoki kimdir oshirib yuborganmi 😉\n\n"
          "Nimadan boshlaymiz? Nima qidirayotganingizni o'z so'zlaringiz bilan yozing — masalan: «markazda 2 xonali "
          "sotib olish, $50 000 gacha, ipoteka kerak».",
    "en": "Hi! I'm <b>Ra'no</b> 👋 — an AI assistant who loves flats in Tashkent: renting and buying.\n\n"
          "I search two ways:\n"
          "🔎 First on my own — every day I go through sites and Telegram channels and bring good deals right away.\n"
          "📇 If the sites are empty — I'll pick brokers: tap a name, the request is already typed, just press "
          "«Send».\n"
          "Every option comes with an honest price analysis: below market or someone's overpricing 😉\n\n"
          "Where do we start? Tell me in your own words what you're looking for — e.g. «buy a 2-room flat in the "
          "centre up to $50,000, need a mortgage».",
}

VIEW_HINT = {
    "ru": "Кнопок больше нет — просто напишите, что поменять, например: «бюджет до 700», «только Мирабад и Юнусабад», "
          "«2–3 комнаты», «пауза». Что я умею — /help.",
    "uz": "Tugmalar endi yo'q — nimani o'zgartirishni yozing, masalan: «byudjet 700 gacha», «faqat Mirobod va "
          "Yunusobod», «2–3 xona», «pauza». Nimalar qila olishim — /help.",
    "en": "There are no buttons anymore — just write what to change, e.g. «budget up to 700», «only Mirabad and "
          "Yunusabad», «2–3 rooms», «pause». What I can do — /help.",
}

GUIDE = {
    "ru": (
    "🔗 <b>Подключите {a} к своему Telegram — и всю переписку с маклерами я возьму на себя.</b>\n\n"
    "<b>Что вы получаете:</b>\n"
    "• Не нужно отвечать каждому маклеру и разбирать десятки сообщений — я читаю их ответы сама.\n"
    "• Фото, точный адрес, этаж, цену, документы, комиссию — уточняю у маклера сама, пока он на связи.\n"
    "• Из каждого ответа делаю карточку: карта, фото, честный разбор цены — сравнивать варианты легко. "
    "Маклер прислал локацию или ссылку на карту — отмечу точку на карте.\n"
    "• Договорюсь о просмотре: скажите, когда вам удобно, — согласую время с маклером, передам ответ и напомню "
    "за 2 часа.\n"
    "• Ничего не теряется и не нужно ничего пересылать: все варианты от всех маклеров — в одном месте.\n"
    "• Вам остаётся только решать: «нравится», «не то», «давай посмотрим».\n\n"
    "<b>Что остаётся за вами:</b> первое сообщение маклеру (нажать «Отправить») и решения: когда смотреть, "
    "торговаться ли, отказать ли — без вашего слова я этого не делаю.\n\n"
    "<b>Как подключить</b> — один раз, около минуты:\n"
    "<b>1.</b> Откройте <b>Настройки</b> → <b>Telegram для бизнеса</b> (Telegram Business).\n"
    "<b>2.</b> Нажмите <b>Чат-боты</b> (ChatBots).\n"
    "<b>3.</b> Впишите <code>{bot}</code> (нажмите — скопируется) и нажмите <b>Добавить</b> (Add).\n"
    "<b>4.</b> В «Чаты, к которым у бота есть доступ» выберите <b>Только выбранные чаты</b> "
    "(Only Selected Chats) → <b>Выбрать чаты</b> (Select Chats) → отметьте <b>Новые чаты</b> (New Chats) "
    "и <b>Не контакты</b> (Non-Contacts) → <b>Готово</b>. Старые переписки и ваши контакты я не увижу.\n"
    "<b>5.</b> Ниже, в разрешениях бота, оставьте включённым <b>Управление сообщениями</b> (Manage Messages). "
    "Остальные переключатели не нужны.\n"
    "<b>6.</b> Нажмите <b>Готово</b> (Done) вверху. Если появится сообщение про подарки и звёзды — просто «OK».\n\n"
    "Всё — я сама увижу подключение и напишу вам здесь.\n\n"
    "<b>Безопасность:</b> читаю только чаты, куда вы отправили запрос маклеру; остальные переписки не открываю "
    "и не храню. Пароль и коды не нужны, отключить можно в любой момент.\n"
    "Если названия пунктов чуть другие — ищите «Чат-боты» в разделе для бизнеса.\n\n"
    "Не хотите давать доступ к чатам — так и скажите, предложу другой способ."),
    "uz": "🔗 <b>{a}ni Telegram'ingizga ulang — maklerlar bilan barcha yozishmalarni o'z zimmamga olaman.</b>\n\n"
          "<b>Nima olasiz:</b>\n"
          "• Har bir maklerga javob yozish va o'nlab xabarlarni ko'rib chiqish shart emas — ularning javoblarini "
          "o'zim o'qiyman.\n"
          "• Rasmlar, aniq manzil, qavat, narx, hujjatlar, komissiya — makler aloqada ekan, o'zim aniqlashtiraman.\n"
          "• Har bir javobdan kartochka yasayman: xarita, rasmlar, halol narx tahlili — variantlarni solishtirish "
          "oson. Makler lokatsiya yoki xarita havolasini yuborsa — xaritada nuqtani belgilayman.\n"
          "• Ko'rish haqida kelishaman: qachon qulayligini ayting — vaqtni makler bilan kelishaman, javobini "
          "yetkazaman va 2 soat oldin eslataman.\n"
          "• Hech narsa yo'qolmaydi va hech narsani uzatish shart emas: barcha maklerlarning variantlari — bir joyda.\n"
          "• Sizga faqat qaror qilish qoladi: «yoqdi», «bu emas», «ko'raylik».\n\n"
          "<b>Sizda qoladi:</b> maklerga birinchi xabar («Yuborish»ni bosish) va qarorlar: qachon ko'rish, "
          "savdolashish, rad etish — sizning so'zingizsiz buni qilmayman.\n\n"
          "<b>Qanday ulash</b> — bir marta, taxminan bir daqiqa:\n"
          "<b>1.</b> <b>Sozlamalar</b> → <b>Telegram Business</b> ni oching.\n"
          "<b>2.</b> <b>Chat-botlar</b> (ChatBots) ni bosing.\n"
          "<b>3.</b> <code>{bot}</code> ni yozing (bossangiz — nusxalanadi) va <b>Qo'shish</b> (Add) ni bosing.\n"
          "<b>4.</b> «Bot kira oladigan chatlar» bo'limida <b>Faqat tanlangan chatlar</b> (Only Selected Chats) → "
          "<b>Chatlarni tanlash</b> (Select Chats) → <b>Yangi chatlar</b> (New Chats) va <b>Kontakt emaslar</b> "
          "(Non-Contacts) ni belgilang → <b>Tayyor</b>. Eski yozishmalar va kontaktlaringizni ko'rmayman.\n"
          "<b>5.</b> Pastda, bot ruxsatlarida <b>Xabarlarni boshqarish</b> (Manage Messages) yoqilgan bo'lsin. "
          "Boshqa kalitlar kerak emas.\n"
          "<b>6.</b> Yuqoridagi <b>Tayyor</b> (Done) ni bosing. Sovg'alar va yulduzlar haqida xabar chiqsa — «OK».\n\n"
          "Bo'ldi — ulanganini o'zim ko'raman va shu yerga yozaman.\n\n"
          "<b>Xavfsizlik:</b> faqat maklerga so'rov yuborgan chatlaringizni o'qiyman; boshqa yozishmalarni "
          "ochmayman va saqlamayman. Parol va kodlar kerak emas, istalgan vaqtda o'chirib qo'yish mumkin.\n"
          "Bo'limlar nomi biroz boshqacha bo'lsa — biznes bo'limidan «Chat-botlar»ni qidiring.\n\n"
          "Chatlarga ruxsat bermoqchi bo'lmasangiz — shunday deng, boshqa usul taklif qilaman.",
    "en": "🔗 <b>Connect {a} to your Telegram — and I'll take over all the messaging with brokers.</b>\n\n"
          "<b>What you get:</b>\n"
          "• No need to answer every broker or sort through dozens of messages — I read their replies myself.\n"
          "• Photos, exact address, floor, price, documents, commission — I ask the broker myself while they're "
          "online.\n"
          "• I turn every reply into a card: map, photos, honest price analysis — easy to compare. If the broker "
          "sends a location or a map link, I mark the spot on the map.\n"
          "• I arrange viewings: tell me when suits you — I agree the time with the broker, pass on the answer and "
          "remind you 2 hours before.\n"
          "• Nothing gets lost and nothing needs forwarding: every option from every broker is in one place.\n"
          "• All that's left for you is to decide: «I like it», «not this one», «let's view it».\n\n"
          "<b>What stays with you:</b> the first message to the broker (press «Send») and the decisions: when to "
          "view, whether to bargain or decline — I don't do any of that without your word.\n\n"
          "<b>How to connect</b> — once, about a minute:\n"
          "<b>1.</b> Open <b>Settings</b> → <b>Telegram Business</b>.\n"
          "<b>2.</b> Tap <b>ChatBots</b>.\n"
          "<b>3.</b> Type <code>{bot}</code> (tap to copy) and tap <b>Add</b>.\n"
          "<b>4.</b> Under «Chats the bot can access» choose <b>Only Selected Chats</b> → <b>Select Chats</b> → tick "
          "<b>New Chats</b> and <b>Non-Contacts</b> → <b>Done</b>. I won't see your old chats or your contacts.\n"
          "<b>5.</b> Below, in the bot permissions, keep <b>Manage Messages</b> on. The other switches aren't needed.\n"
          "<b>6.</b> Tap <b>Done</b> at the top. If a note about gifts and stars pops up — just «OK».\n\n"
          "That's it — I'll see the connection and message you here.\n\n"
          "<b>Privacy:</b> I only read the chats where you sent a request to a broker; I don't open or store your "
          "other chats. No password or codes needed, you can disconnect any time.\n"
          "If the menu names differ slightly — look for «ChatBots» in the Business section.\n\n"
          "Don't want to give access to your chats — just say so, I'll offer another way.",
}

GUIDE_ON = {
    "ru": "✅ {a} уже подключена к вашему Telegram — ответы маклеров вижу и уточняю сама.\n\n"
          "Отключить: Настройки → Telegram для бизнеса → Чат-боты → @{bot} → «Отключить бота».",
    "uz": "✅ {a} Telegram'ingizga allaqachon ulangan — maklerlar javoblarini ko'raman va o'zim aniqlashtiraman.\n\n"
          "O'chirish: Sozlamalar → Telegram Business → Chat-botlar → @{bot} → «Botni o'chirish».",
    "en": "✅ {a} is already connected to your Telegram — I see brokers' replies and clarify details myself.\n\n"
          "To disconnect: Settings → Telegram Business → ChatBots → @{bot} → «Disconnect bot».",
}

MANUAL = {
    "ru": "Хорошо, без подключения 👍 Так тоже работает:\n"
          "1. Скажите «напиши маклерам» — пришлю 5 маклеров. Нажмёте на имя — откроется чат, текст уже вписан, "
          "останется «Отправить».\n"
          "2. В тексте есть ссылка на меня — маклер может прислать варианты сразу мне.\n"
          "3. Ответил вам — перешлите мне его сообщения: нажмите на сообщение и держите → «Переслать» → Ra'no. "
          "Можно выбрать сразу несколько, с фото. Сделаю карточку с картой и разбором цены.\n"
          "4. Если в ответе чего-то не хватает, пришлю готовый текст уточнения — отправите маклеру сами.\n\n"
          "Ваши чаты я не вижу и ничего от вашего имени не пишу. Передумаете — напишите «подключить телеграм».",
    "uz": "Yaxshi, ulamasdan 👍 Bu ham ishlaydi:\n"
          "1. «maklerlarga yoz» deng — 5 ta makler yuboraman. Ismini bossangiz — chat ochiladi, matn tayyor, faqat "
          "«Yuborish»ni bosasiz.\n"
          "2. Matnda mening havolam bor — makler variantlarni to'g'ridan-to'g'ri menga yuborishi mumkin.\n"
          "3. Sizga javob bersa — xabarlarini menga uzating: xabarni bosib turing → «Uzatish» → Ra'no. Bir nechtasini "
          "rasmlari bilan birga tanlash mumkin. Xarita va narx tahlili bilan kartochka yasayman.\n"
          "4. Javobda nimadir yetishmasa, tayyor aniqlashtirish matnini yuboraman — maklerga o'zingiz yuborasiz.\n\n"
          "Chatlaringizni ko'rmayman va sizning nomingizdan hech narsa yozmayman. Fikringiz o'zgarsa — «telegramni "
          "ulash» deb yozing.",
    "en": "OK, no connection 👍 This works too:\n"
          "1. Say «write to brokers» — I'll send 5 brokers. Tap a name — a chat opens with the text already typed, "
          "just press «Send».\n"
          "2. The text has a link to me — a broker can send options straight to me.\n"
          "3. If they reply to you — forward their messages to me: press and hold a message → «Forward» → Ra'no. You "
          "can pick several at once, with photos. I'll make a card with a map and a price analysis.\n"
          "4. If something is missing in a reply, I'll send you a ready follow-up text — you send it to the broker.\n\n"
          "I don't see your chats and don't write anything on your behalf. Change your mind — write «connect "
          "telegram».",
}

# короткие фразы — в каталог
SHORT = [
    ("📇 Маклеров подберу. Но сначала предлагаю подключить меня к вашему Telegram — тогда общаться с маклерами буду "
     "я, а не вы.",
     "📇 Maklerlarni tanlayman. Lekin avval meni Telegram'ingizga ulashni taklif qilaman — shunda maklerlar bilan siz "
     "emas, men gaplashaman.",
     "📇 I'll pick brokers. But first I suggest connecting me to your Telegram — then I'll do the talking with brokers, "
     "not you."),
    ("Как подключите — сразу пришлю маклеров.", "Ulaganingizdan so'ng darhol maklerlarni yuboraman.",
     "As soon as you connect, I'll send the brokers."),
    ("📇 <b>Маклеры под ваш запрос</b> — {n} из {total}", "📇 <b>So'rovingizga mos maklerlar</b> — {total} tadan {n} ta",
     "📇 <b>Brokers for your request</b> — {n} of {total}"),
    ("Нажмите на имя — откроется чат с маклером, запрос уже вписан. Осталось нажать «Отправить».",
     "Ismini bosing — makler bilan chat ochiladi, so'rov tayyor yozilgan. Faqat «Yuborish»ni bosing.",
     "Tap a name — a chat with the broker opens, the request is already typed. Just press «Send»."),
    ("по номеру — если чат не откроется, значит, номер скрыт", "raqam orqali — chat ochilmasa, raqam yashirilgan",
     "by phone — if the chat doesn't open, the number is hidden"),
    ("{n} объявл.", "{n} ta e'lon", "{n} listings"),
    ("Как отправите — я увижу сама и буду следить за ответами: соберу карточки и уточню детали.",
     "Yuborganingizni o'zim ko'raman va javoblarni kuzataman: kartochkalar yasayman va tafsilotlarni aniqlashtiraman.",
     "Once you send it, I'll see it and watch for replies: I'll make cards and clarify details."),
    ("Маклеры ответят вам или сразу мне. Ответили вам — перешлите их сообщения мне (держите сообщение → «Переслать» → "
     "Ra'no), сделаю карточки.",
     "Maklerlar sizga yoki to'g'ridan-to'g'ri menga javob beradi. Sizga javob berishsa — xabarlarini menga uzating "
     "(xabarni bosib turing → «Uzatish» → Ra'no), kartochkalar yasayman.",
     "Brokers will reply to you or straight to me. If they reply to you — forward their messages to me (hold the "
     "message → «Forward» → Ra'no), I'll make cards."),
    (" Хотите, чтобы я читала ответы сама, — «подключить телеграм».",
     " Javoblarni o'zim o'qishimni xohlasangiz — «telegramni ulash».",
     " Want me to read the replies myself — «connect telegram»."),
    (" Ещё {n} — напишите «ещё маклеров».", " Yana {n} ta — «yana maklerlar» deb yozing.",
     " {n} more — write «more brokers»."),
    ("Сегодня уже дала {n} маклеров — на сегодня хватит, чтобы Telegram не счёл это рассылкой. Завтра пришлю "
     "следующих, а пока жду ответы.",
     "Bugun {n} ta makler berdim — Telegram buni ommaviy xabar deb hisoblamasligi uchun bugunga yetarli. Ertaga "
     "keyingilarini yuboraman, hozircha javoblarni kutaman.",
     "I've already given {n} brokers today — that's enough for today so Telegram doesn't treat it as spam. I'll send "
     "the next ones tomorrow; for now I'm waiting for replies."),
    ("✅ <b>{a} подключена к вашему Telegram.</b>\n\nКак это работает: вы отправляете маклеру запрос — я вижу его "
     "ответ, собираю карточку с картой и сама уточняю фото, адрес и цену. Другие ваши чаты не читаю.\n\n",
     "✅ <b>{a} Telegram'ingizga ulandi.</b>\n\nQanday ishlaydi: siz maklerga so'rov yuborasiz — men uning javobini "
     "ko'raman, xaritali kartochka yasayman va rasmlar, manzil va narxni o'zim aniqlashtiraman. Boshqa chatlaringizni "
     "o'qimayman.\n\n",
     "✅ <b>{a} is connected to your Telegram.</b>\n\nHow it works: you send a broker the request — I see their reply, "
     "build a card with a map and clarify photos, address and price myself. I don't read your other chats.\n\n"),
    ("Скажите «напиши маклерам» — пришлю 5 маклеров под ваш запрос. Нажмёте на имя — откроется чат, текст уже вписан, "
     "останется «Отправить». Дальше я сама.\n\nВот что они получат:\n",
     "«maklerlarga yoz» deng — so'rovingizga mos 5 ta makler yuboraman. Ismini bossangiz — chat ochiladi, matn "
     "tayyor, faqat «Yuborish»ni bosasiz. Qolganini o'zim qilaman.\n\nUlar mana buni oladi:\n",
     "Say «write to brokers» — I'll send 5 brokers matching your request. Tap a name — a chat opens with the text "
     "already typed, just press «Send». I'll take it from there.\n\nHere's what they'll get:\n"),
    ("✅ <b>{a} подключена к вашему Telegram.</b> Ваши чаты, кроме маклерских, не читаю. Вот маклеры 👇",
     "✅ <b>{a} Telegram'ingizga ulandi.</b> Maklerlar bilan chatlardan boshqasini o'qimayman. Mana maklerlar 👇",
     "✅ <b>{a} is connected to your Telegram.</b> I only read the broker chats. Here are the brokers 👇"),
    ("⚠️ Почти готово: {a} подключена, но без права отвечать — уточнять у маклеров не смогу.\nОткройте Настройки → "
     "Telegram для бизнеса → Чат-боты → @{bot} и включите <b>Управление сообщениями</b> (Manage Messages).",
     "⚠️ Deyarli tayyor: {a} ulandi, lekin javob berish huquqisiz — maklerlardan aniqlashtira olmayman.\nSozlamalar → "
     "Telegram Business → Chat-botlar → @{bot} ni oching va <b>Xabarlarni boshqarish</b> (Manage Messages) ni yoqing.",
     "⚠️ Almost there: {a} is connected but can't reply — I won't be able to ask brokers anything.\nOpen Settings → "
     "Telegram Business → ChatBots → @{bot} and turn on <b>Manage Messages</b>."),
    ("🔌 {a} отключена от вашего Telegram — ответы маклеров в ваших чатах больше не вижу. Подключить снова: напишите "
     "«подключить телеграм».",
     "🔌 {a} Telegram'ingizdan uzildi — chatlaringizdagi maklerlar javoblarini endi ko'rmayman. Qayta ulash: "
     "«telegramni ulash» deb yozing.",
     "🔌 {a} is disconnected from your Telegram — I no longer see brokers' replies in your chats. To reconnect, write "
     "«connect telegram»."),
]


# ------------------------------------------------------------------------------------------------
# Разбор цены, сводка рынка, итоги дня, напоминания, подборки — вручную (ru, uz, en)
MORE = [
    # условия поиска покупки
    ("{r} комн", "{r} xonali", "{r} rooms"),
    ("любая комнатность", "xonalar soni farqi yo'q", "any number of rooms"),
    ("любые районы", "istalgan tuman", "any district"),
    ("центр города и рядом с ним", "shahar markazi va uning atrofi", "the city centre and around it"),
    ("до {km} км от «{label}»", "«{label}» dan {km} km gacha", "within {km} km of «{label}»"),
    ("Только собственники: у продавца не больше {n} объявлений и нет признаков агентства.",
     "Faqat uy egalari: sotuvchida {n} tadan ko'p e'lon yo'q va agentlik belgilari yo'q.",
     "Owners only: the seller has no more than {n} listings and no signs of an agency."),
    ("Продавцы — собственники и маклеры/агентства; в карточке помечено, кто продаёт.",
     "Sotuvchilar — uy egalari va makler/agentliklar; kartochkada kim sotayotgani belgilangan.",
     "Sellers — owners and brokers/agencies; each card says who is selling."),
    ("Источники — Uybor, Realt24, Joymee, Realting, Yangiuylar и Telegram-каналы.",
     "Manbalar — Uybor, Realt24, Joymee, Realting, Yangiuylar va Telegram-kanallar.",
     "Sources — Uybor, Realt24, Joymee, Realting, Yangiuylar and Telegram channels."),
    ("{rooms} · до ${max} · {districts}", "{rooms} · ${max} gacha · {districts}", "{rooms} · up to ${max} · {districts}"),
    ("За последние {d} дней нашлось подходящих: {n}. Самые выгодные пришлю сразу, остальные — "
     "лучшими карточками с фото, по одной.",
     "Oxirgi {d} kunda mosi topildi: {n} ta. Eng foydalilarini darhol yuboraman, qolganlarini — rasmli "
     "kartochkalar bilan, bittadan.",
     "Matches in the last {d} days: {n}. The best deals come right away, the rest as cards with photos, one by one."),
    ("За последние {d} дней подходящих нет — но я на посту, пришлю, как только появятся 👀",
     "Oxirgi {d} kunda mosi yo'q — lekin men navbatchiman, paydo bo'lishi bilan yuboraman 👀",
     "Nothing suitable in the last {d} days — but I'm on watch and will send them as soon as they appear 👀"),
    ("Условия поиска обновлены", "Qidiruv shartlari yangilandi", "Search criteria updated"),
    ("Поиск квартиры для покупки включён", "Sotib olish uchun kvartira qidiruvi yoqildi", "Flat search for buying is on"),
    ("Как идёт поиск — спросите «как идёт поиск?»", "Qidiruv qanday ketyapti — «qidiruv qanday?» deb so'rang",
     "To see how it's going, ask «how is the search going?»"),
    ("{i} из {n}", "{i} / {n}", "{i} of {n}"),
    ("Есть ещё {n} — скажите, если хотите посмотреть", "Yana {n} ta bor — ko'rmoqchi bo'lsangiz, ayting",
     "{n} more — just say if you want to see them"),
    ("Подборка дня", "Kun to'plami", "Today's picks"),
    ("Последняя подборка", "Oxirgi to'plam", "Latest picks"),
    ("Что нашлось сейчас", "Hozir topilganlar", "What I found now"),
    ("Похожий вариант", "O'xshash variant", "Similar option"),
    ("Подборка", "To'plam", "Picks"),
    ("🔄 Пробежалась по всем сайтам! Сегодня просмотрено {a}, подошло {b}; в подборке ждут {c}.",
     "🔄 Barcha saytlarni ko'rib chiqdim! Bugun ko'rildi {a}, mos keldi {b}; to'plamda kutmoqda {c}.",
     "🔄 Checked every site! Seen today: {a}, matching: {b}; waiting in the picks: {c}."),
    ("собственник", "uy egasi", "owner"),
    ("маклер", "makler", "broker"),
    ("💸 <b>Та же квартира — дешевле на ${d}</b>", "💸 <b>Shu kvartira — ${d} arzonroq</b>",
     "💸 <b>The same flat — ${d} cheaper</b>"),
    ("Раньше присылала её за ${p} ({site}{kind})", "Avval uni ${p} ga yuborgandim ({site}{kind})",
     "I sent it earlier at ${p} ({site}{kind})"),
    # напоминания и итоги дня
    ("вариант #{n}", "#{n} variant", "option #{n}"),
    ("Маклер", "Makler", "Broker"),
    ("⏰ <b>{who}</b> молчит сутки по варианту #{n} ({tag}).",
     "⏰ <b>{who}</b> #{n} variant bo'yicha bir kundan beri jim ({tag}).",
     "⏰ <b>{who}</b> hasn't replied for a day about option #{n} ({tag})."),
    (" Тихонько напомнила — один раз, надоедать не буду 🙂", " Muloyimgina eslatdim — bir marta, bezor qilmayman 🙂",
     " I sent a gentle reminder — just once, I won't pester 🙂"),
    (" Написать ему ещё раз Telegram мне не даёт — если нужно, напомните сами.",
     " Telegram unga yana yozishga ruxsat bermayapti — kerak bo'lsa, o'zingiz eslating.",
     " Telegram won't let me write to them again — remind them yourself if needed."),
    ("время уточнить", "vaqtni aniqlash kerak", "time to be confirmed"),
    ("⏰ Через {h} ч {m} мин просмотр — не опаздываем! 😉", "⏰ {h} soat {m} daqiqadan keyin ko'rish — kechikmaymiz! 😉",
     "⏰ Viewing in {h} h {m} min — let's not be late! 😉"),
    ("🏠 Ну что, как вам вариант #{n} ({tag})? Ответьте на это сообщение своими словами: понравилась, думаете или "
     "не то. Не состоялся — напишите, на когда перенести.",
     "🏠 Xo'sh, #{n} variant ({tag}) qanday? Shu xabarga o'z so'zlaringiz bilan javob bering: yoqdi, o'ylayapsiz "
     "yoki mos emas. Bo'lmagan bo'lsa — qachonga ko'chirishni yozing.",
     "🏠 So, how was option #{n} ({tag})? Reply to this message in your own words: liked it, thinking, or not it. "
     "If it didn't happen, tell me when to reschedule."),
    ("☀️ <b>Доброе утро! Сегодня идём смотреть</b>", "☀️ <b>Xayrli tong! Bugun ko'rishga boramiz</b>",
     "☀️ <b>Good morning! Viewings today</b>"),
    ("За 2 часа напомню ещё раз — я пунктуальная 😉", "2 soat oldin yana eslataman — men aniqman 😉",
     "I'll remind you again 2 hours before — I'm punctual 😉"),
    ("🌙 <b>Итоги дня</b> — {date}. Вот что успели:", "🌙 <b>Kun yakunlari</b> — {date}. Mana nimalarga ulgurdik:",
     "🌙 <b>Day summary</b> — {date}. Here's what we got done:"),
    ("📇 Маклерам написали: {a} сегодня, всего {b}", "📇 Maklerlarga yozildi: bugun {a}, jami {b}",
     "📇 Brokers contacted: {a} today, {b} in total"),
    ("🏠 Новых вариантов от маклеров: {n}", "🏠 Maklerlardan yangi variantlar: {n}", "🏠 New options from brokers: {n}"),
    ("👀 Ждут вашего решения: {n}", "👀 Qaroringizni kutmoqda: {n}", "👀 Waiting for your decision: {n}"),
    ("💬 Маклеры ответили на уточнения: {n}", "💬 Maklerlar savollarga javob berdi: {n}",
     "💬 Brokers answered follow-up questions: {n}"),
    ("⏳ Молчат больше суток: {n}", "⏳ Bir kundan ortiq jim: {n}", "⏳ Silent for over a day: {n}"),
    ("📋 В шортлисте: {n}", "📋 Tanlanganlarda: {n}", "📋 In the shortlist: {n}"),
    ("🔎 Сайты и каналы: новых объявлений {a}, подошло {b}, прислала {c}",
     "🔎 Saytlar va kanallar: yangi e'lonlar {a}, mos keldi {b}, yubordim {c}",
     "🔎 Sites and channels: {a} new listings, {b} matching, {c} sent"),
    ("🔎 С сайтов прислала: {n}", "🔎 Saytlardan yubordim: {n}", "🔎 Sent from sites: {n}"),
    ("📅 <b>Завтра просмотры</b>", "📅 <b>Ertaga ko'rishlar</b>", "📅 <b>Viewings tomorrow</b>"),
    ("Разберите {n} {w} — ответьте на карточки своими словами: «нравится», «потом» или «не то», а дальше я сама.",
     "{n} ta variantni ko'rib chiqing — kartochkalarga o'z so'zlaringiz bilan javob bering: «yoqdi», «keyin» yoki "
     "«mos emas», qolganini o'zim qilaman.",
     "Go through {n} options — reply to the cards in your own words: «like», «later» or «not it», and I'll do the rest."),
    ("Давайте начнём с маклеров — скажите «напиши маклерам», пришлю 5 со вписанным запросом 🙂",
     "Maklerlardan boshlaylik — «maklerlarga yoz» deng, so'rov yozilgan 5 ta maklerni yuboraman 🙂",
     "Let's start with brokers — say «write to brokers» and I'll send 5 with the request already typed 🙂"),
    ("Завтра можно написать ещё маклерам — скажите «напиши маклерам» (до 15 в день, и номер в безопасности).",
     "Ertaga yana maklerlarga yozish mumkin — «maklerlarga yoz» deng (kuniga 15 tagacha, raqamingiz xavfsiz).",
     "Tomorrow you can write to more brokers — say «write to brokers» (up to 15 a day, keeps your number safe)."),
    ("🙈 Пробежалась по всем сайтам и каналам — под ваши условия сейчас ничего нет.",
     "🙈 Barcha saytlar va kanallarni ko'rib chiqdim — shartlaringizga hozircha hech narsa yo'q.",
     "🙈 I've gone through every site and channel — nothing matches your criteria right now."),
    ("🙈 За 2 дня на сайтах нашлось совсем мало подходящего ({n}).",
     "🙈 2 kunda saytlarda mos variantlar juda kam topildi ({n}).",
     "🙈 Very few matches on the sites in 2 days ({n})."),
    ("🙈 За сутки на сайтах не нашлось подходящей аренды.", "🙈 Bir kunda saytlarda mos ijara topilmadi.",
     "🙈 No suitable rentals on the sites in the last day."),
    # разбор цены
    ("📊 <b>Анализ</b>", "📊 <b>Tahlil</b>", "📊 <b>Analysis</b>"),
    ("на {p}% ниже", "{p}% past", "{p}% below"),
    ("на {p}% выше", "{p}% yuqori", "{p}% above"),
    ("на уровне", "bir xil darajada", "on par with"),
    ("💵 ${m2}/м² — {size} похожих (${med}/м²)", "💵 ${m2}/m² — o'xshashlariga nisbatan {size} (${med}/m²)",
     "💵 ${m2}/m² — {size} similar (${med}/m²)"),
    (" · по рынку ≈ ${v}", " · bozor bo'yicha ≈ ${v}", " · market value ≈ ${v}"),
    ("💵 ${m2}/м², а похожие — ${med}/м²: разница слишком большая, чтобы сравнивать, — смотрите квартиру",
     "💵 ${m2}/m², o'xshashlari esa ${med}/m²: farq solishtirish uchun juda katta — kvartirani ko'ring",
     "💵 ${m2}/m² vs ${med}/m² for similar: the gap is too big to compare — see the flat"),
    ("💵 ${m2}/м² — похожих пока мало для сравнения", "💵 ${m2}/m² — solishtirish uchun o'xshashlari hozircha kam",
     "💵 ${m2}/m² — too few similar listings to compare yet"),
    (" (видно: {s})", " (ko'rinadi: {s})", " (visible: {s})"),
    (". В объявлении — «{c}», на фото хуже", ". E'londa — «{c}», rasmda yomonroq", ". The listing says «{c}», photos look worse"),
    (". В объявлении — «{c}», на фото лучше", ". E'londa — «{c}», rasmda yaxshiroq", ". The listing says «{c}», photos look better"),
    ("🛠 Ремонт {by}: хороший — можно заезжать{seen}{differ}", "🛠 Ta'mir {by}: yaxshi — ko'chib kirsa bo'ladi{seen}{differ}",
     "🛠 Renovation {by}: good — move-in ready{seen}{differ}"),
    ("🛠 Ремонт {by}: {cls}{seen} — {what} ~${reno}{differ}. С ним ≈ ${all_in}",
     "🛠 Ta'mir {by}: {cls}{seen} — {what} ~${reno}{differ}. U bilan ≈ ${all_in}",
     "🛠 Renovation {by}: {cls}{seen} — {what} ~${reno}{differ}. With it ≈ ${all_in}"),
    ("освежить", "yangilash", "refresh"),
    ("капитальный", "kapital ta'mir", "full renovation"),
    ("отделка с нуля", "noldan pardozlash", "finishing from scratch"),
    (", на {p}% дешевле похожих с хорошим ремонтом", ", yaxshi ta'mirli o'xshashlaridan {p}% arzon",
     ", {p}% cheaper than similar with good renovation"),
    (", на {p}% дороже похожих с хорошим ремонтом", ", yaxshi ta'mirli o'xshashlaridan {p}% qimmat",
     ", {p}% more than similar with good renovation"),
    (", как похожие с хорошим ремонтом", ", yaxshi ta'mirli o'xshashlari kabi", ", same as similar with good renovation"),
    ("🛠 Ремонт не понять ни по фото, ни по описанию — уточните. Если нужен капитальный, добавьте ~${v}",
     "🛠 Ta'mirni rasmdan ham, tavsifdan ham tushunib bo'lmadi — aniqlang. Kapital ta'mir kerak bo'lsa, ~${v} qo'shing",
     "🛠 Can't tell the renovation from photos or text — ask. If it needs a full renovation, add ~${v}"),
    ("🛠 Ремонт не указан — уточните у продавца", "🛠 Ta'mir ko'rsatilmagan — sotuvchidan so'rang",
     "🛠 Renovation not stated — ask the seller"),
    ("взнос ${down} ({share}%), ≈ {mln} млн сум/мес (~${usd}) на {years} лет",
     "boshlang'ich ${down} ({share}%), oyiga ≈ {mln} mln so'm (~${usd}), {years} yilga",
     "down payment ${down} ({share}%), ≈ {mln} mln UZS/month (~${usd}) for {years} years"),
    ("снимать такую же — ~${r}/мес", "xuddi shundayini ijaraga olish — ~${r}/oy", "renting the same — ~${r}/month"),
    (": ипотека дороже на ~${d}/мес, зато квартира ваша", ": ipoteka oyiga ~${d} qimmat, lekin kvartira sizniki",
     ": the mortgage costs ~${d}/month more, but the flat is yours"),
    (": платёж как аренда — выгоднее брать", ": to'lov ijara bilan teng — sotib olish foydaliroq",
     ": the payment equals rent — buying pays off"),
    ("🤝 Торг: цена уже ниже рынка — просите ~${v}, сильно давить не стоит",
     "🤝 Savdo: narx allaqachon bozordan past — ~${v} so'rang, qattiq bosim qilmang",
     "🤝 Bargaining: already below market — ask ~${v}, don't push too hard"),
    ("🤝 Торг: начните с ~${a}, потолок ~${b}", "🤝 Savdo: ~${a} dan boshlang, chegara ~${b}",
     "🤝 Bargaining: start at ~${a}, ceiling ~${b}"),
    (" · аргументы: {a}", " · dalillar: {a}", " · arguments: {a}"),
    ("висит {n} дн.", "{n} kundan beri sotuvda", "listed for {n} days"),
    ("цену уже снижали", "narx allaqachon tushirilgan", "price already cut"),
    ("нужен ремонт ~${v}", "ta'mir kerak ~${v}", "needs renovation ~${v}"),
    ("по фото ремонт хуже заявленного", "rasmda ta'mir aytilganidan yomonroq", "photos show worse renovation than claimed"),
    ("дороже похожих на {p}%", "o'xshashlaridan {p}% qimmat", "{p}% above similar"),
    ("комиссию маклера — тоже в торг", "makler komissiyasi ham savdoga kiradi", "the broker's fee is negotiable too"),
    ("📈 Как вложение: сдавать ~${r}/мес → ~{y}% в год чистыми, лучше вклада в $ ({d}%)",
     "📈 Investitsiya sifatida: ~${r}/oy ijaraga → yiliga sof ~{y}%, $ omonatdan ({d}%) yaxshiroq",
     "📈 As an investment: rent out at ~${r}/month → ~{y}% a year net, better than a $ deposit ({d}%)"),
    ("📈 Как вложение: сдавать ~${r}/мес → ~{y}% в год чистыми, как вклад в $ ({d}%)",
     "📈 Investitsiya sifatida: ~${r}/oy ijaraga → yiliga sof ~{y}%, $ omonat ({d}%) bilan teng",
     "📈 As an investment: rent out at ~${r}/month → ~{y}% a year net, same as a $ deposit ({d}%)"),
    ("📈 Как вложение: сдавать ~${r}/мес → ~{y}% в год чистыми, хуже вклада в $ ({d}%)",
     "📈 Investitsiya sifatida: ~${r}/oy ijaraga → yiliga sof ~{y}%, $ omonatdan ({d}%) yomonroq",
     "📈 As an investment: rent out at ~${r}/month → ~{y}% a year net, worse than a $ deposit ({d}%)"),
    (", окупится за ~{y} лет", ", ~{y} yilda o'zini oqlaydi", ", pays back in ~{y} years"),
    ("через 5 лет квартира скорее ~${b} (от ${p} до ${o})", "5 yildan keyin kvartira taxminan ~${b} (${p} dan ${o} gacha)",
     "in 5 years the flat is likely ~${b} (${p} to ${o})"),
    ("Похожие: медиана ${m}/м², {n} объявл. ({label})", "O'xshashlari: mediana ${m}/m², {n} ta e'lon ({label})",
     "Similar: median ${m}/m², {n} listings ({label})"),
    ("С хорошим ремонтом: ${m}/м², {n} объявл.", "Yaxshi ta'mirli: ${m}/m², {n} ta e'lon",
     "With good renovation: ${m}/m², {n} listings"),
    ("средняя по району: ${m}/м² (госоценка на 01.09.2026, новостройки и вторичка вместе)",
     "tuman bo'yicha o'rtacha: ${m}/m² (01.09.2026 holatiga davlat bahosi, yangi va ikkilamchi birga)",
     "district average: ${m}/m² (official valuation as of 01.09.2026, new and resale together)"),
    ("На рынке ~{n} дн.", "Bozorda ~{n} kun", "On the market ~{n} days"),
    ("; цена: ${a} → ${b} ({p}%)", "; narx: ${a} → ${b} ({p}%)", "; price: ${a} → ${b} ({p}%)"),
    ("Ремонт с материалами (Ustabor, 2026): освежить ~${a}/м², капитальный ~${n}/м², с нуля ~${b}/м²",
     "Materiallari bilan ta'mir (Ustabor, 2026): yangilash ~${a}/m², kapital ~${n}/m², noldan ~${b}/m²",
     "Renovation incl. materials (Ustabor, 2026): refresh ~${a}/m², full ~${n}/m², from scratch ~${b}/m²"),
    ("Ипотека — типовые условия на вторичку: ~{r}% в сумах, взнос от 25%, {y} лет, переплата ~${o}",
     "Ipoteka — ikkilamchi uyga odatiy shartlar: so'mda ~{r}%, boshlang'ich 25% dan, {y} yil, ortiqcha to'lov ~${o}",
     "Mortgage — typical resale terms: ~{r}% in UZS, from 25% down, {y} years, total interest ~${o}"),
    (". Льготная ({r}%, {y} лет, до {cap} млн сум), если подходите: ≈ {mln} млн сум/мес, взнос ${down}",
     ". Imtiyozli ({r}%, {y} yil, {cap} mln so'mgacha), agar mos kelsangiz: oyiga ≈ {mln} mln so'm, boshlang'ich ${down}",
     ". Subsidised ({r}%, {y} years, up to {cap} mln UZS), if eligible: ≈ {mln} mln UZS/month, down ${down}"),
    (". Платёж в сумах: если сум слабеет, в $ он со временем легче",
     ". To'lov so'mda: so'm zaiflashsa, $ da u vaqt o'tib yengillashadi",
     ". Payments are in UZS: if the sum weakens, they get lighter in $ over time"),
    ("Аренда: {src}; налог {tax}%, месяц простоя в год", "Ijara: {src}; soliq {tax}%, yiliga bir oy bo'sh turadi",
     "Rent: {src}; {tax}% tax, one vacant month a year"),
    ("; в объявлениях похожие сдают за ~${v} — взята осторожная оценка",
     "; e'lonlarda o'xshashlari ~${v} ga ijaraga beriladi — ehtiyotkor baho olindi",
     "; similar ones are listed at ~${v} — a cautious estimate is used"),
    ("Купить с ремонтом и сдавать 3 года: ≈ {p}% (${v}); вклад в $: {d}%",
     "Ta'mir bilan sotib olib, 3 yil ijaraga berish: ≈ {p}% (${v}); $ omonat: {d}%",
     "Buy, renovate and rent out for 3 years: ≈ {p}% (${v}); $ deposit: {d}%"),
    ("Купить и сдавать 3 года: ≈ {p}% (${v}); вклад в $: {d}%",
     "Sotib olib, 3 yil ijaraga berish: ≈ {p}% (${v}); $ omonat: {d}%",
     "Buy and rent out for 3 years: ≈ {p}% (${v}); $ deposit: {d}%"),
    ("Цена через 1 / 3 / 5 лет (база): ${a} / ${b} / ${c}", "1 / 3 / 5 yildan keyingi narx (asosiy): ${a} / ${b} / ${c}",
     "Price in 1 / 3 / 5 years (base): ${a} / ${b} / ${c}"),
    ("Цены — предложения, не сделок; допущения на {d}", "Narxlar — takliflar, bitimlar emas; {d} holatiga taxminlar",
     "Prices are asking prices, not deals; assumptions as of {d}"),
    ("<blockquote expandable>🔍 <b>Как считала</b>", "<blockquote expandable>🔍 <b>Qanday hisobladim</b>",
     "<blockquote expandable>🔍 <b>How I calculated</b>"),
    ("Подробно о рынке и методике", "Bozor va uslub haqida batafsil", "More on the market and method"),
    ("{d}, {r}-комн, {a}–{b} м²", "{d}, {r} xonali, {a}–{b} m²", "{d}, {r}-room, {a}–{b} m²"),
    ("{d}, {band}, $/м² × площадь", "{d}, {band}, $/m² × maydon", "{d}, {band}, $/m² × area"),
    ("до 35 м²", "35 m² gacha", "up to 35 m²"),
    ("35–60 м²", "35–60 m²", "35–60 m²"),
    ("больше 60 м²", "60 m² dan katta", "over 60 m²"),
    ("медиана {n} объявл.: {label}", "{n} ta e'lon medianasi: {label}", "median of {n} listings: {label}"),
    ("средняя $/м² по району (ЦЭИР/ЦБ, 2026) × площадь", "tuman bo'yicha o'rtacha $/m² (IIRM/MB, 2026) × maydon",
     "district average $/m² (CERR/CB, 2026) × area"),
    (", −{p}% за общежитие (допущение)", ", yotoqxona uchun −{p}% (taxmin)", ", −{p}% for a dormitory (assumption)"),
    ("м²", "m²", "m²"),
    (", обычно ${a}–{b}", ", odatda ${a}–{b}", ", usually ${a}–{b}"),
    ("Дом строится, сдача {y} — фото квартиры ещё нет", "Uy qurilmoqda, topshirish {y} — kvartira rasmlari hali yo'q",
     "Under construction, completion {y} — no photos of the flat yet"),
    ("🛠 Дом строится, сдача {y} — фото квартиры ещё нет", "🛠 Uy qurilmoqda, topshirish {y} — kvartira rasmlari hali yo'q",
     "🛠 Under construction, completion {y} — no photos of the flat yet"),
    ("; ремонт обещан по договору: «{c}»", "; ta'mir shartnoma bo'yicha va'da qilingan: «{c}»", "; renovation promised in the contract: «{c}»"),
    ("Ремонт: в объявлении «{c}», но на фото только планировки и документы — попросите фото комнат",
     "Ta'mir: e'londa «{c}», lekin rasmlarda faqat reja va hujjatlar — xonalar rasmini so'rang",
     "Renovation: listing says «{c}», but the photos are only plans and documents — ask for photos of the rooms"),
    ("🛠 Ремонт: в объявлении «{c}», но на фото только планировки и документы — попросите фото комнат",
     "🛠 Ta'mir: e'londa «{c}», lekin rasmlarda faqat reja va hujjatlar — xonalar rasmini so'rang",
     "🛠 Renovation: listing says «{c}», but the photos are only plans and documents — ask for photos of the rooms"),
    ("Фото комнат нет — только планировки и документы; попросите фото у продавца",
     "Xonalar rasmi yo'q — faqat reja va hujjatlar; sotuvchidan rasm so'rang",
     "No photos of the rooms — only plans and documents; ask the seller for photos"),
    ("🛠 Фото комнат нет — только планировки и документы; попросите фото у продавца",
     "🛠 Xonalar rasmi yo'q — faqat reja va hujjatlar; sotuvchidan rasm so'rang",
     "🛠 No photos of the rooms — only plans and documents; ask the seller for photos"),
    ("≈ {p} всего", "≈ jami {p}", "≈ {p} in total"),
    ("Район по точке на карте — {d} (на сайте указан {s})", "Xaritadagi nuqta bo'yicha tuman — {d} (saytda {s} deyilgan)",
     "District by the map pin — {d} (the site says {s})"),
    ("строго центр города (до {km} км от середины, только с точным адресом)",
     "faqat shahar markazi (o'rtasidan {km} km gacha, faqat aniq manzil bilan)",
     "strictly the city centre (within {km} km of its middle, exact address only)"),
    (" (переуступка договора)", " (shartnomani qayta rasmiylashtirish)", " (contract resale)"),
    ("Район по тексту объявления — {d} (на сайте ошибочно: {s})", "E'lon matniga ko'ra tuman — {d} (saytda xato: {s})",
     "District per the listing text — {d} (the site wrongly says {s})"),
    # сводка рынка
    ("📊 Среза рынка ещё нет — он появится после ближайшего прохода поиска.",
     "📊 Bozor kesimi hali yo'q — u keyingi qidiruvdan so'ng paydo bo'ladi.",
     "📊 No market snapshot yet — it appears after the next search pass."),
    ("📊 <b>Рынок по срезу Uybor</b> на {d}", "📊 <b>Uybor kesimi bo'yicha bozor</b>, {d}", "📊 <b>Market per Uybor snapshot</b>, {d}"),
    ("Медиана $/м², вторичка · новостройки · аренда в месяц", "Mediana $/m², ikkilamchi · yangi uylar · oylik ijara",
     "Median $/m², resale · new buildings · monthly rent"),
    ("Снято с продажи с начала наблюдений: {n}", "Kuzatuv boshidan sotuvdan olindi: {n}",
     "Taken off the market since tracking began: {n}"),
    ("Сценарии на год (цена в $): спад {p}%, база {b}%, рост {o}%",
     "Bir yillik ssenariylar ($ da narx): pasayish {p}%, asosiy {b}%, o'sish {o}%",
     "One-year scenarios (price in $): down {p}%, base {b}%, up {o}%"),
    ("Полный отчёт о рынке", "Bozor haqida to'liq hisobot", "Full market report"),
    ("🗺 Карта цен: дорогие и недорогие части районов", "🗺 Narxlar xaritasi: tumanlarning qimmat va arzon qismlari",
     "🗺 Price map: pricier and cheaper parts of districts"),
]

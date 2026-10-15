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

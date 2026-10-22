"""Языки Ra'no: клиент — русский, узбекский (латиница) или английский; маклер — русский или узбекский.

Главные тексты (карточка, разбор цены, справка, инструкции) переведены вручную — каталог CAT ниже,
ключ — русский шаблон, подстановки через {имя}. Всё остальное, что уходит клиенту по-русски, переводит
модель (воркер /svc/translate) — с разметкой, ссылками и числами без изменений, с кэшем.
"""
import re

LANG = "ru"          # язык клиента в этом проходе (сейчас клиент один; для многих — на каждого)
LANGS = ("ru", "uz", "en")


def set_lang(lang):
    global LANG
    LANG = lang if lang in LANGS else "ru"
    return LANG


def t(ru: str, **kw) -> str:
    """Текст на языке клиента: ручной перевод из каталога, иначе — русский (его потом переведёт модель)."""
    s = ru
    if LANG != "ru":
        s = (CAT.get(ru) or {}).get(LANG) or ru
    if kw:
        try:
            return s.format(**kw)
        except (KeyError, IndexError, ValueError):
            return ru.format(**kw)
    return s


def has_russian(text: str) -> bool:
    """Остался ли в тексте русский (а не только адреса и имена): тогда — в перевод моделью."""
    x = re.sub(r"<code>.*?</code>", " ", text or "", flags=re.S)
    x = re.sub(r"<[^>]+>|https?://\S+|«[^»]{0,300}»", " ", x)
    cyr = len(re.findall(r"[а-яё]", x, re.I))
    lat = len(re.findall(r"[a-z]", x, re.I))
    return cyr >= 12 and cyr > lat * 0.6


_CACHE = {}


def ai(cfg, text: str, lang: str = None, post=None) -> str:
    """Перевод моделью через воркер. Не вышло — исходный текст (лучше по-русски, чем ничего).
    lang="ru" — перевод на русский (сообщение маклера по-узбекски для русскоязычного клиента)."""
    lang = lang or LANG
    if not text or not cfg.get("worker_url"):
        return text
    if lang == "ru" and detect_lang(text) != "uz":
        return text
    if lang != "ru" and not has_russian(text):
        return text
    k = (lang, text)
    if k in _CACHE:
        return _CACHE[k]
    if post is None:
        import rent_radar
        post = rent_radar.worker_post
    r = post(cfg, "/svc/translate", {"text": text, "lang": lang}, timeout=40)
    out = (r or {}).get("text") or text
    if out == text:                    # перевод не удался (воркер не ответил) — не запоминаем, попробуем снова
        return out
    if len(_CACHE) > 500:
        _CACHE.clear()
    _CACHE[k] = out
    return out


# ------------------------------------------------------------------ маклеры ---
UZ_LAT = re.compile(r"\b(?:bor|yo['ʻ’]?q|xonali|xona|sotiladi|sotuvda|ijaraga|narxi|qavat|mavjud|rahmat|assalomu|"
                    r"mumkin|bo['ʻ’]?ladi|kerak|uy|tuman|mo['ʻ’]?ljal|kvartira\s+sotiladi|yaxshi|ha|yo['ʻ’]?q|"
                    r"kelishamiz|ko['ʻ’]?rish|qachon|ertaga|bugun)\b", re.I)
UZ_CYR = re.compile(r"[ўқғҳ]|\b(?:бор|йўқ|хонали|сотилади|нархи|қават|рахмат|мумкин|бўлади|керак|уй)\b", re.I)


def detect_lang(text: str) -> str:
    """Язык маклера по его сообщению: «uz» (латиница или кириллица) или «ru»."""
    t_ = text or ""
    if UZ_CYR.search(t_):
        return "uz"
    lat_words = len(UZ_LAT.findall(t_))
    cyr = len(re.findall(r"[а-яё]", t_, re.I))
    if lat_words >= 2 or (lat_words >= 1 and cyr < 10):
        return "uz"
    return "ru"


def broker_lang(store, key, text=None) -> str:
    """Язык маклера: запоминаем по его сообщениям, отвечаем на нём же."""
    langs = store.get_kv("broker_lang") or {}
    if text and len(text.strip()) >= 3:
        lang = detect_lang(text)
        if lang == "ru" and len(re.findall(r"[а-яё]", text, re.I)) < 10 and langs.get(str(key)):
            return langs[str(key)]         # «Ok!», номер телефона — не повод переходить на русский
        if langs.get(str(key)) != lang:
            langs[str(key)] = lang
            store.set_kv("broker_lang", langs)
        return lang
    return langs.get(str(key), "ru")


# ------------------------------------------------------------------ каталог ---
# ключ — русский шаблон как в коде; uz — латиница
CAT = {}


def add(ru, uz, en):
    CAT[ru] = {"uz": uz, "en": en}


# вердикт цены
add("Дорого", "Qimmat", "Expensive")
add("Хорошая цена", "Yaxshi narx", "Good price")
add("Цена в рынке", "Bozor narxida", "Market price")
add("Сравнить не с чем", "Solishtirishga yo'q", "Nothing to compare with")
add("Подозрительно дёшево", "Shubhali arzon", "Suspiciously cheap")
add("на {pct} дороже похожих{near}{with_reno}", "o'xshashlaridan {pct} qimmatroq{near}{with_reno}",
    "{pct} more than similar{near}{with_reno}")
add("на {pct} дешевле похожих{near}{with_reno}", "o'xshashlaridan {pct} arzonroq{near}{with_reno}",
    "{pct} less than similar{near}{with_reno}")
add(" рядом", " (yaqin atrofda)", " nearby")
add(" даже с ремонтом", " (hatto ta'mir bilan)", " even with renovation")
add(" с учётом ремонта", " (ta'mirni hisobga olganda)", " including renovation")
add(" с таким же ремонтом", " (xuddi shunday ta'mirli)", " with the same renovation")
add("как у похожих", "o'xshashlari kabi", "same as similar")
add(" — торгуйтесь", " — savdolashing", " — negotiate")
add("мало похожих объявлений — оценивайте на месте", "o'xshash e'lonlar kam — joyida baholang",
    "few similar listings — judge on site")
add("рядом продают в основном с ремонтом ({med}), а {what} мало — оценивайте на месте",
    "yaqin atrofda asosan ta'mirlisi sotiladi ({med}), {what} esa kam — joyida baholang",
    "nearby are mostly renovated ({med}), {what} are rare — judge on site")
add("коробок", "karobkalar", "shell flats")
add("таких", "bunday", "such flats")
add("на {pct} дешевле похожих{same} ({med}) — проверьте адрес, документы и состояние",
    "o'xshashlaridan {pct} arzon{same} ({med}) — manzil, hujjatlar va holatini tekshiring",
    "{pct} cheaper than similar{same} ({med}) — check the address, documents and condition")
add(" с таким же ремонтом", " (xuddi shunday ta'mirli)", " with the same renovation")
add("{base}, но бывшее общежитие", "{base}, lekin sobiq yotoqxona", "{base}, but a former dormitory")
add("{why}; проверьте кадастр и приватизацию", "{why}; kadastr va xususiylashtirishni tekshiring",
    "{why}; check the cadastre and privatisation")
add("${m2}/м², {n} объявл.", "${m2}/m², {n} ta e'lon", "${m2}/m², {n} listings")

# карточка
add("{n}-комн", "{n} xonali", "{n}-room")
add("{a} м²", "{a} m²", "{a} m²")
add("этаж {f}", "{f}-qavat", "floor {f}")
add("район не указан", "tuman ko'rsatilmagan", "district not stated")
add("новостройка", "yangi uy", "new building")
add("вторичка", "ikkilamchi", "resale")
add(" (у «{label}»)", " («{label}» yonida)", " (near «{label}»)")
add("{mln} млн сум", "{mln} mln so'm", "{mln} mln UZS")
add("рынок ≈ ${v}", "bozor ≈ ${v}", "market ≈ ${v}")
add("хороший", "yaxshi", "good")
add("средний", "o'rtacha", "average")
add("без ремонта", "ta'mirsiz", "no renovation")
add("коробка", "karobka", "shell")
add("по фото", "rasmga ko'ra", "by photos")
add("по описанию", "tavsifga ko'ra", "by description")
add("по пометке сайта", "sayt belgisiga ko'ra", "per the site")
add("Ремонт {cls}", "Ta'mir: {cls}", "Renovation: {cls}")
add("Ремонт коробка ({by}: «без ремонта» в новостройке)", "Ta'mir: karobka ({by}: yangi uyda «ta'mirsiz»)",
    "Renovation: shell ({by}: «no renovation» in a new building)")
add("Ремонт коробка (новостройка без ремонта)", "Ta'mir: karobka (ta'mirsiz yangi uy)",
    "Renovation: shell (new building, no renovation)")
add(" — можно заезжать", " — ko'chib kirsa bo'ladi", " — move-in ready")
add(" — освежить ~${v}", " — yangilash ~${v}", " — refresh ~${v}")
add(" — ремонт ~${v}", " — ta'mir ~${v}", " — renovation ~${v}")
add("; в объявлении «{c}»", "; e'londa «{c}»", "; listing says «{c}»")
add("Ремонт не понять — уточните у продавца", "Ta'mirni tushunib bo'lmadi — sotuvchidan so'rang",
    "Renovation unclear — ask the seller")
add("Собственник", "Uy egasi", "Owner")
add("Маклер / агентство", "Makler / agentlik", "Broker / agency")
add("Продавец не проверен", "Sotuvchi tekshirilmagan", "Seller not verified")
add("Застройщик", "Quruvchi", "Developer")
add("выложено {d}", "joylangan {d}", "posted {d}")
add("перевыложено", "qayta joylangan", "reposted")
add(", на рынке ~{n} дн.", ", bozorda ~{n} kun", ", on the market ~{n} days")
add("Эту же продают ещё: {refs}", "Shu uyni yana sotishyapti: {refs}", "Also listed elsewhere: {refs}")
add(" и ещё {n}", " va yana {n} ta", " and {n} more")
add("⚠️ Бывшее общежитие — проверьте кадастр", "⚠️ Sobiq yotoqxona — kadastrni tekshiring",
    "⚠️ Former dormitory — check the cadastre")
add("Ипотека", "Ipoteka", "Mortgage")
add(" · взнос ${down}, ≈ {mln} млн сум/мес на {years} лет",
    " · boshlang'ich ${down}, ≈ oyiga {mln} mln so'm, {years} yilga",
    " · down payment ${down}, ≈ {mln} mln UZS/month for {years} years")
add("Торг", "Savdo", "Bargain")
add("просите ~${v}, цена уже ниже рынка", "~${v} so'rang, narx bozordan past",
    "ask ~${v}, already below market")
add("начните с ~${a}, потолок ~${b}", "~${a} dan boshlang, chegara ~${b}", "start at ~${a}, ceiling ~${b}")
add("Сдавать", "Ijaraga berish", "Renting out")
add("~${r}/мес, {y}% в год чистыми", "~${r}/oy, yiliga sof {y}%", "~${r}/month, {y}% a year net")
add("На фото: {s}", "Rasmda: {s}", "In the photos: {s}")
add("Ипотека ~${p}/мес против аренды такой же ~${r}", "Ipoteka ~${p}/oy, xuddi shunday ijara ~${r}",
    "Mortgage ~${p}/month vs renting the same ~${r}")
add(" — дороже на ${d}, зато квартира ваша", " — ${d} qimmat, lekin kvartira sizniki",
    " — ${d} more, but the flat is yours")
add(" — выгоднее брать", " — sotib olish foydaliroq", " — buying pays off")
add("Аргументы в торге: {a}", "Savdo dalillari: {a}", "Bargaining points: {a}")
add("Вклад в $ даёт {p}%", "$ omonat {p}% beradi", "A $ deposit gives {p}%")
add("; окупится за ~{y} лет", "; ~{y} yilda o'zini oqlaydi", "; pays back in ~{y} years")
add("Через 5 лет — скорее ~${b} (${p}–${o})", "5 yildan keyin — taxminan ~${b} (${p}–${o})",
    "In 5 years — likely ~${b} (${p}–${o})")
add("Сравнивала с {n} объявл.: {label}, медиана ${m}/м²", "{n} ta e'lon bilan solishtirdim: {label}, mediana ${m}/m²",
    "Compared with {n} listings: {label}, median ${m}/m²")
add("Цена менялась: ${a} → ${b}", "Narx o'zgargan: ${a} → ${b}", "Price changed: ${a} → ${b}")
add("В тексте: «{f}»", "Matnda: «{f}»", "In the text: «{f}»")
add("Открыть на {site}", "{site} da ochish", "Open on {site}")
add("карта цен", "narxlar xaritasi", "price map")
add("как считаю", "qanday hisoblayman", "how I calculate")
add("Подробнее", "Batafsil", "Details")
add("Из объявления: «{d}»", "E'londan: «{d}»", "From the listing: «{d}»")

# место
add("Место — {bits}", "Joy — {bits}", "Location — {bits}")
add("Место — точный адрес не указан, где-то в районе {d}; спросите ориентир у продавца",
    "Joy — aniq manzil ko'rsatilmagan, taxminan {d} tumanida; sotuvchidan mo'ljalni so'rang",
    "Location — exact address not given, somewhere in {d}; ask the seller for a landmark")
add("в центре", "markazda", "in the centre")
add("у центра, {km} км до середины", "markaz yonida, o'rtasigacha {km} km", "near the centre, {km} km from its middle")
add("не центр, {km} км до середины", "markaz emas, o'rtasigacha {km} km", "not the centre, {km} km from its middle")
add("м. {name} ~{min} мин пешком", "{name} metrosi ~{min} daqiqa piyoda", "{name} metro ~{min} min walk")
add("до метро {km} км", "metrogacha {km} km", "{km} km to metro")
add("дорогая часть района", "tumanning qimmat qismi", "pricier part of the district")
add("недорогая часть района", "tumanning arzon qismi", "cheaper part of the district")
add("средняя по цене часть района", "tumanning o'rtacha narxdagi qismi", "mid-priced part of the district")
add("{w} ({pct} к району)", "{w} (tumanga nisbatan {pct})", "{w} ({pct} vs district)")

# ипотека (строка)
add("Ипотека — нет: «{q}»", "Ipoteka — yo'q: «{q}»", "Mortgage — no: «{q}»")
add("Ипотека — нет (по пометке сайта)", "Ipoteka — yo'q (sayt belgisiga ko'ra)", "Mortgage — no (per the site)")
add("Ипотека — в объявлении не указано. Бывшее общежитие: без кадастра банки не дают",
    "Ipoteka — e'londa ko'rsatilmagan. Sobiq yotoqxona: kadastrsiz banklar bermaydi",
    "Mortgage — not stated. Former dormitory: banks won't lend without a cadastre")
add("Ипотека — можно («{q}»)", "Ipoteka — mumkin («{q}»)", "Mortgage — possible («{q}»)")
add("Ипотека — можно (по пометке сайта)", "Ipoteka — mumkin (sayt belgisiga ko'ra)", "Mortgage — possible (per the site)")
add("Ипотека — в объявлении не указано, спросите продавца", "Ipoteka — e'londa ko'rsatilmagan, sotuvchidan so'rang",
    "Mortgage — not stated, ask the seller")
add("; квартира сама в залоге у банка — узнайте, как снимут залог при сделке",
    "; kvartira bankda garovda — bitimda garov qanday olinishini so'rang",
    "; the flat itself is mortgaged — ask how the lien is cleared at the deal")
add("новостройки", "yangi uylar", "new buildings")
add("Рядом ({rad} км) {kind} — ${loc}/м² ({n} объявл.), по району ${dm}/м² ({dn})",
    "Yaqin atrofda ({rad} km) {kind} — ${loc}/m² ({n} ta e'lon), tuman bo'yicha ${dm}/m² ({dn})",
    "Nearby ({rad} km) {kind} — ${loc}/m² ({n} listings), district ${dm}/m² ({dn})")
add("точку поставила по ориентиру «{label}» — адрес уточните", "nuqtani «{label}» mo'ljali bo'yicha qo'ydim — manzilni aniqlang",
    "pin placed by the landmark «{label}» — confirm the address")

# районы Ташкента: uz — как пишут по-узбекски, en — привычная латиница
DISTRICTS_TR = {
    "Алмазар": ("Olmazor", "Almazar"), "Бектемир": ("Bektemir", "Bektemir"), "Мирабад": ("Mirobod", "Mirabad"),
    "Мирзо-Улугбек": ("Mirzo Ulug'bek", "Mirzo-Ulugbek"), "Сергели": ("Sergeli", "Sergeli"),
    "Чиланзар": ("Chilonzor", "Chilanzar"), "Шайхантахур": ("Shayxontohur", "Shaykhantakhur"),
    "Юнусабад": ("Yunusobod", "Yunusabad"), "Яккасарай": ("Yakkasaroy", "Yakkasaray"),
    "Яшнабад": ("Yashnobod", "Yashnabad"), "Учтепа": ("Uchtepa", "Uchtepa"), "Янгихаёт": ("Yangihayot", "Yangihayot"),
    "Ташкент": ("Toshkent", "Tashkent"), "весь Ташкент": ("butun Toshkent", "all of Tashkent"),
}


def dist(name: str) -> str:
    """Район на языке клиента."""
    if LANG == "ru" or not name:
        return name or ""
    tr = DISTRICTS_TR.get(name)
    return (tr[0] if LANG == "uz" else tr[1]) if tr else name
add("бывшие общежития", "sobiq yotoqxonalar", "former dormitories")
add(", хороший ремонт", ", yaxshi ta'mir", ", good renovation")
add(", средний ремонт", ", o'rtacha ta'mir", ", average renovation")
add(", коробка", ", karobka", ", shell")
add(" от «{label}»", " «{label}» dan", " from «{label}»")
add("в радиусе {rad} км{where}, {kind}{rep}, {rng}", "{rad} km radiusda{where}, {kind}{rep}, {rng}",
    "within {rad} km{where}, {kind}{rep}, {rng}")
add("вся продажа", "barcha sotuv", "all sales")


# длинные тексты и короткие фразы, переведённые вручную
import i18n_texts as _tx  # noqa: E402
for _d in (_tx.HELP, _tx.WELCOME, _tx.VIEW_HINT, _tx.GUIDE, _tx.GUIDE_ON, _tx.MANUAL):
    add(_d["ru"], _d["uz"], _d["en"])
for _ru, _uz, _en in _tx.SHORT + _tx.MORE:
    add(_ru, _uz, _en)


def quote_for_client(cfg, text: str) -> str:
    """Слова маклера клиенту: маклер по-узбекски, клиент по-русски — добавляем перевод.
    Клиенту на узбекском/английском всё сообщение и так переведёт модель."""
    if LANG == "ru" and detect_lang(text or "") == "uz":
        tr = ai(cfg, text, "ru")
        if tr and tr != text:
            return f"{text}\n(перевод: {tr})"
    return text

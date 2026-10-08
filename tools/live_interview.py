"""Живые сценарии интервью Ra'no через воркер /svc/try (настоящий Gemini, без Telegram).
Запуск: python3 live_interview.py  (ключ берётся из ~/.rano_svc_key)"""
import json, os, re, time, urllib.parse
import requests

W = "https://rano-bot.sh-pulatov.workers.dev"
KEY = open(os.path.expanduser("~/.rano_svc_key")).read().strip()
H = {"x-svc": KEY, "user-agent": "rano-radar/1.0"}

SCEN = {
  "покупка+ипотека": ["Привет! Хочу купить двушку ближе к центру до 50 тысяч долларов, нужна ипотека",
                      "Юнусабад или Мирабад, ремонт не важен, этаж не первый"],
  "аренда Мирабад": ["снять трёшку в Мирабаде до $1400", "мы пара с кошкой, на год, заезд с 1 ноября, с мебелью"],
  "посуточно": ["нужна квартира посуточно в центре Ташкента с 20 по 23 октября, до 60$ в сутки, 1 комната"],
  "узбекский": ["Assalomu alaykum! Yunusobodda 2 xonali kvartira ijaraga kerak, oyiga 600 dollargacha"],
  "английский": ["Hi, I want to rent a 1-bedroom flat in Tashkent near the center, up to $700 a month, from next week"],
  "размыто": ["ищу жильё", "ну что-нибудь недорогое"],
  "смена бюджета": ["купить 3-комнатную в Чиланзаре до 70 тысяч", "остальное неважно, ищи", "бюджет 80 тысяч и добавь Учтепу"],
  "оффтоп": ["какая сегодня погода в Ташкенте?"],
  "сумы": ["снять однушку в Яккасарае до 8 миллионов сум в месяц"],
}
MASC = re.compile(r"\b(я\s+)?(понял|нашёл|нашел|записал|сделал|собрал|уточнил|поменял|добавил|сохранил|спросил|рад)\b", re.I)
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
BAD = re.compile(r"(отправил[аи]? маклерам|передала маклерам|уже отправ|разослала)", re.I)

out = {}
for name, turns in SCEN.items():
    log = []
    for i, t in enumerate(turns):
        q = {"text": t}
        if i == 0:
            q["reset"] = "1"
        for attempt in range(3):
            r = requests.get(f"{W}/svc/try?" + urllib.parse.urlencode(q), headers=H, timeout=60)
            if r.status_code == 200:
                break
            time.sleep(3)
        d = r.json()
        reply = d.get("reply") or ""
        log.append({"user": t, "reply": reply, "ready": d.get("ready"), "intent": d.get("intent"),
                    "masc": MASC.findall(reply), "emoji": len(EMOJI.findall(reply)),
                    "bad_claim": bool(BAD.search(reply))})
        ans = d.get("ans")
        time.sleep(1)
    out[name] = {"turns": log, "ans": ans}
    print(f"\n=== {name} ===")
    for x in log:
        print(f"👤 {x['user']}\n🤖 {x['reply']}\n   ready={x['ready']} intent={x['intent']} emoji={x['emoji']}"
              + (f" МУЖ.РОД={x['masc']}" if x['masc'] else "") + (" ⚠️ЛОЖНОЕ ОБЕЩАНИЕ" if x['bad_claim'] else ""))
    print("   ans:", json.dumps(ans, ensure_ascii=False))
json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_interview_result.json"), "w"),
          ensure_ascii=False, indent=1)

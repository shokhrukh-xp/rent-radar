"""Живые сценарии разбора вариантов маклеров через воркер /svc/parse (настоящий Gemini)."""
import json, os, time
import requests

W = "https://rano-bot.sh-pulatov.workers.dev"
H = {"x-svc": open(os.path.expanduser("~/.rano_svc_key")).read().strip(), "user-agent": "rano-radar/1.0"}
CASES = [
  ("продажа ru", "sale", "Продаётся 2 комн квартира, Мирабад, ул. Шахрисабз 14, ориентир Ойбек. 5/9 этаж, 58 м², евроремонт, кадастр готов. Цена 52 000 у.е., торг. Комиссия 1%"),
  ("продажа uz", "sale", "Sotiladi! Yunusobod 13-kvartal, 3 xonali, 4/9 qavat, 72 kv.m, remont zo'r. Narxi 68 ming $. Ipoteka bor"),
  ("первый взнос", "sale", "ФИКС ЦЕНА! ЖК Мерос Мирабад 2ком 47 м² 13/16, П/В 23 300 у.е., полная цена 77 550 у.е. Ипотека"),
  ("цена за м²", "sale", "Новостройка Яккасарай, 2 комнаты 55 м², 6/12, цена 1150$ за квадрат, коробка"),
  ("аренда вместо продажи", "sale", "Сдаётся 2 комн квартира в Юнусабаде, 600$ в месяц, депозит 1 месяц, есть мебель"),
  ("просто реплика", "sale", "Здравствуйте, у меня есть варианты, перезвоню вам вечером"),
  ("аренда ok", "rent", "Сдаю 3 комнатную Чиланзар 9 квартал, 4/5, 80м2, мебель и техника, 700 у.е. в месяц + комиссия 50%"),
]
res = []
for name, deal, text in CASES:
    r = requests.post(f"{W}/svc/parse", headers=H, json={"text": text, "deal": deal}, timeout=60)
    d = r.json() if r.status_code == 200 else {"http": r.status_code}
    res.append({"case": name, "text": text, "out": d})
    print(f"\n=== {name} ===\n{text}\n→ {json.dumps(d.get('offer', d), ensure_ascii=False)}")
    time.sleep(1)
json.dump(res, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_parse_result.json"), "w"),
          ensure_ascii=False, indent=1)

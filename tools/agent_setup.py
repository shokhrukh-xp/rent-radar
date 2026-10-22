"""Подключить аккаунт Ra'no (пишет продавцам первой) и SMS. Запускает ВЛАДЕЛЕЦ сам, на сервере:

  ssh -t sonar docker exec -it -e RADAR_STATE_DIR=/opt/rano/state rano python /opt/rano/app/tools/agent_setup.py telegram
  ssh -t sonar docker exec -it -e RADAR_STATE_DIR=/opt/rano/state rano python /opt/rano/app/tools/agent_setup.py sms
  ssh -t sonar docker exec -it -e RADAR_STATE_DIR=/opt/rano/state rano python /opt/rano/app/tools/agent_setup.py status

telegram — api_id и api_hash (my.telegram.org → API development tools, войти номером аккаунта Ra'no), номер,
           код из Telegram и облачный пароль, если есть. Ввод пароля скрыт. Сессия — в /opt/rano/state/agent/
           (права 600, в бэкап не попадает). Отключить: Telegram аккаунта Ra'no → Настройки → Устройства.
sms      — почта и пароль кабинета Eskiz (notify.eskiz.uz), имя отправителя (по умолчанию 4546).
           Текст SMS должен быть одобрен в кабинете Eskiz — он печатается ниже.
status   — что подключено (без ключей).
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import outreach  # noqa: E402


def telegram():
    if not outreach._ensure_telethon():
        sys.exit("telethon не установился — проверьте интернет на сервере")
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession
    api_id = input("api_id: ").strip()
    api_hash = getpass.getpass("api_hash (ввод скрыт): ").strip()
    phone = input("Номер аккаунта Ra'no (+998…): ").strip()
    c = TelegramClient(StringSession(), int(api_id), api_hash, device_model="Ra'no server", app_version="1.0",
                       system_version="Linux")
    c.start(phone=phone, code_callback=lambda: input("Код из Telegram: ").strip(),
            password=lambda: getpass.getpass("Облачный пароль (если нет — Enter): "))
    me = c.get_me()
    outreach.save_conf("telegram", {"api_id": int(api_id), "api_hash": api_hash, "session": c.session.save()})
    c.disconnect()
    print(f"✅ Аккаунт подключён: {me.first_name or ''} (@{me.username or '—'}). Ra'no начнёт писать продавцам "
          "в ближайший проход.")


def sms():
    email = input("Почта кабинета Eskiz: ").strip()
    password = getpass.getpass("Пароль Eskiz (ввод скрыт): ").strip()
    sender = input("Отправитель (Enter — 4546): ").strip() or "4546"
    s = outreach.EskizSMS({"email": email, "password": password})
    try:
        s._login()
    except Exception as e:
        sys.exit(f"Не вошла в Eskiz: {e}")
    outreach.save_conf("sms", {"email": email, "password": password, "from": sender})
    print("✅ Eskiz подключён. Отправьте в кабинете Eskiz на модерацию шаблон:\n")
    print(outreach.SMS_TEXT.replace("{bot}", "rano_smart_bot").replace("{code}", "s1a2b3-4c"))
    print("\n(меняется только код после start= — укажите это как переменную часть шаблона)")


def status():
    print("Аккаунт Ra'no:", "подключён" if outreach.tg_ready() else "нет")
    print("SMS (Eskiz):", "подключено" if outreach.sms_ready() else "нет")


if __name__ == "__main__":
    {"telegram": telegram, "sms": sms, "status": status}.get((sys.argv[1:] or ["status"])[0], status)()

#!/bin/bash
# Запускает ВЛАДЕЛЕЦ на своём Mac (не Claude): ключи остаются у вас.
#  1) добавляет серверный deploy-ключ в приватный репозиторий rent-radar-state (запись бэкапа);
#  2) пишет /opt/rano/.env на сервере: токен бота (спросит, ввод скрыт), ключ воркера из ~/.rano_svc_key,
#     ваш chat id (берётся у воркера), адрес воркера. Файл — только для root (600).
set -euo pipefail
HOST=sonar
W=https://rano-bot.sh-pulatov.workers.dev
SVC=$(cat ~/.rano_svc_key)
PUB=$(ssh $HOST cat /opt/rano/keys/deploy_key.pub)
gh repo deploy-key add <(echo "$PUB") -R shokhrukh-xp/rent-radar-state -t "rano-server (sonar)" -w \
  || echo "ключ уже добавлен — идём дальше"
CHAT=$(curl -fsS -H "x-svc: $SVC" "$W/svc/owner" | python3 -c 'import json,sys; print(json.load(sys.stdin)["chat"] or "")')
[ -n "$CHAT" ] || { read -r -p "Ваш Telegram chat id: " CHAT; }
read -r -s -p "Токен бота (@BotFather → /mybots → Ra'no → API Token): " TOKEN; echo
[ -n "$TOKEN" ] || { echo "токен пустой — выхожу"; exit 1; }
printf 'RADAR_BOT_TOKEN=%s\nRADAR_CHAT_ID=%s\nRADAR_WORKER_URL=%s\nRADAR_WORKER_KEY=%s\n' "$TOKEN" "$CHAT" "$W" "$SVC" \
  | ssh $HOST 'umask 077; cat > /opt/rano/.env && chmod 600 /opt/rano/.env && echo "✅ /opt/rano/.env записан"'
unset TOKEN SVC

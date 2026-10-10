#!/bin/sh
# Ra'no на сервере: бесконечный цикл из 10-минутных проходов, как раньше в GitHub Actions.
# Перед каждым проходом подтягиваем код (деплой = git push в main),
# после — сохраняем базу в приватный репозиторий rent-radar-state (бэкап).
export GIT_SSH_COMMAND="ssh -i /opt/rano/keys/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/rano/keys/known_hosts"
export RADAR_STATE_DIR=/opt/rano/state
cd /opt/rano
git -C state config user.name "rano-server"
git -C state config user.email "rano@sonar"
while true; do
  git -C app pull -q --ff-only || echo "код не обновился — работаю на прежнем"
  (cd app && python rent_radar.py --minutes 10) || sleep 20
  git -C state add radar.db sale.db 2>/dev/null
  git -C state commit -q --amend -m "state $(date -u +'%Y-%m-%d %H:%M') (сервер)" 2>/dev/null
  git -C state push -q --force origin HEAD:main || echo "бэкап не отправился — попробую через 10 минут"
done

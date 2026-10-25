#!/bin/sh
# Выкладка одной командой с Mac: ./deploy/mac_deploy.sh [патч]
# Подтягивает чужие изменения (Codex, Claude), применяет патч, выкладывает воркер (если он менялся с прошлой
# выкладки воркера), отправляет код в GitHub, сервер подтягивает его и перезапускает проход
# (бот продолжает с сохранённого места). Секретов не трогает.
set -e
P="$1"
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
G="GIT_TERMINAL_PROMPT=0"
WD=.git/rano_worker_deployed                 # с какого коммита выложен воркер (общий для всех, кто выкладывает)
env $G git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=20 pull -q --rebase --autostash origin main
before=$(cat "$WD" 2>/dev/null || git rev-parse HEAD)
[ -n "$P" ] && git am -3 "$P"
if ! git diff --quiet "$before" HEAD -- worker; then
  if (cd worker && npx --yes wrangler deploy > /tmp/wdeploy.log 2>&1); then
    grep "Current Version ID" /tmp/wdeploy.log; git rev-parse HEAD > "$WD"
  else
    echo "воркер: см. /tmp/wdeploy.log"
  fi
else
  git rev-parse HEAD > "$WD"
fi
push() { env $G git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=20 push -q origin main; }
push || { env $G git pull -q --rebase origin main && push; }   # кто-то успел раньше — встаём поверх и ещё раз
ssh sonar "docker exec rano sh -c 'cd /opt/rano/app && git pull -q --ff-only && git log --oneline -1' && \
  docker exec rano python3 -c \"import os,signal
for p in os.listdir('/proc'):
    if p.isdigit():
        try:
            c = open('/proc/%s/cmdline' % p, 'rb').read()
        except OSError:
            continue
        if b'rent_radar.py' in c and b'--minutes' in c:
            os.kill(int(p), signal.SIGTERM); print('перезапуск прохода', p)\""

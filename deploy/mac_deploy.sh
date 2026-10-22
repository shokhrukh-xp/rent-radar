#!/bin/sh
# Выкладка одной командой с Mac: ./deploy/mac_deploy.sh <патч>
# Применяет патч, выкладывает воркер (если он менялся), отправляет код в GitHub, сервер подтягивает его
# и перезапускает проход (бот продолжает с сохранённого места). Секретов не трогает.
set -e
P="$1"
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
before=$(git rev-parse origin/main)          # что уже выложено
[ -n "$P" ] && git am "$P"
if ! git diff --quiet "$before" HEAD -- worker; then
  (cd worker && npx --yes wrangler deploy > /tmp/wdeploy.log 2>&1) && grep "Current Version ID" /tmp/wdeploy.log || echo "воркер: см. /tmp/wdeploy.log"
fi
GIT_TERMINAL_PROMPT=0 git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=20 push -q origin main
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

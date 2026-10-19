#!/usr/bin/env bash
# Установка на Ubuntu 22.04+ / Debian 12+. Запускать от root из корня репозитория.
# Повторный запуск обновляет код и перезапускает службы.
set -euo pipefail

python3 -c 'import sys; assert sys.version_info >= (3, 10), "нужен Python 3.10+"'

id kmradar >/dev/null 2>&1 || useradd --system --home /var/lib/kmradar --shell /usr/sbin/nologin kmradar
mkdir -p /opt/kmradar /var/lib/kmradar/data
rm -rf /opt/kmradar/kmradar
cp -r kmradar fixtures pyproject.toml /opt/kmradar/
chown -R kmradar:kmradar /var/lib/kmradar

if [ ! -f /etc/kmradar.env ]; then
  install -m 600 deploy/kmradar.env.example /etc/kmradar.env
  echo "Впишите домен в /etc/kmradar.env (BASE_URL) и запустите скрипт ещё раз."
  exit 0
fi
# shellcheck disable=SC1091
source /etc/kmradar.env

cp deploy/kmradar-web.service deploy/kmradar-daily.service deploy/kmradar-daily.timer \
   deploy/kmradar-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kmradar-web.service kmradar-daily.timer
systemctl restart kmradar-web.service
if [ -n "${TELEGRAM_TOKEN:-}" ]; then
  systemctl enable --now kmradar-bot.service
  systemctl restart kmradar-bot.service
fi

# HTTPS через Caddy
if ! command -v caddy >/dev/null; then
  apt-get update -q
  apt-get install -y -q caddy || {
    echo "Пакета caddy нет в репозиториях этой системы."
    echo "Установите по инструкции https://caddyserver.com/docs/install#debian-ubuntu-raspbian и запустите скрипт ещё раз."
    exit 1
  }
fi
domain="${BASE_URL#https://}"; domain="${domain%%/*}"
if [ -n "$domain" ] && [ "$domain" != "radar.example.ru" ]; then
  sed "s/radar.example.ru/$domain/" deploy/Caddyfile > /etc/caddy/Caddyfile
  systemctl reload caddy || systemctl restart caddy
  echo "Сайт: https://$domain (домен должен указывать на IP этого сервера, порты 80 и 443 открыты)"
else
  echo "BASE_URL не задан — Caddy не настроен."
fi

cat <<MSG
Готово.
Профили КМ:  cd /opt/kmradar && sudo -u kmradar python3 -m kmradar add-km --db /var/lib/kmradar/radar.db /path/kms.json
Ссылки КМ:   cd /opt/kmradar && sudo -u kmradar python3 -m kmradar link --db /var/lib/kmradar/radar.db --base-url $BASE_URL all
MSG

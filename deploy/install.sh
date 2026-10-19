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

if ss -ltn | grep -q '127.0.0.1:8080 \|0.0.0.0:8080 \|\*:8080 ' && ! systemctl is-active --quiet kmradar-web; then
  echo "Порт 8080 занят другим приложением. Поменяйте --port в deploy/kmradar-web.service и порт в deploy/Caddyfile."
  exit 1
fi
cp deploy/kmradar-web.service deploy/kmradar-daily.service deploy/kmradar-daily.timer \
   deploy/kmradar-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kmradar-web.service kmradar-daily.timer
systemctl restart kmradar-web.service
if [ -n "${TELEGRAM_TOKEN:-}" ]; then
  systemctl enable --now kmradar-bot.service
  systemctl restart kmradar-bot.service
fi

# HTTPS через Caddy. Существующую настройку Caddy НЕ трогаем никогда.
domain="${BASE_URL#https://}"; domain="${domain%%/*}"
block="$(sed "s/radar.example.ru/$domain/" deploy/Caddyfile)"
if [ -z "$domain" ] || [ "$domain" = "radar.example.ru" ]; then
  echo "BASE_URL не задан — HTTPS не настроен."
elif command -v caddy >/dev/null || command -v nginx >/dev/null; then
  echo "На сервере уже есть веб-сервер — его настройку скрипт не меняет."
  echo "Добавьте в свой Caddyfile этот блок и выполните: systemctl reload caddy"
  echo "----------------------------------------"
  echo "$block"
  echo "----------------------------------------"
else
  apt-get update -q
  apt-get install -y -q caddy || {
    echo "Пакета caddy нет в репозиториях этой системы."
    echo "Установите по инструкции https://caddyserver.com/docs/install#debian-ubuntu-raspbian и запустите скрипт ещё раз."
    exit 1
  }
  # свежий Caddy из пакета: заменяем только его стандартную заглушку, с копией
  [ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%Y%m%d%H%M%S)"
  echo "$block" > /etc/caddy/Caddyfile
  systemctl reload caddy || systemctl restart caddy
  echo "Сайт: https://$domain (домен должен указывать на IP этого сервера, порты 80 и 443 открыты)"
fi

cat <<MSG
Готово.
Профили КМ:  cd /opt/kmradar && sudo -u kmradar python3 -m kmradar add-km --db /var/lib/kmradar/radar.db /path/kms.json
Ссылки КМ:   cd /opt/kmradar && sudo -u kmradar python3 -m kmradar link --db /var/lib/kmradar/radar.db --base-url $BASE_URL all
MSG

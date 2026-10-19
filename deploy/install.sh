#!/usr/bin/env bash
# Установка на Ubuntu 22.04+ / Debian 12+. Запускать от root из корня репозитория.
set -euo pipefail

python3 -c 'import sys; assert sys.version_info >= (3, 10), "нужен Python 3.10+"'

id kmradar >/dev/null 2>&1 || useradd --system --home /var/lib/kmradar --shell /usr/sbin/nologin kmradar
mkdir -p /opt/kmradar /var/lib/kmradar/data
cp -r kmradar fixtures pyproject.toml /opt/kmradar/
chown -R kmradar:kmradar /var/lib/kmradar

if [ ! -f /etc/kmradar.env ]; then
  install -m 600 deploy/kmradar.env.example /etc/kmradar.env
  echo "Заполните /etc/kmradar.env (TELEGRAM_TOKEN) и запустите скрипт ещё раз."
  exit 0
fi

cp deploy/kmradar-bot.service deploy/kmradar-daily.service deploy/kmradar-daily.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kmradar-bot.service kmradar-daily.timer
echo "Готово. Профили КМ: sudo -u kmradar python3 -m kmradar add-km --db /var/lib/kmradar/radar.db kms.json (из /opt/kmradar)"

#!/usr/bin/env bash
# Установка на Ubuntu 22.04+ / Debian 12+. Запускать от root из корня репозитория.
# Повторный запуск обновляет код и перезапускает службы.
#
# Чужие настройки не трогаем:
#  - основной /etc/caddy/Caddyfile и nginx никогда не меняются;
#  - если Caddyfile подключает /etc/caddy/sites/*.caddy, радар кладёт туда
#    только свой файл kmradar.caddy и проверяет весь конфиг до перезагрузки;
#  - порт радара выбирается свободный и записывается в /etc/kmradar.env.
set -euo pipefail

CADDY_DIR="${CADDY_DIR:-/etc/caddy}"
ENV_FILE=/etc/kmradar.env

python3 -c 'import sys; assert sys.version_info >= (3, 10), "нужен Python 3.10+"'

port_busy() { ss -ltn "( sport = :$1 )" | grep -q LISTEN; }

id kmradar >/dev/null 2>&1 || useradd --system --home /var/lib/kmradar --shell /usr/sbin/nologin kmradar
mkdir -p /opt/kmradar /var/lib/kmradar/data
rm -rf /opt/kmradar/kmradar
cp -r kmradar fixtures pyproject.toml /opt/kmradar/
chown -R kmradar:kmradar /var/lib/kmradar

if [ ! -f "$ENV_FILE" ]; then
  install -m 600 deploy/kmradar.env.example "$ENV_FILE"
  echo "Впишите домен в $ENV_FILE (BASE_URL) и запустите скрипт ещё раз."
  exit 0
fi

# Порт радара: свой, свободный, один раз записывается в настройки
if ! grep -q '^RADAR_PORT=[0-9]' "$ENV_FILE"; then
  systemctl stop kmradar-web 2>/dev/null || true
  port=""
  for p in $(seq 8091 8199); do
    if ! port_busy "$p"; then port=$p; break; fi
  done
  [ -n "$port" ] || { echo "Не нашёл свободный порт в диапазоне 8091–8199."; exit 1; }
  sed -i '/^RADAR_PORT=/d' "$ENV_FILE"
  echo "RADAR_PORT=$port" >> "$ENV_FILE"
  echo "Порт радара: $port"
fi
# shellcheck disable=SC1090
source "$ENV_FILE"
if port_busy "$RADAR_PORT" && ! systemctl is-active --quiet kmradar-web; then
  echo "Порт $RADAR_PORT занят другим приложением. Удалите строку RADAR_PORT из $ENV_FILE и запустите скрипт снова."
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

# HTTPS
domain="${BASE_URL#https://}"; domain="${domain%%/*}"
block="$(sed -e "s/radar.example.ru/$domain/" -e "s/RADAR_PORT/$RADAR_PORT/" deploy/Caddyfile)"
show_block() {
  echo "Добавьте этот блок в настройку своего веб-сервера вручную:"
  echo "----------------------------------------"
  echo "$block"
  echo "----------------------------------------"
}

if [ -z "$domain" ] || [ "$domain" = "radar.example.ru" ]; then
  echo "BASE_URL не задан — HTTPS не настроен."
elif command -v caddy >/dev/null; then
  if grep -qE "^[[:space:]]*import[[:space:]]+($CADDY_DIR/)?sites/\*(\.caddy)?" "$CADDY_DIR/Caddyfile" 2>/dev/null; then
    site="$CADDY_DIR/sites/kmradar.caddy"
    had_site=0; [ -f "$site" ] && { had_site=1; cp "$site" /tmp/kmradar.caddy.prev; }
    mkdir -p "$CADDY_DIR/sites"
    echo "$block" > "$site"
    if (cd "$CADDY_DIR" && caddy validate --adapter caddyfile --config "$CADDY_DIR/Caddyfile" >/tmp/kmradar-caddy-validate.log 2>&1); then
      systemctl reload caddy
      echo "Сайт: https://$domain (подключён файлом $site, остальной конфиг Caddy не менялся)"
    else
      if [ "$had_site" = 1 ]; then cp /tmp/kmradar.caddy.prev "$site"; else rm -f "$site"; fi
      echo "Конфиг Caddy с радаром не прошёл проверку — файл радара убран, Caddy не перезагружался."
      tail -3 /tmp/kmradar-caddy-validate.log
      exit 1
    fi
  else
    echo "Caddy уже настроен, а строки «import $CADDY_DIR/sites/*.caddy» в нём нет — конфиг не трогаю."
    show_block
  fi
elif command -v nginx >/dev/null; then
  echo "На сервере nginx — его настройку скрипт не меняет. Проксируйте домен $domain на 127.0.0.1:$RADAR_PORT."
else
  apt-get update -q
  apt-get install -y -q caddy || {
    echo "Пакета caddy нет в репозиториях этой системы."
    echo "Установите по инструкции https://caddyserver.com/docs/install#debian-ubuntu-raspbian и запустите скрипт ещё раз."
    exit 1
  }
  # свежий Caddy из пакета: заменяем только его стандартную заглушку, с копией
  [ -f "$CADDY_DIR/Caddyfile" ] && cp "$CADDY_DIR/Caddyfile" "$CADDY_DIR/Caddyfile.bak.$(date +%Y%m%d%H%M%S)"
  echo "$block" > "$CADDY_DIR/Caddyfile"
  systemctl reload caddy || systemctl restart caddy
  echo "Сайт: https://$domain (домен должен указывать на IP этого сервера, порты 80 и 443 открыты)"
fi

cat <<MSG
Готово.
Профили КМ:  cd /opt/kmradar && sudo -u kmradar python3 -m kmradar add-km --db /var/lib/kmradar/radar.db /var/lib/kmradar/kms.json
Ссылки КМ:   cd /opt/kmradar && sudo -u kmradar python3 -m kmradar link --db /var/lib/kmradar/radar.db --base-url $BASE_URL all
MSG

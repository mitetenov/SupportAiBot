#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== Peipivo VPN Support Bot ===${NC}"
echo ""

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo -e "${YELLOW}Создан .env из .env.example.${NC}"
        echo -e "${YELLOW}Отредактируйте .env (TELEGRAM_BOT_TOKEN, REMNAWAVE_BASE_URL и т.д.)${NC}"
        echo -e "${YELLOW}и запустите скрипт снова.${NC}"
        exit 0
    else
        echo -e "${RED}Нет ни .env, ни .env.example. Создайте .env вручную.${NC}"
        exit 1
    fi
fi

REQUIRED_VARS=(TELEGRAM_BOT_TOKEN TELEGRAM_SUPPORT_GROUP_CHAT_ID REMNAWAVE_BASE_URL REMNAWAVE_API_TOKEN PG_PASSWORD)
MISSING=()
while IFS='=' read -r key value; do
    key=$(echo "$key" | xargs)
    for req in "${REQUIRED_VARS[@]}"; do
        if [ "$key" = "$req" ] && ([ -z "$value" ] || [ "$value" = "your_${req}_here" ] || [ "$value" = "your_secure_password" ]); then
            MISSING+=("$req")
        fi
    done
done < <(grep -v '^#' .env | grep -v '^$')

if [ ${#MISSING[@]} -gt 0 ]; then
    echo -e "${RED}Не заполнены обязательные переменные в .env:${NC}"
    for m in "${MISSING[@]}"; do echo "  - $m"; done
    exit 1
fi

echo -e "${GREEN}Загрузка образа и запуск...${NC}"
docker compose pull
docker compose up -d

echo ""
echo -e "${GREEN}Проверка состояния...${NC}"
sleep 5
docker compose ps

echo ""
echo -e "${GREEN}Готово. Логи: docker compose logs -f bot${NC}"

#!/bin/sh
set -eu

SERVICE_NAME="${SERVICE_NAME:-agentic_farm_assistant}"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    echo "Docker Compose is not installed."
    exit 1
fi

echo "Pulling latest image for ${SERVICE_NAME}..."
$COMPOSE_CMD pull "${SERVICE_NAME}"

echo "Recreating ${SERVICE_NAME} with the pulled image..."
$COMPOSE_CMD up -d "${SERVICE_NAME}"

echo "Current status:"
$COMPOSE_CMD ps

echo "Done."

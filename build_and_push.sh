#!/bin/sh
set -eu

REGISTRY="${REGISTRY:-ghcr.io/pranavnbapat}"
IMAGE_NAME="${IMAGE_NAME:-$REGISTRY/agentic_farm_assistant}"
TAG="${1:-latest}"

echo "Building image: ${IMAGE_NAME}:${TAG}"
docker build -t "${IMAGE_NAME}:${TAG}" .

echo "Pushing image: ${IMAGE_NAME}:${TAG}"
docker push "${IMAGE_NAME}:${TAG}"

echo "Done."
echo "Image: ${IMAGE_NAME}:${TAG}"

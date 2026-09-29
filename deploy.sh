#!/usr/bin/env bash
#
# Deploy the ingestion service on the server.
#
#     cd ~/invoice-automation/invoice-automation-ingestion && ./deploy.sh
#
# Same shape as the backend script: build before touching the running
# container, keep a :prev image, roll back automatically if the new one does
# not come up.
set -euo pipefail

SERVICE=ingestion_service
IMAGE=invoice-ingestion
PORT=8002

cd "$(dirname "$0")"

run_container() {
    docker run -d \
        --name "$SERVICE" \
        --restart unless-stopped \
        -p "${PORT}:${PORT}" \
        -v "$(pwd)/.env:/app/.env" \
        -v "$(pwd)/credentials.json:/app/credentials.json" \
        -v "$(pwd)/token.json:/app/token.json" \
        "$1" > /dev/null
}

echo "==> Checking disk"
AVAIL_MB=$(df --output=avail -BM / | tail -1 | tr -dc '0-9')
echo "    ${AVAIL_MB}MB free"
if [ "$AVAIL_MB" -lt 1500 ]; then
    echo "!!  Too little space - the build will fail part-way through."
    echo "    Free some first:  docker builder prune -f"
    exit 1
fi

echo "==> Pulling"
git pull

echo "==> Tagging the current image as :prev for rollback"
if docker image inspect "$IMAGE" > /dev/null 2>&1; then
    docker tag "$IMAGE" "${IMAGE}:prev"
fi

echo "==> Building"
docker build -t "$IMAGE" .

echo "==> Replacing the container"
docker rm -f "$SERVICE" > /dev/null 2>&1 || true
run_container "$IMAGE"

echo "==> Waiting for it to answer"
OK=0
for _ in $(seq 1 15); do
    if curl -fsS "localhost:${PORT}/health" > /dev/null 2>&1; then OK=1; break; fi
    sleep 1
done

if [ "$OK" -ne 1 ]; then
    echo "!!  It never came up. Rolling back to :prev."
    docker logs --tail 30 "$SERVICE" || true
    docker rm -f "$SERVICE" > /dev/null 2>&1 || true
    if docker image inspect "${IMAGE}:prev" > /dev/null 2>&1; then
        run_container "${IMAGE}:prev"
        echo "    Rolled back. The old version is running again."
    else
        echo "    No :prev image to roll back to - the service is DOWN."
    fi
    exit 1
fi

echo "==> Confirming it is polling"
sleep 3
docker logs --tail 5 "$SERVICE" 2>&1 | grep -i "poll" || echo "    (no poll line yet - check again in a minute)"

echo
echo "Deployed."

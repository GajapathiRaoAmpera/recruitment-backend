#!/usr/bin/env bash
# Runs on the VM (piped over SSH by .github/workflows/cd.yml, or by hand to roll back):
#   IMAGE=ghcr.io/<owner>/recruitment-backend:<sha> bash deploy/deploy.sh
# Expects ~/recruitment-backend/.env and Judge0 already up (make judge0-up) on network judge0_default.
set -euo pipefail
: "${IMAGE:?set IMAGE}"
cd ~/recruitment-backend
NET=judge0_default

docker pull "$IMAGE"
# migrate first: if it fails the old container keeps serving
docker run --rm --env-file .env --network "$NET" "$IMAGE" alembic upgrade head
docker rm -f api 2>/dev/null || true
docker run -d --restart unless-stopped --name api --env-file .env --network "$NET" \
  -p 127.0.0.1:8000:8000 -v /srv/resumes:/app/storage/resumes "$IMAGE"

for _ in $(seq 20); do
  curl -fsS localhost:8000/healthz >/dev/null && { echo "deployed $IMAGE"; exit 0; }
  sleep 2
done
docker logs --tail 50 api
echo "api did not become healthy" >&2
exit 1

#!/usr/bin/env bash
# Outer driver (Windows host, Git Bash): one disposable DinD host per proof.
set -uo pipefail
export MSYS_NO_PATHCONV=1
D="$(cd "$(dirname "$0")" && pwd -W)"; SHA="$1"; OUT="$D/evidence"; mkdir -p "$OUT"
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$OUT/driver.log"; }
log "master under test: $SHA"
docker build -q -t g2proof-host:1 -f "$D/Dockerfile.host" "$D" >> "$OUT/driver.log" 2>&1 || { log "host image build FAILED"; exit 1; }
for mode in ${MODES:-upgrade fresh}; do
  name="g2proof-$mode"
  log "starting disposable host $name"
  docker run -d --privileged --cgroupns private --name "$name" -v "$name-docker:/var/lib/docker" g2proof-host:1 >> "$OUT/driver.log" 2>&1 || { log "run FAILED"; exit 1; }
  for i in $(seq 1 60); do docker exec "$name" docker info >/dev/null 2>&1 && break; sleep 2; done
  docker exec "$name" docker info --format 'inner dockerd {{.ServerVersion}} storage {{.Driver}}' | tee -a "$OUT/driver.log"
  docker exec -e PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "$name" /opt/proof/proof.sh "$mode" "$SHA" > "$OUT/$mode-console.log" 2>&1
  ec=$?; log "$mode proof exit $ec"
  MSYS_NO_PATHCONV=1 docker cp "$name:/proof/ledger.jsonl" "$OUT/$mode-ledger.jsonl" 2>>"$OUT/driver.log"
  MSYS_NO_PATHCONV=1 docker cp "$name:/proof/logs" "$OUT/$mode-logs" 2>>"$OUT/driver.log"
  MSYS_NO_PATHCONV=1 docker cp "$name:/proof/printed-rollback.sh" "$OUT/$mode-printed-rollback.sh" 2>/dev/null
  docker exec "$name" sh -c 'docker ps -a --format "{{.Names}} {{.Image}} {{.Status}}"; docker images --format "{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}"; docker volume ls' > "$OUT/$mode-inner-state.txt" 2>&1
  log "removing disposable host $name and its own volume"
  docker rm -fv "$name" >> "$OUT/driver.log" 2>&1
  docker volume rm "$name-docker" >> "$OUT/driver.log" 2>&1
done
log "done"

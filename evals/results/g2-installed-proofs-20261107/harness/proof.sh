#!/usr/bin/env bash
# G2 installed-execution proofs on a disposable Docker host.
#   proof.sh upgrade <master-sha>   proofs 2 and 3: v0.22.1 install -> Rust -> backup/restore -> rollback
#   proof.sh fresh   <master-sha>   proof 1: fresh master install -> Rust -> restart
# Every step appends {ts, proof, step, cmd, exit, log} to /proof/ledger.jsonl.
set -uo pipefail
MODE="$1"; HEAD_SHA="$2"
P=/proof; R=$P/repo-$MODE; mkdir -p $P/logs
LEDGER=$P/ledger.jsonl
URL=http://127.0.0.1:8765
export HF_HUB_OFFLINE=0 PSEUDOLIFE_UNATTENDED=1
step() {  # step <proof> <name> <cmd...>
  local proof="$1" name="$2"; shift 2
  local log="$P/logs/$MODE-$name.log" start; start=$(date -u +%FT%TZ)
  echo "==> [$proof] $name: $*" | tee -a $P/console.log
  ( "$@" ) > "$log" 2>&1; local ec=$?
  python3 - "$LEDGER" "$start" "$proof" "$name" "$ec" "$log" "$*" <<'PY'
import json, sys, datetime
f, start, proof, name, ec, log, cmd = sys.argv[1:]
open(f, "a").write(json.dumps({"start": start, "end": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "proof": proof, "step": name, "exit": int(ec), "cmd": cmd, "log": log}) + "\n")
PY
  echo "    exit $ec ($log)" | tee -a $P/console.log
  return $ec
}
diagnose() {  # on a failed step: what the daemon itself said
  { date -u +%FT%TZ; docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}'
    echo "--- health"; curl -sS --max-time 5 $URL/health; echo
    echo "--- daemon logs (last 300)"; docker logs --tail 300 pseudolife-mcp-daemon 2>&1
    echo "--- daemon inspect"; docker inspect -f '{{json .State}} {{json .Config.Labels}}' pseudolife-mcp-daemon
  } > "$P/logs/$MODE-diagnose-$1.log" 2>&1
}
must() { step "$@" || { echo "FAILED: $2" | tee -a $P/console.log; diagnose "$2"; exit 1; }; }
health() { curl -fsS $URL/health; }
psqlq() { docker exec pseudolife-mcp-postgres psql -U pseudolife -d pseudolife_memory -At -c "$1"; }
snapshot() {  # a content fingerprint of the bank tables the proofs write
  psqlq "SELECT 'entries', count(*), md5(coalesce(string_agg(id::text||':'||md5(text), ',' ORDER BY id), '')) FROM entries
         UNION ALL SELECT 'facts', count(*), md5(coalesce(string_agg(entity||'|'||attribute||'|'||value||'|'||status, ',' ORDER BY id), '')) FROM facts
         UNION ALL SELECT 'schema', 0, (SELECT value::text FROM meta WHERE key='schema_version')"
}
mcp() { python3 /opt/proof/mcp_call.py $P/claude-mcp-add.args "$@"; }
engine() { docker inspect -f '{{index .Config.Labels "org.pseudolife.engine"}} {{.Config.Image}} {{.Image}}' pseudolife-mcp-daemon; }
activate_rust() {  # activate_rust <proof> <step>: as a user would, then the documented longer wait
  local proof="$1" name="$2"
  if step "$proof" "$name-default" python3 ops/update.py --engine rust; then
    cp "$P/logs/$MODE-$name-default.log" "$P/logs/$MODE-$name.log"; return 0
  fi
  diagnose "$name-default"
  python3 /opt/proof/printed_rollback.py "$P/logs/$MODE-$name-default.log" > $P/failed-activation-rollback.sh
  must "$proof" "$name-show-failed-rollback" cat $P/failed-activation-rollback.sh
  must "$proof" "$name-run-failed-rollback" bash -e $P/failed-activation-rollback.sh
  for i in $(seq 1 60); do health >/dev/null 2>&1 && break; sleep 5; done
  must "$proof" "$name-health-after-failed-rollback" health
  must "$proof" "$name-engine-after-failed-rollback" engine
  must "$proof" "$name" python3 ops/update.py --engine rust --health-retries 1300
}
stub_extractor() { pgrep -f "http.server 9000" >/dev/null || (cd /tmp && nohup python3 -m http.server 9000 >/dev/null 2>&1 &); sleep 1; }
INSTALL=(ops/install.sh --yes --no-art --extractor endpoint --extractor-url http://127.0.0.1:9000/v1
         --model proof-model --client claude --claude-plugin skip --instructions skip)

stub_extractor
if [ "$MODE" = upgrade ]; then
  must P2 clone git clone -q https://github.com/Pseudogiant-xr/Pseudolife-MCP "$R"
  cd "$R"
  must P2 checkout-v0.22.1 git checkout -q v0.22.1
  must P2 install-python-0.22.1 "${INSTALL[@]}"
  must P2 health-python health
  must P2 engine-python engine
  must P2 seed-via-registered-shim mcp memory_store '{"text":"G2 proof note one, written by Python 0.22.1","source":"g2-proof"}' \
       memory_store '{"text":"G2 proof note two: the bank must survive the engine switch","source":"g2-proof"}' \
       memory_fact_set '{"entity":"g2-proof","attribute":"written-by","value":"python-0.22.1"}'
  must P2 snapshot-before snapshot
  must P2 checkout-master git checkout -q "$HEAD_SHA"
  activate_rust P2 update-to-rust
  must P2 health-rust health
  must P2 engine-rust engine
  must P2 rollback-tags docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' --filter reference='pseudolife-daemon*'
  must P2 snapshot-after snapshot
  must P2 read-via-registered-shim mcp memory_search '{"query":"G2 proof note bank survive engine switch","top_k":5}' \
       memory_fact_get '{"entity":"g2-proof","attribute":"written-by"}'
  # Proof 3: backup and restore under Rust, then roll back to Python.
  must P3 write-under-rust mcp memory_store '{"text":"G2 proof note three, written by the Rust daemon","source":"g2-proof"}' \
       memory_fact_set '{"entity":"g2-proof","attribute":"written-by","value":"rust"}'
  must P3 snapshot-rust snapshot
  must P3 backup-under-rust pseudolife-mcp backup
  must P3 write-after-backup mcp memory_store '{"text":"G2 proof marker written AFTER the backup; restore must remove it","source":"g2-proof"}'
  must P3 list-backups ls -l /root/.pseudolife-mcp/backups
  dump=$(ls -t /root/.pseudolife-mcp/backups/pseudolife_memory-*.sql.gz | head -1)
  state=$(ls -t /root/.pseudolife-mcp/backups/pseudolife_state-*.tgz 2>/dev/null | head -1)
  must P3 restore-under-rust pseudolife-mcp restore --backup-file "$dump" ${state:+--state-archive "$state"} --yes
  must P3 health-after-restore health
  must P3 engine-after-restore engine
  must P3 snapshot-after-restore snapshot
  must P3 read-after-restore mcp memory_search '{"query":"G2 proof marker written AFTER the backup","top_k":5}' \
       memory_fact_get '{"entity":"g2-proof","attribute":"written-by"}'
  # The rollback the Rust activation printed, run as printed: back to the
  # tagged Python 0.22.1 image, which must read what Rust wrote.
  python3 /opt/proof/printed_rollback.py "$P/logs/upgrade-update-to-rust.log" > $P/printed-rollback.sh
  must P3 show-printed-rollback cat $P/printed-rollback.sh
  must P3 printed-rollback-to-python-0.22.1 bash -e $P/printed-rollback.sh
  for i in $(seq 1 120); do health >/dev/null 2>&1 && break; sleep 5; done
  must P3 health-python-0.22.1-after-rollback health
  must P3 engine-after-printed-rollback engine
  must P3 snapshot-after-printed-rollback snapshot
  must P3 python-0.22.1-reads-rust-writes mcp memory_search '{"query":"G2 proof note three written by the Rust daemon","top_k":5}' \
       memory_fact_get '{"entity":"g2-proof","attribute":"written-by"}'
  must P3 python-0.22.1-writes mcp memory_store '{"text":"G2 proof note four, written by Python 0.22.1 after the rollback","source":"g2-proof"}'
  # And the checkout's own engine switch back to Python at master.
  must P3 engine-python-from-master python3 ops/update.py --engine python
  must P3 health-python-master health
  must P3 engine-python-master engine
  must P3 snapshot-python-master snapshot
else
  must P1 clone git clone -q https://github.com/Pseudogiant-xr/Pseudolife-MCP "$R"
  cd "$R"
  must P1 checkout-master git checkout -q "$HEAD_SHA"
  must P1 install-checkout "${INSTALL[@]}"
  must P1 health-after-install health
  activate_rust P1 select-rust
  must P1 health-rust health
  must P1 engine-rust engine
  must P1 store-via-registered-shim mcp memory_store '{"text":"G2 fresh-install note served by Rust","source":"g2-proof"}' \
       memory_fact_set '{"entity":"g2-fresh","attribute":"engine","value":"rust"}'
  must P1 restart-daemon docker restart pseudolife-mcp-daemon
  for i in $(seq 1 120); do health >/dev/null 2>&1 && break; sleep 5; done
  must P1 health-after-restart health
  must P1 engine-after-restart engine
  must P1 read-after-restart mcp memory_search '{"query":"G2 fresh-install note served by Rust","top_k":5}' \
       memory_fact_get '{"entity":"g2-fresh","attribute":"engine"}'
  must P1 rerun-select-rust python3 ops/update.py --engine rust
fi
echo "ALL STEPS PASSED ($MODE)" | tee -a $P/console.log

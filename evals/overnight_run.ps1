# Overnight eval orchestrator (dev-only, not committed).
# Runs pytest + ladder_sweep + relations_bench (+ lesson bench) across the three
# extractors: Gemma E2B (bench container), Gemma E4B (swapped GGUF), Qwen-27B (4090).
# Each step is best-effort: a failure is logged and the run continues.
$ErrorActionPreference = "Continue"
$repo = "C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP"
Set-Location $repo
$py  = ".\.venv\Scripts\python.exe"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path $repo "data\overnight-$stamp"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$env:HF_HUB_OFFLINE="1"; $env:TRANSFORMERS_OFFLINE="1"; $env:TORCHDYNAMO_DISABLE="1"; $env:PYTHONPATH="."
$E4B = "C:/Users/HAMO9/ClaudeCode/PseudoLife-MCP/evals/models/gemma-4-E4B-it-Q4_K_M.gguf"
$logf = Join-Path $out "run.log"

function Log($m){ $line = "$((Get-Date).ToString('HH:mm:ss')) $m"; $line; $line | Out-File -FilePath $logf -Append -Encoding utf8 }
function Wait-Health($url,$tries=40){ for($i=0;$i -lt $tries;$i++){ try{ if((Invoke-WebRequest $url -TimeoutSec 5 -UseBasicParsing).StatusCode -eq 200){return $true} }catch{}; Start-Sleep 4 }; return $false }
function Start-GemmaBench($e4b){
  docker rm -f pseudolife-mcp-extractor-bench 2>$null | Out-Null
  if($e4b){ docker run -d --name pseudolife-mcp-extractor-bench -p 127.0.0.1:8081:8081 -v "$($E4B):/models/extractor.gguf:ro" pseudolife-extractor:gemma4-e2b 2>&1 | Out-Null }
  else    { docker run -d --name pseudolife-mcp-extractor-bench -p 127.0.0.1:8081:8081 pseudolife-extractor:gemma4-e2b 2>&1 | Out-Null }
  return (Wait-Health "http://127.0.0.1:8081/health" 45)
}

Log "=== OVERNIGHT RUN start -> $out ==="

# Phase 0: pytest regression
Log "[pytest] running full suite..."
& $py -m pytest -q *> (Join-Path $out "pytest.txt")
Log "[pytest] done (see pytest.txt)"

# server reachability snapshot
$qwen = Wait-Health "http://127.0.0.1:1234/v1/models" 3
Log "qwen-27b reachable at start: $qwen"

# Phase 1: in-process baselines (no LLM)
foreach($r in "naive-rag","floor"){ Log "[ladder] $r..."; & $py evals/ladder_sweep.py --rung $r *> (Join-Path $out "ladder-$r.txt") }

# Phase 2: Gemma E2B (shipped sidecar GGUF)
if(Start-GemmaBench $false){
  Log "[gemma-e2b] container healthy"
  & $py evals/ladder_sweep.py --rung gemma-e2b   *> (Join-Path $out "ladder-gemma-e2b.txt"); Log "[ladder] gemma-e2b done"
  & $py evals/relations_bench.py --rungs gemma-e2b *> (Join-Path $out "relations-gemma-e2b.txt"); Log "[relations] gemma-e2b done"
} else { Log "[gemma-e2b] FAILED to start - skipped" }

# Phase 3: Gemma E4B (swapped GGUF, same :8081)
if(Start-GemmaBench $true){
  Log "[gemma-e4b] container healthy"
  & $py evals/ladder_sweep.py --rung gemma-e4b   *> (Join-Path $out "ladder-gemma-e4b.txt"); Log "[ladder] gemma-e4b done"
  & $py evals/relations_bench.py --rungs gemma-e4b *> (Join-Path $out "relations-gemma-e4b.txt"); Log "[relations] gemma-e4b done"
} else { Log "[gemma-e4b] FAILED to start - skipped" }

# Phase 4: Qwen-27B (4090)
if($qwen){
  Log "[qwen-27b] running rungs..."
  & $py evals/ladder_sweep.py --rung qwen-27b    *> (Join-Path $out "ladder-qwen-27b.txt"); Log "[ladder] qwen-27b done"
  & $py evals/relations_bench.py --rungs qwen-27b  *> (Join-Path $out "relations-qwen-27b.txt"); Log "[relations] qwen-27b done"
} else { Log "[qwen-27b] UNREACHABLE - skipped" }

# Phase 5: lesson-synthesis bench (optional; inside daemon container, E2B sidecar + qwen)
if(Test-Path "evals/lesson_synthesis_bench.py"){
  Log "[lesson] running --target all in daemon container..."
  docker cp evals/lesson_synthesis_bench.py pseudolife-mcp-daemon:/tmp/lb.py 2>&1 | Out-Null
  docker exec pseudolife-mcp-daemon python /tmp/lb.py --target all *> (Join-Path $out "lesson-synthesis.txt"); Log "[lesson] done"
} else { Log "[lesson] script absent - skipped" }

# Phase 6: aggregate
Log "[report] ladder --report..."
& $py evals/ladder_sweep.py --report *> (Join-Path $out "ladder-REPORT.txt")

# cleanup the bench container (leave the live sidecar + qwen running)
docker rm -f pseudolife-mcp-extractor-bench 2>$null | Out-Null
Log "[cleanup] removed bench container (live sidecar untouched; qwen left running for morning)"
Log "=== OVERNIGHT RUN done -> $out ==="
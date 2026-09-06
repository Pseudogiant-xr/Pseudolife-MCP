# Incremental daemon builds

Use `ops/update.ps1` for deployments. It preserves the existing backup,
rollback-image, daemon-only recreation and health-check workflow. No database
or schema migration is introduced by the build-cache changes.

## What is cached

The daemon Dockerfile installs CPU-only torch, then the declared runtime and
ONNX dependencies constrained by `ops/requirements.lock.txt`, then bakes the
embedding models. Application source is copied only after those expensive
layers. Editing CSS, JavaScript or Python therefore rebuilds the application
package without reinstalling its dependencies or downloading models.

`pyproject.toml` and the lockfile precede dependency installation. Changing
either intentionally invalidates that layer: a newly declared requirement
must not silently disappear from the image. The final `pip check` fails the
build for missing or incompatible installed requirements.

BuildKit cache mounts retain pip downloads and Hugging Face model downloads
between builds, including failed builds. They are locked while in use to
avoid concurrent copying/installing against changing cache contents. Only
the two supported model directories are copied into the offline runtime
image; the download caches themselves are not runtime volumes.

## Expected costs and limits

- First build still downloads dependencies and models and may be slow.
- Source-only builds reuse those layers; packaging, image export, backup and
  health checks still take time. This is not hot reload.
- Dependency/base-image changes still require installation and model loading,
  but retained download caches can avoid transferring existing files again.
- Caches are local to the selected builder. A new builder, cache pruning or
  Docker data reset can remove them. Do not treat the cache as a backup.
- Build-cache pruning is the **default**, not an opt-in: every healthy
  `ops/update.ps1` / `ops/update.sh` deploy ends by running
  `ops/prune-build-cache.ps1` (age 168 h, 20 GB ceiling, least-recently-used
  first — see [docker-disk-retention.md](docker-disk-retention.md)). The
  `pseudolife-hf` mount (~1.4 GB) and the pip mount live inside that same
  budget, so a deploy can evict them and the next build re-downloads. Pass
  `-NoCachePrune` (PowerShell) / `--no-cache-prune` (bash) to keep them for a
  run. Never prune database volumes to speed a build.

## Verification

Build a candidate image under a separate tag without deploying it. Run from
the repository root — the build context is the root (see the Dockerfile
header), and the commands are the same in PowerShell and bash:

```bash
docker build -f ops/Dockerfile.daemon -t pseudolife-daemon:cache-check --progress plain .
docker run --rm --network none --entrypoint pip pseudolife-daemon:cache-check check
```

Repeat after a small source edit and confirm the torch, dependency and model
steps report `CACHED`. Compare elapsed time on the same builder; do not compare
a first download against a warm build as if the cache eliminated cold cost.
Check actual offline model loading through the daemon's own
`EmbeddingPipeline`, which resolves the baked snapshot path and reports the
backend it really loaded (it falls soft to torch when ONNX cannot load, so
assert on `.backend`):

```bash
docker run --rm --network none --entrypoint python pseudolife-daemon:cache-check -c "
from pseudolife_memory.utils.config import EmbeddingConfig
from pseudolife_memory.memory.embedding import EmbeddingPipeline
p = EmbeddingPipeline(EmbeddingConfig(model_name='all-MiniLM-L6-v2', backend='onnx'))
assert p.backend == 'onnx', p.backend
print('minilm', p.backend, tuple(p.encode_single('offline check').shape))
q = EmbeddingPipeline(EmbeddingConfig())
print('default', q.backend, tuple(q.encode_single('offline check').shape))
"
```

The bare `SentenceTransformer('all-MiniLM-L6-v2', backend='onnx', ...)` repo-id
form is *not* a valid check: under `HF_HUB_OFFLINE=1` it raises
`OfflineModeIsEnabled` even on a correct image, because only the pipeline
resolves the local snapshot. `/health` alone does not prove model or
document-ingestion readiness.

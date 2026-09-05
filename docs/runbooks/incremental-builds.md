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
- Existing `ops/update.ps1 -NoCachePrune` can retain build cache when desired;
  monitor Docker disk usage. Never prune database volumes to speed a build.

## Verification

Build a candidate image under a separate tag without deploying it:

```powershell
docker build -f ops/Dockerfile.daemon -t pseudolife-daemon:cache-check --progress plain .
docker run --rm --network none --entrypoint pip pseudolife-daemon:cache-check check
```

Repeat after a small source edit and confirm the torch, dependency and model
steps report `CACHED`. Compare elapsed time on the same builder; do not compare
a first download against a warm build as if the cache eliminated cold cost.
Check actual offline model loading, including MiniLM's ONNX backend, before
deploying. `/health` alone does not prove model or document-ingestion readiness.

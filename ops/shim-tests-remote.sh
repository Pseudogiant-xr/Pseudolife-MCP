#!/usr/bin/env bash
# Far half of integrate.py shim-tests. Arguments and bundle arrive over SSH;
# this controller is read before building a batch, never imported from it.
set -u
repo=$1 base=$2 head=$3 ref=$4 nonce=$5 wait_seconds=$6
export PATH="$PATH:$HOME/.cargo/bin" GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=Never
export CARGO_TERM_COLOR=never NEXTEST_COLOR=never
failure='the run did not finish'
verdict=''
bundle=''
private=''
locked=0
holder=''
finish() {
    trap - EXIT
    if [[ -n "$private" ]]; then git -c core.hooksPath="$no_hooks" -C "$repo" update-ref -d "$private" >/dev/null 2>&1 || true; fi
    if [[ -n "$bundle" ]]; then rm -f -- "$bundle"; fi
    if (( locked )) && [[ -n "$holder" ]]; then rm -f -- "$holder"; fi
    if [[ -n "$verdict" ]]; then
        printf 'shim-tests %s: nextest exit %s\n' "$nonce" "$verdict" >&2
        exit "$verdict"
    fi
    printf 'shim-tests %s: not run: %s\n' "$nonce" "$failure" >&2
    exit 3
}
trap finish EXIT
trap 'failure="interrupted; check the owned run before retrying"; exit 3' HUP INT TERM
refuse() { failure=$1; exit 3; }
[[ "$head" =~ ^[0-9a-f]{40}$ && "$base" =~ ^[0-9a-f]{40}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] || refuse 'invalid head, base or nonce'
[[ "$ref" =~ ^refs/heads/integrate/[0-9A-Za-z._-]+$ ]] || refuse 'the bundle ref must be an integrate/* branch'
[[ "$wait_seconds" =~ ^[0-9]+$ ]] || refuse 'invalid lock wait'
if [[ "$repo" == '~/'* ]]; then repo="$HOME/${repo:2}"; fi
[[ "$repo" == /* && -d "$repo/.git" ]] || refuse 'repo= must name an existing absolute clone path'
repo=$(realpath -e "$repo") || refuse 'cannot resolve clone path'
no_hooks="$repo.shim-tests-no-hooks"
[[ ! -e "$no_hooks" && ! -L "$no_hooks" ]] || refuse 'the no-hooks path exists; remove it before retrying'
for tool in git flock cargo python3 base64; do command -v "$tool" >/dev/null || refuse "$tool is missing; install it on the second Linux dev box"; done
cargo nextest --version >/dev/null 2>&1 || refuse 'cargo-nextest is missing; install it on the second Linux dev box'
locks="${PSEUDOLIFE_SUITE_LOCK_DIR:-$HOME/.pseudolife-mcp/locks}"
mkdir -p "$locks" || refuse 'cannot open machine lock folder'
exec 9>>"$locks/full-suite.lock" || refuse 'cannot open full-suite.lock'
deadline=$((SECONDS + wait_seconds))
if ! flock -n 9; then
    owner=$(cat "$locks/full-suite.holder.json" 2>/dev/null || printf 'another full-suite run')
    printf 'shim-tests: waiting for full-suite lock: %s (limit %s s)\n' "$owner" "$wait_seconds" >&2
    flock -w "$wait_seconds" 9 || refuse 'full-suite lock wait exhausted; retry after the holder releases it'
fi
locked=1
holder="$locks/full-suite.holder.json"
python3 -c 'import json,os,sys,time; json.dump({"pid":os.getppid(),"holder":"batch shim tests", "commit":sys.argv[2],"started":time.time()},open(sys.argv[1],"w"))' "$holder" "$head" || refuse 'cannot record full-suite holder'
exec 8>>"$repo.shim-tests.lock" || refuse 'cannot open clone lock'
remaining=$((deadline - SECONDS)); (( remaining >= 0 )) || remaining=0
flock -w "$remaining" 8 || refuse 'clone lock wait exhausted; retry after its holder releases it'
gate="$repo.shim-tests"
target="$repo.shim-tests-target"
parent=$(dirname "$repo")
for location in "$parent" "$target"; do
    [[ -e "$location" ]] || continue
    free=$(df --output=avail -B1 "$location" | tail -1 | tr -d ' ')
    [[ "$free" =~ ^[0-9]+$ ]] || refuse 'cannot determine free disk on the work/cache filesystem'
    (( free >= 8589934592 )) || refuse 'under 8 GB free on the work/cache filesystem; free owned caches before retrying'
done
[[ ! -L "$gate" && ! -L "$target" ]] || refuse 'worktree or target is a symlink; inspect before retrying'
g() { git -c core.hooksPath="$no_hooks" -C "$repo" "$@"; }
g fetch --quiet origin master || refuse 'clone cannot fetch origin; restore access and retry'
g cat-file -e "$base^{commit}" 2>/dev/null || g fetch --quiet origin "$base" || refuse 'batch base is unavailable'
bundle=$(mktemp "$parent/shim-tests-XXXXXXXX.bundle") || refuse 'cannot create bundle file'
base64 --decode > "$bundle" || refuse 'bundle on stdin is not base64'
g bundle verify --quiet "$bundle" || refuse 'bundle does not apply to the clone'
private="refs/shim-tests/$nonce"
g fetch --quiet "$bundle" "+$ref:$private" || refuse 'bundle ref is missing'
[[ "$(g rev-parse "$private^{commit}")" == "$head" ]] || refuse 'bundle does not name the batch head'
if [[ -e "$gate" ]]; then
    [[ -f "$gate/.git" ]] || refuse 'interrupted worktree has no .git; inspect it before retrying'
    git -c core.hooksPath="$no_hooks" -C "$gate" checkout --quiet --detach --force "$head" || refuse 'cannot set gate worktree to head'
    git -c core.hooksPath="$no_hooks" -C "$gate" clean -ffdxq || refuse 'cannot clean gate worktree'
else
    g worktree add --quiet --detach "$gate" "$head" || refuse 'cannot create gate worktree'
fi
[[ "$(git -C "$gate" rev-parse HEAD)" == "$head" ]] || refuse 'gate worktree is not the batch head'
cd "$gate/rust" || refuse 'the batch has no rust workspace'
export CARGO_TARGET_DIR="$target"
# This target belongs only to this fixed worktree. It is never shared with
# workers or another checkout, so cargo's path-based freshness stays valid.
printf 'shim-tests: holding full-suite lock; testing %s on the second Linux dev box\n' "$head"
cargo nextest run --locked -p pseudolife-stdio --lib --bins -j 4 --no-fail-fast 8>&- 9>&-
verdict=$?
exit "$verdict"

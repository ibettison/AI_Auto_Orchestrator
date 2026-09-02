#!/usr/bin/env bash

set -Eeuo pipefail
umask 027

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV_DIR=${AI_ORCHESTRATOR_VENV:-/opt/ai-orchestrator/venv}
WORKTREE_DIR=""
STAGING_ROOT=""
VENV_BACKUP=""
VENV_DETACHED=0
SOURCE_ADVANCED=0

cleanup() {
    if ((VENV_DETACHED == 1 && SOURCE_ADVANCED == 0)); then
        # Restore the commissioned environment if candidate installation,
        # validation, or source advance failed.
        mv "$VENV_DIR" "${STAGING_ROOT}/failed-live-venv" >/dev/null 2>&1 || true
        mv "$VENV_BACKUP" "$VENV_DIR" >/dev/null 2>&1 || true
    fi
    if [[ -n "$WORKTREE_DIR" && -d "$WORKTREE_DIR" ]]; then
        git -C "$SOURCE_DIR" worktree remove --force "$WORKTREE_DIR" >/dev/null 2>&1 || true
    fi
    if [[ -n "$STAGING_ROOT" && -d "$STAGING_ROOT" ]]; then
        rm -rf "$STAGING_ROOT"
    fi
}
trap cleanup EXIT

fail() {
    printf '%s\n' "ai-orchestrator update failed closed" >&2
    exit 1
}

usage() {
    printf 'usage: %s (--revision COMMIT_SHA | --current-main)\n' "$0" >&2
    exit 2
}

[[ -d "$SOURCE_DIR/.git" ]] || fail
[[ -x "$VENV_DIR/bin/python" ]] || fail
[[ -z "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=all)" ]] || fail

target_ref=""
mode_count=0
while (($#)); do
    case "$1" in
        --revision)
            (($# >= 2)) || usage
            target_ref=$2
            mode_count=$((mode_count + 1))
            shift 2
            ;;
        --current-main)
            target_ref=origin/main
            mode_count=$((mode_count + 1))
            shift
            ;;
        *)
            usage
            ;;
    esac
done
((mode_count == 1)) || usage

if [[ "$target_ref" == origin/main ]]; then
    git -C "$SOURCE_DIR" fetch --prune origin main >/dev/null || fail
elif [[ ! "$target_ref" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
    fail
fi
target_sha=$(git -C "$SOURCE_DIR" rev-parse --verify "$target_ref^{commit}") || fail

WORKTREE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ai-orchestrator-update.XXXXXX")
git -C "$SOURCE_DIR" worktree add --detach "$WORKTREE_DIR" "$target_sha" >/dev/null || fail

STAGING_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/ai-orchestrator-venv.XXXXXX")
VENV_BACKUP="$STAGING_ROOT/live-venv"

# Hold the previous environment aside, then create the candidate at its final
# commissioned path. This ensures pip-generated console-script shebangs use
# the path that will remain after cleanup.
mv "$VENV_DIR" "$VENV_BACKUP" || fail
VENV_DETACHED=1
cp -a "$VENV_BACKUP" "$VENV_DIR" || fail

# Install and validate at the final runtime path. Never pass the runtime
# OpenAI credential to installation, tests, imports, or CLI checks.
env -u OPENAI_API_KEY "$VENV_DIR/bin/python" -m pip install --upgrade "$WORKTREE_DIR" >/dev/null || fail
(cd "$WORKTREE_DIR" && env -u OPENAI_API_KEY "$VENV_DIR/bin/python" -m unittest discover -v) || fail
(cd "$WORKTREE_DIR" && env -u OPENAI_API_KEY "$VENV_DIR/bin/python" -c 'import openai; import orchestrator.live_review; import orchestrator.prepare_live_review') || fail
(cd "$WORKTREE_DIR" && env -u OPENAI_API_KEY "$VENV_DIR/bin/orchestrator-live-review" --help) || fail
(cd "$WORKTREE_DIR" && env -u OPENAI_API_KEY "$VENV_DIR/bin/orchestrator-prepare-live-review" --help) || fail

git -C "$SOURCE_DIR" switch --detach "$target_sha" >/dev/null || fail
[[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$target_sha" ]] || fail
SOURCE_ADVANCED=1
printf 'ai-orchestrator updated to %s\n' "$target_sha"

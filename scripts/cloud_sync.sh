#!/usr/bin/env bash
#
# Runs the Spotify sync on a machine that starts empty every time, such as a
# cloud agent, and keeps what the tool remembers on a git branch in between.
#
#   cloud_sync.sh run [sync options]   pull the state, sync Spotify, push the state
#   cloud_sync.sh pull                 replace this machine's state with the branch's
#   cloud_sync.sh push                 store this machine's state on the branch
#
# The state is encrypted with SONG_AUTOMATIONS_STATE_KEY before it leaves the
# machine and holds no login. When the variable is not set, the key is read
# from ~/.song_automations/cloud.env.
#
# A run builds on the state it pulled. If another machine stores its state in
# the meantime, the run's push is refused instead of replacing that state.
#
# Other variables, all optional:
#   SONG_AUTOMATIONS_PYTHON        python that has song_automations installed
#   SONG_AUTOMATIONS_STATE_BRANCH  branch holding the state (sync-state)
#   SONG_AUTOMATIONS_STATE_REMOTE  remote holding that branch (origin)
#   SONG_AUTOMATIONS_STATE_REPO    checkout used for git (this script's)
#   SONG_AUTOMATIONS_CLOUD_ENV     file the key is read from

set -euo pipefail

BRANCH="${SONG_AUTOMATIONS_STATE_BRANCH:-sync-state}"
REMOTE="${SONG_AUTOMATIONS_STATE_REMOTE:-origin}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${SONG_AUTOMATIONS_STATE_REPO:-$HERE}"
KEY_FILE="${SONG_AUTOMATIONS_CLOUD_ENV:-$HOME/.song_automations/cloud.env}"
BUNDLE="state.tar.gz.enc"
CIPHER=(-aes-256-cbc -pbkdf2 -iter 200000 -md sha256)
PYTHON="${SONG_AUTOMATIONS_PYTHON:-}"
PULLED=""
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

die() {
    echo "cloud_sync: $*" >&2
    exit 1
}

load_key() {
    if [[ -z "${SONG_AUTOMATIONS_STATE_KEY:-}" && -f "$KEY_FILE" ]]; then
        SONG_AUTOMATIONS_STATE_KEY="$(sed -n 's/^SONG_AUTOMATIONS_STATE_KEY=//p' "$KEY_FILE" | head -n 1 | tr -d "\r\"'")"
    fi
    [[ -n "${SONG_AUTOMATIONS_STATE_KEY:-}" ]] || die "SONG_AUTOMATIONS_STATE_KEY is not set, so the state cannot be read or written."
    export SONG_AUTOMATIONS_STATE_KEY
}

ensure_python() {
    if [[ -n "$PYTHON" ]]; then
        return
    fi
    echo "cloud_sync: installing song_automations"
    if python3 -m venv "$WORK/venv" 2>/dev/null; then
        "$WORK/venv/bin/pip" install --quiet "$HERE"
    elif command -v uv >/dev/null; then
        rm -rf "$WORK/venv"
        uv venv --quiet "$WORK/venv"
        uv pip install --quiet --python "$WORK/venv/bin/python" "$HERE"
    else
        die "song_automations could not be installed: python3 has no venv module and uv is not here."
    fi
    PYTHON="$WORK/venv/bin/python"
}

tool() {
    "$PYTHON" -m song_automations "$@"
}

pull_state() {
    git -C "$REPO" fetch --quiet "$REMOTE" "$BRANCH" \
        || die "the branch '$BRANCH' is not on '$REMOTE'. Store the state from a machine that has it first: cloud_sync.sh push"
    PULLED="$(git -C "$REPO" rev-parse FETCH_HEAD)"
    git -C "$REPO" show "$PULLED:$BUNDLE" > "$WORK/in.enc"
    openssl enc -d "${CIPHER[@]}" -pass env:SONG_AUTOMATIONS_STATE_KEY -in "$WORK/in.enc" -out "$WORK/in.tar.gz" 2>/dev/null \
        || die "the state on '$BRANCH' could not be decrypted. SONG_AUTOMATIONS_STATE_KEY is not the key it was stored with."
    tool state import "$WORK/in.tar.gz"
}

push_state() {
    tool state export "$WORK/out.tar.gz"
    openssl enc "${CIPHER[@]}" -salt -pass env:SONG_AUTOMATIONS_STATE_KEY -in "$WORK/out.tar.gz" -out "$WORK/$BUNDLE"
    local blob tree commit
    local parent=()
    if [[ -n "$PULLED" ]]; then
        parent=(-p "$PULLED")
    elif git -C "$REPO" fetch --quiet "$REMOTE" "$BRANCH" 2>/dev/null; then
        parent=(-p "$(git -C "$REPO" rev-parse FETCH_HEAD)")
    fi
    blob="$(git -C "$REPO" hash-object -w "$WORK/$BUNDLE")"
    tree="$(printf '100644 blob %s\t%s\n' "$blob" "$BUNDLE" | git -C "$REPO" mktree)"
    commit="$(
        GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-$(git -C "$REPO" config user.name || echo song-automations)}" \
        GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-$(git -C "$REPO" config user.email || echo song-automations@users.noreply.github.com)}" \
        GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$(git -C "$REPO" config user.name || echo song-automations)}" \
        GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$(git -C "$REPO" config user.email || echo song-automations@users.noreply.github.com)}" \
        git -C "$REPO" commit-tree "$tree" ${parent[@]+"${parent[@]}"} -m "State after the sync of $(date -u +%Y-%m-%dT%H:%MZ)"
    )"
    git -C "$REPO" push --quiet "$REMOTE" "$commit:refs/heads/$BRANCH" \
        || die "the state could not be pushed to '$BRANCH'. Either pushing is not allowed, or another machine stored its state after this one pulled. This run's matches are not stored, so the next run repeats its searches."
    echo "cloud_sync: state stored on $REMOTE/$BRANCH"
}

run_sync() {
    pull_state
    local status=0
    tool --unattended sync spotify "$@" || status=$?
    push_state
    if [[ "$status" -ne 0 ]]; then
        echo "cloud_sync: the sync stopped early with status $status; what it did until then is stored." >&2
    fi
    return "$status"
}

command="${1:-}"
[[ $# -gt 0 ]] && shift
case "$command" in
    run)
        load_key
        ensure_python
        run_sync "$@"
        ;;
    pull)
        load_key
        ensure_python
        pull_state
        ;;
    push)
        load_key
        ensure_python
        push_state
        ;;
    *)
        die "usage: cloud_sync.sh run [sync options] | pull | push"
        ;;
esac

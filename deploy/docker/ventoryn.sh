#!/usr/bin/env bash
# Ventoryn in Docker — operations for the Fedora server.
#
#   deploy/docker/ventoryn.sh backup     consistent backup of data/ + reports/
#   deploy/docker/ventoryn.sh build      build the image (old app keeps running)
#   deploy/docker/ventoryn.sh cutover    backup, stop the host process, start
#                                        the container; auto-rollback if unhealthy
#   deploy/docker/ventoryn.sh rollback   container -> host process (old way)
#   deploy/docker/ventoryn.sh deploy     git pull, rebuild, replace container;
#                                        previous image restored if unhealthy
#   deploy/docker/ventoryn.sh status | logs
#
# State and secrets never go into the image: data/, reports/ and
# .env.ventoryn are mounted from the repo on the host. Host networking
# keeps the Cloudflare tunnel's target (http://localhost:8000) and its
# loopback peer (needed to trust CF-Connecting-IP) exactly as today.
set -euo pipefail

REPO="${VENTORYN_REPO:-$HOME/Projects/AI_Agents_Ecosystem}"
ENV_FILE="${VENTORYN_ENV_FILE:-$REPO/.env.ventoryn}"
NAME=ventoryn
IMAGE=ventoryn
PORT=8000
BACKUPS="${VENTORYN_BACKUPS:-$HOME/ventoryn-backups}"
HOST_LOG="$HOME/ventoryn-host.log"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

healthy() {  # wait up to $1 seconds for the app to answer on localhost
    local deadline=$((SECONDS + ${1:-90}))
    while (( SECONDS < deadline )); do
        if curl -fsS -o /dev/null --max-time 4 "http://127.0.0.1:$PORT/"; then
            return 0
        fi
        sleep 3
    done
    return 1
}

host_pids() {  # the app when it runs directly on the host (not in Docker)
    local pid
    for pid in $(pgrep -f "launch.py all" || true); do
        # Container processes live in another mount namespace; skip them.
        if [[ "$(readlink "/proc/$pid/ns/mnt" 2>/dev/null)" == \
              "$(readlink /proc/self/ns/mnt)" ]]; then
            echo "$pid"
        fi
    done
}

port_free() { ! ss -ltn "( sport = :$PORT )" | grep -q LISTEN; }

cmd_backup() {
    local dest="$BACKUPS/$(date +%Y%m%d-%H%M%S)"
    # Backups hold a copy of the secrets file: private to this user.
    mkdir -p "$dest" && chmod 700 "$BACKUPS" "$dest"
    say "Backing up to $dest"
    # Online, consistent SQLite copy (safe while the app is writing).
    python3 - "$REPO/data/ecosystem_state.db" "$dest/ecosystem_state.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close(); s.close()
PY
    cp -a "$REPO/data/"*.json "$dest/" 2>/dev/null || true
    tar -C "$REPO" -czf "$dest/reports.tgz" reports
    cp -a "$ENV_FILE" "$dest/env.ventoryn" && chmod 600 "$dest/env.ventoryn"
    ls -la "$dest"
}

cmd_build() {
    [[ -f "$REPO/Dockerfile" ]] || die "no Dockerfile in $REPO"
    local tag
    tag="$(git -C "$REPO" rev-parse --short HEAD)"
    say "Building $IMAGE:$tag (the running app is not touched)"
    docker build --build-arg APP_UID="$(id -u)" --build-arg APP_GID="$(id -g)" \
        -t "$IMAGE:$tag" -t "$IMAGE:latest" "$REPO"
}

run_container() {
    local image="$1"
    docker run -d --name "$NAME" --restart unless-stopped \
        --network host \
        --user "$(id -u):$(id -g)" \
        -v "$REPO/data:/app/data:z" \
        -v "$REPO/reports:/app/reports:z" \
        -v "$ENV_FILE:/run/ventoryn.env:ro,z" \
        -e ECOSYSTEM_ENV_FILE=/run/ventoryn.env \
        -e ECOSYSTEM_STATE_DB=/app/data/ecosystem_state.db \
        --log-opt max-size=20m --log-opt max-file=5 \
        "$image" >/dev/null
}

start_host_process() {
    say "Starting the app on the host (old way), bound to 127.0.0.1"
    (cd "$REPO" && nohup bash -c 'set -a; . "$0"; set +a; exec .venv/bin/python launch.py all --host 127.0.0.1 --port '"$PORT" \
        "$ENV_FILE" >>"$HOST_LOG" 2>&1 &)
}

cmd_rollback() {
    if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
        say "Stopping container $NAME"
        docker stop -t 30 "$NAME" >/dev/null || true
        docker rm "$NAME" >/dev/null || true
    fi
    if [[ -n "$(host_pids)" ]]; then
        say "Host process already running"; return 0
    fi
    start_host_process
    healthy 90 && say "Rolled back: host process is answering on :$PORT (log: $HOST_LOG)" \
        || die "host process did not come up — see $HOST_LOG"
}

cmd_cutover() {
    [[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE"
    docker image inspect "$IMAGE:latest" >/dev/null 2>&1 || die "build first: $0 build"
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
        die "container $NAME is already running"
    fi
    cmd_backup
    local pids; pids="$(host_pids)"
    if [[ -n "$pids" ]]; then
        say "Stopping the host process ($pids)"
        kill -INT $pids || true
        for _ in $(seq 1 20); do [[ -z "$(host_pids)" ]] && break; sleep 1; done
        [[ -z "$(host_pids)" ]] || { kill -TERM $(host_pids) || true; sleep 5; }
    fi
    for _ in $(seq 1 15); do port_free && break; sleep 1; done
    port_free || die "port $PORT still in use — nothing changed in Docker"
    say "Starting container $NAME"
    run_container "$IMAGE:latest"
    if healthy 120; then
        say "Cut-over done: the container answers on :$PORT"
        docker ps --filter "name=^$NAME$" --format '{{.Names}}  {{.Status}}'
    else
        printf '\033[31mContainer did not become healthy — rolling back\033[0m\n'
        docker logs --tail 60 "$NAME" || true
        cmd_rollback
        exit 1
    fi
}

cmd_deploy() {
    docker ps --format '{{.Names}}' | grep -qx "$NAME" || die "not running in Docker yet — use cutover"
    local previous
    previous="$(docker inspect --format '{{.Config.Image}}' "$NAME")"
    git -C "$REPO" pull --ff-only
    cmd_build
    cmd_backup
    say "Replacing container ($previous -> $IMAGE:latest)"
    docker stop -t 30 "$NAME" >/dev/null && docker rm "$NAME" >/dev/null
    run_container "$IMAGE:latest"
    if healthy 120; then
        say "Deployed $(git -C "$REPO" rev-parse --short HEAD)"
    else
        printf '\033[31mNew image unhealthy — restoring %s\033[0m\n' "$previous"
        docker logs --tail 60 "$NAME" || true
        docker stop "$NAME" >/dev/null || true; docker rm "$NAME" >/dev/null || true
        run_container "$previous"
        healthy 120 && say "Previous image restored" || die "previous image unhealthy too — run: $0 rollback"
        exit 1
    fi
}

cmd_status() {
    docker ps -a --filter "name=^$NAME$" --format 'container: {{.Names}}  {{.Image}}  {{.Status}}' || true
    local pids; pids="$(host_pids)"; [[ -n "$pids" ]] && echo "host process: $pids" || echo "host process: none"
    curl -fsS -o /dev/null -w "http://127.0.0.1:$PORT/ -> %{http_code}\n" --max-time 4 "http://127.0.0.1:$PORT/" || echo "not answering on :$PORT"
}

case "${1:-}" in
    backup) cmd_backup ;;
    build) cmd_build ;;
    cutover) cmd_cutover ;;
    rollback) cmd_rollback ;;
    deploy) cmd_deploy ;;
    status) cmd_status ;;
    logs) docker logs -f --tail 200 "$NAME" ;;
    *) sed -n '2,13p' "$0"; exit 2 ;;
esac

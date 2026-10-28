#!/usr/bin/env bash
# Web app + queue workers in one container. If either process exits, stop the other and exit, so Docker's
# restart policy brings both back (jobs left "running" are requeued by the workers' recovery task).
set -uo pipefail
# fewer malloc arenas: PDF work runs in a new thread pool per job and glibc kept the freed memory per arena
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"

huey_consumer app.worker.huey --worker-type process --workers "${WORKERS:-2}" \
    --max-delay 1 --max-tasks "${WORKER_MAX_TASKS:-10}" --graceful-signal TERM --shutdown-timeout 5 --simple &
worker=$!

uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips "*" &
web=$!

trap 'kill -TERM "$worker" "$web" 2>/dev/null' TERM INT
wait -n "$worker" "$web"
code=$?
kill -TERM "$worker" "$web" 2>/dev/null
wait
exit "$code"

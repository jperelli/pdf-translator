"""Prometheus text exposition for /metrics, computed from SQLite and the Huey queue at scrape time.

Workers are separate processes, so nothing is kept in memory: gauges are read from the database and the
queue, counters and histograms come from the persisted `counters` table (they survive restarts and the TTL
cleanup of old jobs, so `rate()` and `increase()` behave).
"""

from __future__ import annotations

import math
import time
from collections import defaultdict

from app.store import DURATION_BUCKETS, WAIT_BUCKETS, Store

PREFIX = "translator_"
HISTOGRAMS = {"job_duration_seconds": DURATION_BUCKETS, "queue_wait_seconds": WAIT_BUCKETS}
HELP = {
    "jobs_created_total": "Jobs enqueued (translations and redos).",
    "jobs_finished_total": "Jobs finished, by status and purpose.",
    "jobs_retried_total": "Jobs started again (lost worker or admin retry).",
    "jobs_canceled_total": "Jobs canceled by an administrator.",
    "pages_translated_total": "Pages of finished translations.",
    "llm_input_tokens_total": "LLM input tokens, by model and stage.",
    "llm_output_tokens_total": "LLM output tokens, by model and stage.",
    "llm_cached_tokens_total": "LLM cached input tokens, by model and stage.",
    "llm_requests_total": "LLM requests, by model and stage.",
    "llm_cost_usd_total": "Estimated LLM cost at list prices (USD), by model and stage.",
    "job_duration_seconds": "Time from a worker starting a job to its end.",
    "queue_wait_seconds": "Time a job waited in the queue before a worker took it.",
}


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(v: float) -> str:
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return f"{v:.10g}"


def _line(name: str, labels: dict[str, str], value: float) -> str:
    lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
    return f"{PREFIX}{name}{{{lab}}} {_num(value)}" if lab else f"{PREFIX}{name} {_num(value)}"


def render(db: Store, huey) -> str:
    now = time.time()
    out: list[str] = []

    def gauge(name: str, help_: str, samples: list[tuple[dict[str, str], float]]) -> None:
        out.extend([f"# HELP {PREFIX}{name} {help_}", f"# TYPE {PREFIX}{name} gauge"])
        out.extend(_line(name, lab, v) for lab, v in samples)

    gauge("up", "The web process answered this scrape.", [({}, 1)])
    try:
        pending, scheduled = huey.pending_count(), huey.scheduled_count()
    except Exception:
        pending = scheduled = float("nan")
    gauge("queue_pending", "Tasks waiting in the Huey queue.", [({}, pending)])
    gauge("queue_scheduled", "Tasks scheduled for later in Huey.", [({}, scheduled)])
    counts = db.status_counts()
    gauge("jobs", "Jobs currently stored, by status (old ones are deleted after JOB_TTL_HOURS).",
          [({"status": s}, counts.get(s, 0)) for s in ("queued", "running", "done", "error")])
    running, queued = db.by_status("running", 1000), db.by_status("queued", 1000)
    gauge("job_oldest_running_seconds", "Age of the longest running job (0 when none).",
          [({}, max((now - (j["started"] or j["updated"]) for j in running), default=0))])
    gauge("job_oldest_queued_seconds", "Age of the oldest queued job (0 when none).",
          [({}, max((now - (j["queued_at"] or j["created"]) for j in queued), default=0))])
    from app.worker import worker_processes

    procs = worker_processes()
    gauge("worker_processes", "Huey processes on this host, by role (consumer, worker).",
          [({"role": r}, sum(p["role"] == r for p in procs)) for r in ("consumer", "worker")])
    gauge("worker_memory_bytes", "Resident memory of each Huey process.",
          [({"pid": str(p["pid"]), "role": p["role"]}, p["rss_bytes"]) for p in procs])
    seen = db.meta("worker_seen")
    gauge("worker_last_seen_seconds", "Seconds since the workers' periodic task last ran (runs every minute).",
          [({}, now - seen if seen else float("nan"))])

    families: dict[str, list[tuple[str, dict[str, str], float]]] = defaultdict(list)
    for name, labels, value in db.counters():
        base = next((h for h in HISTOGRAMS if name.startswith(h + "_")), name)
        families[base].append((name, labels, value))
    for base, rows in sorted(families.items()):
        if base in HISTOGRAMS:
            out.extend([f"# HELP {PREFIX}{base} {HELP.get(base, base)}", f"# TYPE {PREFIX}{base} histogram"])
            sets: dict[tuple, dict[str, float]] = defaultdict(dict)
            for name, labels, value in rows:
                if name.endswith("_bucket"):
                    key = tuple(sorted((k, v) for k, v in labels.items() if k != "le"))
                    sets[key][labels["le"]] = value
            for key, buckets in sorted(sets.items()):
                for le in [*map(str, HISTOGRAMS[base]), "+Inf"]:  # every bucket, zeros included
                    out.append(_line(f"{base}_bucket", {**dict(key), "le": le}, buckets.get(le, 0)))
            out.extend(_line(n, lab, v) for n, lab, v in rows if not n.endswith("_bucket"))
        else:
            out.extend([f"# HELP {PREFIX}{base} {HELP.get(base, base)}", f"# TYPE {PREFIX}{base} counter"])
            out.extend(_line(n, lab, v) for n, lab, v in rows)
    return "\n".join(out) + "\n"

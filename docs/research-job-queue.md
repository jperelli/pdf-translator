# Research: a job queue for PDF translations (SQLite, progress, events)

Date: 2026-10-28. Status: implemented with Huey (`SqliteHuey`) and our own `events` table; see the README section "Jobs: a Huey queue on SQLite".

Questions:
1. What can put each PDF translation into a queued job, report progress, and keep SQLite as the backend?
2. Can the same SQLite carry the events between the app and the worker, or is there something better?
3. Which Python alternatives exist besides Celery?

## Where we are today

- `JobStore` (`app/jobs.py`) runs each translation as an asyncio task **inside the web process**: at most 2 at a
  time, events fanned out in memory to the SSE endpoint.
- SQLite (`app/store.py`) persists jobs, versions and the final event list.

The gaps a queue would close:

- A restart kills running jobs. They are marked "interrupted" and must be redone, paying the tokens again.
- PDF work (MuPDF, OCR, rendering) shares the process with the web server; a crash in MuPDF takes the UI down.
- Workers cannot be added, or restarted, independently of the web app.
- Live events exist only in memory, so a second process (a worker) cannot stream to the UI without a channel.

## 1. Queues that work with SQLite

| Library | SQLite backend | Progress from inside the job | Async jobs | Notes |
|---|---|---|---|---|
| **Huey** | **Built in**: `SqliteHuey`, only the standard `sqlite3` module; priorities supported ([API][huey-api]) | No progress API; the result store takes arbitrary keys (`huey.put` / `huey.get(peek=True)`) ([API][huey-api], [recipes][huey-recipes]); signals for executing/complete/error, run in the worker ([recipes][huey-recipes]) | Tasks are sync; run our async pipeline with `asyncio.run()` inside the task | Small, mature, separate `huey_consumer` process; retries, delays, periodic tasks, revoke |
| **DBOS Transact** | **Built in**: SQLite is the default system database; Postgres recommended for production and required for more than one server ([connections][dbos-db], [config][dbos-config]) | **Yes**: `set_event`/`get_event` for current progress, and **streams** (`write_stream`/`read_stream`, append-only, readable from another process with the DBOS Client) for an event log ([communication][dbos-comm], [client][dbos-client]) | Yes (`write_stream_async`, async workflows) | Durable workflows: each step is checkpointed, so after a crash a workflow resumes from the last completed step ([queue worker example][dbos-worker]). Queues with concurrency control ([queues][dbos-queues]). Multi-process recovery needs their Conductor service ([FAQ][dbos-faq]). |
| **Celery** | Only via the kombu SQLAlchemy transport, **marked experimental**: no events or remote control, and "more than a few workers can lead to messages being executed multiple times" ([Celery docs][celery-sqla], [kombu][kombu-sqla]) | `update_state` + database result backend | No (sync) | Heavy for what we need, and the SQLite path is the weak one |
| **Taskiq** | Only through a third-party `taskiq-sqlalchemy` plugin (v0.1.1, **requires Python ≥ 3.14**, polling) ([PyPI][taskiq-sqla]); official brokers/backends are Redis, NATS, S3 ([result backends][taskiq-rb]) | Via result backend | Yes | Not usable today: we run Python 3.12 |
| **SAQ** | No: Redis or Postgres ([repo][saq-repo]) | **Yes**: `job.update(progress=...)`, `meta`, heartbeat, abort, small web UI ([job][saq-job], [tasks][saq-tasks]) | Yes | Best progress model, but needs Redis or Postgres |
| **Procrastinate** | No: PostgreSQL only (LISTEN/NOTIFY, `SELECT FOR UPDATE`) ([repo][procrastinate]) | | Yes | Good choice if we ever move to Postgres |
| **Dramatiq** | No: RabbitMQ or Redis ([guide][dramatiq]) | | | |
| **RQ / ARQ** | No: Redis | | | |
| **litequeue / persist-queue** | Yes, pure SQLite queues ([litequeue][litequeue], [persist-queue][persistqueue]) | No | | Building blocks (put/pop/done), not a job system: no worker, retries or progress |

### Option: our own queue on the SQLite we already have

We already have a `jobs` table and a single server. A minimal queue is about 150 lines:

- **Claim a job atomically:** `UPDATE jobs SET status='running', worker=?, heartbeat=? WHERE id = (SELECT id
  FROM jobs WHERE status='queued' ORDER BY created LIMIT 1) RETURNING *`. `RETURNING` exists since SQLite
  3.35 ([RETURNING][sqlite-returning]). With WAL mode and one writer at a time, two workers cannot claim the
  same row.
- **Heartbeat:** a job whose heartbeat is older than N seconds goes back to `queued`, or to `error` after
  M attempts.
- **Worker:** a separate process (`python -m app.worker`) running the same `TranslationPipeline`. Several of
  them can run on the same server.
- **Progress and events:** in an `events` table (next section).

It has no new dependency, the schema is ours, and progress is just our events.

## 2. Events between app and worker

**SQLite cannot push notifications across processes.**

- `sqlite3_update_hook` only fires for changes made on the same connection ([update hook][sqlite-hook]; SQLite
  forum: "it can only intercept changes invoked on said connection ... much less changes by another
  process" [forum][sqlite-forum]).
- There is no LISTEN/NOTIFY as in Postgres.

**What does work with SQLite is cheap polling:**

- The worker appends rows to an `events(id INTEGER PRIMARY KEY, job_id, version, type, payload, t)` table, one
  per pipeline event (stage, page, refine, trace span...). The worker already emits these.
- The SSE endpoint keeps a cursor (`last id`) and reads `WHERE job_id=? AND id > ?`.
- To avoid querying when nothing changed, poll `PRAGMA data_version`. It changes only when *another*
  connection committed something ([PRAGMA data_version][sqlite-dataversion]). A 200-300 ms poll gives
  near-real-time progress for almost no cost.
- The same table then *is* the run log (the Log tab and history), with no separate "save events at the end"
  step.

This is the same pattern DBOS uses for its streams on SQLite: it polls (`polling_interval_sec`, default 1 s,
"when not using LISTEN/NOTIFY") ([DBOS client][dbos-client]).

**If push is wanted instead of polling:**

| Channel | What it needs | Notes |
|---|---|---|
| Worker → app HTTP callback (`POST /internal/events`) | Nothing new | The app fans out in memory, as today. Events are lost if the app is down, unless also written to SQLite. |
| Unix socket / ZeroMQ between processes | A small socket server, or `pyzmq` | Only on one host |
| Redis pub/sub | A Redis service | Opens SAQ, RQ, ARQ, Dramatiq |
| NATS | A NATS service | Also a Taskiq broker |
| Postgres LISTEN/NOTIFY | Moving to Postgres | Opens Procrastinate, SAQ (Postgres), Huey's `PostgresHuey` ([API][huey-api]), DBOS in production |

## Recommendation

1. **Now: our own SQLite queue plus an `events` table polled with `PRAGMA data_version`.**
   - Zero dependencies and one database file.
   - Progress is our existing event stream, persisted as it happens. Live and replayed logs become the same
     thing, and redo versions keep working unchanged.
   - Workers run as separate processes, so a MuPDF crash or a deploy no longer kills a translation silently: the
     heartbeat puts it back in the queue.
2. **If we want a library for the queue mechanics:** Huey with `SqliteHuey` (retries, scheduling, revoke,
   mature), keeping our `events` table for progress.
3. **If resuming after a crash without paying the tokens again matters most:** DBOS Transact. Make each page
   translation a checkpointed step: a crash on page 15 of 20 resumes at page 15, not page 1. Use `write_stream`
   for events. Costs: a new programming model, and Postgres is recommended for production.
4. **Not recommended here:** Celery (its SQLite transport is experimental, with duplicate execution warnings);
   Taskiq (no usable SQLite backend on Python 3.12); SAQ, Procrastinate, Dramatiq, RQ, ARQ (they need Redis,
   RabbitMQ or Postgres). SAQ and Procrastinate become good options if Redis or Postgres enters the stack.

## Open questions

1. Should a redo also be a queued job (same worker), or stay in the web process? It is cheap (often re-render
   only), but consistent behaviour argues for the queue.
2. How many workers? The server size is unknown; each worker holds a PDF and its fonts in memory, and the LLM
   rate limit is shared.
3. Cancel a running job from the UI: needs a `cancel_requested` flag the pipeline checks between pages.

[huey-api]: https://huey.readthedocs.io/en/latest/api.html
[huey-recipes]: https://huey.readthedocs.io/en/stable/recipes.html
[dbos-db]: https://docs.dbos.dev/python/tutorials/database-connection
[dbos-config]: https://docs.dbos.dev/python/reference/configuration
[dbos-comm]: https://docs.dbos.dev/python/tutorials/workflow-communication
[dbos-client]: https://docs.dbos.dev/python/reference/client
[dbos-worker]: https://docs.dbos.dev/python/examples/queue-worker
[dbos-queues]: https://docs.dbos.dev/python/tutorials/queue-tutorial.md
[dbos-faq]: https://docs.dbos.dev/faq
[celery-sqla]: https://docs.celeryq.dev/en/3.1/getting-started/brokers/sqlalchemy.html
[kombu-sqla]: https://docs.celeryq.dev/projects/kombu/en/main/reference/kombu.transport.sqlalchemy.html
[taskiq-sqla]: https://pypi.org/project/taskiq-sqlalchemy/
[taskiq-rb]: https://taskiq-python.github.io/available-components/result-backends.html
[saq-repo]: https://github.com/tobymao/saq/
[saq-job]: https://github.com/tobymao/saq/blob/master/saq/job.py
[saq-tasks]: https://saq-py.readthedocs.io/en/stable/task.html
[procrastinate]: https://github.com/procrastinate-org/procrastinate/
[dramatiq]: https://dramatiq.io/guide.html
[litequeue]: https://github.com/litements/litequeue
[persistqueue]: https://github.com/peter-wangxu/persist-queue
[sqlite-returning]: https://www.sqlite.org/lang_returning.html
[sqlite-hook]: https://sqlite.org/c3ref/update_hook.html
[sqlite-forum]: https://sqlite.org/forum/forumpost/524b9656ce3bb216
[sqlite-dataversion]: https://www.sqlite.org/pragma.html#pragma_data_version

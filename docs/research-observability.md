# Research: observability for the translator (traces, errors, UI usage / session replay)

Date: 2026-10-28. Status: research only, nothing implemented.

Questions:
1. Can we use Grafana and store every span in Tempo?
2. What can store errors like Sentry, self-hosted? Can Grafana do it, or is there something better?
3. What is there for UI usage and screen recording (session replay), self-hosted?

## What we already have

On the Coolify server (project `jperelli`), checked through the Coolify API:

- **Running:** `grafana`, `loki` (status "unknown"), `prometheus`, `prometheus-node-exporter`, `cadvisor`.
- **Not running:** Tempo, a collector (Alloy or OpenTelemetry Collector), any error tracker or session-replay tool.

Coolify one-click templates relevant here:

- **Available:** `grafana`, `grafana-with-postgresql`, `bugsink`, `glitchtip`, `signoz`, `openobserve`, `langfuse`, `umami`, `openpanel`, `rybbit`.
- **Not available:** Tempo, Alloy, OpenReplay, PostHog. These would be deployed as a "Docker Compose Empty" service.

The server's CPU and RAM are not exposed by the API, so the sizing below is relative.

Inside the app:

- The Agents SDK already emits spans for agents, generations, tools and guardrails, sent to the OpenAI Traces
  dashboard.
- A local `TracingProcessor` (`app/observability.py`) streams those spans to the UI.
- Every LLM call is recorded with stage, page, tokens and estimated cost (`CallRecord`, in SQLite).
- Errors only reach the container logs (`log.exception`).

---

## 1. Traces: Grafana + Tempo

**Yes, this works, and it fits what is already running.**

- **Tempo is a fine store for these spans.**
  - It ingests OTLP over gRPC (4317) and HTTP (4318).
  - A single binary with local-filesystem storage (WAL + blocks on a volume) is the documented setup for
    single-node use ([Tempo, deploy on Linux][tempo-linux]).
  - Object storage is recommended only for production scale.
- **Tempo has no authentication** ([Tempo, deploy locally][tempo-local]). Keep it on the internal Docker
  network: Grafana and the collector talk to it, the internet does not.
- **A collector in front is optional but useful.** Grafana Alloy receives OTLP and forwards to Tempo
  ([Tempo, Alloy][tempo-alloy]; [`otelcol.receiver.otlp`][alloy-otlp]). The same Alloy can also:
  - ship container logs to the existing Loki;
  - receive browser telemetry with `faro.receiver` ([faro.receiver][faro-receiver]).

  One process then covers traces, logs and frontend events.
- **Getting the agent spans into OpenTelemetry**, two options:
  - **`openinference-instrumentation-openai-agents`**: Apache-2.0, production/stable, supports
    `openai-agents>=0.11` (we run 0.24). It turns Agents SDK spans into OTLP spans with LLM attributes
    (input, output, tokens, tool calls) ([PyPI][openinference]). This is also what Langfuse uses for the Agents
    SDK ([Langfuse][langfuse-agents]).
  - **Our own `TracingProcessor` → OTel bridge**: about 60 lines. We already have one for the UI. It gives
    full control over attributes and redaction.
- **The rest of the app** uses standard OpenTelemetry Python instrumentation:
  - FastAPI request spans;
  - our pipeline stages (extract, analyze, per page, layout round, render, review) as manual spans;
  - logs carrying the trace id (`OTEL_PYTHON_LOG_CORRELATION`, [OTel Python config][otel-py]), so Grafana can
    jump between a Loki log line and its Tempo trace.
- **Limitation.** Grafana's trace view is generic: LLM spans appear as spans with attributes. There is no
  prompt/response viewer, no per-model cost view and no eval UI out of the box. Dashboards can be built from
  span attributes (TraceQL, span metrics). If an LLM-specific UI is wanted, the same OTLP stream can also go to:
  - **Arize Phoenix:**
    - one container, SQLite by default, Postgres optional, OTLP on 4317;
    - free to self-host with no feature gates, but under the Elastic License 2.0, not OSI open source
      ([Phoenix self-hosting][phoenix-self], [architecture][phoenix-arch], [license][phoenix-license]).
  - **Langfuse:**
    - Coolify template available, but heavy: web + worker + Postgres + ClickHouse + Redis + S3, and ClickHouse
      is mandatory ([Langfuse v3 architecture][langfuse-v3], [ClickHouse][langfuse-ch]);
    - much more than needed for one app.

**Recommendation.** Tempo + Alloy next to the existing Grafana/Loki/Prometheus, the OpenInference instrumentor
for agent spans, manual spans for pipeline stages. Phoenix only if an LLM-specific UI turns out to be needed.

---

## 2. Errors: Grafana or a Sentry-compatible tracker?

### Can Grafana alone do it?

Partly.

- Python exceptions can be recorded as span events (visible in Tempo) and logged to Loki.
- Browser errors can be sent with the Faro Web SDK, which is open source, to Alloy and then Loki
  ([Faro][faro-oss], [faro-web-sdk][faro-sdk]).
- Grafana Alerting can alert on log queries.
- **What is missing** is what makes an error tracker useful: grouping occurrences into issues, first/last seen,
  resolve/regress, per-issue notifications, and stack traces with local variables.

### Sentry-compatible, self-hosted

All three accept the official Sentry SDKs: `sentry-sdk[fastapi]` on the backend, `@sentry/browser` for our
vanilla JS UI.

| Option | Footprint | Scope | Coolify template | Notes |
|---|---|---|---|---|
| **Bugsink** | One container, SQLite by default (MySQL/Postgres optional), no queue ([built to self-host][bugsink-self], [docker][bugsink-docker]) | Errors only, deliberately: no traces, replays or transactions ([SDK notes][bugsink-sdk], [vs Sentry][bugsink-vs]) | yes | 1.5M events/day on a 2 vCPU / 4 GB VPS ([comparison][bugsink-compare]) |
| **GlitchTip** | Django web + worker + Postgres + Valkey; ~512 MiB suggested by one template vendor ([Levelrail][glitchtip-levelrail]); compose has low-RAM options ([GlitchTip MR][glitchtip-mr]) | Errors, transactions/spans, uptime monitors, logs (OTel log ingestion since 6.2) ([comparison][bugsink-compare]) | yes | A small suite; overlaps with Tempo/Loki |
| **Sentry self-hosted** | Minimum 4 cores, 16 GB RAM + 16 GB swap, 20 GB disk ([Sentry docs][sentry-req]) | Everything | no | Too heavy for this server and this app |
| **SigNoz** | ClickHouse-based full APM ([ingestion][signoz-ingest]) | Exceptions read from OTel span events, grouped, with alerts ([exceptions][signoz-exc]) | yes | It would replace Grafana/Tempo/Loki rather than complement them |

**Recommendation: Bugsink.**
- It does one thing, runs in one container on SQLite with a volume, and has a Coolify template.
- It leaves traces and logs to Tempo/Loki, so nothing is duplicated.
- Link both ways: put the OTel trace id in the Sentry event tags, and the Sentry event id in the span.
- Choose GlitchTip instead only if uptime checks and a lightweight transaction view inside the same tool matter.

---

## 3. UI usage and session replay

### Grafana

Grafana does not cover replay for a self-hosted stack.

- Faro OSS sends web vitals, errors, logs and frontend traces to Alloy, then Loki/Tempo.
- **Session Replay** (`@grafana/faro-instrumentation-replay`) only works with the Grafana Cloud collector and
  the Grafana Cloud Frontend Observability app, and is in preview ([Session Replay][grafana-replay],
  [how it works][grafana-replay-how], [instrument][grafana-replay-inst]).
- `faro.receiver` explicitly does not work with Cloud Frontend Observability ([faro.receiver][faro-receiver]).
- So: Faro for usage/errors/performance into our own Loki, but no replay.

### Self-hosted replay tools

| Option | Footprint | Status | Notes |
|---|---|---|---|
| **OpenObserve** | Single binary / container ([getting started][o2-start]) | Coolify template; AGPL-3.0, open-source edition "feature-complete" ([pricing][o2-pricing], [GitHub][o2-gh]) | RUM, session replay, error tracking, plus logs/metrics/traces ([session replay][o2-replay], [RUM setup][o2-rum]). A second observability backend next to Grafana. |
| **OpenReplay** | Minimum 2 vCPU, 8 GB RAM, 50 GB, public IP and domain; Docker Compose marked experimental ([docs][openreplay-docker]) | no template | Full product analytics + replay; heavy |
| **PostHog (open source)** | Minimum 4 vCPU, 16 GB RAM, 30 GB; recommended only up to ~300k events/month ([PostHog blog][posthog-req]) | Self-hosting "officially unsupported", no versioned releases ([self-host][posthog-self], [disclaimer][posthog-disclaimer]) | Best product analytics, but the self-hosted story is weak |
| **Highlight.io** | Hobby: 8 GB RAM, 4 CPU ([repo][highlight-repo]) | Acquired by LaunchDarkly; services deprecated Feb 28, 2026 ([announcement][highlight-eol]) | Do not start on it |
| **rrweb, built into the app** | A browser library (MIT) + a table in our SQLite + a replay page using `rrweb-player` ([rrweb][rrweb], [license][rrweb-license], [recipe][rrweb-recipe]) | We own it | The lightest option. We control masking, retention and access, and can link a replay to a job id. |

For simple usage analytics (pages, events, funnels) without replay, Coolify has `umami`, `openpanel` and `rybbit`
templates. Faro events into Loki also cover "which tabs and options do people use" for an app this size.

**Privacy is not optional here.** Users upload documents (a CV is full of personal data), and our UI renders
their text in the Refinement and Review tabs. A replay records the DOM, so:
- mask text by default;
- never record the preview images or the edit textareas;
- set short retention;
- disclose the recording to users.

The same applies to traces: the Agents SDK records inputs and outputs by default (`trace_include_sensitive_data`),
so decide what translated text goes into Tempo/Phoenix and redact in our processor before export
([Agents SDK tracing][agents-tracing]).

**Recommendation.**
- **If replay is mostly for debugging our own app:** rrweb inside the app, recording only when the user opts in
  or when an error occurs, stored next to the job and viewable from the Log tab.
- **If you want RUM, replay and error tracking in one product:** OpenObserve (Coolify template, single
  container). It accepts that some data lives outside Grafana.

---

## Summary of options

| Need | Option A: Grafana-centric (recommended) | Option B: one tool |
|---|---|---|
| Traces | Tempo (+ Alloy), viewed in the existing Grafana | OpenObserve |
| Logs | Existing Loki (via Alloy, with trace ids) | OpenObserve |
| Metrics | Existing Prometheus | OpenObserve |
| Agent / LLM spans | OpenInference instrumentor → Tempo; optional Phoenix for an LLM UI | OpenObserve (generic spans) |
| Errors | Bugsink (Sentry SDKs) | OpenObserve error tracking |
| Frontend usage | Faro → Alloy → Loki | OpenObserve RUM |
| Session replay | rrweb inside the app | OpenObserve session replay |
| New services | Tempo, Alloy, Bugsink | OpenObserve |

## Open questions

1. Server size (CPU/RAM/disk): it decides whether ClickHouse-based tools (Langfuse, SigNoz) are even an option.
2. Is the existing Loki healthy? Its status is "unknown" in Coolify.
3. Who should be able to see traces and replays (they contain document text)? Should Grafana and Bugsink be
   internal-only?
4. Replay always on, sampled, on error only, or opt-in?

[tempo-linux]: https://grafana.com/docs/tempo/latest/set-up-for-tracing/setup-tempo/deploy/locally/linux/
[tempo-local]: https://grafana.com/docs/tempo/latest/set-up-for-tracing/setup-tempo/deploy/locally/
[tempo-alloy]: https://grafana.com/docs/tempo/latest/set-up-for-tracing/instrument-send/set-up-collector/grafana-alloy/
[alloy-otlp]: https://grafana.com/docs/alloy/latest/reference/components/otelcol/otelcol.receiver.otlp/
[faro-receiver]: https://grafana.com/docs/alloy/latest/reference/components/faro/faro.receiver/
[openinference]: https://pypi.org/project/openinference-instrumentation-openai-agents/
[langfuse-agents]: https://langfuse.com/docs/integrations/openai-agents
[otel-py]: https://opentelemetry.io/docs/zero-code/python/configuration/
[phoenix-self]: https://arize.com/docs/phoenix/self-hosting
[phoenix-arch]: https://arize.com/docs/phoenix/self-hosting/architecture
[phoenix-license]: https://arize.com/docs/phoenix/self-hosting/license
[langfuse-v3]: https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v2-to-v3
[langfuse-ch]: https://langfuse.com/self-hosting/deployment/infrastructure/clickhouse
[faro-oss]: https://grafana.com/oss/faro/
[faro-sdk]: https://github.com/grafana/faro-web-sdk/
[bugsink-self]: https://www.bugsink.com/built-to-self-host/
[bugsink-docker]: https://www.bugsink.com/docs/docker-install/
[bugsink-sdk]: https://www.bugsink.com/docs/sdk-recommendations/
[bugsink-vs]: https://www.bugsink.com/sentry-vs-bugsink/
[bugsink-compare]: https://www.bugsink.com/blog/glitchtip-vs-sentry-vs-bugsink/
[glitchtip-levelrail]: https://docs.levelrail.com/self-host/glitchtip
[glitchtip-mr]: https://gitlab.com/glitchtip/glitchtip-frontend/-/merge_requests/659
[sentry-req]: https://develop.sentry.dev/self-hosted/
[signoz-ingest]: https://signoz.io/docs/ingestion/self-hosted/overview/
[signoz-exc]: https://signoz.io/docs/userguide/exceptions/
[grafana-replay]: https://grafana.com/docs/grafana-cloud/observe-and-act/monitor-applications/frontend-observability/session-replay/
[grafana-replay-how]: https://grafana.com/docs/grafana-cloud/monitor-applications/frontend-observability/session-replay/how-it-works/
[grafana-replay-inst]: https://grafana.com/docs/grafana-cloud/observe-and-act/monitor-applications/session-replay/instrument/
[o2-start]: https://openobserve.ai/docs/getting-started/
[o2-pricing]: https://openobserve.ai/pricing.md
[o2-gh]: https://github.com/openobserve/openobserve
[o2-replay]: https://openobserve.ai/docs/user-guide/data-exploration/rum/session-replay/
[o2-rum]: https://openobserve.ai/docs/user-guide/data-exploration/rum/setup/
[openreplay-docker]: https://docs.openreplay.com/en/deployment/deploy-docker/
[posthog-req]: https://posthog.com/blog/best-open-source-analytics-tools
[posthog-self]: https://posthog.com/docs/self-host
[posthog-disclaimer]: https://posthog.com/docs/self-host/open-source/disclaimer
[highlight-repo]: https://github.com/highlight/highlight
[highlight-eol]: https://www.linkedin.com/posts/highlightio_hello-highlight-community-were-shifting-activity-7394843729746112513-h4Tc
[rrweb]: https://github.com/rrweb-io/rrweb/
[rrweb-license]: https://github.com/rrweb-io/rrweb/blob/main/LICENSE
[rrweb-recipe]: https://github.com/rrweb-io/rrweb/blob/main/docs/recipes/record-and-replay.md
[agents-tracing]: https://openai.github.io/openai-agents-python/tracing/

# Latency harness

Operator runbook for per-step booking-flow latency measurement in booking-agent-cli.

## What is measured

Six agent-owned steps emit `step_timing` on stderr (and persist on the session as `flow_timings`):

| Step | Meaning |
|---|---|
| `discover` | Registry search POST |
| `profile_resolve` | Parallel bounded profile + service fan-out after search |
| `availability` | Live catalog + availability query |
| `checkout_create` | USP `create_booking` or UCP `create_checkout` |
| `pay` | Charge path excluding Link approval wait |
| `confirm` | Time to terminal booking status after pay |

Fields: `wall_ms`, `remote_ms`, `agent_ms`, `pacing_ms`. Human gates emit `human_gate_timing` (`gate`, `human_gate_ms`) and are **never** folded into service p50/p95.

Every event carries `correlation_id` when the flow is bound. Businesses receive the same value in the `USP-Correlation-Id` request header, so a slow step can be joined to server logs.

## Run the harness

Run these from the `booking-agent/` directory.

**Hermetic (CI / PR regression):**

```bash
uv run python scripts/latency_soak.py --mode hermetic --iterations 20
```

**Staging (live registry search):**

```bash
export BOOKING_AGENT_STAGING_SEARCH_QUERY=haircut   # optional
uv run python scripts/latency_soak.py --mode staging --iterations 5
```

**Check against published budgets:**

```bash
uv run python scripts/latency_soak.py --mode hermetic --budgets ops/budgets.json
```

Exits **non-zero** on p95 breach.

## Environment

| Variable | Purpose |
|---|---|
| `USP_REGISTRY_URL` | Optional public registry override |
| `BOOKING_AGENT_STAGING_SEARCH_QUERY` | Registry query (default `haircut`) |
| `BOOKING_AGENT_MERCHANT_REQUEST_DELAY_MS` | Min interval between HTTP calls to the same merchant host (default `200`) |
| `BOOKING_AGENT_DISCOVERY_CONCURRENCY` | Max parallel registry-hit enrich tasks (default `8`) |
| `BOOKING_AGENT_HTTP_*` / `BOOKING_AGENT_CIRCUIT_*` | HTTP deadlines, retries, and circuit breaker (see "Outbound HTTP" in [CONTRIBUTING.md](../CONTRIBUTING.md)) |

## Regression guard

- **PR CI:** `packages/cli/tests/e2e/test_latency_soak.py` (hermetic, breach detection)
- **Scheduled:** `.github/workflows/latency-soak.yml` at the repository root (weekly + manual). Uses the public Wix registry unless the repository variable `USP_REGISTRY_URL` overrides it.

## Published budgets

See [`budgets.md`](budgets.md) for the p50/p95 table, HTTP deadline reconciliation, discovery fan-out, and confirm poll schedule.

## Normal runs without the harness

Any `booking-agent flow ...` command in a booking flow emits timings on stdout `data.flow_timings` and stderr `step_timing` events. Operators can diagnose a slow booking from a single failed run using `correlation_id` from the JSON envelope.

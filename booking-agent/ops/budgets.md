# Latency budgets

**Measured:** 2026-08-24  
**Load:** `concurrency=1`, serial iterations  
**Dataset:** `DEFAULT_REGISTRY_SEARCH_LIMIT=5`, query `haircut`

Budgets are **p50/p95 wall-clock ms** for service time. **Human gates** (Link spend approval) are excluded and reported separately. Each p95 is **reconciled with the HTTP deadline** on the same hop (see "Outbound HTTP" in [CONTRIBUTING.md](../CONTRIBUTING.md)); the budget is never above its deadline.

| Step | p50 wall (ms) | p95 wall (ms) | p50 remote (ms) | p95 remote (ms) | HTTP deadline (ms) | Notes |
|---|---:|---:|---:|---:|---:|---|
| `discover` | 500 | 15,000 | 400 | 15,000 | 15,000 | Registry POST only |
| `profile_resolve` | 2,500 | 30,000 | 2,000 | 28,000 | 30,000 | Parallel fan-out (bounded concurrency) + per-host 200ms min interval |
| `availability` | 1,500 | 15,000 | 1,200 | 14,000 | 15,000 | GET service + availability query |
| `checkout_create` | 2,000 | 60,000 | 1,800 | 55,000 | 60,000 | USP create_booking or UCP create_checkout |
| `pay` | 3,000 | 90,000 | 2,500 | 85,000 | 90,000 | UCP `complete_checkout`; **excludes** approval poll |
| `confirm` | 1,000 | 120,000 | 800 | 15,000 | 120,000 | Poll safety net 120s; quoted p95 is service GET time, not poll ceiling |

The machine-readable copy that `scripts/latency_soak.py` enforces is [`budgets.json`](budgets.json).

## Measurement commands

**Hermetic regression (synthetic in-process steps, no network):**

```bash
uv run python scripts/latency_soak.py --mode hermetic --iterations 20
```

Observed hermetic p95 (2026-08-24, synthetic sleeps): discover 11ms, profile_resolve 21ms, other steps ~11ms. These are **regression floors**, not production targets.

**Staging (live registry search):**

```bash
uv run python scripts/latency_soak.py --mode staging --iterations 5 --query haircut
```

Staging mode measures the `discover` step against the public Wix USP registry. `USP_REGISTRY_URL` may override the registry.

## Human gates (separate series)

| Gate | Ceiling | Target |
|---|---:|---|
| `link_spend_approval` poll | 600,000 ms (10 min) | Ceiling only, not SLO |

## Confirm poll schedule

- Interval: **1.0s** default (`book poll --poll-interval`)
- Wait timeout: **120s** default (`book poll --timeout`)
- Terminal statuses: `confirmed`, `completed`, `canceled`, `no_show`
- The 120s cap is a **safety net**, not the quoted SLO. Time-to-terminal distribution comes from `step_timing` on `confirm` during real runs.

## Discovery fan-out

Profile and service enrichment after a registry search runs in parallel (default `BOOKING_AGENT_DISCOVERY_CONCURRENCY=8`). Pacing is a **per-host minimum interval** between HTTP calls (`BOOKING_AGENT_MERCHANT_REQUEST_DELAY_MS`, default 200): the first call to a host is immediate, and file-cache profile hits skip pacing. For 5 distinct merchants, worst-case pacing is roughly one interval per host plus HTTP overlap, instead of up to 10 stacked sleeps (about 2s) in a serial fan-out.

# Contributing to booking-agent-cli

All development happens in [`wix/booking-agent`](https://github.com/wix/booking-agent) on the `master` branch. This directory, `booking-agent/`, is the product workspace. The repository root holds the hosted platform profile and its validators; see the [root CONTRIBUTING.md](../CONTRIBUTING.md) for branch roles and the profile update flow.

## Dev environment setup

### Clone and install

```bash
git clone https://github.com/wix/booking-agent.git
cd booking-agent/booking-agent
uv sync --frozen --all-packages --all-extras  # exact versions from uv.lock, editable packages + pytest
```

Run every command in this guide from `booking-agent/`.

`uv.lock` is committed and CI installs from it with `--frozen`. After changing dependencies in a `pyproject.toml`, run `uv lock` and commit the updated lockfile.

Plain pip works too if you're not on `uv`:

```bash
pip install -e "./packages/cli[dev]"
```

Smoke check:

```bash
uv run booking-agent --help
uv run pytest -q
```

### Running the flow locally

The flow talks to real USP businesses via the live registry, with no local servers. Defaults ship in `packages/cli/src/booking_agent/defaults.env`; override them in `~/.booking-agent/.env` (`booking-agent config init` creates it) or `./.env`, and check the result with `booking-agent config show`.

```bash
booking-agent flow init --user-request "haircut"
booking-agent flow prereq check               # registry reachability only
booking-agent flow discover --query "haircut"
# ... follow `next_command` in the agent-facing JSON
booking-agent flow validate-skill-compliance  # before declaring success
```

`buyer resolve` and payment require an authenticated `link-cli`. The v2 flow books only UCP-native businesses and does not require a browser. It fails closed for redirect checkout, deposits, pay-at-service, and other explicit payment timing values besides `at_booking`. Discovery remains free-text only and must not add a structured `categories[]` filter.

To enable Google Calendar conflict checking, set the `GOOGLE_CALENDAR_*` settings listed by `booking-agent config show` in your `.env`. `.env` is gitignored. Never commit OAuth client secrets or other credentials. Without calendar credentials, use `booking-agent flow calendar skip` to proceed.

### GitHub MCP for AI coding assistants (optional)

If your AI coding assistant should file issues and open PRs for you, connect it to the official GitHub MCP server (`https://api.githubcopilot.com/mcp/`) with your own GitHub credentials, so actions are attributed to you. The `wix` org does not allow fine-grained personal access tokens, so use the server's OAuth sign-in or your existing `gh` login. Never commit a token.

---

## Where code lives

| Path | What goes there |
|------|-----------------|
| `packages/cli/src/` | Product code only |
| `packages/cli/tests/unit/` | Pure functions, no I/O |
| `packages/cli/tests/integration/` | CLI commands in-process with the mock backend, calendar, and Link |
| `packages/cli/tests/e2e/` | End-to-end runs over the loopback HTTP stack, the latency soak, and opt-in live registry checks |
| `packages/cli/tests/support/mock/` | `MockUSPBackend`, `MockCalendarService`, mock merchants, mock `link-cli` |
| `packages/cli/tests/support/fixtures/` | Recorded registry, catalog, and checkout payloads |
| `packages/cli/tests/support/offline_stack/` | Loopback registry, USP REST, and UCP checkout servers that E2E tests start in pytest |
| `packages/cli/tests/.env.tests` | Test-only environment switches (export them in your shell; the CLI never reads them) |
| `scripts/` | `install.sh`, `latency_soak.py`, `generate_platform_profile.py` |
| `ops/` | Latency budgets and the soak runbook |

Product code has no test shortcuts. Do not add environment flags, mock branches, or demo paths to `src`. Give the code a seam instead (for example `payment_token.shared_payment_token_source()`) and have tests inject a fake with `monkeypatch`, such as `support.mock.link_cli.fake_shared_payment_token`.

## Pull request flow

1. Branch off `master`: `git checkout -b your-name/short-description`.
2. Make the change. Keep diffs focused: one logical change per PR.
3. Run the full test suite locally: `uv run pytest -q`.

   Hermetic UCP-native E2E (mocks): `uv run pytest packages/cli/tests/e2e/test_ucp_native_e2e.py -q`.

   SKILL flow over loopback HTTP: `uv run pytest packages/cli/tests/e2e/test_ucp_skill_flow_e2e.py -q`.

   Optional live registry check: set `BOOKING_AGENT_STAGING_REGISTRY_TEST=1` to run `packages/cli/tests/e2e/test_staging_registry_integration.py`. It searches `haircut` and `AC repair Yuma` and resolves the HVAC test business profile by default; override with `USP_REGISTRY_URL`, `BOOKING_AGENT_STAGING_SEARCH_QUERY` (comma-separated), or `BOOKING_AGENT_STAGING_UCP_PROFILE_URL`. See [`packages/cli/tests/.env.tests`](packages/cli/tests/.env.tests).
4. If you changed `platform_profile_doc()`, regenerate the artifact (`uv run python scripts/generate_platform_profile.py`) and follow the profile flow in the [root CONTRIBUTING.md](../CONTRIBUTING.md).
5. Open the PR against `master`. The CI workflow at [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) must be green, and a repository admin merges it.

## Outbound HTTP: timeouts, retries, circuit breaker

Every agent HTTP call goes through `traced_async_client` (or `coerce_timeout` for the few sync `httpx` call sites). A missing timeout is not allowed to fall through to httpx's default hang: connect and overall deadlines are always set.

| Knob | Default | Meaning |
|---|---|---|
| `BOOKING_AGENT_HTTP_TIMEOUT` | `15` | Overall read/write/pool deadline (seconds) when a call site does not pass its own overall timeout |
| `BOOKING_AGENT_HTTP_CONNECT_TIMEOUT` | `5` | TCP connect deadline (seconds), capped at the overall deadline |
| `BOOKING_AGENT_HTTP_MAX_RETRIES` | `4` | Attempt cap for 429, 500, 502, 503, 504, connect timeout, connect error, and pool timeout |
| `BOOKING_AGENT_HTTP_BACKOFF_BASE` | `1.0` | Initial backoff (seconds); doubled each attempt, with jitter |
| `BOOKING_AGENT_HTTP_BACKOFF_MAX` | `300` | Backoff cap (seconds) |
| `BOOKING_AGENT_CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive per-host failures (5xx, 429, timeout, network error) before the circuit opens |
| `BOOKING_AGENT_CIRCUIT_COOLDOWN_SECONDS` | `30` | How long an open circuit sheds load before a half-open probe |
| `BOOKING_AGENT_CIRCUIT_DISABLED` | unset | Set to `1` to disable circuit breaking (retries and timeouts still apply) |

Retries on booking/checkout writes reuse the `Idempotency-Key` minted for that logical call. Read timeouts are **not** retried (the peer may already have processed the body). Per-call overall deadlines stay with the call site (discovery 10s, USP REST 15s, profile 30s, UCP checkout 60s, `complete_checkout` 90s, registry probe 5s).

A hung or failing merchant therefore fails the agent turn within those bounds instead of blocking indefinitely. Staging check: point discovery or checkout at a black hole / 5xx host and confirm the command returns an error instead of hanging.

## Money-path observability

Each booking flow gets a stable `correlation_id` (uuid4, distinct from `session.id`) persisted on the session record and bound for the CLI process lifetime. It is sent on every outbound HTTP call as `USP-Correlation-Id` (injected in `traced_async_client`, including registry search) and surfaced on every `AgentOutput` / `emit_error` envelope as `correlation_id`.

Machine-countable money-path and resilience events are one JSON object per line on **stderr** (not stdout). Each record has a stable `event` name and, when bound, `correlation_id`. Join keys such as `spend_request_id` and `booking_id` may appear as log fields; they are never metric labels. No SPT, credentials, or buyer PII appear in event fields.

| Event | When |
|---|---|
| `charge_attempt` | Immediately before UCP `complete_checkout` |
| `payment_declined` | UCP/processor decline (`reason_class`: `declined`, `processor_error`, `unknown`). A declined SPT also carries `message_code`, `checkout_id`, `merchant_profile_url`, `merchant_name`, and `new_card_retry` (true when this spend request replaced an earlier declined one) |
| `post_failure_action_suppressed` | After a declined `ucp-complete`, the one spend-request retrieve was `requires_action`; the `action_url` was withheld. Carries `checkout_id` for reconciliation |
| `declined_spt_retry_succeeded` | A new spend request completed checkout after a declined SPT (`declined_spend_request_id`, `spend_request_id`, `message_code`, merchant fields) |
| `payment_captured_booking_unconfirmed` | Charge completed but booking not confirmed, or booking poll timed out after payment |
| `confirmation_failed` | Booking reached a terminal failure after payment was attempted |
| `price_mismatch` | Checkout totals disagree with the expected charge |
| `registry_search` | Registry POST succeeded (`ok`, `latency_ms`) |
| `registry_search_error` | Registry POST failed (`error_class`) |
| `http_retry` | `with_http_retry` backoff |
| `circuit_open` / `circuit_half_open` | Per-host circuit breaker transitions |
| `http_timeout` | Connect/read timeout on outbound HTTP |
| `step_timing` | Per-step wall/remote/agent/pacing ms for booking flow steps (`discover`, `profile_resolve`, `availability`, `checkout_create`, `pay`, `confirm`) |
| `human_gate_timing` | Buyer think-time excluded from service budgets (`gate`, `human_gate_ms`) |

Opt-in HTTP trace (`BOOKING_AGENT_HTTP_TRACE=1`) writes redacted request/response records to `~/.booking-agent/http_trace.log` and includes `correlation_id` alongside the per-exchange `trace_id`. Trace-on-disk is for debugging only; operators should read `correlation_id` from the CLI JSON envelope.

## Dispute evidence join keys

After the appointment (`slot.end`), archive hygiene strips remaining buyer fields and secrets from the session row but **retains payment join keys** needed for post-service disputes: `correlation_id`, `booking.id`, `payment.spend_request_id`, `payment.order_id`, `payment.status`, and optional `payment.approved_at` (convenience copy from Link). SPTs and booking credentials are always wiped.

Live session files (`.usp-session.json`) omit buyer email and phone at every save. `buyer.name` and `buyer_source` may remain; contact is rehydrated from `link-cli user-info retrieve` when a command needs it.

The agent session holds handles only. Link remains the system of record for irrefutable spend approval and charge proof. When a consumer disputes after the service date, retrieve the spend request with `link-cli spend-request retrieve <spend_request_id>` using the id kept on the session.

## Latency budgets and soak harness

Per-step timings are persisted on the session as `flow_timings` and `human_gate_timings`, emitted on every flow command stdout `data`, and mirrored as `step_timing` / `human_gate_timing` stderr events. Human gates such as Link spend approval polling are never folded into service p50/p95.

Run the hermetic soak (always in CI):

```bash
uv run python scripts/latency_soak.py --mode hermetic
```

Staging soak (uses the public Wix USP registry by default):

```bash
uv run python scripts/latency_soak.py --mode staging --iterations 3
```

Published budgets and measurement commands live in [`ops/budgets.md`](ops/budgets.md). Operator runbook: [`ops/latency.md`](ops/latency.md).

## Repo conventions

- **Spec conformance:** USP models in `packages/cli/src/booking_agent/models/` mirror the schemas in [`wix/universal-scheduling-protocol`](https://github.com/wix/universal-scheduling-protocol). Permissive divergences (Optional fields where the spec REQUIRES) carry docstrings explaining why. Don't tighten those without coordinating; see the relevant model docstring.
- **Flow gates are mandatory:** the `booking-agent flow` state machine in `packages/cli/src/booking_agent_cli/engine.py` enforces calendar, slot-selection, and Link-buyer gates. Skill docs and examples must drive through `booking-agent flow *`, never raw `curl` to merchant USP endpoints.
- **No mocks in source:** product code (`packages/cli/src`) must never import test mocks. The in-memory `MockUSPBackend`/`MockCalendarService` and fixtures live under `packages/cli/tests/support/mock` and are injected into tests via monkeypatch.
- **Doc sync:** if you change repo structure, command surface, or as-built status, update [`README.md`](README.md) and this file in the same PR.

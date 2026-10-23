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

`buyer resolve` and payment require an authenticated `link-cli`. The v2 flow books only UCP-native businesses and does not require a browser. It fails closed for redirect checkout and payment timing values it does not know. A pay-at-appointment service goes through a checkout only when the business declares `dev.usp-protocol.services.pay_at_service` (USP 11.3), and then completes with no payment instrument. Discovery remains free-text only and must not add a structured `categories[]` filter.

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
| `scripts/` | `install.sh`, `latency_soak.py`, `generate_platform_profile.py`, `check_version.py`, `release_notes.py`, `sync_skill_bundle.py` |
| `ops/` | Latency budgets and the soak runbook |

Product code has no test shortcuts. Do not add environment flags, mock branches, or demo paths to `src`. Give the code a seam instead (for example `payment_token.shared_payment_token_source()`) and have tests inject a fake with `monkeypatch`, such as `support.mock.link_cli.fake_shared_payment_token`.

## Pull request flow

1. Branch off `master`: `git checkout -b your-name/short-description`.
2. Make the change. Keep diffs focused: one logical change per PR.
3. Run the full test suite locally: `uv run pytest -q`.

   Hermetic UCP-native E2E (mocks): `uv run pytest packages/cli/tests/e2e/test_ucp_native_e2e.py -q`.

   SKILL flow over loopback HTTP: `uv run pytest packages/cli/tests/e2e/test_ucp_skill_flow_e2e.py -q`.

   Optional live registry check: set `BOOKING_AGENT_STAGING_REGISTRY_TEST=1` to run `packages/cli/tests/e2e/test_staging_registry_integration.py`. It searches `haircut` and `AC repair Yuma` and resolves the HVAC test business profile by default; override with `USP_REGISTRY_URL`, `BOOKING_AGENT_STAGING_SEARCH_QUERY` (comma-separated), or `BOOKING_AGENT_STAGING_UCP_PROFILE_URL`. See [`packages/cli/tests/.env.tests`](packages/cli/tests/.env.tests).
4. If you changed `platform_profile_doc()`, regenerate the artifact (`uv run python scripts/generate_platform_profile.py`) and follow the profile flow in the [root CONTRIBUTING.md](../CONTRIBUTING.md). If you changed `skills/booking-agent/SKILL.md`, raise its frontmatter `version` and copy it into the package with `uv run python scripts/sync_skill_bundle.py` (CI runs it with `--check`).
5. If you changed shipped code (`packages/cli/src/`, which includes the bundled copy of the skill, or the dependencies or other distribution metadata in `packages/cli/pyproject.toml`), raise the version, add its release metadata, and fill in the PR's rollout section (see [Releasing](#releasing)). Check with `uv run python scripts/check_version.py --base origin/master` and `uv run python scripts/release_notes.py --check`.
6. Open the PR against `master` with exactly one release label (see [Pull request title and label](#pull-request-title-and-label)). The CI workflows at [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and [`.github/workflows/release-pr.yml`](../.github/workflows/release-pr.yml) must be green, and a repository or `wix` org admin merges it.

## Releasing

Merging a PR into `master` that raises the `booking-agent-cli` version publishes that version to PyPI automatically ([`.github/workflows/release.yml`](../.github/workflows/release.yml)), then tags the commit `v<version>` and creates a GitHub Release. There is no release to cut and no deployment to approve. This section is the whole policy; `AGENTS.md` points agents here.

A release answers three separate questions, and each has its own place:

| Question | Where it is decided |
|---|---|
| What number? | Semantic versioning, below |
| What does it say to readers? | The PR title and label, which become the GitHub Release notes, plus the `CHANGE_LOG.md` entry |
| Should an installed assistant tell its user about it? | `release-notes/<version>.json`, which the installed CLI reads |

### Version number

- Every PR that changes shipped code must raise the version above the one on `master`; the CI `version` job fails otherwise. Docs, CI, test, and script changes need no bump. A skill change is shipped code: the wheel carries a copy of the skill so the installed CLI can hand an assistant the instructions that match it.
- Choose the bump with semantic versioning. A breaking change is anything a user or the `booking-agent` skill depends on that stops working: a removed or renamed command, flag, or environment variable, a changed JSON output field or exit code, a changed flow step order, or a raised minimum Python.
  - Below `1.0.0`: patch (`0.1.0` to `0.1.1`) for fixes and internal changes users cannot notice; minor (`0.1.0` to `0.2.0`) for new features **and** for breaking changes.
  - From `1.0.0` on: patch for fixes, minor for backwards-compatible features, major for breaking changes.
  - Going to `1.0.0` is the release owner's decision.
  - A PR that mixes kinds uses the largest bump it needs. CI rejects a version that is not exactly one step after `master`'s, for example `0.1.0` to `0.1.2` or `0.3.0`.
- Because a minor bump below `1.0.0` can be either a feature or a breaking change, the number alone never tells anyone which. `change_type` in the release metadata does.
- To bump, change `version` in `packages/cli/pyproject.toml` and `__version__` in both `packages/cli/src/booking_agent/__init__.py` and `packages/cli/src/booking_agent_cli/__init__.py`, then run `uv lock`. CI checks that all four agree.
- PyPI versions are permanent and cannot be re-uploaded. Work that should ship as one version goes into one PR with one bump; each merged bump is its own release.
- `.devN` versions go to TestPyPI only, by publishing a GitHub Release tagged `v<version>` on a commit already on `master`.

### Pull request title and label

`release.yml` creates each GitHub Release with `--generate-notes`, which lists the title of every PR merged since the previous `v*` tag, unchanged. [`.github/release.yml`](../.github/release.yml) groups them by label.

- **Label.** Every PR carries exactly one of `fix`, `feature`, or `breaking` when it raises the version (matching `change_type` in its release metadata), or `internal` when it does not. `internal` PRs are left out of the release notes.
- **Title.** For a PR that raises the version, the title is the release note. Write what users get, in plain words: `Recover safely when payment confirmation times out`, `Add calendar conflict checking`. Not mechanics: no "Bump to 0.2.0", "Sync files", `feat:` prefixes, file or module names, version numbers, or issue numbers. If several changes ship together, name the largest user-visible outcome and describe the rest in the PR body and the changelog.
- **Changelog.** The PR's `CHANGE_LOG.md` entry names the version it releases, for example `Released as 0.2.0`, and says what changed for users and why.

`release-pr.yml` checks the label, the title, and the rollout section on every PR edit.

### Rollout: should users be told?

Each published version has `release-notes/<version>.json`. From `booking-agent-cli` 0.2.0, an installed assistant reads the generated `release-notes/index.json` and decides from it whether to mention a newer version to its user. A patch should not interrupt anyone, and a release that businesses or the registry are not ready for must stay quiet even though it is on PyPI. Regenerate the index with `uv run python scripts/release_notes.py` after editing any entry.

```json
{
  "schema_version": 1,
  "cli_version": "0.2.0",
  "skill_version": "0.48.0",
  "change_type": "feature",
  "update_status": "deferred",
  "ecosystem_ready": false,
  "relevant_capabilities": ["booking"],
  "user_summary": "Lets the assistant tell you when an update is ready.",
  "details_url": "https://github.com/wix/booking-agent/releases/tag/v0.2.0",
  "readiness_evidence": []
}
```

- `skill_version` is the skill frontmatter version this release ships.
- `relevant_capabilities` is any of `search`, `sign_in`, `calendar`, `booking`, `payment`, `cancel`, `reschedule`.
- `user_summary` is one plain sentence the assistant may repeat to a user: no commands, flags, links, or issue numbers.
- `replacement_version` is required for, and only for, `withdrawn`.

Decide `change_type`, `update_status`, and `ecosystem_ready` in this order. Do not infer any of them from the version number.

1. **Classify `change_type`** (`fix`, `feature`, or `breaking`) with the rules under [Version number](#version-number). Below `1.0.0`, say explicitly whether a minor release is a backwards-compatible feature or a breaking change.
2. **List every compatibility surface the change touches:** CLI commands and JSON output, the skill's instructions, saved session files, USP, UCP, and platform-profile versions, registry requests and responses, and business requests and responses.
3. **Start from `update_status: deferred` and `ecosystem_ready: false`** whenever an external contract changes or readiness is unproven. Silence is the safe default.
4. **Set `ecosystem_ready: true` only with evidence**, recorded in `readiness_evidence` and in the PR body:
   - existing businesses stay compatible, through unchanged contracts, version negotiation, or a tested fallback;
   - the registry in use today accepts and returns the shapes and versions the release needs;
   - saved session files stay safe across the upgrade (and a downgrade, where one is possible);
   - integration or staging tests cover the affected path;
   - for a change to `sign_in`, `calendar`, or `payment` behavior, someone ran it in a real hosted session (see [Hosted replay](#hosted-replay)) and `readiness_evidence` has a line starting `Hosted replay:`. `scripts/release_notes.py --check` fails without it from 0.5.0 on.
5. **Choose `update_status`:**
   - `available`: a normal fix or internal improvement. Users are not prompted.
   - `recommended`: a ready feature with a clear benefit to users, or a broadly useful fix. Mentioned after a booking, or before one when it is relevant to it. A ready breaking release is offered before the next booking.
   - `urgent`: only for a security, data-loss, payment-safety, or severe correctness problem, and only with the release owner's approval written in the PR as `Release-owner approval: <who, why>`. Offered before the next booking.
   - `deferred`: the rollout is not finished or evidence is missing. Never offered, and a newer version is not offered past it either, because installing it would bring this release's changes too.
   - `withdrawn`: a released version that must not be installed any more. Needs a newer, installable `replacement_version`.
6. **Green CI is not readiness.** A newer version, passing tests, or a schema that validates does not show that the registry and live businesses work with the release.

#### Hosted replay

Offline tests cover the CLI; they cannot show how a hosted runtime and its agent behave. Before a release that touches sign-in, calendar, or payment is marked `ecosystem_ready: true`, run one throwaway booking (a $1 service) in a real hosted session and record what you saw in one `Hosted replay:` evidence line. The person asking for the rollout runs it; the release owner checks the line when reviewing the PR. The check proves the line exists, not that the replay happened, so review it.

1. Start a booking, sign in to Link when asked, and confirm the search area.
2. Force a restart of the runtime (new conversation, fresh filesystem) after choosing the slot. Expect at most **one** message asking to reconnect anything (Link, calendar, or both together), and no second sign-in unless the Link session really expired.
3. Say yes at the confirmation. Expect a one-line acknowledgment immediately.
4. Approve the payment in Link. Expect the "You're booked" message. If calendar was
   connected, expect the raw calendar event link. If calendar was explicitly skipped or
   is unavailable in the harness, expect a clear statement that no calendar event was added.
5. Ask "what's going on?" at any point. Expect an answer from `flow status`, never "I couldn't confirm".
6. Run `booking-agent runtime doctor`. Expect no findings.

Steps 1 and 4 each wait on something that happens outside the chat: signing in to Link,
and approving the payment in Link. A harness that notices that on its own, with no new
inbound message, and replies right away is doing more than this checklist asks, and that
is worth noting as a plus in the evidence line. It is **not** required here: send a plain
follow-up message ("I signed in", "approved") to prompt the next reply, and the step still
passes once that reply matches what "Expect" says. Requiring the no-nudge behavior is only
for a specific verified harness's own replay, and only once that harness's procedure in
[ops/verified-harnesses.md](ops/verified-harnesses.md) says it proved it; it is never
assumed from this general checklist or from another harness passing it.

This checklist is the **unverified** replay. It is what Cursor, Claude Code, and any other harness without its own replay uses. Passing it moves `deferred` to `recommended` only for those harnesses.

A **verified** harness never inherits that result. Grok Bot and Base44 Superagent each have a replay in [ops/verified-harnesses.md](ops/verified-harnesses.md), including the restart that harness actually supports and whether calendar is connected. Passing one moves `deferred` to `recommended` only for the harnesses that share that replay. Two harnesses share a replay when both names in the registry point at the same replay id, so one successful run promotes both. The only way to recommend a release on a verified harness is to pass its replay and record that replay's rollout. A release that is `withdrawn` for everyone stays withdrawn for verified harnesses too.

Saved session files have their own rule. Adding an optional field keeps `STORE_SCHEMA_VERSION` and `SCHEMA_VERSION` in `session.py` as they are: older builds keep fields they do not know when they save. Raising either number makes every older build refuse the file and ask its user to update, so that is a breaking change, and its release cannot be `available`.

What an installed assistant does with the decision:

| Situation | What the user sees |
|---|---|
| The loaded skill and the installed CLI do not match | Asked before the next booking starts (no network needed) |
| `urgent`, or a ready `breaking` release that is `recommended` | Asked before the next booking starts |
| `recommended` feature or fix | Offered after a booking; offered before one only when the request needs an affected capability; otherwise mentioned only if the assistant checks on the user's request |
| `available` | Nothing |
| `deferred`, or any release after a `deferred` one | Nothing |
| The installed version is `withdrawn` | Asked to move to `replacement_version` |

Nothing is ever installed without the user's yes, never during a booking, and never from a source checkout. A user who says no is not asked about that version again. `BOOKING_AGENT_UPDATE_CHECK=0` turns all of it off.

To roll a deferred release out later, open an `internal` PR that changes only its entry (for example `deferred` to `recommended`, with evidence) and the regenerated index. That ships no code and needs no version bump. Never edit a published GitHub Release body to change the decision: the release's attached copy of its metadata records what was decided at release time.

The PR body's `## Release and rollout` section (in the [pull request template](../.github/pull_request_template.md)) repeats the version, change type, update status, affected capabilities, interoperability impact, and readiness evidence. CI checks that it is filled in and matches the metadata file, that `recommended` and `urgent` have evidence and `ecosystem_ready: true`, and that `urgent` has the release owner's approval. Whether the evidence is good enough is for the admin who merges.

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

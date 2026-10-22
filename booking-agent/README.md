# Booking Agent

`booking-agent` lets agents book time-based services (appointments, lessons, repairs, classes) through the [Universal Scheduling Protocol](https://github.com/wix/universal-scheduling-protocol) (USP), with hard human gates at calendar, slot selection, and payment. Payment uses [stripe/link-cli](https://github.com/stripe/link-cli) for spend-request approval from the user's Link wallet. The v2 agent path supports UCP-native checkout only and fails closed for other merchant checkout profiles.

This directory is the source of the Booking Agent. Changes are made here, on the `master` branch, through pull requests (see [CONTRIBUTING.md](CONTRIBUTING.md)).

- `packages/cli` - `booking-agent-cli`: the `booking-agent` command (`booking_agent_cli`) and its USP/UCP protocol library (`booking_agent`: models, discovery, REST client, platform profile).
- `skills/booking-agent/SKILL.md` - the agent skill. Raw URL:
  https://raw.githubusercontent.com/wix/booking-agent/master/booking-agent/skills/booking-agent/SKILL.md
- `scripts/` - local install, latency soak, and platform profile generator.
- `ops/` - latency budgets and the soak runbook.

The `main` branch of this repository is the GitHub Pages branch for the hosted platform profile at `https://profile.booking-agent.dev/platform-profile.json`. It does not contain this directory.

## Installation

**Prerequisites:** [uv](https://docs.astral.sh/uv/), Python 3.10 or newer, and [`link-cli`](https://github.com/stripe/link-cli) for buyer identity and spend-request approval. The v2 checkout path does not require a browser.

Install the CLI from PyPI:

```bash
uv tool install booking-agent-cli
# or
pipx install booking-agent-cli

booking-agent --help
```

Every release on PyPI carries a PEP 740 attestation signed by this repository's `release.yml` workflow. To check a wheel before installing it:

```bash
uvx pip download "booking-agent-cli==<version>" --no-deps --only-binary :all: -d dist-verify
uvx pypi-attestations verify pypi --repository https://github.com/wix/booking-agent \
  dist-verify/booking_agent_cli-<version>-py3-none-any.whl
uv tool install dist-verify/booking_agent_cli-<version>-py3-none-any.whl
```

`booking-agent update` does this on its own: it installs only a wheel whose attestation verifies. On the first `booking-agent flow init` after an install, the CLI also compares its installed files with the attested wheel and refuses to book or pay (`installation_unverified`) when they differ. Source checkouts are not checked.

Agents provision `booking-agent` from the [skill metadata](skills/booking-agent/SKILL.md) (`kind: uv`, `package: "booking-agent-cli==<version>"`), which pins the release the skill was written for; `scripts/sync_skill_bundle.py` keeps that pin equal to the package version.

To install from a checkout instead (development, or to test unreleased changes):

```bash
git clone https://github.com/wix/booking-agent.git
cd booking-agent/booking-agent
uv sync --frozen --all-packages --all-extras
uv run pytest -q
uv build --package booking-agent-cli
uv tool install ./packages/cli
booking-agent --help
```

For development, `./scripts/install.sh` installs an editable `booking-agent` (source edits take effect without reinstalling) and symlinks the skill to `~/.claude/skills/booking-agent`.

## Quickstart

There is one runtime. It talks to the live registry and real USP businesses with the shipped defaults, and no local server is required.

```bash
# Initialize a session (--user-request is required)
booking-agent flow init --user-request "haircut San Francisco"

# Verify registry reachability
booking-agent flow prereq check

# Discover real businesses from the live registry
booking-agent flow discover --query "haircut San Francisco"

# Select a business and walk through the flow
booking-agent flow business use --profile-url <profile_url from discover>
booking-agent flow calendar ask            # agent asks user; then connect or skip
booking-agent flow calendar skip           # or `calendar connect` to check conflicts
booking-agent flow availability --service-id <id>
booking-agent flow slots list
booking-agent flow slots select --slot-id <id> --user-confirmed

# Buyer identity requires Link wallet sign-in. `flow init` points here first when signed out.
# connect returns the sign-in URL at once. Same turn: start poll in the background, post the URL.
booking-agent flow link-auth connect
booking-agent flow link-auth poll          # background; never block on it before the URL is shown
booking-agent flow buyer resolve

# Book and pay through the UCP-native path selected by `business use`
booking-agent flow book --service-id <id>
# ... create link-cli spend-request from payment_context ...
# Default: pay request returns immediately with approval URL; show it, then poll in foreground
booking-agent flow pay request --payment-method-id <id> --context "<100+ char description>"
# Same turn, after posting the URL to the user:
booking-agent flow pay poll
# Scripts/CI only: add --then-complete to block until checkout finishes in one process.
booking-agent flow validate-skill-compliance
```

Confirm with `booking-agent flow book poll --wait`, which uses repeated `GET /bookings/{id}` calls until a terminal status. The v2 payment path does not start an inbound webhook.

If `init` returns `session_active`, run `booking-agent flow sessions list` and ask the user whether to resume (`sessions resume`) or start fresh (`init --force`).

Each command prints a JSON object with `phase`, `next_command`, and (when waiting on the user) `awaiting_user`. Errors also emit JSON with `data.error.code`. Follow `next_command` to advance, or run `booking-agent flow status` at any point. Session state is stored in `~/.booking-agent/.usp-session.json`.

List available commands:

```bash
booking-agent --help
booking-agent flow --help
```

**Scope today:** the v2 agent slice books only businesses that satisfy the UCP-native paid-bookings gate. It rejects redirect checkout and any payment timing it does not know. Deposits go through checkout with negotiated payment terms, pay-at-appointment services are booked directly or, when the business declares the pay-at-service extension, through a checkout that completes with no payment instrument, and free services skip payment. Discovery sends the user's free-text request and does not add a structured `categories[]` filter. Embedded USP `confirm-payment` and MPP are not supported yet.

## Configuration

Settings come from, highest first: the process environment, `~/.booking-agent/.env`, `./.env`, then the shipped `defaults.env`. Run `booking-agent config show` to see each effective value and its source, and `booking-agent config init` to create `~/.booking-agent/.env`.

The platform identity is `USP_AGENT_PROFILE_URL`, defaulting to the hosted platform profile. `init` refuses a non-`https` value, except plain `http` on a literal loopback address (`127.0.0.1`, `::1`) for local development against a local merchant.

Operators may set `GOOGLE_CALENDAR_*` in `.env` to enable local Google OAuth on the base `booking-agent` path; without those settings the flow skips personal calendar conflict checking. Never commit `.env` or OAuth client secrets.

## The skill

Install the [`booking-agent` skill](skills/booking-agent/SKILL.md) in your agent harness with:

```bash
npx skills add wix/booking-agent --skill booking-agent
```

That takes the latest skill, from the canonical raw URL above. Each release also publishes the
skill at an immutable tag, `skill-v<frontmatter version>`, and signs it with the same workflow that
signs the wheels, so a harness can pin and verify the exact text it follows:

```bash
curl -fsSLO https://raw.githubusercontent.com/wix/booking-agent/skill-v0.57.2/booking-agent/skills/booking-agent/SKILL.md
gh attestation verify SKILL.md \
  --repo wix/booking-agent \
  --signer-workflow wix/booking-agent/.github/workflows/release.yml
```

The same `SKILL.md` and its `SKILL.md.sigstore.json` bundle are attached to every GitHub Release,
for runtimes that verify the bundle offline instead of calling `gh`.

At runtime the CLI checks the file the agent actually loaded: `flow init --skill-path <path>`
hashes it and compares it with the signed copy inside the installed wheel. A file that claims the
bundled version but does not match it fails `skill_unverified`, and `flow book` and `flow pay`
refuse until `booking-agent skill print --output <path>` restores the official text with the
user's consent. A file declaring a different version is a stale copy, which the update flow
offers to replace instead. Skill files resolving into a source checkout are left alone, so
development edits are not blocked.

This check covers a file that was modified after it was installed. It cannot catch a copy written
to evade it, since such a copy can omit `--skill-path` or point it at a genuine file — the first
read is what `gh attestation verify` and the harness cover.

It provisions `booking-agent`, defines the booking contract (gates, stop points, and error recovery), and follows the UCP-native path from `business use`, using [create-payment-credential](https://github.com/stripe/link-cli/blob/main/skills/create-payment-credential/SKILL.md) for spend-request approval.

The same skill runs in every agent runtime. The agent supplies what only it can observe: the Google Calendar route (`flow calendar connect --route connector --token-env <NAME>` when its platform hands it a calendar token, or `--route local-oauth` for a sign-in link), and, when its filesystem resets between conversations, a folder that survives (`booking-agent runtime prepare --persistent-root <dir>`).

Every `booking-agent` command emits JSON. The agent reads `next_command` and `awaiting_user` on each response; the CLI drives the loop. Full contract in [SKILL.md](skills/booking-agent/SKILL.md).

When a booking fails, the agent can offer the user a support report. With the user's consent, the CLI sends a redacted bundle of the session to the Booking Agent support team (`support@booking-agent.dev`); nothing is sent without that consent.

Faults the user never sees are reported automatically, without a prompt: a business whose booking profile cannot be read or validated, a catalog entry that cannot be fetched, a warning from discovery, or the agent calling the CLI out of order. Such a report holds only the step, the exception type, a fixed log format string, the business's public profile host and path and registry IDs, an HTTP status or validation reason, the CLI and skill versions, and the OS family. It never holds the user's request, contact details, address, Link, payment, or calendar data, or any exception or log text. Each report is sent once a day at most, after the command has printed its output. Set `BOOKING_AGENT_FAULT_REPORTS=0` to turn this off; `booking-agent config show` shows the current value.

## Tests

Headless tests live under `packages/cli/tests/` and run with pytest only. They skip human gates and are not a demonstration of the agent flow in [SKILL.md](skills/booking-agent/SKILL.md).

```bash
# Hermetic UCP-native E2E (mocks; registry to complete_checkout)
uv run pytest packages/cli/tests/e2e/test_ucp_native_e2e.py -q

# SKILL flow over a loopback HTTP stack started by the test (booking-agent flow *; no webhook)
uv run pytest packages/cli/tests/e2e/test_ucp_skill_flow_e2e.py -q

# Full suite
uv run pytest -q
```

Optional live registry check: `BOOKING_AGENT_STAGING_REGISTRY_TEST=1 uv run pytest packages/cli/tests/e2e/test_staging_registry_integration.py -q`. [`packages/cli/tests/.env.tests`](packages/cli/tests/.env.tests) lists every test-only switch.

Per-step latency budgets and the soak harness: [`ops/latency.md`](ops/latency.md), [`ops/budgets.md`](ops/budgets.md), and [`scripts/latency_soak.py`](scripts/latency_soak.py).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Version numbers, release notes, and when an installed assistant offers a user an update are covered under [Releasing](CONTRIBUTING.md#releasing). What changed in each version is on the [releases page](https://github.com/wix/booking-agent/releases).

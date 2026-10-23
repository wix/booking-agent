# booking-agent-cli

The `booking-agent` command books time-based services (haircuts, lessons,
repairs, classes, appointments) through the Universal Scheduling Protocol (USP),
paying from a Link wallet. It owns the booking state machine and refuses to
proceed when a human approval gate is skipped, so an AI agent can drive it safely.

The wheel ships two import packages:

- `booking_agent_cli` - the `booking-agent` command
- `booking_agent` - the USP/UCP protocol library (models, discovery, REST client)

## Install

Requires Python 3.10 or newer.

```bash
uv tool install booking-agent-cli
# or
pipx install booking-agent-cli

booking-agent --help
```

Every release carries a PEP 740 attestation from the `wix/booking-agent` release
workflow. Check a downloaded wheel with
`uvx pypi-attestations verify pypi --repository https://github.com/wix/booking-agent <wheel>`.
`booking-agent update` installs only verified wheels, and the CLI checks its own installed
files against the attested release before it books or pays.

On a verified agent harness (Grok Bot, Base44 Superagent), follow that harness's install
guide instead: it pins the CLI and skill pair that passed the harness's own booking replay.
Guides: https://github.com/wix/booking-agent/blob/master/booking-agent/docs/harnesses/README.md

Agents use the CLI through the Booking Agent skill, installed with
`npx skills add wix/booking-agent --skill booking-agent`, or taken from the canonical raw URL:
https://raw.githubusercontent.com/wix/booking-agent/master/booking-agent/skills/booking-agent/SKILL.md

Each release also publishes that skill at an immutable `skill-v<version>` tag and signs it with
the release workflow, so a harness can pin and verify the exact text it follows:

```bash
curl -fsSLO https://raw.githubusercontent.com/wix/booking-agent/skill-v0.57.4/booking-agent/skills/booking-agent/SKILL.md
gh attestation verify SKILL.md --repo wix/booking-agent \
  --signer-workflow wix/booking-agent/.github/workflows/release.yml
```

`flow init --skill-path <path>` then checks the file the agent actually loaded against the signed
copy inside the wheel; a modified file claiming the same version fails `skill_unverified` and
blocks booking and payment until `booking-agent skill print --output <path>` restores it. A copy
written to evade the check can omit `--skill-path`, so the first read is what the attestation and
the harness cover.

The skill also needs `link-cli` for buyer identity and payment approval; see the
skill for details.

## Source and license

Source: https://github.com/wix/booking-agent (directory `booking-agent/`).
Licensed under the Apache License 2.0; see
https://github.com/wix/booking-agent/blob/master/LICENSE.

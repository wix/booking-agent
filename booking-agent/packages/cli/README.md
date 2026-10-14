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

Agents use the CLI through the Booking Agent skill:
https://raw.githubusercontent.com/wix/booking-agent/master/booking-agent/skills/booking-agent/SKILL.md

The skill also needs `link-cli` for buyer identity and payment approval; see the
skill for details.

## Source and license

Source: https://github.com/wix/booking-agent (directory `booking-agent/`).
Licensed under the Apache License 2.0; see
https://github.com/wix/booking-agent/blob/master/LICENSE.

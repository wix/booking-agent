# Booking Agent

This directory is the public source of the Booking Agent: a CLI and agent skill
that book time-based services through the Universal Scheduling Protocol (USP),
paying from a Link wallet.

- `packages/usp-core` - `booking-agent-core`: USP/UCP models, discovery, REST client.
- `packages/cli` - `booking-agent-cli`: the `booking-agent` command.
- `skills/booking-agent/SKILL.md` - the agent skill. Raw URL:
  https://raw.githubusercontent.com/wix/booking-agent/master/booking-agent/skills/booking-agent/SKILL.md

The contents of this directory are generated from the private canonical monorepo
and published through reviewed sync pull requests into `master`. Pull requests
that edit `booking-agent/` directly are closed.

The `main` branch of this repository is the GitHub Pages branch for the hosted
platform profile at `https://profile.booking-agent.dev/platform-profile.json`.
It does not contain this directory.

## Build and install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.10 or newer.

```bash
git clone https://github.com/wix/booking-agent.git
cd booking-agent/booking-agent
uv sync --all-packages --all-extras
uv run pytest -q
uv build --all-packages
uv tool install ./packages/cli --with ./packages/usp-core
booking-agent --help
```

The skill also needs `link-cli` for buyer identity and payment approval; see
the skill for details.

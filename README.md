# booking-agent

Home of the Booking Agent and its shared, keyless [USP](https://usp-protocol.dev/)/[UCP](https://ucp.dev/) platform profile.

`wix/booking-agent` is the canonical repository for both. The Booking Agent source (CLI, core library, and agent skill) is in [`booking-agent/`](booking-agent/), and pull requests against `master` are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Branches

- `master` (default) holds the CLI, library, skill, and tests under `booking-agent/`, plus a copy of the platform profile files. Development targets `master`.
- `main` is the GitHub Pages branch. It holds only the profile, its vendored schemas, validators, and Pages config. It never contains `booking-agent/`.
- A profile change lands on `main` first, then the same bytes land on `master`.

## Installation

See the [Booking Agent README](booking-agent/README.md). Install from a checkout of this repository with `uv`.

## Platform profile

Businesses fetch the profile at:

`https://profile.booking-agent.dev/platform-profile.json`

The document is keyless: it carries no signing keys. One profile is used for the whole platform. It is generated from `platform_profile_doc()` in [`booking-agent/packages/cli/src/booking_agent/platform_profile.py`](booking-agent/packages/cli/src/booking_agent/platform_profile.py) into the committed `platform-profile.json`, which GitHub Pages serves (HTTPS) from `main`.

## Validate locally

Product (from `booking-agent/`):

```bash
cd booking-agent
uv sync --frozen --all-packages --all-extras
uv run pytest -q
uv build --package booking-agent-cli
```

Profile (from the repository root):

```bash
python3 tools/build.py
python3 -m pytest -q tests
(cd booking-agent && uv run python scripts/generate_platform_profile.py --check)
```

`tools/build.py` and `tests/` confirm the committed JSON matches the vendored USP/UCP schemas and stays keyless. The generator's `--check` confirms the committed JSON matches `platform_profile_doc()`; run it without `--check` to regenerate after changing the function.

## Support

Questions about the Booking Agent: `support@booking-agent.dev`.

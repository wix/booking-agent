# booking-agent

Public home of the Booking Agent and its shared, keyless USP/UCP platform profile.

The Booking Agent source (CLI, core library, and agent skill) is in [`booking-agent/`](booking-agent/). That directory is generated from the private canonical monorepo and arrives only through reviewed sync pull requests; see [CONTRIBUTING.md](CONTRIBUTING.md). The `main` branch is the GitHub Pages branch for the profile and does not contain `booking-agent/`.

Businesses fetch the profile at:

`https://profile.booking-agent.dev/platform-profile.json`

The document is keyless: it carries no signing keys. One profile is used for the whole platform.

This repository publishes the committed `platform-profile.json` artifact at the URL above (GitHub Pages, HTTPS).

## Validate locally

If you clone this repository:

```bash
python3 tools/build.py
python3 -m pytest -q
```

Those checks confirm the committed JSON matches the vendored USP/UCP schemas and stays keyless. They do not own the document content.

# booking-agent

Shared, keyless USP/UCP platform profile for the Booking Agent. This `main` branch is the GitHub Pages branch and holds only the profile. The Booking Agent source, including the profile generator, lives on the default branch, `master`, under `booking-agent/`. Development and pull requests target `master`.

Businesses fetch the profile at:

`https://profile.booking-agent.dev/platform-profile.json`

The document is keyless: it carries no signing keys. One profile is used for the whole platform.

This repository publishes the committed `platform-profile.json` artifact at the URL above (GitHub Pages, HTTPS). The artifact is generated from `platform_profile_doc()` in `booking-agent/packages/cli/src/booking_agent/platform_profile.py` on `master`. A profile change lands here first, then the same bytes land on `master`; see `CONTRIBUTING.md` on `master`.

## Validate locally

If you clone this repository:

```bash
python3 tools/build.py
python3 -m pytest -q
```

Those checks confirm the committed JSON matches the vendored USP/UCP schemas and stays keyless. They do not own the document content.

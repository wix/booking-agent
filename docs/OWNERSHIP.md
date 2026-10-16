# Booking Agent platform profile - ownership

Canonical profile URI (platform identity; do not change lightly):

`https://profile.booking-agent.dev/platform-profile.json`

| Asset | Owner |
|-------|-------|
| Domain `booking-agent.dev` | Wix DNS / domain registrant for `booking-agent.dev` |
| GitHub repo `booking-agent` | Public org [`wix`](https://github.com/wix/booking-agent) (renamed from `booking-agent-profile`; the old URL redirects) |
| GitHub Pages publisher | `wix/booking-agent` serving the canonical URI (source: `main` / root) |
| Profile document content | Authored in `platform_profile_doc()` (`booking-agent/packages/cli/src/booking_agent/platform_profile.py` on `master`) and generated into the committed `platform-profile.json` by `booking-agent/scripts/generate_platform_profile.py`. This repo is both the source and the host. |

## Branches

- `main` is the only GitHub Pages branch. It contains only the profile, its schemas, validators, and these docs. The `validate` check rejects any other path and any `CNAME` other than `profile.booking-agent.dev`.
- `master` is the default and development branch. It carries the same profile files plus the Booking Agent source under `booking-agent/`.
- Profile updates enter through a pull request into `main`, followed by a pull request that carries the same bytes into `master`. Do not merge `master` into `main` while `booking-agent/` exists, and do not reset or fast-forward `master` to `main`.
- Changes under `booking-agent/` arrive through pull requests into `master`. Only repository and `wix` org admins can merge into `master` or `main`.

## Who can transfer what

- **Domain transfer / registrar changes:** domain registrant for `booking-agent.dev` (Wix Domains).
- **Repo admin / transfer:** `wix` org owners (this repo). Do not recreate; prefer history-preserving moves.
- **DNS for the profile host:** whoever can edit Wix DNS for `booking-agent.dev`. Apex and `www` records serve the live Wix site and must not be used for this profile.
- **GitHub org domain verification:** `wix` org owners, via org Settings -> Pages -> Verified domains.

## Rotation of neither

This document is keyless. There are no profile signing keys to rotate. Proof-of-possession keys are per-credential and ephemeral in the client; they are never published here.

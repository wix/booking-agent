# Booking Agent platform profile - ownership

Canonical profile URI (platform identity; do not change lightly):

`https://profile.booking-agent.dev/platform-profile.json`

| Asset | Owner |
|-------|-------|
| Domain `booking-agent.dev` | Wix DNS / domain registrant for `booking-agent.dev` |
| GitHub repo `booking-agent` | Public org [`wix`](https://github.com/wix/booking-agent) (renamed from `booking-agent-profile`; the old URL redirects) |
| GitHub Pages publisher | `wix/booking-agent` serving the canonical URI (source: `main` / root) |
| Profile document content | Generated in the private canonical Booking Agent monorepo and published as the committed `platform-profile.json`. This repo validates and hosts; it is not a second authoring source. |

## Branches

- `main` is the only GitHub Pages branch. It contains only the profile, its schemas, validators, and these docs. The `validate` check rejects any other path and any `CNAME` other than `profile.booking-agent.dev`.
- `master` is the default branch. It carries the same profile files plus the generated Booking Agent mirror under `booking-agent/`.
- Profile updates enter through a pull request into `main`, followed by a pull request that copies the same bytes into `master`. Do not merge `master` into `main` while `booking-agent/` exists, and do not reset or fast-forward `master` to `main`.
- Changes under `booking-agent/` arrive only through generated sync pull requests from the private canonical monorepo.

## Who can transfer what

- **Domain transfer / registrar changes:** domain registrant for `booking-agent.dev` (Wix Domains).
- **Repo admin / transfer:** `wix` org owners (this repo). Do not recreate; prefer history-preserving moves.
- **DNS for the profile host:** whoever can edit Wix DNS for `booking-agent.dev`. Apex and `www` records serve the live Wix site and must not be used for this profile.
- **GitHub org domain verification:** `wix` org owners, via org Settings -> Pages -> Verified domains.

## Rotation of neither

This document is keyless. There are no profile signing keys to rotate. Proof-of-possession keys are per-credential and ephemeral in the client; they are never published here.

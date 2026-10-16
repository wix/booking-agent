# AGENTS.md

## Change Log Policy

After completing any set of file changes in this repository, you
MUST append a new entry to the root-level `CHANGE_LOG.md` file before finishing your response.

### Entry format

Each entry is a level-2 heading followed by a bulleted list:

    ## DD/MM/YY at HH:MM:SS by [<git_username>](mailto:<git_email>)

    - <change description>
    - <change description>

Where:
- `DD/MM/YY` is the current date (day/month/year, zero-padded)
- `HH:MM:SS` is the current time in the user's local timezone (24-hour, zero-padded)
- `<git_username>` is the output of `git config user.name`
- `<git_email>` is the output of `git config user.email`. If unavailable, use the plain username without a mailto link
- Each bullet describes one logical change with its **motivation or rationale** (e.g. "Fixed the bug where X caused Y", "Added missing Z required by W"), not just what was changed

### Rules

1. Append new entries to the **start** of the file (newest first)
2. Separate each entry from the previous one with a blank line and a horizontal rule (`---`)
3. If you make changes across multiple files, group related changes into a single entry
4. Do NOT edit or remove existing entries
5. Obtain the current date, time, and git username at the time you write the entry - do not guess or reuse stale values
6. New bullets MUST NOT cite GitHub issues or pull requests (`#123`, `owner/repo#123`, or issue/PR URLs)
7. `CHANGE_LOG.md` lives on `master` only. Never add it to a pull request into `main` (the Pages allow-list rejects it)

## This repository

`wix/booking-agent` is both the source and the host:

- `booking-agent/` on `master` is the Booking Agent source (CLI, library, skill, tests, scripts, ops docs). Work from that directory; see `booking-agent/CONTRIBUTING.md`.
- The repository root holds the hosted platform profile (`platform-profile.json`), its vendored schemas (`schemas/`), validators (`tools/build.py`, `tests/`), and Pages config (`CNAME`).

`CLAUDE.md` is a symlink to this file.

## Branches

- `master` is the default and development branch. It holds `booking-agent/` plus the same profile files as `main`.
- `main` is the GitHub Pages branch for `https://profile.booking-agent.dev/platform-profile.json`. It holds only the profile, vendored schemas, validators, and docs, and MUST NOT contain `booking-agent/`. Its `validate` check rejects any path outside the allow-list in `.github/workflows/publish.yml` and any `CNAME` other than `profile.booking-agent.dev`.
- Never merge `master` into `main`: it would bring `booking-agent/` and fail `validate`.
- Never reset or fast-forward `master` to `main`: it would delete `booking-agent/`.
- Only repository and `wix` org admins can merge into `master` or `main` (ruleset "Admin-only merges (main, master)"). Open the PR and leave the merge to an admin unless you were asked to merge it. If a merge is denied (permissions, failing check, branch not up to date), leave the PR open, report the URL and error, and stop. Do not use `--admin`, force-push, or push directly to `main` or `master`.
- Do not change the default branch, delete `master`, or retarget Pages.

## Platform profile

| Role | Location |
|------|----------|
| Single source of truth | `platform_profile_doc()` in `booking-agent/packages/cli/src/booking_agent/platform_profile.py` |
| Generated artifact | `platform-profile.json` at the repository root (`json.dumps(..., indent=2, sort_keys=True)` plus a trailing newline) |
| Generator / no-drift check | From `booking-agent/`: `uv run python scripts/generate_platform_profile.py`, and the same command with `--check` |
| CI no-drift gate | `.github/workflows/ci.yml` step `No-drift platform profile artifact` |
| Hosting validation | From the root: `python3 tools/build.py` and `python3 -m pytest -q tests` (keyless, REST-only, namespaces, vendored USP/UCP schemas) |
| Parity monitor | `.github/workflows/profile-monitor.yml` on `master`: its `profile-parity` job fails a `master` PR whose `platform-profile.json` is not yet on `main` |
| Canonical identity URI | `https://profile.booking-agent.dev/platform-profile.json` (`DEFAULT_PLATFORM_PROFILE_URL`) |

The only runtime override is `USP_AGENT_PROFILE_URL`. Loopback is for development; production businesses must reject RFC 6890 special-use addresses.

### Invariants the document must keep

- Keyless: no `keys`, `signing_keys`, or `webhook_url`. Proof-of-possession keys stay ephemeral in the client.
- REST-only USP service bindings (`transport: rest`). No MCP, A2A, or webhook fields.
- Current namespaces only: `dev.usp-protocol.*` and `https://usp-protocol.dev`. Never reintroduce `dev.usp.services` or `https://usp.dev`.
- USP capability entries need `version`, `spec`, and `schema`. USP `spec`/`schema` origins are `https://usp-protocol.dev`. UCP-owned entries use `https://ucp.dev`.
- UCP capability negotiation (`intersect_ucp_capabilities()`) requires an **exact version overlap**. If the hosted `dev.usp-protocol.services.paid_bookings` version does not match live merchants, that capability is silently dropped.

### Update sequence (a pair of PRs in this repo)

1. **Author on a branch off `master`.** Change `platform_profile_doc()` only, plus its tests. Do not hand-edit `platform-profile.json`, and do not produce it with any other serializer.
2. **Regenerate and prove no drift** (from `booking-agent/`):
   ```bash
   uv run python scripts/generate_platform_profile.py
   uv run python scripts/generate_platform_profile.py --check
   uv run pytest packages/cli/tests/integration/test_platform_profile.py -q
   ```
3. **Open the `main` PR first.** Branch off `origin/main` and copy only the regenerated bytes: `git checkout <your-master-branch> -- platform-profile.json`. Nothing else goes in this PR. Once `validate` passes and an admin merges it, wait for Pages and diff the live body:
   ```bash
   curl -fsS 'https://profile.booking-agent.dev/platform-profile.json' \
     | diff -u - platform-profile.json
   ```
   Empty diff means the published document matches the generator.
4. **Then the `master` PR** with the function change, tests, and the same `platform-profile.json` bytes. Re-run its `profile-parity` check if it ran before step 3; it fails until `main` has the same bytes.

### What not to do

- Do not edit `platform-profile.json` on either branch by hand.
- Do not land a profile change only on `master`. Pages serves `main`.
- Do not add signing keys or webhooks to the hosted profile.
- Do not use the `booking-agent.dev` apex or `www` for this profile (those serve the Wix site). The profile host is `profile.booking-agent.dev` only.

## Public repository hygiene

Everything here is public. Do not commit `.env` files, credentials, OAuth client secrets, tokens, or buyer data. Do not cite private repositories or their issue numbers in code, docs, or CHANGE_LOG bullets. The `booking-agent` skill MUST carry `license: Complete terms at https://github.com/wix/booking-agent/blob/master/LICENSE`, and the Apache-2.0 `LICENSE` stays at the repository root.

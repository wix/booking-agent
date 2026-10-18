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

## Releasing to PyPI

Merging a pull request into `master` that raises `version` in `booking-agent/packages/cli/pyproject.toml` publishes that version of `booking-agent-cli` to PyPI automatically. The admin merge is the only human step: there is no release to cut and no deployment to approve.

- `.github/workflows/release.yml` runs on every push to `master`. If tag `v<version>` does not exist and the version is not on PyPI, it runs the tests, builds, publishes with Trusted Publishing (environment `pypi`), then creates tag `v<version>` and a GitHub Release with generated notes on the merged commit. Otherwise it does nothing.
- Every pull request that changes shipped code must raise the version above the version on `master`. Shipped code is what the wheel contains: `booking-agent/packages/cli/src/booking_agent/`, `booking-agent/packages/cli/src/booking_agent_cli/` (including `defaults.env`), and the distribution metadata in `booking-agent/packages/cli/pyproject.toml` (dependencies, entry points, build config). Docs, CI, tests, scripts, the skill, and `uv.lock`-only changes need no bump. The `version` job in `ci.yml` enforces this with `booking-agent/scripts/check_version.py`.
- **The versioning, release-note, and rollout policy is "Releasing" in `booking-agent/CONTRIBUTING.md`.** Read it before opening any pull request; it is the one copy humans and agents both follow. Do not move to `1.0.0` on your own; ask the release owner.
- A bump changes four places that must agree (the `version` job checks this on every run): `version` in `booking-agent/packages/cli/pyproject.toml`, `__version__` in `booking_agent/__init__.py` and in `booking_agent_cli/__init__.py`, and the `booking-agent-cli` entry in `booking-agent/uv.lock` (run `uv lock` from `booking-agent/`).
- PyPI versions are permanent. A published version can never be re-uploaded or replaced, even after deletion, so a broken release is fixed only by merging a new, higher version.
- **One PyPI version, one pull request.** Each merged bump becomes its own permanent PyPI release. If you have several pieces of work that should ship as one version, combine them into a single pull request with one version bump and merge that. Do not merge several pull requests that each bump the version, and do not merge un-bumped shipped changes (CI blocks them) planning to bump later.
- `.devN` versions are never published from `master`. To test a build on TestPyPI, set a `.devN` version and publish a GitHub Release whose tag is `v<version>` on a commit already on `master`; `release.yml` sends it to TestPyPI (environment `testpypi`). A manually published non-`.devN` release still goes to PyPI if that version is not there yet.
- `.github/workflows/release-watch.yml` runs daily. If the version on `master` is not on PyPI or has no `v<version>` tag (a release run failed), it fails and opens or updates an issue assigned to the release owner. Re-run the failed `release.yml` run, or merge a new version.

### When you open a pull request

`release-pr.yml` enforces most of this, but the judgment is yours:

1. **Label it.** Exactly one of `fix`, `feature`, `breaking` (the PR raises the version) or `internal` (it does not).
2. **Title it for users when it raises the version.** The title is published unchanged as that version's GitHub Release note. Say what users get ("Recover safely when payment confirmation times out"), never the mechanics ("Bump to 0.2.0", "Update flow.py", `feat:`, an issue number, a version number). Several changes in one version: name the largest user-visible outcome; put the rest in the body and `CHANGE_LOG.md`.
3. **Add `booking-agent/release-notes/<version>.json`** and run `uv run python scripts/release_notes.py` from `booking-agent/` to regenerate `index.json`.
4. **Decide the rollout with the decision order in "Rollout: should users be told?"**, not from the version number or green CI. Default to `update_status: deferred` and `ecosystem_ready: false`. Set `ecosystem_ready: true` only with concrete evidence that existing businesses, the registry in use today, and saved session files work with the release, and write that evidence in `readiness_evidence` and the PR body. A change to sign-in, calendar, or payment also needs a `Hosted replay:` evidence line (see "Hosted replay" in `booking-agent/CONTRIBUTING.md`); `release_notes.py --check` fails without it. Never set `urgent` without the release owner's written approval in the PR (`Release-owner approval: <who, why>`). If you are unsure, leave it `deferred` and say so in the PR; rolling out later is a small `internal` PR.
5. **Fill in `## Release and rollout`** in the PR body (see `.github/pull_request_template.md`) with the same values as the metadata file.
6. **Name the version in `CHANGE_LOG.md`** (`Released as <version>`).

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
4. **Then the `master` PR** with the function change, tests, and the same `platform-profile.json` bytes. Re-run its `profile-parity` check if it ran before step 3; it fails until `main` has the same bytes. `platform_profile.py` ships in the wheel, so this PR must also raise the `booking-agent-cli` version (see "Releasing to PyPI"), and merging it publishes that version. The `main` PR needs no version change; releases never touch `main`.

### What not to do

- Do not edit `platform-profile.json` on either branch by hand.
- Do not land a profile change only on `master`. Pages serves `main`.
- Do not add signing keys or webhooks to the hosted profile.
- Do not use the `booking-agent.dev` apex or `www` for this profile (those serve the Wix site). The profile host is `profile.booking-agent.dev` only.

## Public repository hygiene

Everything here is public. Do not commit `.env` files, credentials, OAuth client secrets, tokens, or buyer data. Do not cite private repositories or their issue numbers in code, docs, or CHANGE_LOG bullets. The `booking-agent` skill MUST carry `license: Complete terms at https://github.com/wix/booking-agent/blob/master/LICENSE`, and the Apache-2.0 `LICENSE` stays at the repository root.

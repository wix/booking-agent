# Change Log

## 17/10/26 at 12:13:16 by [Ran Yahalom](mailto:ranya@wix.com)

- The release workflow's artifact count now ignores `dist/.gitignore`, because `uv build` writes that file next to the wheel and sdist and the count treated it as a third artifact. The 0.2.0 build produced the right files and then stopped before publishing, so PyPI still has only 0.1.0

---

## 16/10/26 at 17:06:18 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.2.0 (skill 0.48.0). The booking assistant can now tell the user when a newer version is ready and install it only after they agree, because until now an assistant could run instructions that no longer matched its installed CLI, and users had no way to learn about fixes. The installed CLI makes the decision from the release metadata (`booking-agent update check`), so a patch never interrupts anyone and a release the registry or businesses are not ready for stays quiet even though it is on PyPI. A mismatch between the loaded skill and the CLI is caught at `flow init` with no network
- Added `booking-agent update --user-confirmed`, which installs the version the check selected (never past a `deferred` release), refuses while a booking is unfinished, leaves source checkouts alone, prints the install commands instead when the CLI was not installed with uv or pipx, and confirms from a fresh process that the new version starts. `booking-agent update decline` records a no, so the user is not asked about that version again. `BOOKING_AGENT_UPDATE_CHECK=0` turns checks off, and a failed fetch never blocks a booking
- The wheel now ships a copy of the skill (`booking-agent skill print`, `skill version`), kept equal to `skills/booking-agent/SKILL.md` by `scripts/sync_skill_bundle.py --check` in CI, so after an update the assistant can load the instructions that match the new CLI without network access. `SKILL_VERSION` is now read from that copy instead of a second constant
- A booking-agent that finds saved bookings written by a newer version now refuses them with `session_store_too_new` instead of crashing or rewriting them, and keeps fields it does not know (including inside the business, buyer, booking, and payment records) when it saves, so moving between versions cannot silently drop a booking credential or a captured-payment marker
- Added the PyPI `Changelog` link to the GitHub releases page, and documented in "Releasing" that a skill change is shipped code and how each rollout status reaches users

---

## 16/10/26 at 16:49:18 by [Ran Yahalom](mailto:ranya@wix.com)

- Wrote the whole release policy into "Releasing" in `booking-agent/CONTRIBUTING.md` (version number, PR title and label, rollout decision), and pointed `AGENTS.md`, the root `CONTRIBUTING.md`, and `booking-agent/README.md` at it, because the policy was split between files and the parts about release notes and rollout were not written down anywhere humans would look. `AGENTS.md` keeps a short checklist for agents opening a PR instead of a second copy of the rules
- Added `booking-agent/release-notes/<version>.json` (with a `0.1.0` entry) and the generated `index.json`, plus `scripts/release_notes.py` and its tests, because a version number cannot say whether a release is a feature or a breaking change below `1.0.0`, or whether businesses and the registry are ready for it. The metadata records the change type, the rollout status (`available`, `recommended`, `urgent`, `deferred`, `withdrawn`), readiness evidence, and a one-sentence summary that an installed assistant can later use to decide whether to tell its user about an update. The CI `version` job now checks that every entry is valid, that the change type fits the semantic-versioning step, that non-deferred releases have evidence, and that the current version has an entry
- Added `release-pr.yml`, which checks on every PR edit that the PR has exactly one release label (`fix`, `feature`, `breaking`, or `internal`), that a version-raising PR's title describes the change for users, and that its `Release and rollout` section is filled in and matches the metadata, because `--generate-notes` publishes PR titles unchanged as the release notes. Added a pull request template with that section, defaulting to `deferred`
- Added `.github/release.yml` so generated release notes are grouped into breaking changes, features, and fixes and leave out `internal` PRs, and `release.yml` now attaches the version's release metadata to its GitHub Release so the decision made at release time stays on record

---

## 16/10/26 at 15:59:53 by [Ran Yahalom](mailto:ranya@wix.com)

- Documented a semantic versioning policy in `AGENTS.md` and `booking-agent/CONTRIBUTING.md` (patch for fixes, minor for features and, below `1.0.0`, for breaking changes, major for breaking changes from `1.0.0` on, and `1.0.0` itself left to the release owner), because the bump rule only said "raise the version" and every merged bump becomes a permanent PyPI release, so authors and agents need the same rule for picking the number
- `scripts/check_version.py` now rejects a changed version that is not `MAJOR.MINOR.PATCH` (optionally `.devN`) exactly one step after the base (next patch, next minor, or next major), so a typo such as `0.1.0` to `0.3.0` cannot publish a version that skips numbers; moving forward within one release (`0.2.0.dev1` to `0.2.0`) still passes

---

## 16/10/26 at 15:13:54 by [Ran Yahalom](mailto:ranya@wix.com)

- `release.yml` now also runs on every push to `master`, so merging a PR that raises the `booking-agent-cli` version publishes it to PyPI without anyone cutting a GitHub Release. A new `decide` job skips the run when tag `v<version>` exists or the version is already on the target index, which also guards against double publishing; after a successful PyPI upload a `github-release` job (the only job with `contents: write`) creates the tag and a GitHub Release with generated notes, since a release made with `GITHUB_TOKEN` cannot trigger another workflow. Publishing stays in `release.yml` because the PyPI Trusted Publisher is bound to that file and the `pypi` environment
- Kept the manual `release: published` path with its master-ancestry and tag-equals-version checks, and `.devN` versions still go to TestPyPI only through that path; a push to `master` never publishes a `.devN` version. The concurrency group is now per index, so two quick merges cannot race to publish
- Added a `version` job to `ci.yml` and `booking-agent/scripts/check_version.py` (with unit tests): every run checks that the pyproject version, both `__version__` strings, and `uv.lock` agree, and pull requests that change shipped code (the wheel packages from the hatch config, or distribution metadata in `pyproject.toml`) fail unless they raise the version above the base branch, so a release is never forgotten while docs, CI, and test-only PRs pass without a bump
- Added `release-watch.yml`, a daily safety net that fails and opens or updates an issue assigned to the release owner when the version on `master` is not on PyPI or has no `v<version>` tag, so a failed release run cannot go unnoticed; it closes the issue once the release exists
- Replaced `ls | wc -l` with `find` in the dist check so `actionlint` reports no shellcheck findings
- Documented the automatic release flow in `AGENTS.md` and both `CONTRIBUTING.md` files, including that PyPI versions are permanent, that work meant to ship as one version must be combined into a single PR with one bump, and that the `master` half of a profile change must bump the version because `platform_profile.py` ships in the wheel

---

## 16/10/26 at 14:37:47 by [Ran Yahalom](mailto:ranya@wix.com)

- Restored the change log entries that the squash merge of the version bump dropped from `master`, which had left `CHANGE_LOG.md` with only its heading, so the record of earlier changes is complete again

---

## 16/10/26 at 14:03:20 by [Ran Yahalom](mailto:ranya@wix.com)

- The `booking-agent` skill (0.47.0) now carries `metadata.openclaw.install` with `kind: python` and `package: booking-agent-cli`, and its "Installing" section gives the `uv tool install booking-agent-cli` / `pipx install booking-agent-cli` commands directly, so harnesses and agents can provision the CLI from PyPI instead of only being pointed at a checkout install in the README
- `booking-agent/README.md` "Installation" now leads with the PyPI install and keeps the checkout install for development and unreleased changes
- Bumped the support report `SKILL_VERSION` constant to match the skill front matter

---

## 16/10/26 at 14:01:44 by [Ran Yahalom](mailto:ranya@wix.com)

- Bumped `booking-agent-cli` to `0.1.0` for the first production PyPI release, after a paid booking passed end to end on the editable `scripts/install.sh` install. The release tag must be `v0.1.0`, because `release.yml` refuses tags that do not equal `v` plus this version
- Bumped `__version__` in `booking_agent` and `booking_agent_cli` to match, since the CLI sends it in its user agent and in support reports, and refreshed `uv.lock` (only the `booking-agent-cli` version line changes) so frozen CI keeps passing

---

## 16/10/26 at 14:00:12 by [Ran Yahalom](mailto:ranya@wix.com)

- `release.yml` now publishes non-`.devN` tags to production PyPI: added a `publish-pypi` job (`environment: pypi`, `id-token: write` on that job only, `uv publish --trusted-publishing always`, no `twine` and no token secret). The protected `pypi` environment (required reviewer, `v*` tags only, no admin bypass) now exists, so the guard that failed every production tag in the build job is removed
- Fixed the `release.yml` header comment, which still described building "from the mirrored booking-agent/ tree" after development moved into this repository

---

## 16/10/26 at 13:33:51 by [Ran Yahalom](mailto:ranya@wix.com)

- Added a test that builds the `booking-agent-cli` wheel and fails on any file outside the two product packages, their `.py` modules, `defaults.env`, and the package metadata, so a packaging mistake cannot ship tests, mocks, or local files to PyPI; `release.yml` runs the suite before building, so the check also gates every release

---

## 16/10/26 at 13:05:24 by [Ran Yahalom](mailto:ranya@wix.com)

- Corrected the contributor docs and `AGENTS.md` to say only repository and `wix` org admins can merge into `master` or `main`, because the admin-only rule now covers just those two branches and contributors can again push their own branches

---

## 16/10/26 at 09:42:48 by [Ran Yahalom](mailto:ranya@wix.com)

- Made `wix/booking-agent` the canonical development repository instead of a generated mirror, so contributors work and open pull requests here directly and no sync step is needed
- Added `uv.lock` and the CI and weekly latency soak workflows (SHA-pinned GitHub actions, uv installed with pip, run from `booking-agent/`) so every pull request is tested against the same locked dependencies, including a profile job that runs the hosting validators
- Moved the platform profile generator into `booking-agent/scripts/` writing the root `platform-profile.json`, and pointed CI at its no-drift check, because profile changes are now authored in this repository
- Added the end-to-end tests and the loopback HTTP stack they start, the platform profile and support report tests, and the latency soak harness, so the UCP-native booking path, profile artifact, and latency budgets stay covered without any operator demo tooling
- Added `packages/cli/tests/.env.tests` listing only the test-only switches, and made the config test require it, so it is clear those variables exist only for pytest
- Added the latency budgets and runbook under `booking-agent/ops/`, with internal tracking references and stale measurement notes removed
- Rewrote the root and `booking-agent/` README and CONTRIBUTING files for in-repo development (checkout install, live quickstart, configuration precedence, tests, profile pull-request pair) and removed the "generated, hand edits are closed" wording
- Updated `docs/OWNERSHIP.md` and `schemas/PIN.md` to name this repository as the profile source
- Added `AGENTS.md` (with the `CLAUDE.md` symlink), assistant ignore files, and the JetBrains ignore file, and extended `.gitignore` for secrets, build outputs, and runtime state

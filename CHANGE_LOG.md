# Change Log

## 18/10/26 at 17:26:45 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.4.2 (skill 0.52.0). Reordered the skill's `## Installing` section so it leads with `npx skills add wix/booking-agent --skill booking-agent`, the same command the README leads with. An agent that was handed only the raw `SKILL.md` URL installed from it top to bottom, hit the manual wheel-verification block first, and never reached the one-command installer further down; it copied the file by hand and installed the CLI separately. The content was already there, so this is ordering and emphasis, not new instructions
- Split the section into `This skill file`, `The booking-agent CLI`, `link-cli` and `After installing`, each ending in a gate the installer has to pass before going on (`Do not follow the file until that verify step passes`, `Do not go on until booking-agent --help runs`). The verify steps were inside code fences before, which reads as an example rather than a prerequisite, and the same agent skipped the PEP 740 wheel check for exactly that reason
- Moved `runtime prepare --persistent-root` out of mid-section prose into `After installing`, and said plainly that skipping it on a runtime that needs it loses saved bookings, calendar preferences and the sign-in silently, with no error. The same agent missed it at install time and only prepared the persistent root after a later audit caught it
- Told a runtime without `gh` what to do instead of `verify the bundle offline`, which named no command: install the CLI first, then pass `--skill-path` on every `flow init` and let the CLI compare the loaded file with the signed copy inside the wheel. That is the check it already performs and it blocks booking and payment on a mismatch, so it needs no extra tooling. Adding an untested `cosign` line to a security-critical install path would have been worse than pointing at the gate that already exists
- Gave `link-cli` a concrete `npm install -g @stripe/link-cli` fallback. `your harness's link-cli provisioner` left an installer without one to guess, and the guess happened to be right; the next one may not be
- Said where the file goes (its own directory named `booking-agent`, as `SKILL.md`) and that no second copy may sit beside it, after a leftover `booking-agent-SKILL.md` at the skills root registered as a junk skill. Deliberately described the layout rather than naming an absolute path: installers write into several layouts, and `test_skill_portability` fails the build on harness paths in this file
- Kept `## Installing` and `### Updates (only with the user's yes)` spelled exactly as before. The first is matched literally by `test_what_the_hash_must_still_catch`, the second is the target of three in-file anchors, and nothing in the repository checks that an anchor still resolves
- Rolled out as `available` with `ecosystem_ready: true`: no contract, command, JSON field or session schema changed, and the text this fixes is read at install time, so no installed assistant needs to hear about it. `deferred` would have been the quieter default but would also hold back every later version behind a documentation fix, since a release after a deferred one is never offered either

---

## 18/10/26 at 13:25:51 by [Ran Yahalom](mailto:ranya@wix.com)

- Rolled 0.4.0 and 0.4.1 out to users: both release-notes entries move from `update_status: deferred` to `recommended` with `ecosystem_ready: true`, and `readiness_evidence` now records the facts that were re-confirmed before the flip. Both shipped deliberately quiet, and the skill signing and verification they add is only worth having once users actually run it; nothing is offered while either entry stays deferred, because a release after a deferred one is never offered either
- Recommended 0.4.0 as well, rather than leaving it deferred or withdrawing it, after reading how the update flow picks a version. `select_target()` takes the newest release that is not deferred or withdrawn and `update --user-confirmed` installs that exact version's verified wheel, so with 0.4.1 recommended the target is always 0.4.1 and no installed assistant can land on the raw-byte-hash bug 0.4.0 shipped with. Leaving 0.4.0 deferred would not protect anyone; it would only hold 0.4.1 back behind it
- Did not mark 0.4.0 `withdrawn`: it was never offered to anyone, and the only way onto it is a manual pin. Such a user is already carried forward, because 0.4.1 is recommended and lists the booking and payment capabilities, so the move is offered before any request that needs them. `withdrawn` would add an urgent pre-booking interruption for a population that is reached anyway
- Left `change_type` as released, `feature` for 0.4.0 and `fix` for 0.4.1: it records what shipped in each version and is not a rollout decision to revisit
- Regenerated `release-notes/index.json`, the single file an installed CLI fetches. No shipped code changes, so no version bump

---

## 18/10/26 at 13:02:36 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.4.1 (skill 0.51.0). The skill check now hashes what installers leave alone -- the instructions, plus `name`, `version` and a whitespace-collapsed `description` -- instead of the raw bytes, because `skill-install-check.yml` showed that one agent layout written by `npx skills add` re-serializes the file: `description` is refolded from `|` to `>` and rewrapped, and `allowed-tools` and `user-invocable` are dropped. Under the old raw-byte hash every user who installed through that layout would have been told their instructions were tampered with and refused booking and payment. 0.4.0 was never rolled out (`deferred`), so no installed assistant was affected
- `description` is covered despite being refolded, by collapsing runs of whitespace before hashing, so the same sentences hash the same whatever the fold width. It carries real instructions (never start an inbound webhook, surface the approval URL once), so leaving it out would have let someone change the assistant's behaviour unnoticed
- `allowed-tools` is deliberately **not** covered, although it is security-relevant: no installer preserves it, so hashing it would refuse every rewritten install. This is recorded in the module docstring rather than left implicit
- Added `.github/workflows/skill-install-check.yml`, which installs the skill the way users do (`npx skills add`, and OpenClaw from a local directory) and fails if the CLI reports `skill_unverified` for a genuinely installed copy. The check asserts acceptance rather than byte equality, since installers are expected to rewrite layout; it is how this bug was found, and it runs daily so a future installer change is caught the next morning rather than by a user who cannot pay

---

## 18/10/26 at 12:37:36 by [Ran Yahalom](mailto:ranya@wix.com)

- Added `.github/workflows/skill-install-check.yml`, which installs the skill the way users do (`npx skills add wix/booking-agent --skill booking-agent`) and fails if the installed bytes differ from the published skill or if the CLI reports `skill_unverified` for that copy. The runtime check hashes raw bytes, so an installer that started rewriting `SKILL.md` on install would make every genuine install refuse to book or pay; that is a silent, external regression no existing test could catch, and it needs a recurring check rather than a one-off manual install
- The OpenClaw job is a non-blocking probe in this first run, because the CLI's package name and its local-install behaviour are not something to guess at in a committed workflow; it is tightened into an assertion once the run reports them

---

## 18/10/26 at 11:55:59 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.4.0 (skill 0.50.0). Every release now signs the skill itself: `release.yml` attests `skills/booking-agent/SKILL.md` with GitHub build provenance, attaches that file and its `SKILL.md.sigstore.json` bundle to the GitHub Release, and creates the immutable tag `skill-v<frontmatter version>`, because the wheel was the only thing a harness could verify while the file that actually drives the assistant was fetched from an unpinned `master` URL. A tampered skill could redirect a booking or a payment with every wheel check still passing
- `flow init` takes `--skill-path` and compares the loaded `SKILL.md` with the signed copy inside the wheel. A file claiming the bundled version that does not match it fails `skill_unverified`, and `flow book` and every `flow pay` command refuse until the user consents to `booking-agent skill print --output <path>` restoring the official text. A file declaring another version stays with the existing update flow, which already offers to replace a stale copy, and skill files resolving into a source checkout are left alone so development edits are not blocked. The saved booking file keeps format 2 with one new optional top-level field
- The internal bundled copy of the skill is now `booking_agent_cli/bundled_skill.md`, not `SKILL.md`, and CI fails on any second tracked file named `SKILL.md`, because skill installers copy the directory holding a `SKILL.md` and `npx skills add wix/booking-agent --skill booking-agent` would otherwise be able to install the Python package directory in place of the skill
- `check_version.py` now also fails CI when a skill URL anywhere in the repository is neither the canonical `master` URL nor the pinned URL of the current skill version, when the skill's `metadata.url` or `openclaw.homepage` is not the canonical URL, or when a changed skill does not raise its frontmatter version, because a released `skill-v<version>` tag can never move. `sync_skill_bundle.py` rewrites those pinned URLs from the skill's own frontmatter
- `release-watch.yml` now also fails when the skill tag, either release asset, or the attestation for the file served at the pinned URL is missing, so a half-finished release is caught the next morning rather than by a harness
- Rollout is `deferred` with `ecosystem_ready: false`: the pinned tag, the release assets, and the attestation this release describes exist only once it is published, so it should be offered to users only after the published artifacts are confirmed

---

## 18/10/26 at 09:50:49 by [Ran Yahalom](mailto:ranya@wix.com)

- 0.3.0 is now `recommended` with `ecosystem_ready: true`, because it was held at `deferred` only until its published files were shown to carry attestations, and that is now confirmed: PyPI serves provenance for the wheel and the sdist signed by `wix/booking-agent` `release.yml`, a clean install from PyPI passes its own first-run check, a tampered install is refused, and saved bookings move between 0.2.0 and 0.3.0 in both directions. Installed assistants on 0.2.0 will now offer the update, once, with the user's consent
- The 0.3.0 release run first stalled in `build` for an hour after all tests had passed (the job outlived its 20-minute timeout, and the same commit's CI exited normally), so it was cancelled and re-run; the re-run published 0.3.0, its tag, and its GitHub Release with every job green

---

## 18/10/26 at 09:16:49 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.3.0 (skill 0.49.0). Every wheel and sdist `release.yml` publishes now carries a PEP 740 attestation (Sigstore keyless, signed by `pypi-attestations sign` and uploaded by `uv publish`) plus a GitHub build provenance attestation, because until now nothing tied a `booking-agent-cli` file on PyPI to this repository, so a compromised PyPI account or a swapped file could not be told apart from a real release. TestPyPI uploads are signed against production Sigstore too, since TestPyPI verifies attestations there. Build provenance runs in its own `attest` job rather than in `build`, so the job that runs the tests still never holds `id-token: write`
- `booking-agent update` now downloads the selected wheel, checks it against the hash PyPI lists, verifies its attestation names `wix/booking-agent` and workflow `release.yml`, and installs that exact file from `~/.booking-agent/verified-wheels/`, because an updater that trusts whatever the index serves would install a malicious release on the user's yes. Any failure (no attestation, wrong publisher, bad signature, different bytes, PyPI unreachable) installs nothing and reports `update_not_verified` with the reason
- On the first `flow init` after an install, the CLI compares every installed file with the `RECORD` of the attested wheel for its own version, and `flow init`, `flow book`, and every `flow pay` command refuse with `installation_unverified` and a reinstall command when they differ, because the update check alone could not catch a tampered first install. The result is remembered per installation (by its `RECORD` fingerprint), so the check runs once and a reinstall runs it again; a PyPI outage never blocks a booking, and source checkouts are not checked. The saved booking file keeps format 2 with two new optional top-level fields
- The skill now installs a pinned release (`kind: uv`, `booking-agent-cli==0.3.0`) and documents verifying the wheel before installing it, because an unpinned install could pull a release the skill was not written for. `scripts/sync_skill_bundle.py` writes the pin from `pyproject.toml`, `scripts/check_version.py` fails CI when any pin in either skill copy disagrees, and `release-watch.yml` now also fails when the published version has no provenance on PyPI or the skill pins another version
- Rollout is `deferred` with `ecosystem_ready: false`: 0.3.0 checks itself against PyPI's provenance for 0.3.0, which exists only once this release is published, so it should be offered to users only after the published files are confirmed to carry attestations

---

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

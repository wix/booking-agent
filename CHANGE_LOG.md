# Change Log

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

# Verified agent harnesses

A verified harness has passed a hosted booking replay of its own: a real booking that
survived the restart that harness actually performs, with that harness's calendar
behavior. Each guide below installs the Booking Agent CLI and skill pair approved for
the harness and sets up updates so the harness is offered a newer release only after
that release passes the same replay.

<!-- verified-harnesses:start -->
| Harness | Install guide | Approved CLI | Approved skill | Hosted replay |
|---|---|---|---|---|
| Base44 Superagent | [Install on Base44 Superagent](base44-superagent.md) | `0.10.2` | `0.57.4` | Passed 22 Oct 2026 with CLI 0.10.2 |
| Grok Bot | [Install on Grok Bot](grok-bot.md) | `0.10.0` | `0.57.2` | Passed 22 Oct 2026 with CLI 0.9.1 |
| Muse | [Install on Muse](muse.md) | `0.11.1` | `0.58.1` | Passed 25 Oct 2026 with CLI 0.11.1 |
<!-- verified-harnesses:end -->

The approved CLI is the newest release that the harness's replay recommends. It can be
newer than the CLI the replay ran when that release carries every replayed change; the
release notes for the approved CLI record why.

## Any other harness

Use the general install in the [README](../../README.md#on-any-other-agent-harness).
Without a replay setting, the CLI follows the general rollout.

## Adding a harness

The replay procedures, and how a harness gets its own replay or shares an existing one,
are in [ops/verified-harnesses.md](../../ops/verified-harnesses.md). The table above,
the pinned values in each guide, and the README table are generated from
`ops/verified-harnesses.json` by `scripts/harness_docs.py`; never edit them by hand.

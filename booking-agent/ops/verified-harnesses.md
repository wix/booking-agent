# Verified harness replays

A verified harness has a replay of its own. Passing that replay moves `deferred` to
`recommended` only for the harnesses that share it. Passing the general replay in
[CONTRIBUTING.md](../CONTRIBUTING.md#hosted-replay) does that only for harnesses that are
not listed here (Cursor, Claude Code, and any later harness without a replay).

The CLI enforces this when `BOOKING_AGENT_REPLAY` is set to the replay id assigned in
`ops/verified-harnesses.json`, either in the runtime environment or the persistent
`~/.booking-agent/.env` file. The harness operator sets it once; the assistant must not
guess it from the conversation. With the variable unset, the CLI uses the general
rollout. With any replay id set, a release without a matching `replay_rollouts` record
stays `deferred`, even when the general status is `recommended`. A release that is
`withdrawn` for everyone stays withdrawn here too.

CLI versions before 0.10.0 do not understand replay-specific rollouts. A verified
harness installs its approved release manually, from its guide in
[docs/harnesses](../docs/harnesses/README.md). From 0.10.0 onward, the installed CLI
enforces the harness rollout for later updates. The completed Grok Bot replay is
recorded as `recommended` for 0.9.0, 0.9.1, and 0.10.0, so its cumulative rollout has no
deferred gap through 0.10.0. The completed Base44 Superagent replay of 0.10.2 is
recorded as `recommended` for 0.10.0, 0.10.1, and 0.10.2, so an installed 0.10.0 or
0.10.1 is offered 0.10.2.

## Registry

Harness-to-replay assignments live in `ops/verified-harnesses.json`, outside the
portable CLI. The replay id is what `BOOKING_AGENT_REPLAY` and `replay_rollouts` use.
Two harnesses assigned the same replay id promote together from one successful replay.

Each harness entry also holds its display name, its install guide under
`docs/harnesses/`, and its status (`verified` or `pending`). A verified entry records
`replayed`, the CLI and skill pair the replay ran and its date, and `recommended`, the
pair its guide installs. `python scripts/release_notes.py --check` fails unless
`recommended` is the newest release whose `replay_rollouts` recommends that replay, with
that release's skill. After recording a replay rollout, update the entry and run
`python scripts/harness_docs.py` to regenerate the README table and the guides' pinned
values.

| Harness | Id | Replay id | Calendar during the replay |
|---|---|---|---|
| Grok Bot | `grok-bot` | `grok-bot` | Unavailable. Expect a clear statement that no calendar event was added. |
| Base44 Superagent | `base44-superagent` | `base44-superagent` | Connected. Expect the raw calendar event link. |

To add a harness that shares an existing replay, point its id at that replay id and
do not invent a second procedure. To add a harness with a different restart or calendar
rule, give it a new replay id and add the procedure below. The release-notes validator
loads the allowed replay ids from the registry.

## What every replay still checks

Use the six steps in CONTRIBUTING, with the restart and calendar rows from the harness
below. Also run any payment path the release notes still call unreplayed. For 0.9.1
that is a pay-at-appointment booking: the business confirms it without a charge, and
canceling an open checkout does not cancel a booking. Record the result as
`Hosted replay (<replay id>):` inside that replay's `readiness_evidence`.

A targeted supplement for the same release and harness does not repeat steps that an
earlier recorded replay already passed. For example, the pay-at-appointment supplement
does not repeat the restart after the base Grok Bot replay has already proved that the
saved state survives Update Bot's Computer. The evidence must identify both runs so a
reviewer can see the complete result.

### Delegated calendar

A replay that connects Google Calendar with `--route delegated` (the harness's own platform
writes the event) also checks the report-back:

1. After payment, the output has `awaiting_user: calendar_delegated_write`. The booking
   confirmation is posted first, then the platform creates one event with the requested fields.
2. `flow calendar record-event` succeeds with the platform's event id and link, and the booked
   message shows that raw link. A report with other times fails with
   `calendar_delegated_time_mismatch`.
3. After the restart, `flow status` does not ask for the write again. Cancel or move the booking
   and expect a delete or move request that is also reported back.
4. The availability step says the user's calendar was not checked. Never record this route as
   conflict checking.

A harness's calendar row changes to delegated only after a replay of these steps passes.

Set the assigned replay before the run. On a host that does not inject it, put this non-secret
setting in the persistent Booking Agent `.env` file so every CLI process sees it:

```bash
printf '\nBOOKING_AGENT_REPLAY=<replay id>\n' >> ~/.booking-agent/.env
```

## Grok Bot

Grok Bot keeps one cloud computer for every bot. A new bot is a new conversation on
that same computer. It does not reboot the computer, so it does not change the boot id
and it does not clear temporary files. Quitting the desktop app does not do that either.

The restart that counts is **Update Bot's Computer** in Settings, on the Computer tab
(`grokbot://app/v1/settings?id=update-computer`). Press Update, then confirm. Files and
logins stay, including `/workspace`. Installed programs may need reinstalling from a
copy kept under `/workspace`. Do not use **Reset Bot's Computer**. That restores an
older snapshot and can drop a booking that just completed.

1. Export `BOOKING_AGENT_REPLAY=grok-bot`.
2. Start the booking and choose a slot. Stop there.
3. Back up `/workspace/booking-agent`, `/workspace/link-cli`, and the installed skill
   into a dated folder under `/workspace`. Confirm `.booking-agent-boot` and
   `.usp-session.json` are in the backup.
4. Update the computer.
5. Check that the boot id changed and `.usp-session.json` still matches the backup.
   Reinstall `booking-agent` from the verified wheel in the backup if it is missing,
   and reinstall `link-cli` the same way. Continue the saved booking. Do not search
   again and do not sign in again unless Link expired.
6. Expect "You're booked" and a clear statement that no calendar event was added.
7. Ask "what's going on?" and run `booking-agent runtime doctor`. Expect
   `survived_restart: true`, `survived_restart_basis: boot_id_changed`, `healthy: true`,
   and no findings.

A doctor result of `folder_not_yet_shown_to_survive_a_restart` before this update is
expected. Another new bot will not clear it.

## Base44 Superagent

Superagent can connect Google Calendar, and this replay requires that connection. The
booked message must include the raw calendar event link.

A new conversation is not enough when the sandbox keeps its temporary files. The
restart that counts is the platform sandbox reset that wipes the sandbox root and
restores the saved-agent folder (`/app/.agents` on the replays run so far), then a
fresh conversation. After it, `runtime doctor` must report `survived_restart: true`.
The exact control is the one the Superagent host shows for resetting the sandbox while
keeping that saved folder. Do not delete the saved folder. The host used for the 0.10.2
replay showed no such control, so that run wiped everything under `/app` except
`/app/.agents` (plus `/tmp` and `/root`) from inside the sandbox, and the boot id also
changed. When the host has no reset control, do the same and record it as a caveat.

1. Export `BOOKING_AGENT_REPLAY=base44-superagent`.
2. Connect Google Calendar when the assistant asks. Start the booking and choose a slot.
3. Reset the sandbox as above, then open a fresh conversation and continue the saved
   booking. Expect at most one reconnect message, and no second Link sign-in unless
   the Link session expired.
4. Expect "You're booked" and the raw calendar event link.
5. Ask "what's going on?" and run `booking-agent runtime doctor`. Expect
   `survived_restart: true`, `healthy: true`, and no findings.

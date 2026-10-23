# Change Log

## 23/10/26 at 13:57:09 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.11.0 (skill 0.58.0). Added a third Google Calendar route, `--route delegated`, for assistants whose platform can write calendar events itself but cannot hand a token to the CLI, so their users can still get the booked appointment on their calendar instead of a skipped calendar step. The CLI never holds a token on this route: it tells the agent exactly which event to create, move, or delete, and records the event id and link the agent reports back with the new `flow calendar record-event`
- Made the delegated route events-only (`calendar_delegated_events_only` for free/busy) and marked availability as not checked against the user's calendar, because without a token the CLI cannot read busy times and must never claim it did. A saved delegated choice is reused on later bookings without a token and is never mistaken for an expired one, and a late connect at the summary keeps the chosen time like the other routes
- Kept each delegated write as a pending request with a one-time request id in the existing calendar event file, so a repeated or stale report (`calendar_delegated_request_unknown`), a report missing the id or link (`calendar_delegated_report_incomplete`), or an event written at other times than the booking (`calendar_delegated_time_mismatch`, compared as instants so any offset is accepted) is refused instead of recording a wrong event. A failed write tells the user to add, move, or delete the event themselves. `flow status`, `calendar create-event`, `update-event`, and `delete-event` re-issue an open request with the same id so the platform never writes it twice, and an event keeps the route it was written on so a later route change never sends it to the wrong writer
- Handed move, cancel, reset, sessions forget, and cancel-open cleanup to the platform on this route. `flow purge` sends its deletes without a report command because purge erases the calendar event file the report would be recorded in; this deviates from giving every delete its own reportable request
- Documented the route in the skill (within its size budget: the delegated rules live in the CLI prompts, and two skill lines that repeated CLI-enforced checks were shortened or removed), the README route sentence, and a "Delegated calendar" replay rule in `ops/verified-harnesses.md`. Grok Bot and Base44 Superagent rows, guides, and recommended pins are unchanged; the release is deferred until a harness replays the route

---

## 23/10/26 at 09:12:37 by [Ran Yahalom](mailto:ranya@wix.com)

- Recorded the Base44 Superagent replay of 0.10.2 as `recommended` on 0.10.0 and 0.10.1 too, because 0.10.2 carries every change in them and an update always targets the newest release the replay recommends. Base44 Superagent installs of 0.10.0 and 0.10.1 were otherwise never offered the replayed 0.10.2. 0.9.1 and earlier stay deferred for this harness because its pay-at-appointment path was not replayed, and the general rollout is unchanged. The 0.10.2 caveat that said earlier entries were not yet updated now says they are
- Extended `ops/verified-harnesses.json` to schema 2 so it is the one source for verified-harness install docs: each harness has its name, replay id, status, install guide, the CLI and skill pair its replay ran, and the pair its guide installs. `release_notes.py --check` now fails when an approved pair is not the newest release that replay recommends with that release's skill, when a replayed pair does not exist, when a guide is missing, or when a pending harness already has a recommended release, so docs cannot pin a version the harness was never approved for
- Rewrote the README install section so users start from installation: verified harnesses first (a generated table linking to per-harness guides and a warning to install the approved pair), then the general `npx skills add` path, then "Pin and verify a release", which now shows how to write the matching skill from the installed CLI with `booking-agent skill print`. The wheel download commands use `uvx --python 3.12` because plain `uvx pip` chose a system Python 3.9 in the smoke test and found no release
- Added install guides for Grok Bot and Base44 Superagent under `docs/harnesses/`, each with a one-paste prompt for the harness, a signed-wheel install into the harness's persistent folder, the bundled skill, `runtime prepare`, the harness's `BOOKING_AGENT_REPLAY`, checks, recovery after that harness's restart, and its calendar behavior and known gaps. Every command was smoke-tested in throwaway folders for both pinned pairs (0.10.0 with skill 0.57.2, 0.10.2 with skill 0.57.4): the wheel signature verified, the written skill matched its signed `skill-v` tag byte for byte, `config show` read the replay from the persistent `.env`, and repeating `runtime prepare` was harmless. The PyPI readme and the replay runbook link to them
- Added `scripts/harness_docs.py`, its tests, and a CI step that fail when the README table, the harness index, or a guide's pinned values differ from the registry, so a rollout change cannot leave stale install pins behind. The runbook explains how to update the registry after a replay and records that the accepted Base44 run used an agent-run wipe because the host had no reset control. No shipped code or skill text changed, so the version stays 0.10.2

---

## 22/10/26 at 16:50:02 by [Ran Yahalom](mailto:ranya@wix.com)

- Recorded the Base44 Superagent replay of the published 0.10.2 as `recommended` for that replay only: a signed install, a booking resumed after a sandbox wipe with the same summary and slot, a calendar reconnect that kept the chosen time, a $1 payment, the booked message with the raw calendar link, and a healthy `runtime doctor`. The evidence also states what it does not show: the harness first omitted the calendar link from its own message, the restart was an agent-run wipe because the host has no reset control, and the pay-at-appointment path was not run. Earlier releases stay deferred for this harness and the general rollout is unchanged

---

## 22/10/26 at 16:25:08 by [Ran Yahalom](mailto:ranya@wix.com)

- Made the search for unlinked saved state tolerate folders it cannot read: on CI runners `Path.is_file` raised `PermissionError` for a home-folder path, which crashed `flow init`, `flow status`, and the persistence tests; an unreadable path now counts as absent, and a test covers it

---

## 22/10/26 at 16:05:35 by [Ran Yahalom](mailto:ranya@wix.com)

- Fixed a second Base44 Superagent rehearsal failure for the unreleased 0.10.2: reconnecting an expired Google Calendar token at the booking summary sent the booking back to the calendar step, so the chosen time and summary were dropped and times were listed again. Calendar connect, skip, and the unavailable fallback now keep a booking that already has a time where it is and point at `buyer resolve`; early in a booking they still lead to availability

---

## 22/10/26 at 15:54:49 by [Ran Yahalom](mailto:ranya@wix.com)

- Fixed the Base44 Superagent replay failure where, after a sandbox wipe that kept the saved folder, a new conversation found no linked state, started a new booking with `--no-persistent-root`, and asked for a second Link sign-in even though the saved booking and sign-in were intact: `flow status`, `flow init`, and `runtime doctor` now find an unfinished booking in a folder this CLI prepared earlier and point at `runtime prepare --persistent-root <folder>` instead of letting the assistant start over
- Told the skill what to do with that answer (run `next_command` once, then `flow status`; never `--no-persistent-root` or a new booking) and trimmed other install wording to stay inside the skill's size budget; released as 0.10.2 with skill 0.57.4, left deferred with `ecosystem_ready: false` until its hosted replay passes. 0.10.1 is already published and unchanged

---

## 22/10/26 at 15:30:31 by [Ran Yahalom](mailto:ranya@wix.com)

- Raised the skill version to 0.57.3 for the 0.10.1 release, because the CLI version bump changes the skill's install pins and the `version` CI check requires every changed skill to carry a new version; the release notes, the bundled copy, and the version the skill passes on every command now agree

---

## 22/10/26 at 15:19:38 by [Ran Yahalom](mailto:ranya@wix.com)

- Fixed the Base44 Superagent replay failure where a new conversation could not pick up a booking waiting for the user's yes: `flow status` now returns the summary step for that booking and `buyer resolve` runs again from that phase and shows the same summary, so the assistant no longer abandons the saved booking and starts a request from scratch. Before this, the command offered after a changed summary (`buyer resolve`) was refused from that phase
- Released as 0.10.1 (skill 0.57.2, pin updated), left deferred with `ecosystem_ready: false` until its hosted replay passes; added three tests that fail without the change

---

## 22/10/26 at 14:31:48 by [Ran Yahalom](mailto:ranya@wix.com)

- Promoted the cumulative Grok Bot rollout through 0.10.0 to `recommended` after its restart, paid booking, pay-at-appointment, abandoned checkout, cancellation, and runtime health replays passed; the general rollout and Base44 Superagent remain deferred
- Released as 0.10.0 with skill 0.57.2: the portable CLI now consumes a generic replay id while the external verified-harness registry maps harnesses to replays, so one successful replay can safely promote multiple harnesses without naming hosts in shipped code
- Documented the one-time manual 0.10.0 bootstrap required by older CLIs that cannot interpret replay-specific rollout metadata, preventing the new policy from being mistaken for an automatic upgrade from 0.9.1

---

## 22/10/26 at 13:40:13 by [Ran Yahalom](mailto:ranya@wix.com)

- Added verified-harness rollouts, released as 0.10.0 (skill 0.57.1) and left deferred: Grok Bot and Base44 Superagent move from deferred to recommended only when their own replay passes, harnesses that share a replay id promote together, and every other harness still uses the general rollout
- Recorded the Grok Bot computer update and the Base44 Superagent sandbox reset, including which replay expects a calendar link, so a later harness can reuse a replay instead of inventing a second one

---

## 22/10/26 at 12:00:36 by [Ran Yahalom](mailto:ranya@wix.com)

- Made the hosted replay's calendar expectation conditional because some agent harnesses cannot pass a calendar connector token to the CLI. Connected runs must show the raw event link; explicitly skipped or unavailable calendar runs must clearly state that no event was added

---

## 22/10/26 at 11:55:55 by [Ran Yahalom](mailto:ranya@wix.com)

- Dropped "without nudging" from the hosted replay's payment step, because waking the assistant when a background command finishes belongs to the agent harness, which this project does not control. The step still requires the "You're booked" message and the raw calendar event link

---

## 21/10/26 at 16:21:41 by [Ran Yahalom](mailto:ranya@wix.com)

- Part of the unreleased 0.9.1 (skill 0.57.1, no further version bump), still deferred with `ecosystem_ready: false`: these fixes ship together with the resume-after-restart work in one PyPI version
- Accepted the live pay-at-appointment wire: a completed offline checkout whose booking is still pending is an accepted order, never a charge. `pay ucp-complete` now waits for the business to confirm and finishes with payment `not_required`, instead of reporting a captured payment with an unconfirmed booking. A timeout stops with `booking_confirmation_pending` (nothing charged, resume with `book poll --wait`) and a decline stops with `booking_not_confirmed`
- Stopped treating a pay-at-appointment order id as money collected, so confirmation, reschedule (unknown price is no longer refused) and cancellation (no refund steps or refund wording) all reflect that nothing was charged
- Stopped after a checkout cancellation that returns no booking: USP 7.5 places no requirement on a booking that was never confirmed, so the agent no longer falls through to a USP cancel. It tells the user nothing was booked or charged, saves the optional `ucp_checkout_abandoned` flag so a repeat cancel never reaches USP, and leaves the pending booking untouched
- Added a test-only switch to the loopback checkout store that keeps the booking pending at completion, plus tests for delayed confirmation, timeout, decline, booking-less cancellation, pay-at-appointment cancel without refund, and reschedule with an unknown price
- Updated the skill (same 0.57.1) so the pay-at-appointment checkout paragraph covers the two new codes, listed them as expected codes so they never offer a support note, trimmed two redundant phrases to stay inside the size budget, and refreshed the 0.9.1 release notes with the evidence and the new optional session field

---

## 21/10/26 at 12:40:46 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.9.1 (skill 0.57.1), deferred with `ecosystem_ready: false` until a hosted replay passes: the hosted replay of 0.9.0 failed, and 0.9.0 stays deferred, so this release carries both
- Made a new runtime read the saved booking first: any state-changing `flow` command in a runtime that has not run `flow status` (a marker in the temporary folder, which a fresh sandbox does not keep) now refuses with `resume_check_required` and points at `flow status`. The replay showed an assistant in a new conversation answering "no active session" or guessing; this is enforced by the CLI instead of skill text. `BOOKING_AGENT_RESUME_CHECK=0` turns it off
- Counted each new runtime beside the saved booking (`.runtime-starts`) and made `runtime doctor` report `survived_restart_basis`: it said `null` on a host whose sandboxes share one boot id, which proved nothing either way
- Added `calendar skip --session-only --no-route` and a saved `calendar_skipped_unasked` flag: both replays skipped the calendar without asking, so the booked message had no event link and said nothing. It now says no calendar event was added when the user did not choose to skip. The skill now treats a connector the platform lists but the user has not connected as a route
- Updated the skill (0.57.1): a new conversation starts at `flow status` and a reply answers only the question status shows; trimmed repeated text to stay inside the size budget
- Folder names such as the kept `/app/.agents` stay out of the CLI (a test forbids naming hosts), so the persistence question is unchanged; CLI persistence of the install itself was not done because a CLI cannot run when it is missing

---

## 21/10/26 at 12:21:04 by [Ran Yahalom](mailto:ranya@wix.com)

- Stopped a forgotten `--service-id` on `flow availability` from queuing a support note on its first occurrence: the hosted replay of 0.9.0 showed the error had no recovery command, so it counted as an unknown failure and left a note waiting that the user was never asked about. The error now names `flow discover` as the recovery and is listed with the agent-contract codes that never offer a note
- Made `flow status` drop a waiting note once the booking has moved past the step the note describes, instead of showing only the consent question: after a restart the user's "yes" to the booking summary was read as consent to send the note
- Refused `flow availability` once a time is chosen and the summary is shown (`availability_after_slot_confirmed`) unless the user asks for another time (`--change-time`, summary stage only): re-listing times had moved a booking back from the summary and discarded the confirmed slot
- Added `test_resume_after_restart.py` covering the three behaviors above; the full suite passes
- Not released: the version, skill text, resume check, folder suggestion, and calendar changes from the plan are still to do, so release 0.9.0 stays deferred

---

## 21/10/26 at 08:09:43 by [Ran Yahalom](mailto:ranya@wix.com)

- Removed `scripts/generate_ingest_schemas.py` and its drift test: generating the support ingest's files and checking them for drift now lives with the ingest itself, so this public repository carries no code or paths for the support ingest and the check runs where the ingest's files are rather than being skipped here
- Reworded the schema module docstrings and a `support_report.py` comment to describe the support ingest's contract without naming its internal files

---

## 21/10/26 at 08:09:22 by [Ran Yahalom](mailto:ranya@wix.com)

- Part of 0.9.0. A cancellation or rescheduling policy that is only the schema's required `allowed: true`, with no window, fee, or limit, no longer counts as a business policy, because businesses with no policy configured still send that default and users were being asked to accept a bare "Cancellation allowed" the business never set
- The user now accepts the business's policies in the same question as the booking summary on every route, and `flow book` enforces it, so paid and pay-at-service checkouts no longer add a second policy question in the common case. Policy links a checkout adds afterwards (terms, privacy, refund) are shown to the user without a new question; payment asks again only when the checkout adds policy text, payment terms, or disclosures the user has not seen

---

## 20/10/26 at 20:53:15 by [Ran Yahalom](mailto:ranya@wix.com)

- Made the support report leak test deterministic: its fake gate code was the digits `4242`, which a report timestamp's microseconds can contain by chance, so the test failed intermittently in CI. The gate code is now letters, which no timestamp or ID in the report can produce

---

## 20/10/26 at 20:31:17 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.9.0 (skill 0.57.0), a breaking release below 1.0.0 because a booking now needs new confirmation flags: a transcript of an AC repair booking showed an agent skipping the calendar question, never showing the business's policies, booking without the user's yes on the summary, and editing state files by hand, so the steps that the skill only asked for are now enforced by the CLI and cannot be skipped by an agent that ignores the skill
- `flow book` now needs `--user-confirmed`, bound to a digest of the recap the user saw, so a booking can no longer be committed on a summary the user never confirmed or that changed afterwards
- The business's cancellation and rescheduling policies, checkout policy links (terms, privacy, refund), and disclosures are collected and shown with the recap, and the step that commits the booking (`flow book` for direct routes, `pay request` for paid checkouts, `pay ucp-complete` for pay-at-service) needs `--policies-accepted` bound to a digest of exactly those policies. There is no policy step when the business sent no policies, since the acceptance is a contractual requirement owed to the provider, not a generic confirmation
- Pay-at-service checkouts now complete in two steps (`flow book` surfaces, `pay ucp-complete` commits) so the policy acceptance happens before anything is committed
- `calendar connect` and `calendar skip` now fail with `calendar_offer_not_shown` until the calendar question was asked, and a calendar connection found in the environment or cache is no longer taken as the user's choice, so the user always gets the choice; logout now says honestly what was disconnected
- Discovery validates structured times, derives the availability window from the requested time range, drops results whose business profile cannot be resolved (answering `no_bookable_results` when none remain), detects a mismatch between the request and the chosen service and blocks `business use` until the user confirmed it, and lists asking about the search area before `search-area propose` as forbidden (the user was asked twice), because the transcript booked an unbookable business and an unrequested service
- `business use` on a business whose profile fails to resolve now returns the recoverable `profile_resolve_failed` with a message offering the other results, instead of an internal error
- Added `flow purge`, which deletes local booking state only after the user saw what will be removed and confirmed, so agents never need to touch state files; `forget`, `reset`, and `purge` keep calendar events of upcoming bookings
- The Link approval link is always shown once before `link-auth poll` starts polling, cancellation now returns a `user_message`, and library events no longer print to stderr unless `BOOKING_AGENT_STDERR_EVENTS=1`, because the transcript's agent never showed the link and was confused by stderr noise
- `validate-skill-compliance` now flags bookings committed without policy acceptance, recap confirmation, mismatch confirmation, the calendar question, or a structured time preference
- Added automatic fault reports: when the CLI absorbs an error the user never sees (such as a broken business profile during discovery), it sends a small report with no user data (step, exception type, code-defined message template, failing host and path, registry IDs, versions) to the support ingest, at most once a day per fault, after the command's output is written and within one second. Turn it off with `BOOKING_AGENT_FAULT_REPORTS=0`. Tests guard that every absorbed exception in the live backend is recorded and that no buyer data can enter a report
- Added `scripts/generate_ingest_schemas.py` and a drift test (run when `USP_SPEC_INGEST_DIR` is set) so the ingest's generated schema files stay identical to the CLI schemas
- Release order: this version sends fault reports, so merge it only after the updated support ingest is deployed and its fault smoke test passes

---

## 20/10/26 at 13:45:51 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.8.0 (skill 0.56.0). A service paid at the appointment can now be booked through the business's checkout when the business declares the pay-at-service extension, and the booking completes with an empty `payment` and no Link spend request. The business now answers these bookings with a checkout and an order, and it refuses a payment instrument on them rather than ignoring it, so a client that kept sending one would break; USP 11.3.5 S4/S5 (not base UCP, whose schema only permits an empty `payment`) defines the instrument-free completion
- Routing is by the business's declaration, not the service alone: a business that does not declare the extension keeps the direct booking path, because USP 7.6.1 forbids routing a pay-at-appointment service through a checkout the business has not declared the extension for. A declaration also needs the payment-terms extension (11.3.2), and the live checkout is re-checked before it is created
- Added strict validation of the offline settlement (unknown mode, a term that is not the selected one, more than one offline term, a `due_at` that is not the slot start, amounts that do not sum to the total all fail closed), so a booking that looks confirmed with no record of how it will be paid cannot be completed. An empty payment-handler set is not treated as a reason to leave for the business's site
- The `payment_due_at_service` disclosure is now shown before the booking completes (completion needs `--disclosures-confirmed`), and the confirmation says nothing was charged and what is due at the appointment, instead of the deposit-only handling it had before
- Declared the extension in the hosted platform profile (regenerated `platform-profile.json`) and in the skill, and removed the stale README and CONTRIBUTING claim that pay-at-service always fails closed. Rollout is `deferred` with `ecosystem_ready: false`: the new path was proven only against loopback mocks, and no hosted replay against a live business that declares the extension has been run
- Added loopback end-to-end and unit tests for both the declared and undeclared cases. The opt-in staging-registry search test fails the same way on unchanged 0.7.0 (live business profiles do not resolve), so it is not caused by this change

---

## 19/10/26 at 13:05:12 by [Ran Yahalom](mailto:ranya@wix.com)

- Rolled out 0.5.0 (`available`), 0.6.0 (`available`) and 0.7.0 (`recommended`) together, with `ecosystem_ready: true`, because a deferred release holds back everything after it (installing 0.7.0 brings 0.5.0 and 0.6.0), so flipping only the newest would have left installed assistants on 0.4.2 never offered anything. After this, an assistant on 0.4.2, 0.5.0 or 0.6.0 is offered 0.7.0 with its consent
- The evidence is a hosted replay on a Superagent: 0.5.0 booked end to end on 18/10/26, and on 19/10/26 a real sandbox restart (boot id changed, `/root` wiped, `/app/.agents` restored) after which the first `flow init` was refused with `persistence_unresolved`, the assistant named the surviving folder, the earlier booking was found again, and `runtime doctor` reported `survived_restart: true`. Each release's evidence line states what was not shown: the platform does not wake the assistant after an approval (a platform matter), the host-variable setup of 0.6.0 was not exercised, and a complete new booking after the restart was not repeated
- Rewrote the 0.5.0 summary users may be shown: it said booking "no longer stalls", which the hosted run contradicted (the user still had to ask once after each approval), and the 0.7.0 summary no longer says the assistant "can no longer forget". A summary the assistant repeats to a user must not promise more than was shown

---

## 19/10/26 at 12:07:00 by [Ran Yahalom](mailto:ranya@wix.com)

- Part of 0.7.0 (skill 0.55.0). Fixed the runtime doctor test that passed on macOS and failed on every Linux Python in CI: where the machine has a boot id, a folder set up in the current boot cannot yet be shown to survive a restart, so `runtime doctor` correctly adds an informational finding and the test that expected none was only true where boots cannot be told apart. The test now pins the boot id both ways, with a new test for the Linux case, so it no longer depends on the OS it runs on
- A folder accepted after two corrections used to leave the correction count at two, so a later, different bad folder was waved through with no correction at all. The count now starts over when a folder is accepted, because each folder the agent names is its own decision
- The correction message said "Nothing was changed" although it records that it sent the agent back; it now says "Nothing was moved", which is what is true. Wrapped the over-long persistence paragraph in the skill, and wrote down in the code and the release evidence that the boot-id proof never gives a false yes but can stay unproven when sandboxes share one kernel, and that the first `flow init` of a runtime now needs one of the two flags

---

## 19/10/26 at 11:03:43 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.7.0 (skill 0.55.0). `flow init` now refuses (`persistence_unresolved`) until the agent says how this runtime keeps its files: `--persistent-root <folder>` links saved bookings and the Link sign-in into the folder it names, `--no-persistent-root` records that the runtime keeps its files anyway. A hosted replay showed the assistant skipping `runtime prepare` with nothing failing, so the booking, calendar link and sign-in sat outside the folder the platform restores and would be lost at the next restart. The calendar already works this way: the CLI will not run `availability` until the agent has observed the runtime and answered, and the same pattern removes the chance to forget. The CLI never guesses a folder
- The folder an agent names is checked before anything moves: absolute and writable, not under a temporary folder, not on a memory-backed filesystem, not the filesystem root. A folder that fails is turned back with `persistence_folder_not_durable` and the reason, at most twice; the third time the folder is used as given and `runtime doctor` notes it, so a stubborn agent can never leave a booking stuck on this question. `--no-persistent-root` is always accepted and never counted. Whether a folder on ordinary disk is really restored cannot be known in the same boot, so `prepare` also keeps the boot id that first used the folder and `runtime doctor` reports `survived_restart` once a later boot finds it still there. The four persistence error codes are listed as expected so an agent being asked how state is kept is never offered a note to the assistant team
- A host never has to be asked: a linked state folder, `BOOKING_AGENT_PERSISTENT_ROOT`, or `BOOKING_AGENT_PERSISTENCE=none` all count as answered. The "no folder" answer is a small note beside the saved state, so a fresh runtime that lost the state is asked again, and no field is added to the saved booking file. Both settings are listed in `defaults.env`
- The skill now describes this one step in place of the old "run `runtime prepare` when this runtime needs it" judgment, and the tests run as a host that keeps its own files so only the gate's own tests exercise the question. Rolled out as `deferred` with `ecosystem_ready: false` until the same restart is replayed with a real assistant

---

## 19/10/26 at 00:20:44 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.6.0 (skill 0.54.0). A host that names its persistent folder in `BOOKING_AGENT_PERSISTENT_ROOT` now gets booking state and the Link sign-in linked into it by the CLI itself, before every command except `runtime`, quietly and without ever blocking a command. A hosted replay showed the assistant skipping `runtime prepare` (the skill told it to run it "when this runtime needs it"), so today's booking, calendar link and sign-in lived outside the folder the platform restores at boot and would be lost at the next restart. The setting is listed in `defaults.env` and `runtime doctor` flags a named folder the state is not in
- `runtime prepare` now merges instead of setting one side aside when both the runtime folder and the persistent folder hold data, and when it moves from one root to another or lifts the old nested layout. Booking files are merged by session id and the copy that got further wins; for any other file the newer one stays and the older is kept aside, and nothing is deleted. The platform's restored top-level booking file was a stale `INIT` snapshot while the real one sat in the nested folder, and the previous "persistent copy wins" rule would have hidden today's finished booking behind it
- The skill (about 78.3 kB) tells the assistant to do nothing when the host names the folder and keeps the explicit `runtime prepare` step only for runtimes that do not. Rolled out as `deferred` with `ecosystem_ready: false` until the same restart is replayed in a real hosted session

---

## 18/10/26 at 22:31:47 by [Ran Yahalom](mailto:ranya@wix.com)

- Released as 0.5.0 (skill 0.53.0). `runtime prepare` no longer splits the saved state and the Link sign-in across folders: a root already named `booking-agent` is treated as its parent, a different root than the last run brings the existing state along (or leaves the old one untouched when the new root has its own), and the old nested `booking-agent/booking-agent` layout is lifted back up. Two recorded hosted runs lost the sign-in and the calendar after a restart because each run picked a different root, and the user was asked to sign in and reconnect one step at a time. Added `runtime doctor` to report where state lives and what an earlier layout left behind
- Moved `.calendar-events.json` from a hard-coded `~/.booking-agent` to the configured state folder, because it was the one file that ignored `BOOKING_AGENT_SESSION_DIR` and the persistent-folder links, so the calendar link could sit outside the preserved state
- `buyer resolve` and `pay request` now trust the live Link status over the cached sign-in flag, because a sign-in poll killed after the user approved left the flag false and made the user sign in again for nothing
- Added `flow connections ensure`, and `buyer resolve` now runs the same check before the confirmation question, so a lost calendar surfaces in one combined message before the user says yes instead of after. A calendar reconnect made late in a booking now continues at `buyer resolve` rather than restarting at availability
- `pay poll` now emits one terminal object. The earlier "checkout completed, now run book poll" object sent the agent after a step that was already done and was the object a harness could mistake for the result. The final object carries a plain "You're booked" message built from saved state, the raw calendar link when an event was added, and the booking link, and hands over the after-booking update check as its `next_command` so no skill text has to remember it
- `flow status` and `book poll` on a confirmed booking return that same message, and the calendar event id and link are saved on the session (kept out of support reports). The assistant had said it could not confirm an event that existed because only the final `pay poll` prompt ever held the link. A calendar write that fails after payment no longer replaces the booking confirmation with an error
- `flow status` while a payment waits on approval says so in a plain sentence once the user already has the link, and `flow status` before a business is chosen hands back the saved search results instead of making the agent search again. The default next step after listing slots now includes `--user-confirmed`, and `flow init` warns before an unfinished session with a payment in progress is abandoned, because the second recorded run paid twice after starting over
- Shortened the skill from about 99.7 kB to 78.4 kB by deleting rules the CLI now enforces or already states in its own `prompt` (the sign-in and background-poll choreography, the duplicated payment steps, the update-check and calendar-event steps, slot and timezone presentation, the per-route confirmation wording, the decline matrix, most self-evident error rows) and added the rules that were missing: one line after the user says yes, a one-line wait notice, and running `flow status` whenever a background command's result was not seen. Error codes whose rows were removed moved to the prose list so the support-report offer still treats them as expected. A size ceiling test stops prose creeping back
- `flow init` now asks whether to resume when an unfinished request similar to the new one exists (`awaiting_user: resume_or_fresh`), and `discover --asap` fills in the current time, because both were skill rules an agent could skip or get wrong (a skipped question starts a second booking; a hand-written timestamp can be wrong). The tests that pinned skill wording for discover ordering and per-route confirmation now assert the CLI's own prompts instead
- Added an offline replay of the recorded failure (restart, then a differently named persistent folder, mid-booking) to the loopback end-to-end tests
- `release_notes.py --check` now requires a `Hosted replay:` readiness-evidence line before a release that touches sign-in, calendar, or payment can be `ecosystem_ready: true` (from 0.5.0), and CONTRIBUTING, AGENTS.md, and the PR template point to the replay steps. The steps first sat in an unlinked `ops/` file that nothing referred to, so neither an agent following AGENTS.md nor a reviewer would have found them; green offline tests do not show how a hosted runtime behaves
- Added a regression index (`test_regression_index.py`) naming the test that holds each failure seen in the two recorded runs, and a check that the CLI code names no hosting platform. The earlier fixes for these problems lived only in skill wording and nothing failed when an agent skipped them, which is why they came back. Rolled out as `deferred` with `ecosystem_ready: false` until the same flow is replayed in a real hosted session

---

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

---
version: 0.58.1
name: booking-agent
description: |
  Books time-based services via USP from a Link wallet. Use when the user wants to book, schedule, reserve, or pay for a service from a business (haircut, lesson, repair, class, appointment, consult). The `booking-agent` CLI owns the booking state machine and refuses to proceed when human gates are skipped. Sign the user in to Link on your own, right after install and at the start of every booking (`booking-agent flow link-auth connect`, then start `booking-agent flow link-auth poll` in the background and post the sign-in URL in the same turn, exactly like `pay poll`); never wait for the user to ask to sign in or to say they approved. After `booking-agent flow business use`, follow `next_command` through `use-ucp-checkout-mode`; businesses without native checkout fail closed. Payment uses `booking-agent flow pay request` (default returns immediately with the approval URL), then start `booking-agent flow pay poll` in the background and post the approval URL to the user in the same turn; the poll finishes checkout once they approve. Confirm through `booking-agent flow book poll --wait`; never start an inbound webhook. Surface the approval URL exactly once. Personal identification (name, phone, email, address) must always come from the user's Link account via `link-cli`, never from chat or placeholders. Before any location-based search, the CLI proposes the Link shipping address and the user confirms it or gives another; online services skip this. Talk to the user in plain everyday language only; never expose CLI, UCP, auth, session, or other implementation details unless they explicitly ask.
allowed-tools:
  - Bash(booking-agent:*)
  - Bash(link-cli:*)
license: Complete terms at https://github.com/wix/booking-agent/blob/master/LICENSE
metadata:
  author: booking-agent
  url: https://raw.githubusercontent.com/wix/booking-agent/master/booking-agent/skills/booking-agent/SKILL.md
  openclaw:
    emoji: "📅"
    homepage: https://raw.githubusercontent.com/wix/booking-agent/master/booking-agent/skills/booking-agent/SKILL.md
    requires:
      bins:
        - booking-agent
        - link-cli
    install:
      - kind: uv
        package: "booking-agent-cli==0.11.1"
        bins: [booking-agent]
user-invocable: true
---

# USP Service Booking

`booking-agent` lets agents book time-based services through the Universal Scheduling Protocol. The
CLI owns the booking state machine and refuses to proceed when human gates are skipped. Your
job is to drive it, stop at the checkpoints below, and keep the user informed in language they
can understand without any technical background.

**Payment approval always happens in Link's trusted UI, never by typing approval in chat.**

## Talking to the user (required)

The person you are helping wants to book a service. They are **not** technically savvy and
do not understand terms like CLI, UCP, auth, session, OAuth, checkout mode, symlink, polling,
or orchestrator.

### Rules

1. **Never tell the user about technical details** of the booking flow unless they explicitly
   ask how something works behind the scenes.
2. **Always guide the user in simple, everyday terms**: what you just did for them, where the
   booking stands right now, and what you need from them next (or what will happen next).
3. **Keep all implementation vocabulary internal.** Command names, JSON fields, error codes,
   tool names, and skill jargon are for you only, not for chat.
4. **Do not narrate setup or housekeeping.** Never mention reading skills, contracts, verifying
   CLI tools, symlink setup, re-auth, sessions, prerequisites, or checkout paths. Just do that
   work silently and tell the user only what matters to their booking. This includes the very
   first message: never open with lines like "I'll read the booking skill first" or "checking
   for an earlier booking". The first thing the user sees is the message from rule 5.
5. **One short opening message, then silence until the next required question or results.**
   Run Step 1 setup (`sessions list`, `flow init`, `prereq check` when needed) silently. The only
   thing you may send before the opening message is the Link sign-in link (see [Link
   sign-in](#link-sign-in-automatic)). When the service happens at a place, the opening message
   asks the user to confirm the search area `flow search-area propose` returned (see [Search
   area](#search-area)); nothing is searched until they confirm. Online or remote services skip
   the area question. If the time they want is unknown, ask it in the same message and stop.
   Send the opening message **once per booking**; if the next reply only answers a question, go
   straight to the next question or result.
6. **Never use markdown hyperlinks with labels** (`[text](https://...)`). Many chat clients
   (including WhatsApp) do not render them; the trailing `)` is included in the URL and breaks
   the link. **Always post the raw URL** (e.g. `https://app.link.com/...`). You may add
   plain-language narration before or after, but the URL itself must not be wrapped in markdown
   link syntax. This applies to Link spend approval, Link sign-in, Google Calendar OAuth, and
   post-booking calendar event links (`data.html_link`).
7. **Never format user-facing messages as markdown tables** (`| col | col |`). Many chat clients
   (including WhatsApp) do not render them and show raw pipe characters instead. **Use numbered
   or bulleted lists** (e.g. `1. 11:00 AM - 12:00 PM`). This applies especially when presenting
   available time slots, service options, or booking recaps.
8. **Only state facts the CLI returned.** When something changes (a time that was open is gone,
   a price differs), say what changed, not why, unless the output gives a reason. For example,
   "That time is no longer open" is right; "it was held for your current booking" is a guess.

### What to say instead

| Internal (never say unless asked) | User-facing |
|---|---|
| Reading skill / contract / instructions | (say nothing; proceed silently) |
| CLI tools / symlink setup / prereq check | (say nothing; proceed silently) |
| Environment check / tools ready / starting fresh | (say nothing; proceed silently) |
| re-auth / session / flow phase | "You're all set" / "your earlier booking request" / "where we left off" |
| UCP-native checkout | "I can take care of booking and payment right here" |
| checkout mode / orchestrator | (omit; explain only that this business cannot be booked here yet) |
| auth_url / OAuth / calendar connect | "Here's a link to connect your Google Calendar" |
| spend request / pay poll / approval URL | Narrate in plain terms, then post the raw approval URL from `user_message` (never `[label](url)`) |
| buyer resolve / link-cli | "I pulled your name and contact info from your Link account" |

### Search area

The CLI finds, matches, and remembers the search area. **Never** geocode an address yourself,
never pass coordinates, and never invent or complete an address.

1. **Propose.** When the service happens at a place, run:

   ```bash
   booking-agent flow search-area propose
   ```

   It reads the user's saved Link addresses, matches the default one on the map, and returns
   `awaiting_user: search_area_confirmation` with `data.proposed_area.label` (and any other saved
   addresses in `data.other_addresses`). Ask the question in `prompt` and **STOP**. If it fails
   with `link_auth_gate_not_completed`, the user is signed out: sign them in (see [Link
   sign-in](#link-sign-in-automatic)) and run `propose` again. Never ask for an address
   because of a sign-in problem.
2. **The user answers.**
   - Yes: run `next_command` (`booking-agent flow search-area confirm --user-confirmed`).
   - Another saved address: `booking-agent flow search-area set --link-address-id <id>`.
   - A different address or place they type ("downtown Phoenix", a street address):
     `booking-agent flow search-area set --address '<exactly what they typed>'`. The CLI shows
     what the map matched (`data.proposed_area.matched`); show that match and ask whether it is
     right. A typed address is never used until the user confirms the match.
   - No saved address (`awaiting_user: search_area_input`): ask where to search, then run `set
     --address` with their words.
3. **Search.** After `confirm`, run `next_command` (`booking-agent flow discover --radius-km
   <km>`) with the radius below. Discover searches the confirmed area; it refuses a place
   search without one (`search_area_unconfirmed`).

Skip all of this when the service is not tied to a place (online lessons, remote consults,
a specific named business with no area). Run plain `booking-agent flow discover`.

**Radius (required on place-based search).** The CLI does not pick a hidden default. Always pass
`--radius-km` once an area is confirmed, except for the area-only fallback below.

1. **User stated a distance limit** (e.g. "within 10 miles"): convert to kilometres and use that.
2. **Otherwise use this heuristic:**
   - Buyer travels to the business, salon, or a named city area: start at **25 km**.
   - Provider comes to the buyer (`at_buyer_location`, home visit): start at **40 km**.
   - User named a wide region only (e.g. "anywhere in Arizona"): do not guess a tight metro
     radius; use a large cap they implied.
3. **Expand if too few results.** "Too few" means zero services, or fewer than three when the
   query was generic (not a named business). Retry with the next rung:
   - Buyer-travel starts: 25 -> 50 -> 100 km (at most two expansions).
   - Home-visit starts: 40 -> 80 -> 120 km.
   Tell the user in plain language that you widened the search area. Do not mention CLI flags.
4. **Area-only fallback.** If the last expanded radius still returns zero services, retry
   once with no `--radius-km`. Report distances when the CLI provides them. Do not loop. This
   escape hatch is for registry quirks; cite issue #225 only in agent reasoning, not in
   user-facing copy unless they ask.

If a service is outside the radius you actually sent (including after expansion), the CLI
refuses to offer it. Do not bypass with `business use`. Online or remote services with no
coordinates stay offerable. Present `data.services` in registry order; never re-sort.

**Never suggest or apply filters.** Discover is called with no service-type or category
filter. Do not offer the user filters ("only home visits?", "only salons?") and do not narrow
results yourself.

### Opening response (first booking request)

1. Silent setup: session check, `init`, prereq. If `init` returns `link-auth connect`, sign the user
   in first (the only message before the opening one; never ask whether they want to).
2. For a service at a place, run `flow search-area propose` before your first chat message.
3. One opening message that mirrors the request and asks what is still unknown: the area question
   from `propose`, plus the time question when time intent is unknown (for example "Should I look
   for AC repair technicians near 3194 West Rome Street, Yuma AZ, or somewhere else? And when
   would you like them to come?"). An online service just says "Understood, looking for online
   Spanish tutors next week..." and runs discover with no area.
4. After they confirm the area and time, show the discover results, or ask whether to resume an
   unfinished booking. Never explain what you did behind the scenes.

### Narration pattern (every turn)

After each successful step (except silent setup above), write 1-2 short sentences for the user:

1. **What happened** in plain terms (e.g. "I found three AC repair options near your home.")
2. **What's next** or what you need from them (e.g. "Which one would you like?")

When waiting on them, ask one clear question. Do not explain how you got the information.

Say each fact once. Do not restate a recap, a price, or a status the user already saw unless it
changed. Do not narrate progress between gates ("now checking availability", "still
working"); the one-line notes under Background commands are the only exception.

### Background commands and never going quiet

- When a background command finishes, report only its **final** outcome. If a newer command
  already superseded it, say nothing about the older one. Relay each `user_message` once.
- Never run `sleep`, a fixed wait, or a timer loop next to a foreground `next_command`; wait for
  the command itself to exit.
- Never leave the user guessing. After they say yes to a booking, send one short line ("Booking it
  now.") before running `flow book`. While a background command waits on them, one line saying
  what they are waiting on is enough ("Waiting for your approval in Link; I'll confirm here as
  soon as it goes through."). If something blocks the booking, say what it is and what you need
  from them in that same turn.
- If you cannot tell whether a background command finished (no exit notice, or the user wrote
  again, even just "well?" or "what's going on?"), run `booking-agent flow status` and answer
  from its `user_message`. Do not ask them to wait or to check for themselves. If this runtime
  cannot run background commands, run the poll in the foreground; it has its own time limit.

### When they explicitly ask technical questions

Only then may you use terms like CLI, UCP, or session. Keep explanations brief.

## Installing

**Start here.** When this runtime can run `npx`, one command installs this file:

```bash
npx skills add wix/booking-agent --skill booking-agent
```

That takes the latest skill, from the canonical raw URL in this file's metadata. Then go to
[After installing](#after-installing). A harness that also reads the `install` block in that
metadata provisions the pinned `booking-agent` CLI from it; one that does not still needs
[The booking-agent CLI](#the-booking-agent-cli) below. Everything between here and
[After installing](#after-installing) is the manual path, for a runtime that cannot run the
installer or that wants a fixed, verified version.

### This skill file

Keep this file in its own directory named `booking-agent`, as `SKILL.md`. Leave no second copy of
it beside that directory: some harnesses register every `.md` under the skills root as its own
skill, and a stray copy becomes a second set of instructions that nothing verifies.

To take a fixed, published version instead of the latest, fetch it at the release tag the official
workflow signs it under, and check the signature before you follow it:

```bash
curl -fsSLO https://raw.githubusercontent.com/wix/booking-agent/skill-v0.58.1/booking-agent/skills/booking-agent/SKILL.md
gh attestation verify SKILL.md \
  --repo wix/booking-agent \
  --signer-workflow wix/booking-agent/.github/workflows/release.yml
```

Do not follow the file until that verify step passes. Every release also attaches the same
`SKILL.md` and its `SKILL.md.sigstore.json` bundle to the GitHub Release, so a runtime that
verifies bundles offline can check it without calling `gh`.

**Without `gh`, the CLI runs this check for you.** Install the CLI first (next section, which
verifies its own signature), then pass the path you loaded this file from on every `flow init`
(`--skill-path`, below). The CLI compares it with the signed copy inside the installed CLI and
refuses to book or pay when a file claiming this same version does not match it. That gate needs
no extra tooling, and it catches a file changed after it was installed; only the attestation above
covers the very first read.

### The booking-agent CLI

Install the `booking-agent` CLI release this skill was written for (package `booking-agent-cli` on
PyPI). Every release is signed by the official release workflow (PEP 740), so check the signature
before installing:

```bash
dir="$HOME/.booking-agent/verified-wheels"
uvx pip download "booking-agent-cli==0.11.1" --no-deps --only-binary :all: -d "$dir"
uvx pypi-attestations==0.0.30 verify pypi --repository https://github.com/wix/booking-agent \
  "$dir/booking_agent_cli-0.11.1-py3-none-any.whl"
uv tool install --force "$dir/booking_agent_cli-0.11.1-py3-none-any.whl"

booking-agent --help
```

Install only a wheel whose verify step passed. If `uvx` is not
available, install the same pinned release with `uv tool install booking-agent-cli==0.11.1` or
`pipx install booking-agent-cli==0.11.1`; the CLI checks its own files against the signed release
on the first `booking-agent flow init` and refuses to book or pay if they do not match.

Harnesses that read this skill's `install` block do this for you. Do not go on
until `booking-agent --help` runs. If it still does not, run the commands above yourself; if that
is not possible in this runtime, ask the user to install the skill, then try again. To develop the
CLI from a checkout instead, see the [Booking Agent README](https://github.com/wix/booking-agent/blob/master/booking-agent/README.md).

### link-cli

`link-cli` is required for buyer identity and spend-request create/approve. Install it through the
[link-cli skill](https://github.com/stripe/link-cli/blob/main/skills/create-payment-credential/SKILL.md)
or your harness's link-cli provisioner. With neither, install it from npm:

```bash
npm install -g @stripe/link-cli
```

### After installing

**Keep state between conversations.** `flow init` asks how this runtime keeps its files and refuses
(`persistence_unresolved`) until you answer. Look at what the runtime offers. If it gives you a
folder that survives between conversations, re-run the same init with `--persistent-root <absolute
path of that folder>`: the CLI links saved bookings and the Link sign-in into it, merging what each
folder holds (booking files by session, the newer copy of anything else) and deleting nothing. If
the runtime keeps its files anyway, or gives no such folder, re-run it with `--no-persistent-root`.
If it answers `persistence_folder_not_durable` (temporary, memory-backed, or unwritable), name the
folder the runtime really keeps, or use `--no-persistent-root`.
`persistence_setup_failed` and `persistence_flags_conflict` say what to change. Use the same folder every time; never guess a path, ask the user, or mention it. A host
that sets `BOOKING_AGENT_PERSISTENT_ROOT` or `BOOKING_AGENT_PERSISTENCE=none` is never asked. If the
user is asked to sign in or reconnect right after a restart, run `booking-agent runtime doctor` and
follow its `next_command`. If `flow status` or `flow init` answers `saved_state_not_linked`, run its `next_command` once, then
`flow status`; never answer it with `--no-persistent-root` or a new booking. For extra cache folders the runtime asks you to keep, run `booking-agent
runtime prepare --persistent-root <folder> --link <runtime folder>=<persistent folder>`.

**Sign the user in to Link**, as described in [Link sign-in](#link-sign-in-automatic). It works
with no booking in progress. Do not wait for the user to offer to sign in.

**Pass `--skill-path` and `--skill-version` on every `flow init`**: the path you loaded this file
from, and the version from the frontmatter above.

Nothing needs configuring: every setting has a shipped default, and the user never configures the
calendar: you work out at runtime how this runtime can reach Google Calendar (see [the calendar gate](#step-3-select-business-and-resolve-calendar-gate)).
**Never** write tokens, keys, or personal details into any env file. `booking-agent config show`
prints each effective setting and where it came from; `booking-agent config init` creates
`~/.booking-agent/.env` for an operator to edit.

### Updates (only with the user's yes)

The CLI decides whether a newer version is worth mentioning; you never decide from version
numbers or release pages. The `--skill-version` you pass on every `flow init` lets the CLI tell when these instructions
and the installed CLI no longer match.

- When a command returns `awaiting_user: update_consent`, ask the question in `prompt` in plain
  words, using `data.user_summary` as the reason, and **STOP**. On yes, run `next_command`
  (`booking-agent update --user-confirmed`). On no, run `data.decline_command` (the offer is not
  made again), then follow the rest of `prompt`.
- When `booking-agent update --user-confirmed` finishes, run its `next_command` (`booking-agent
  skill print`). If you know the path of the skill file you loaded, run `booking-agent skill
  print --output <that path>` to replace it. Otherwise follow `data.skill_text` for the rest of
  this conversation. Either way, tell the user that starting a new chat loads the updated
  assistant.
- Never upgrade or reinstall an installed CLI or this skill yourself, and never offer an update
  in the middle of a booking. The CLI refuses while a booking is in progress.
- If the user asks to update, run `booking-agent update check`, then follow it.

## Link sign-in (automatic)

Every booking needs the user signed in to Link: their name, contact details, saved addresses, and
payment all come from it. **You start the sign-in yourself.** The user never asks for it and never
has to tell you they approved.

Run `booking-agent flow link-auth connect` right after installing, and whenever `flow init` or any
other command returns it as `next_command`. It returns at once: `data.authenticated: true` means say
nothing and run `next_command`; otherwise it returns `awaiting_user: link_auth_approval` with the
sign-in URL in `user_message`. In the **same turn**:

1. Start `booking-agent flow link-auth poll` **in the background** (`run_in_background`). It exits
   the moment the user approves.
2. Post the raw URL with one short sentence, for example "Please sign in to your Link account here
   so I can use your saved details: <url> I'll continue as soon as you approve." and end the turn so
   the message is delivered. Never run the first `poll` in the foreground.
3. When the poll exits, run its `next_command` and carry on, without waiting for a reply and
   without repeating the link.
4. On `link_auth_timeout`: if `next_command` is `link-auth poll`, the same link still works (poll
   again in the background, do not re-post it); if it is `link-auth connect`, the link expired (run
   it, post the new link once, poll in the background).
5. If the user says they signed in and no poll is running, run `link-auth poll` in the foreground;
   it returns at once when they are signed in.

Never call `link-cli auth login` directly, and never use `link-auth connect --wait` (scripts only).

## Personal identification (always from Link)

Whenever you need the user's **name**, **phone number**, **email**, or **address** — for
booking, checkout, forms, or any other step:

1. **Never** ask the user to type these in chat or invent placeholders (e.g. "Ada Lovelace",
   `ada@example.com`, fake street addresses). Personal user details such as name, phone, and
   email **always** come from the user's Link account — never invented or requested as
   placeholders.
2. **Never** invent an address and never type one into a command yourself. The CLI proposes
   the user's Link shipping address and the user confirms it or gives another (see [Search
   area](#search-area)); a typed address is used only after the CLI shows its map match and the
   user confirms it. For home visits (`channel.type: at_buyer_location`) the confirmed street
   address is sent to the business automatically on `flow book`. **Never** put the address in
   `--notes`.

3. **Always** derive personal details from the user's Link account using `link-cli`:

   ```bash
   link-cli user-info retrieve --format json       # name, email, phone
   link-cli shipping-address list --format json    # shipping address (use as home when needed)
   ```

4. During the booking flow, run **`booking-agent flow buyer resolve`** instead of calling
   `user-info retrieve` yourself — it invokes link-cli, stores buyer identity in the session,
   and satisfies compliance gates.
5. If retrieval fails because the user is signed out, sign them in as described in [Link
   sign-in](#link-sign-in-automatic). Do **not** call `link-cli auth login` directly.

Show retrieved values to the user for confirmation before submitting them anywhere.

## Running commands

Every `booking-agent` command emits one JSON object (sometimes preceded by a bare `user_message`
line on stdout). Long-running commands such as `pay poll` may emit earlier progress objects;
the **last** object is the result. Always read:

- `phase` — current booking phase
- `awaiting_user` — when set, **stop** before any other tool call. For `link_spend_approval`,
  post `user_message` as the raw URL (never markdown `[label](url)`). Otherwise ask the user
  the question in `prompt` and stop.
- `user_message` — when set, show **only** this text to the user immediately (for spend
  approval it is the bare Link URL; do not relay stderr). The CLI also prints
  `user_message` as the **first stdout line** before the JSON.
- `next_command` — the exact command to run next
- `forbidden` — what you must not do at this phase
- `data.error.code` (on non-zero exit) — what gate failed

List all commands: `booking-agent --help`. List flow subcommands: `booking-agent flow --help`. Recover
current state at any time: `booking-agent flow status`.

## Agent loop (run on every turn)

1. `booking-agent flow status` — read `phase`, `awaiting_user`, `prompt`, `next_command`, `forbidden`.
   A new conversation starts here; the user's reply answers only the question status shows.
2. If `awaiting_user` is set:
   - **`link_spend_approval`** or **`link_auth_approval`:** start the matching poll (`pay poll` or
     `link-auth poll`) **in the background** (`run_in_background`) and, in the **same turn**, post
     `user_message` as the raw URL before any other narration. Never block the turn on the poll
     first: the user would never see the link and the booking would look stuck. When the poll
     exits, post its `user_message` (after `pay poll` it is the booked answer) and run its
     `next_command` without waiting for a chat reply. The user approves in Link, not in chat.
   - **`connections`:** something this booking needs was disconnected (Link, Google Calendar, or
     both). Post one short message with every link (`user_message` is the Link sign-in link when
     Link is listed). When Link is listed, start `link-auth poll` in the background. Run the
     calendar `command` from `data.connections` and post any link it returns in that same
     message. Then run `data.resume_command` once they are back. Ask nothing else.
   - **`support_report_consent`:** show `user_message` to the user word for word (when it is
     set) and **STOP**. On yes, run `next_command` exactly as printed. On no, run
     `booking-agent flow support-report decline`. See
     [Notes to the assistant team](#notes-to-the-assistant-team).
   - **`update_consent`:** ask the question in `prompt` and **STOP**. On yes, run
     `next_command`; on no, run `data.decline_command`. See [Updates](#updates-only-with-the-users-yes).
   - **Any other value:** ask the user the question in `prompt` and **STOP**.
3. If the latest command output has `user_message` set (or a bare `https://` URL as the first
   stdout line), show that URL to the user as the **raw URL** before any other narration
   (never markdown `[label](url)`).
   **Show the Link approval URL at most once per booking** — never repeat it from `flow status`,
   `pay ucp-complete`, or error recovery output.
   If the output also has `support_report_offered: true`, show its `user_message` (when set),
   then run `next_command` in the **same turn**.
4. Otherwise run exactly the command in `next_command`.
5. Tell the user what just happened and what comes next, in everyday language only (see
   [Talking to the user](#talking-to-the-user-required)). Never mention commands, phases,
   sessions, auth, or checkout internals.
6. Repeat.

**Payment is two steps for agents:** `pay request` returns at once with the approval URL; start
`pay poll` in the background and post the URL in the same turn (Step 6c). Use `pay poll`, never
`pay ucp-complete` or `--then-complete`. If the user already approved but checkout did not finish,
run `booking-agent flow pay poll` in the foreground to resume and finish the booking.

## Core flow

Copy this checklist and track progress:

- Step 1: Initialize
- Step 2: Discover services
- Step 3: Select business and resolve calendar gate
- Step 4: Check availability and select a slot
- Step 5: Authenticate Link wallet and resolve buyer
- Step 6: Book and pay
- Step 7: Finish (validation, calendar event, update check)

### Step 1: Initialize

**Step 1a — Existing requests.** `flow init` asks for you: when an unfinished request similar to
this one exists it returns `awaiting_user: resume_or_fresh` (or errors with `session_active` for the
active one, or `duplicate_booking_confirmation` for an upcoming booking). Ask the user the question
in plain words (never say "session" or a phase name) and **STOP**; never answer for them or "peek"
with `flow status` and carry on. On continue run `data.resume_command` (if it fails with
`session_not_found`, ask again); on fresh re-run the same `init` with `--force`.

`sessions list` is local and operational, not booking history. Use it only to find upcoming
bookings on this device (`actionable: true`, with `booking_id` and slot times) to cancel or move.
For past appointments say you can only see upcoming ones booked here; older ones live in their
confirmation email or on the business's page (`booking_url`). Do not invent a lookup.

**Step 1b — Create a new session:**

```bash
booking-agent flow init --user-request "<user's exact request>" --skill-version 0.58.1 \
  --skill-path "<absolute path of the SKILL.md you loaded>"
```

Add `--capability <name>` once for each part of the request that clearly needs it: `calendar`
(they mention their calendar), `reschedule`, or `cancel`. Leave it out when unsure.

Pass `--skill-path` whenever you know where you loaded this file from, so the CLI can check it
against the signed copy it ships with. Leave it out when you do not know the path; never guess one
and never point it at a different file.

- If `init` returns `awaiting_user: update_consent`, no booking was started yet. Follow
  [Updates](#updates-only-with-the-users-yes); after the answer, run `data.continue_command` to
  start the booking.
- If `init` returns `awaiting_user: duplicate_booking_confirmation`, the user already has an
  upcoming booking on this device that matches this request (`data.upcoming_matches`). No new
  booking was started. Tell them about it in one sentence (service, business, time from
  `slot_start_display`) and ask whether they want an **additional** booking, then **STOP**.
  Run `next_command` (it adds `--allow-duplicate`) only if they say yes. Never cancel, move, or
  resume the existing booking unless they ask.
- If `init` returns `next_command: booking-agent flow link-auth connect`, the user is signed out
  of Link. Sign them in now, before any question or search (see [Link
  sign-in](#link-sign-in-automatic)), then follow the `next_command` from `link-auth poll`.
- If `init` returns `phase == "PREREQ_OK"`, skip directly to Step 2 (discover services).
- If `phase == "INIT"` (first session ever), run prereq check first:

```bash
booking-agent flow prereq check
```

`prereq check` verifies the USP registry is reachable. Native checkout validation is deferred
to Step 3 after the business is selected. It does not require a browser.

### Step 2: Discover services

```bash
booking-agent flow discover
```

**Time preference, before every discover:** if the user has not said when they want it in this
booking, **STOP** and ask in plain language (for example soonest, a day or window, a deadline,
several alternatives, or any time). Do not run discover or invent a range before they answer.

Pass time flags on every discover for this booking only when the user has a real time constraint:

- First available or soonest: `--asap` (the CLI fills in the current time).
- Any time is fine: omit all `--desired-*` flags (this is not ASAP). Add
  `--no-prefer-sooner-availability-slots` only if they also say earlier versus later does not
  matter.
- A specific clock time: `--desired-at <RFC3339>`. A date, window, or deadline:
  `--desired-range <startRfc3339>,<endRfc3339>` (the end is the latest acceptable start, not the
  end of the appointment); repeat it once per alternative window. After a date or event:
  `--desired-from <RFC3339>`. Encode a later period directly; never disable sooner preference
  to mean "later is better".

For date-only preferences, use that date's bounds in the service timezone when known.
Otherwise use the search-area or buyer-local date. Ask if the timezone or wording is
ambiguous. If the user later changes their time policy, run a new discover with new flags.
Do not reshuffle an existing page.

`time_preference_not_structured`: the request names a time but you passed no flag. Encode
`data.named_time` yourself and rerun; do not ask again.

Uses the exact `user_request` stored at `flow init` for the search query. **Do not pass
`--query` with a paraphrased or shortened version** — the registry ranks results for the
query text, and rewording it changes which services appear first.

Discovery uses that free-text query only. Do not add or infer a structured `categories[]`
filter, including when the user's request appears to name a category.

For a place-based service, confirm the area first (see [Search area](#search-area)) and pass
`--radius-km`. Discover uses the confirmed area; never pass coordinates. Without a place,
run discover with no area.

When presenting services, you may mention distance or service location **only when the CLI
returns that data**. Do not invent location restrictions. If a business defined no locations or
service area (online course, remote mentoring), do not tell the user location is "missing"; just
omit location from the recap.

**STOP.** Show the returned services and wait for the user to pick one. `prompt` says how: each
row's `display_line` verbatim, one numbered line each, in the exact order returned, business name
always shown, never filtered or re-sorted. Label a result a less-direct alternative only when its
returned data objectively mismatches the request (for example `channel.type: at_business_location`
for a home visit), keeping its position. Values come from the live catalog when `field_sources`
says `live_catalog`; with `discovery_mismatches` the live value is what will be booked, so never
present the search-index value as current.

When the user picks a result you labeled less-direct, pass the label on `business use
--less-direct '<label shown>'` (the CLI also detects some mismatches itself). It answers
`awaiting_user: intent_mismatch_confirmation`: say the mismatch plainly, **STOP**, and run
`next_command` (it adds `--mismatch-confirmed`) only after they agree.

Availability hints are advisory: never infer availability from result order or a hint (null or
zero included), keep `hint_usable: false` results, and check live availability before telling the
user their time cannot be booked.

### Step 3: Select business and resolve calendar gate

```bash
booking-agent flow business use --profile-url <profile_url> --service-id <service_id> --name '<business_name>'
```

Pass the row's `business_name` as `--name` so the booking, payment request, and calendar event
name the business the user picked. When omitted, the CLI falls back to the discovered name.

**Home visits need a street address.** When the picked service comes to the user
(`at_buyer_location`) and the confirmed area is not a full street address (a city, or no area
at all), `business use` returns `awaiting_user: service_address_confirmation` (or
`service_address_input` when Link has no saved address). Ask the question in `prompt`, then use
the same `search-area set` / `confirm` commands with `--purpose service-address` as `prompt` shows.
`confirm` resumes the business selection by itself.

After `business use` succeeds, follow `next_command` exactly. It must be
`booking-agent flow use-ucp-checkout-mode`; run it to validate native checkout. **Do not** tell the
user about checkout modes or UCP.

If `business use` fails with `service_details_changed`, the live catalog differs from the row
the user picked (`data.changes`, for example price, home visit versus at the shop, or
duration). Nothing was selected. Tell the user what changed in one plain sentence, ask whether
they still want it, and **STOP**. Run `next_command` (it adds `--details-confirmed`) only after
they agree; otherwise offer the other results.

If `business use` fails with `ucp_native_required`, or the checkout prerequisite fails, stop.
Tell the user in plain language that this business cannot be booked here yet and offer another
returned service. Never describe a fallback.

**STOP.** After `use-ucp-checkout-mode` succeeds, resolve the calendar gate:

```bash
booking-agent flow calendar ask
```

Follow `next_command` from the JSON. **Do not ask the user calendar options yourself.**

**Pick the calendar route from what you observe at runtime**, before asking the user anything:

- **`--route connector`**: your platform connects Google Calendar for the user and hands the
  access token to the commands you run in an environment variable. Pass that
  variable's **name** with `--token-env <NAME>`. Never pass, print, or store the token itself,
  and never copy it into an env file. Look up the name each conversation; do not assume it.
- **`--route local-oauth`**: `data.local_oauth_configured` is true and you can relay a sign-in
  link to the user.
- **`--route delegated`** (events only; busy times are never checked): your platform writes
  calendar events itself. Each write returns `awaiting_user: calendar_delegated_write`: post
  `user_message`, do it as the `prompt` says, then run `next_command` with only what it returned. On `calendar_delegated_events_only`,
  `calendar_delegated_time_mismatch`, `calendar_delegated_report_incomplete`, or
  `calendar_delegated_request_unknown`, run `next_command`.
- **Neither** (a listed but unconnected connector is a route): run
  `booking-agent flow calendar skip --session-only --no-route` without asking, then continue.

The CLI saves the route and variable name for later bookings and never guesses one: `calendar
connect` without `--route` fails with `calendar_route_required`, and `--route connector` without a valid `--token-env` fails with `calendar_token_env_required`.

`calendar ask` outcomes (each carries its own `prompt`; ask only what it says):

- **A saved preference:** the CLI applies it and advances to availability. Say briefly that you are using their calendar when conflict checking is on.
- **`awaiting_user: calendar_reconnect`:** the saved access expired. Do **not** ask the three
  calendar questions again. Ask only the two choices in `data.calendar_reconnect_choices`
  (reconnect for the same use, or continue without calendar checking) and run the matching
  `command` exactly as printed, or `booking-agent flow calendar skip
  --session-only`. With `data.calendar_checking_unavailable` the CLI already skipped it for
  this booking: say so in one plain sentence and continue.
- **`awaiting_user: calendar_consent`:** not connected yet. With a route available, ask which they
  prefer and run the matching command with your route; with none, skip as above without asking.
- Never quote calendar access tokens or Google token URLs.

```bash
booking-agent flow calendar skip                                                     # neither
booking-agent flow calendar connect --mode freebusy --route connector --token-env <NAME>  # free/busy only
booking-agent flow calendar connect --mode events --route connector --token-env <NAME>    # + post-booking event
booking-agent flow calendar connect --mode freebusy --route local-oauth                   # same, by sign-in link
booking-agent flow calendar connect --mode events --route local-oauth
```

**Connector route.** `calendar connect` returns at once. Connected: the gate completes. With
`awaiting_user: calendar_connector` (`data.error: calendar_not_connected`) the variable holds no
valid token yet: connect Google Calendar through your platform's own way, asking for the access in
`data.scope_request`, then rerun `data.retry_command`. Never show a sign-in link from this CLI for
this route; if your platform cannot connect it, run `booking-agent flow calendar skip
--session-only`. `calendar_insufficient_scope`: reconnect with `data.scope_request`, then rerun
the same command.

**Local OAuth route.** `calendar connect` returns at once with `awaiting_user: oauth_approval` and
`data.auth_url`. The CLI never opens a browser itself.

1. Post the raw `auth_url` (never markdown `[label](url)`), ask them to approve the access the
   CLI `prompt` describes, and say how long it works (`data.link_valid_minutes`).
2. In the same turn, run `booking-agent flow calendar poll` without blocking your turn (in the
   background when you can). While it returns `awaiting_user: oauth_approval` with `data.status:
   pending`, run it again without re-posting the link. When it connects, run `next_command`.
3. On `calendar_oauth_timeout` or `calendar_oauth_failed`, post the error's `user_message` and
   **STOP**; offer a new link (`new_link` in `data.choices`) or `calendar skip --session-only`
   only if the user asks. `calendar_insufficient_scope`: ask whether they want a new link.
   `calendar_oauth_not_started`: run `booking-agent flow calendar ask`.

`calendar_oauth_not_configured` or `calendar_callback_port_busy`: run `booking-agent flow calendar
skip --session-only` (or use the connector route when your platform has one).

If Google Calendar drops later in the booking, the CLI notices before the final confirmation and
returns `awaiting_user: connections` (see the Agent loop); you do not check it yourself.

### Step 4: Check availability and select a slot

```bash
booking-agent flow availability --service-id <id>
booking-agent flow slots list
```

`flow availability` defaults to a fixed near-term window (roughly the next two weeks). **You**
are responsible for converting the user's natural-language date (e.g. "next Thursday") into a
concrete calendar date. If the requested date might fall outside the default window — or you
just want to target one specific day — pass it explicitly:

```bash
booking-agent flow availability --service-id <id> --target-date 2026-06-11   # single day
# or an explicit ISO8601 range:
booking-agent flow availability --service-id <id> --from <ISO8601> --to <ISO8601>
```

If the requested date is not in the returned slots, **re-query with `--target-date` rather
than silently offering a different day** — then tell the user if that date truly has no
availability.

**STOP.** Show the available slots and wait for the user to pick one. Never auto-pick.
Present slots as a **numbered or bulleted list** in everyday time format (never a markdown
table with `|` pipes). Example:

```
1. 11:00 AM - 12:00 PM
2. 11:30 AM - 12:30 PM
3. 12:00 PM - 1:00 PM
```

Then ask which number or time works best.

**Timezones.** Slot times are the business's (`data.business_timezone`). The user's zone comes only
from their Link default address, never from this device, the host, a VPN, or chat. `slots list`'s
`prompt` says exactly how to show each slot (business time first, theirs in parentheses, the
one-time notice, the calendar zone, any fallback note); follow it. There is no separate timezone
question. When the CLI asks for a time confirmation (`next_command` includes
`--timezone-confirmed`), ask the user to confirm that exact time and **STOP**; without it `slots
select` returns `timezone_confirmation_required`.

Each listed time is one choice. When a slot has `resource_options`, several staff or resources
can take that time; do not list it more than once and do not ask the user to choose between
them.

```bash
booking-agent flow slots select --slot-id <chosen by user> --user-confirmed
```

`--user-confirmed` is required and the CLI refuses without it.

### Step 5: Authenticate Link wallet and resolve buyer

**Step 5a — Link sign-in.** `slots select` hands you `link-auth connect`. The user is normally
already signed in, so it returns at once. If it returns `awaiting_user: link_auth_approval`, follow
[Link sign-in](#link-sign-in-automatic).

**Step 5b — Resolve buyer**

```bash
booking-agent flow buyer resolve --notes '<short note for the business>'
```

Draft `--notes` from the user's request: the problem, the item, what they need (for example
"Window AC unit, completely unresponsive, brand unknown"). Leave it out when the request says
nothing the business needs. Never put the address, name, or contact details in it.

**STOP.** Show every line of `data.booking_recap_lines` verbatim, one per line, in order. The CLI
builds them (service and business, when, where with the exact address, price, name, email,
phone, and the note for the business), so never reword, merge, or drop a line. When
`data.selected_slot.buyer_display` is present, the When line already has the user's time in
parentheses, so one "yes" also covers the timezone difference. When `data.timezone_notice` is
present (the user's timezone was found only now), open with it. Then ask **one combined
confirmation**. If the user corrects the note, re-run `buyer resolve --notes '<corrected
note>'`; if they correct the address, run `booking-agent flow search-area propose --purpose
service-address`.

Ask the one question `prompt` describes for `data.payment_route` (free, pay at the appointment,
charge now, or deposit) and quote `data.price_display`, never raw `amount_minor_units`. For a free or
pay-at-appointment service never mention charging or the Link wallet. When `data.quote_caveat` is
set the amount is an estimate until the business's checkout total.

With `data.policy_lines` / `data.policy_links`, show them after the recap (links raw) and ask
for acceptance in the same question. Do not run `flow book` until they approve, then run
`next_command` as printed. Do not send a separate price-only or timezone confirmation after
booking.

This step applies [Personal identification (always from Link)](#personal-identification-always-from-link).
Never hand-enter buyer details. If `buyer resolve` fails with `link_cli_failed` or
`link_auth_expired`, sign the user in again (see [Link sign-in](#link-sign-in-automatic)), then
retry.

### Step 6: Book and pay

**Step 6a — Book**

```bash
booking-agent flow book --user-confirmed [--policies-accepted]
```

For home visits (`channel.type: at_buyer_location`), `flow book` sends the confirmed street
address and the note from `buyer resolve` automatically. Never map or pass an address yourself.
If it fails with `service_address_unconfirmed`, run its `next_command` and confirm the address
with the user.

`flow book` returns `data.merchant_quote` and `data.price_display`. **Never** quote `amount_minor_units`, `price_minor_units`, or
raw `amount` as dollars, only `price_display` or `merchant_quote.total_display`. Quote tax only
when `merchant_quote` includes a tax line from the merchant; never assume zero tax or warn that
tax might be extra when no tax line was returned. The spend request uses the merchant-advertised
total only.

**Business policies.** On `awaiting_user: policy_acceptance` or `policies_not_accepted`, post
`user_message` (every policy line and raw link), ask whether they accept, and **STOP**. Run
`next_command` (it adds `--policies-accepted`) only after a yes; on a no, do not book. No policies
sent: say nothing about them. Links `flow book` shows with no `awaiting_user`: post them, ask
nothing.

**Free and pay-at-appointment bookings (`free`, `at_service`).** `flow book` places the booking
directly and `next_command` is `booking-agent flow book poll --wait`. Before running it, tell the user
in one short plain message what `data.booking_narration` says: the request is in, nothing was
charged (or the price is paid at the appointment), and you are now waiting for the business to
confirm. Then run it and report the result. Never create a payment and skip
Steps 6b to 6d.

**Pay-at-appointment with a checkout (`pay_at_service`).** Nothing is charged; skip `pay request`.
Show the amount due at the appointment and any policies, then run `next_command`. If
`merchant_quote.total_changed`, get approval and add `--total-confirmed`. On
`booking_confirmation_pending` or `booking_not_confirmed`, post `user_message` and run any
`next_command`.

**Deposit bookings (`deposit_required`).** Before any charge, show `data.due_now_display` (the
deposit charged now), the amount due at the appointment, and every item in
`data.payment_schedules` and `data.payment_disclosures`, get the user's agreement, then run
`next_command`. Quote schedule times from `due_at_display` and
disclosure text from `content_display`; never quote raw `due_at` or timestamps inside
`content`.

The user already approved booking and charge at buyer resolve when the checkout total matches
what you quoted. Continue to Step 6b once any policies are accepted, unless
`data.merchant_quote.total_changed` is true. The CLI has already checked that the checkout line item matches the quoted service
price, so a changed total means the business added tax or fees (or a discount). Then **STOP**:
tell the user the service price (`merchant_quote.line_item_display` or `subtotal_display`),
the tax (`tax_display`, only when present), and the new total (`total_display`), and get their
approval for that total. Pass `--total-confirmed` to `pay request` only after they approve. The
spend request charges the checkout total.

**Step 6b — Resolve funding source** (shared, runs once before any payment branching)

```bash
link-cli payment-methods list --format json
```

Take the `id` of the default card. Pass it to `pay request` in Step 6c.

**Step 6c — Ask for approval, then wait in the background**

```bash
booking-agent flow pay request --payment-method-id <id-from-6b>
```

The CLI writes the note the user reads when approving in Link. Never write or pass that note yourself.
It returns at once with `awaiting_user: link_spend_approval` and the approval URL in
`user_message` (shown once per booking). In the **same turn**, start `booking-agent flow pay poll`
in the background, then post `user_message` as the raw URL before any other narration. `pay poll`
waits for the user to approve in Link, finishes checkout, validates the booking, and adds the
calendar event; when it exits, follow [Step 7](#step-7-finish). Do not read stderr for the URL, do
not wait for a chat reply, and never run this first `pay poll` in the foreground: the turn would
block and the user would never see the link.

**Step 6c-iii - Bank verification and declined payments**

There are two bank-verification moments, and they are handled in opposite ways:

- **Before the charge (issuance).** While `pay request` or `pay poll` waits on Link, the CLI may
  return `awaiting_user: link_spend_approval` with the bank's message and a raw verification URL
  in `user_message` (Link `next_action.type: three_d_secure`, `resolution: auto_resume`). This is
  not a payment failure. Start `next_command` (`booking-agent flow pay poll`) in the background and
  post `user_message` in the same turn; the poll keeps waiting on the **same** spend request. If the CLI instead returns
  `spend_request_replacement_required` (for example `three_d_secure_retry`), show `user_message`
  and run `next_command` to create a new spend request.
- **After a failed charge (post-failure).** Once `pay ucp-complete` (or `pay poll`) has sent the
  token to the business and the charge failed, **never** send the user to any spend-request
  `action_url` or bank verification link, even if Link shows one. That link would be the
  charge-side challenge, and finishing it can take the money with no confirmed booking. The CLI
  checks the spend request once, withholds that URL, and logs `post_failure_action_suppressed`
  with the checkout id. Tell the user the payment did not go through.

What to do after `pay ucp-complete` fails depends only on `data.error.code`. Do not decide from
the checkout status or from whether a `continue_url` exists. Every one of these errors carries a
plain-language `user_message` that tells the user what happened and what happens next. **Always
post it as is** (raw URLs, never markdown `[label](url)`) before running `next_command`:

Every one of these carries a plain `user_message` and a `next_command`: post the message as is (raw
URLs), then run the command.

- `payment_declined`, `payment_failed` (recoverable), `payment_authentication_unsupported`,
  `payment_not_processed`: the card was declined and the spend request is used up. When the user
  picks a **different** Link card, run `next_command` (`booking-agent flow pay request --payment-method-id <id>`).
- `payment_unrecoverable`: the checkout can no longer be completed (session `FAILED`). Post the
  message and stop; there is no `next_command`. Never retry this checkout.
- `complete_in_progress`, `checkout_already_in_progress`: not a decline. Same spend request and
  checkout; never a new spend request or charge.
- `payment_captured_booking_unconfirmed`: money taken, booking not confirmed. See below.
- `mandatory_acceptance_pending`: a policy or other non-payment step. The message holds
  `continue_url` and the policy links; wait until the user says they finished. Never accept for them.
- `approval_timeout`, `spend_request_not_approved`, `ucp_complete_failed`, and anything else: run
  `next_command` as returned; if it fails again, tell the user it could not be completed right now.

**Payment taken, booking not confirmed, during `pay poll`.** The background `pay poll` does not
fail here. It prints one output with `data.status: waiting_for_business_confirmation` and a
`user_message`, then keeps waiting for the business in the same process. Post that
`user_message` once as soon as you see it, then wait for the same `pay poll` to exit. Do not
run `book poll`, do not sleep, and do not start anything else. When it exits 0 its final output
is the confirmed booking; tell the user they are booked. If it exits nonzero, its final output
is one of the handoffs in step 3 below.

**Payment taken, booking not confirmed, from any other command.** On
`payment_captured_booking_unconfirmed`:

1. Post `user_message` **right away, before** running `next_command`. It tells the user the
   payment went through, the confirmation can take a couple of minutes, you are monitoring it, and
   they should not contact the business yet or pay again. Without it, the user may contact the
   business about a booking that is about to confirm on its own. Never retry the payment and
   never ask for another card. The CLI enforces this:
   `pay ucp-complete`, `pay request`, and `pay rebook` return `payment_already_captured`, and
   `flow status` points back at the booking check.
2. Run `next_command` (`booking-agent flow book poll --wait`) in the foreground. If the business
   confirms, it finishes the booking exactly like a normal success; tell the user they are booked.
3. If the business still has not confirmed, or cancels (`booking_canceled_after_payment`), the CLI
   returns a `user_message` with a link to reach the business (booking page or
   their site; never the checkout page), the booking and order references, and what to ask for:
   confirm the booking, or refund the payment. Post it as is.
4. If the user wants to dispute the charge, run `booking-agent flow evidence` and share its payment
   and booking references. Do not open a dispute on their behalf.

**`continue_url` rule:** after a `payment_declined` or `payment_failed` decline, **never** show
the business's hosted checkout page (`continue_url`), even if it appears in the response. The
CLI omits it. This rule covers only those declines. `continue_url` is still shown for
`mandatory_acceptance_pending`.

**Never** re-run `pay ucp-complete` with a spend request that was declined. The CLI clears it
and the next completion needs a new approval from the user.

The CLI handles credential-type routing internally from session `checkout_mode`. For UCP-native
checkout (Link Agent Wallet), `network_id` for the spend request comes from checkout
`com.stripe.payments` → `available_instruments` entry `type: "link"` → `config.network_id`
after `flow book` (read-only in `data.ucp_spt_network_id`); `pay request` applies it from the
session. Never pass `--network-id`, invent a `network_id`, run `link-cli mpp decode`, probe
merchant HTTP 402 / `WWW-Authenticate`, initialize Link UI, or call Stripe `/tokenize` for Link.
Handler contract: [Stripe Payments handler for UCP](https://docs.stripe.com/agentic-commerce/ucp/stripe-payments-handler)
(schema: `https://ucp.stripe.com/payments/2026-06-25/schema.json`). USP §7.4:
https://github.com/wix/universal-scheduling-protocol/blob/master/specification.md#74-paid-bookings-extension-schema

UCP `complete_checkout` (CLI builds this internally): `handler_id: "stripe_payments"`,
`type: "link"`, `credential.type: "stripe_payment_token"`, `credential.token` = SPT from
`link-cli` after approval (`--credential-type shared_payment_token` on spend-request create only).

**Step 6d** (validation) is part of `pay poll`; see Step 7.

**Payment notes (error recovery only):**

- `at_service`: tell the user the price is paid at the appointment; never run `pay request`.
- `deposit_required`: charge only the due-now amount; never compute a deposit yourself.
- Unknown or changed payment timing is not bookable. Get a new quote if timing changed.
- Businesses and the client fetch the document at `USP_AGENT_PROFILE_URL`; the
  canonical URL is used by default.
- After payment, follow `next_command` to `booking-agent flow book poll --wait`. Confirmation is GET
  polling only. Never start or request an inbound booking webhook.

### Step 7: Finish

`pay poll` (and `book poll --wait` for free or pay-at-appointment bookings) validates the booking,
adds the calendar event when the user chose events mode, and exits with the final answer. Post its
`user_message` exactly as written: it holds the booked sentence, the raw calendar link when an
event was added, and the link to manage the booking. Then run its `next_command` (a quick check
for a newer assistant) and follow it as in [Updates](#updates-only-with-the-users-yes). If
`data.calendar.calendar_not_added` is true the message already says so; run `booking-agent flow
calendar create-event` only if the user wants another try. If `validate-skill-compliance` ever
exits non-zero, read `data.error.message` and fix the missing gate before calling it done.

### Authorization trail (when the user asks)

After payment or when the user asks for receipt-style authorization ids:

```bash
booking-agent flow evidence
```

Share the join keys in plain language (booking reference, payment reference). Never expose card
data, credentials, or shared payment tokens.

### Step 8 and after the booking

Nothing to run; Step 7's command already added the calendar event.

## Important

- **Never** break the [Talking to the user](#talking-to-the-user-required) rules: raw URLs only,
  no tables, no technical jargon.
- **Never** substitute a different payment command than `next_command` after `pay request`.
- **Never** run the first `pay poll` or `link-auth poll` in the foreground, use `pay request
  --then-complete` or `link-auth connect --wait` in interactive flows, ask the user to reply
  "approved" or "done", or show the Link approval URL more than once. Start the poll in the
  background and post the URL in the same turn.
- **Never** continue an existing session without asking the user first. When `sessions list`
  shows an incomplete session that matches the request, or when `flow init` returns
  `session_active`, you must stop and ask the user whether to continue or start fresh.
  Using `flow status` to inspect the active session and then proceeding silently is
  forbidden — the user must always make this choice explicitly.
- **Never** treat `sessions list` as booking history. It is only unfinished flows and
  upcoming bookings on this device. Do not answer "show my past bookings" from leftover
  rows, and do not claim you queried the business for a history you do not have.
- **Never** call USP `/availability/query`, `/bookings`, `/availability/holds`, or
  `/bookings/*/confirm-payment` with raw `curl`, `httpx`, or any HTTP client. Always drive
  booking through `booking-agent flow ...`.
- **Never** use placeholder or chat-entered personal details (name, phone, email, address).
  Always derive them from Link via `link-cli` — see [Personal identification (always from Link)](#personal-identification-always-from-link).
  During booking, use `booking-agent flow buyer resolve`. Addresses come only from `flow
  search-area` after the user confirms them; `flow book` sends the confirmed address for
  `at_buyer_location` services. Never put it in `--notes`.
- **Never** auto-pick a slot.
- **Never** pass `--mismatch-confirmed`, `--details-confirmed`, `--timezone-confirmed`,
  `--user-confirmed`, `--policies-accepted`, or `--allow-duplicate` before the user has answered
  that exact question. Never paraphrase or hide a policy line or link.
- **Never** read or delete the CLI's state files or `link-cli` config; use `flow purge`.
- **Never** run `sleep` or a fixed wait alongside a `booking-agent` command; wait for the command to
  exit.
- **Never** skip the calendar gate.
- **Never** guess a calendar route or token variable name, or pass a token value on the command
  line.
## Errors

Every error exits 1 with `data.error.code`, `data.error.message`, and a `next_command`; a code not
listed here recovers by following `next_command`. These gate errors need nothing more: `approval_not_surfaced_to_user`, `booking_canceled_after_payment`, `buyer_email_invalid`, `buyer_not_from_link_cli`, `calendar_gate_not_completed`, `checkout_prereq_not_completed`, `context_required`, `discover_radius_required`, `link_address_not_found`, `missing_network_id_for_spt`, `payment_already_captured`, `reschedule_at_business`, `reschedule_booking_not_active`, `reschedule_failed`, `reschedule_fee_unsupported`, `reschedule_price_changed`, `reschedule_price_unknown`, `reschedule_same_slot`, `reschedule_unauthorized`, `search_area_input_invalid`, `search_area_not_user_confirmed`, `search_area_unconfirmed`, `service_address_unconfirmed`, `service_details_changed`, `slot_not_selected`, `spend_request_replacement_required`, `timezone_confirmation_required`.
Payment codes are in Step 6c; the `reschedule_*`
codes all end the same way: show the raw `booking_url` and say the user can change it on the
business's own page (`reschedule_price_changed` and `reschedule_same_slot`: ask for another slot;
`reschedule_failed`: run `reschedule availability` again).

| Error code | Cause | Recovery |
|---|---|---|
| `link_auth_gate_not_completed` | `search-area propose` or `buyer resolve` while signed out of Link | Sign the user in (`link-auth connect`, then `link-auth poll` in the background and post the URL in the same turn), then retry |
| `link_auth_expired` | Link session expired before payment | Sign the user in the same way, then retry |
| `link_auth_timeout` | User did not approve the Link sign-in in time | Run `next_command`: `link-auth poll` again in the background (same link, do not re-post it) or `link-auth connect` (new link, post it once, then poll in the background) |
| `link_auth_not_started` | `link-auth poll` with no sign-in waiting | Run `booking-agent flow link-auth connect`, start `link-auth poll` in the background, and post the URL |
| `ucp_native_required` | Business does not support the native v2 checkout path | Tell the user this business cannot be booked here yet and offer another result |
| `policies_not_accepted`, `booking_confirmation_required`, `mismatch_not_surfaced`, `purge_not_surfaced` | A confirmation flag came before its content was shown, or the content changed | Post `user_message`, ask, **STOP**; run `next_command` only after a yes |
| `calendar_offer_not_shown` | Calendar answer before `calendar ask` | Run `calendar ask` |
| `no_bookable_results`, `profile_resolve_failed` | No business, or the picked one, can take bookings | Post `user_message`; offer other results |
| `shared_payment_token_not_issued` | Link approved the spend request but returned no shared payment token, so nothing was charged | Post `user_message`, re-run `pay ucp-complete` once; if it fails again, tell the user this business cannot take a Link payment right now and do not ask them to approve again |
| `checkout_total_unconfirmed` | Checkout total (tax, fees, discounts) differs from the quoted price and the user has not approved it | Show service price, tax, and total from `data.merchant_quote`, get approval, then re-run `pay request` with `--total-confirmed` |
| `payment_timing_changed` | Live catalog timing no longer matches the quote | Stop, re-quote, and get a new approval |
| `payment_timing_not_supported` | Live catalog declares another unsupported timing | Stop and explain that this service cannot be booked here yet |
| `mandatory_acceptance_pending` | Merchant requires policy acceptance or another non-payment step before checkout | Show `continue_url` / policy links from the error; wait; retry `flow book` (or run `next_command` when it came from `pay ucp-complete`); never accept for the user |
| `service_not_available_in_area` | Service coordinates are farther than the `--radius-km` you sent | Widen search or pick another result; do not bypass with direct profile URL |
| `profile_unreachable` | Business profile URL unreachable | Surface the URL from `data.error.message` to the user |
| `agent_profile_unresolvable` | The business could not fetch the booking assistant's platform profile, so nothing was booked | Post `user_message`, then run `next_command` once (it reuses the same idempotency key, so it cannot double-book); if it fails again, offer another time or business |
| `reschedule_not_actionable` | Session is not a confirmed upcoming booking from this device | Resume the right booking from `sessions list`; past or other-device appointments move only on the business's page |
| `reschedule_not_allowed` | Business policy does not allow moving this booking | Show the raw `booking_url` and say they can change it with the business |
| `session_store_too_new` | Saved bookings on this device were written by a newer version of the assistant | Run `next_command` (`booking-agent update check`) and follow it; never delete or edit the saved state |
| `update_not_confirmed` | `update` ran without `--user-confirmed` | Ask the user first; pass `--user-confirmed` only after they agree |
| `update_session_in_progress` | `update` ran while a booking is unfinished | Finish or cancel the booking first, then offer the update again |
| `update_failed` / `update_not_verified` | The update did not install, did not start, or could not be shown to come from the official release (`data.reason`); nothing unverified is installed | Tell the user the update did not take; carry on with the installed version |
| `installation_unverified` | The installed CLI files do not match the signed official release, so it will not book or pay | Tell the user in plain words the assistant needs reinstalling; run `data.reinstall_command` only with their yes, then start the booking again. Never book another way |
| `skill_unverified` | The SKILL.md you loaded does not match the signed skill this CLI was released with, so it will not book or pay | Follow `data.skill_text` from now on. Tell the user plainly that the assistant's instructions file was changed and ask whether to restore the official one; only with their yes run `data.replace_command`, then run `flow init` again with `--skill-path`. Never book another way |
| `skill_path_invalid` | `--skill-path` named a file that is missing, unreadable, or not a booking-agent SKILL.md | Run `flow init` again with the real path of the file you loaded, or without `--skill-path` if you do not know it |
| `skill_output_refused` | `skill print --output` named a file that is not the loaded skill | Follow `data.skill_text` from `booking-agent skill print` instead |

### Notes to the assistant team

When a failure cannot be fixed from here, the output sets `support_report_offered: true` and
`next_command` to `booking-agent flow support-report`. Show that output's `user_message` (when set),
then run `next_command` in the same turn. It returns `awaiting_user: support_report_consent`
with the question in `user_message`.

- Show that question word for word and **STOP**. Do not rephrase it or add details.
- On yes, run the printed `next_command` (`booking-agent flow support-report send --user-confirmed`).
  On no, run `booking-agent flow support-report decline`. Either way, continue with the
  `next_command` it returns.
- The note is prepared in advance and sent as is. Never write it yourself, pick an address,
  attach anything, or send mail.
- If sending fails, show `user_message` and continue with `next_command`. Try again only if
  the user asks, by running `booking-agent flow support-report` and following it.

## Booking authorization

The CLI authenticates every privileged step (booking, changing, cancelling, payment) and refuses
rather than falling back to anonymous requests. Searching, catalogs, and availability are public.

- A business can be browsable but not bookable. If booking is refused for that reason, say plainly
  that this business cannot be booked through you yet and offer the alternatives. Do not retry or
  describe the mechanism.
- Cancelling uses a credential scoped to that one booking, kept on this device. If it has expired
  or the business rejects it, there is nothing to retry: give the user the `booking_url` and say
  they finish the cancellation on the business's own page.

Never mention any of this machinery. "I've cancelled that for you" or "you'll need to cancel this
one on their website, here's the link" is all they need.

## Cancel upcoming bookings

To cancel one booking, resume its session and run `booking-agent flow book cancel`. To cancel several,
or to clear out unfinished requests, use the two-stage bulk command. Never loop `sessions resume`
and `book cancel` in the shell.

After a successful cancel, post `user_message` as is (raw URLs). It already says:

- that the booking is cancelled, and whether the money paid was refunded;
- when it was not refunded in full, numbered steps from `refund_follow_up.steps`: where to reach
  the business (`contact_url`, the manage page or the business's website), the exact amount to
  ask for (`outstanding_display`), and the booking and order references;
- whether the Google Calendar event was removed (`data.calendar.status` is `deleted` or
  `already_missing`). When it is `failed`, the message asks the user to delete the event
  themselves and gives its link. Once they reconnect Google Calendar, you may retry with
  `booking-agent flow calendar delete-event`.

Never shorten the refund steps to "the charge was not refunded". The user needs to know how to
get the money back.

1. Preview (read-only, nothing changes):

   ```bash
   booking-agent flow sessions cancel-open
   ```

   `data.bookings` lists upcoming appointments that can still be cancelled, `data.unfinished`
   lists requests that never booked, and `data.held` lists requests with a payment in progress.
   Rows keep `user_request`, business, and slot times. Cancelled, completed, no-show, and past
   bookings are left out.
2. Show the user the rows in everyday words and ask which ones to cancel, then **STOP**. Pick only
   rows that match what the user is asking about. **Never** assume that rows that look like tests,
   labs, or other requests belong to this request, and never select rows on your own. Skip the
   question only when the user already explicitly said to cancel all of them.
3. Run the confirmed command with exactly the user's choice:

   ```bash
   booking-agent flow sessions cancel-open --user-confirmed --session-id <id> [--session-id <id> ...]
   # only when the user said all of them:
   booking-agent flow sessions cancel-open --user-confirmed --all
   ```

   Add `--forget-unfinished` only when the user agreed to drop unfinished requests. Without it,
   unfinished rows are never removed. `held` rows are never removed.
4. Report each row in `data.results` by `outcome`. One failure does not stop the others.
   - `canceled`: post the row's `user_message` as is. It includes the refund steps from
     `refund_follow_up` (the business's `contact_url`, the amount to ask for, and the booking and
     order references) and the calendar cleanup result. Never show an order or checkout page URL
     for the refund; those pages only work right after payment.
   - `needs_human`: show the raw `booking_url` (or the first `cancel_urls` entry) and say they
     finish the cancellation on the business's page.
   - `failed`: say it did not go through, and share the same link when present. Do not retry on
     your own.
   - `forgotten`: the unfinished request was removed from this device.
   - `skipped`: nothing was done; `skip_reason` says why.

| Error code | Cause | Recovery |
|---|---|---|
| `cancel_unauthorized` / `cancel_forbidden` | Business rejected the booking credential (HTTP 401 / 403) | Show the raw `booking_url` or first `cancel_urls` entry; do not retry |
| `cancel_failed` | Any other cancellation failure (`http_status` when the business answered) | Tell the user it did not go through; share `booking_url` / `cancel_urls` when present |
| `cancel_open_selection_required` | `--user-confirmed` without `--session-id` or `--all` | Preview, ask the user, then pass their choice |
| `cancel_open_selection_conflict` | Both `--session-id` and `--all` were passed | Pass one or the other |

## Reschedule an upcoming booking

When the user wants to move (reschedule, change the time of) an appointment you booked on this
device, move the existing booking. **Never** run `flow init` or book a new appointment for this,
and never cancel and rebook.

1. `booking-agent flow sessions list` and pick rows with `actionable: true` and a `booking_id`. If more
   than one could match, ask the user which appointment and **STOP**.
2. `booking-agent flow sessions resume --session-id <id>` for that booking.
3. `booking-agent flow reschedule start`. It checks that the business allows moves and that this
   booking is still upcoming.
4. If the user has not said when they want it instead, ask them and **STOP**. Use the same time
   reasoning as booking (a specific day → `--target-date`, a window → `--from`/`--to`).
5. `booking-agent flow reschedule availability [--target-date YYYY-MM-DD | --from ... --to ...]`.
   Present only slots with `selectable: true`, as a numbered or bulleted list, and **STOP** for
   the user's pick. **Never** auto-pick. Times follow the same timezone rules as Step 4: show
   `business_start_display`, and when `data.timezone_notice` is present post it once above the
   list and add `buyer_start_display` in parentheses. If the time the user asked for is not in
   the list, say only that it is not open now. Availability changes between queries, and the
   CLI does not report why a slot is gone, so do not offer a cause.
6. `booking-agent flow reschedule select --slot-id <chosen by user> --user-confirmed`.
7. On `awaiting_user: reschedule_confirm`, ask the user to confirm the move in everyday words
   and **STOP**. Show the two times on separate labeled lines, exactly as the prompt gives them
   (business time first, the user's time in parentheses when shown), for example:

   ```
   Current time: Tue, Oct 13, 2026, 11:30 AM MST (America/Phoenix) to Tue, Oct 13, 2026, 12:30 PM MST (America/Phoenix)
   New time: Tue, Oct 13, 2026, 1:15 PM MST (America/Phoenix) to Tue, Oct 13, 2026, 2:15 PM MST (America/Phoenix)
   ```

   Never fold both ranges into one "from ... to ..." sentence. Only after they agree, run
   `booking-agent flow reschedule apply --user-confirmed`. If they change their mind, run
   `booking-agent flow reschedule abort`.
8. After a successful apply, tell the user the new time. When `next_command` is
   `booking-agent flow calendar update-event`, run it and share the raw `html_link` if one is
   returned. If that calendar step fails, the appointment is still moved; say so plainly.

Moves are supported only to a slot at the **same price**, and only when the business charges no
reschedule fee. Never offer to pay a difference or a fee, and never promise a refund. When a move
cannot be done here (`reschedule_not_allowed`, `reschedule_fee_unsupported`,
`reschedule_at_business`, or a rejected credential), show the raw `booking_url` (or the first
entry in `manage_urls`) and tell the user they can change it on the business's own page.
Appointments that were not booked on this device can only be changed on the business's page.

## Log out / disconnect accounts

When the user asks to log out, sign out, or disconnect Link and Google Calendar, run:

```bash
booking-agent flow logout
```

Tell them in everyday language that they are signed out on this device. Upcoming bookings stay saved so they can still cancel them. Do not send them to `link-cli` directly.

To forget one **upcoming** booking from this device (not the same as logout, and not a
history delete at the business):

```bash
booking-agent flow sessions forget --session-id <id>
```

To wipe all of this assistant's data on this device, only when asked, run `booking-agent flow
purge`. It deletes nothing yet: post `user_message`, **STOP**, and run `next_command` only after a
yes.

Use `booking-agent flow sessions list` only to find unfinished flows or upcoming bookings
(`actionable: true`, `booking_id`, slot times). Do not use it as a past-booking list.

## Settings

Two settings matter to a host (`booking-agent config show` lists all): `BOOKING_AGENT_BUYER_DEVICE_IP` (the buyer's device IP when it
observed one) and `BOOKING_AGENT_USP_AUTH_SCOPED_CREDENTIAL` (`required` by default; see [Booking
authorization](#booking-authorization)). Faults the user never sees are reported automatically,
with no user data; `BOOKING_AGENT_FAULT_REPORTS=0` turns that off.

## Further docs

- Stripe link-cli spend-request commands: https://github.com/stripe/link-cli
- USP spec: https://usp-protocol.dev/specification

## License

This skill and the Booking Agent source are released under the Apache License 2.0, Copyright 2026 Wix.com. Complete terms: https://github.com/wix/booking-agent/blob/master/LICENSE

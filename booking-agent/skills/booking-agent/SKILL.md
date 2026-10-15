---
version: 0.46.0
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
   When the user first asks to book something, run Step 1 setup (`sessions list`, `flow init`,
   `prereq check` when needed) silently. If they are not signed in to Link, the only thing
   you send before that is the sign-in link (see [Link sign-in](#link-sign-in-automatic));
   when the background sign-in poll exits, carry on without waiting for a reply. When the service happens at a place (at home, at a
   shop, in a named city), the opening message **asks the user to confirm the search area**
   that `flow search-area propose` returned, for example "Should I look for AC repair
   technicians near 3194 West Rome Street, Yuma AZ, or somewhere else?" Nothing is searched until
   they confirm (see [Search area](#search-area)). Online or remote services skip the area
   question; the opening line then just mirrors the request ("Understood, looking for an
   online Spanish tutor..."). Before Step 2, apply the required time-preference gate: if time
   intent is unknown, ask when they want the service in the same message and stop. Do not post
   progress updates like "checking the environment", "tools are ready", or "no previous
   bookings". Send the opening message **once per booking**. If the user's next reply only
   answers a question (for example the area or the time), do not repeat or rephrase it; go
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

When the user asks to book a service for the first time in the conversation:

1. **Silent setup** - run session check, init, and prereq without chat. When `init` returns
   `next_command: booking-agent flow link-auth connect`, sign the user in first (see [Link
   sign-in](#link-sign-in-automatic)). The sign-in link is the only message before the opening
   message, and you never ask whether they want to sign in.
2. **Area, when the service happens at a place** - run `flow search-area propose` before your
   first chat message. Do not narrate it.
3. **One opening message** - mirror the request and ask what is still unknown in one message:
   the area question from `propose`, plus the time question when time intent is unknown.
   Examples:
   - Home AC repair, no time given -> "Should I look for AC repair technicians near 3194 West
     Rome Street, Yuma AZ, or somewhere else? And when would you like them to come?"
   - Haircut in Austin -> run `search-area set --address 'Austin'` first, then "Should I look
     for haircuts in Austin, TX?"
   - Online tutoring next week -> "Understood, looking for online Spanish tutors next week..."
     and run discover with no area.
4. **Next message** - after they confirm the area (and the time is known), show the service
   options from discover, or ask whether to resume an incomplete booking.
   Never explain what you did behind the scenes to get there.

### Narration pattern (every turn)

After each successful step (except silent setup above), write 1-2 short sentences for the user:

1. **What happened** in plain terms (e.g. "I found three AC repair options near your home.")
2. **What's next** or what you need from them (e.g. "Which one would you like?")

When waiting on them, ask one clear question. Do not explain how you got the information.

Say each fact once. Do not restate a recap, a price, or a status the user already saw unless it
changed. Do not narrate progress between gates ("now checking availability", "still
working").

### Background commands

- When a background command finishes, report only its **final** outcome. If a newer command
  already superseded it (for example an older poll failed but a later one confirmed the
  booking), say nothing about the older one.
- Never run `sleep`, a fixed wait, or a timer loop next to a foreground `next_command`, and
  never poll a background command on a fixed schedule. Wait for the command itself to exit;
  its final JSON is the result.
- Relay each `user_message` once. A later output with the same message is not news.

### When they explicitly ask technical questions

Only then may you use terms like CLI, UCP, or session. Keep explanations brief.

## Installing

Install the `booking-agent` CLI by following the [Booking Agent README](https://github.com/wix/booking-agent/blob/master/booking-agent/README.md). If
`booking-agent --help` does not run, your harness has not provisioned it — ask the user to install
the skill, then try again.

`link-cli` is required for buyer identity and spend-request create/approve — install via the
[link-cli skill](https://github.com/stripe/link-cli/blob/main/skills/create-payment-credential/SKILL.md) or your harness's link-cli provisioner.

Nothing needs configuring for a basic booking: every setting has a shipped default. The user
never configures anything for the calendar either: you work out at runtime how this runtime can
reach Google Calendar (see [the calendar gate](#step-3-select-business-and-resolve-calendar-gate)).
**Never** write tokens, keys, or personal details into any env file. `booking-agent config show`
prints each effective setting and where it came from; `booking-agent config init` creates
`~/.booking-agent/.env` for an operator to edit.

**Keep state between conversations (only when this runtime needs it).** Inspect the runtime you
are in. When its filesystem starts fresh in each conversation and it gives you a folder that
survives between conversations, run this once per conversation, before `booking-agent flow
sessions list`:

```bash
booking-agent runtime prepare --persistent-root <absolute folder that survives>
```

It links the booking state and the Link sign-in into that folder, so saved bookings, calendar
preferences, and the sign-in carry over. Add `--link <runtime folder>=<persistent folder>` once
for each extra cache folder the runtime asks you to keep (a relative persistent folder is placed
under the root). When the runtime keeps its files anyway, or offers no surviving folder, skip the
command. Do not tell the user about it. If it fails, continue the booking without it.

**Right after installing, sign the user in to Link** as described in [Link
sign-in](#link-sign-in-automatic). It works with no booking in progress. Do not wait for the
user to offer to sign in.

## Link sign-in (automatic)

Every booking needs the user signed in to their Link account, because their name, contact
details, saved addresses, and payment all come from it. **You start the sign-in yourself.**
The user never has to ask for it, and you never ask them to tell you when they have approved.

Sign the user in:

- right after installing this skill,
- at the start of every booking, when `flow init` returns `next_command: booking-agent flow
  link-auth connect` (it does whenever the user is signed out), before any question or search,
- whenever any command returns `next_command: booking-agent flow link-auth connect` (for
  example `link_auth_gate_not_completed` or `link_auth_expired`).

Do it in **one turn**:

1. Run `booking-agent flow link-auth connect`. It returns at once.
   - Already signed in (`data.authenticated: true`): say nothing, run `next_command`.
   - Otherwise it returns `awaiting_user: link_auth_approval` and the sign-in URL in
     `user_message`.
2. In the same turn, start `booking-agent flow link-auth poll` **in the background**
   (`run_in_background`), the same way as `pay poll`. It waits until the user approves in Link
   and exits with `data.authenticated: true` the moment they do.
3. Post the raw URL to the user with one short sentence, for example: "Please sign in to your
   Link account here so I can use your saved details: https://app.link.com/device/setup?code=...
   I'll continue as soon as you approve." Then end the turn so the message is delivered.
   **Never** run the first `poll` in the foreground: the turn would block until approval and
   clients that deliver text only when the turn ends would never show the link.
4. When the background `poll` exits, run its `next_command` right away and carry on with the
   booking (for example, propose the search area and send the opening question). Do not wait
   for a chat reply and do not repeat the link. **Never** ask the user to tell you they signed
   in.
5. If `poll` ends with `link_auth_timeout`:
   - `next_command` is `link-auth poll`: the same link still works. Start `poll` again in the
     background without posting the link again.
   - `next_command` is `link-auth connect`: the link expired. Run `connect`, post the new link
     once, and start `poll` in the background.
6. If the user says they signed in while no `poll` is running (the link was already shown),
   run `link-auth poll` in the foreground. It returns at once when they are signed in.

Never call `link-cli auth login` directly, and never use `link-auth connect --wait` (scripts
only): like `pay request --then-complete`, it blocks before the link reaches the user.

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
2. If `awaiting_user` is set:
   - **`link_spend_approval`:** start `booking-agent flow pay poll` **in the background**
     (`run_in_background`) and, in the **same turn**, post `user_message` to the user as the
     raw URL (before any other narration; never markdown `[label](url)`). Never block the turn
     on the poll before the user has seen the URL: a foreground poll keeps the link hidden
     until approval, so the booking looks stuck. The poll waits for Link approval, completes
     checkout, validates skill compliance, and creates a calendar event when needed. When it
     exits, tell the user they are booked. Do not wait for a chat reply. The user approves in
     the Link app, not in chat.
   - **`link_auth_approval`:** start `booking-agent flow link-auth poll` **in the background**
     (`run_in_background`) and, in the **same turn**, post `user_message` to the user as the
     raw URL (never markdown `[label](url)`). Never run the first poll in the foreground. When
     it exits, run its `next_command` without waiting for a chat reply. See [Link
     sign-in](#link-sign-in-automatic).
   - **`support_report_consent`:** show `user_message` to the user word for word (when it is
     set) and **STOP**. On yes, run `next_command` exactly as printed. On no, run
     `booking-agent flow support-report decline`. See
     [Notes to the assistant team](#notes-to-the-assistant-team).
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

**Payment is two steps for agents.** Default `pay request` returns immediately (it does not
block on polling).

1. Run `pay request`.
2. In the same turn, start `booking-agent flow pay poll` **in the background**
   (`run_in_background`) and post `user_message` to the user as the raw URL (never markdown
   `[label](url)`). Do not wait for the user to reply in chat, and never block the turn on the
   poll before the URL is visible to the user.

`pay poll` acks that the URL was shown, polls Link until approved, completes checkout, runs
`validate-skill-compliance`, and `calendar create-event` when applicable — then emits a final
prompt to tell the user they are booked.
`pay ucp-complete` refuses to run until `pay poll` (or
`pay ack-surface`) has started — use `pay poll` instead of calling them directly. Never use
`--then-complete` in interactive agent flows.

If the user already approved but checkout did not finish (the URL was already shown), run
`booking-agent flow pay poll` in the foreground to resume polling and finish the booking.

## Core flow

Copy this checklist and track progress:

- Step 1: Initialize (check unfinished or upcoming local sessions, then init)
- Step 2: Discover services
- Step 3: Select business and resolve calendar gate
- Step 4: Check availability and select a slot
- Step 5: Authenticate Link wallet and resolve buyer
- Step 6: Book and pay
- Step 7: Validate compliance
- Step 8: Add booking to Google Calendar (when user chose events mode)

### Step 1: Initialize

**Step 1a — Check for an existing session:**

```bash
booking-agent flow sessions list
```

Scan `data.sessions` for any entry where `flow_phase` is **not** `CONFIRMED` or `FAILED`
and whose `user_request` is semantically similar to the current request. If such a session
exists, **STOP** and ask the user in plain language (translate `flow_phase` internally; never
say "session" or phase names aloud). For example:

> "I found an earlier booking request for '[user_request]' that we didn't finish. Would you
> like to pick up where we left off, or start a new booking?"

**Do not skip this question under any circumstances.** Do not infer the answer from context,
do not treat a matching request as implicit consent to continue, and do not use
`booking-agent flow status` to "peek" at the active session and proceed silently. The user must
answer explicitly every time.

- **Continue**: run `booking-agent flow sessions resume --session-id <id>`, then skip ahead to the
  step that matches the resumed `flow_phase`.
  - If `resume` fails with `session_not_found`, tell the user and ask again whether they want
    to continue with the currently active session (use `booking-agent flow status` to show it) or
    start fresh with `--force`.
- **Start fresh**: proceed to Step 1b with `--force`.

If `flow init` itself fails with `session_active` (you skipped the list check or the active
session wasn't in the list), **STOP immediately** — do not continue with the active session
without asking. Show the user the blocked session details from the error and ask the same
continue-or-fresh question before taking any further action.

`sessions list` is **local and operational**, not a booking history API. Use it only to
resume an unfinished flow or to find **upcoming** bookings on this device (`actionable:
true`, with `booking_id` and slot times) that you can still cancel or move. Do not present the list
as the user's past bookings, do not say you looked them up with the business, and do not
rebuild a history from old rows. Past appointments are not queryable here.

If the user asks what they booked last week, for a past appointment list, or for live
status of a finished visit: say you can see upcoming appointments you booked here. Older ones
live in their confirmation email or on the business's own page (`booking_url` if you still
have it). Do not invent a lookup.

**Step 1b — Create a new session:**

```bash
booking-agent flow init --user-request "<user's exact request>"
```

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

**Time-preference gate, required before every discover:** Determine whether the user has
already said when they want the service in the current request or an earlier turn of this
booking. If not, **STOP** and ask in plain language when they want it. Offer examples such as
as soon as possible, a day or time window, a deadline, several alternatives, or that any time
is fine. Do not run discover and do not invent a range before they answer.

Pass time flags on every discover for this booking only when the user has a real time
constraint:

- First available, ASAP, or soonest: `--desired-from <current RFC3339 time>`. Leave sooner
  preference unset or explicitly true.
- Any time is fine, whenever, or no time window: omit all `--desired-*` flags. This is not
  ASAP and must not be encoded as a range starting now. Leave sooner preference unset unless
  the user also says earlier versus later does not matter, then pass
  `--no-prefer-sooner-availability-slots`.
- A specific clock time where the next grid tick is acceptable: `--desired-at <RFC3339>`.
- A calendar date, time window, deadline, or specific time with a strict latest acceptable
  start: `--desired-range <startRfc3339>,<endRfc3339>`. The end is the latest acceptable
  start, not the end of the appointment.
- After a date or event: `--desired-from <RFC3339>`.
- Several alternative windows: repeat `--desired-range` once per window.
- A later period such as after payday or during a trip: encode that period directly. Do not
  disable sooner preference to mean later is better.

For date-only preferences, use that date's bounds in the service timezone when known.
Otherwise use the search-area or buyer-local date. Ask if the timezone or wording is
ambiguous. If the user later changes their time policy, run a new discover with new flags.
Do not reshuffle an existing page.

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

**STOP.** Show the returned services to the user and wait for them to choose one before
continuing. Show each row's `display_line` verbatim, one numbered line per row (it names the
service, the business, price, duration, and where, for example "AC Repair by HVAC-Koby, $1.00,
1 hr, comes to your address"). Never drop the business name. Present `data.services` in the
exact order returned by default (option 1 is the top registry hit). Never re-sort by price, distance, perceived fit, raw
`next_available_date`, hint age, or bitmap contents.

**Less-direct results:** Never omit, filter, or reorder results, even when some look
irrelevant. Present direct matches plainly. When a result's returned data objectively
mismatches the request, keep it in its registry position and label it as a less-direct
alternative with the specific mismatch, for example "(at the business location, not a home
visit)" for `channel.type: at_business_location` when the user needs a home visit, or
"(duct cleaning, not AC repair)" when the service name is clearly a different service. Base
labels only on returned fields such as `channel.type` and `service_name`. Do not infer
categories, add filters, or compute your own relevance score.

**Where each value comes from.** Each row's `field_sources` says whether price, duration,
channel, and locations come from the business's live catalog (`live_catalog`) or only from the
search index (`registry`). Present the row's values as returned. When `discovery_mismatches`
is present, the search index and the live catalog disagree (for example a different price);
the live value is what the business will book. Never guess which is right and never present
the search-index value as current.

**Choosing a less-direct result.** When the user picks a row you labeled as a less-direct
alternative, pass the label you showed on `business use`:

```bash
booking-agent flow business use --profile-url <url> --service-id <id> --less-direct '<label shown>'
```

The CLI returns `awaiting_user: intent_mismatch_confirmation` without selecting it. Ask the
user to confirm that this service still works for them despite the mismatch (say the mismatch
plainly, e.g. "they work at their shop, not at your home") and **STOP**. Run `next_command`
(it adds `--mismatch-confirmed`) only after they agree.

Availability hints are advisory and never replace a live availability query:

- If `rank_signals` is omitted, do not infer availability from result order.
- If `hint_usable` is false and coverage, density, and soonness are null, availability was
  unknown for ranking. Keep the result. It is not unavailable.
- If `hint_usable` is true and coverage is null, the result is neither a known time match nor
  a known mismatch.
- Coverage zero means known non-overlap in the snapshot, but live availability must still be
  checked before telling the user their requested time cannot be booked.
- Never collapse null into zero or derive scores from a prose summary.
- A page-local re-order is optional only when using the returned `rank_signals`. Keep
  `hint_usable: false` results. A changed global policy requires a new discover.

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

**Pick the calendar route from what you observe at runtime.** The CLI cannot see how this
runtime reaches Google Calendar, so you tell it on `calendar connect`. Decide before you ask
the user anything:

- **`--route connector`**: your platform can connect Google Calendar on the user's behalf and
  hands the access token to the commands you run in an environment variable. Pass that
  variable's **name** with `--token-env <NAME>`. Never pass, print, or store the token itself,
  and never copy it into an env file. Look up the name each conversation; do not assume it.
- **`--route local-oauth`**: `data.local_oauth_configured` is true and you can relay a sign-in
  link to the user.
- **Neither**: run `booking-agent flow calendar skip --session-only` without asking the user
  about the calendar, then continue to availability.

The CLI stores the route and the variable name for later commands and later bookings, and it
never guesses one: `calendar connect` without `--route` fails with `calendar_route_required`,
and `--route connector` without a valid `--token-env` fails with `calendar_token_env_required`.

`calendar ask` outcomes:

- **Connected or remembered preference:** The CLI auto-applies conflict checking (or a saved
  skip) and advances to availability. Briefly tell the user you are using their calendar when
  conflict checking is active.
- **Saved calendar access expired** (`awaiting_user: calendar_reconnect`): The user previously
  connected Google Calendar but that access is no longer valid. Do **not** ask the three
  calendar questions again. Ask only the two choices in `data.calendar_reconnect_choices`:
  reconnect for the same use (`data.remembered_calendar_mode`), or continue this booking
  without calendar checking. Run the matching `command` exactly as printed (it already carries
  the stored route) or `booking-agent flow calendar skip
  --session-only`, which keeps the saved preference.
- **Saved access expired and reconnect unavailable** (`data.calendar_checking_unavailable`):
  The CLI already skipped calendar checking for this booking only. Tell the user in one plain
  sentence that calendar checking was unavailable, then continue.
- Never quote calendar access tokens or Google token URLs. `data.connection_problem` holds
  only sanitized codes (`invalid_token`, `tokeninfo_http_error`, `tokeninfo_unavailable`).
- **Not connected yet** (`awaiting_user: calendar_consent`): When a route is available, ask the
  user which they prefer and run the matching command with your route. With no route, skip
  as above without asking.

```bash
booking-agent flow calendar skip                                                     # neither
booking-agent flow calendar connect --mode freebusy --route connector --token-env <NAME>  # free/busy only
booking-agent flow calendar connect --mode events --route connector --token-env <NAME>    # + post-booking event
booking-agent flow calendar connect --mode freebusy --route local-oauth                   # same, by sign-in link
booking-agent flow calendar connect --mode events --route local-oauth
```

The CLI remembers explicit user choices (`last_calendar_state` / `last_calendar_mode`) for
future bookings when Google Calendar stays connected.

**Connector route.** `calendar connect` returns at once:

- Connected: it completes the gate and `next_command` is `booking-agent flow availability`.
- `awaiting_user: calendar_connector` (`data.error: calendar_not_connected`): the variable is
  empty or holds no valid token, so the user has not connected Google Calendar to you yet. Use
  your platform's own way of connecting Google Calendar, ask for the access in
  `data.scope_request`, and when the connection is in place rerun `data.retry_command` (the same
  command). Never show a sign-in link from this CLI for this route. If your platform cannot
  connect it, run `booking-agent flow calendar skip --session-only`.
- `calendar_insufficient_scope`: the connection lacks the access the chosen mode needs.
  Reconnect through your platform with `data.scope_request`, then rerun the same command.

**Local OAuth route.** `calendar connect` returns at once with `awaiting_user: oauth_approval`
and `data.auth_url`. The CLI does **not** open a browser itself.

1. **Present the raw `auth_url` to the user** (never markdown `[label](url)`) and ask them to
   open it and approve the Google Calendar access described in the CLI `prompt` (free/busy only
   vs events). Never open the browser for them; always relay the URL. Tell them how long the
   link works (`data.link_valid_minutes`, about 10 minutes by default).
2. In the same turn, run `booking-agent flow calendar poll` without blocking your turn (in the
   background when you can). Each `poll` returns within its `--wait` budget (2 minutes by
   default). While it returns `awaiting_user: oauth_approval` with `data.status: pending`, run
   `poll` again without posting the link again. When it connects, run `next_command`.
3. On `calendar_oauth_timeout` or `calendar_oauth_failed`, post the error's `user_message` and
   **STOP**. Do not start a new link on your own. Run the `new_link` command from
   `data.choices` only if the user asks for a new link, or `booking-agent flow calendar skip
   --session-only` if they want to continue without calendar checking.
4. `calendar_insufficient_scope` from `poll` means the user approved less access than the mode
   needs. Ask whether they want a new link; do not start one on your own.
5. `calendar_oauth_not_started` means no sign-in is waiting. Run `booking-agent flow calendar ask`.

The sign-in listener stops by itself when the link expires. `calendar skip` stops it too, and
`booking-agent flow calendar cleanup` stops it when the user abandons the sign-in.

If `calendar connect` fails with `calendar_oauth_not_configured`, local sign-in is not set up on
this device: use the connector route when your platform has one, otherwise run
`booking-agent flow calendar skip --session-only`. If it fails with
`calendar_callback_port_busy`, no link was created; run `booking-agent flow calendar skip
--session-only`.

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

**Timezones (show both, ask once).** Availability windows and every slot time are in the
**business's** timezone (`data.business_timezone`). The user's timezone comes only from the
default shipping address on their Link account; **never** take it from this device, the agent
host, a VPN, a browser, or a cloud region, and never guess it from chat.

- Show each slot from its `business_range_display` (for example "Tue, Oct 13, 10:00 - 11:00 AM
  MST"). Never re-convert the raw `start` / `end` yourself.
- When `data.timezone_notice` is present, the user's address is in a different timezone. Post
  the notice **once**, right above the list, then show each slot as the business time first with
  the user's time in parentheses (`buyer_range_display`; the full `buyer_start_display` is there
  too). Example:

  ```
  I'm detecting a timezone difference: this business books in Jerusalem time (Asia/Jerusalem)
  and your address is in Phoenix time (America/Phoenix), so please note that the times below are
  the business's local time, with your local time in parentheses.

  1. Tue, Oct 6, 5:00 - 6:00 PM IDT (7:00 - 8:00 AM MST)
  2. Tue, Oct 6, 5:30 - 6:30 PM IDT (7:30 - 8:30 AM MST)
  ```

- When a slot has `calendar_display`, the user's Google Calendar uses yet another timezone. Add
  it in parentheses as "(8:00 - 9:00 PM on your Google Calendar)".

- There is no separate timezone question. Picking a slot from that list, plus the one booking
  confirmation in Step 5b, covers it. The only exception is the time confirmation below.
- When `data.timezone_fallback_note` is present, the user's timezone could not be found. Add the
  note as one line under the list and continue in the business's time; do not ask the user for
  their timezone.

**Time confirmation (when the CLI asks for it).** When `slots list` returns
`data.timezone_conflict` (the business's published timezone does not match where the service
is), or `data.buyer_timezone_unresolved_for_home_visit` (a home visit and the user's local time
is unknown), `next_command` includes `--timezone-confirmed`. Say above the list that the times
are in the business's timezone as published and, for a conflict, that this looks inconsistent
with the service location. When the user picks a slot, ask them to confirm that exact time and
**STOP**. Pass `--timezone-confirmed` only after they confirm. Without it, `slots select`
returns `timezone_confirmation_required`.

Each listed time is one choice. When a slot has `resource_options`, several staff or resources
can take that time; do not list it more than once and do not ask the user to choose between
them.

```bash
booking-agent flow slots select --slot-id <chosen by user> --user-confirmed
```

`--user-confirmed` is required and the CLI refuses without it.

### Step 5: Authenticate Link wallet and resolve buyer

**Step 5a — Link wallet sign-in (required before buyer resolve)**

```bash
booking-agent flow link-auth connect
```

The user is normally already signed in from Step 1, so this returns at once with
`data.authenticated: true`. If it returns `awaiting_user: link_auth_approval` instead, follow
[Link sign-in](#link-sign-in-automatic): start `booking-agent flow link-auth poll` in the
background and post the URL in the same turn.

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

Word the question for `data.payment_route` (`data.payment_summary` has the amounts). Never use
raw `amount_minor_units`; quote `data.price_display`.

- **`free`:** say the service is free and ask only whether to book it. Never mention charging,
  a card, or the Link wallet.
  > I'll book Tue, Oct 6, 5:00 PM IDT (7:00 AM MST your time) with Koby's Studio as Ran
  > Yahalom (email, phone from Link). This consultation is free. Shall I book it?
- **`at_service`:** say `data.price_display` is paid to the business at the appointment and
  nothing is charged now; ask only whether to book it. Never ask to charge the Link wallet.
  > ... The AC Repair is $40.00, paid to the business at the appointment; nothing is charged
  > now. Shall I book it?
- **`at_booking`:** ask to book and charge the exact `data.price_display` to their Link wallet.
  Before checkout exists, `data.quote_caveat` means the amount is from the catalog, not the
  merchant checkout total yet; say it is an estimate and that the final total comes from the
  business at booking time.
  > I'll book the 10:30 AM - 11:30 AM slot on Tuesday July 21 with HVAC-Koby as Ran Yahalom
  > (email, phone from Link). The total for the AC Repair appointment is $1.00. Shall I go ahead
  > and book it and charge that to your Link wallet?
- **`deposit_required`:** say a deposit is due at booking and the rest of `data.price_display`
  later; the exact deposit and its terms come from the business at checkout and are shown
  before anything is charged. Ask to book and charge **only the deposit due now** to their Link
  wallet. Do not compute a percentage deposit yourself.

Do not run `flow book` until they approve. Do not send a separate price-only or timezone
confirmation after booking.

This step applies [Personal identification (always from Link)](#personal-identification-always-from-link).
Never hand-enter buyer details. If `buyer resolve` fails with `link_cli_failed` or
`link_auth_expired`, sign the user in again (see [Link sign-in](#link-sign-in-automatic)), then
retry.

### Step 6: Book and pay

**Step 6a — Book**

```bash
booking-agent flow book --service-id <id>
```

For home visits (`channel.type: at_buyer_location`), `flow book` sends the confirmed street
address and the note from `buyer resolve` automatically. Never map or pass an address yourself.
If it fails with `service_address_unconfirmed`, run its `next_command` and confirm the address
with the user.

`flow book` returns `data.merchant_quote` and `data.price_display` (always use these for the
dollar total shown to the user). **Never** quote `amount_minor_units`, `price_minor_units`, or
raw `amount` as dollars, only `price_display` or `merchant_quote.total_display`. Quote tax only
when `merchant_quote` includes a tax line from the merchant; never assume zero tax or warn that
tax might be extra when no tax line was returned. The spend request uses the merchant-advertised
total only.

If `flow book` fails with `mandatory_acceptance_pending`, show the user any policy links or
`continue_url` from the error JSON, wait while they complete the merchant step, then retry
`flow book`. **Never** accept policies or tick boxes on their behalf.

If `data.checkout_links` or `data.checkout_policies` are present after a successful book, show
those links before payment. If the merchant sent none, say nothing about policies; do not tell
the user policies are "missing."

**Free and pay-at-appointment bookings (`free`, `at_service`).** `flow book` places the booking
directly and `next_command` is `booking-agent flow book poll --wait`. Before running it, tell the user
in one short plain message what `data.booking_narration` says: the request is in, nothing was
charged (or the price is paid at the appointment), and you are now waiting for the business to
confirm. Then run `book poll --wait` and report the result. Never create a payment and skip
Steps 6b to 6d.

**Deposit bookings (`deposit_required`).** Before any charge, show `data.due_now_display` (the
deposit charged now), the amount due at the appointment, and every item in
`data.payment_schedules` and `data.payment_disclosures`, and get the user's agreement. Then run
`pay request` with `--disclosures-confirmed`. Quote schedule times from `due_at_display` and
disclosure text from `content_display`; never quote raw `due_at` or timestamps inside
`content`.

The user already approved booking and charge at buyer resolve when the checkout total matches
what you quoted. **Proceed directly to Step 6b** unless `data.merchant_quote.total_changed` is
true. The CLI has already checked that the checkout line item matches the quoted service
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

**Step 6c — Create spend request, surface approval URL, then poll in background**

**Step 6c-i — Create spend request (returns immediately)**

```bash
booking-agent flow pay request --payment-method-id <id-from-6b>
```

The CLI writes the note the user reads when approving in Link (service, business, time, where,
and total). Never write or pass that note yourself.

This command:

1. Creates the spend request and emits `user_message` with the bare approval URL as the
   **first stdout line** (exactly once per booking).
2. Sets `awaiting_user` to `link_spend_approval` and returns immediately.

In the same turn, start `pay poll` **in the background** (`run_in_background`):

```bash
booking-agent flow pay poll
```

Then **post `user_message` to the user as the raw URL before any other narration** (never
markdown `[label](url)`). Do not read stderr for the URL. Do not wait for the user to reply in
chat before starting `pay poll`.

**Step 6c-ii — Poll and complete (background, same turn)**

`pay poll` waits until the user approves in Link and checkout finishes, then runs skill
compliance validation and calendar create-event when needed. **Never** run it in the foreground
before the user has seen the URL: the turn blocks until approval, the user never receives the
link, and the booking looks stuck. When the background poll exits, tell the user they are booked
(or relay its error `user_message`). **Do not** re-surface the approval URL from this command's
output.

Use `--then-complete` on `pay request` only for scripts/CI (not interactive agents).

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

| `data.error.code` | Meaning | What you do |
|---|---|---|
| `payment_declined` (any severity, including `recoverable` before the charge) | The card was declined or needed verification the agent cannot complete. The spend request is used up. | Post `user_message` (the business's explanation plus the request to pick a different card). When the user picks a **different** Link card, run `next_command`: `booking-agent flow pay request --payment-method-id <id>` with that card's id from `link-cli payment-methods list`, then follow the normal approval steps. |
| `payment_failed` with severity `recoverable`, or `payment_authentication_unsupported` / `payment_not_processed` | Same as `payment_declined` (newer business responses for bank verification and for a payment that could not be processed) | Same as `payment_declined` |
| `payment_unrecoverable` | The business sent `payment_failed` with severity `unrecoverable`. This checkout can no longer be completed, and the session is `FAILED`. The response does not say whether a charge was made or whether another payment method would work. | Post `user_message`, then stop. There is no `next_command`. Do not retry this checkout. |
| `complete_in_progress` / `checkout_already_in_progress` | The business is still finishing this checkout | Not a decline. Post `user_message`, then run `next_command` (same spend request, same checkout). Never create a new spend request or a new charge. |
| `payment_captured_booking_unconfirmed` | The money was taken but the booking is not confirmed | Not a decline. See "Payment taken, booking not confirmed" below. |
| `mandatory_acceptance_pending` | The business needs a policy acceptance or other non-payment step | Not a decline. Post `user_message` (it holds `continue_url` and the policy links), wait until the user says they finished, then run `next_command`. Never accept for the user. |
| `approval_timeout` / `spend_request_not_approved` | Link approval has not arrived | Post `user_message`, then run `next_command` |
| anything else (for example `ucp_complete_failed`) | Transport or other error | Post `user_message`, then run `next_command` as returned (it may retry the same spend request). If it fails again, tell the user the booking could not be completed right now. |

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

**Step 6d — Validate** (after `pay request` succeeds)

```bash
booking-agent flow validate-skill-compliance
```

**Payment notes (error recovery only):**

- A live catalog with `payment_timing: at_service` is booked directly. Tell the user the quoted
  price is paid at the appointment. Do not run a payment command.
- A live catalog with `payment_timing: deposit_required` goes through checkout. Charge only the
  due-now (immediate) amount from the payment term. Show every disclosure and the amount due at
  the appointment, then pass `--disclosures-confirmed` on `pay request` after the user agrees.
  Do not compute a percentage deposit yourself.
- Unknown or changed payment timing is not bookable. Get a new quote if timing changed.
- Businesses and the client fetch the document at `USP_AGENT_PROFILE_URL`; the
  canonical URL is used by default.
- After payment, follow `next_command` to `booking-agent flow book poll --wait`. Confirmation is GET
  polling only. Never start or request an inbound booking webhook.

### Step 7: Validate compliance

```bash
booking-agent flow validate-skill-compliance
```

If this exits non-zero, read `data.error.message` and fix the missing gate. Do **not** claim
the booking is done until it passes.

### Authorization trail (when the user asks)

After payment or when the user asks for receipt-style authorization ids:

```bash
booking-agent flow evidence
```

Share the join keys in plain language (booking reference, payment reference). Never expose card
data, credentials, or shared payment tokens.

### Step 8: Add booking to Google Calendar

After Step 7 passes and `phase == "CONFIRMED"`:

```bash
booking-agent flow calendar create-event
```

- Runs only when the user chose **option 2** (`calendar connect --mode events`) at the gate.
- If the user skipped calendar or chose free/busy only, this command no-ops (`data.skipped: true`).
- On success, when `data.html_link` is set, show the user the **raw `html_link` URL** (never
  markdown `[View on Google Calendar](url)` or any other labeled link).
- The event description is written by the CLI. It is a summary of the booking: the request,
  service, who it is for, where it happens, payment (free, pay when they receive the service,
  deposit already paid plus the amount still due, or paid in full), booking and order
  references, the business site, and how to cancel or reschedule. That last part uses the
  manage booking link when the business provided one, otherwise the fallback links saved on
  the session (checkout page, business website, and checkout links). Do not edit the Google
  event to add them.
- Narrate success in everyday language or surface `data.error` only on unexpected API failure.

## Important

- **Never** use markdown hyperlinks with labels (`[text](url)`). Always post raw URLs for Link
  approval, Link sign-in, Google Calendar OAuth, and post-booking calendar links.
- **Never** format user-facing messages as markdown tables (`| ... |`). Use numbered or bulleted
  lists instead (especially for available slots and booking recaps).
- **Never** use technical jargon with the user (CLI, UCP, auth, session, OAuth, checkout
  mode, symlink, polling, orchestrator, flow phase, etc.) unless they explicitly ask how
  something works. Always describe booking progress in everyday language.
- **Never** substitute a different payment command than `next_command` after `pay request`.
- **Never** re-surface the Link approval URL after `pay request` has emitted it once. Commands
  like `flow status` and `pay ucp-complete` poll silently.
- **Never** use `pay request --then-complete` in interactive agent flows — it blocks the shell
  until checkout finishes, so the agent cannot show the approval URL until after the user may
  have already approved via a mobile notification.
- **Never** run the first `pay poll` after `pay request` in the foreground. Start it in the
  background and post the approval URL in the same turn; otherwise the turn blocks until
  approval and the user never sees the link.
- **Never** run the first `link-auth poll` after `link-auth connect` in the foreground, and never
  use `link-auth connect --wait` in interactive flows. Start the poll in the background and post
  the sign-in URL in the same turn, exactly like `pay poll`.
- **Never** ask the user to reply "approved" or "done" after showing a Link approval URL.
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
- **Never** auto-pick a slot. `booking-agent flow slots select` requires `--user-confirmed` and
  refuses otherwise.
- **Never** pass `--mismatch-confirmed`, `--details-confirmed`, `--timezone-confirmed`, or
  `--allow-duplicate` before the user has answered that exact question.
- **Never** run `sleep` or a fixed wait alongside a `booking-agent` command; wait for the command to
  exit.
- **Never** skip the calendar gate. `booking-agent flow availability` fails until the user has
  connected a calendar or declined, or you skipped it because this runtime has no calendar route.
- **Never** guess a calendar route or token variable name, and never pass a token value on the
  command line. Observe the runtime, then pass `--route` (and `--token-env <NAME>` for a
  connector).
- Selecting a business via direct profile URL does not skip gates — the same gates apply as
  for registry discovery.

## Errors

All errors exit with code 1 and a JSON body containing `data.error.code` and
`data.error.message`. Always read `next_command` in the error body — the CLI tells you how to
recover.

| Error code | Cause | Recovery |
|---|---|---|
| `calendar_gate_not_completed` | `availability` called before calendar step | Run `booking-agent flow calendar ask` and follow [the calendar gate](#step-3-select-business-and-resolve-calendar-gate) |
| `link_auth_gate_not_completed` | `search-area propose` or `buyer resolve` while signed out of Link | Sign the user in (`link-auth connect`, then `link-auth poll` in the background and post the URL in the same turn), then retry |
| `link_auth_expired` | Link session expired before payment | Sign the user in the same way, then retry |
| `link_auth_timeout` | User did not approve the Link sign-in in time | Run `next_command`: `link-auth poll` again in the background (same link, do not re-post it) or `link-auth connect` (new link, post it once, then poll in the background) |
| `link_auth_not_started` | `link-auth poll` with no sign-in waiting | Run `booking-agent flow link-auth connect`, start `link-auth poll` in the background, and post the URL |
| `checkout_prereq_not_completed` | Calendar step before native checkout validation | Run `next_command` from `business use` (`use-ucp-checkout-mode`) |
| `ucp_native_required` | Business does not support the native v2 checkout path | Tell the user this business cannot be booked here yet and offer another result |
| `disclosures_unconfirmed` | Deposit checkout returned terms or disclosures the user has not acknowledged | Show due now, due at the appointment, and every disclosure, then re-run `pay request` with `--disclosures-confirmed` |
| `shared_payment_token_not_issued` | Link approved the spend request but returned no shared payment token, so nothing was charged | Post `user_message`, re-run `pay ucp-complete` once; if it fails again, tell the user this business cannot take a Link payment right now and do not ask them to approve again |
| `checkout_total_unconfirmed` | Checkout total (tax, fees, discounts) differs from the quoted price and the user has not approved it | Show service price, tax, and total from `data.merchant_quote`, get approval, then re-run `pay request` with `--total-confirmed` |
| `payment_timing_changed` | Live catalog timing no longer matches the quote | Stop, re-quote, and get a new approval |
| `payment_timing_not_supported` | Live catalog declares another unsupported timing | Stop and explain that this service cannot be booked here yet |
| `mandatory_acceptance_pending` | Merchant requires policy acceptance or another non-payment step before checkout | Show `continue_url` / policy links from the error; wait; retry `flow book` (or run `next_command` when it came from `pay ucp-complete`); never accept for the user |
| `service_not_available_in_area` | Service coordinates are farther than the `--radius-km` you sent | Widen search or pick another result; do not bypass with direct profile URL |
| `slot_not_selected` | `book` called before slot selection | Run `booking-agent flow slots list`, ask user, then `slots select --user-confirmed` |
| `buyer_not_from_link_cli` | Buyer details not sourced from Link wallet | Run `booking-agent flow buyer resolve` |
| `buyer_email_invalid` | Placeholder or malformed email detected | Run `booking-agent flow buyer resolve` — never hand-enter |
| `profile_unreachable` | Business profile URL unreachable | Surface the URL from `data.error.message` to the user |
| `agent_profile_unresolvable` | The business could not fetch the booking assistant's platform profile, so nothing was booked | Post `user_message`, then run `next_command` once (it reuses the same idempotency key, so it cannot double-book); if it fails again, offer another time or business |
| `spend_request_not_approved` | Spend request was denied, canceled, expired, or remained pending | Re-run `pay ucp-complete` with the same spend id, or create a new spend request |
| `missing_network_id_for_spt` | UCP checkout lacked `com.stripe.payments` Link instrument `config.network_id` | `booking-agent flow pay rebook --service-id <id>` for a fresh checkout, or stop if the merchant omitted `link.config.network_id` |
| `approval_not_surfaced_to_user` | `pay ucp-complete` ran before the approval URL was shown | Start `booking-agent flow pay poll` in the background and post `user_message` in the same turn |
| `approval_timeout` | User did not approve within the wait window | Re-run `pay ucp-complete` with the same spend id, or run `pay request` again |
| `payment_declined` / `payment_failed` (recoverable) / `payment_authentication_unsupported` / `payment_not_processed` | `pay ucp-complete` charge was declined; the spend request is used up | Post `user_message`, ask for a different Link card, run `next_command` (`booking-agent flow pay request ...`). Never show `continue_url` or a bank `action_url`. See Step 6c-iii |
| `payment_unrecoverable` | `pay ucp-complete` got `payment_failed` with severity `unrecoverable`, so this checkout can no longer be completed and the session is `FAILED` | Post `user_message` and stop. Do not retry this checkout. See Step 6c-iii |
| `complete_in_progress` / `checkout_already_in_progress` | Business is still completing the same checkout | Run `next_command` (same spend request); no new spend request |
| `payment_captured_booking_unconfirmed` | Payment captured, booking not confirmed | Post `user_message`; never retry or ask for another card; run `next_command` (`booking-agent flow book poll --wait`). See Step 6c-iii |
| `booking_canceled_after_payment` | Business canceled a booking it was paid for | Post `user_message` (how to reach the business and ask for a refund); never retry or ask for another card |
| `payment_already_captured` | A payment command ran after a capture without a confirmed booking | Do not charge again; run `next_command` (`booking-agent flow book poll --wait`) |
| `service_details_changed` | The live catalog differs from the discover row the user picked (`data.changes`) | Tell the user what changed, ask, then run `next_command` (`--details-confirmed`) only if they agree |
| `timezone_confirmation_required` | Slot time unverified: business timezone conflicts with the service location, or a home visit with unknown local time | Ask the user to confirm the exact time in the business's timezone, then run `next_command` (`--timezone-confirmed`) |
| `context_required` | `pay request` got both `--context` and `--context-file` | Run `pay request` with neither; the CLI writes the note |
| `search_area_unconfirmed` | Place search (`--radius-km`) with no confirmed area | Run `next_command` (`flow search-area propose`) and confirm the area with the user |
| `discover_radius_required` | Confirmed area but no `--radius-km` | Re-run discover with `--radius-km` (see [Search area](#search-area)) |
| `service_address_unconfirmed` | Home visit with no confirmed street address | Run `next_command` and confirm the address with the user |
| `search_area_not_user_confirmed` | `search-area confirm` without `--user-confirmed` | Ask the area question; pass `--user-confirmed` only after they agree |
| `search_area_input_invalid` | `search-area set` got neither or both of `--address` and `--link-address-id` | Pass exactly one |
| `link_address_not_found` | `--link-address-id` is not one of the user's saved addresses | Run `next_command` (`search-area propose`) |
| `spend_request_replacement_required` | Link cannot resume this spend request (for example `three_d_secure_retry`) | Show `user_message`, then run `next_command` to create a new spend request |
| `reschedule_not_actionable` | Session is not a confirmed upcoming booking from this device | Resume the right booking from `sessions list`; past or other-device appointments move only on the business's page |
| `reschedule_not_allowed` | Business policy does not allow moving this booking | Show the raw `booking_url` and say they can change it with the business |
| `reschedule_fee_unsupported` | Business charges a reschedule fee | Show the raw `booking_url`; never offer to pay the fee here |
| `reschedule_at_business` | Saved booking credential is missing, expired, or for another business | Show the raw `booking_url`; do not retry |
| `reschedule_unauthorized` | Business rejected the booking credential | Show the raw `booking_url`; do not retry |
| `reschedule_price_changed` | Chosen slot has a different price than the booked one | Ask the user to pick another slot, or query a different window |
| `reschedule_price_unknown` | Paid booking and the old or new price is unknown | Show the raw `booking_url`; the move cannot be priced here |
| `reschedule_same_slot` | User picked the time the booking already has | Ask for a different slot |
| `reschedule_failed` | Business refused the move (e.g. slot just taken) | Run `booking-agent flow reschedule availability` again and re-ask |
| `reschedule_booking_not_active` | Booking was canceled or completed meanwhile | Tell the user plainly; nothing to move |

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

USP splits every operation into two tiers, and the CLI handles both without you doing anything.
It matters here only because it changes what you can promise the user.

**Public — no authentication.** Searching for businesses, reading a catalog, and checking
availability. The agent browses anonymously; nothing identifies it.

**Privileged — always authenticated.** Creating, changing, confirming or cancelling a booking,
holding a slot, anything payment-adjacent, and anything that returns the buyer's personal
details. The business publishes which authentication mechanisms it accepts, and the CLI picks
one it supports. If it recognizes none, it refuses rather than falling back to browsing
anonymously — an unauthenticated booking attempt would either fail or, worse, succeed without
anyone being accountable for it.

**A business can be browsable but not bookable.** Search, catalog and availability still work
against a business that does not support authenticated booking; only the booking itself refuses.
If that happens the CLI returns an error rather than a booking, and the honest thing to tell the
user is that this particular business cannot be booked through you yet and they will need to book
it directly. Do not retry, and do not describe policies or mechanisms — just say it plainly and
offer the alternatives you found.

**What this means for cancelling.** When a booking is created the business hands back a
credential scoped to that one booking, tied to a key the agent keeps locally and never sends.
`booking-agent flow book cancel` uses it. Two consequences worth knowing:

- The credential works for **that booking at that business only**. It is not an account and not
  a login.
- If it has expired, or the business rejects it, there is **nothing to retry** — the agent has no
  standing identity there. The CLI returns a `booking_url` instead. Give the user that link and
  tell them, in plain words, that they will need to finish the cancellation on the business's own
  page. Do not describe credentials, keys, or proofs.

Never mention any of this machinery to the user. "I've cancelled that for you" or "you'll need to
cancel this one on their website — here's the link" is the whole of what they need.

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

Use `booking-agent flow sessions list` only to find unfinished flows or upcoming bookings
(`actionable: true`, `booking_id`, slot times). Do not use it as a past-booking list.

## Environment variables (UCP-native and auth stubs)

| Variable | Role |
|----------|------|
| `USP_REGISTRY_URL` | Optional registry `search_services` POST URL or API-base override. Live discovery defaults to the public Wix USP registry. |
| `USP_AGENT_PROFILE_URL` | D2 platform identity URI used for both `USP-Agent` and `UCP-Agent`. Defaults to `https://profile.booking-agent.dev/platform-profile.json`; businesses and the client fetch this document over HTTP. A staging or loopback override uses this same variable. Loopback is non-conformant because production businesses must reject RFC 6890 special-use addresses and defend against DNS rebinding, so `init` refuses a non-`https` value (`platform_profile_url_invalid`) unless the host is a literal loopback address, which is for local development only. |
| `BOOKING_AGENT_HTTP_TRACE` | Set to `1` to opt in to redacted HTTP trace logging under `~/.booking-agent/http_trace.log` (off by default in production). |
| `BOOKING_AGENT_BUYER_DEVICE_IP` | Set by a hosting surface that observed the buyer's device IP. Sent as `dev.ucp.buyer_ip`; an invalid address stops checkout. The user agent and `dev.usp-protocol.agent_host_ip` are always sent and cannot be turned off. |
| `BOOKING_AGENT_USP_AUTH_SCOPED_CREDENTIAL` | `required` \| `preferred`. How strictly to enforce authentication on privileged operations. Default `required` (the conformant setting). See [Booking authorization](#booking-authorization) below. |

## Further docs

- Stripe link-cli spend-request commands: https://github.com/stripe/link-cli
- USP spec: https://usp-protocol.dev/specification

## License

This skill and the Booking Agent source are released under the Apache License 2.0, Copyright 2026 Wix.com. Complete terms: https://github.com/wix/booking-agent/blob/master/LICENSE

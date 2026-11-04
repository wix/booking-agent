# Install on Base44 Superagent

## Add the skill

The Booking Agent ships with Base44 Superagent as a suggested skill. Add it from
Superagent. That is the only step for the skill.

1. Open **Plugins**, then **Skills**.
2. Open **Suggested**.
3. Find **booking-agent** and add it.

It is already on that list, marked Base44. The card describes booking a location-based
service from a business, such as a haircut, lesson, repair, class, or appointment, and
connecting a Link wallet. Adding it puts the skill in your workspace, and Superagent
loads that copy.

## What still runs in the sandbox

Base44 Superagent runs the Booking Agent in its sandbox. Only the saved-agent folder,
`/app/.agents`, survives a sandbox reset, so the booking, the Link sign-in, and a
verified copy of the CLI all live there. The skill stays the one you added from
Plugins -> Skills.

<!-- harness-pins:start -->
| Item | Value |
|---|---|
| Approved Booking Agent CLI | `0.11.2` |
| Approved Booking Agent skill | `0.58.2`, bundled in that CLI |
| Replay id | `base44-superagent` |
| Hosted replay | Passed 22 Oct 2026 with CLI 0.10.2 and skill 0.57.4 |

The approved CLI carries every change the replayed CLI ran. Its release notes record why it is approved.

```bash
cli=0.11.2
replay=base44-superagent
```
<!-- harness-pins:end -->

The skill row above is the copy bundled in the approved CLI. Superagent runs the
suggested skill you added from Plugins -> Skills.

## Sandbox setup: paste this into Superagent

The skill is already added. Paste this so Superagent installs the approved CLI and
keeps bookings in the saved folder:

```text
The Booking Agent skill is already added from Plugins -> Skills, on the Suggested
tab. Use that skill. Set up the sandbox by following
https://github.com/wix/booking-agent/blob/master/booking-agent/docs/harnesses/base44-superagent.md
from "What the install runs". Use the pinned CLI in that guide, and keep
everything it saves under /app/.agents. Stop after the checks at the end and do not
start a booking.
```

## What the install runs

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Node.js if
   they are missing, then `link-cli`, which handles Link sign-in and payment approval:

   ```bash
   npm install -g @stripe/link-cli
   ```

2. Set the pinned values from the table above (`cli=...` and `replay=...`).

3. Download the approved CLI, check its signature from this repository's release
   workflow, and install it from the checked file:

   ```bash
   root=/app/.agents
   mkdir -p "$root/verified-wheels"
   uvx --python 3.12 pip download "booking-agent-cli==$cli" --no-deps --only-binary :all: \
     -d "$root/verified-wheels"
   uvx --python 3.12 pypi-attestations verify pypi --repository https://github.com/wix/booking-agent \
     "$root/verified-wheels/booking_agent_cli-$cli-py3-none-any.whl"
   uv tool install --force "$root/verified-wheels/booking_agent_cli-$cli-py3-none-any.whl"
   ```

4. Keep the booking and the Link sign-in in `/app/.agents`, and set this harness's
   replay so updates follow Base44 Superagent's own rollout. Leave the skill added
   from Plugins -> Skills in place:

   ```bash
   booking-agent runtime prepare --persistent-root "$root"
   grep -qx "BOOKING_AGENT_REPLAY=$replay" ~/.booking-agent/.env 2>/dev/null \
     || printf '\nBOOKING_AGENT_REPLAY=%s\n' "$replay" >> ~/.booking-agent/.env
   ```

5. Check the result:

   ```bash
   booking-agent skill version      # data shows the approved cli_version and skill_version
   booking-agent config show        # BOOKING_AGENT_REPLAY is base44-superagent, from "user .env"
   booking-agent runtime doctor     # healthy is true
   link-cli --help
   ```

   Before the first sandbox reset, `runtime doctor` can report
   `folder_not_yet_shown_to_survive_a_restart`. That is expected.

## After a sandbox reset

A reset removes installed programs and the links into `/app/.agents`. The skill you
added from Plugins -> Skills stays in the workspace. In a fresh conversation, set
`cli` as in step 2, reinstall from the verified copy, and relink the saved folder.
`booking-agent flow status` then finds the saved booking and the Link sign-in, so
there is no new search and no second sign-in:

```bash
uv tool install --force "/app/.agents/verified-wheels/booking_agent_cli-$cli-py3-none-any.whl"
npm install -g @stripe/link-cli
booking-agent runtime prepare --persistent-root /app/.agents
```

## What to expect

- **Calendar.** Connect Google Calendar when the assistant asks. If the saved calendar
  connection drops, reconnecting keeps the chosen time.
- **Booked message.** It includes the raw Google Calendar event link. Superagent may
  paraphrase the booking and leave the link out; ask it to post the booking
  assistant's exact booked message.
- **Updates.** The CLI offers a newer release only after it passes the Base44
  Superagent replay. Until then it reports that the installed version is up to date.
- **Not replayed here yet:** a pay-at-appointment booking (one paid in person, with no
  charge at booking time). Paid bookings were replayed.

Replay procedure and evidence: [ops/verified-harnesses.md](../../ops/verified-harnesses.md#base44-superagent)
and the release notes for the approved CLI.

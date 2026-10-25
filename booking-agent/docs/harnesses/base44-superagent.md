# Install on Base44 Superagent

Base44 Superagent runs the Booking Agent in its sandbox. Only the saved-agent folder,
`/app/.agents`, survives a sandbox reset, so the booking, the Link sign-in, the skill,
and a verified copy of the CLI all live there.

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

## Fastest install: paste this into Superagent

```text
Install the Booking Agent in your sandbox by following
https://github.com/wix/booking-agent/blob/master/booking-agent/docs/harnesses/base44-superagent.md
exactly. Use the pinned versions in that guide, not the latest release, and keep
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

4. Write the skill bundled in that CLI into the skills folder under `/app/.agents`. If
   Superagent loads skills from a different folder inside `/app/.agents`, use that one;
   the file must stay inside the saved folder:

   ```bash
   mkdir -p "$root/skills/booking-agent"
   booking-agent skill print \
     | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["data"]["skill_text"])' \
     > "$root/skills/booking-agent/SKILL.md"
   ```

5. Keep the booking and the Link sign-in in `/app/.agents`, and set this harness's
   replay so updates follow Base44 Superagent's own rollout:

   ```bash
   booking-agent runtime prepare --persistent-root "$root"
   grep -qx "BOOKING_AGENT_REPLAY=$replay" ~/.booking-agent/.env 2>/dev/null \
     || printf '\nBOOKING_AGENT_REPLAY=%s\n' "$replay" >> ~/.booking-agent/.env
   ```

6. Check the result:

   ```bash
   booking-agent skill version      # data shows the approved cli_version and skill_version
   booking-agent config show        # BOOKING_AGENT_REPLAY is base44-superagent, from "user .env"
   booking-agent runtime doctor     # healthy is true
   link-cli --help
   ```

   Before the first sandbox reset, `runtime doctor` can report
   `folder_not_yet_shown_to_survive_a_restart`. That is expected.

## After a sandbox reset

A reset removes installed programs and the links into `/app/.agents`. In a fresh
conversation, set `cli` as in step 2, reinstall from the verified copy, and relink the
saved folder. `booking-agent flow status` then finds the saved booking and the Link
sign-in, so there is no new search and no second sign-in:

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

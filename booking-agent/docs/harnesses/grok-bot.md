# Install on Grok Bot

Grok Bot runs the Booking Agent on the Bot's cloud computer, not on your own machine.
Only the Grok Bot app is installed by you; everything below runs on that cloud computer.

<!-- harness-pins:start -->
| Item | Value |
|---|---|
| Approved Booking Agent CLI | `0.10.0` |
| Approved Booking Agent skill | `0.57.2`, bundled in that CLI |
| Replay id | `grok-bot` |
| Hosted replay | Passed 22 Oct 2026 with CLI 0.9.1 and skill 0.57.1 |

The approved CLI carries every change the replayed CLI ran. Its release notes record why it is approved.

```bash
cli=0.10.0
replay=grok-bot
```
<!-- harness-pins:end -->

## Fastest install: paste this into Grok Bot

```text
Install the Booking Agent on your cloud computer, not on my local computer, by following
https://github.com/wix/booking-agent/blob/master/booking-agent/docs/harnesses/grok-bot.md
exactly. Use the pinned versions in that guide, not the latest release. List the commands
before you run them and wait for my OK. Stop after the checks at the end and do not start
a booking.
```

When the checks pass, start a new bot so it loads the skill from the beginning.

## What the install runs

Everything persistent goes under `/workspace`, which Grok Bot keeps when the computer is
updated. Installed programs may not survive an update, so the verified wheel is kept
there too.

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Node.js if
   they are missing, then `link-cli`, which handles Link sign-in and payment approval:

   ```bash
   npm install -g @stripe/link-cli
   ```

2. Set the pinned values from the table above (`cli=...` and `replay=...`).

3. Download the approved CLI, check its signature from this repository's release
   workflow, and install it from the checked file:

   ```bash
   root=/workspace
   mkdir -p "$root/verified-wheels"
   uvx --python 3.12 pip download "booking-agent-cli==$cli" --no-deps --only-binary :all: \
     -d "$root/verified-wheels"
   uvx --python 3.12 pypi-attestations verify pypi --repository https://github.com/wix/booking-agent \
     "$root/verified-wheels/booking_agent_cli-$cli-py3-none-any.whl"
   uv tool install --force "$root/verified-wheels/booking_agent_cli-$cli-py3-none-any.whl"
   ```

4. Write the skill bundled in that CLI, then save `/workspace/skills/booking-agent/SKILL.md`
   as a private Grok skill named `booking-agent`, with its full text unchanged:

   ```bash
   mkdir -p "$root/skills/booking-agent"
   booking-agent skill print \
     | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["data"]["skill_text"])' \
     > "$root/skills/booking-agent/SKILL.md"
   ```

5. Keep the booking and the Link sign-in in `/workspace`, and set this harness's replay
   so updates follow Grok Bot's own rollout:

   ```bash
   booking-agent runtime prepare --persistent-root "$root"
   grep -qx "BOOKING_AGENT_REPLAY=$replay" ~/.booking-agent/.env 2>/dev/null \
     || printf '\nBOOKING_AGENT_REPLAY=%s\n' "$replay" >> ~/.booking-agent/.env
   ```

6. Check the result:

   ```bash
   booking-agent skill version      # data shows the approved cli_version and skill_version
   booking-agent config show        # BOOKING_AGENT_REPLAY is grok-bot, from "user .env"
   booking-agent runtime doctor     # healthy is true
   link-cli --help
   ```

   Before the first computer update, `runtime doctor` can report
   `folder_not_yet_shown_to_survive_a_restart`. That is expected.

## After Update Bot's Computer

Use **Update Bot's Computer** (Settings, Computer tab), never **Reset Bot's Computer**,
which restores an older snapshot and can drop a booking that just completed. After an
update, set `cli` as in step 2, reinstall whatever is missing, and relink the saved
folder. Then continue the saved booking:

```bash
uv tool install --force "/workspace/verified-wheels/booking_agent_cli-$cli-py3-none-any.whl"
npm install -g @stripe/link-cli
booking-agent runtime prepare --persistent-root /workspace
```

## What to expect

- **Calendar.** Grok Bot cannot hand its calendar connector token to the CLI, so the
  assistant skips the calendar and says plainly that no calendar event was added.
- **Updates.** The CLI offers a newer release only after it passes the Grok Bot replay.
  Until then it reports that the installed version is up to date.
- **Paid and pay-at-appointment bookings** were both replayed on this harness.

Replay procedure and evidence: [ops/verified-harnesses.md](../../ops/verified-harnesses.md#grok-bot)
and the release notes for the approved CLI.

# Install on Muse

Muse runs the Booking Agent inside its Secure VM. The folder that survives a replacement
of that VM is `/home/hatch/workspace`, so the booking, the Link sign-in, the skill, and
a verified copy of the CLI all live there.

<!-- harness-pins:start -->
| Item | Value |
|---|---|
| Approved Booking Agent CLI | `0.11.1` |
| Approved Booking Agent skill | `0.58.1`, bundled in that CLI |
| Replay id | `muse` |
| Hosted replay | Passed 25 Oct 2026 with CLI 0.11.1 and skill 0.58.1 |

```bash
cli=0.11.1
replay=muse
```
<!-- harness-pins:end -->

## Fastest install: paste this into Muse

```text
Install the Booking Agent inside your Secure VM, not on my computer, by following
https://github.com/wix/booking-agent/blob/master/booking-agent/docs/harnesses/muse.md
exactly. Use the pinned versions in that guide, not the latest release, and keep
everything it saves under /home/hatch/workspace. List the commands before you run them
and wait for my OK. Stop after the checks at the end and do not start a booking.
```

When the checks pass, start a new chat so Muse loads the skill from the beginning.

## What the install runs

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Node.js if
   they are missing, then `link-cli`, which handles Link sign-in and payment approval.
   Keep that install under `/home/hatch/workspace`, which is the folder this VM keeps:

   ```bash
   root=/home/hatch/workspace
   npm install -g @stripe/link-cli --prefix "$root/npm-global"
   export PATH="$root/npm-global/bin:$PATH"
   ```

2. Set the pinned values from the table above (`cli=...` and `replay=...`).

3. Download the approved CLI, check its signature from this repository's release
   workflow, and install it from the checked file:

   ```bash
   mkdir -p "$root/verified-wheels"
   uvx --python 3.12 pip download "booking-agent-cli==$cli" --no-deps --only-binary :all: \
     -d "$root/verified-wheels"
   uvx --python 3.12 pypi-attestations verify pypi --repository https://github.com/wix/booking-agent \
     "$root/verified-wheels/booking_agent_cli-$cli-py3-none-any.whl"
   uv tool install --force "$root/verified-wheels/booking_agent_cli-$cli-py3-none-any.whl"
   ```

4. Write the skill bundled in that CLI. If Muse also requires a `skill.yaml` next to
   it, that file may point at this one. Leave the skill text unchanged:

   ```bash
   mkdir -p "$root/custom-skills/booking-agent"
   booking-agent skill print \
     | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["data"]["skill_text"])' \
     > "$root/custom-skills/booking-agent/SKILL.md"
   ```

5. Keep the booking and the Link sign-in in `/home/hatch/workspace`, and set this
   harness's replay so updates follow Muse's own rollout:

   ```bash
   booking-agent runtime prepare --persistent-root "$root"
   grep -qx "BOOKING_AGENT_REPLAY=$replay" ~/.booking-agent/.env 2>/dev/null \
     || printf '\nBOOKING_AGENT_REPLAY=%s\n' "$replay" >> ~/.booking-agent/.env
   ```

6. Check the result:

   ```bash
   readlink -f ~/.booking-agent/.env   # the file is inside /home/hatch/workspace
   booking-agent skill version         # data shows the approved cli_version and skill_version
   booking-agent config show           # BOOKING_AGENT_REPLAY is muse, from "user .env"
   booking-agent runtime doctor        # healthy is true
   link-cli --help
   ```

   Before the first VM replacement, `runtime doctor` can report
   `folder_not_yet_shown_to_survive_a_restart`. That is expected.

## After the Secure VM is replaced

A replacement can remove installed programs. The workspace, including the verified
wheel and the skill, stays. In the chat that continues the booking, set `cli` as in
step 2, reinstall whatever is missing, and relink the saved folder. `booking-agent flow
status` then finds the saved booking and the Link sign-in, so there is no new search
and no second sign-in:

```bash
export PATH="/home/hatch/workspace/npm-global/bin:$PATH"
uv tool install --force "/home/hatch/workspace/verified-wheels/booking_agent_cli-$cli-py3-none-any.whl"
npm install -g @stripe/link-cli --prefix /home/hatch/workspace/npm-global
booking-agent runtime prepare --persistent-root /home/hatch/workspace
```

Do not use Reset in Muse's data settings. That deletes the computer. A new chat on the
same boot is not this replacement: the boot id has to change.

## What to expect

- **Calendar.** Muse reads and writes Google Calendar itself and does not hand the CLI
  a token. The assistant connects with `--route delegated --delegated-can-read-busy`,
  reports busy times when asked, and after payment creates the one event it was given
  and reports that event back. The booked message includes the raw calendar link.
- **Payment link.** After you say yes, Muse may ask once more before it posts the Link
  approval URL. That second yes is Muse's own safety check. Approve the payment only
  in Link.
- **Updates.** The CLI offers a newer release only after it passes the Muse replay.
  Until then it reports that the installed version is up to date.
- **Not replayed here yet:** a pay-at-appointment booking (one paid in person, with no
  charge at booking time). A paid booking, and cancelling it afterwards, were replayed.

Replay procedure and evidence: [ops/verified-harnesses.md](../../ops/verified-harnesses.md#muse)
and the release notes for the approved CLI.

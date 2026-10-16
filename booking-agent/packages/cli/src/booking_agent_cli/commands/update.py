"""booking-agent skill / update: the matching instructions, and consented upgrades."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import typer

from .. import __version__ as CLI_VERSION
from .. import updates
from ..output import AgentOutput, emit, emit_error
from ..session import (
    TERMINAL_PHASES,
    SessionStoreTooNew,
    get_active_session,
    load_store,
    save_store,
)
from ..skill_bundle import SKILL_VERSION, skill_text

skill_app = typer.Typer(help="The booking-agent skill this CLI was built with.", no_args_is_help=True)
update_app = typer.Typer(help="Check for, and install with the user's consent, a newer booking-agent.")

PACKAGE = "booking-agent-cli"
DECLINE_COMMAND = "booking-agent update decline --prompt-key"
SKILL_PRINT_COMMAND = "booking-agent skill print"
INSTALL_TIMEOUT_SECONDS = 600
VERIFY_TIMEOUT_SECONDS = 60
_MAX_ANSWERED = 50


# ---------------------------------------------------------------------------
# skill
# ---------------------------------------------------------------------------


@skill_app.command("version")
def skill_version() -> None:
    """The installed CLI version and the skill version it was built with."""
    emit(AgentOutput(
        phase="UPDATE",
        prompt="Versions of the installed booking assistant.",
        data={"cli_version": CLI_VERSION, "skill_version": SKILL_VERSION},
    ))


@skill_app.command("print")
def skill_print(
    output: Optional[Path] = typer.Option(
        None, "--output",
        help="Replace this existing booking-agent SKILL.md with the matching copy.",
    ),
) -> None:
    """The skill text that matches this CLI, or write it over the loaded skill file."""
    text = skill_text()
    if output is None:
        emit(AgentOutput(
            phase="UPDATE",
            prompt=(
                f"data.skill_text is booking-agent skill {SKILL_VERSION}, the instructions "
                "that match the installed assistant. If you know the path of the skill file "
                f"you loaded, run `{SKILL_PRINT_COMMAND} --output <that path>`. Otherwise "
                "follow data.skill_text for the rest of this conversation, and tell the user "
                "in plain words that starting a new chat loads the updated assistant."
            ),
            data={"skill_version": SKILL_VERSION, "skill_text": text},
        ))
        return
    error = _skill_target_error(output)
    if error is not None:
        emit_error("UPDATE", code="skill_output_refused", message=error)
        raise typer.Exit(code=1)
    from booking_agent.secure_file import write_private_text

    write_private_text(output, text)
    try:
        output.chmod(0o644)
    except OSError:
        pass
    emit(AgentOutput(
        phase="UPDATE",
        prompt=(
            f"Wrote booking-agent skill {SKILL_VERSION} to the loaded skill file. Tell the "
            "user in plain words that starting a new chat loads the updated assistant; until "
            "then, follow the new text, which you can read with "
            f"`{SKILL_PRINT_COMMAND}`."
        ),
        data={"skill_version": SKILL_VERSION, "path": str(output)},
    ))


def _skill_target_error(path: Path) -> Optional[str]:
    if path.name != "SKILL.md":
        return "--output must name a SKILL.md file."
    if path.is_symlink():
        return (
            "That skill file is a link into a source checkout, which updates with the "
            "checkout; it is never overwritten."
        )
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        return "--output must be an existing booking-agent SKILL.md."
    if not current.startswith("---\n") or "\nname: booking-agent\n" not in current.split("\n---", 1)[0] + "\n":
        return "--output must be an existing booking-agent SKILL.md."
    return None


# ---------------------------------------------------------------------------
# update check / decline
# ---------------------------------------------------------------------------


def consent_output(decision: updates.Decision, *, phase: str, continue_command: Optional[str] = None) -> AgentOutput:
    """What the agent tells the user about ``decision``; asks only for recommend and urgent."""
    data = decision.as_data()
    data["decline_command"] = (
        f"{DECLINE_COMMAND} {decision.prompt_key}" if decision.prompt_key else None
    )
    if continue_command:
        data["continue_command"] = continue_command
    summary = decision.user_summary or "It brings the latest improvements."
    if decision.action in updates.PROMPT_ACTIONS:
        carry_on = "run data.continue_command" if continue_command else "carry on"
        decline = f"run data.decline_command, then {carry_on}" if decision.prompt_key else carry_on
        if decision.reason == "skill_newer":
            decline = (
                f"run data.decline_command, then run `{SKILL_PRINT_COMMAND}` and follow the "
                f"instructions it returns, then {carry_on}"
            )
        return AgentOutput(
            phase=phase,
            awaiting_user=updates.AWAITING_UPDATE_CONSENT,
            prompt=(
                "STOP. Ask the user in plain words: \"A newer version of the booking "
                f"assistant is available. {summary} Shall I install it now?\" Wait for "
                f"their answer. If yes, run next_command. If no, {decline}. Do not install "
                "anything yourself."
            ),
            next_command=updates.UPDATE_COMMAND,
            forbidden=["Installing or upgrading without the user's yes"],
            data=data,
        )
    if decision.action == updates.ACTION_INFORM:
        return AgentOutput(
            phase=phase,
            prompt=(
                "A newer version of the booking assistant is available. Mention it to the "
                f"user in one plain sentence ({summary}). Do not ask, and install it only "
                f"if they ask, with `{updates.UPDATE_COMMAND}`."
            ),
            data=data,
        )
    return AgentOutput(phase=phase, prompt="No update to mention. Carry on.", data=data)


def current_decision(
    *,
    reported_skill: Optional[str],
    capabilities: List[str],
    after_booking: bool,
    check_local: bool,
    refresh: bool = False,
) -> updates.Decision:
    try:
        store = load_store()
        answered, too_new = list(store.update_prompts_answered), False
    except SessionStoreTooNew:
        answered, too_new = [], True
    if not updates.checks_enabled() and not too_new:
        return updates.Decision(reason="checks_disabled")
    return updates.decide(
        reported_skill=reported_skill,
        releases=updates.load_releases(refresh=refresh),
        answered=answered,
        capabilities=capabilities,
        after_booking=after_booking,
        check_local=check_local,
        store_too_new=too_new,
    )


@update_app.command("check")
def update_check(
    skill_version: Optional[str] = typer.Option(
        None, "--skill-version", help="The version in the loaded skill's frontmatter.",
    ),
    capability: Optional[List[str]] = typer.Option(
        None, "--capability", help="A capability the user's request needs; repeatable.",
    ),
    after_booking: bool = typer.Option(
        False, "--after-booking", help="The booking just reached CONFIRMED.",
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the cached release index."),
) -> None:
    """Whether to tell the user about a newer version, and how."""
    decision = current_decision(
        reported_skill=skill_version,
        capabilities=list(capability or []),
        after_booking=after_booking,
        check_local=skill_version is not None,
        refresh=refresh,
    )
    emit(consent_output(decision, phase="UPDATE"))


@update_app.command("decline")
def update_decline(
    prompt_key: str = typer.Option(..., "--prompt-key", help="data.prompt_key of the declined offer."),
) -> None:
    """Remember that the user said no, so this offer is not made again."""
    store = load_store()
    answered = [k for k in store.update_prompts_answered if k != prompt_key]
    store.update_prompts_answered = (answered + [prompt_key])[-_MAX_ANSWERED:]
    save_store(store)
    emit(AgentOutput(
        phase="UPDATE",
        prompt="Noted; this update will not be offered again. Carry on with the user's request.",
        data={"prompt_key": prompt_key},
    ))


# ---------------------------------------------------------------------------
# update --user-confirmed
# ---------------------------------------------------------------------------


@update_app.callback(invoke_without_command=True)
def update(
    ctx: typer.Context,
    user_confirmed: bool = typer.Option(
        False, "--user-confirmed", help="The user said yes to installing the update.",
    ),
) -> None:
    """Install the update the user agreed to (``update check`` picks the version)."""
    if ctx.invoked_subcommand is not None:
        return
    if not user_confirmed:
        emit_error(
            "UPDATE",
            code="update_not_confirmed",
            message=(
                "Installing an update needs the user's yes. Run `booking-agent update check`, "
                "ask the user, and pass --user-confirmed only after they agree."
            ),
            next_command=updates.CHECK_COMMAND,
        )
        raise typer.Exit(code=1)

    store = None
    try:
        store = load_store()
    except SessionStoreTooNew:
        pass
    active = get_active_session(store) if store is not None else None
    if active is not None and active.flow_phase not in TERMINAL_PHASES:
        emit_error(
            "UPDATE",
            code="update_session_in_progress",
            message=(
                "A booking is in progress. Finish or cancel it before updating, so it is "
                "completed by the version that started it."
            ),
            next_command=f"booking-agent flow sessions resume --session-id {active.id}",
        )
        raise typer.Exit(code=1)

    if is_editable_install():
        emit(AgentOutput(
            phase="UPDATE",
            prompt=(
                "This booking assistant runs from a source checkout, so it updates with that "
                "checkout and is never replaced from the package index. Tell the user that in "
                "plain words. Then run next_command to load the instructions that match it."
            ),
            next_command=SKILL_PRINT_COMMAND,
            data={"mode": "editable", "installed_version": CLI_VERSION},
        ))
        return

    releases = updates.load_releases(refresh=True) if updates.checks_enabled() else None
    target, _ = updates.select_target(CLI_VERSION, releases or [])
    if target is None:
        emit(AgentOutput(
            phase="UPDATE",
            prompt=(
                "No newer version can be installed right now; this is the latest one ready "
                "for use. Run next_command to load the instructions that match the installed "
                "assistant."
            ),
            next_command=SKILL_PRINT_COMMAND,
            data={"mode": "current", "installed_version": CLI_VERSION},
        ))
        return

    newest = max(r.key for r in releases or [])
    commands = install_commands(target.cli_version, newest=target.key == newest)
    installer = detect_installer()
    if installer is None:
        emit(AgentOutput(
            phase="UPDATE",
            prompt=(
                "This install was not made with uv or pipx, so it cannot update itself. Nothing "
                "was changed. Tell the user in plain words that the update needs one of the "
                "commands in data.commands, run in their own terminal."
            ),
            data={"mode": "manual", "target_version": target.cli_version, "commands": commands},
        ))
        return

    ok, detail = _run(commands[installer], timeout=INSTALL_TIMEOUT_SECONDS)
    if not ok:
        emit_error(
            "UPDATE",
            code="update_failed",
            message=(
                "The update did not install; the previous version is still in place. Tell the "
                "user in plain words and carry on with the installed version."
            ),
            data={"target_version": target.cli_version, "detail": detail},
        )
        raise typer.Exit(code=1)

    installed = verify_installed()
    found = updates.parse_version((installed or {}).get("cli_version"))
    if found is None or found < target.key:
        emit_error(
            "UPDATE",
            code="update_not_verified",
            message=(
                "The update ran, but the new version did not start as expected. Tell the user "
                "in plain words that the update did not take, and carry on."
            ),
            data={"target_version": target.cli_version, "found": installed},
        )
        raise typer.Exit(code=1)

    _remember(store, f"release:{target.cli_version}")
    emit(AgentOutput(
        phase="UPDATE",
        prompt=(
            f"Updated to version {installed['cli_version']}. Tell the user in one plain sentence. "
            "Then run next_command to load the instructions that match it, and follow them."
        ),
        next_command=SKILL_PRINT_COMMAND,
        data={
            "mode": installer,
            "previous_version": CLI_VERSION,
            "installed_version": installed["cli_version"],
            "skill_version": installed.get("skill_version"),
        },
    ))


def _remember(store, key: Optional[str]) -> None:
    if store is None or key is None:
        return
    try:
        store = load_store()
        if key not in store.update_prompts_answered:
            store.update_prompts_answered = (store.update_prompts_answered + [key])[-_MAX_ANSWERED:]
            save_store(store)
    except Exception:
        pass


def is_editable_install() -> bool:
    try:
        from importlib.metadata import distribution

        raw = distribution(PACKAGE).read_text("direct_url.json")
    except Exception:
        return False
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except ValueError:
        return False
    return bool((info.get("dir_info") or {}).get("editable"))


def detect_installer() -> Optional[str]:
    """``uv`` or ``pipx`` when this process runs from that tool's venv for the package."""
    prefix = Path(sys.prefix)
    if prefix.name != PACKAGE:
        return None
    if prefix.parent.name == "tools" and shutil.which("uv"):
        return "uv"
    if prefix.parent.name == "venvs" and shutil.which("pipx"):
        return "pipx"
    return None


def install_commands(version: str, *, newest: bool) -> dict:
    # ">=" leaves later upgrades free; "==" holds back a newer release that is not ready.
    spec = f"{PACKAGE}>={version}" if newest else f"{PACKAGE}=={version}"
    return {
        "uv": ["uv", "tool", "install", "--force", spec],
        "pipx": ["pipx", "install", "--force", spec],
    }


def _run(argv: List[str], *, timeout: int) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=os.environ.copy())
    except (OSError, subprocess.SubprocessError) as exc:
        return False, type(exc).__name__
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
    return proc.returncode == 0, "\n".join(tail)


def verify_installed() -> Optional[dict]:
    """``skill version`` from a fresh process of the (re)installed CLI."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "booking_agent_cli.main", "skill", "version"],
            capture_output=True, text=True, timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout).get("data")
    except ValueError:
        return None

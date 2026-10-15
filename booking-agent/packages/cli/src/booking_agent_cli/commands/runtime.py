"""``booking-agent runtime``: prepare the folders this runtime keeps its state in."""
from __future__ import annotations

from typing import List

import typer

from ..output import AgentOutput, emit
from ..runtime_prepare import PrepareError, prepare

app = typer.Typer(help="Runtime setup for agents whose filesystem resets between conversations.")


@app.command("prepare")
def runtime_prepare(
    persistent_root: str = typer.Option(
        ...,
        "--persistent-root",
        help="Absolute folder that survives between conversations in this runtime",
    ),
    link: List[str] = typer.Option(
        [],
        "--link",
        help="Extra RUNTIME_PATH=PERSISTENT_PATH folder to keep (relative PERSISTENT_PATH is under the root)",
    ),
) -> None:
    """Link Booking Agent state and the Link sign-in into a folder that outlives this runtime."""
    try:
        links = prepare(persistent_root, link)
    except (PrepareError, OSError) as exc:
        emit(AgentOutput(
            phase="INIT",
            prompt=(
                "Persistence was not set up. Continue the booking without it; state from "
                "this conversation may not survive a restart."
            ),
            next_command="booking-agent flow sessions list",
            data={"error": {"code": "runtime_prepare_failed", "message": str(exc)}},
        ))
        raise typer.Exit(code=1)
    emit(AgentOutput(
        phase="INIT",
        prompt="Booking Agent state and the Link sign-in now live in the persistent folder.",
        next_command="booking-agent flow sessions list",
        data={"links": links},
    ))

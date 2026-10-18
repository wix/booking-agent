"""booking-agent CLI entry point."""
from __future__ import annotations

import sys

import typer

from .commands.config import app as config_app
from .commands.flow import app as flow_app
from .commands.lowlevel import (
    availability_app,
    profile_app,
    search_app,
)
from .commands.runtime import app as runtime_app
from .commands.update import skill_app, update_app

app = typer.Typer(
    help="booking-agent — stateful USP service discovery, availability, and booking. "
         "Skills must call `booking-agent flow ...` instead of raw curl.",
    no_args_is_help=True,
)
app.add_typer(flow_app, name="flow")
app.add_typer(config_app, name="config")
app.add_typer(search_app, name="search")
app.add_typer(profile_app, name="profile")
app.add_typer(availability_app, name="availability")
app.add_typer(runtime_app, name="runtime")
app.add_typer(skill_app, name="skill")
app.add_typer(update_app, name="update")

@app.callback()
def _main(ctx: typer.Context) -> None:
    """Keep state in the host's persistent folder (when it names one) before any command runs."""
    if ctx.invoked_subcommand != "runtime":
        from .runtime_prepare import auto_prepare

        auto_prepare()


INTERNAL_ERROR_MESSAGE = (
    "The booking assistant hit an unexpected internal error. Tell the user in plain "
    "words that something went wrong on our side, then follow next_command."
)
_FALLBACK_LINE = (
    '{"phase": "INIT", "awaiting_user": null, "user_message": null, '
    '"prompt": "[internal_error] The booking assistant hit an unexpected internal error.", '
    '"next_command": null, "forbidden": [], '
    '"data": {"error": {"code": "internal_error", "message": "unexpected internal error"}}, '
    '"correlation_id": null}\n'
)


def _emit_internal_error() -> None:
    """Agent-facing JSON for an uncaught exception; never prints the traceback."""
    try:
        from .output import emit_error
        from .session import load_session

        try:
            session = load_session()
        except Exception:
            session = None
        emit_error(
            session.flow_phase if session is not None else "INIT",
            code="internal_error",
            message=INTERNAL_ERROR_MESSAGE,
            next_command="booking-agent flow status" if session is not None else None,
        )
    except Exception:
        sys.stdout.write(_FALLBACK_LINE)
        sys.stdout.flush()


def _emit_store_too_new(exc) -> None:
    from .output import emit_error
    from .updates import CHECK_COMMAND

    emit_error(
        "INIT",
        code="session_store_too_new",
        message=str(exc),
        next_command=CHECK_COMMAND,
        data={"found_format": exc.found, "supported_format": exc.supported},
    )


def run() -> None:
    """Console entry point: the Typer app plus the uncaught-exception boundary."""
    from .session import SessionStoreTooNew

    try:
        app()
    except (SystemExit, KeyboardInterrupt):
        raise
    except SessionStoreTooNew as exc:
        _emit_store_too_new(exc)
        raise SystemExit(1)
    except Exception:
        _emit_internal_error()
        raise SystemExit(1)


if __name__ == "__main__":
    run()

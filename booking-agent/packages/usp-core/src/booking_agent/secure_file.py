"""Atomic, owner-only writes for the on-disk artefacts under ``~/.booking-agent``.

Everything this package persists is either a credential or something that
identifies the user: the OAuth token store, the session store (which now carries
booking-scoped credentials), the resolved-profile cache, and the
``platform_key_pop`` keystore.  USP Section 10.1.6 leans on those files being
unreadable by other local users -- session-file theft is the dominant residual
risk of the whole mechanism -- so they must not be written at the default umask.

``write_private_text`` writes through a temp file created by ``mkstemp`` in the
same directory, which is 0600 from the moment it exists, then renames it into
place.  A plain ``open()`` followed by ``chmod`` would leave the contents
world-readable for the window in between.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

FILE_MODE = 0o600
DIR_MODE = 0o700


def ensure_private_dir(path: Path) -> Path:
    """Create *path* if needed and restrict it to the owner."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(DIR_MODE)
    except OSError:
        # A directory we do not own (a shared mount, a container bind) is not
        # worth failing a booking over; the file mode below is the real control.
        pass
    return path


def write_private_text(path: Path, text: str) -> None:
    """Atomically write *text* to *path* with mode 0600."""
    parent = path.parent
    ensure_private_dir(parent)
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp_name, FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

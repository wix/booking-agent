"""Whether the skill file the agent loaded is the skill this CLI was built with.

``flow init --skill-path`` hashes the loaded ``SKILL.md`` and compares it with the
bundled copy (``skill_bundle``), which ships inside the attested wheel. The comparison
runs only when the loaded file declares the bundled version: another version is a
stale copy, which the update check offers to replace. A same-version mismatch is
remembered, so ``flow book`` and every ``flow pay`` command refuse until the file is
replaced (``skill print --output``) or a later ``flow init --skill-path`` matches.

The hash covers what installers leave alone: the instructions, plus ``name`` and
``version``. It cannot cover the raw bytes, because an installer may write the file in
its own layout -- refolding ``description``, dropping frontmatter keys it does not
know, adding or removing the blank line after the frontmatter -- and a genuine install
must not be refused. ``allowed-tools`` is therefore **not** covered, although it is
security-relevant: no installer preserves it, so hashing it would refuse every install
that rewrites it. ``skill-install-check.yml`` installs the skill the way users do and
fails if a real install ever stops matching.

A skill file that resolves into a source checkout is not checked, so developers can
edit the skill in place. The check cannot catch a copy written to evade it, which can
omit ``--skill-path`` or point it at a genuine file; the first read is covered by the
signed release (``gh attestation verify``) and by the harness.
"""
from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .skill_bundle import SKILL_VERSION, skill_text

CODE = "skill_unverified"
PATH_INVALID_CODE = "skill_path_invalid"
REPLACE_COMMAND = "booking-agent skill print --output"

_VERSION_LINE = re.compile(r"^version:\s*[\"']?([^\s\"']+)[\"']?\s*$", re.MULTILINE)
_NAME_VALUE = re.compile(r"^name:\s*[\"']?([^\s\"']+)[\"']?\s*$", re.MULTILINE)
_NAME_LINE = re.compile(r"^name:\s*booking-agent\s*$", re.MULTILINE)


def _split(text: str) -> Tuple[str, str]:
    """``(frontmatter, body)``, with LF line endings and the terminator line dropped."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---", 4)
    if end == -1:
        return "", text
    rest = text[end + 4:]
    newline = rest.find("\n")
    return text[4:end], rest[newline + 1:] if newline != -1 else ""


def normalized(text: str) -> bytes:
    """The instructions in ``text``, with LF endings and no blank lines around them."""
    return (_split(text)[1].strip("\n") + "\n").encode("utf-8")


def frontmatter_description(text: str) -> str:
    """``description`` with every run of whitespace collapsed to one space.

    The value is a block scalar, and an installer may rewrite ``|`` as ``>`` and rewrap
    the lines. Collapsing whitespace makes every folding of the same sentences equal,
    so the description is covered even though its layout is not.
    """
    lines = _frontmatter(text).split("\n")
    collected: list = []
    for index, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        collected.append(line[len("description:"):].strip().lstrip("|>-+0123456789 "))
        for following in lines[index + 1:]:
            if following.strip() and not following.startswith((" ", "\t")):
                break
            collected.append(following.strip())
        break
    return " ".join(" ".join(collected).split())


def canonical(text: str) -> bytes:
    """The parts of a skill file that every installer leaves alone.

    ``name``, ``version`` and ``description`` as values rather than as written, then the
    instructions. Anything an installer may re-serialize is normalized away or left out;
    see the module docstring.
    """
    header = (
        f"name:{frontmatter_name(text) or ''}\n"
        f"version:{frontmatter_version(text) or ''}\n"
        f"description:{frontmatter_description(text)}\n\n"
    )
    return header.encode("utf-8") + normalized(text)


def skill_sha256(text: str) -> str:
    return hashlib.sha256(canonical(text)).hexdigest()


def _frontmatter(text: str) -> str:
    return _split(text)[0]


def frontmatter_version(text: str) -> Optional[str]:
    match = _VERSION_LINE.search(_frontmatter(text))
    return match.group(1) if match else None


def frontmatter_name(text: str) -> Optional[str]:
    match = _NAME_VALUE.search(_frontmatter(text))
    return match.group(1) if match else None


def is_booking_agent_skill(text: str) -> bool:
    return _NAME_LINE.search(_frontmatter(text)) is not None


def is_source_checkout(path: Path) -> bool:
    """``path`` resolves to ``skills/booking-agent/SKILL.md`` of a booking-agent checkout.

    Resolving follows a symlinked file and a symlinked skill directory alike, so the
    ``scripts/install.sh`` link into a checkout counts, while a ``npx skills`` link into
    its own copy under ``.agents/skills`` does not.
    """
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    parents = resolved.parents
    return (
        resolved.name == "SKILL.md"
        and len(parents) > 2
        and parents[0].name == "booking-agent"
        and parents[1].name == "skills"
        and (parents[2] / "packages" / "cli" / "pyproject.toml").is_file()
    )


def _applies(failure: Any) -> bool:
    return isinstance(failure, dict) and failure.get("skill_version") == SKILL_VERSION


def recorded_failure(store: Any) -> Optional[Dict[str, Any]]:
    """The remembered mismatch for this CLI's skill version, if any."""
    failure = store.skill_integrity_failure
    return failure if _applies(failure) else None


def _set_failure(store: Any, failure: Optional[Dict[str, Any]]) -> None:
    from .session import save_store

    if store.skill_integrity_failure != failure:
        store.skill_integrity_failure = failure
        save_store(store)


def clear(store: Any) -> None:
    _set_failure(store, None)


def check(store: Any, skill_path: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(failure, loaded_version)`` for the skill file at ``skill_path``.

    ``failure`` is the remembered mismatch when there is one, and ``loaded_version`` is
    the version the loaded file declares (None when no path was given). Raises
    ``ValueError`` when ``skill_path`` is not a readable booking-agent skill file.
    """
    if skill_path is None:
        return recorded_failure(store), None
    path = Path(skill_path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {type(exc).__name__}") from exc
    if not is_booking_agent_skill(text):
        raise ValueError(f"{path} is not a booking-agent SKILL.md")
    loaded_version = frontmatter_version(text)
    if is_source_checkout(path) or (loaded_version is not None and loaded_version != SKILL_VERSION):
        clear(store)
        return None, loaded_version
    actual = skill_sha256(text)
    if actual == skill_sha256(skill_text()):
        clear(store)
        return None, loaded_version
    _set_failure(store, {
        "skill_version": SKILL_VERSION,
        "skill_path": str(path),
        "actual_sha256": actual,
    })
    return store.skill_integrity_failure, loaded_version


def error_message() -> str:
    return (
        "The booking-agent skill file you loaded has been changed and does not match the "
        "signed skill this assistant was released with, so it will not book or take "
        "payments. Follow data.skill_text, the signed skill, from now on. Tell the user in "
        "plain words that the assistant's instructions file was changed and ask whether to "
        "restore the official one; only with their yes, run data.replace_command, then run "
        "flow init again with --skill-path. Do not try to book another way."
    )


def error_data(failure: Dict[str, Any]) -> Dict[str, Any]:
    path = failure.get("skill_path")
    return {
        "skill_path": path,
        "skill_version": SKILL_VERSION,
        "expected_sha256": skill_sha256(skill_text()),
        "actual_sha256": failure.get("actual_sha256"),
        "replace_command": f"{REPLACE_COMMAND} {shlex.quote(path)}" if path else None,
        "skill_text": skill_text(),
    }


def path_invalid_message(detail: str) -> str:
    return (
        f"--skill-path must be the SKILL.md file you loaded the booking-agent skill from "
        f"({detail}). Run flow init again with that file's path, or without --skill-path if "
        "you do not know it."
    )

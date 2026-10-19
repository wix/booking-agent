"""The base skill runs in any agent runtime, so it names none of them."""
import re
import subprocess
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[4] / "skills" / "booking-agent" / "SKILL.md"

# Harness names, sandbox paths, and token variables from the removed platform-specific overlay.
_HARNESS_SPECIFIC = re.compile(
    r"base44|superagent|composio|GOOGLECALENDAR|AGENTS_ROOT|restore-sandbox|/app/\.|/root/|\.agents/",
    re.IGNORECASE,
)


def _text() -> str:
    return _SKILL.read_text(encoding="utf-8")


def test_skill_names_no_harness_paths_or_token_variables():
    found = sorted({m.group(0) for m in _HARNESS_SPECIFIC.finditer(_text())})
    assert found == []


def test_skill_has_the_agent_supply_runtime_facts():
    text = _text()
    assert "--route connector" in text and "--token-env <NAME>" in text
    assert "--route local-oauth" in text
    assert "--persistent-root" in text and "--no-persistent-root" in text
    assert "booking-agent flow calendar poll" in text
    assert "Never** guess a calendar route or token variable name" in text


def test_skill_keeps_local_sign_in_non_blocking():
    text = _text()
    gate = text[text.index("**Local OAuth route.**"):text.index("### Step 4")]
    assert "returns at once" in gate
    assert "without blocking your turn" in gate
    assert "blocks waiting" not in text


def test_the_repository_holds_exactly_one_skill_md():
    """Installers copy the directory holding a SKILL.md, so a second one is installable."""
    repository = _SKILL.parents[3]
    tracked = subprocess.run(
        ["git", "-C", str(repository), "ls-files"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert [p for p in tracked if p.rsplit("/", 1)[-1] == "SKILL.md"] == [
        "booking-agent/skills/booking-agent/SKILL.md"
    ]


def test_cli_code_names_no_host_either():
    """The fixes must hold on any host, so no host's folder layout is baked into the CLI."""
    src = _SKILL.parents[2] / "packages" / "cli" / "src"
    host = re.compile(r"base44|superagent|composio|AGENTS_ROOT|restore-sandbox|/app/\.", re.IGNORECASE)
    offenders = []
    for path in src.rglob("*.py"):
        if host.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(src)))
    assert offenders == []


# Prose is read by a model and can be skipped; behaviour in the CLI cannot. This ceiling can
# only be raised on purpose, so rules do not creep back into the skill once code enforces them.
_MAX_SKILL_BYTES = 79_000
_MAX_SKILL_LINES = 1_225


def test_skill_stays_within_its_size_budget():
    text = _text()
    assert len(text.encode("utf-8")) <= _MAX_SKILL_BYTES
    assert text.count("\n") + 1 <= _MAX_SKILL_LINES

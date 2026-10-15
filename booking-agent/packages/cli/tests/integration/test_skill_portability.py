"""The base skill runs in any agent runtime, so it names none of them."""
import re
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[4] / "skills" / "booking-agent" / "SKILL.md"

# Harness names, sandbox paths, and token variables from the removed platform-specific overlay.
_HARNESS_SPECIFIC = re.compile(
    r"base44|superagent|composio|GOOGLECALENDAR|AGENTS_ROOT|restore-sandbox|/app/\.|/root/",
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
    assert "booking-agent runtime prepare --persistent-root" in text
    assert "booking-agent flow calendar poll" in text
    assert "Never** guess a calendar route or token variable name" in text


def test_skill_keeps_local_sign_in_non_blocking():
    text = _text()
    gate = text[text.index("**Local OAuth route.**"):text.index("### Step 4")]
    assert "returns at once" in gate
    assert "without blocking your turn" in gate
    assert "blocks waiting" not in text

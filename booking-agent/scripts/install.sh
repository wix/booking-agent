#!/usr/bin/env bash
# Install booking-agent globally on PATH for agents (editable; tracks this repo checkout).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI_PKG="${REPO_ROOT}/packages/cli"
SKILL_NAME="booking-agent"
SKILL_SRC="${REPO_ROOT}/skills/${SKILL_NAME}"
SKILL_LINK="${HOME}/.claude/skills/${SKILL_NAME}"

FORCE=0

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh [options]

Install booking-agent globally via `uv tool install` (editable). Source edits under
packages/cli take effect without reinstalling.

  --force    Reinstall even if booking-agent-cli is already installed
  -h, --help Show this help

Requires: uv (https://docs.astral.sh/uv/)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not on PATH; install from https://docs.astral.sh/uv/" >&2
  exit 1
fi

if [[ ! -f "${CLI_PKG}/pyproject.toml" ]]; then
  echo "error: expected the workspace package at ${CLI_PKG}" >&2
  exit 1
fi

ARGS=(--editable "${CLI_PKG}")
if [[ "${FORCE}" -eq 1 ]]; then
  ARGS+=(--force)
fi

echo "Installing booking-agent from ${REPO_ROOT} (editable)..."
uv tool install "${ARGS[@]}"

if ! command -v booking-agent >/dev/null 2>&1; then
  UV_BIN="${HOME}/.local/bin"
  echo ""
  echo "booking-agent was installed but is not on PATH."
  echo "Add ${UV_BIN} to your PATH, then run: booking-agent --help"
  exit 1
fi

booking-agent --help >/dev/null
echo "Installed: $(command -v booking-agent)"
echo "Editable package: ${CLI_PKG}"

# Register the skill as an editable Personal skill so agents always load the
# latest SKILL.md straight from this checkout (the skill analog of the editable
# CLI install above). `ln -sfn` is idempotent and re-points the link every run,
# so a future rename of the repo skill dir can't silently leave a stale/dangling
# link behind. Invoke it by its bare name (`/booking-agent`) rather than any
# bundled `anthropic-skills:` namespaced copy, which is a frozen snapshot.
if [[ -d "${SKILL_SRC}" ]]; then
  mkdir -p "${HOME}/.claude/skills"
  ln -sfn "${SKILL_SRC}" "${SKILL_LINK}"
  echo "Editable skill: ${SKILL_LINK} -> ${SKILL_SRC}"
else
  echo "warning: skill source ${SKILL_SRC} not found; skipped skill link" >&2
fi

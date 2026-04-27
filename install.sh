#!/usr/bin/env bash
#
# install.sh — set up dependencies for the design-block-generator skill
#
# What this does:
#   1. Verifies prerequisites (Python 3.10+, kicad-cli, claude CLI)
#   2. Installs kicad-sch-api from PyPI (pipx if available, else pip --user)
#   3. Resolves the kicad-sch-api MCP server entry point
#   4. Registers it with Claude Code (`claude mcp add`)
#   5. Installs jsonschema for the pre-flight validator
#   6. Verifies registration with `claude mcp list`
#
# Idempotent — safe to re-run. Will not double-register the MCP server.
#
# Usage:
#   ./install.sh
#   ./install.sh --uninstall    # remove the MCP registration
#   ./install.sh --dry-run      # print what would happen, change nothing

set -euo pipefail

MCP_NAME="kicad-sch-api"
PYPI_PACKAGE="kicad-sch-api"

DRY_RUN=false
UNINSTALL=false

die()  { echo "✗ $*" >&2; exit 1; }
info() { echo "→ $*"; }
ok()   { echo "✓ $*"; }
warn() { echo "⚠ $*" >&2; }

run() {
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

# ---- args -------------------------------------------------------------------
for arg in "$@"; do
  case "$arg" in
    --dry-run)   DRY_RUN=true ;;
    --uninstall) UNINSTALL=true ;;
    -h|--help)   sed -n '3,/^$/p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *)           die "Unknown option: $arg" ;;
  esac
done

# ---- prerequisites ----------------------------------------------------------
info "Checking prerequisites…"

command -v python3 >/dev/null || die "python3 not found"
PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
PY_MAJOR="${PY_VERSION%%.*}"; PY_MINOR="${PY_VERSION##*.}"
if (( PY_MAJOR < 3 )) || { (( PY_MAJOR == 3 )) && (( PY_MINOR < 10 )); }; then
  die "Python 3.10+ required (found $PY_VERSION)"
fi
ok "Python $PY_VERSION"

command -v claude >/dev/null || die "claude CLI not found — install Claude Code first (https://docs.claude.com/en/docs/claude-code)"
ok "claude CLI on PATH"

if command -v kicad-cli >/dev/null; then
  KICAD_VERSION="$(kicad-cli --version 2>&1 | head -1)"
  ok "kicad-cli: $KICAD_VERSION"
else
  warn "kicad-cli not on PATH — needed for post-flight ERC validation. Install KiCad 10+."
fi

# ---- uninstall path ---------------------------------------------------------
if [[ "$UNINSTALL" == "true" ]]; then
  info "Uninstalling…"
  run claude mcp remove "$MCP_NAME" || warn "MCP '$MCP_NAME' was not registered"
  run pip uninstall -y "$PYPI_PACKAGE" || warn "$PYPI_PACKAGE was not installed via pip"
  ok "Uninstalled"
  exit 0
fi

# ---- install kicad-sch-api --------------------------------------------------
info "Installing $PYPI_PACKAGE from PyPI…"

if command -v pipx >/dev/null; then
  run pipx install "$PYPI_PACKAGE" 2>/dev/null || run pipx upgrade "$PYPI_PACKAGE"
  INSTALL_METHOD="pipx"
else
  # Fall back to pip --user. On Debian/Ubuntu with PEP 668 lockdown, may need --break-system-packages.
  if pip install --user "$PYPI_PACKAGE" 2>/dev/null; then
    INSTALL_METHOD="pip --user"
  elif pip install --user --break-system-packages "$PYPI_PACKAGE" 2>/dev/null; then
    INSTALL_METHOD="pip --user --break-system-packages"
  else
    die "Failed to install $PYPI_PACKAGE — install pipx (recommended) or fix your pip setup"
  fi
fi
ok "Installed $PYPI_PACKAGE via $INSTALL_METHOD"

# Also install jsonschema for the pre-flight validator
info "Installing jsonschema (for validate_block_spec.py)…"
pip install --user jsonschema 2>/dev/null \
  || pip install --user --break-system-packages jsonschema 2>/dev/null \
  || warn "jsonschema install failed — validator will run with structural checks only"

# ---- locate the MCP server entry point --------------------------------------
# kicad-sch-api ships its MCP server under one of a few likely names depending
# on packaging. Probe in order; the first hit wins.
info "Resolving kicad-sch-api MCP entry point…"
MCP_CMD=""

# pip --user lands in a python-version-specific bin dir that's often NOT on
# PATH (e.g. ~/Library/Python/X.Y/bin on macOS), so probe it explicitly in
# addition to PATH.
USER_BIN="$(python3 -m site --user-base 2>/dev/null)/bin"

for candidate in kicad-sch-mcp kicad-sch-api-mcp kicad_sch_mcp; do
  if command -v "$candidate" >/dev/null; then
    MCP_CMD="$candidate"
    break
  fi
  if [[ -x "$USER_BIN/$candidate" ]]; then
    MCP_CMD="$USER_BIN/$candidate"
    break
  fi
done

# Fall back to module-style invocation
if [[ -z "$MCP_CMD" ]]; then
  for module in kicad_sch_api.mcp kicad_sch_api.server kicad_sch_api; do
    if python3 -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$module') else 1)" 2>/dev/null; then
      MCP_CMD="python3 -m $module"
      break
    fi
  done
fi

if [[ -z "$MCP_CMD" ]]; then
  cat >&2 <<EOF
✗ Could not auto-detect kicad-sch-api MCP server command.
  Check the package's docs (https://github.com/circuit-synth/kicad-sch-api)
  for the exact invocation, then register it manually:
      claude mcp add $MCP_NAME -- <correct-command>
EOF
  exit 1
fi
ok "MCP entry point: $MCP_CMD"

# ---- register with Claude Code ---------------------------------------------
info "Registering MCP server '$MCP_NAME' with Claude Code…"

# Check if already registered
if claude mcp list 2>/dev/null | grep -q "^$MCP_NAME\b"; then
  warn "MCP '$MCP_NAME' already registered — removing and re-adding to refresh"
  run claude mcp remove "$MCP_NAME" || true
fi

# shellcheck disable=SC2086  # we want word-splitting on $MCP_CMD here
# --scope user: this is a cross-product skill, register globally not per-project
run claude mcp add --scope user "$MCP_NAME" -- $MCP_CMD
ok "Registered (user scope)"

# ---- verify -----------------------------------------------------------------
info "Verifying with 'claude mcp list'…"
if claude mcp list | grep -q "^$MCP_NAME\b"; then
  ok "Verification passed"
else
  warn "Verification failed — '$MCP_NAME' not found in claude mcp list. "
  warn "Restart Claude Code and check manually."
fi

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✓ design-block-generator dependencies installed

  Restart Claude Code if it was already running.

  Next steps:
    1. Confirm tools are exposed: 'claude mcp tools $MCP_NAME'
    2. In Claude Code, ask: "Make a design block for an AMS1117 LDO"
       — the design-block-generator skill will trigger automatically.

  To uninstall: ./install.sh --uninstall
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF

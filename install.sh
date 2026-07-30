#!/usr/bin/env bash
# Install `da` so it works under launchd (and any other process that
# doesn't have TCC access to ~/Documents).
#
# Layout after install:
#   ~/.local/share/da-cli/da         — the shim
#   ~/.local/share/da-cli/dacli/     — the package
#   ~/.local/bin/da                  — symlink → ~/.local/share/da-cli/da
#
# Why copy out of ~/Documents: macOS's TCC blocks user agents (like
# launchd jobs) from reading ~/Documents, ~/Desktop, ~/Downloads etc.
# unless the parent process has Full Disk Access. Stashing the files
# in ~/.local/share avoids the prompt entirely.
#
# Override the install root with DA_INSTALL_PREFIX (default ~/.local).

set -euo pipefail

# Resolve the real source dir even if this script is invoked via a
# symlink (e.g. `~/bin/install-da.sh → …/install.sh`). The `readlink`
# chain handles BSD readlink (no `-f` on older macOS) by manual loop.
resolve_src() {
  local src="$1"
  while [ -L "$src" ]; do
    local dir
    dir="$(cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  (cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd)
}
REPO_DIR="$(resolve_src "${BASH_SOURCE[0]}")"
PREFIX="${DA_INSTALL_PREFIX:-$HOME/.local}"
SHARE_DIR="$PREFIX/share/da-cli"
BIN_DIR="$PREFIX/bin"

if [[ ! -f "$REPO_DIR/da" ]]; then
  echo "error: $REPO_DIR/da not found" >&2
  exit 1
fi
if [[ ! -f "$REPO_DIR/dacli/__init__.py" ]]; then
  echo "error: $REPO_DIR/dacli/ package not found" >&2
  exit 1
fi

mkdir -p "$SHARE_DIR" "$BIN_DIR"

# Copy (don't symlink) so the runtime path lives outside ~/Documents.
install -m 0755 "$REPO_DIR/da" "$SHARE_DIR/da"
# Mirror the package, PRESERVING its directory structure. A flat copy
# collapses dacli/commands/config.py onto dacli/config.py (and the same
# for index.py), which silently produces an installed package that
# cannot import. Remove first so a module deleted upstream does not
# linger here.
rm -rf "$SHARE_DIR/dacli"
(cd "$REPO_DIR" && find dacli -name '__pycache__' -prune -o -name '*.py' -print) \
  | while IFS= read -r rel; do
      install -d -m 0755 "$SHARE_DIR/$(dirname "$rel")"
      install -m 0644 "$REPO_DIR/$rel" "$SHARE_DIR/$rel"
    done

# Shim symlink in $PATH; the shim's realpath() resolves to $SHARE_DIR,
# so the dacli package is imported from the launchd-safe location.
ln -sf "$SHARE_DIR/da" "$BIN_DIR/da"

# Clear bash's per-session command cache so the next `da` invocation
# resolves to the freshly installed symlink, not a stale cached path.
hash -r 2>/dev/null || true

echo "installed:"
echo "  $SHARE_DIR/da"
echo "  $SHARE_DIR/dacli/  ($(find "$SHARE_DIR/dacli" -name '*.py' | wc -l | tr -d ' ') modules)"
echo "  $BIN_DIR/da -> $SHARE_DIR/da"

# Sanity check — but don't let a failure here hide the success banner.
# `set +e` so the PATH hint below still prints if the binary errors.
set +e
echo
# Run the binary we just wrote, by absolute path. Going through PATH
# would test whichever `da` happens to come first — a leftover install
# in /usr/local/bin would report success for a broken one here, and if
# $BIN_DIR is not on PATH at all the check would be skipped entirely,
# which is exactly when it is most worth running.
"$BIN_DIR/da" --version
rc=$?
if [[ $rc -ne 0 ]]; then
  echo "warn: '$BIN_DIR/da --version' exited $rc — the shim could not import dacli." >&2
  echo "      check that $SHARE_DIR/dacli/ is intact." >&2
fi

# Separately: is the copy we just installed the one the user will get?
resolved="$(command -v da 2>/dev/null)"
if [[ -z "$resolved" ]]; then
  echo
  echo "warn: 'da' is not on your PATH."
  echo "      add this to your shell rc:  export PATH=\"$BIN_DIR:\$PATH\""
elif [[ "$(cd "$(dirname "$resolved")" && pwd -P)/$(basename "$resolved")" \
        != "$(cd "$BIN_DIR" && pwd -P)/da" ]]; then
  echo
  echo "warn: 'da' on your PATH is $resolved, not the copy just installed." >&2
  echo "      that older install will keep shadowing this one." >&2
fi

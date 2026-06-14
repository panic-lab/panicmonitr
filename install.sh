#!/bin/sh
# panic-monitr installer — downloads the prebuilt binary for this machine.
#
#   curl -fsSL https://raw.githubusercontent.com/Pallav0099/panicmonitr/main/install.sh | sh
#
# No Python required. Honors these environment variables:
#   PANIC_MONITOR_VERSION   release tag to install (default: latest)
#   PANIC_MONITOR_BINDIR    install directory (default: /usr/local/bin,
#                           or ~/.local/bin when /usr/local/bin isn't writable)
set -eu

REPO="Pallav0099/panicmonitr"
VERSION="${PANIC_MONITOR_VERSION:-latest}"

err()  { printf 'error: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*" >&2; }

# --- platform detection ----------------------------------------------------
[ "$(uname -s)" = "Linux" ] || err "panic-monitr only supports Linux (got $(uname -s))."

case "$(uname -m)" in
  x86_64 | amd64)  ARCH="x86_64" ;;
  aarch64 | arm64) ARCH="aarch64" ;;
  *) err "unsupported architecture: $(uname -m) (only x86_64 and aarch64 are built)." ;;
esac

ASSET="panic-monitor-linux-${ARCH}"

# --- download helper -------------------------------------------------------
if command -v curl >/dev/null 2>&1; then
  dl() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  dl() { wget -qO "$2" "$1"; }
else
  err "need curl or wget to download."
fi

# --- resolve release URL ---------------------------------------------------
if [ "$VERSION" = "latest" ]; then
  BASE="https://github.com/${REPO}/releases/latest/download"
else
  BASE="https://github.com/${REPO}/releases/download/${VERSION}"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

info "Downloading ${ASSET} (${VERSION})..."
dl "${BASE}/${ASSET}" "${TMP}/${ASSET}" || err "download failed: ${BASE}/${ASSET}"

# --- verify checksum (best effort) -----------------------------------------
if dl "${BASE}/checksums.txt" "${TMP}/checksums.txt" 2>/dev/null; then
  EXPECTED="$(grep " ${ASSET}\$" "${TMP}/checksums.txt" | awk '{print $1}')"
  if [ -n "$EXPECTED" ] && command -v sha256sum >/dev/null 2>&1; then
    ACTUAL="$(sha256sum "${TMP}/${ASSET}" | awk '{print $1}')"
    [ "$EXPECTED" = "$ACTUAL" ] || err "checksum mismatch (expected ${EXPECTED}, got ${ACTUAL})."
    info "Checksum verified."
  else
    info "Skipping checksum verification (no entry or sha256sum unavailable)."
  fi
else
  info "Warning: checksums.txt not found; skipping verification."
fi

# --- choose install directory ----------------------------------------------
if [ -n "${PANIC_MONITOR_BINDIR:-}" ]; then
  BINDIR="$PANIC_MONITOR_BINDIR"
elif [ "$(id -u)" = "0" ]; then
  BINDIR="/usr/local/bin"
elif [ -w /usr/local/bin ]; then
  BINDIR="/usr/local/bin"
else
  BINDIR="${HOME}/.local/bin"
fi

mkdir -p "$BINDIR" || err "cannot create ${BINDIR}"
install -m755 "${TMP}/${ASSET}" "${BINDIR}/panic-monitor" \
  || err "cannot install to ${BINDIR}; re-run with sudo, or set PANIC_MONITOR_BINDIR to a writable dir."

info ""
info "Installed panic-monitor -> ${BINDIR}/panic-monitor"

# --- PATH hint -------------------------------------------------------------
case ":${PATH}:" in
  *":${BINDIR}:"*) ;;
  *)
    info "Note: ${BINDIR} is not on your PATH. Add it with:"
    info "  export PATH=\"${BINDIR}:\$PATH\""
    ;;
esac

info ""
info "Next steps:"
info "  panic-monitor --init              # create your cryptographic identity"
info "  panic-monitor --install-service   # run it as a background service"

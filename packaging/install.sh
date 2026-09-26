#!/bin/sh
# Installs mcsm on a Linux computer (a spare PC, a home server, a Raspberry Pi 4/5 with a
# 64-bit OS) and runs its control panel in the background, so you can manage everything
# from a browser on another computer. Run it on that computer, e.g. over SSH:
#
#   ssh you@192.168.1.50 "curl -fsSL https://raw.githubusercontent.com/silverWRX03/mc-server-management/main/packaging/install.sh | sh"
#
# It downloads the newest release for this CPU, checks it against the release's SHA256SUMS,
# puts it in ~/.local/bin (MCSM_BIN_DIR to change), and runs `mcsm service install --panel`,
# which prints the address to open and a one-time password. Nothing is run as root.
set -eu

REPO="silverWRX03/mc-server-management"
BASE="${MCSM_RELEASE_URL:-https://github.com/$REPO/releases/latest/download}"
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64|Linux-amd64) ASSET="mcsm-linux-x64" ;;
  Linux-aarch64|Linux-arm64) ASSET="mcsm-linux-arm64" ;;
  *) echo "mcsm: this installer is for 64-bit Linux (x86_64 or arm64); this is $(uname -s) $(uname -m)." >&2
     echo "On Windows or macOS, download mcsm from https://github.com/$REPO/releases/latest" >&2
     exit 1 ;;
esac
if [ "$(id -u)" = "0" ]; then
  echo "mcsm: please run this as the user that should own the servers, not root" >&2
  echo "      (e.g. make one: sudo adduser minecraft, then ssh minecraft@this-computer)." >&2
  exit 1
fi

BIN_DIR="${MCSM_BIN_DIR:-$HOME/.local/bin}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM
fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -q -O "$2" "$1"
  else echo "mcsm: needs curl or wget" >&2; exit 1; fi
}

echo "Downloading $ASSET ..."
fetch "$BASE/$ASSET" "$tmp/mcsm"
fetch "$BASE/SHA256SUMS.txt" "$tmp/SHA256SUMS.txt"
expected="$(grep " \*\{0,1\}$ASSET\$" "$tmp/SHA256SUMS.txt" | cut -d' ' -f1)"
actual="$(sha256sum "$tmp/mcsm" | cut -d' ' -f1)"
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
  echo "mcsm: the download doesn't match the release's checksum; not installing it" >&2
  exit 1
fi

mkdir -p "$BIN_DIR"
install -m 755 "$tmp/mcsm" "$BIN_DIR/mcsm"
echo "Installed $BIN_DIR/mcsm"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "(add $BIN_DIR to your PATH to type 'mcsm' directly)" ;; esac

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  "$BIN_DIR/mcsm" service install --panel
else
  echo
  echo "This system doesn't use systemd, so mcsm can't start itself at boot here."
  echo "Start it with:  $BIN_DIR/mcsm start --no-browser --web-host 0.0.0.0"
fi

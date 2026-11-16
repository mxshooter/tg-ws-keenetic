#!/bin/sh
# tgwsproxy installer for Keenetic Entware.
#
# Run from inside the unpacked source directory:
#     opkg install python3 python3-cryptography
#     sh scripts/install.sh
#
# After install:
#     /opt/etc/init.d/S99tgwsproxy start
#     http://<router-ip>:1434/

set -e

INSTALL_ROOT=/opt/share/tgwsproxy
CONFIG_DIR=/opt/etc/tgwsproxy
INIT_SCRIPT=/opt/etc/init.d/S99tgwsproxy
LOG_DIR=/opt/var/log/tgwsproxy
RUN_DIR=/opt/var/run

bold()    { printf '\033[1m%s\033[0m\n' "$1"; }
warn()    { printf '\033[33m! %s\033[0m\n' "$1"; }
ok()      { printf '\033[32mok\033[0m %s\n' "$1"; }
die()     { printf '\033[31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || warn "Running as non-root: some paths may need sudo."
[ -d /opt ] || die "/opt not found — install Entware first."
[ -d ./tgwsproxy ] || die "Run this from the project root (need ./tgwsproxy/ folder)."

bold "Checking Python and AES backend..."
PYTHON_BIN=$(command -v python3 || true)
[ -x "$PYTHON_BIN" ] || die "python3 not installed. Run: opkg install python3"

# AES-CTR prefers python3-cryptography, but falls back to the system libcrypto
# (OpenSSL) via ctypes when it is absent. Only fail if neither is usable.
if "$PYTHON_BIN" -c 'import cryptography' >/dev/null 2>&1; then
    ok "python3-cryptography is available"
elif "$PYTHON_BIN" -c '
import ctypes, ctypes.util
name = ctypes.util.find_library("crypto")
ctypes.CDLL(name or "libcrypto.so")
' >/dev/null 2>&1; then
    ok "python3-cryptography missing; using system libcrypto via ctypes"
else
    die "No AES backend. Install python3-cryptography or libopenssl: opkg install python3-cryptography"
fi

# HTTP/2 media multiplexing is optional: it needs httpx + h2. Without them the
# proxy still works, just without the (cheaper) HTTP/2 path for media.
if "$PYTHON_BIN" -c 'import httpx, h2' >/dev/null 2>&1; then
    ok "httpx + h2 present: HTTP/2 media multiplexing available"
else
    warn "httpx/h2 missing: HTTP/2 media disabled (WS fallback still works)."
    warn "  To enable: pip3 install 'httpx[http2]' (or opkg install python3-httpx python3-h2)"
fi

# Self-update and the CF domain refresh talk to GitHub over verified HTTPS.
# Entware does not always ship a CA bundle, and without one those features
# fail with a certificate error; try to install one now.
bold "Checking CA certificates (needed for self-update)..."
CA_STATUS=$("$PYTHON_BIN" - <<'PY' 2>/dev/null || true
import ssl
import urllib.error
import urllib.request
try:
    urllib.request.urlopen("https://api.github.com", timeout=5).close()
except urllib.error.URLError as exc:
    if isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
        print("no-ca")
    else:
        print("offline")
except Exception:
    print("offline")
else:
    print("ok")
PY
)
case "$CA_STATUS" in
    ok)
        ok "HTTPS to GitHub works"
        ;;
    no-ca)
        warn "No usable CA bundle: web self-update will fail with a TLS error."
        if command -v opkg >/dev/null 2>&1; then
            warn "Trying to install ca-certificates..."
            opkg install ca-certificates >/dev/null 2>&1 \
                || opkg install ca-bundle >/dev/null 2>&1 \
                || true
        fi
        if "$PYTHON_BIN" -c 'import ssl,urllib.request; urllib.request.urlopen("https://api.github.com", timeout=5).close()' >/dev/null 2>&1; then
            ok "CA bundle installed"
        else
            warn "Still no trusted CA; install one manually:"
            warn "  opkg install ca-certificates   (or: pip3 install certifi)"
            warn "The proxy itself keeps working; only in-app updates are affected."
        fi
        ;;
    *)
        warn "GitHub is unreachable; skipping the CA check (proxy still works)."
        warn "Self-update needs outbound HTTPS to api.github.com."
        ;;
esac

bold "Creating directories..."
mkdir -p "$INSTALL_ROOT" "$CONFIG_DIR" "$LOG_DIR" "$RUN_DIR"
ok "Directories ready"

bold "Stopping existing service (if any)..."
if [ -x "$INIT_SCRIPT" ]; then
    "$INIT_SCRIPT" stop || true
fi

bold "Copying source..."
# Keep code separate from data so upgrades are clean.
rm -rf "$INSTALL_ROOT/tgwsproxy" "$INSTALL_ROOT/webui"
cp -r ./tgwsproxy "$INSTALL_ROOT/tgwsproxy"
cp -r ./webui     "$INSTALL_ROOT/webui"
ok "Code copied to $INSTALL_ROOT"

bold "Installing init script..."
cp ./etc/init.d/S99tgwsproxy "$INIT_SCRIPT"
chmod +x "$INIT_SCRIPT"
ok "Init script installed at $INIT_SCRIPT"

if [ ! -f "$CONFIG_DIR/config.json" ]; then
    bold "Generating default config..."
    "$PYTHON_BIN" -m tgwsproxy --config "$CONFIG_DIR/config.json" --init-config
    ok "Default config written to $CONFIG_DIR/config.json"
else
    ok "Config already exists at $CONFIG_DIR/config.json (kept)"
fi

bold "Starting service..."
"$INIT_SCRIPT" start

echo
bold "tgwsproxy installed."
echo "  - Init script: $INIT_SCRIPT"
echo "  - Config:      $CONFIG_DIR/config.json"
echo "  - Logs:        $LOG_DIR/"
echo "  - Web UI:      http://<router-ip>:$(grep -E '"web_port"' "$CONFIG_DIR/config.json" | tr -dc 0-9)/"
echo "  - Proxy:       <router-ip>:$(grep -E '"port"' "$CONFIG_DIR/config.json" | head -n1 | tr -dc 0-9)"
echo
echo "Get the tg:// link with:"
echo "    python3 -m tgwsproxy --config $CONFIG_DIR/config.json --print-link"

#!/usr/bin/env bash
# Install / update the monitoring panel with one command:
#   curl -fsSL https://raw.githubusercontent.com/USER/REPO/main/install.sh | sudo bash
#
# Before uploading to GitHub, change the REPO value below (or pass it as a variable when running):
#   curl -fsSL ... | sudo REPO=user/repo bash
set -euo pipefail

REPO="${REPO:-mohmd-h/server-monitor}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/server-monitor}"
SERVICE="server-monitor"
URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}/monitor.py"

[ "$(id -u)" -eq 0 ] || { echo "Run as root (use sudo bash)"; exit 1; }
case "$REPO" in YOUR_USER/*) echo "First edit the REPO value inside install.sh (set it to YOUR_GITHUB_USER/YOUR_REPO)"; exit 1;; esac

# --- python3 ---
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found, trying to install it..."
  if   command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3
  elif command -v dnf     >/dev/null; then dnf install -y -q python3
  elif command -v yum     >/dev/null; then yum install -y -q python3
  elif command -v apk     >/dev/null; then apk add --no-cache python3
  else echo "Please install python3 manually"; exit 1; fi
fi
PY="$(command -v python3)"

# --- download (if already installed this updates the script; history and settings are kept) ---
mkdir -p "$DIR"
TMP="$(mktemp)"
if command -v curl >/dev/null; then curl -fsSL "$URL" -o "$TMP"
elif command -v wget >/dev/null; then wget -qO "$TMP" "$URL"
else echo "curl or wget is required"; exit 1; fi
"$PY" -m py_compile "$TMP" || { echo "Downloaded file is not valid Python"; rm -f "$TMP"; exit 1; }
install -m 755 "$TMP" "$DIR/monitor.py"
rm -f "$TMP"

# --- run ---
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Lightweight server monitor panel
After=network.target

[Service]
ExecStart=${PY} ${DIR}/monitor.py
WorkingDirectory=${DIR}
Restart=always
RestartSec=5
Nice=10

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1
  systemctl restart "$SERVICE"
else
  echo "systemd not found; starting with nohup (you must start it again manually after a reboot)"
  pkill -f "$DIR/monitor.py" 2>/dev/null || true
  (cd "$DIR" && nohup "$PY" monitor.py >"$DIR/monitor.log" 2>&1 &)
fi

# --- print the URL ---
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$DIR/monitor_config.json" ] && break; sleep 0.5; done
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"; IP="${IP:-SERVER_IP}"
"$PY" - "$DIR/monitor_config.json" "$IP" <<'EOF'
import json, sys
c = json.load(open(sys.argv[1]))
print("\nInstalled. Panel URL:\n   http://%s:%s/%s/\n" % (sys.argv[2], c["port"], c["token"]))
print("If it does not open from outside, open port %s in your firewall." % c["port"])
EOF

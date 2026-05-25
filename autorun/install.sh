#!/bin/bash
set -euo pipefail

error() { echo "ERROR: $*" >&2; exit 1; }
info()  { echo "==> $*"; }

# ── Privilege check ───────────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || error "Run this script with sudo: sudo $0"
REAL_USER="${SUDO_USER:-}"
[ -n "$REAL_USER" ] || error "Run with sudo (not as root directly) so the user account is known."
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Locate the conda environment binaries ────────────────────────────────────
# On Fedora Silverblue the project runs from a conda environment, so we need
# the absolute path to the env's bin/ to embed in the service ExecStart lines.
find_env_bin() {
    local env_name="ancs4linux"
    for base in \
        "$REAL_HOME/.conda" \
        "$REAL_HOME/miniforge3" \
        "$REAL_HOME/mambaforge" \
        "$REAL_HOME/miniconda3" \
        "$REAL_HOME/anaconda3" \
        /opt/conda /opt/miniforge3 /opt/mambaforge; do
        local candidate="$base/envs/$env_name/bin"
        if [ -x "$candidate/ancs4linux-observer" ]; then
            echo "$candidate"
            return 0
        fi
    done
    # Fallback: ask the user's login shell (works if conda auto-activates)
    local result
    result="$(sudo -u "$REAL_USER" bash -lc \
        "which ancs4linux-observer 2>/dev/null" 2>/dev/null | xargs -r dirname)" || true
    if [ -n "$result" ] && [ -x "$result/ancs4linux-observer" ]; then
        echo "$result"
        return 0
    fi
    return 1
}

info "Locating ancs4linux binaries..."
ENV_BIN="$(find_env_bin)" || {
    cat >&2 <<'MSG'

Could not find the ancs4linux conda environment. Set it up first, then
re-run this script:

  conda create -n ancs4linux python=3.11
  conda activate ancs4linux
  conda install -c conda-forge pygobject
  pip install -e /path/to/ancs4linux

MSG
    exit 1
}
info "Found binaries in: $ENV_BIN"

# ── Group setup ───────────────────────────────────────────────────────────────
info "Creating ancs4linux group and adding $REAL_USER..."
groupadd -f ancs4linux
usermod -a -G ancs4linux "$REAL_USER"
info "Group membership takes effect at next login."

# ── D-Bus policy files ────────────────────────────────────────────────────────
# /etc/dbus-1/system.d/ is writable on Silverblue.
info "Installing D-Bus policy files..."
install -Dm 644 "$SCRIPT_DIR/ancs4linux-observer.xml"    /etc/dbus-1/system.d/ancs4linux-observer.conf
install -Dm 644 "$SCRIPT_DIR/ancs4linux-advertising.xml" /etc/dbus-1/system.d/ancs4linux-advertising.conf
systemctl reload dbus.service

# ── System service files ──────────────────────────────────────────────────────
# On Silverblue /usr/lib/systemd/ is read-only; /etc/systemd/ is writable.
info "Writing system service files to /etc/systemd/system/..."
mkdir -p /etc/systemd/system

cat > /etc/systemd/system/ancs4linux-observer.service <<EOF
[Unit]
Description=ancs4linux Observer daemon
Requires=bluetooth.service
After=bluetooth.service

[Service]
Type=dbus
BusName=ancs4linux.Observer
ExecStart=$ENV_BIN/ancs4linux-observer

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/ancs4linux-advertising.service <<EOF
[Unit]
Description=ancs4linux Advertising daemon
Requires=bluetooth.service
After=bluetooth.service

[Service]
Type=dbus
BusName=ancs4linux.Advertising
ExecStart=$ENV_BIN/ancs4linux-advertising

[Install]
WantedBy=multi-user.target
EOF

# ── User service files ────────────────────────────────────────────────────────
info "Writing user service files to /etc/systemd/user/..."
mkdir -p /etc/systemd/user

cat > /etc/systemd/user/ancs4linux-desktop-integration.service <<EOF
[Unit]
Description=ancs4linux Desktop Integration daemon
Requires=bluetooth.target
After=bluetooth.target

[Service]
Type=simple
ExecStart=$ENV_BIN/ancs4linux-desktop-integration
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# ── SELinux file context (Fedora Silverblue) ─────────────────────────────────
# Binaries in the user home directory carry user_home_t by default, which
# systemd (running as root) cannot execute. Relabel the entire conda env so
# system services can exec the binaries and load their shared libraries.
if [ -d /sys/fs/selinux ]; then
    ENV_ROOT="$(dirname "$ENV_BIN")"
    info "Configuring SELinux file context for conda env ($ENV_ROOT)..."
    if command -v semanage &>/dev/null; then
        # -a adds the rule; if it already exists, -m modifies it instead
        semanage fcontext -a -t bin_t "${ENV_ROOT}(/.*)?" 2>/dev/null || \
            semanage fcontext -m -t bin_t "${ENV_ROOT}(/.*)?"
        restorecon -Rv "$ENV_ROOT"
    else
        info "semanage not found — using chcon (not persistent across SELinux relabel)."
        info "Install policycoreutils-python-utils for a persistent fix."
        chcon -R -t bin_t "$ENV_ROOT"
    fi
fi

# Wrapper script in /etc/ancs4linux/ (writable on Silverblue, unlike /usr/local).
# Detects the first HCI address at runtime; reads an optional device name from
# ~/.config/ancs4linux/advertising.env (ANCS4LINUX_DEVICE_NAME=my-laptop).
info "Writing enable-advertising wrapper script..."
mkdir -p /etc/ancs4linux
cat > /etc/ancs4linux/enable-advertising <<EOF
#!/bin/bash
set -euo pipefail
addr="\$($ENV_BIN/ancs4linux-ctl get-all-hci | python3 -c 'import sys,json; print(json.load(sys.stdin)[0])')"
exec "$ENV_BIN/ancs4linux-ctl" enable-advertising --hci-address="\$addr" --name="\${ANCS4LINUX_DEVICE_NAME:-\$(hostname -s)}"
EOF
chmod 755 /etc/ancs4linux/enable-advertising

cat > /etc/systemd/user/ancs4linux-enable-advertising.service <<EOF
[Unit]
Description=Enable ancs4linux Bluetooth advertising
After=ancs4linux-desktop-integration.service
Wants=ancs4linux-desktop-integration.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/etc/ancs4linux/enable-advertising
EnvironmentFile=-%h/.config/ancs4linux/advertising.env

[Install]
WantedBy=default.target
EOF

# ── WirePlumber config ────────────────────────────────────────────────────────
# Prevent the iPhone (or any paired Bluetooth phone) from routing audio to this
# computer. WirePlumber matches on device.icon = "phone" and sets the audio
# profile to "off", so classic BT audio (A2DP/HFP) is suppressed while the BLE
# ANCS connection is unaffected.
WP_CONF_DIR="$REAL_HOME/.config/wireplumber/wireplumber.conf.d"
info "Installing WirePlumber rule to disable Bluetooth phone audio..."
install -Dm 644 -o "$REAL_USER" "$SCRIPT_DIR/51-disable-phone-audio.conf" \
    "$WP_CONF_DIR/51-disable-phone-audio.conf"

# Restart WirePlumber if it is already running for this user session.
WP_RUNTIME="/run/user/$(id -u "$REAL_USER")"
if sudo -u "$REAL_USER" XDG_RUNTIME_DIR="$WP_RUNTIME" \
        systemctl --user is-active --quiet wireplumber 2>/dev/null; then
    info "Restarting WirePlumber..."
    sudo -u "$REAL_USER" XDG_RUNTIME_DIR="$WP_RUNTIME" \
        systemctl --user restart wireplumber
else
    info "WirePlumber is not running yet — rule will take effect at next login."
fi

# ── Enable and start ──────────────────────────────────────────────────────────
info "Reloading systemd daemon..."
systemctl daemon-reload

info "Enabling and starting system services..."
systemctl enable ancs4linux-observer.service
systemctl enable ancs4linux-advertising.service
systemctl restart ancs4linux-observer.service
systemctl restart ancs4linux-advertising.service

info "Enabling user services for all users..."
systemctl --global enable ancs4linux-desktop-integration.service
systemctl --global enable ancs4linux-enable-advertising.service

cat <<'MSG'

System services are running. Start the user services for your current session:

  systemctl --user daemon-reload
  systemctl --user start ancs4linux-desktop-integration.service
  systemctl --user start ancs4linux-enable-advertising.service

To use a custom Bluetooth device name instead of the system hostname, create
~/.config/ancs4linux/advertising.env containing:

  ANCS4LINUX_DEVICE_NAME=my-laptop

MSG

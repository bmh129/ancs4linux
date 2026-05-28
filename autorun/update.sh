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
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ── Locate pip in the conda environment ──────────────────────────────────────
find_env_pip() {
    local env_name="ancs4linux"
    for base in \
        "$REAL_HOME/.conda" \
        "$REAL_HOME/miniforge3" \
        "$REAL_HOME/mambaforge" \
        "$REAL_HOME/miniconda3" \
        "$REAL_HOME/anaconda3" \
        /opt/conda /opt/miniforge3 /opt/mambaforge; do
        local candidate="$base/envs/$env_name/bin/pip"
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    local result
    result="$(sudo -u "$REAL_USER" bash -lc \
        "which pip 2>/dev/null" 2>/dev/null)" || true
    if [ -n "$result" ] && [ -x "$result" ]; then
        echo "$result"
        return 0
    fi
    return 1
}

info "Locating ancs4linux conda environment..."
ENV_PIP="$(find_env_pip)" || {
    cat >&2 <<'MSG'

Could not find the ancs4linux conda environment. Run install.sh first:

  cd autorun && sudo ./install.sh

MSG
    exit 1
}
info "Found pip at: $ENV_PIP"

# ── Reinstall package (picks up new dependencies if any) ─────────────────────
info "Reinstalling ancs4linux package..."
sudo -u "$REAL_USER" "$ENV_PIP" install -e "$PROJECT_DIR" --quiet

# ── Update WirePlumber config ─────────────────────────────────────────────────
WIREPLUMBER_CONF_DIR="$REAL_HOME/.config/wireplumber/wireplumber.conf.d"
WIREPLUMBER_CONF="$WIREPLUMBER_CONF_DIR/51-disable-phone-audio.conf"
if [ -f "$WIREPLUMBER_CONF" ]; then
    info "Updating WirePlumber phone audio rule..."
    cp "$SCRIPT_DIR/51-disable-phone-audio.conf" "$WIREPLUMBER_CONF"
    chown "$REAL_USER" "$WIREPLUMBER_CONF"
    WP_RUNTIME="/run/user/$(id -u "$REAL_USER")"
    sudo -u "$REAL_USER" XDG_RUNTIME_DIR="$WP_RUNTIME" systemctl --user restart wireplumber
fi

# ── Restart system services ───────────────────────────────────────────────────
info "Restarting system services..."
systemctl restart ancs4linux-observer.service
systemctl restart ancs4linux-advertising.service

cat <<'MSG'

System services restarted. To pick up changes in your current session:

  systemctl --user restart ancs4linux-desktop-integration.service
  systemctl --user restart ancs4linux-enable-advertising.service

MSG

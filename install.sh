#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_TV_IP="192.168.1.30"
CONF_DIR="/etc/lgtvcontrol"
SERVICE_DIR="/etc/systemd/system"
SELECTED_DISPLAY_KARG=""

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { printf "${GREEN}  [OK]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}  [!!]${NC} %s\n" "$*"; }
info() { printf "\n${BOLD}==> %s${NC}\n" "$*"; }
die()  { printf "${RED} [ERR]${NC} %s\n" "$*" >&2; exit 1; }

cmd_exists()       { command -v "$1" >/dev/null 2>&1; }
selinux_enforcing(){ cmd_exists getenforce && [[ "$(getenforce 2>/dev/null)" == "Enforcing" ]]; }

semanage_add() {
    sudo semanage fcontext -a -t bin_t "$1" 2>/dev/null \
        || sudo semanage fcontext -m -t bin_t "$1"
}

detect_drm_connectors() {
    DRM_NAMES=()
    DRM_PATHS=()
    DRM_STATES=()

    local status_file dir base name state

    for status_file in /sys/class/drm/card*-*/status; do
        [[ -e "$status_file" ]] || continue

        dir="${status_file%/status}"
        base="$(basename "$dir")"
        name="$(printf '%s\n' "$base" | sed -E 's/^card[0-9]+-//')"
        state="$(cat "$status_file" 2>/dev/null || printf "unknown")"

        # Ignore DRM writeback pseudo-connectors.
        [[ "$name" == Writeback-* ]] && continue

        DRM_NAMES+=("$name")
        DRM_PATHS+=("$dir")
        DRM_STATES+=("$state")
    done
}

configure_display_override() {
    info "Display mode override"

    SELECTED_DISPLAY_KARG=""

    if ! cmd_exists rpm-ostree; then
        warn "rpm-ostree not found — skipping automatic display mode override"
        warn "If Bazzite boots to 800x600 when the TV is off, manually add a kernel video= argument."
        return
    fi

    echo "  Some LG TVs stop exposing HDMI information after entering deep standby."
    echo "  If the PC boots while that happens, Bazzite may fall back to 800x600"
    echo "  or start KDE with a broken desktop."
    echo
    echo "  This installer can force a selected display connector to a known mode at boot."
    echo "  It uses rpm-ostree kargs --append-if-missing, so rerunning install is safe."
    echo

    local answer
    read -rp "$(printf "  Force a boot display mode to prevent 800x600 fallback? [Y/n]: ")" answer
    answer="${answer:-Y}"
    if [[ ! "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        warn "Skipping display mode override"
        return
    fi

    detect_drm_connectors

    if [[ "${#DRM_NAMES[@]}" -eq 0 ]]; then
        warn "No DRM display connectors found under /sys/class/drm"
        warn "Skipping display mode override"
        return
    fi

    local ordered_indexes=()
    local i

    # Connected connectors first.
    for i in "${!DRM_NAMES[@]}"; do
        if [[ "${DRM_STATES[$i]}" == "connected" ]]; then
            ordered_indexes+=("$i")
        fi
    done

    # Then everything else.
    for i in "${!DRM_NAMES[@]}"; do
        if [[ "${DRM_STATES[$i]}" != "connected" ]]; then
            ordered_indexes+=("$i")
        fi
    done

    echo "  Detected display connectors:"

    local n=1
    local idx modes_file mode_preview
    for idx in "${ordered_indexes[@]}"; do
        modes_file="${DRM_PATHS[$idx]}/modes"
        mode_preview="$(head -n 1 "$modes_file" 2>/dev/null || true)"
        [[ -n "$mode_preview" ]] || mode_preview="no modes listed"

        printf "    %d) %-12s %-12s %s\n" \
            "$n" "${DRM_NAMES[$idx]}" "${DRM_STATES[$idx]}" "$mode_preview"
        ((n++))
    done
    echo

    if [[ "${DRM_STATES[${ordered_indexes[0]}]}" != "connected" ]]; then
        warn "No connected display was detected."
        warn "If the TV is off, turn it on and rerun the installer for best auto-detection."
    fi

    local choice
    read -rp "$(printf "  Select TV connector [1]: ")" choice
    choice="${choice:-1}"

    if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > ${#ordered_indexes[@]} )); then
        warn "Invalid connector selection — skipping display mode override"
        return
    fi

    idx="${ordered_indexes[$((choice - 1))]}"

    local connector connector_path default_res default_mode selected_mode
    connector="${DRM_NAMES[$idx]}"
    connector_path="${DRM_PATHS[$idx]}"

    default_res="$(head -n 1 "$connector_path/modes" 2>/dev/null || true)"
    default_res="${default_res:-3840x2160}"
    default_mode="${default_res}@60"

    read -rp "$(printf "  Display mode [%s]: " "$default_mode")" selected_mode
    selected_mode="${selected_mode:-$default_mode}"

    # If user typed only a resolution, add @60.
    if [[ "$selected_mode" != *@* ]]; then
        selected_mode="${selected_mode}@60"
    fi

    SELECTED_DISPLAY_KARG="video=${connector}:${selected_mode}D"

    echo
    printf "  Selected display override: %s\n" "$SELECTED_DISPLAY_KARG"
    read -rp "$(printf "  Use this display mode override? [Y/n]: ")" answer
    answer="${answer:-Y}"

    if [[ ! "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        SELECTED_DISPLAY_KARG=""
        warn "Skipping display mode override"
        return
    fi

    ok "Display mode override selected: $SELECTED_DISPLAY_KARG"
}

apply_display_override() {
    [[ -n "$SELECTED_DISPLAY_KARG" ]] || return 0

    info "Applying display mode override"

    sudo rpm-ostree kargs --append-if-missing="$SELECTED_DISPLAY_KARG"
    echo "$SELECTED_DISPLAY_KARG" | sudo tee "$CONF_DIR/display_karg" > /dev/null
    sudo chmod 644 "$CONF_DIR/display_karg"

    ok "Kernel argument present: $SELECTED_DISPLAY_KARG"
    warn "Reboot required for display mode override to take effect"
}

# ---------------------------------------------------------------------------
# Test mode — turn screen off, countdown, turn screen on
# ---------------------------------------------------------------------------
run_test() {
    [[ -x "$CONF_DIR/lgtv-off.sh" && -x "$CONF_DIR/lgtv-on.sh" ]] \
        || die "Scripts not found in $CONF_DIR — run install first."

    info "Screen test"
    printf "  Turning screen OFF... "
    "$CONF_DIR/lgtv-off.sh" && printf "${GREEN}done${NC}\n" \
        || { printf "${RED}FAILED${NC}\n"; exit 1; }

    for i in {10..1}; do
        printf "\r  Turning screen back ON in %2d seconds... " "$i"
        sleep 1
    done
    printf "\r  Turning screen ON...                         \n"

    "$CONF_DIR/lgtv-on.sh" && printf "  ${GREEN}done${NC}\n" \
        || { printf "  ${RED}FAILED${NC}\n"; exit 1; }

    echo
    ok "Test complete — screen should be on."
}

if [[ "${1:-}" == "--test" ]]; then
    run_test
    exit 0
fi

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
info "Checking dependencies"

cmd_exists python3 || die "python3 not found. Install it first."
ok "python3"

if selinux_enforcing; then
    cmd_exists semanage \
        || die "semanage not found. Install: sudo dnf install policycoreutils-python-utils"
    ok "semanage (SELinux is Enforcing)"
    cmd_exists restorecon && ok "restorecon" \
        || warn "restorecon not found — install policycoreutils for best results"
else
    warn "SELinux is not Enforcing — skipping SELinux context steps"
fi

# ---------------------------------------------------------------------------
# Gather config
# ---------------------------------------------------------------------------
info "Configuration"

read -rp "$(printf "  LG TV IP address [${DEFAULT_TV_IP}]: ")" TV_IP
TV_IP="${TV_IP:-$DEFAULT_TV_IP}"

if [[ ! "$TV_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    die "'$TV_IP' is not a valid IPv4 address"
fi
ok "TV IP: $TV_IP"

# ---------------------------------------------------------------------------
# Display mode override
# ---------------------------------------------------------------------------
configure_display_override

# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------
echo
printf "  Scripts:  %s\n" "$CONF_DIR"
printf "  Services: %s\n" "$SERVICE_DIR"
if [[ -n "$SELECTED_DISPLAY_KARG" ]]; then
    printf "  Display:  %s\n" "$SELECTED_DISPLAY_KARG"
fi
echo
read -rp "$(printf "${BOLD}Continue? [Y/n]: ${NC}")" answer
answer="${answer:-Y}"
[[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || { echo "Aborted."; exit 0; }

# ---------------------------------------------------------------------------
# Install scripts
# ---------------------------------------------------------------------------
info "Installing scripts to $CONF_DIR"

sudo mkdir -p "$CONF_DIR"

apply_display_override

# TV IP (read by lgtv.py at runtime)
echo "${TV_IP}" | sudo tee "$CONF_DIR/tv_ip" > /dev/null
sudo chmod 644 "$CONF_DIR/tv_ip"
ok "tv_ip"

# lgtv.py
sudo cp "$SCRIPT_DIR/scripts/lgtv.py" "$CONF_DIR/lgtv.py"
sudo chmod 755 "$CONF_DIR/lgtv.py"
ok "lgtv.py"

# Reference config (optional tunables) — copy to "config" to activate.
sudo cp "$SCRIPT_DIR/config.example" "$CONF_DIR/config.example"
sudo chmod 644 "$CONF_DIR/config.example"
ok "config.example (copy to $CONF_DIR/config to tune retries)"

# Shell wrappers
for cmd in on off; do
    sudo cp "$SCRIPT_DIR/scripts/lgtv-${cmd}.sh.tpl" "$CONF_DIR/lgtv-${cmd}.sh"
    sudo chmod 755 "$CONF_DIR/lgtv-${cmd}.sh"
    ok "lgtv-${cmd}.sh"
done

# ---------------------------------------------------------------------------
# SELinux file contexts
# ---------------------------------------------------------------------------
if selinux_enforcing; then
    info "Applying SELinux file contexts"

    semanage_add "${CONF_DIR}/lgtv-on\.sh"
    semanage_add "${CONF_DIR}/lgtv-off\.sh"
    semanage_add "${CONF_DIR}/lgtv\.py"

    if cmd_exists restorecon; then
        sudo restorecon -Rv "$CONF_DIR/" > /dev/null
        ok "Contexts applied and restored"
    else
        warn "restorecon not found — reboot or run: sudo restorecon -Rv $CONF_DIR/"
    fi
fi

# ---------------------------------------------------------------------------
# Pair with TV
# ---------------------------------------------------------------------------
info "Pairing with TV at $TV_IP"

if [[ -f "$CONF_DIR/client.key" ]]; then
    warn "Existing client key found."
    read -rp "$(printf "  Re-pair with TV? [y/N]: ")" repair
    repair="${repair:-N}"
    if [[ ! "$repair" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        ok "Keeping existing key"
    else
        sudo python3 "$CONF_DIR/lgtv.py" pair || die "Pairing failed"
    fi
else
    echo "  Make sure your TV is on and reachable at $TV_IP"
    echo "  A prompt will appear on the TV — press OK with your remote."
    echo
    sudo python3 "$CONF_DIR/lgtv.py" pair || die "Pairing failed. Ensure TV is on and try again."
fi

# ---------------------------------------------------------------------------
# Detect TV MAC address for Wake-on-LAN
# ---------------------------------------------------------------------------
info "Detecting TV MAC address (Wake-on-LAN)"

# Ping once to ensure the ARP cache is fresh, then read /proc/net/arp
ping -c 1 -W 2 "$TV_IP" > /dev/null 2>&1 || true
tv_mac=$(awk -v ip="$TV_IP" '$1==ip && $4!="00:00:00:00:00:00" { print $4 }' /proc/net/arp 2>/dev/null || true)

if [[ -n "$tv_mac" ]]; then
    echo "$tv_mac" | sudo tee "$CONF_DIR/tv_mac" > /dev/null
    sudo chmod 644 "$CONF_DIR/tv_mac"
    ok "TV MAC: $tv_mac (saved for Wake-on-LAN)"
else
    warn "Could not detect TV MAC — Wake-on-LAN will not be available"
    warn "If needed: echo 'AA:BB:CC:DD:EE:FF' | sudo tee $CONF_DIR/tv_mac"
fi

# ---------------------------------------------------------------------------
# Systemd services
# ---------------------------------------------------------------------------
info "Installing systemd services"

for svc in lgtv-startup lgtv-shutdown lgtv-sleep; do
    sudo cp "$SCRIPT_DIR/systemd/${svc}.service" "$SERVICE_DIR/"
    ok "${svc}.service"
done

sudo systemctl daemon-reload
sudo systemctl enable lgtv-startup.service lgtv-shutdown.service lgtv-sleep.service
ok "Services enabled"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
info "Installation complete"
echo
printf "  Useful commands:\n"
printf "    Test:   ./install.sh --test\n"
printf "    Logs:   journalctl -u lgtv-startup -u lgtv-shutdown -u lgtv-sleep -f\n"
printf "    Re-pair: sudo python3 %s/lgtv.py pair\n" "$CONF_DIR"
if [[ -n "$SELECTED_DISPLAY_KARG" ]]; then
    printf "    Display: reboot to apply %s\n" "$SELECTED_DISPLAY_KARG"
fi
echo

# shellcheck disable=SC2059
read -rp "$(printf "${BOLD}Run screen test now? (off 10s → on) [Y/n]: ${NC}")" answer
answer="${answer:-Y}"
if [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    run_test
fi
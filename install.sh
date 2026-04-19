#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_TV_IP="192.168.1.30"
VENV_DIR="/opt/lgtvcontrol"
CONF_DIR="/etc/lgtvcontrol"
SERVICE_DIR="/etc/systemd/system"

# --- Colors ---
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { printf "${GREEN}  [OK]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}  [!!]${NC} %s\n" "$*"; }
info() { printf "\n${BOLD}==> %s${NC}\n" "$*"; }
die()  { printf "${RED} [ERR]${NC} %s\n" "$*" >&2; exit 1; }

cmd_exists()       { command -v "$1" >/dev/null 2>&1; }
selinux_enforcing(){ cmd_exists getenforce && [[ "$(getenforce 2>/dev/null)" == "Enforcing" ]]; }

semanage_add() {
    # Add fcontext rule, silently switch to modify if it already exists
    sudo semanage fcontext -a -t bin_t "$1" 2>/dev/null \
        || sudo semanage fcontext -m -t bin_t "$1"
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

SQLITE_SRC="$HOME/.aiopylgtv.sqlite"
if [[ ! -f "$SQLITE_SRC" ]]; then
    die "Auth database not found at $SQLITE_SRC
       Pair your TV first by running:
         bscpylgtvcommand $TV_IP turn_screen_on
       Accept the prompt on the TV, then re-run this installer."
fi
ok "Auth database: $SQLITE_SRC"

# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------
echo
printf "  Venv:     %s\n" "$VENV_DIR"
printf "  Scripts:  %s\n" "$CONF_DIR"
printf "  Services: %s\n" "$SERVICE_DIR"
echo
read -rp "$(printf "${BOLD}Continue? [Y/n]: ${NC}")" answer
answer="${answer:-Y}"
[[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || { echo "Aborted."; exit 0; }

# ---------------------------------------------------------------------------
# Install bscpylgtv venv
# ---------------------------------------------------------------------------
info "Installing bscpylgtv to $VENV_DIR"

if [[ -f "$VENV_DIR/bin/bscpylgtvcommand" ]]; then
    ok "Already installed — skipping venv creation"
else
    sudo python3 -m venv "$VENV_DIR"
    sudo "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    sudo "$VENV_DIR/bin/pip" install --quiet bscpylgtv
    ok "bscpylgtv installed"
fi

# ---------------------------------------------------------------------------
# Install scripts and auth DB
# ---------------------------------------------------------------------------
info "Installing scripts to $CONF_DIR"

sudo mkdir -p "$CONF_DIR"

sudo cp "$SQLITE_SRC" "$CONF_DIR/.aiopylgtv.sqlite"
sudo chmod 644 "$CONF_DIR/.aiopylgtv.sqlite"
ok "Auth database copied"

for cmd in on off; do
    sudo sed "s|TV_IP|${TV_IP}|g" "$SCRIPT_DIR/scripts/lgtv-${cmd}.sh.tpl" \
        | sudo tee "$CONF_DIR/lgtv-${cmd}.sh" > /dev/null
    sudo chmod 755 "$CONF_DIR/lgtv-${cmd}.sh"
    ok "lgtv-${cmd}.sh"
done

# ---------------------------------------------------------------------------
# SELinux file contexts
# ---------------------------------------------------------------------------
if selinux_enforcing; then
    info "Applying SELinux file contexts"

    semanage_add "${VENV_DIR}/bin(/.*)?"
    semanage_add "${CONF_DIR}/lgtv-on\.sh"
    semanage_add "${CONF_DIR}/lgtv-off\.sh"

    if cmd_exists restorecon; then
        sudo restorecon -Rv "$VENV_DIR/bin/" "$CONF_DIR/" > /dev/null
        ok "Contexts applied and restored"
    else
        warn "restorecon not found — reboot or run: sudo restorecon -Rv $VENV_DIR/bin/ $CONF_DIR/"
    fi
fi

# ---------------------------------------------------------------------------
# Systemd services
# ---------------------------------------------------------------------------
info "Installing systemd services"

for svc in lgtv-startup lgtv-shutdown; do
    sudo cp "$SCRIPT_DIR/systemd/${svc}.service" "$SERVICE_DIR/"
    ok "${svc}.service"
done

sudo systemctl daemon-reload
sudo systemctl enable lgtv-startup.service lgtv-shutdown.service
ok "Services enabled"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
info "Installation complete"
echo
printf "  Useful commands:\n"
printf "    Start now:   sudo systemctl start lgtv-startup.service\n"
printf "    Test:        ./install.sh --test\n"
printf "    Logs:        journalctl -u lgtv-startup -u lgtv-shutdown -f\n"
echo

# shellcheck disable=SC2059
read -rp "$(printf "${BOLD}Run screen test now? (off 10s → on) [Y/n]: ${NC}")" answer
answer="${answer:-Y}"
if [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    run_test
fi

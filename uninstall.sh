#!/usr/bin/env bash
set -euo pipefail

VENV_DIR="/opt/lgtvcontrol"
CONF_DIR="/etc/lgtvcontrol"
SERVICE_DIR="/etc/systemd/system"

RED='\033[0;31m'; GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { printf "${GREEN}  [OK]${NC} %s\n" "$*"; }
info() { printf "\n${BOLD}==> %s${NC}\n" "$*"; }
die()  { printf "${RED} [ERR]${NC} %s\n" "$*" >&2; exit 1; }

cmd_exists()       { command -v "$1" >/dev/null 2>&1; }
selinux_enforcing(){ cmd_exists getenforce && [[ "$(getenforce 2>/dev/null)" == "Enforcing" ]]; }

echo
printf "${BOLD}This will remove:${NC}\n"
printf "  %s/{lgtv-startup,lgtv-shutdown}.service\n" "$SERVICE_DIR"
printf "  %s/\n" "$VENV_DIR"
printf "  %s/\n" "$CONF_DIR"
selinux_enforcing && printf "  SELinux fcontext rules for the above paths\n"
echo
read -rp "$(printf "${BOLD}Continue? [y/N]: ${NC}")" answer
[[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || { echo "Aborted."; exit 0; }

info "Disabling and removing services"
for svc in lgtv-startup lgtv-shutdown; do
    if systemctl is-enabled --quiet "${svc}.service" 2>/dev/null; then
        sudo systemctl disable "${svc}.service"
    fi
    sudo rm -f "$SERVICE_DIR/${svc}.service"
    ok "${svc}.service removed"
done
sudo systemctl daemon-reload

info "Removing files"
sudo rm -rf "$CONF_DIR"
ok "$CONF_DIR"
sudo rm -rf "$VENV_DIR"
ok "$VENV_DIR"

if selinux_enforcing && cmd_exists semanage; then
    info "Removing SELinux fcontext rules"
    sudo semanage fcontext -d "${VENV_DIR}/bin(/.*)?" 2>/dev/null && ok "venv bin rule" || true
    sudo semanage fcontext -d "${CONF_DIR}/lgtv-on\.sh"  2>/dev/null && ok "lgtv-on.sh rule"  || true
    sudo semanage fcontext -d "${CONF_DIR}/lgtv-off\.sh" 2>/dev/null && ok "lgtv-off.sh rule" || true
fi

echo
ok "Uninstall complete."

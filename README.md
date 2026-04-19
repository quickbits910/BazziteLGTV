# BazziteLGTV

Automatically turn your LG TV's screen on at boot and off at shutdown, when using it as a PC monitor on **Bazzite** (or any SELinux-enforcing Fedora Atomic distro).

Existing solutions like [LG_Buddy](https://github.com/jesseposner/LG_Buddy) and [lgpowercontrol](https://github.com/nicholasgasior/lgpowercontrol) don't work on Bazzite because systemd runs in the `init_t` SELinux domain and cannot execute scripts installed to `~/.local/` (`user_home_t`). This project fixes that properly — no custom SELinux policy modules, no hacks.

---

## Requirements

- Bazzite / Fedora Atomic (or any SELinux-enforcing distro)
- LG WebOS TV on your local network
- [`bscpylgtv`](https://github.com/chros73/bscpylgtv) — used to communicate with the TV
- `policycoreutils-python-utils` — for `semanage` (SELinux context management)

Install the SELinux tools if not already present:
```bash
sudo rpm-ostree install policycoreutils-python-utils
# reboot after if it was missing
```

---

## First-time TV pairing

Before installing, you need to pair with the TV so the auth key gets saved to `~/.aiopylgtv.sqlite`. If you've already run `bscpylgtvcommand` and accepted the prompt on the TV, you're done. If not:

```bash
pip install --user bscpylgtv
bscpylgtvcommand 192.168.1.30 turn_screen_on
```

Accept the pairing prompt that appears on the TV screen. You only need to do this once.

---

## Install

```bash
git clone https://github.com/yourusername/BazziteLGTV
cd BazziteLGTV
./install.sh
```

The installer will:

1. Check dependencies (`python3`, `semanage`, `restorecon`)
2. Ask for your TV's IP address (default: `192.168.1.30`)
3. Create a Python venv at `/opt/lgtvcontrol/` and install `bscpylgtv`
4. Copy your auth database (`~/.aiopylgtv.sqlite`) to `/etc/lgtvcontrol/`
5. Write the on/off scripts to `/etc/lgtvcontrol/`
6. Apply `bin_t` SELinux contexts so systemd can execute them
7. Install and enable the systemd services
8. Offer to run a screen test

---

## Test

Run at any time after install to verify everything works — turns the screen off for 10 seconds then back on:

```bash
./install.sh --test
```

---

## How it works

### The SELinux problem

systemd launches services in the `init_t` SELinux domain. Files in your home directory have `user_home_t` context. SELinux blocks `init_t` from executing `user_home_t` files, which is why running scripts from `~/.local/` silently fails.

### The fix

Scripts and the `bscpylgtv` venv are placed in system paths (`/etc/` and `/opt/`) and labelled `bin_t` using `semanage fcontext`. This is the standard Fedora way to make custom paths executable by systemd — it survives reboots and SELinux filesystem relabels.

```
/opt/lgtvcontrol/        ← bscpylgtv venv (bin_t via semanage)
/etc/lgtvcontrol/        ← scripts + auth db (bin_t via semanage)
/etc/systemd/system/     ← lgtv-startup.service + lgtv-shutdown.service
```

The scripts use `--key_file_path /etc/lgtvcontrol/.aiopylgtv.sqlite` so the TV auth lookup never depends on `$HOME` being set correctly in the service environment.

### Shutdown vs reboot

`lgtv-shutdown.service` has `Conflicts=reboot.target`, so the screen turns off on **shutdown only** — reboots leave it alone.

---

## Useful commands

```bash
# Check service status
systemctl status lgtv-startup.service
systemctl status lgtv-shutdown.service

# View logs
journalctl -u lgtv-startup -u lgtv-shutdown -f

# Manually trigger
sudo systemctl start lgtv-startup.service   # screen on
sudo /etc/lgtvcontrol/lgtv-off.sh           # screen off
```

---

## Uninstall

```bash
./uninstall.sh
```

Removes the services, `/opt/lgtvcontrol/`, `/etc/lgtvcontrol/`, and the SELinux fcontext rules.

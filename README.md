# BazziteLGTV

Automatically power your LG TV on at boot and off at shutdown, when using it as a PC monitor on **Bazzite** (or any SELinux-enforcing Fedora Atomic distro).

Existing solutions like [LG_Buddy](https://github.com/jesseposner/LG_Buddy) and [lgpowercontrol](https://github.com/nicholasgasior/lgpowercontrol) don't work on Bazzite because systemd runs in the `init_t` SELinux domain and cannot execute scripts installed to `~/.local/` (`user_home_t`). This project fixes that correctly — no custom SELinux policy modules, no hacks.

**Zero external dependencies.** Everything is implemented in Python's standard library (`asyncio`, `ssl`, `socket`). No pip packages required.

---

## Requirements

### System

- Bazzite / Fedora Atomic (or any SELinux-enforcing distro)
- LG WebOS TV connected via **wired LAN** on your local network (192.168.1.x)
- `python3` (already present on all Fedora-based systems)
- `policycoreutils-python-utils` — for `semanage` (only needed if SELinux is Enforcing)

If `semanage` is missing:
```bash
sudo rpm-ostree install policycoreutils-python-utils
# reboot after
```

### TV Settings

These must be configured on the TV before install:

- **Quick Start must be disabled** — Quick Start prevents Wake-on-LAN magic packets from working
- **Settings → General → External Devices → TV On with Mobile** must be **enabled** — this keeps the network chip powered in standby so the TV can receive WoL packets

---

## Install

```bash
git clone https://github.com/yourusername/BazziteLGTV
cd BazziteLGTV
./install.sh
```

The installer will:

1. Check for `python3` and (if SELinux is Enforcing) `semanage`
2. Ask for your TV's IP address
3. Copy scripts to `/etc/lgtvcontrol/`
4. Apply `bin_t` SELinux contexts so systemd can execute them
5. Pair with the TV — a prompt appears on screen, press OK with your remote
6. Detect and save the TV's MAC address for Wake-on-LAN
7. Install and enable the systemd services
8. Offer to run a screen test (off 10s → on)

---

## Test

Run at any time after install:

```bash
./install.sh --test
```

Turns the screen off for 10 seconds then back on.

---

## How it works

### The SELinux problem

systemd launches services in the `init_t` SELinux domain. Files in your home directory have `user_home_t` context. SELinux blocks `init_t` from executing `user_home_t` files — this is why running scripts from `~/.local/` silently fails.

### The fix

Scripts are installed to `/etc/lgtvcontrol/` and labelled `bin_t` via `semanage fcontext`. This is the standard Fedora approach to making custom paths executable by systemd. It survives reboots and SELinux filesystem relabels.

```
/etc/lgtvcontrol/
  lgtv.py          ← WebSocket client (stdlib only)
  lgtv-on.sh       ← calls: python3 /etc/lgtvcontrol/lgtv.py on
  lgtv-off.sh      ← calls: python3 /etc/lgtvcontrol/lgtv.py off
  tv_ip            ← TV IP address (written by installer)
  tv_mac           ← TV wired MAC address (written by installer, used for WoL)
  client.key       ← auth key (written during pairing)

/etc/systemd/system/
  lgtv-startup.service
  lgtv-shutdown.service
```

### TV communication

`lgtv.py` connects over WebSocket (port 3001, TLS) and sends LG's SSAP protocol commands:

- **on** → `ssap://com.webos.service.tvpower/power/turnOnScreen`
- **off** → `ssap://system/turnOff` with `standbyMode: active` (keeps network chip alive for WoL)

Pairing is done once. The client key is saved to `/etc/lgtvcontrol/client.key` and sent with every subsequent connection.

### Wake-on-LAN

When powering on, `lgtv.py` attempts to connect to the TV. If the first attempt fails (TV is in standby), it sends a WoL magic packet to the TV's wired MAC address via UDP broadcast (`192.168.1.255`, port 9), then retries the connection up to 8 times with 5-second delays while the TV boots.

The TV's MAC is detected automatically from the ARP cache during install.

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
sudo systemctl start lgtv-startup.service
sudo systemctl start lgtv-shutdown.service

# Re-pair if the TV rejects the stored key
sudo python3 /etc/lgtvcontrol/lgtv.py pair
```

---

## Uninstall

```bash
./uninstall.sh
```

Removes the services, `/etc/lgtvcontrol/`, and the SELinux fcontext rules.

---

## Development

```bash
pip install -r requirements-test.txt
pytest tests/
```

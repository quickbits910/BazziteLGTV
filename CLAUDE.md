# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all tests
pytest tests/

# Run a single test
pytest tests/test_lgtv.py::test_name

# Install test dependencies
pip install -r requirements-test.txt

# Install to the local system
./install.sh

# Test installed scripts (off 10s → on)
./install.sh --test

# Uninstall
./uninstall.sh
```

## Architecture

The project has one real source file: `scripts/lgtv.py`. Everything else is glue.

**`scripts/lgtv.py`** — standalone Python script, zero external dependencies. Implements a minimal WebSocket client from scratch (RFC 6455 framing, TLS via `ssl`, masking, all frame sizes) because the TV requires WSS on port 3001. Uses LG's SSAP protocol over that WebSocket. Runtime config is read from `/etc/lgtvcontrol/`: `tv_ip`, `client.key`, and `tv_mac` (optional, for WoL).

Commands: `pair | on | off`. `on`/`off` register with a stored client key then send a single SSAP request. `pair` does the same registration flow but waits for user approval on the TV and saves the returned key.

**Retry/WoL loop** — `run()` retries the connection up to 8 times (5s delay). On the first failure for `on`, it broadcasts a WoL magic packet to the TV's MAC so the TV wakes from standby before retrying.

**Systemd services** (in `systemd/`, installed to `/etc/systemd/system/`):
- `lgtv-startup.service` — runs `lgtv-on.sh` at boot (`WantedBy=multi-user.target`)
- `lgtv-shutdown.service` — runs `lgtv-off.sh` at shutdown (`Conflicts=reboot.target` so reboots are skipped)
- `lgtv-sleep.service` — `ExecStart=lgtv-off.sh` before suspend, `ExecStop=lgtv-on.sh` after resume; `RemainAfterExit=yes` + `WantedBy=sleep.target` makes this work across all sleep states

**SELinux** — scripts must be in `/etc/lgtvcontrol/` with `bin_t` context, applied via `semanage fcontext`. This is why `~/.local/` paths don't work on Bazzite: systemd's `init_t` domain cannot execute `user_home_t` files.

**Tests** (`tests/test_lgtv.py`) — pure unit tests, no system dependencies. `conftest.py` adds `scripts/` to `sys.path` so `import lgtv` works. `asyncio_mode = "auto"` in `pyproject.toml` means async test functions need no decorator. Tests cover: WebSocket frame encoding/decoding, `ws_connect`, `run()` for all commands, retry logic, WoL behaviour, and service file content validation for `lgtv-sleep.service`.

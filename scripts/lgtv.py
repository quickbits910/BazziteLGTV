#!/usr/bin/env python3
"""LG TV power control — usage: lgtv.py pair|on|off
Zero external dependencies: uses only Python standard library (asyncio + ssl).
"""

import asyncio
import base64
import fcntl
import json
import os
import socket
import ssl
import struct
import sys
from pathlib import Path

CONF_DIR = Path("/etc/lgtvcontrol")
KEY_FILE = CONF_DIR / "client.key"
IP_FILE  = CONF_DIR / "tv_ip"
MAC_FILE = CONF_DIR / "tv_mac"
CONFIG_FILE = CONF_DIR / "config"  # optional KEY=VALUE tunables (see _apply_config)

# Defaults — overridable per key via CONFIG_FILE.
TIMEOUT        = 10   # per-connection timeout (seconds)
RETRY_ATTEMPTS = 8    # max connection attempts at startup
# Exponential backoff between attempts: delay = min(BACKOFF_MAX, BACKOFF_BASE * 2**n).
# Gives 1, 2, 4, 8, 8, 8, 8s (~39s total) — long enough for a WiFi TV to wake
# from standby, re-associate with the AP and pick up a DHCP lease.
BACKOFF_BASE   = 1.0  # seconds before the first retry
BACKOFF_MAX    = 8.0  # cap on the per-retry delay

# Tunable name -> (module global, parser). Anything else in CONFIG_FILE is ignored.
_CONFIG_KEYS = {
    "timeout":        ("TIMEOUT", float),
    "retry_attempts": ("RETRY_ATTEMPTS", int),
    "backoff_base":   ("BACKOFF_BASE", float),
    "backoff_max":    ("BACKOFF_MAX", float),
}


def _apply_config() -> None:
    """Override the tunable globals from CONFIG_FILE, if it exists.

    Format is one ``key = value`` per line; ``#`` starts a comment and blank
    lines are ignored. Unknown keys and unparseable/negative values are warned
    about on stderr and left at their default — a bad config never aborts a
    power command.
    """
    try:
        text = CONFIG_FILE.read_text()
    except FileNotFoundError:
        return

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            print(f"{CONFIG_FILE}:{lineno}: ignoring malformed line: {raw!r}", file=sys.stderr)
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key not in _CONFIG_KEYS:
            print(f"{CONFIG_FILE}:{lineno}: unknown setting {key!r} — ignored", file=sys.stderr)
            continue
        attr, parse = _CONFIG_KEYS[key]
        try:
            parsed = parse(value)
        except ValueError:
            print(f"{CONFIG_FILE}:{lineno}: bad value for {key!r}: {value!r} — using default", file=sys.stderr)
            continue
        if parsed <= 0:
            print(f"{CONFIG_FILE}:{lineno}: {key!r} must be positive — using default", file=sys.stderr)
            continue
        globals()[attr] = parsed

ENDPOINTS = {
    "on":  "com.webos.service.tvpower/power/turnOnScreen",
    # standbyMode=active keeps the network chip alive so WoL can wake the TV
    "off": "system/turnOff",
}

# Standard LG remote app manifest — the signature was issued by LG and must be
# sent verbatim so the TV accepts the pairing without rejecting unknown clients.
MANIFEST = {
    "appVersion": "1.1",
    "manifestVersion": 1,
    "permissions": [
        "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE",
        "TEST_OPEN", "TEST_PROTECTED", "CONTROL_AUDIO", "CONTROL_DISPLAY",
        "CONTROL_INPUT_JOYSTICK", "CONTROL_INPUT_MEDIA_RECORDING",
        "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_TV", "CONTROL_POWER",
        "CONTROL_TV_SCREEN", "READ_APP_STATUS", "READ_CURRENT_CHANNEL",
        "READ_INPUT_DEVICE_LIST", "READ_NETWORK_STATE", "READ_RUNNING_APPS",
        "READ_TV_CHANNEL_LIST", "WRITE_NOTIFICATION_TOAST", "READ_POWER_STATE",
        "READ_COUNTRY_INFO", "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD",
        "READ_INSTALLED_APPS", "READ_SETTINGS", "READ_STORAGE_DEVICE_LIST",
    ],
    "signatures": [{"signature": (
        "eyJhbGdvcml0aG0iOiJSU0EtU0hBMjU2Iiwia2V5SWQiOiJ0ZXN0LXNpZ25pbm"
        "ctY2VydCIsInNpZ25hdHVyZVZlcnNpb24iOjF9.hrVRgjCwXVvE2OOSpDZ58hR"
        "+59aFNwYDyjQgKk3auukd7pcegmE2CzPCa0bJ0ZsRAcKkCTJrWo5iDzNhMBWRy"
        "aMOv5zWSrthlf7G128qvIlpMT0YNY+n/FaOHE73uLrS/g7swl3/qH/BGFG2Hu4"
        "RlL48eb3lLKqTt2xKHdCs6Cd4RMfJPYnzgvI4BNrFUKsjkcu+WD4OO2A27Pq1n"
        "50cMchmcaXadJhGrOqH5YmHdOCj5NSHzJYrsW0HPlpuAx/ECMeIZYDh6RMqaFM"
        "2DXzdKX9NmmyqzJ3o/0lkk/N97gfVRLW5hA29yeAwaCViZNCP8iC9aO0q9fQoj"
        "oa7NQnAtw=="
    ), "signatureVersion": 1}],
    "signed": {
        "appId": "com.lge.test",
        "created": "20140509",
        "localizedAppNames": {
            "": "LG Remote App",
            "ko-KR": "리모컨 앱",
            "zxx-XX": "ЛГ Rэмotэ AПП",
        },
        "localizedVendorNames": {"": "LG Electronics"},
        "permissions": [
            "TEST_SECURE", "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD",
            "READ_INSTALLED_APPS", "READ_LGE_SDX", "READ_NOTIFICATIONS",
            "SEARCH", "WRITE_SETTINGS", "WRITE_NOTIFICATION_ALERT",
            "CONTROL_POWER", "READ_CURRENT_CHANNEL", "READ_RUNNING_APPS",
            "READ_UPDATE_INFO", "UPDATE_FROM_REMOTE_APP",
            "READ_LGE_TV_INPUT_EVENTS", "READ_TV_CURRENT_TIME",
        ],
        "serial": "2f930e2d2cfe083771f68e4fe7bb07",
        "vendorId": "com.lge",
    },
}


# ---------------------------------------------------------------------------
# Minimal WebSocket client (standard library only)
# ---------------------------------------------------------------------------

class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._r = reader
        self._w = writer

    async def send(self, text: str) -> None:
        data = text.encode()
        n = len(data)
        # Client→server frames must be masked (RFC 6455 §5.3)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        header = b'\x81'  # FIN=1, opcode=1 (text)
        if n < 126:
            header += bytes([0x80 | n]) + mask
        elif n < 65536:
            header += bytes([0xFE]) + struct.pack('>H', n) + mask
        else:
            header += bytes([0xFF]) + struct.pack('>Q', n) + mask
        self._w.write(header + masked)
        await self._w.drain()

    async def recv(self) -> str:
        header = await self._r.readexactly(2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack('>H', await self._r.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack('>Q', await self._r.readexactly(8))[0]
        payload = await self._r.readexactly(length)
        if opcode == 8:  # close frame
            raise ConnectionError("Server closed the WebSocket connection")
        return payload.decode()

    async def close(self) -> None:
        try:
            self._w.write(b'\x88\x80' + os.urandom(4))
            await self._w.drain()
        except Exception:
            pass
        self._w.close()
        try:
            await self._w.wait_closed()
        except Exception:
            pass


async def ws_connect(host: str, port: int, ctx: ssl.SSLContext) -> WebSocket:
    reader, writer = await asyncio.open_connection(host, port, ssl=ctx)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write((
        f"GET / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    ).encode())
    await writer.drain()
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(4096)
        if not chunk:
            raise ConnectionError("Connection closed during WebSocket handshake")
        buf += chunk
    if b" 101 " not in buf.split(b"\r\n")[0]:
        raise ConnectionError(
            f"WebSocket upgrade failed: {buf[:200].decode(errors='replace')}"
        )
    return WebSocket(reader, writer)


# ---------------------------------------------------------------------------
# LG TV control
# ---------------------------------------------------------------------------

_SIOCGIFADDR    = 0x8915  # Linux ioctl: get interface IPv4 address
_SIOCGIFNETMASK = 0x891b  # Linux ioctl: get interface netmask


def _subnet_broadcast(tv_ip: str) -> str:
    """Return the directed broadcast address of the interface that routes to tv_ip."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((tv_ip, 9))
        local_ip = probe.getsockname()[0]

    for _, ifname in socket.if_nameindex():
        packed = struct.pack("16sH14s", ifname.encode()[:16], socket.AF_INET, b"\0" * 14)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                addr_res = fcntl.ioctl(s.fileno(), _SIOCGIFADDR, packed)
                if socket.inet_ntoa(addr_res[20:24]) != local_ip:
                    continue
                mask_res = fcntl.ioctl(s.fileno(), _SIOCGIFNETMASK, packed)
        except OSError:
            continue

        ip_int   = struct.unpack("!I", socket.inet_aton(local_ip))[0]
        mask_int = struct.unpack("!I", mask_res[20:24])[0]
        bcast    = (ip_int & mask_int) | (~mask_int & 0xFFFFFFFF)
        return socket.inet_ntoa(struct.pack("!I", bcast))

    return "255.255.255.255"  # global broadcast fallback


def _send_wol(mac: str, tv_ip: str) -> None:
    """Send WoL magic packet via subnet broadcast and directly to the TV's IP."""
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    broadcast = _subnet_broadcast(tv_ip)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, 9))
        s.sendto(packet, (tv_ip, 9))


async def _session(ws: WebSocket, mode: str, client_key: str | None) -> None:
    """Run the SSAP registration + command over an already-open WebSocket.

    Raises ConnectionError if the TV drops the connection mid-protocol so the
    caller's retry loop can attempt a reconnect.  All other failures call
    sys.exit() directly — they are not transient and should not be retried.
    """
    try:
        await ws.send(json.dumps({
            "type": "register",
            "id": "register_0",
            "payload": {
                "client-key": client_key,
                "forcePairing": False,
                "manifest": MANIFEST,
                "pairingType": "PROMPT",
            },
        }))

        recv_timeout = 60 if mode == "pair" else TIMEOUT
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "response" and msg.get("payload", {}).get("pairingType") == "PROMPT":
                if mode != "pair":
                    sys.exit(
                        "Stored key was rejected by TV — re-pair: "
                        f"sudo python3 {__file__} pair"
                    )
                print("Approve the connection on your TV (press OK on the prompt)...")
                continue

            if mtype == "registered":
                if mode == "pair":
                    key = msg["payload"]["client-key"]
                    KEY_FILE.write_text(key + "\n")
                    KEY_FILE.chmod(0o644)
                    print(f"Paired. Client key saved to {KEY_FILE}")
                break

            if mtype == "error":
                sys.exit(f"Registration error: {msg}")

        if mode == "pair":
            return

        uri = ENDPOINTS[mode]
        payload = {"standbyMode": "active"} if mode == "off" else {}
        await ws.send(json.dumps({
            "id": 1,
            "type": "request",
            "uri": f"ssap://{uri}",
            "payload": payload,
        }))

        raw = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT)
        msg = json.loads(raw)
        payload = msg.get("payload", {})
        print(payload)

        # errorCode -102 = screen already in desired state, not a real error
        if msg.get("type") == "error" and payload.get("errorCode") != "-102":
            sys.exit(f"Command failed: {payload}")

    except (asyncio.TimeoutError, TimeoutError) as e:
        sys.exit(f"Timed out waiting for TV response: {e}")
    # ConnectionError (TV sent close frame) propagates — caller will retry


async def run(mode: str) -> None:
    _apply_config()
    try:
        tv_ip = IP_FILE.read_text().strip()
    except FileNotFoundError:
        sys.exit(f"TV IP not configured — expected {IP_FILE}")

    client_key = None
    if mode != "pair":
        if not KEY_FILE.exists():
            sys.exit(f"No client key at {KEY_FILE} — run: sudo python3 {__file__} pair")
        client_key = KEY_FILE.read_text().strip() or None

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    mac = MAC_FILE.read_text().strip() if MAC_FILE.exists() else None

    # For "on" the TV is almost certainly in standby, so wake it *before* the
    # first attempt — its WiFi radio then has the whole retry window to
    # re-associate with the AP and pick up an address. Harmless if already on.
    if mac and mode == "on":
        _send_wol(mac, tv_ip)
        print("Sent Wake-on-LAN — waiting for TV to boot...", file=sys.stderr)

    last_err = ""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            ws = await asyncio.wait_for(ws_connect(tv_ip, 3001, ctx), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            last_err = f"connection timed out after {TIMEOUT}s"
        except (OSError, ConnectionError) as e:
            last_err = str(e)
        else:
            try:
                await _session(ws, mode, client_key)
                return
            except ConnectionError as e:
                last_err = str(e)
            finally:
                await ws.close()

        if attempt < RETRY_ATTEMPTS:
            # Re-send WoL on every retry. A WiFi TV waking from standby can miss
            # the first magic packet before its radio re-associates, and APs drop
            # broadcast frames to sleeping stations. Packets are cheap and
            # idempotent — the TV ignores them once it is awake.
            if mac and mode == "on":
                _send_wol(mac, tv_ip)

            delay = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (attempt - 1))
            print(
                f"[attempt {attempt}/{RETRY_ATTEMPTS}] {last_err} — retrying in {delay:.0f}s",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)

    sys.exit(f"Could not reach TV at {tv_ip} after {RETRY_ATTEMPTS} attempts: {last_err}")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ("pair", "on", "off"):
        sys.exit(f"Usage: {sys.argv[0]} pair|on|off")
    asyncio.run(run(args[0]))


if __name__ == "__main__":
    main()

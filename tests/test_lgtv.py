"""Unit tests for scripts/lgtv.py"""

import asyncio
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import lgtv
from lgtv import WebSocket, run, ws_connect


@pytest.fixture
def no_retries(monkeypatch):
    """Collapse retry loop to a single attempt so error-path tests exit fast."""
    monkeypatch.setattr(lgtv, "RETRY_ATTEMPTS", 1)


# ── helpers ───────────────────────────────────────────────────────────────────

def server_frame(payload: bytes | str, opcode: int = 1) -> bytes:
    """Build an unmasked server→client WebSocket frame."""
    if isinstance(payload, str):
        payload = payload.encode()
    n = len(payload)
    if n < 126:
        return bytes([0x80 | opcode, n]) + payload
    if n < 65536:
        return bytes([0x80 | opcode, 126]) + struct.pack(">H", n) + payload
    return bytes([0x80 | opcode, 127]) + struct.pack(">Q", n) + payload


def make_reader(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    return r


def make_writer() -> MagicMock:
    w = MagicMock()
    w.write = MagicMock()
    w.drain = AsyncMock()
    w.close = MagicMock()
    w.wait_closed = AsyncMock()
    return w


def make_ws(*messages) -> MagicMock:
    ws = MagicMock(spec=WebSocket)
    ws.send = AsyncMock()
    ws.recv = AsyncMock(side_effect=list(messages))
    ws.close = AsyncMock()
    return ws


def setup_files(tmp_path, client_key="stored-key"):
    ip = tmp_path / "tv_ip"
    ip.write_text("192.168.1.100\n")
    key = tmp_path / "client.key"
    key.write_text(client_key + "\n")
    return ip, key


# ── canned TV messages ────────────────────────────────────────────────────────

REGISTERED = json.dumps({"type": "registered", "payload": {"client-key": "test-key-123"}})
PROMPT     = json.dumps({"type": "response",   "payload": {"pairingType": "PROMPT"}})
SUCCESS    = json.dumps({"type": "response",   "payload": {"returnValue": True}})
ERROR      = json.dumps({"type": "error",      "payload": {"errorCode": "-500", "errorText": "fail"}})
ALREADY    = json.dumps({"type": "error",      "payload": {"errorCode": "-102"}})


# ── WebSocket.send ────────────────────────────────────────────────────────────

async def test_send_small_payload():
    ws = WebSocket(asyncio.StreamReader(), make_writer())
    await ws.send("hello")
    data = ws._w.write.call_args[0][0]
    assert data[0] == 0x81          # FIN + text opcode
    assert data[1] == 0x80 | 5      # masked, length=5
    mask = data[2:6]
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(data[6:])) == b"hello"


async def test_send_medium_payload():
    ws = WebSocket(asyncio.StreamReader(), make_writer())
    await ws.send("x" * 200)
    data = ws._w.write.call_args[0][0]
    assert data[1] == 0xFE                               # masked + 16-bit length
    assert struct.unpack(">H", data[2:4])[0] == 200


async def test_send_large_payload():
    ws = WebSocket(asyncio.StreamReader(), make_writer())
    await ws.send("x" * 70000)
    data = ws._w.write.call_args[0][0]
    assert data[1] == 0xFF                               # masked + 64-bit length
    assert struct.unpack(">Q", data[2:10])[0] == 70000


# ── WebSocket.recv ────────────────────────────────────────────────────────────

async def test_recv_small_frame():
    ws = WebSocket(make_reader(server_frame("hello")), make_writer())
    assert await ws.recv() == "hello"


async def test_recv_medium_frame():
    payload = "x" * 200
    ws = WebSocket(make_reader(server_frame(payload)), make_writer())
    assert await ws.recv() == payload


async def test_recv_large_frame():
    payload = "x" * 70000
    ws = WebSocket(make_reader(server_frame(payload)), make_writer())
    assert await ws.recv() == payload


async def test_recv_close_frame_raises():
    ws = WebSocket(make_reader(server_frame(b"", opcode=8)), make_writer())
    with pytest.raises(ConnectionError, match="Server closed"):
        await ws.recv()


# ── WebSocket.close ───────────────────────────────────────────────────────────

async def test_close_sends_close_frame():
    writer = make_writer()
    ws = WebSocket(asyncio.StreamReader(), writer)
    await ws.close()
    assert writer.write.call_args[0][0][0] == 0x88   # FIN + close opcode
    writer.close.assert_called_once()


# ── ws_connect ────────────────────────────────────────────────────────────────

async def test_ws_connect_success():
    resp = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"
    with patch("asyncio.open_connection", return_value=(make_reader(resp), make_writer())):
        ws = await ws_connect("192.168.1.1", 3001, MagicMock())
    assert isinstance(ws, WebSocket)
    written = ws._w.write.call_args[0][0].decode()
    assert "Upgrade: websocket" in written
    assert "Sec-WebSocket-Key" in written


async def test_ws_connect_bad_status():
    resp = b"HTTP/1.1 403 Forbidden\r\n\r\n"
    with patch("asyncio.open_connection", return_value=(make_reader(resp), make_writer())):
        with pytest.raises(ConnectionError, match="WebSocket upgrade failed"):
            await ws_connect("192.168.1.1", 3001, MagicMock())


# ── run() — pair ──────────────────────────────────────────────────────────────

async def test_pair_saves_key(tmp_path):
    ip = tmp_path / "tv_ip"
    ip.write_text("192.168.1.100\n")
    key = tmp_path / "client.key"
    ws = make_ws(PROMPT, REGISTERED)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(return_value=ws)):
        await run("pair")
    assert key.read_text().strip() == "test-key-123"


async def test_pair_prompts_user(tmp_path, capsys):
    ip = tmp_path / "tv_ip"
    ip.write_text("192.168.1.100\n")
    key = tmp_path / "client.key"
    ws = make_ws(PROMPT, REGISTERED)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(return_value=ws)):
        await run("pair")
    assert "Approve" in capsys.readouterr().out


# ── run() — on/off ────────────────────────────────────────────────────────────

async def test_on_uri_and_payload(tmp_path):
    ip, key = setup_files(tmp_path)
    ws = make_ws(REGISTERED, SUCCESS)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(return_value=ws)):
        await run("on")
    cmd = json.loads(ws.send.call_args_list[1].args[0])
    assert cmd["uri"] == "ssap://com.webos.service.tvpower/power/turnOnScreen"
    assert cmd["payload"] == {}


async def test_off_uri_and_payload(tmp_path):
    ip, key = setup_files(tmp_path)
    ws = make_ws(REGISTERED, SUCCESS)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(return_value=ws)):
        await run("off")
    cmd = json.loads(ws.send.call_args_list[1].args[0])
    assert cmd["uri"] == "ssap://system/turnOff"
    assert cmd["payload"] == {"standbyMode": "active"}


async def test_sends_stored_client_key(tmp_path):
    ip, key = setup_files(tmp_path, "my-client-key")
    ws = make_ws(REGISTERED, SUCCESS)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(return_value=ws)):
        await run("on")
    reg = json.loads(ws.send.call_args_list[0].args[0])
    assert reg["payload"]["client-key"] == "my-client-key"


async def test_error_102_not_fatal(tmp_path):
    """errorCode -102 means screen already in desired state — not an error."""
    ip, key = setup_files(tmp_path)
    ws = make_ws(REGISTERED, ALREADY)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(return_value=ws)):
        await run("on")   # must not raise


async def test_command_error_exits(tmp_path):
    ip, key = setup_files(tmp_path)
    ws = make_ws(REGISTERED, ERROR)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(return_value=ws)):
        with pytest.raises(SystemExit):
            await run("on")


async def test_registration_error_exits(tmp_path):
    ip, key = setup_files(tmp_path)
    ws = make_ws(ERROR)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(return_value=ws)):
        with pytest.raises(SystemExit):
            await run("on")


# ── run() — missing files ─────────────────────────────────────────────────────

async def test_missing_ip_exits(tmp_path):
    ip = tmp_path / "tv_ip"          # not created
    key = tmp_path / "client.key"
    key.write_text("key\n")
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key):
        with pytest.raises(SystemExit, match="TV IP not configured"):
            await run("on")


async def test_missing_key_exits(tmp_path):
    ip = tmp_path / "tv_ip"
    ip.write_text("192.168.1.100\n")
    key = tmp_path / "client.key"    # not created
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key):
        with pytest.raises(SystemExit, match="No client key"):
            await run("on")


# ── run() — connection failures ───────────────────────────────────────────────

async def test_connect_timeout_exits(tmp_path, no_retries):
    ip, key = setup_files(tmp_path)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(side_effect=asyncio.TimeoutError())):
        with pytest.raises(SystemExit, match="Could not reach TV"):
            await run("on")


async def test_connect_os_error_exits(tmp_path, no_retries):
    ip, key = setup_files(tmp_path)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(side_effect=OSError("refused"))):
        with pytest.raises(SystemExit, match="Could not reach TV"):
            await run("on")


# ── run() — retry logic ───────────────────────────────────────────────────────

async def test_retries_on_connection_drop(tmp_path, monkeypatch):
    """TV closes the WebSocket during registration; script retries and succeeds."""
    monkeypatch.setattr(lgtv, "RETRY_DELAY", 0)
    ip, key = setup_files(tmp_path)
    ws_drop = make_ws(ConnectionError("Server closed the WebSocket connection"))
    ws_ok   = make_ws(REGISTERED, SUCCESS)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(side_effect=[ws_drop, ws_ok])):
        await run("on")   # must not raise
    cmd = json.loads(ws_ok.send.call_args_list[1].args[0])
    assert cmd["uri"] == "ssap://com.webos.service.tvpower/power/turnOnScreen"


async def test_retries_on_connect_error_then_succeeds(tmp_path, monkeypatch):
    """TCP connection refused on first attempt; second attempt succeeds."""
    monkeypatch.setattr(lgtv, "RETRY_DELAY", 0)
    ip, key = setup_files(tmp_path)
    ws_ok = make_ws(REGISTERED, SUCCESS)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(side_effect=[OSError("refused"), ws_ok])):
        await run("on")   # must not raise


async def test_exhausts_retries_and_exits(tmp_path, monkeypatch):
    """Every attempt fails; script exits after RETRY_ATTEMPTS attempts."""
    monkeypatch.setattr(lgtv, "RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(lgtv, "RETRY_DELAY", 0)
    ip, key = setup_files(tmp_path)
    # side_effect as a bare exception (not a list) means it fires on every call
    ws_drop = MagicMock(spec=WebSocket)
    ws_drop.send = AsyncMock()
    ws_drop.recv = AsyncMock(side_effect=ConnectionError("Server closed the WebSocket connection"))
    ws_drop.close = AsyncMock()
    mock_connect = AsyncMock(return_value=ws_drop)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", mock_connect):
        with pytest.raises(SystemExit, match="Could not reach TV"):
            await run("on")
    assert mock_connect.call_count == 3


# ── _send_wol ─────────────────────────────────────────────────────────────────

def test_send_wol_packet_structure():
    """Magic packet = 6×0xFF header + MAC repeated 16 times."""
    import socket as _socket
    with patch("socket.socket") as mock_cls:
        sock = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=sock)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        lgtv._send_wol("aa:bb:cc:dd:ee:ff")
    packet = sock.sendto.call_args[0][0]
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("aabbccddeeff") * 16
    sock.setsockopt.assert_called_once_with(_socket.SOL_SOCKET, _socket.SO_BROADCAST, 1)


def test_send_wol_normalises_separators():
    """MAC with dashes and mixed case is handled correctly."""
    with patch("socket.socket") as mock_cls:
        sock = MagicMock()
        mock_cls.return_value.__enter__ = MagicMock(return_value=sock)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)
        lgtv._send_wol("AA-BB-CC-DD-EE-FF")
    packet = sock.sendto.call_args[0][0]
    assert packet[6:] == bytes.fromhex("AABBCCDDEEFF") * 16


# ── run() — Wake-on-LAN ───────────────────────────────────────────────────────

async def test_wol_sent_on_first_failure(tmp_path, monkeypatch):
    """WoL is broadcast on first connection failure when MAC is configured."""
    monkeypatch.setattr(lgtv, "RETRY_DELAY", 0)
    ip, key = setup_files(tmp_path)
    mac_file = tmp_path / "tv_mac"
    mac_file.write_text("aa:bb:cc:dd:ee:ff\n")
    ws_drop = make_ws(ConnectionError("Server closed the WebSocket connection"))
    ws_ok   = make_ws(REGISTERED, SUCCESS)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch.object(lgtv, "MAC_FILE", mac_file), \
         patch("lgtv.ws_connect", AsyncMock(side_effect=[ws_drop, ws_ok])), \
         patch("lgtv._send_wol") as mock_wol:
        await run("on")
    mock_wol.assert_called_once_with("aa:bb:cc:dd:ee:ff")


async def test_wol_sent_only_once(tmp_path, monkeypatch):
    """WoL is sent exactly once even when multiple retries are needed."""
    monkeypatch.setattr(lgtv, "RETRY_ATTEMPTS", 4)
    monkeypatch.setattr(lgtv, "RETRY_DELAY", 0)
    ip, key = setup_files(tmp_path)
    mac_file = tmp_path / "tv_mac"
    mac_file.write_text("aa:bb:cc:dd:ee:ff\n")
    ws_fail = MagicMock(spec=WebSocket)
    ws_fail.send = AsyncMock()
    ws_fail.recv = AsyncMock(side_effect=ConnectionError("Server closed"))
    ws_fail.close = AsyncMock()
    ws_ok = make_ws(REGISTERED, SUCCESS)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch.object(lgtv, "MAC_FILE", mac_file), \
         patch("lgtv.ws_connect", AsyncMock(side_effect=[ws_fail, ws_fail, ws_ok])), \
         patch("lgtv._send_wol") as mock_wol:
        await run("on")
    mock_wol.assert_called_once()


async def test_wol_not_sent_without_mac_file(tmp_path, monkeypatch):
    """No WoL when MAC file does not exist."""
    monkeypatch.setattr(lgtv, "RETRY_DELAY", 0)
    ip, key = setup_files(tmp_path)
    mac_file = tmp_path / "tv_mac"   # not created
    ws_drop = make_ws(ConnectionError("Server closed"))
    ws_ok   = make_ws(REGISTERED, SUCCESS)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch.object(lgtv, "MAC_FILE", mac_file), \
         patch("lgtv.ws_connect", AsyncMock(side_effect=[ws_drop, ws_ok])), \
         patch("lgtv._send_wol") as mock_wol:
        await run("on")
    mock_wol.assert_not_called()


async def test_wol_not_sent_for_off_command(tmp_path, monkeypatch):
    """WoL is never sent for the off command."""
    monkeypatch.setattr(lgtv, "RETRY_DELAY", 0)
    ip, key = setup_files(tmp_path)
    mac_file = tmp_path / "tv_mac"
    mac_file.write_text("aa:bb:cc:dd:ee:ff\n")
    ws = make_ws(REGISTERED, SUCCESS)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch.object(lgtv, "MAC_FILE", mac_file), \
         patch("lgtv.ws_connect", AsyncMock(return_value=ws)), \
         patch("lgtv._send_wol") as mock_wol:
        await run("off")
    mock_wol.assert_not_called()


# ── main() ────────────────────────────────────────────────────────────────────

def test_main_no_args(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lgtv.py"])
    with pytest.raises(SystemExit):
        lgtv.main()


def test_main_bad_arg(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lgtv.py", "restart"])
    with pytest.raises(SystemExit):
        lgtv.main()


def test_main_on(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lgtv.py", "on"])
    with patch("lgtv.run", AsyncMock()) as mock_run:
        lgtv.main()
    mock_run.assert_called_once_with("on")


def test_main_off(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lgtv.py", "off"])
    with patch("lgtv.run", AsyncMock()) as mock_run:
        lgtv.main()
    mock_run.assert_called_once_with("off")


def test_main_pair(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lgtv.py", "pair"])
    with patch("lgtv.run", AsyncMock()) as mock_run:
        lgtv.main()
    mock_run.assert_called_once_with("pair")

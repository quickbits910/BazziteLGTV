"""Unit tests for scripts/lgtv.py"""

import asyncio
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import lgtv
from lgtv import WebSocket, run, ws_connect


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
    assert cmd["uri"] == "ssap://system/turnOn"
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

async def test_connect_timeout_exits(tmp_path):
    ip, key = setup_files(tmp_path)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(side_effect=asyncio.TimeoutError())):
        with pytest.raises(SystemExit, match="Timed out"):
            await run("on")


async def test_connect_os_error_exits(tmp_path):
    ip, key = setup_files(tmp_path)
    with patch.object(lgtv, "IP_FILE", ip), patch.object(lgtv, "KEY_FILE", key), \
         patch("lgtv.ws_connect", AsyncMock(side_effect=OSError("refused"))):
        with pytest.raises(SystemExit, match="Could not connect"):
            await run("on")


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

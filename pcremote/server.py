"""PC Remote server: serves the phone web app and injects input over WebSocket."""
import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import sys
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from . import winput

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA_DIR = ROOT.parent / "data"
DEVICES_FILE = DATA_DIR / "devices.json"

PIN_MAX_FAILURES = 5
AUTH_TIMEOUT = 5.0

log = logging.getLogger("pcremote")


# ------------------------------------------------------------------ auth

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DeviceStore:
    """Paired devices. Only token hashes are stored on disk."""

    def __init__(self, path: Path):
        self.path = path
        self.devices: list[dict] = []
        if path.exists():
            try:
                self.devices = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                log.warning("Could not read %s; starting with no paired devices", path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.devices, indent=2), "utf-8")

    def add(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        self.devices.append({"name": name[:60] or "Device", "hash": _hash(token),
                             "paired": int(time.time())})
        self._save()
        return token

    def check(self, token: str) -> dict | None:
        if not isinstance(token, str) or not token:
            return None
        h = _hash(token)
        for d in self.devices:
            if hmac.compare_digest(d["hash"], h):
                return d
        return None

    def clear(self) -> None:
        self.devices = []
        self._save()


class Pairing:
    """One-time 6-digit PIN. Rotates after each successful pairing and after
    too many wrong guesses, so brute forcing is impractical."""

    def __init__(self, on_rotate):
        self.on_rotate = on_rotate
        self.failures = 0
        self.pin = ""
        self.rotate()

    def rotate(self) -> None:
        self.pin = f"{secrets.randbelow(1_000_000):06d}"
        self.failures = 0
        self.on_rotate(self.pin)

    def attempt(self, pin: str) -> bool:
        if isinstance(pin, str) and hmac.compare_digest(pin.strip(), self.pin):
            self.rotate()
            return True
        self.failures += 1
        if self.failures >= PIN_MAX_FAILURES:
            log.warning("Too many wrong PIN attempts; generating a new PIN")
            self.rotate()
        return False


def _is_local(remote: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(remote or "")
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


@web.middleware
async def lan_only(request: web.Request, handler):
    if not _is_local(request.remote):
        log.warning("Rejected non-LAN request from %s", request.remote)
        raise web.HTTPForbidden(text="PC Remote only accepts local network connections")
    resp = await handler(request)
    resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


# --------------------------------------------------------------- handlers

async def index(request: web.Request):
    return web.FileResponse(STATIC / "index.html")


async def info(request: web.Request):
    return web.json_response({"name": socket.gethostname()})


async def pair(request: web.Request):
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest()
    pairing: Pairing = request.app["pairing"]
    if not pairing.attempt(str(body.get("pin", ""))):
        await asyncio.sleep(1)  # slow down guessing
        return web.json_response({"error": "Wrong or expired PIN"}, status=403)
    name = str(body.get("name") or "iPhone")
    token = request.app["store"].add(name)
    log.info("Paired new device '%s' from %s", name, request.remote)
    return web.json_response({"token": token, "name": socket.gethostname()})


class Session:
    """Per-connection input state."""

    def __init__(self):
        self.rem_x = 0.0
        self.rem_y = 0.0
        self.held: set[str] = set()

    def move(self, dx: float, dy: float) -> None:
        self.rem_x += dx
        self.rem_y += dy
        ix, iy = int(self.rem_x), int(self.rem_y)
        self.rem_x -= ix
        self.rem_y -= iy
        winput.mouse_move(ix, iy)

    def button(self, b: str, a: str) -> None:
        if b not in ("left", "right", "middle"):
            return
        if a == "down":
            self.held.add(b)
        elif a == "up":
            self.held.discard(b)
        winput.mouse_button(b, a)

    def release_all(self) -> None:
        for b in list(self.held):
            winput.mouse_button(b, "up")
        self.held.clear()


def _num(v, limit: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return max(-limit, min(limit, f))


def handle(session: Session, msg: dict) -> dict | None:
    t = msg.get("t")
    if t == "m":
        session.move(_num(msg.get("x"), 2000), _num(msg.get("y"), 2000))
    elif t == "s":
        winput.mouse_scroll(int(_num(msg.get("x"), 12000)), int(_num(msg.get("y"), 12000)))
    elif t == "b":
        session.button(str(msg.get("b")), str(msg.get("a")))
    elif t == "k":
        mods = msg.get("mods") or []
        if not isinstance(mods, list):
            mods = []
        winput.key_combo(str(msg.get("k", "")), [str(m) for m in mods])
    elif t == "txt":
        text = str(msg.get("s", ""))[:2000]
        winput.type_text(text)
    elif t == "ping":
        return {"t": "pong", "id": msg.get("id")}
    return None


async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=None, max_msg_size=64 * 1024)
    await ws.prepare(request)

    # First message must be a valid device token.
    try:
        first = await ws.receive(timeout=AUTH_TIMEOUT)
        data = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
    except (asyncio.TimeoutError, ValueError):
        data = {}
    device = request.app["store"].check(data.get("token")) if data.get("t") == "auth" else None
    try:
        if not device:
            await ws.send_json({"t": "auth_fail"})
            await ws.close()
            return ws
        await ws.send_json({"t": "auth_ok", "name": socket.gethostname()})
    except ConnectionError:  # client went away mid-handshake
        return ws
    log.info("'%s' connected from %s", device["name"], request.remote)

    session = Session()
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(msg.data)
            except ValueError:
                continue
            batch = payload if isinstance(payload, list) else [payload]
            for item in batch:
                if not isinstance(item, dict):
                    continue
                try:
                    reply = handle(session, item)
                except Exception:  # never let one bad event kill the connection
                    log.exception("Error handling %r", item)
                    continue
                if reply:
                    await ws.send_json(reply)
    finally:
        session.release_all()
        log.info("'%s' disconnected", device["name"])
    return ws


# ------------------------------------------------------------------- main

def lan_ips() -> list[str]:
    ips = []
    try:
        # UDP "connect" sends no packets; it just picks the outbound interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info_ in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info_[4][0]
            if ip not in ips and not ip.startswith("127.") and _is_local(ip):
                ips.append(ip)
    except OSError:
        pass
    # VPN adapters often own the default route (commonly 10.x / 100.x), so
    # prefer the address ranges home routers typically hand out.
    def rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ipaddress.ip_address(ip) in ipaddress.ip_network("172.16.0.0/12"):
            return 1
        return 2
    ips.sort(key=rank)
    return ips or ["127.0.0.1"]


def print_banner(port: int, pin: str, show_qr: bool, ip: str | None = None) -> None:
    ips = lan_ips()
    if ip:
        ips = [ip] + [i for i in ips if i != ip]
    url = f"http://{ips[0]}:{port}"
    print("\n" + "=" * 52)
    print("  PC Remote is running")
    print(f"  Open on your iPhone:  {url}")
    for ip in ips[1:]:
        print(f"           (or)         http://{ip}:{port}")
    print(f"  Pairing PIN:          {pin[:3]} {pin[3:]}")
    print("=" * 52)
    if show_qr:
        try:
            import qrcode

            qr = qrcode.QRCode(border=2)
            qr.add_data(f"{url}/?pin={pin}")
            qr.make(fit=True)
            print("  Scan with the iPhone camera to open and pair:\n")
            qr.print_ascii(invert=True)
        except (ImportError, UnicodeEncodeError):
            print("  (QR code unavailable in this console; type the address instead)")
    print("  Press Ctrl+C to stop.\n", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Control this PC from your phone.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--ip", help="LAN address to advertise in the URL/QR code")
    parser.add_argument("--no-qr", action="store_true", help="don't print a QR code")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--forget-devices", action="store_true",
                        help="revoke every paired device")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    store = DeviceStore(DEVICES_FILE)
    if args.list_devices:
        for d in store.devices:
            print(f"- {d['name']}  (paired {time.strftime('%Y-%m-%d %H:%M', time.localtime(d['paired']))})")
        if not store.devices:
            print("No paired devices.")
        return
    if args.forget_devices:
        store.clear()
        print("All paired devices have been revoked.")
        return

    started = False

    def on_rotate(pin: str) -> None:
        if started:  # PIN is single-use: show the fresh PIN/QR for the next device
            print_banner(args.port, pin, not args.no_qr, args.ip)

    app = web.Application(middlewares=[lan_only])
    app["store"] = store
    app["pairing"] = Pairing(on_rotate)
    app.add_routes([
        web.get("/", index),
        web.get("/api/info", info),
        web.post("/api/pair", pair),
        web.get("/ws", ws_handler),
        web.static("/static", STATIC),
    ])

    print_banner(args.port, app["pairing"].pin, not args.no_qr, args.ip)
    started = True
    try:
        web.run_app(app, host=args.host, port=args.port, print=None)
    except OSError as e:
        print(f"Could not start server on port {args.port}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

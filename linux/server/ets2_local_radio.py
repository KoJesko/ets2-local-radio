#!/usr/bin/env python3
# ETS2/ATS Local Radio - Linux server
# SPDX-License-Identifier: MPL-2.0
"""
Native Linux replacement for "ETS2 Local Radio server.exe".

It serves the unchanged upstream web/ front end and re-implements the HTTP API
the page talks to (/api/, /commands/, /favourite/, /station/, /eskago/), fed by
the small UDP telemetry plugin in linux/plugin (works for the native Linux game
and for the Windows game under Proton/TruckersMP).

Standard library only. Hotkeys and wheel/controller buttons are read straight
from evdev (/dev/input/event*), so they work under Wayland and X11 alike.
"""

import argparse
import errno
import json
import mimetypes
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = "ets2-local-radio"
HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.realpath(os.path.join(HERE, "..", ".."))
DEFAULT_WEB = os.path.join(REPO, "web")
PLUGIN_DIR = os.path.join(REPO, "linux", "plugin")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP)
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
FAVOURITES_FILE = os.path.join(CONFIG_DIR, "favourites.json")

TELEMETRY_TIMEOUT = 5.0  # seconds without a datagram before the game counts as gone

GAMES = {
    "ets2": {"appid": "227300", "dir": "Euro Truck Simulator 2"},
    "ats": {"appid": "270880", "dir": "American Truck Simulator"},
}
PLUGIN_NAME = "local-radio-linux"


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    # Same shape as the Windows settings.json, so that file can be copied over.
    "language": "en-GB",
    "port": 8330,
    "host": "0.0.0.0",          # 127.0.0.1 = this PC only; 0.0.0.0 also allows phones/tablets on the LAN
    "udpPort": 8331,            # must match LOCAL_RADIO_UDP_PORT in the game env (default 8331)
    "controller": None,         # substring of the controller name for "buttons"; null = any joystick
    "notify": False,            # desktop notification on station change (Linux stand-in for the overlay)
    "lastGame": "ets2",
    "keys": {
        "previous": "PageUp",
        "next": "Next",
        "stop": "End",
        "volumeUp": "Oemplus",
        "volumeDown": "OemMinus",
        "makeFavourite": "Pause",
        "goToFavourite": "",
    },
    "buttons": {k: "" for k in ("previous", "next", "stop", "volumeUp", "volumeDown", "makeFavourite", "goToFavourite")},
}


class Settings:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.load()

    def load(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8-sig") as f:
                loaded = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log(f"settings: could not read {SETTINGS_FILE}: {e} (using defaults)")
            return
        for k, v in loaded.items():
            if isinstance(v, dict) and isinstance(self.data.get(k), dict):
                self.data[k].update({kk: vv for kk, vv in v.items() if vv is not None})
            elif v is not None or k == "controller":
                self.data[k] = v

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with self.lock:
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, SETTINGS_FILE)

    def __getitem__(self, k):
        return self.data[k]


class Favourites:
    """country -> station name, same file format as the Windows favourites.json."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        try:
            with open(FAVOURITES_FILE, encoding="utf-8-sig") as f:
                self.data = json.load(f) or {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log(f"favourites: could not read {FAVOURITES_FILE}: {e}")
        if not self.data:
            self.data = {"_info": "File to store favourite station on country - station name basis"}

    def get(self, key):
        with self.lock:
            return self.data.get(key)

    def all_json(self):
        with self.lock:
            return json.dumps(self.data, indent=2, ensure_ascii=False)

    def set(self, key, value):
        with self.lock:
            self.data[key] = value
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = FAVOURITES_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, FAVOURITES_FILE)


# --------------------------------------------------------------------------- shared state

class State:
    def __init__(self, settings):
        self.settings = settings
        self.cond = threading.Condition()
        # telemetry
        self.last_packet = 0.0
        self.paused = True
        self.valid = False
        self.pos = (0.0, 0.0, 0.0)
        self.seq = 0
        # Sticky: the page re-runs loadScripts() whenever "game" changes and has no
        # config for "none", so we only ever switch between real games.
        self.game = settings["lastGame"] if settings["lastGame"] in GAMES else "ets2"
        self.command = self._make_command("none", "0", "0")

    def _make_command(self, action, amount, cmd_id=None):
        return {
            "id": cmd_id or uuid.uuid4().hex,
            "action": action,
            "amount": str(amount),
            "language": self.settings["language"],
            "game": self.game,
        }

    def push_command(self, action, amount="0", cmd_id=None):
        with self.cond:
            self.command = self._make_command(action, amount, cmd_id)
            self.cond.notify_all()
        log(f"command: {action} {amount}")

    def telemetry(self, pkt):
        game = pkt.get("game")
        with self.cond:
            self.seq = int(pkt.get("seq") or 0)
            if game in GAMES:
                self.last_packet = time.monotonic()
                self.paused = bool(pkt.get("paused"))
                self.valid = bool(pkt.get("valid"))
                self.pos = (float(pkt.get("x") or 0), float(pkt.get("y") or 0), float(pkt.get("z") or 0))
            else:  # plugin shutdown datagram ("none")
                self.last_packet = 0.0
                self.valid = False
            changed = game in GAMES and game != self.game
        if changed:
            log(f"game: {game}")
            self.game = game
            self.settings.data["lastGame"] = game
            try:
                self.settings.save()
            except OSError as e:
                log(f"settings: save failed: {e}")
            self.push_command("game", "0", "0")  # same as the Windows server

    def connected(self):
        return self.last_packet and time.monotonic() - self.last_packet < TELEMETRY_TIMEOUT

    def api_json(self):
        """Subset of the SCSSdkClient SCSTelemetry object that the web pages read."""
        with self.cond:
            live = self.connected()
            x, y, z = self.pos if (live and self.valid) else (0.0, 0.0, 0.0)
            return {
                "SdkActive": bool(live),
                "Paused": self.paused if live else True,
                "Timestamp": self.seq if live else 0,
                "Game": {"ets2": "Ets2", "ats": "Ats"}.get(self.game, "Unknown") if live else "Unknown",
                "TruckValues": {
                    "Positioning": {
                        "HeadPositionInWorldSpace": {"X": x, "Y": y, "Z": z},
                        "TruckPosition": {"X": x, "Y": y, "Z": z},
                    }
                },
            }


# --------------------------------------------------------------------------- telemetry (UDP)

def udp_listener(state, sock):
    log(f"telemetry: listening on udp://127.0.0.1:{sock.getsockname()[1]}")
    announced = False
    while True:
        data, _ = sock.recvfrom(2048)
        try:
            pkt = json.loads(data.decode("utf-8"))
            if not isinstance(pkt, dict):
                continue
            for k in ("x", "y", "z"):
                v = float(pkt.get(k) or 0)
                if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
                    raise ValueError(k)
            int(pkt.get("seq") or 0)
        except (ValueError, TypeError, UnicodeDecodeError, OverflowError):
            continue  # malformed datagram: drop it, never let it kill this thread
        try:
            if not announced and pkt.get("game") in GAMES:
                log(f"telemetry: receiving from {pkt.get('game')}")
                announced = True
            elif pkt.get("game") not in GAMES and announced:
                announced = False
                log("telemetry: game closed")
            state.telemetry(pkt)
        except Exception as e:  # keep listening whatever happens
            log(f"telemetry: bad datagram ignored: {e!r}")


# --------------------------------------------------------------------------- HTTP

ACTIONS = {  # /control/<name> and `ets2-local-radio ctl <name>`
    "next": ("next", "1"),
    "previous": ("next", "-1"),
    "stop": ("stop", "0"),
    "volume-up": ("volume", "5"),
    "volume-down": ("volume", "-5"),
    "favourite": ("favourite", "0"),
    "go-to-favourite": ("goToFavourite", "0"),
}


TRUSTED_ORIGIN = "http://localradio.koenvh.nl"  # upstream's hosted page may drive a local server via ?api=
STATE_CHANGING = ("/favourite/", "/control/", "/station/")


def host_allowed(host_header):
    """DNS-rebinding guard: only accept requests addressed to an IP or a local name."""
    if not host_header:
        return True  # HTTP/1.0 clients; browsers always send Host
    host = host_header.strip().lower()
    host = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
    if host in ("localhost", socket.gethostname().lower()) or host.endswith((".local", ".localhost", ".lan", ".home.arpa")):
        return True
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
        return True
    except OSError:
        return False


def make_handler(state, favourites, settings, web_root):
    web_root = os.path.realpath(web_root)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ETS2LocalRadioLinux/1.0"

        def log_message(self, fmt, *args):  # quiet: the page polls /api/ every second
            if os.environ.get("LOCAL_RADIO_DEBUG"):
                log("http:", fmt % args)

        def _headers(self, code, ctype, length=None, extra=None):
            self.send_response(code)
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "http://localradio.koenvh.nl")
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self._headers(code, ctype, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            raw_path = self.path.split("?", 1)[0]
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            try:
                if not host_allowed(self.headers.get("Host")):
                    return self._send(421, "Misdirected request", "text/plain")
                # The upstream page uses plain GETs for state changes, so block what browsers
                # label as cross-site instead (CSRF from other websites), except upstream's own page.
                if raw_path.startswith(STATE_CHANGING) and not self._write_allowed():
                    return self._send(403, '{"Success": false}')
                if raw_path == "/api/":
                    return self._send(200, json.dumps(state.api_json()))
                if raw_path == "/commands/":
                    return self._commands()
                if raw_path.startswith("/favourite/"):
                    return self._favourite(raw_path[len("/favourite/"):])
                if raw_path.startswith("/station/"):
                    return self._station(raw_path[len("/station/"):], query)
                if raw_path.startswith("/control/"):
                    action = raw_path[len("/control/"):].strip("/")
                    if action not in ACTIONS:
                        return self._send(404, json.dumps({"Success": False, "Actions": sorted(ACTIONS)}))
                    state.push_command(*ACTIONS[action])
                    return self._send(200, '{"Success": true}')
                if raw_path.startswith("/eskago/"):
                    return self._eskago(raw_path[len("/eskago/"):])
                return self._static(urllib.parse.unquote(raw_path))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _write_allowed(self):
            """Fail-closed CSRF check for state-changing GETs (the upstream page can't send tokens)."""
            sfs = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if sfs in ("same-origin", "none"):
                return True
            source = self.headers.get("Origin") or self.headers.get("Referer") or ""
            if source:
                p = urllib.parse.urlsplit(source)
                origin = f"{p.scheme}://{p.netloc}".lower()
                own = f"http://{(self.headers.get('Host') or '').lower()}"
                return origin in (own, TRUSTED_ORIGIN)
            # No browser headers at all: a non-browser client (ctl CLI, curl, apps).
            return not sfs

        # -- endpoints -------------------------------------------------------

        def _commands(self):
            # Server-sent events. Like the Windows server we keep re-sending the current
            # command; the page de-duplicates on "id". Sent on change + once per second.
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "http://localradio.koenvh.nl")
            self.end_headers()
            last = None
            while True:
                with state.cond:
                    if state.command is last:
                        state.cond.wait(timeout=1.0)
                    last = state.command
                    payload = json.dumps(last)
                self.wfile.write(f"event: commands\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()

        def _favourite(self, rest):
            rest = urllib.parse.unquote(rest)
            if rest == "":
                return self._send(200, favourites.all_json())
            if "/" not in rest:
                return self._send(200, json.dumps({"Name": favourites.get(rest) or ""}, ensure_ascii=False))
            country, name = rest.split("/", 1)
            if not re.fullmatch(r"[\w\-]{1,64}", country) or len(name) > 200:
                return self._send(400, '{"Success": false}')
            favourites.set(country, name)
            return self._send(200, '{"Success": true}')

        def _station(self, rest, query):
            # /station/<name>/<signal>?<logo>  - the Windows server drew its in-game overlay here.
            name = urllib.parse.unquote(rest.split("/", 1)[0])
            if settings["notify"] and name and shutil.which("notify-send"):
                prefix = "Now playing:"
                try:
                    with open(os.path.join(web_root, "lang", settings["language"] + ".json"), encoding="utf-8-sig") as f:
                        prefix = json.load(f).get("server", {}).get("now-playing", prefix)
                except (OSError, ValueError):
                    pass
                subprocess.Popen(
                    ["notify-send", "-a", "ETS2 Local Radio", "-t", "4000", "-i", "audio-radio", f"{prefix} {name}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            return self._send(200, '{"Success": true}')

        def _eskago(self, station):
            station = urllib.parse.unquote(station)
            req = urllib.request.Request(
                "https://www.eskago.pl/radio/" + urllib.parse.quote(station),
                headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    page = r.read().decode("utf-8", "replace")
            except OSError as e:
                log(f"eskago: {e}")
                return self._send(502, '{"Success": false}')
            base = re.search(r"streamUrl\s=\s'(.+?)';", page)
            icsu = re.search(r'icsu"\svalue="(.+?)"\s', page)
            if not base:
                return self._send(502, '{"Success": false}')
            self._headers(302, "text/plain", 0, {"Location": base.group(1) + (icsu.group(1) if icsu else "")})

        def _static(self, path):
            rel = path.lstrip("/")
            if rel == "":
                rel = "index.html"
            # custom.js lives in the config dir so `git pull` never conflicts with it.
            if rel == "custom.js" and os.path.isfile(os.path.join(CONFIG_DIR, "custom.js")):
                full = os.path.join(CONFIG_DIR, "custom.js")
            else:
                full = os.path.realpath(os.path.join(web_root, rel))
                if full != web_root and not full.startswith(web_root + os.sep):
                    return self._send(403, "Forbidden", "text/plain")
                if os.path.isdir(full):
                    full = os.path.join(full, "index.html")
            if not os.path.isfile(full):
                return self._send(404, "Not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            size = os.path.getsize(full)
            start, end = 0, size - 1
            # Single byte-range support: <audio> elements (static noise, switch sound) seek/loop with Range.
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
            if m and size and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    return self._headers(416, "text/plain", 0, {"Content-Range": f"bytes */{size}"})
                self._headers(206, ctype, end - start + 1,
                              {"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes"})
            else:
                self._headers(200, ctype, size, {"Accept-Ranges": "bytes"})
            if self.command == "HEAD":
                return
            with open(full, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    return Handler


# --------------------------------------------------------------------------- evdev input

EV_KEY, EV_ABS = 0x01, 0x03
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11
BTN_MISC, BTN_JOYSTICK, KEY_MAX = 0x100, 0x120, 0x2FF
EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux

# Windows Forms "Keys" names (as stored in the Windows settings.json) -> evdev codes.
_KEYS = {
    "Escape": 1, "D1": 2, "D2": 3, "D3": 4, "D4": 5, "D5": 6, "D6": 7, "D7": 8, "D8": 9, "D9": 10, "D0": 11,
    "OemMinus": 12, "Oemplus": 13, "Back": 14, "Tab": 15,
    "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21, "U": 22, "I": 23, "O": 24, "P": 25,
    "OemOpenBrackets": 26, "Oem4": 26, "Oem6": 27, "OemCloseBrackets": 27, "Return": 28, "Enter": 28,
    "LControlKey": 29, "A": 30, "S": 31, "D": 32, "F": 33, "G": 34, "H": 35, "J": 36, "K": 37, "L": 38,
    "Oem1": 39, "OemSemicolon": 39, "Oem7": 40, "OemQuotes": 40, "Oemtilde": 41, "Oem3": 41,
    "LShiftKey": 42, "Oem5": 43, "OemPipe": 43, "OemBackslash": 86, "Oem102": 86,
    "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48, "N": 49, "M": 50,
    "Oemcomma": 51, "OemPeriod": 52, "OemQuestion": 53, "Oem2": 53, "RShiftKey": 54, "Multiply": 55,
    "LMenu": 56, "Space": 57, "Capital": 58, "CapsLock": 58,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64, "F7": 65, "F8": 66, "F9": 67, "F10": 68,
    "NumLock": 69, "Scroll": 70, "NumPad7": 71, "NumPad8": 72, "NumPad9": 73, "Subtract": 74,
    "NumPad4": 75, "NumPad5": 76, "NumPad6": 77, "Add": 78, "NumPad1": 79, "NumPad2": 80, "NumPad3": 81,
    "NumPad0": 82, "Decimal": 83, "F11": 87, "F12": 88, "RControlKey": 97, "Divide": 98,
    "PrintScreen": 99, "Snapshot": 99, "RMenu": 100, "Home": 102, "Up": 103, "PageUp": 104, "Prior": 104,
    "Left": 105, "Right": 106, "End": 107, "Down": 108, "Next": 109, "PageDown": 109, "Insert": 110,
    "Delete": 111, "VolumeMute": 113, "VolumeDown": 114, "VolumeUp": 115, "Pause": 119,
    "LWin": 125, "RWin": 126, "Apps": 127, "MediaNextTrack": 163, "MediaPlayPause": 164,
    "MediaPreviousTrack": 165, "MediaStop": 166,
    **{f"F{n}": 170 + n for n in range(13, 25)},  # F13..F24 = 183..194
}
_KEY_NAMES = {}
for _n, _c in _KEYS.items():
    _KEY_NAMES.setdefault(_c, _n)


def parse_key(spec):
    """'PageUp' (Windows name), 'KEY_PAGEUP' / 'pageup' (Linux name) or a raw code -> evdev code."""
    if not spec:
        return None
    spec = str(spec).strip()
    if spec.isdigit():
        return int(spec)
    for name, code in _KEYS.items():
        if name.lower() == spec.lower():
            return code
    linux = spec.upper()
    linux = linux if linux.startswith("KEY_") else "KEY_" + linux
    return _linux_key_codes().get(linux)


_LINUX_CODES = None


def _linux_key_codes():
    """KEY_* names from the kernel header if present (optional nicety)."""
    global _LINUX_CODES
    if _LINUX_CODES is None:
        _LINUX_CODES = {}
        try:
            with open("/usr/include/linux/input-event-codes.h") as f:
                for m in re.finditer(r"#define\s+(KEY_\w+|BTN_\w+)\s+(0x[0-9a-fA-F]+|\d+)\b", f.read()):
                    _LINUX_CODES.setdefault(m.group(1), int(m.group(2), 0))
        except OSError:
            pass
    return _LINUX_CODES


def _read_sys(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _caps(event_name, kind):
    words = _read_sys(f"/sys/class/input/{event_name}/device/capabilities/{kind}").split()
    bits, value = set(), 0
    for w in words:  # most significant word first, 64-bit words
        value = (value << 64) | int(w, 16)
    i = 0
    while value:
        if value & 1:
            bits.add(i)
        value >>= 1
        i += 1
    return bits


class InputDevice:
    def __init__(self, event_name):
        self.event_name = event_name
        self.path = "/dev/input/" + event_name
        self.name = _read_sys(f"/sys/class/input/{event_name}/device/name") or event_name
        self.keys = _caps(event_name, "key")
        self.abs = _caps(event_name, "abs")
        dev = _read_sys(f"/sys/class/input/{event_name}/dev")
        udev = _read_sys(f"/run/udev/data/c{dev}") if dev else ""
        if "ID_INPUT_JOYSTICK=1" in udev:
            self.joystick = True
        elif udev:
            self.joystick = False
        else:  # no udev db (containers): guess from joystick/gamepad buttons
            self.joystick = any(0x120 <= k <= 0x13F or 0x2C0 <= k <= 0x2E7 for k in self.keys)
        # Button numbering identical to the kernel joydev driver (= jstest / Steam "button N").
        self.button_index = {}
        order = [k for k in range(BTN_JOYSTICK, KEY_MAX + 1) if k in self.keys]
        order += [k for k in range(BTN_MISC, BTN_JOYSTICK) if k in self.keys]
        for i, k in enumerate(order):
            self.button_index[k] = i
        self.n_buttons = len(order)
        self.hat = {}  # (axis) -> last value
        self.fd = None

    def open(self):
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        return self


def list_devices():
    try:
        names = sorted(n for n in os.listdir("/sys/class/input") if n.startswith("event"))
    except OSError:
        return []
    return [InputDevice(n) for n in names]


def read_events(dev):
    """Yield ('key', code) for key/button presses and ('button', index) for joystick buttons/hat."""
    try:
        data = os.read(dev.fd, EVENT.size * 64)
    except BlockingIOError:
        return
    for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
        _, _, etype, code, value = EVENT.unpack_from(data, off)
        if etype == EV_KEY and value == 1:  # press only, ignore autorepeat (2) and release (0)
            if code < BTN_MISC:
                yield ("key", code)
            elif dev.joystick and code in dev.button_index:
                yield ("button", dev.button_index[code])
        elif etype == EV_ABS and dev.joystick and code in (ABS_HAT0X, ABS_HAT0Y):
            prev = dev.hat.get(code, 0)
            dev.hat[code] = value
            if value != 0 and value != prev:
                # Windows layout: buttons, then POV up/right/down/left.
                offset = {(ABS_HAT0Y, -1): 0, (ABS_HAT0X, 1): 1, (ABS_HAT0Y, 1): 2, (ABS_HAT0X, -1): 3}.get(
                    (code, 1 if value > 0 else -1))
                if offset is not None:
                    yield ("button", dev.n_buttons + offset)


BINDING_ACTIONS = {
    "next": ("next", "1"),
    "previous": ("next", "-1"),
    "stop": ("stop", "0"),
    "volumeUp": ("volume", "5"),
    "volumeDown": ("volume", "-5"),
    "makeFavourite": ("favourite", "0"),
    "goToFavourite": ("goToFavourite", "0"),
}


def input_loop(state, settings):
    keymap = {}
    for action, spec in settings["keys"].items():
        code = parse_key(spec)
        if code is not None and action in BINDING_ACTIONS:
            keymap[code] = action
        elif spec:
            log(f"input: unknown key name {spec!r} for {action}")
    buttonmap = []  # (device-substring or None, index, action)
    for action, spec in settings["buttons"].items():
        if not spec or action not in BINDING_ACTIONS:
            continue
        spec = str(spec)
        dev_filter, _, idx = spec.rpartition("#")
        if not idx.isdigit():
            log(f"input: bad button binding {spec!r} for {action}")
            continue
        buttonmap.append((dev_filter or settings["controller"], int(idx), action))
    if not keymap and not buttonmap:
        log("input: no keys or buttons bound")
        return

    open_devs = {}  # event name -> InputDevice
    warned = set()
    last_scan = 0.0
    while True:
        if time.monotonic() - last_scan > 5.0:  # hotplug
            last_scan = time.monotonic()
            for dev in list_devices():
                if dev.event_name in open_devs:
                    continue
                wanted = bool(dev.keys & set(keymap)) or (
                    dev.joystick and any(f is None or f.lower() in dev.name.lower() for f, _, _ in buttonmap))
                if not wanted:
                    continue
                try:
                    open_devs[dev.event_name] = dev.open()
                    log(f"input: watching {dev.name} ({dev.event_name}{', joystick' if dev.joystick else ''})")
                except PermissionError:
                    if dev.event_name not in warned:
                        warned.add(dev.event_name)
                        hint = "" if dev.joystick else " - add yourself to the 'input' group, or bind DE shortcuts to `ets2-local-radio ctl ...`"
                        log(f"input: no permission for {dev.name} ({dev.path}){hint}")
                except OSError:
                    pass
        if not open_devs:
            time.sleep(1.0)
            continue
        fds = {d.fd: d for d in open_devs.values()}
        try:
            ready, _, _ = select.select(list(fds), [], [], 1.0)
        except OSError:
            ready = []
        for fd in ready:
            dev = fds[fd]
            try:
                for kind, value in read_events(dev):
                    if kind == "key" and value in keymap:
                        state.push_command(*BINDING_ACTIONS[keymap[value]])
                    elif kind == "button":
                        for dev_filter, idx, action in buttonmap:
                            if idx == value and (dev_filter is None or dev_filter.lower() in dev.name.lower()):
                                state.push_command(*BINDING_ACTIONS[action])
            except OSError as e:
                if e.errno in (errno.ENODEV, errno.EIO, errno.EBADF):
                    log(f"input: {dev.name} disconnected")
                    os.close(dev.fd)
                    del open_devs[dev.event_name]


# --------------------------------------------------------------------------- commands

def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the outgoing interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


def cmd_serve(args):
    settings = Settings()
    if args.port:
        settings.data["port"] = args.port
    if args.host:
        settings.data["host"] = args.host
    web_root = args.web or DEFAULT_WEB
    if not os.path.isfile(os.path.join(web_root, "index.html")):
        sys.exit(f"web folder not found at {web_root} (use --web)")
    if not os.path.isfile(SETTINGS_FILE):
        settings.save()  # write defaults so they're easy to edit
    state = State(settings)
    favourites = Favourites()

    # Bind both sockets before starting anything, so a second instance fails loudly
    # instead of half-running without telemetry.
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.bind(("127.0.0.1", int(settings["udpPort"])))
    except OSError as e:
        sys.exit(f"cannot listen on udp 127.0.0.1:{settings['udpPort']}: {e} (already running?)")
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    # Browsers drop idle keep-alive sockets all the time; don't print tracebacks for that.
    _default_handle_error = ThreadingHTTPServer.handle_error
    ThreadingHTTPServer.handle_error = lambda self, req, addr: (
        None if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)) else _default_handle_error(self, req, addr))
    try:
        httpd = ThreadingHTTPServer((settings["host"], int(settings["port"])), make_handler(state, favourites, settings, web_root))
    except OSError as e:
        sys.exit(f"cannot listen on {settings['host']}:{settings['port']}: {e} (already running?)")

    threading.Thread(target=udp_listener, args=(state, udp), daemon=True).start()
    if not args.no_input:
        threading.Thread(target=input_loop, args=(state, settings), daemon=True).start()
    port = settings["port"]
    log(f"ETS2 Local Radio: http://localhost:{port}/")
    if settings["host"] in ("0.0.0.0", "::"):
        for ip in local_ips():
            log(f"  on your phone/tablet: http://{ip}:{port}/")
    log(f"  settings: {SETTINGS_FILE}")
    if args.open:
        subprocess.Popen(["xdg-open", f"http://localhost:{port}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def _base_url():
    s = Settings()
    return f"http://127.0.0.1:{s['port']}"


def cmd_ctl(args):
    url = f"{_base_url()}/control/{args.action}"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            r.read()
    except OSError as e:
        sys.exit(f"could not reach the Local Radio server ({url}): {e}")


def cmd_open(args):
    url = _base_url() + "/"
    try:
        urllib.request.urlopen(url + "version.json", timeout=2).read()
    except OSError:
        # Not running: try the systemd user unit, else start in the background.
        started = shutil.which("systemctl") and subprocess.run(
            ["systemctl", "--user", "start", f"{APP}.service"], stderr=subprocess.DEVNULL).returncode == 0
        if not started:
            subprocess.Popen([sys.executable, os.path.realpath(__file__), "serve"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "version.json", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
    subprocess.Popen(["xdg-open", url.replace("127.0.0.1", "localhost")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_gui(args):
    sys.path.insert(0, HERE)
    try:
        import ets2_local_radio_gui as gui
    except ImportError as e:
        msg = f"The window needs PySide6 or PyQt6 ({e}). Opening the radio page instead."
        log(msg)
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "ETS2 Local Radio", msg], stderr=subprocess.DEVNULL)
        return cmd_open(args)
    sys.exit(gui.main())


def cmd_status(args):
    try:
        with urllib.request.urlopen(_base_url() + "/api/", timeout=3) as r:
            d = json.load(r)
    except OSError as e:
        sys.exit(f"server not reachable: {e}")
    p = d["TruckValues"]["Positioning"]["HeadPositionInWorldSpace"]
    print(f"game: {d['Game']}  sdk active: {d['SdkActive']}  paused: {d['Paused']}")
    print(f"position: X={p['X']:.1f} Y={p['Y']:.1f} Z={p['Z']:.1f}")


def cmd_learn(args):
    """Print key and controller-button presses so they can be put into settings.json."""
    devs = []
    for dev in list_devices():
        if not dev.keys and not dev.abs:
            continue
        try:
            devs.append(dev.open())
        except OSError:
            pass
    if not devs:
        sys.exit("no readable input devices (keyboards need the 'input' group; controllers usually work as-is)")
    print("Press keys or controller buttons (Ctrl+C to stop).")
    print("Keys go into \"keys\", controller buttons into \"buttons\" in", SETTINGS_FILE)
    fds = {d.fd: d for d in devs}
    try:
        while True:
            ready, _, _ = select.select(list(fds), [], [], 1.0)
            for fd in ready:
                dev = fds[fd]
                try:
                    for kind, value in read_events(dev):
                        if kind == "key":
                            name = _KEY_NAMES.get(value) or next(
                                (n for n, c in _linux_key_codes().items() if c == value and n.startswith("KEY_")), str(value))
                            print(f"  key     {name!r:20}  ({dev.name})")
                        else:
                            print(f"  button  \"{value}\"  or  \"{dev.name}#{value}\"")
                except OSError:
                    pass
    except KeyboardInterrupt:
        print()


def steam_libraries():
    roots = [
        "~/.local/share/Steam", "~/.steam/steam", "~/.steam/root",
        "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
        "~/snap/steam/common/.local/share/Steam",
    ]
    libs = []
    for root in roots:
        vdf = os.path.join(os.path.expanduser(root), "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = os.path.realpath(m.group(1).replace("\\\\", "\\"))
            if p not in libs:
                libs.append(p)
    return libs


def find_games():
    found = {}
    for lib in steam_libraries():
        for game, info in GAMES.items():
            d = os.path.join(lib, "steamapps", "common", info["dir"])
            if os.path.isdir(os.path.join(d, "bin")) and game not in found:
                found[game] = d
    return found


def plugin_binaries():
    out = {}
    for ext in ("dll", "so"):
        for d in ("build", "prebuilt"):
            p = os.path.join(PLUGIN_DIR, d, f"{PLUGIN_NAME}.{ext}")
            if os.path.isfile(p):
                out[ext] = p
                break
    return out


PLUGIN_TARGETS = (("dll", "win_x64"), ("so", "linux_x64"))


def plugin_installed(folder):
    return any(os.path.isfile(os.path.join(folder, "bin", sub, "plugins", f"{PLUGIN_NAME}.{ext}"))
               for ext, sub in PLUGIN_TARGETS)


def install_plugin(folder, uninstall=False):
    """Copy (or remove) the plugin for one game folder. Returns the paths touched."""
    bins = plugin_binaries()
    if not bins and not uninstall:
        raise FileNotFoundError("no plugin binaries: run `make -C linux/plugin` first")
    if not os.path.isdir(os.path.join(folder, "bin")):
        raise FileNotFoundError(f"not a game folder (no bin/ inside): {folder}")
    touched = []
    for ext, sub in PLUGIN_TARGETS:
        dest_dir = os.path.join(folder, "bin", sub, "plugins")
        dest = os.path.join(dest_dir, f"{PLUGIN_NAME}.{ext}")
        if uninstall:
            if os.path.exists(dest):
                os.remove(dest)
                touched.append(dest)
            continue
        if ext not in bins or not os.path.isdir(os.path.join(folder, "bin", sub)):
            continue  # e.g. no linux_x64 binaries in this install
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(bins[ext], dest)
        touched.append(dest)
    return touched


def cmd_install_plugin(args):
    if args.path:
        targets = {args.game: os.path.realpath(args.path)}
    else:
        targets = {g: p for g, p in find_games().items() if args.game in ("all", g)}
    if not targets:
        sys.exit("no ETS2/ATS install found in your Steam libraries (use --path <game folder>)")
    for folder in targets.values():
        try:
            for path in install_plugin(folder, args.uninstall):
                print(f"{'removed' if args.uninstall else 'installed'} {path}")
        except FileNotFoundError as e:
            sys.exit(str(e))
    if not args.uninstall:
        print("Done. The game shows an 'Advanced SDK features' prompt on start; accept it.")


def main():
    ap = argparse.ArgumentParser(prog=APP, description="ETS2/ATS Local Radio for Linux")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="run the server (default)")
    p.add_argument("--port", type=int, help="HTTP port (default from settings, 8330)")
    p.add_argument("--host", help="bind address (default 0.0.0.0)")
    p.add_argument("--web", help="path to the web/ folder")
    p.add_argument("--open", action="store_true", help="open the page in the browser")
    p.add_argument("--no-input", action="store_true", help="don't read keyboard/controller hotkeys")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("open", help="start the server if needed and open the page")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("gui", help="server window with status, key bindings and plugin install (needs PySide6 or PyQt6)")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("ctl", help="send a command (bind these to desktop shortcuts)")
    p.add_argument("action", choices=sorted(ACTIONS))
    p.set_defaults(func=cmd_ctl)

    p = sub.add_parser("status", help="show what the server receives from the game")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("learn", help="show names of pressed keys / controller buttons for settings.json")
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("install-plugin", help="copy the telemetry plugin into ETS2/ATS")
    p.add_argument("--game", choices=["all", "ets2", "ats"], default="all")
    p.add_argument("--path", help="game folder (default: found via Steam libraries)")
    p.add_argument("--uninstall", action="store_true", help="remove the plugin instead")
    p.set_defaults(func=cmd_install_plugin)

    args = ap.parse_args()
    if not args.cmd:
        args = ap.parse_args(["serve"] + sys.argv[1:])
    args.func(args)


if __name__ == "__main__":
    main()

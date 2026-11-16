#!/usr/bin/env python3
# ETS2/ATS Local Radio - Linux server window
# SPDX-License-Identifier: MPL-2.0
"""
The Linux counterpart of the Windows "ETS2 Local Radio server" window:
game status, URL + open button, plugin install, key / controller bindings,
options and the live server log.

Like on Windows the window *is* the server: it starts ets2_local_radio.py as a
child process and stops it when closed. If the systemd user service is already
running, the window attaches to it instead (and leaves it running).
"""

import os
import select
import shutil
import subprocess
import sys
import threading
import urllib.request
import json

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import ets2_local_radio as core  # noqa: E402

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    Signal = QtCore.Signal
except ImportError:
    from PyQt6 import QtCore, QtGui, QtWidgets
    Signal = QtCore.pyqtSignal

SERVER_SCRIPT = os.path.join(core.HERE, "ets2_local_radio.py")
ICON_FILE = os.path.join(core.REPO, "server", "ETS2 Local Radio desktop", "icon.ico")
SERVICE = f"{core.APP}.service"

# (settings key, translation key in web/lang/*.json "server", English fallback)
BINDINGS = [
    ("next", "next-station-key", "Next station:"),
    ("previous", "previous-station-key", "Previous station:"),
    ("stop", "stop-playback-key", "Stop playback:"),
    ("volumeUp", "volume-up-key", "Volume up:"),
    ("volumeDown", "volume-down-key", "Volume down:"),
    ("makeFavourite", "make-favourite-key", "Make favourite:"),
    ("goToFavourite", "go-to-favourite-key", "Switch to favourite:"),
]
GAME_NAMES = {"ets2": "Euro Truck Simulator 2", "ats": "American Truck Simulator"}

# Fallback when the platform gives no native scan code (Qt key -> Windows key name).
_QT_KEYS = {
    "Key_PageUp": "PageUp", "Key_PageDown": "Next", "Key_End": "End", "Key_Home": "Home",
    "Key_Insert": "Insert", "Key_Pause": "Pause", "Key_Plus": "Oemplus", "Key_Equal": "Oemplus",
    "Key_Minus": "OemMinus", "Key_Up": "Up", "Key_Down": "Down", "Key_Left": "Left", "Key_Right": "Right",
    "Key_Space": "Space", "Key_Return": "Return", "Key_Tab": "Tab", "Key_Comma": "Oemcomma",
    "Key_Period": "OemPeriod", "Key_Slash": "OemQuestion", "Key_ScrollLock": "Scroll",
    "Key_MediaNext": "MediaNextTrack", "Key_MediaPrevious": "MediaPreviousTrack",
    "Key_MediaPlay": "MediaPlayPause", "Key_MediaTogglePlayPause": "MediaPlayPause",
    "Key_MediaStop": "MediaStop", "Key_VolumeUp": "VolumeUp", "Key_VolumeDown": "VolumeDown",
}


def key_name_from_event(ev):
    """Name for settings.json: the evdev code (scan code - 8 on Linux) mapped to a Windows key name."""
    code = ev.nativeScanCode() - 8
    if code > 0:
        name = core._KEY_NAMES.get(code)
        if name:
            return name
        linux = next((n for n, c in core._linux_key_codes().items() if c == code and n.startswith("KEY_")), None)
        return linux or str(code)
    key = ev.key()
    for attr, name in _QT_KEYS.items():
        if getattr(QtCore.Qt.Key, attr, None) == key:
            return name
    text = ev.text().upper()
    if len(text) == 1 and text.isalpha():
        return text
    if len(text) == 1 and text.isdigit():
        return "D" + text
    for n in range(1, 25):
        if key == getattr(QtCore.Qt.Key, f"Key_F{n}", None):
            return f"F{n}"
    return None


class Translator:
    def __init__(self, web_root, language):
        self.strings = {}
        try:
            with open(os.path.join(web_root, "lang", language + ".json"), encoding="utf-8-sig") as f:
                self.strings = json.load(f).get("server", {}) or {}
        except (OSError, ValueError):
            pass

    def __call__(self, key, default):
        return (self.strings.get(key) or default).replace("\\n", "\n")


# --------------------------------------------------------------------------- server process

class ServerLink(QtCore.QObject):
    """Runs the server as a child process, or attaches to the systemd user service."""
    log_line = Signal(str)

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.child = None
        self.journal = None
        self.mode = None  # "child" | "service" | "external"

    @property
    def base(self):
        return f"http://127.0.0.1:{self.settings['port']}"

    def reachable(self):
        try:
            urllib.request.urlopen(self.base + "/version.json", timeout=0.5).read()
            return True
        except OSError:
            return False

    @staticmethod
    def service_active():
        return bool(shutil.which("systemctl")) and subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", SERVICE]).returncode == 0

    def _pump(self, stream):
        for line in iter(stream.readline, ""):
            self.log_line.emit(line.rstrip("\n"))

    def start(self):
        if self.reachable():
            if self.service_active():
                self.mode = "service"
                if shutil.which("journalctl"):
                    self.journal = subprocess.Popen(
                        ["journalctl", "--user", "-u", SERVICE, "-f", "-n", "40", "-o", "cat"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                    threading.Thread(target=self._pump, args=(self.journal.stdout,), daemon=True).start()
            else:
                self.mode = "external"
                self.log_line.emit("A Local Radio server is already running (started from a terminal?).")
            return
        self.mode = "child"
        self.child = subprocess.Popen(
            [sys.executable, "-u", SERVER_SCRIPT, "serve"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        threading.Thread(target=self._pump, args=(self.child.stdout,), daemon=True).start()

    def stop(self):
        for p in (self.journal, self.child):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.journal = self.child = None

    def restart(self):
        if self.mode == "service":
            subprocess.run(["systemctl", "--user", "restart", SERVICE])
            self.log_line.emit("— service restarted —")
            return True
        if self.mode == "child":
            self.stop()
            self.log_line.emit("— server restarted —")
            self.start()
            return True
        return False  # external: we don't own it

    def child_died(self):
        return self.mode == "child" and self.child is not None and self.child.poll() is not None

    def api(self):
        try:
            with urllib.request.urlopen(self.base + "/api/", timeout=0.4) as r:
                return json.load(r)
        except (OSError, ValueError):
            return None


# --------------------------------------------------------------------------- binding capture

class ButtonCapture(QtCore.QObject):
    """Waits (in a thread) for the first controller button / hat press on any joystick."""
    pressed = Signal(str, int)

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self._stop.set()

    def _run(self):
        devs = []
        for d in core.list_devices():
            if d.joystick:
                try:
                    devs.append(d.open())
                except OSError:
                    pass
        try:
            fds = {d.fd: d for d in devs}
            while fds and not self._stop.is_set():
                ready, _, _ = select.select(list(fds), [], [], 0.2)
                for fd in ready:
                    try:
                        for kind, value in core.read_events(fds[fd]):
                            if kind == "button" and not self._stop.is_set():
                                self._stop.set()
                                self.pressed.emit(fds[fd].name, value)
                                return
                    except OSError:
                        pass
        finally:
            for d in devs:
                try:
                    os.close(d.fd)
                except OSError:
                    pass


class BindingButton(QtWidgets.QPushButton):
    """Shows a binding; click, then press a key / controller button. Esc, Backspace or Delete clears."""
    listening = Signal(object)

    def __init__(self, value, kind):
        super().__init__()
        self.kind = kind  # "key" | "button"
        self.value = value or ""
        self.active = False
        self.setCheckable(True)
        self.setMinimumWidth(170)
        self.clicked.connect(self._toggle)
        self.refresh()

    def refresh(self):
        if self.active:
            self.setText("Press a key…" if self.kind == "key" else "Press a controller button…")
        else:
            self.setText(self.value or "—")
        self.setChecked(self.active)

    def _toggle(self):
        self.active = not self.active
        self.refresh()
        self.listening.emit(self if self.active else None)

    def finish(self, value=None, clear=False):
        if clear:
            self.value = ""
        elif value is not None:
            self.value = value
        self.active = False
        self.refresh()


# --------------------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = core.Settings()
        if not os.path.isfile(core.SETTINGS_FILE):
            self.settings.save()
        self.web_root = core.DEFAULT_WEB
        self.tr_ = Translator(self.web_root, self.settings["language"])
        self.link = ServerLink(self.settings)
        self.link.log_line.connect(self.append_log)
        self.capturing = None
        self.capture = None
        self.games = core.find_games()

        self.setWindowTitle("ETS2 Local Radio server")
        if os.path.isfile(ICON_FILE):
            self.setWindowIcon(QtGui.QIcon(ICON_FILE))
        else:
            self.setWindowIcon(QtGui.QIcon.fromTheme("audio-radio"))
        self._build()
        QtWidgets.QApplication.instance().installEventFilter(self)

        self.link.start()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)
        QtCore.QTimer.singleShot(300, self.poll)

    # -- layout -------------------------------------------------------------

    def _build(self):
        t = self.tr_
        central = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(central)

        # Info
        self.info_box = QtWidgets.QGroupBox(t("info", "Info"))
        form = QtWidgets.QFormLayout(self.info_box)
        self.game_value = QtWidgets.QLabel("—")
        self.status_value = QtWidgets.QLabel("—")
        self.coord_value = QtWidgets.QLabel("—")
        self.coord_value.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        url_row = QtWidgets.QHBoxLayout()
        self.url_combo = QtWidgets.QComboBox()
        self.url_combo.setEditable(False)
        self.open_button = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("internet-web-browser"),
                                                 t("open-local-radio", "Open ETS2 Local Radio"))
        self.open_button.clicked.connect(self.open_radio)
        url_row.addWidget(self.url_combo, 1)
        url_row.addWidget(self.open_button)
        self.game_label = QtWidgets.QLabel(t("game", "Game:"))
        self.status_label = QtWidgets.QLabel(t("status", "Status:"))
        self.coord_label = QtWidgets.QLabel(t("coordinates", "Coordinates:"))
        self.url_label = QtWidgets.QLabel(t("url", "URL:"))
        form.addRow(self.game_label, self.game_value)
        form.addRow(self.status_label, self.status_value)
        form.addRow(self.coord_label, self.coord_value)
        form.addRow(self.url_label, url_row)
        root.addWidget(self.info_box)

        # Plugin
        self.plugin_box = QtWidgets.QGroupBox(t("install", "Install plugin"))
        prow = QtWidgets.QHBoxLayout(self.plugin_box)
        self.plugin_buttons = {}
        for game in ("ets2", "ats"):
            b = QtWidgets.QPushButton()
            b.setMinimumHeight(48)
            b.clicked.connect(lambda _=False, g=game: self.install_plugin(g))
            self.plugin_buttons[game] = b
            prow.addWidget(b)
        self.remove_plugin = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("edit-delete"), t("remove-plugin", "Remove plugin"))
        self.remove_plugin.setFlat(True)
        self.remove_plugin.clicked.connect(self.uninstall_plugins)
        prow.addWidget(self.remove_plugin)
        root.addWidget(self.plugin_box)

        # Settings tabs
        self.tabs = QtWidgets.QTabWidget()
        self.key_buttons, self.pad_buttons = {}, {}
        self.binding_labels = []
        for kind, store, title_key, title in (("key", self.key_buttons, "keyboard", "Keyboard"),
                                              ("button", self.pad_buttons, "controller", "Controller")):
            page = QtWidgets.QWidget()
            f = QtWidgets.QFormLayout(page)
            section = "keys" if kind == "key" else "buttons"
            for action, tkey, default in BINDINGS:
                b = BindingButton(self.settings[section].get(action, ""), kind)
                b.listening.connect(self.start_capture)
                store[action] = b
                label = QtWidgets.QLabel(self.binding_text(tkey, default, kind))
                self.binding_labels.append((label, tkey, default, kind))
                f.addRow(label, b)
            hint = QtWidgets.QLabel(
                "Click a box, then press the key. Esc, Backspace or Delete clears it."
                if kind == "key" else
                "Click a box, then press a button on your wheel / controller. Esc clears it.")
            hint.setWordWrap(True)
            hint.setEnabled(False)
            f.addRow(hint)
            self.tabs.addTab(page, t(title_key, title))

        # Options tab
        opt = QtWidgets.QWidget()
        of = QtWidgets.QFormLayout(opt)
        self.lang_combo = QtWidgets.QComboBox()
        langs = sorted(n[:-5] for n in os.listdir(os.path.join(self.web_root, "lang")) if n.endswith(".json"))
        self.lang_combo.addItems(langs)
        self.lang_combo.setCurrentText(self.settings["language"])
        self.controller_combo = QtWidgets.QComboBox()
        self.controller_combo.addItem("(any controller)", None)
        names = sorted({d.name for d in core.list_devices() if d.joystick})
        current = self.settings["controller"]
        if current and current not in names:
            names.append(current)
        for n in names:
            self.controller_combo.addItem(n, n)
        if current:
            self.controller_combo.setCurrentIndex(self.controller_combo.findData(current))
        self.lan_check = QtWidgets.QCheckBox("Allow phones / tablets on my network to connect")
        self.lan_check.setChecked(self.settings["host"] not in ("127.0.0.1", "localhost", "::1"))
        self.notify_check = QtWidgets.QCheckBox("Desktop notification when the station changes")
        self.notify_check.setChecked(bool(self.settings["notify"]))
        self.port_spin = QtWidgets.QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(self.settings["port"]))
        of.addRow("Language:", self.lang_combo)
        of.addRow(QtWidgets.QLabel(t("controller", "Controller") + ":"), self.controller_combo)
        of.addRow("", self.lan_check)
        of.addRow("", self.notify_check)
        of.addRow("Port:", self.port_spin)
        folder = QtWidgets.QLabel(f'<a href="file://{core.CONFIG_DIR}">{core.CONFIG_DIR}</a>')
        folder.setOpenExternalLinks(True)
        of.addRow("Settings folder:", folder)
        self.tabs.addTab(opt, "Options")

        # Log tab
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.tabs.addTab(self.log_view, "Log")
        root.addWidget(self.tabs, 1)

        # Save
        brow = QtWidgets.QHBoxLayout()
        brow.addStretch(1)
        self.save_button = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("document-save"), t("save", "Save"))
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        brow.addWidget(self.save_button)
        root.addLayout(brow)

        self.setCentralWidget(central)
        self.server_label = QtWidgets.QLabel()
        self.statusBar().addWidget(self.server_label, 1)
        self.refresh_plugins()
        self.refresh_urls()
        self.resize(560, 700)

    def retranslate(self):
        self.tr_ = Translator(self.web_root, self.settings["language"])
        t = self.tr_
        self.info_box.setTitle(t("info", "Info"))
        self.game_label.setText(t("game", "Game:"))
        self.status_label.setText(t("status", "Status:"))
        self.coord_label.setText(t("coordinates", "Coordinates:"))
        self.url_label.setText(t("url", "URL:"))
        self.open_button.setText(t("open-local-radio", "Open ETS2 Local Radio"))
        self.plugin_box.setTitle(t("install", "Install plugin"))
        self.remove_plugin.setText(t("remove-plugin", "Remove plugin"))
        self.save_button.setText(t("save", "Save"))
        self.tabs.setTabText(0, t("keyboard", "Keyboard"))
        self.tabs.setTabText(1, t("controller", "Controller"))
        for label, tkey, default, kind in self.binding_labels:
            label.setText(self.binding_text(tkey, default, kind))
        self.refresh_plugins()

    def binding_text(self, tkey, default, kind):
        text = self.tr_(tkey, default)
        # Translations only have "... key:" labels; say "button" on the controller tab in English.
        if kind == "button" and self.settings["language"].startswith("en"):
            text = text.replace(" key:", " button:")
        return text

    # -- info / status ------------------------------------------------------

    def refresh_urls(self):
        port = self.settings["port"]
        self.url_combo.clear()
        if self.settings["host"] in ("0.0.0.0", "::"):
            for ip in core.local_ips():
                self.url_combo.addItem(f"http://{ip}:{port}")
        self.url_combo.insertItem(0, f"http://localhost:{port}")
        self.url_combo.setCurrentIndex(0)

    def poll(self):
        t = self.tr_
        if self.link.child_died():
            self.append_log("Server stopped unexpectedly — see the log above. Fix the problem and press Save to restart.")
            self.link.child = None
        data = self.link.api()
        mode = {"child": "Server running in this window (stops when you close it)",
                "service": "Server running as a background service (keeps running after closing)",
                "external": "Using a server started elsewhere"}.get(self.link.mode, "")
        if data is None:
            self.server_label.setText("Server not reachable — see the Log tab")
            self.status_value.setText("—")
            return
        self.server_label.setText(mode)
        pos = data["TruckValues"]["Positioning"]["HeadPositionInWorldSpace"]
        game = {"Ets2": "ets2", "Ats": "ats"}.get(data.get("Game"))
        self.game_value.setText(GAME_NAMES.get(game, "—"))
        if not data.get("SdkActive"):
            self.status_value.setText(t("simulator-not-running", "Simulator not yet running"))
            self.coord_value.setText("—")
        else:
            driving = not data.get("Paused") and any(pos[k] for k in ("X", "Y", "Z"))
            self.status_value.setText(t("simulator-running", "Simulator running!") if driving
                                      else t("simulator-not-driving", "Simulator running, let's get driving!"))
            self.coord_value.setText(f"{pos['X']:.0f}; {pos['Y']:.0f}; {pos['Z']:.0f}")

    def append_log(self, line):
        self.log_view.appendPlainText(line)

    def open_radio(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.url_combo.currentText() + "/"))

    # -- plugin -------------------------------------------------------------

    def refresh_plugins(self):
        t = self.tr_
        for game, b in self.plugin_buttons.items():
            folder = self.games.get(game)
            installed = bool(folder) and core.plugin_installed(folder)
            text = t(f"install-plugin-{game}", f"Install plugin for\n{GAME_NAMES[game]}")
            b.setText(("✓ " if installed else "") + text)  # text mark: icon themes aren't guaranteed
            b.setIcon(QtGui.QIcon.fromTheme("dialog-ok-apply") if installed else QtGui.QIcon())
            b.setToolTip(f"Installed in {folder}" if installed else
                         (f"Found: {folder}" if folder else "Game not found in your Steam libraries — you'll be asked for the folder"))

    def install_plugin(self, game):
        folder = self.games.get(game)
        if not folder:
            folder = QtWidgets.QFileDialog.getExistingDirectory(
                self, f"{GAME_NAMES[game]} folder (the one that contains bin/)", os.path.expanduser("~"))
            if not folder:
                return
        try:
            paths = core.install_plugin(folder)
        except (OSError, FileNotFoundError) as e:
            QtWidgets.QMessageBox.warning(self, "Install plugin", str(e))
            return
        self.games[game] = folder
        self.append_log("installed " + ", ".join(paths))
        self.refresh_plugins()
        QtWidgets.QMessageBox.information(
            self, "Install plugin",
            f"Plugin installed for {GAME_NAMES[game]}.\n\nRestart the game if it is running, and accept the "
            "“Advanced SDK features” dialog when it starts.")

    def uninstall_plugins(self):
        removed = []
        for folder in self.games.values():
            try:
                removed += core.install_plugin(folder, uninstall=True)
            except OSError as e:
                self.append_log(f"remove failed: {e}")
        self.append_log("removed " + (", ".join(removed) or "nothing"))
        self.refresh_plugins()

    # -- bindings -----------------------------------------------------------

    def start_capture(self, button):
        if self.capture:
            self.capture.cancel()
            self.capture = None
        if self.capturing and self.capturing is not button:
            self.capturing.finish()
        self.capturing = button
        if button is not None and button.kind == "button":
            self.capture = ButtonCapture()
            self.capture.pressed.connect(self.on_pad_button)
            self.capture.start()

    def on_pad_button(self, device, index):
        if not self.capturing or self.capturing.kind != "button":
            return
        controller = self.controller_combo.currentData()
        if controller and controller.lower() in device.lower():
            value = str(index)
        else:
            value = f"{device}#{index}"
        self.capturing.finish(value)
        self.capturing = None
        self.capture = None

    def eventFilter(self, obj, ev):
        if self.capturing and ev.type() == QtCore.QEvent.Type.KeyPress:
            if ev.isAutoRepeat():
                return True
            if ev.key() in (QtCore.Qt.Key.Key_Escape, QtCore.Qt.Key.Key_Backspace, QtCore.Qt.Key.Key_Delete):
                self.capturing.finish(clear=True)
            elif self.capturing.kind == "key":
                name = key_name_from_event(ev)
                if not name:
                    return True
                self.capturing.finish(name)
            else:
                return True  # controller box: ignore other keys
            self.capturing = None
            if self.capture:
                self.capture.cancel()
                self.capture = None
            return True
        return super().eventFilter(obj, ev)

    # -- save / close ---------------------------------------------------------

    def save(self):
        if self.capturing:
            self.capturing.finish()
            self.capturing = None
        d = self.settings.data
        for action, b in self.key_buttons.items():
            d["keys"][action] = b.value
        for action, b in self.pad_buttons.items():
            d["buttons"][action] = b.value
        d["language"] = self.lang_combo.currentText()
        d["controller"] = self.controller_combo.currentData()
        d["host"] = "0.0.0.0" if self.lan_check.isChecked() else "127.0.0.1"
        d["notify"] = self.notify_check.isChecked()
        d["port"] = self.port_spin.value()
        try:
            self.settings.save()
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Save", f"Could not save settings:\n{e}")
            return
        self.retranslate()
        self.refresh_urls()
        if not self.link.restart():
            self.append_log("Settings saved. Restart the server you started elsewhere to apply them.")
        else:
            self.statusBar().showMessage("Saved — server restarted", 4000)

    def closeEvent(self, ev):
        if self.capture:
            self.capture.cancel()
        self.link.stop()
        super().closeEvent(ev)


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("ETS2 Local Radio")
    app.setDesktopFileName(core.APP)  # Wayland: match the .desktop entry for the taskbar icon
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

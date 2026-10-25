#!/usr/bin/env python3
"""Front end for keymouse, the keyboard-driven mouse daemon (keymouse.service).

Turns mouse mode on and off, starts/stops the service, sets autostart,
and edits $XDG_CONFIG_HOME/keymouse.conf (the service reloads it on SIGHUP, so
changes apply immediately -- no restart).

State comes from $XDG_RUNTIME_DIR/keymouse.state ("off" / "mouse", removed
when the daemon exits) plus one `systemctl show` call, like vmouse-control.

Key assignments are captured from a real key press: the Qt native scan code
is the evdev code + 8 (xkb keycode) on both Wayland and X11, which is what the
daemon matches against.

Run with --tray to start minimised to the system tray. Clicking the tray
icon toggles mouse mode.
"""
import os
import re
import subprocess
import sys

from evdev import ecodes as e
from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget,
)

UNIT = "keymouse.service"
VERSION = "1.0.0"   # keep in step with keymouse.py
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONF = os.path.join(CONFIG_HOME, "keymouse.conf")
UDEV_DATA = "/run/udev/data"
STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "keymouse.state")

TOUCH_HEIGHT = 42

# (label, toggle_key, toggle_style)
TOGGLES = [
    ("Double-tap Right Shift", "KEY_RIGHTSHIFT", "double-tap"),
    ("Double-tap Left Shift", "KEY_LEFTSHIFT", "double-tap"),
    ("Double-tap Right Ctrl", "KEY_RIGHTCTRL", "double-tap"),
    ("Double-tap Left Ctrl", "KEY_LEFTCTRL", "double-tap"),
    ("Scroll Lock (single press)", "KEY_SCROLLLOCK", "single"),
    ("Pause/Break (single press)", "KEY_PAUSE", "single"),
]

# (config key, label, min, max, step, suffix, default)
NUMBERS = [
    ("double_tap_ms", "Double-tap window", 150, 1000, 50, " ms", 400),
    ("speed", "Starting speed", 20, 3000, 25, " px/s", 250),
    ("max_speed", "Top speed", 50, 6000, 100, " px/s", 1600),
    ("accel_ms", "Time to top speed", 0, 5000, 100, " ms", 900),
    ("slow_percent", "Shift (precise) speed", 5, 100, 5, " %", 25),
    ("scroll_per_sec", "Scroll rate (held)", 1, 60, 1, " /s", 12),
]

# (config key, label, default keys) -- defaults match DEFAULTS in keymouse.py
KEY_ACTIONS = [
    ("move_up_keys", "Move up", ["KEY_I"]),
    ("move_down_keys", "Move down", ["KEY_K"]),
    ("move_left_keys", "Move left", ["KEY_J"]),
    ("move_right_keys", "Move right", ["KEY_L"]),
    ("left_click_keys", "Left click", ["KEY_SPACE", "KEY_F"]),
    ("right_click_keys", "Right click", ["KEY_R", "KEY_COMPOSE"]),
    ("middle_click_keys", "Middle click", ["KEY_M"]),
]
SLOTS = 2   # keys per action

# Keys that can never be assigned (the toggle key is added at run time).
RESERVED = {
    "KEY_UP": "the arrow keys always move the pointer",
    "KEY_DOWN": "the arrow keys always move the pointer",
    "KEY_LEFT": "the arrow keys always move the pointer",
    "KEY_RIGHT": "the arrow keys always move the pointer",
    "KEY_ESC": "Esc always leaves mouse mode",
    **{k: "modifiers pass through for Ctrl/Shift+click" for k in (
        "KEY_LEFTSHIFT", "KEY_RIGHTSHIFT", "KEY_LEFTCTRL", "KEY_RIGHTCTRL",
        "KEY_LEFTALT", "KEY_RIGHTALT", "KEY_LEFTMETA", "KEY_RIGHTMETA")},
}
# Fixed bindings an assignment takes over (mirrors SCROLL/DOUBLE_CLICK/DRAG_LOCK).
FIXED = {
    "KEY_W": "scroll up", "KEY_PAGEUP": "scroll up",
    "KEY_S": "scroll down", "KEY_PAGEDOWN": "scroll down",
    "KEY_A": "scroll left", "KEY_D": "scroll right",
    "KEY_ENTER": "double-click", "KEY_KPENTER": "double-click", "KEY_G": "drag lock",
}
PRETTY = {
    "COMPOSE": "Menu", "SPACE": "Space", "ENTER": "Enter", "KPENTER": "Keypad Enter",
    "BACKSPACE": "Backspace", "TAB": "Tab", "CAPSLOCK": "Caps Lock",
    "PAGEUP": "PageUp", "PAGEDOWN": "PageDown", "SCROLLLOCK": "Scroll Lock",
    "SEMICOLON": ";", "APOSTROPHE": "'", "COMMA": ",", "DOT": ".", "SLASH": "/",
    "BACKSLASH": "\\", "LEFTBRACE": "[", "RIGHTBRACE": "]", "MINUS": "-",
    "EQUAL": "=", "GRAVE": "`", "INSERT": "Insert", "DELETE": "Delete",
    "HOME": "Home", "END": "End", "SYSRQ": "PrtSc", "PAUSE": "Pause",
    "LEFTSHIFT": "Left Shift", "RIGHTSHIFT": "Right Shift", "LEFTCTRL": "Left Ctrl",
    "RIGHTCTRL": "Right Ctrl", "LEFTALT": "Alt", "RIGHTALT": "AltGr",
    "LEFTMETA": "Meta", "RIGHTMETA": "Right Meta",
}


def key_label(name):
    """KEY_SPACE -> Space, KEY_KP5 -> Keypad 5, KEY_F -> F."""
    bare = name[4:] if name.startswith("KEY_") else name
    if bare in PRETTY:
        return PRETTY[bare]
    if bare.startswith("KP") and len(bare) > 2:
        return "Keypad " + bare[2:].title()
    return bare if len(bare) == 1 or re.fullmatch(r"F\d+", bare) else bare.title()


def evdev_name(code):
    names = e.KEY.get(code)
    if isinstance(names, (tuple, list)):     # aliases, e.g. MIN_INTERESTING/MUTE
        names = [n for n in names if "MIN_INTERESTING" not in n][-1]
    return names


def join_keys(names):
    return " or ".join(key_label(n) for n in names) or "<i>(none)</i>"


def keymap_html(assigned):
    """The cheat-sheet, built from the current assignments."""
    a = {k: join_keys(v) for k, v in assigned.items()}
    left = a["left_click_keys"]
    gone = {FIXED[n] for keys in assigned.values() for n in keys if n in FIXED}
    rows = [
        ("Move", "Arrow keys, or {} / {} / {} / {} (up/down/left/right) &nbsp;"
                 "(hold to speed up; hold Shift for slow, precise moves)".format(
                     a["move_up_keys"], a["move_down_keys"],
                     a["move_left_keys"], a["move_right_keys"])),
        ("Left click", f"{left} &nbsp;(hold to drag)"),
        ("Right click", a["right_click_keys"]),
        ("Middle click", a["middle_click_keys"]),
    ]
    if "double-click" not in gone:
        rows.append(("Double-click", "Enter"))
    if "drag lock" not in gone:
        rows.append(("Drag lock", f"G &nbsp;(button stays down until G or {left})"))
    used = {n for keys in assigned.values() for n in keys}
    free = lambda *keys: not used & set(keys)
    scroll = [lbl for lbl, keys in (("W / S", ("KEY_W", "KEY_S")),
                                    ("PageUp / PageDown", ("KEY_PAGEUP", "KEY_PAGEDOWN")))
              if free(*keys)]
    side = free("KEY_A", "KEY_D")
    if scroll or side:
        rows.append(("Scroll", " or ".join(scroll) + ("; A / D sideways" if side else "")))
    rows.append(("Leave", "Esc, or the toggle again"))
    body = "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows)
    return (f'<table cellspacing="0" cellpadding="3">{body}</table>'
            "<p>Shift, Ctrl, Alt and Meta still work, so Ctrl+click and Shift+click "
            "work. Every other key is ignored while mouse mode is on.</p>")


class KeyButton(QPushButton):
    """Tap, then press a key: emits the key's evdev name."""
    captured = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.capturing = False
        self.name = None
        self.setMinimumHeight(TOUCH_HEIGHT)
        self.setMinimumWidth(96)

    def set_key(self, name):
        self.name = name
        self.capturing = False
        self.setText(key_label(name) if name else "—")

    def start_capture(self):
        self.capturing = True
        self.setText("Press a key…")
        self.setFocus()
        self.grabKeyboard()

    def stop_capture(self):
        if self.capturing:
            self.releaseKeyboard()
            self.set_key(self.name)

    def event(self, ev):
        # Tab/Backtab never reach keyPressEvent otherwise (focus navigation).
        if self.capturing and ev.type() == QEvent.Type.KeyPress:
            self.keyPressEvent(ev)
            return True
        return super().event(ev)

    def keyPressEvent(self, ev):
        if not self.capturing:
            return super().keyPressEvent(ev)
        ev.accept()
        if ev.isAutoRepeat():
            return
        if ev.key() == Qt.Key.Key_Escape:
            self.stop_capture()
            self.cancelled.emit()
            return
        name = evdev_name(ev.nativeScanCode() - 8) if ev.nativeScanCode() > 8 else None
        self.stop_capture()
        if name:
            self.captured.emit(name)
        else:
            self.cancelled.emit()

    def keyReleaseEvent(self, ev):
        if self.capturing:
            ev.accept()
        else:
            super().keyReleaseEvent(ev)

    def focusOutEvent(self, ev):
        self.stop_capture()
        super().focusOutEvent(ev)


def run(args, timeout=10):
    """Run a command, returning (rc, stdout). Never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def systemctl(*args):
    return run(["systemctl", "--user", *args])


def unit_state():
    """Return (active, autostart_enabled) in a single systemctl call."""
    _, out = systemctl("show", UNIT, "-p", "ActiveState", "-p", "UnitFileState")
    fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return (
        fields.get("ActiveState") == "active",
        fields.get("UnitFileState") == "enabled",
    )


def current_mode():
    try:
        with open(STATE) as f:
            value = f.read().strip()
        return value if value in ("off", "mouse") else None
    except OSError:
        return None


def read_conf():
    values = {}
    try:
        with open(CONF) as f:
            for m in re.finditer(r"^\s*(\w+)\s*=\s*(.*?)\s*$", f.read(), re.M):
                values[m.group(1)] = m.group(2)
    except OSError:
        pass
    return values


def write_conf(updates):
    """Rewrite keys in place, preserving comments and everything else."""
    try:
        with open(CONF) as f:
            text = f.read()
    except OSError:
        text = "[keymouse]\n"
    for key, value in updates.items():
        text, count = re.subn(rf"^\s*{key}\s*=.*$", f"{key} = {value}", text, flags=re.M)
        if count == 0:
            text = text.rstrip("\n") + f"\n{key} = {value}\n"
    try:
        with open(CONF, "w") as f:
            f.write(text)
    except OSError:
        return False
    return True


def access_problems():
    """What stops the daemon working on this machine, as plain sentences.

    Keyboards are recognised from udev's database (ID_INPUT_KEYBOARD), so
    nothing is opened -- this is cheap enough to run on every Start.
    """
    problems = []
    if not os.access("/dev/uinput", os.W_OK):
        problems.append("cannot create the virtual mouse (/dev/uinput)")
    keyboards = readable = 0
    try:
        nodes = [n for n in os.listdir("/dev/input") if n.startswith("event")]
    except OSError:
        nodes = []
    for node in nodes:
        path = os.path.join("/dev/input", node)
        try:
            rdev = os.stat(path).st_rdev
            with open(f"{UDEV_DATA}/c{os.major(rdev)}:{os.minor(rdev)}") as f:
                if "E:ID_INPUT_KEYBOARD=1" not in f.read():
                    continue
        except OSError:
            continue
        keyboards += 1
        readable += os.access(path, os.R_OK)
    if keyboards and not readable:
        problems.append("cannot read the keyboard")
    return problems


def muted(label):
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(placeholder-text);")
    return label


class KeyMouseControl(QMainWindow):
    def __init__(self, start_in_tray=False):
        super().__init__()
        self.setWindowTitle("Keyboard Mouse")
        self.setWindowIcon(QIcon.fromTheme("input-mouse"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.setCentralWidget(scroll)
        central = QWidget()
        scroll.setWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        # ---- status + toggle ----------------------------------------
        status_box = QGroupBox("Status")
        sl = QVBoxLayout(status_box)
        self.mode_label = QLabel("…")
        f = QFont()
        f.setPointSize(15)
        f.setBold(True)
        self.mode_label.setFont(f)
        sl.addWidget(self.mode_label)
        self.detail_label = muted(QLabel("…"))
        sl.addWidget(self.detail_label)
        self.access_label = QLabel()
        self.access_label.setWordWrap(True)
        self.access_label.setTextFormat(Qt.TextFormat.RichText)
        self.access_label.setStyleSheet(
            "background: palette(alternate-base); border-left: 4px solid #d9822b;"
            " padding: 8px;")
        self.access_label.hide()
        sl.addWidget(self.access_label)

        self.toggle_btn = QPushButton("Turn Mouse Mode On")
        self.toggle_btn.setMinimumHeight(TOUCH_HEIGHT + 8)
        tf = QFont()
        tf.setPointSize(12)
        tf.setBold(True)
        self.toggle_btn.setFont(tf)
        self.toggle_btn.clicked.connect(self.on_toggle)
        sl.addWidget(self.toggle_btn)
        root.addWidget(status_box)

        # ---- key map ------------------------------------------------
        map_box = QGroupBox("Keys in mouse mode")
        ml = QVBoxLayout(map_box)
        self.map_label = QLabel()
        self.map_label.setWordWrap(True)
        ml.addWidget(self.map_label)
        root.addWidget(map_box)

        # ---- key assignments ----------------------------------------
        keys_box = QGroupBox("Key assignments")
        kl = QVBoxLayout(keys_box)
        kl.addWidget(muted(QLabel(
            "Tap a box, then press the key you want (Esc cancels). Each action "
            "can have two keys; ✕ clears one. The arrow keys always move the "
            "pointer as well. Mouse mode must be off while you assign keys.")))
        kform = QFormLayout()
        self.key_buttons = {}       # config key -> [KeyButton, ...]
        for opt, label, _ in KEY_ACTIONS:
            row = QHBoxLayout()
            slots = []
            for i in range(SLOTS):
                b = KeyButton()
                b.clicked.connect(lambda _=False, o=opt, n=i: self.on_key_slot(o, n))
                b.captured.connect(lambda name, o=opt, n=i: self.assign_key(o, n, name))
                b.cancelled.connect(lambda: self.key_status.setText(""))
                clear = QPushButton("✕")
                clear.setToolTip("Clear this key")
                clear.setFixedWidth(TOUCH_HEIGHT)
                clear.setMinimumHeight(TOUCH_HEIGHT)
                clear.clicked.connect(lambda _=False, o=opt, n=i: self.assign_key(o, n, None))
                row.addWidget(b, 1)
                row.addWidget(clear)
                slots.append(b)
            self.key_buttons[opt] = slots
            kform.addRow(label, row)
        kl.addLayout(kform)
        self.key_status = muted(QLabel(""))
        kl.addWidget(self.key_status)
        reset = QPushButton("Restore default keys")
        reset.setMinimumHeight(TOUCH_HEIGHT)
        reset.clicked.connect(self.on_reset_keys)
        kl.addWidget(reset)
        root.addWidget(keys_box)

        # ---- settings -----------------------------------------------
        set_box = QGroupBox("Settings (applied immediately)")
        form = QFormLayout(set_box)

        self.toggle_combo = QComboBox()
        self.toggle_combo.setMinimumHeight(TOUCH_HEIGHT)
        for label, _, _ in TOGGLES:
            self.toggle_combo.addItem(label)
        self.toggle_combo.activated.connect(self.on_settings_changed)
        form.addRow("Turn on/off with", self.toggle_combo)

        self.spins = {}
        for key, label, lo, hi, step, suffix, _ in NUMBERS:
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(step)
            sp.setSuffix(suffix)
            sp.setMinimumHeight(TOUCH_HEIGHT)
            sp.editingFinished.connect(self.on_settings_changed)
            self.spins[key] = sp
            form.addRow(label, sp)

        form.addRow(muted(QLabel(
            "The toggle is read straight from the keyboard hardware, so it works "
            "even when a Citrix or other remote desktop window has the keyboard "
            "captured. A double-tap only counts if nothing else is pressed in "
            "between, so ordinary typing will not trigger it."
        )))
        root.addWidget(set_box)

        # ---- notifications ------------------------------------------
        note_box = QGroupBox("Notifications")
        nl = QVBoxLayout(note_box)
        self.osd_cb = QCheckBox("Show on-screen notifications")
        self.osd_cb.setMinimumHeight(TOUCH_HEIGHT)
        self.osd_cb.toggled.connect(self.on_settings_changed)
        nl.addWidget(self.osd_cb)
        nl.addWidget(muted(QLabel(
            "The message in the middle of the screen when mouse mode turns on "
            "or off. Turning this off hides it; the tray icon still shows the "
            "current mode.")))
        root.addWidget(note_box)

        # ---- service ------------------------------------------------
        svc_box = QGroupBox("Service")
        vl = QVBoxLayout(svc_box)
        row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setMinimumHeight(TOUCH_HEIGHT)
        self.start_btn.clicked.connect(lambda: self.service("start"))
        row.addWidget(self.start_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setMinimumHeight(TOUCH_HEIGHT)
        self.stop_btn.clicked.connect(lambda: self.service("stop"))
        row.addWidget(self.stop_btn)
        vl.addLayout(row)
        self.autostart_cb = QCheckBox("Start automatically at login")
        self.autostart_cb.setMinimumHeight(TOUCH_HEIGHT)
        self.autostart_cb.toggled.connect(self.on_autostart)
        vl.addWidget(self.autostart_cb)
        vl.addWidget(muted(QLabel(
            "The service must be running for the keyboard toggle to work. It "
            "always starts with mouse mode off, turns mouse mode off while the "
            "screen is locked, and lets go of the keyboard if it ever hangs."
        )))
        root.addWidget(svc_box)
        root.addStretch(1)

        # ---- tray ---------------------------------------------------
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(QIcon.fromTheme("input-keyboard"), self)
            menu = QMenu()
            act = QAction("Toggle mouse mode", self)
            act.triggered.connect(self.on_toggle)
            menu.addAction(act)
            menu.addSeparator()
            show = QAction("Open Keyboard Mouse settings", self)
            show.triggered.connect(self.show_window)
            menu.addAction(show)
            quit_action = QAction("Quit this window", self)
            quit_action.triggered.connect(QApplication.quit)
            menu.addAction(quit_action)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self.on_tray_activated)
            self.tray.show()

        self.load_settings()
        self.check_access()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

        self.resize(540, 860)
        if not start_in_tray:
            self.show()

    # -- settings -----------------------------------------------------
    def load_settings(self):
        self._loading = True
        conf = read_conf()
        key = conf.get("toggle_key", "KEY_RIGHTSHIFT").upper()
        style = conf.get("toggle_style", "double-tap").lower()
        idx = next((i for i, t in enumerate(TOGGLES) if t[1] == key and t[2] == style), None)
        if idx is None:     # a hand-edited value: show it rather than lose it
            TOGGLES.append((f"{key} ({style})", key, style))
            self.toggle_combo.addItem(TOGGLES[-1][0])
            idx = len(TOGGLES) - 1
        self.toggle_combo.setCurrentIndex(idx)
        for k, *_, default in NUMBERS:
            try:
                self.spins[k].setValue(int(float(conf.get(k, default))))
            except ValueError:
                self.spins[k].setValue(default)
        self.osd_cb.setChecked(conf.get("osd", "1") != "0")
        for opt, _, default in KEY_ACTIONS:
            names = (conf[opt].replace(",", " ").upper().split()
                     if opt in conf else list(default))
            for i, b in enumerate(self.key_buttons[opt]):
                b.set_key(names[i] if i < len(names) else None)
        self.update_keymap()
        self._loading = False

    # -- key assignments ----------------------------------------------
    def assigned(self):
        return {opt: [b.name for b in slots if b.name]
                for opt, slots in self.key_buttons.items()}

    def update_keymap(self):
        self.map_label.setText(keymap_html(self.assigned()))

    def on_key_slot(self, opt, slot):
        for slots in self.key_buttons.values():
            for b in slots:
                b.stop_capture()
        if current_mode() == "mouse":
            self.key_status.setText(
                "Turn mouse mode off first — while it is on, the keyboard is "
                "captured and this window cannot see your key press.")
            return
        self.key_status.setText("Press the key for "
                                f"{dict((o, l) for o, l, _ in KEY_ACTIONS)[opt].lower()}…")
        self.key_buttons[opt][slot].start_capture()

    def assign_key(self, opt, slot, name):
        labels = {o: l for o, l, _ in KEY_ACTIONS}
        notes = []
        if name:
            toggle = TOGGLES[self.toggle_combo.currentIndex()][1]
            why = RESERVED.get(name) or (
                "it turns mouse mode on/off" if name == toggle else None)
            if why:
                self.key_status.setText(f"{key_label(name)} can't be used: {why}.")
                return
            for o, slots in self.key_buttons.items():
                for i, b in enumerate(slots):
                    if b.name == name and (o, i) != (opt, slot):
                        b.set_key(None)
                        if o != opt:
                            notes.append(f"removed from {labels[o].lower()}")
            if name in FIXED:
                notes.append(f"it no longer does {FIXED[name]}")
        self.key_buttons[opt][slot].set_key(name)
        what = f"{key_label(name)} → {labels[opt].lower()}" if name else \
            f"Cleared a key from {labels[opt].lower()}"
        self.key_status.setText(what + (f" ({'; '.join(notes)})" if notes else "") + ".")
        self.save_keys()

    def on_reset_keys(self):
        for opt, _, default in KEY_ACTIONS:
            for i, b in enumerate(self.key_buttons[opt]):
                b.set_key(default[i] if i < len(default) else None)
        self.key_status.setText("Default keys restored.")
        self.save_keys()

    def save_keys(self):
        self.update_keymap()
        updates = {opt: " ".join(names) for opt, names in self.assigned().items()}
        if not write_conf(updates):
            self.warn(f"Could not write {CONF}.")
            return
        systemctl("kill", "-s", "HUP", UNIT)

    def on_settings_changed(self, *_):
        if getattr(self, "_loading", False):
            return
        _, key, style = TOGGLES[self.toggle_combo.currentIndex()]
        updates = {"toggle_key": key, "toggle_style": style,
                   "osd": "1" if self.osd_cb.isChecked() else "0"}
        for k, sp in self.spins.items():
            updates[k] = str(sp.value())
        conf = read_conf()
        if all(conf.get(k) == v for k, v in updates.items()):
            return
        if not write_conf(updates):
            self.warn(f"Could not write {CONF}.")
            return
        systemctl("kill", "-s", "HUP", UNIT)

    def check_access(self):
        problems = access_problems()
        if problems:
            self.access_label.setText(
                "<b>Keyboard access is not set up on this computer</b> — keymouse "
                f"{' and '.join(problems)}.<br>Install it from the package or "
                "<code>sudo ./install.sh</code> (both add the udev rule "
                "<code>70-keymouse.rules</code>), then log out and back in.")
        self.access_label.setVisible(bool(problems))
        return not problems

    # -- actions ------------------------------------------------------
    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.on_toggle()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def on_toggle(self):
        self.check_access()
        active, _ = unit_state()
        if not active:
            rc, _ = systemctl("start", UNIT)
            if rc != 0:
                self.warn(f"Could not start the service.\n\njournalctl --user -u {UNIT}")
                return
            # Turn mouse mode on once it is up (it always starts off).
            QTimer.singleShot(1500, lambda: systemctl("kill", "-s", "USR1", UNIT))
        else:
            systemctl("kill", "-s", "USR1", UNIT)
        QTimer.singleShot(600, self.refresh)
        QTimer.singleShot(2000, self.refresh)

    def service(self, verb):
        self.check_access()
        rc, _ = systemctl(verb, UNIT)
        if rc != 0:
            self.warn(f"Could not {verb} the service.\n\njournalctl --user -u {UNIT}")
        QTimer.singleShot(500, self.refresh)

    def on_autostart(self, checked):
        if getattr(self, "_syncing", False):
            return
        rc, _ = systemctl("enable" if checked else "disable", UNIT)
        if rc != 0:
            self.warn("Could not change the autostart setting.")
        QTimer.singleShot(200, self.refresh)

    def warn(self, text):
        QMessageBox.warning(self, "Keyboard Mouse", text)

    # -- state --------------------------------------------------------
    def refresh(self):
        active, enabled = unit_state()
        mode = current_mode() if active else None
        toggle_label = TOGGLES[self.toggle_combo.currentIndex()][0]

        if not active:
            self.mode_label.setText("Not running")
            self.detail_label.setText(
                "The keyboard toggle does nothing until the service runs. "
                "Turn Mouse Mode On starts it.")
            self.toggle_btn.setText("Turn Mouse Mode On")
        elif mode == "mouse":
            self.mode_label.setText("Mouse mode ON")
            self.detail_label.setText(
                "The keyboard is driving the pointer. Press Esc or "
                f"{toggle_label.lower()} to type again.")
            self.toggle_btn.setText("Turn Mouse Mode Off")
        else:
            self.mode_label.setText("Ready (mouse mode off)")
            self.detail_label.setText(
                f"Keyboard types normally. {toggle_label} to start moving the pointer.")
            self.toggle_btn.setText("Turn Mouse Mode On")

        self.start_btn.setEnabled(not active)
        self.stop_btn.setEnabled(active)
        self._syncing = True
        self.autostart_cb.setChecked(enabled)
        self._syncing = False

        if self.tray:
            self.tray.setIcon(QIcon.fromTheme(
                "input-mouse" if mode == "mouse" else "input-keyboard"))
            self.tray.setToolTip(f"Keyboard Mouse: {self.mode_label.text()}")


def main():
    if "--version" in sys.argv:
        print(VERSION)
        return
    app = QApplication(sys.argv)
    app.setApplicationName("Keyboard Mouse")
    app.setDesktopFileName("keymouse-control")
    tray_mode = "--tray" in sys.argv
    app.setQuitOnLastWindowClosed(not tray_mode)
    window = KeyMouseControl(start_in_tray=tray_mode)
    if tray_mode and not QSystemTrayIcon.isSystemTrayAvailable():
        window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

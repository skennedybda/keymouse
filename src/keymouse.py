#!/usr/bin/env python3
"""keymouse -- drive the mouse pointer from the keyboard.

Works *below* the compositor, which is what makes it survive remote desktop
clients such as Citrix Workspace:

  * Keys are read straight from the kernel (evdev). A Citrix session that has
    grabbed the keyboard -- so KDE global shortcuts never fire -- cannot hide
    the toggle gesture from us, because we see the key before KWin does.
  * Pointer output is a kernel uinput mouse ("keymouse pointer"). KWin adopts
    it like any USB mouse, so wfica (and every other app, local or remote)
    receives perfectly ordinary pointer motion and button clicks. Nothing is
    synthesised at the X11/Wayland level that a remote client could ignore.

Mouse mode
  The default toggle is a double-tap of Right Shift (configurable). While
  mouse mode is on, every keyboard is EVIOCGRAB'd so the mapped keys do not
  also type into the focused app. Modifiers (Shift/Ctrl/Alt/Meta) are passed
  through on a virtual keyboard so Ctrl+click, Shift+click etc. still work.
  Every other unmapped key is swallowed.

    Move ............ arrow keys (always)  and  I J K L   (hold to accelerate,
                      hold Shift for slow/precise movement)
    Left button ..... Space  or  F   (hold to drag)
    Right button .... R  or  the Menu key
    Middle button ... M

  Move/click keys other than the arrows are configurable (move_*_keys and
  *_click_keys in keymouse.conf; the values above are the defaults). A key
  assigned there takes over from the scroll, double-click or drag-lock key of
  the same name. Arrows, Esc, modifiers and the toggle key cannot be assigned.
    Double-click .... Enter
    Drag lock ....... G   (left button stays down until G/Space/F)
    Scroll .......... W / S  or  PageUp / PageDown;  A / D sideways
    Leave ........... Esc, or the toggle gesture again

Control: SIGUSR1 toggles, SIGUSR2 forces mouse mode off, SIGHUP reloads
$XDG_CONFIG_HOME/keymouse.conf (~/.config), SIGTERM exits cleanly
(releasing every grab).

Desktop support: anything with systemd and a Wayland or X11 session. D-Bus
is spoken through busctl (part of systemd), so no desktop-specific tools are
needed. On KDE Plasma the mode message is a Plasma OSD and the pointer gets a
flat acceleration profile; elsewhere a standard desktop notification is used
and the compositor's own acceleration applies.

Permissions: the user needs read access to /dev/input/event* keyboards and
write access to /dev/uinput -- the packaged udev rule (70-keymouse.rules)
grants both to the logged-in seat user.
State: $XDG_RUNTIME_DIR/keymouse.state holds "off" or "mouse" while running.

Safeguards:
  * Mouse mode is dropped while the screen is locked, and cannot be entered
    while it is locked -- a grabbed keyboard would make the password field
    unreachable. The lock is read from logind's LockedHint (set by KDE,
    GNOME and most lockers) and from org.freedesktop.ScreenSaver.
  * A watchdog thread releases the grabs if the main loop stalls.
  * The grab is only taken once no key is physically held, so the compositor
    can never be left with a stuck key it saw pressed but never released.
"""
import configparser
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time

import evdev
from evdev import ecodes as e

__version__ = "1.0.0"

CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONF = os.path.join(CONFIG_HOME, "keymouse.conf")
RUNTIME = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
STATE = os.path.join(RUNTIME, "keymouse.state")

POINTER_NAME = "keymouse pointer"
PASSTHRU_NAME = "keymouse passthrough"
OWN_NAMES = {POINTER_NAME, PASSTHRU_NAME}

DEFAULTS = {
    "toggle_key": "KEY_RIGHTSHIFT",
    "toggle_style": "double-tap",      # double-tap | single
    "double_tap_ms": "400",
    "speed": "250",                    # px/s when a move key is first held
    "max_speed": "1600",               # px/s after full acceleration
    "accel_ms": "900",                 # time to reach max_speed
    "slow_percent": "25",              # speed while Shift is held
    "scroll_per_sec": "12",            # wheel notches/s while a scroll key is held
    "flat_accel": "1",                 # ask KWin for a flat profile on our pointer
    "osd": "1",                        # show a Plasma OSD on mode change
    "watchdog_secs": "6",
    # Key assignments: evdev KEY_* names, space- or comma-separated.
    "move_up_keys": "KEY_I",
    "move_down_keys": "KEY_K",
    "move_left_keys": "KEY_J",
    "move_right_keys": "KEY_L",
    "left_click_keys": "KEY_SPACE KEY_F",
    "right_click_keys": "KEY_R KEY_COMPOSE",
    "middle_click_keys": "KEY_M",
}

# config key -> what it does: a (dx, dy) move or a mouse button
ASSIGNABLE = {
    "move_up_keys": ("move", (0, -1)),
    "move_down_keys": ("move", (0, 1)),
    "move_left_keys": ("move", (-1, 0)),
    "move_right_keys": ("move", (1, 0)),
    "left_click_keys": ("button", e.BTN_LEFT),
    "right_click_keys": ("button", e.BTN_RIGHT),
    "middle_click_keys": ("button", e.BTN_MIDDLE),
}

# The arrows always move the pointer, whatever else is assigned.
ARROWS = {
    e.KEY_UP: (0, -1), e.KEY_DOWN: (0, 1), e.KEY_LEFT: (-1, 0), e.KEY_RIGHT: (1, 0),
}
SCROLL = {
    e.KEY_W: (e.REL_WHEEL, 1), e.KEY_PAGEUP: (e.REL_WHEEL, 1),
    e.KEY_S: (e.REL_WHEEL, -1), e.KEY_PAGEDOWN: (e.REL_WHEEL, -1),
    e.KEY_A: (e.REL_HWHEEL, -1), e.KEY_D: (e.REL_HWHEEL, 1),
}
DOUBLE_CLICK = {e.KEY_ENTER, e.KEY_KPENTER}
DRAG_LOCK = {e.KEY_G}
EXIT = {e.KEY_ESC}
MODIFIERS = {
    e.KEY_LEFTSHIFT, e.KEY_RIGHTSHIFT, e.KEY_LEFTCTRL, e.KEY_RIGHTCTRL,
    e.KEY_LEFTALT, e.KEY_RIGHTALT, e.KEY_LEFTMETA, e.KEY_RIGHTMETA,
}
SHIFTS = {e.KEY_LEFTSHIFT, e.KEY_RIGHTSHIFT}
RESERVED = set(ARROWS) | EXIT | MODIFIERS

TICK = 1 / 125          # motion update rate while a key is held
IDLE_TIMEOUT = 0.5      # main loop wakes at least this often (watchdog heartbeat)
RESCAN_SECS = 2.0       # look for hot-plugged keyboards
LOCK_POLL_SECS = 1.0
GRAB_WAIT_SECS = 3.0    # how long to wait for held keys to be released
CLEAN_TAP_SECS = 0.35   # a toggle tap must be shorter than this


def sd_notify(msg):
    """Tell systemd we are alive (Type=notify + WatchdogSec in the unit).

    The in-process watchdog cannot help if the whole process freezes; the
    systemd watchdog then kills it, and the kernel drops every grab with it.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


def log(msg):
    print(f"keymouse: {msg}", flush=True)


def load_config():
    cp = configparser.ConfigParser()
    cp.read_dict({"keymouse": DEFAULTS})
    try:
        cp.read(CONF)
    except configparser.Error as exc:
        log(f"bad config, using defaults: {exc}")
    s = cp["keymouse"]

    def num(key, cast=int):
        try:
            return cast(s.get(key))
        except (TypeError, ValueError):
            return cast(DEFAULTS[key])

    key_name = s.get("toggle_key", DEFAULTS["toggle_key"]).strip().upper()
    toggle_key = e.ecodes.get(key_name)
    if not isinstance(toggle_key, int):
        log(f"unknown toggle_key {key_name!r}, using {DEFAULTS['toggle_key']}")
        toggle_key = e.ecodes[DEFAULTS["toggle_key"]]
    keymaps = build_keymaps(s, toggle_key)
    return {
        **keymaps,
        "toggle_key": toggle_key,
        "toggle_style": s.get("toggle_style", "double-tap").strip().lower(),
        "double_tap": max(0.1, num("double_tap_ms") / 1000),
        "speed": max(10.0, num("speed", float)),
        "max_speed": max(10.0, num("max_speed", float)),
        "accel": max(0.0, num("accel_ms") / 1000),
        "slow": min(1.0, max(0.02, num("slow_percent") / 100)),
        "scroll_rate": max(1.0, num("scroll_per_sec", float)),
        "flat_accel": num("flat_accel") != 0,
        "osd": num("osd") != 0,
        "watchdog": max(3.0, num("watchdog_secs", float)),
    }


def parse_keys(value):
    """'KEY_SPACE, KEY_F' -> [57, 33]; unknown names are logged and skipped."""
    codes = []
    for name in value.replace(",", " ").upper().split():
        code = e.ecodes.get(name)
        if isinstance(code, int) and name.startswith("KEY_"):
            codes.append(code)
        else:
            log(f"unknown key name {name!r} ignored")
    return codes


def build_keymaps(s, toggle_key):
    """Build the move/button/scroll/... tables from the key assignments.

    An assigned key takes over from a fixed scroll/double-click/drag-lock
    binding on the same key; the first action to claim a key keeps it.
    """
    move, buttons, claimed = dict(ARROWS), {}, {}
    for opt, (kind, what) in ASSIGNABLE.items():
        for code in parse_keys(s.get(opt, DEFAULTS[opt])):
            name = e.KEY.get(code, code)
            if code in RESERVED or code == toggle_key:
                log(f"{opt}: {name} is reserved, ignored")
                continue
            if code in claimed:
                log(f"{opt}: {name} already used by {claimed[code]}, ignored")
                continue
            claimed[code] = opt
            (move if kind == "move" else buttons)[code] = what
    return {
        "move": move,
        "buttons": buttons,
        "scroll": {k: v for k, v in SCROLL.items() if k not in claimed},
        "double_click": DOUBLE_CLICK - set(claimed),
        "drag_lock": DRAG_LOCK - set(claimed),
    }


def is_keyboard(dev):
    if dev.name in OWN_NAMES:
        return False
    keys = set(dev.capabilities().get(e.EV_KEY, []))
    return {e.KEY_A, e.KEY_Z, e.KEY_SPACE} <= keys


def write_state(value):
    tmp = STATE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(value + "\n")
        os.replace(tmp, STATE)
    except OSError:
        pass


def busctl(*args, system=False, timeout=1.5):
    """Run busctl; return stdout, or None if it failed or is missing."""
    cmd = ["busctl", "--system" if system else "--user", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def has_bus_name(name):
    return busctl("status", name) is not None


def parse_bool(out):
    """busctl prints a boolean as 'b true' / 'b false'."""
    return {"b true": True, "b false": False}.get(out)


LOGIN1 = "org.freedesktop.login1"
_session_path = None


def logind_locked():
    """LockedHint of this user's graphical session (desktop-neutral)."""
    global _session_path
    if _session_path is None:
        out = busctl("get-property", LOGIN1, "/org/freedesktop/login1/user/self",
                     "org.freedesktop.login1.User", "Display", system=True)
        # '(so) "3" "/org/freedesktop/login1/session/_33"'
        parts = out.split('"') if out else []
        if len(parts) < 4 or not parts[3].startswith("/"):
            return None
        _session_path = parts[3]
    value = parse_bool(busctl("get-property", LOGIN1, _session_path,
                              "org.freedesktop.login1.Session", "LockedHint",
                              system=True))
    if value is None:
        _session_path = None    # session went away; look it up again next time
    return value


def screensaver_locked():
    return parse_bool(busctl("call", "org.freedesktop.ScreenSaver", "/ScreenSaver",
                             "org.freedesktop.ScreenSaver", "GetActive"))


def session_locked():
    """True if any source says locked; False if one answered; None if none did."""
    answers = [logind_locked(), screensaver_locked()]
    if True in answers:
        return True
    return False if False in answers else None


def osd(text, icon="input-mouse"):
    """Plasma OSD where available, otherwise a transient desktop notification."""
    def show():
        if has_bus_name("org.kde.plasmashell"):
            if busctl("call", "org.kde.plasmashell", "/org/kde/osdService",
                      "org.kde.osdService", "showText", "ss", icon, text) is not None:
                return
        busctl("call", "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
               "org.freedesktop.Notifications", "Notify", "susssasa{sv}i",
               "Keyboard Mouse", "0", icon, text, "", "0",
               "1", "transient", "b", "true", "1500")
    threading.Thread(target=show, daemon=True).start()


def set_flat_accel(event_path):
    """KWin only, best effort: its adaptive profile would distort our speeds."""
    if not has_bus_name("org.kde.KWin"):
        return
    node = os.path.basename(event_path)
    obj = f"/org/kde/KWin/InputDevice/{node}"
    for _ in range(20):
        if busctl("set-property", "org.kde.KWin", obj, "org.kde.KWin.InputDevice",
                  "pointerAccelerationProfileFlat", "b", "true", timeout=3) is not None:
            busctl("set-property", "org.kde.KWin", obj, "org.kde.KWin.InputDevice",
                   "pointerAcceleration", "d", "0", timeout=3)
            log(f"flat pointer acceleration set on {node}")
            return
        time.sleep(0.5)
    log(f"could not set flat acceleration on {node} (KWin never adopted it?)")


PERMISSION_HINT = (
    "keymouse needs read access to keyboards (/dev/input/event*) and write "
    "access to /dev/uinput. Install the udev rule (70-keymouse.rules, part of "
    "the package), then log out and back in.")


class KeyMouse:
    def __init__(self):
        self.cfg = load_config()
        self.denied = set()          # keyboard nodes we could not open
        self.sel = selectors.DefaultSelector()
        self.devices = {}            # path -> InputDevice
        self.mouse_mode = False
        self.grabbed = False
        self.pending_enter = None    # deadline while waiting for keys to lift
        self.heartbeat = time.monotonic()
        self.lock = threading.Lock()  # guards grab/ungrab against the watchdog

        try:
            self.make_uinput()
        except (OSError, evdev.UInputError) as exc:
            log(f"cannot create virtual devices: {exc}")
            log(PERMISSION_HINT)
            raise SystemExit(78)     # EX_CONFIG: the unit does not restart-loop on it
        self.finish_init()

    def make_uinput(self):
        self.pointer = evdev.UInput(
            {
                e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
                e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL],
            },
            name=POINTER_NAME, vendor=0x1209, product=0x4b4d, version=1,
        )
        self.passthru = evdev.UInput(
            {e.EV_KEY: sorted(MODIFIERS)},
            name=PASSTHRU_NAME, vendor=0x1209, product=0x4b4e, version=1,
        )
        log(f"pointer {self.pointer.device.path}, passthrough {self.passthru.device.path}")

    def finish_init(self):
        if self.cfg["flat_accel"]:
            threading.Thread(target=set_flat_accel, args=(self.pointer.device.path,),
                             daemon=True).start()

        self.reset_input_state()
        # Toggle-gesture tracking, live in both modes.
        self.tap_down_at = None
        self.tap_clean = False
        self.last_tap = 0.0

        self.next_rescan = 0.0
        self.next_lock_poll = 0.0
        self.signal_r, self.signal_w = os.pipe()
        os.set_blocking(self.signal_w, False)
        self.sel.register(self.signal_r, selectors.EVENT_READ, "signal")
        self.pending_signals = []

    def reset_input_state(self):
        self.move_held = {}          # keycode -> (dx, dy)
        self.move_started = None
        self.frac = [0.0, 0.0]
        self.last_tick = None
        self.button_keys = {}        # keycode -> button currently held by it
        self.buttons_down = set()
        self.drag_locked = False
        self.scroll_held = {}        # keycode -> (axis, dir)
        self.scroll_next = 0.0
        self.passthru_down = set()

    # ---- devices ------------------------------------------------------
    def rescan(self):
        present = set(evdev.list_devices())
        for path in list(self.devices):
            if path not in present:
                self.drop_device(path, "removed")
        for path in present - set(self.devices):
            try:
                dev = evdev.InputDevice(path)
            except PermissionError:
                if path not in self.denied:
                    self.denied.add(path)
                    log(f"no permission to read {path}")
                continue
            except OSError:
                continue
            if not is_keyboard(dev):
                dev.close()
                continue
            self.devices[path] = dev
            self.sel.register(dev.fd, selectors.EVENT_READ, path)
            log(f"watching {path} {dev.name!r}")
            if self.grabbed:
                self.grab_one(dev)

    def drop_device(self, path, why):
        dev = self.devices.pop(path, None)
        if not dev:
            return
        try:
            self.sel.unregister(dev.fd)
        except (KeyError, ValueError):
            pass
        try:
            dev.close()
        except OSError:
            pass
        log(f"dropped {path} ({why})")

    def grab_one(self, dev):
        try:
            dev.grab()
        except OSError as exc:
            log(f"could not grab {dev.path} {dev.name!r}: {exc}")

    def keys_held(self):
        for dev in self.devices.values():
            try:
                if dev.active_keys():
                    return True
            except OSError:
                pass
        return False

    def grab_all(self):
        with self.lock:
            for dev in self.devices.values():
                self.grab_one(dev)
            self.grabbed = True

    def ungrab_all(self):
        with self.lock:
            for dev in self.devices.values():
                try:
                    dev.ungrab()
                except OSError:
                    pass
            self.grabbed = False

    # ---- mode -----------------------------------------------------------
    def request_enter(self, why):
        if self.mouse_mode or self.pending_enter:
            return
        if session_locked():
            log(f"not entering mouse mode ({why}): screen is locked")
            return
        self.pending_enter = time.monotonic() + GRAB_WAIT_SECS
        log(f"entering mouse mode ({why})")

    def try_finish_enter(self):
        if self.keys_held():
            if time.monotonic() > self.pending_enter:
                log("gave up entering mouse mode: a key stayed held")
                self.pending_enter = None
            return
        self.pending_enter = None
        self.reset_input_state()
        self.grab_all()
        self.mouse_mode = True
        write_state("mouse")
        if self.cfg["osd"]:
            osd("Keyboard mouse ON  —  Esc to leave")
        log("mouse mode ON")

    def leave(self, why):
        self.pending_enter = None
        if not self.mouse_mode:
            return
        self.release_everything()
        self.ungrab_all()
        self.mouse_mode = False
        write_state("off")
        if self.cfg["osd"]:
            osd("Keyboard mouse off", "input-keyboard")
        log(f"mouse mode OFF ({why})")

    def toggle(self, why):
        if self.mouse_mode:
            self.leave(why)
        else:
            self.request_enter(why)

    def release_everything(self):
        for btn in list(self.buttons_down):
            self.pointer.write(e.EV_KEY, btn, 0)
        for key in list(self.passthru_down):
            self.passthru.write(e.EV_KEY, key, 0)
        self.pointer.syn()
        self.passthru.syn()
        self.reset_input_state()

    # ---- output helpers -------------------------------------------------
    def press(self, btn):
        if btn not in self.buttons_down:
            self.buttons_down.add(btn)
            self.pointer.write(e.EV_KEY, btn, 1)
            self.pointer.syn()

    def release(self, btn):
        if btn in self.buttons_down:
            self.buttons_down.discard(btn)
            self.pointer.write(e.EV_KEY, btn, 0)
            self.pointer.syn()

    def scroll_step(self):
        for axis, direction in set(self.scroll_held.values()):
            self.pointer.write(e.EV_REL, axis, direction)
        self.pointer.syn()

    # ---- key handling ---------------------------------------------------
    def track_toggle(self, code, value, now):
        """Return True when this event completes the toggle gesture."""
        key = self.cfg["toggle_key"]
        if code != key:
            if value == 1:          # any other key spoils a pending tap
                self.tap_clean = False
                self.last_tap = 0.0
            return False
        if value == 1:
            self.tap_down_at = now
            self.tap_clean = True
            return False
        if value != 0 or self.tap_down_at is None:
            return False
        clean = self.tap_clean and now - self.tap_down_at < CLEAN_TAP_SECS
        self.tap_down_at = None
        if not clean:
            self.last_tap = 0.0
            return False
        if self.cfg["toggle_style"] == "single":
            return True
        if now - self.last_tap <= self.cfg["double_tap"]:
            self.last_tap = 0.0
            return True
        self.last_tap = now
        return False

    def handle_key(self, code, value, now):
        toggled = self.track_toggle(code, value, now)

        if not self.mouse_mode:
            if toggled:
                self.request_enter("toggle key")
            return

        # Modifiers pass through so Ctrl/Shift/Alt+click work in the session.
        if code in MODIFIERS:
            if value == 1:
                self.passthru_down.add(code)
            elif value == 0:
                if code not in self.passthru_down:
                    code = None     # pressed before the grab: nothing to undo
                self.passthru_down.discard(code)
            if code is not None:
                self.passthru.write(e.EV_KEY, code, value)
                self.passthru.syn()
            if toggled:
                self.leave("toggle key")
            return

        if toggled:                 # a non-modifier toggle key (e.g. ScrollLock)
            self.leave("toggle key")
            return
        if value == 2:              # we do our own timing; ignore autorepeat
            return

        cfg = self.cfg
        if value == 0 and code in self.move_held:
            self.move_held.pop(code)
        elif value == 0 and code in self.button_keys:
            btn = self.button_keys.pop(code)
            still = btn in self.button_keys.values()
            if not still and not (btn == e.BTN_LEFT and self.drag_locked):
                self.release(btn)
        elif value == 0 and code in self.scroll_held:
            self.scroll_held.pop(code)
        elif code in cfg["move"]:
            if value == 1:
                if not self.move_held:
                    self.move_started = now
                    self.last_tick = now
                    self.frac = [0.0, 0.0]
                    self.nudge(cfg["move"][code])
                self.move_held[code] = cfg["move"][code]
        elif code in cfg["buttons"]:
            btn = cfg["buttons"][code]
            if value == 1:
                if btn == e.BTN_LEFT and self.drag_locked:
                    self.drag_locked = False
                    self.release(btn)
                    return
                self.button_keys[code] = btn
                self.press(btn)
        elif code in cfg["double_click"]:
            if value == 1:
                for _ in range(2):
                    self.pointer.write(e.EV_KEY, e.BTN_LEFT, 1)
                    self.pointer.syn()
                    self.pointer.write(e.EV_KEY, e.BTN_LEFT, 0)
                    self.pointer.syn()
                    time.sleep(0.03)
        elif code in cfg["drag_lock"]:
            if value == 1:
                if self.drag_locked:
                    self.drag_locked = False
                    self.release(e.BTN_LEFT)
                else:
                    self.drag_locked = True
                    self.press(e.BTN_LEFT)
        elif code in cfg["scroll"]:
            if value == 1:
                first = not self.scroll_held
                self.scroll_held[code] = cfg["scroll"][code]
                if first:
                    self.scroll_step()
                    self.scroll_next = now + 0.3   # repeat delay
        elif code in EXIT:
            if value == 0:          # on release, so Esc never reaches the app half-pressed
                self.leave("Esc")
        # every other key is swallowed

    def speed_now(self, now):
        cfg = self.cfg
        if cfg["accel"] > 0:
            t = min(1.0, (now - self.move_started) / cfg["accel"])
        else:
            t = 1.0
        v = cfg["speed"] + (cfg["max_speed"] - cfg["speed"]) * t * t
        if self.passthru_down & SHIFTS:
            v = max(20.0, v * cfg["slow"])
        return v

    def nudge(self, direction):
        """One pixel immediately on press, so a quick tap is a precise step."""
        self.pointer.write(e.EV_REL, e.REL_X, direction[0])
        self.pointer.write(e.EV_REL, e.REL_Y, direction[1])
        self.pointer.syn()

    def motion_tick(self, now):
        if self.move_held:
            dt = min(0.05, now - (self.last_tick or now))
            self.last_tick = now
            dx = sum(d[0] for d in self.move_held.values())
            dy = sum(d[1] for d in self.move_held.values())
            if dx or dy:
                norm = (dx * dx + dy * dy) ** 0.5
                step = self.speed_now(now) * dt / norm
                self.frac[0] += dx * step
                self.frac[1] += dy * step
                ix, iy = int(self.frac[0]), int(self.frac[1])
                self.frac[0] -= ix
                self.frac[1] -= iy
                if ix:
                    self.pointer.write(e.EV_REL, e.REL_X, ix)
                if iy:
                    self.pointer.write(e.EV_REL, e.REL_Y, iy)
                if ix or iy:
                    self.pointer.syn()
        if self.scroll_held and now >= self.scroll_next:
            self.scroll_step()
            self.scroll_next = now + 1 / self.cfg["scroll_rate"]

    # ---- signals / watchdog ---------------------------------------------
    def on_signal(self, signum, _frame):
        self.pending_signals.append(signum)
        try:
            os.write(self.signal_w, b"x")
        except OSError:
            pass

    def process_signals(self):
        try:
            os.read(self.signal_r, 512)
        except OSError:
            pass
        sigs, self.pending_signals = self.pending_signals, []
        for s in sigs:
            if s == signal.SIGUSR1:
                self.toggle("SIGUSR1")
            elif s == signal.SIGUSR2:
                self.leave("SIGUSR2")
            elif s == signal.SIGHUP:
                if self.mouse_mode:     # key assignments may change under held keys
                    self.release_everything()
                self.cfg = load_config()
                log("config reloaded")
            elif s in (signal.SIGTERM, signal.SIGINT):
                raise SystemExit(0)

    def watchdog(self):
        while True:
            time.sleep(1)
            if self.grabbed and time.monotonic() - self.heartbeat > self.cfg["watchdog"]:
                log("WATCHDOG: main loop stalled, releasing keyboard grabs")
                with self.lock:
                    for dev in list(self.devices.values()):
                        try:
                            dev.ungrab()
                        except OSError:
                            pass
                    self.grabbed = False
                write_state("off")

    # ---- main loop --------------------------------------------------------
    def run(self):
        for s in (signal.SIGUSR1, signal.SIGUSR2, signal.SIGHUP,
                  signal.SIGTERM, signal.SIGINT):
            signal.signal(s, self.on_signal)
        threading.Thread(target=self.watchdog, daemon=True).start()
        write_state("off")
        sd_notify("READY=1")
        next_ping = 0.0
        log(f"keymouse {__version__} ready; toggle = {self.cfg['toggle_style']} "
            f"{e.KEY.get(self.cfg['toggle_key'])}")
        self.rescan()
        self.next_rescan = time.monotonic() + RESCAN_SECS
        if not self.devices:
            log("WARNING: no keyboard could be opened, the toggle will not work. "
                + PERMISSION_HINT)

        while True:
            now = time.monotonic()
            self.heartbeat = now
            if now >= next_ping:
                sd_notify("WATCHDOG=1")
                next_ping = now + 2.0
            if now >= self.next_rescan:
                self.rescan()
                self.next_rescan = now + RESCAN_SECS

            # The watchdog may have dropped the grab under us: never stay in
            # mouse mode without one, or keys would both type and move.
            if self.mouse_mode and not self.grabbed:
                self.leave("grab lost")

            if self.mouse_mode and now >= self.next_lock_poll:
                self.next_lock_poll = now + LOCK_POLL_SECS
                if session_locked():
                    self.leave("screen locked")

            busy = self.mouse_mode and (self.move_held or self.scroll_held)
            timeout = TICK if (busy or self.pending_enter) else IDLE_TIMEOUT

            for key, _ in self.sel.select(timeout):
                if key.data == "signal":
                    self.process_signals()
                    continue
                dev = self.devices.get(key.data)
                if dev is None:
                    continue
                try:
                    events = list(dev.read())
                except BlockingIOError:
                    continue
                except OSError as exc:
                    self.drop_device(key.data, str(exc))
                    continue
                t = time.monotonic()
                for ev in events:
                    if ev.type == e.EV_KEY:
                        self.handle_key(ev.code, ev.value, t)

            if self.pending_enter:
                self.try_finish_enter()
            if self.mouse_mode:
                self.motion_tick(time.monotonic())

    def shutdown(self):
        try:
            self.release_everything()
        except OSError:
            pass
        self.ungrab_all()
        for dev in self.devices.values():
            try:
                dev.close()
            except OSError:
                pass
        self.pointer.close()
        self.passthru.close()
        try:
            os.unlink(STATE)
        except OSError:
            pass
        log("exited cleanly")


def main():
    if "--version" in sys.argv:
        print(__version__)
        return 0
    km = KeyMouse()
    try:
        km.run()
    except SystemExit:
        pass
    finally:
        km.shutdown()


if __name__ == "__main__":
    sys.exit(main())

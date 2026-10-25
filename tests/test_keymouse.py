"""Tests for the keymouse daemon. Nothing is grabbed or injected: the pointer
and passthrough devices are replaced by recorders, D-Bus by a fake busctl."""
import configparser
import importlib.util
import os
import unittest
from unittest import mock

from evdev import ecodes as e

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SRC, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


km = load("keymouse", "keymouse.py")
km.log = lambda msg: None


class Recorder:
    def __init__(self):
        self.events = []

    def write(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        pass


def make_daemon(**conf):
    """A KeyMouse in mouse mode without touching any device."""
    k = km.KeyMouse.__new__(km.KeyMouse)
    k.pointer, k.passthru = Recorder(), Recorder()
    k.cfg = {**km.load_config(),
             **km.build_keymaps(dict(km.DEFAULTS, **conf), e.KEY_RIGHTSHIFT)}
    k.mouse_mode = True
    k.tap_down_at, k.tap_clean, k.last_tap = None, False, 0.0
    k.reset_input_state()
    return k


def tap(k, code):
    """Press and release; return (events on press, events on release)."""
    k.pointer.events.clear()
    k.handle_key(code, 1, 1.0)
    down = list(k.pointer.events)
    k.pointer.events.clear()
    k.handle_key(code, 0, 1.1)
    return down, list(k.pointer.events)


class KeymapTests(unittest.TestCase):
    def test_defaults(self):
        m = km.build_keymaps(dict(km.DEFAULTS), e.KEY_RIGHTSHIFT)
        self.assertEqual(m["move"][e.KEY_I], (0, -1))
        self.assertEqual(m["move"][e.KEY_L], (1, 0))
        self.assertEqual(m["buttons"], {e.KEY_SPACE: e.BTN_LEFT, e.KEY_F: e.BTN_LEFT,
                                        e.KEY_R: e.BTN_RIGHT, e.KEY_COMPOSE: e.BTN_RIGHT,
                                        e.KEY_M: e.BTN_MIDDLE})
        self.assertEqual(len(m["scroll"]), 6)

    def test_arrows_always_move(self):
        m = km.build_keymaps(dict(km.DEFAULTS, move_up_keys="", move_down_keys="",
                                  move_left_keys="", move_right_keys=""),
                             e.KEY_RIGHTSHIFT)
        self.assertEqual(set(m["move"]), {e.KEY_UP, e.KEY_DOWN, e.KEY_LEFT, e.KEY_RIGHT})

    def test_reserved_duplicate_and_unknown_are_ignored(self):
        m = km.build_keymaps(dict(km.DEFAULTS, move_up_keys="KEY_W",
                                  left_click_keys="KEY_ENTER, KEY_UP KEY_BOGUS",
                                  right_click_keys="KEY_W KEY_RIGHTSHIFT KEY_ESC"),
                             e.KEY_RIGHTSHIFT)
        self.assertEqual(m["move"][e.KEY_W], (0, -1))
        self.assertEqual(m["move"][e.KEY_UP], (0, -1))         # still an arrow
        self.assertEqual(m["buttons"], {e.KEY_ENTER: e.BTN_LEFT, e.KEY_M: e.BTN_MIDDLE})

    def test_assignment_takes_over_fixed_bindings(self):
        m = km.build_keymaps(dict(km.DEFAULTS, move_up_keys="KEY_W",
                                  left_click_keys="KEY_ENTER", middle_click_keys="KEY_G"),
                             e.KEY_RIGHTSHIFT)
        self.assertNotIn(e.KEY_W, m["scroll"])
        self.assertNotIn(e.KEY_ENTER, m["double_click"])
        self.assertIn(e.KEY_KPENTER, m["double_click"])
        self.assertEqual(m["drag_lock"], set())

    def test_example_config_matches_defaults(self):
        cp = configparser.ConfigParser()
        cp.read(os.path.join(HERE, "..", "data", "keymouse.conf.example"))
        self.assertEqual(dict(cp["keymouse"]), km.DEFAULTS)


class KeyHandlingTests(unittest.TestCase):
    def test_custom_move_and_click(self):
        k = make_daemon(move_up_keys="KEY_W", left_click_keys="KEY_ENTER",
                        right_click_keys="")
        down, _ = tap(k, e.KEY_W)
        self.assertIn((e.EV_REL, e.REL_Y, -1), down)
        down, up = tap(k, e.KEY_ENTER)
        self.assertEqual((down, up), ([(e.EV_KEY, e.BTN_LEFT, 1)], [(e.EV_KEY, e.BTN_LEFT, 0)]))
        self.assertEqual(tap(k, e.KEY_SPACE), ([], []))         # no longer a click
        self.assertEqual(tap(k, e.KEY_R), ([], []))             # right click cleared

    def test_arrow_moves(self):
        down, _ = tap(make_daemon(), e.KEY_LEFT)
        self.assertIn((e.EV_REL, e.REL_X, -1), down)

    def test_release_after_remap_does_not_stick(self):
        k = make_daemon()
        k.handle_key(e.KEY_M, 1, 2.0)
        k.cfg.update(km.build_keymaps(dict(km.DEFAULTS, middle_click_keys=""),
                                      e.KEY_RIGHTSHIFT))
        k.handle_key(e.KEY_M, 0, 2.1)
        self.assertEqual(k.buttons_down, set())
        self.assertEqual(k.pointer.events[-1], (e.EV_KEY, e.BTN_MIDDLE, 0))

    def test_modifier_passes_through(self):
        k = make_daemon()
        k.handle_key(e.KEY_LEFTCTRL, 1, 1.0)
        self.assertEqual(k.passthru.events, [(e.EV_KEY, e.KEY_LEFTCTRL, 1)])


class FakeBus:
    """Answers busctl argument lists from a table; anything else fails."""
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, *args, system=False, timeout=1.5):
        self.calls.append(args)
        for prefix, out in self.answers.items():
            if args[:len(prefix)] == prefix:
                return out
        return None


SESSION = ("get-property", km.LOGIN1, "/org/freedesktop/login1/user/self")
LOCKED_HINT = ("get-property", km.LOGIN1, "/org/freedesktop/login1/session/_33")
SCREENSAVER = ("call", "org.freedesktop.ScreenSaver")


class LockTests(unittest.TestCase):
    def setUp(self):
        km._session_path = None

    def check(self, answers, expected):
        with mock.patch.object(km, "busctl", FakeBus(answers)):
            self.assertIs(km.session_locked(), expected)

    def test_logind_locked(self):
        self.check({SESSION: '(so) "3" "/org/freedesktop/login1/session/_33"',
                    LOCKED_HINT: "b true", SCREENSAVER: "b false"}, True)

    def test_screensaver_only(self):       # e.g. no logind session found
        self.check({SCREENSAVER: "b true"}, True)

    def test_unlocked(self):
        self.check({SESSION: '(so) "3" "/org/freedesktop/login1/session/_33"',
                    LOCKED_HINT: "b false"}, False)

    def test_nobody_answers(self):
        self.check({}, None)


class OsdTests(unittest.TestCase):
    def run_osd(self, answers):
        bus = FakeBus(answers)
        with mock.patch.object(km, "busctl", bus), \
                mock.patch.object(km.threading, "Thread") as thread:
            km.osd("hello")
            thread.call_args.kwargs["target"]()
        return [c[1] for c in bus.calls if c[0] == "call"]

    def test_plasma_osd(self):
        self.assertEqual(self.run_osd({("status", "org.kde.plasmashell"): "ok",
                                       ("call", "org.kde.plasmashell"): ""}),
                         ["org.kde.plasmashell"])

    def test_other_desktops_get_a_notification(self):
        self.assertEqual(self.run_osd({}), ["org.freedesktop.Notifications"])


class PermissionTests(unittest.TestCase):
    def test_uinput_denied_exits_78(self):
        with mock.patch.object(km.KeyMouse, "make_uinput",
                               side_effect=PermissionError(13, "denied")):
            with self.assertRaises(SystemExit) as cm:
                km.KeyMouse()
        self.assertEqual(cm.exception.code, 78)


if __name__ == "__main__":
    unittest.main()

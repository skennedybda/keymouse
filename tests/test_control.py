"""Tests for the settings window, run offscreen against a scratch config.
Run with QT_QPA_PLATFORM=offscreen (packaging/build.sh sets it)."""
import importlib.util
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt          # noqa: E402
from PyQt6.QtGui import QKeyEvent            # noqa: E402
from PyQt6.QtWidgets import QApplication     # noqa: E402


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SRC, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kc = load("keymouse_control", "keymouse-control.py")
km = load("keymouse_daemon", "keymouse.py")
app = QApplication.instance() or QApplication([])


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conf = os.path.join(self.tmp, "keymouse.conf")
        shutil.copy(os.path.join(HERE, "..", "data", "keymouse.conf.example"), self.conf)
        self.patches = [mock.patch.object(kc, "CONF", self.conf),
                        mock.patch.object(kc, "systemctl", lambda *a: (0, "")),
                        mock.patch.object(kc, "current_mode", lambda: "off")]
        for p in self.patches:
            p.start()
        self.w = kc.KeyMouseControl(start_in_tray=True)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.w.deleteLater()
        shutil.rmtree(self.tmp)

    def press(self, opt, slot, evdev_code, qtkey=Qt.Key.Key_A):
        self.w.on_key_slot(opt, slot)
        b = self.w.key_buttons[opt][slot]
        b.event(QKeyEvent(QEvent.Type.KeyPress, qtkey, Qt.KeyboardModifier.NoModifier,
                          evdev_code + 8, 0, 0, ""))
        return b.text()

    def conf_value(self, key):
        with open(self.conf) as f:
            m = re.search(rf"^{key}\s*=\s*(.*)$", f.read(), re.M)
        return m.group(1).strip() if m else None

    def test_loads_defaults(self):
        self.assertEqual([b.text() for b in self.w.key_buttons["left_click_keys"]],
                         ["Space", "F"])
        self.assertTrue(self.w.osd_cb.isChecked())

    def test_assign_steal_reserved_reset(self):
        self.assertEqual(self.press("move_up_keys", 0, 17), "W")
        self.assertEqual(self.conf_value("move_up_keys"), "KEY_W")
        self.press("right_click_keys", 1, 57, Qt.Key.Key_Space)   # steals Space
        self.assertEqual(self.conf_value("left_click_keys"), "KEY_F")
        self.assertEqual(self.press("left_click_keys", 0, 103), "—")  # Up: reserved
        self.assertIn("arrow keys", self.w.key_status.text())
        self.w.on_reset_keys()
        self.assertEqual(self.conf_value("move_up_keys"), "KEY_I")
        self.assertEqual(self.conf_value("left_click_keys"), "KEY_SPACE KEY_F")

    def test_notifications_toggle(self):
        self.w.osd_cb.setChecked(False)
        self.assertEqual(self.conf_value("osd"), "0")

    def test_permission_banner(self):
        with mock.patch.object(kc, "access_problems",
                               lambda: ["cannot read the keyboard"]):
            self.assertFalse(self.w.check_access())
        self.assertFalse(self.w.access_label.isHidden())
        self.assertIn("70-keymouse.rules", self.w.access_label.text())

    def test_versions_match(self):
        self.assertEqual(kc.VERSION, km.__version__)


class AccessProblemTests(unittest.TestCase):
    def test_no_uinput_access(self):
        real = os.access
        with mock.patch.object(kc.os, "access",
                               lambda p, m: False if p == "/dev/uinput" else real(p, m)):
            self.assertIn("cannot create the virtual mouse (/dev/uinput)",
                          kc.access_problems())


if __name__ == "__main__":
    unittest.main()

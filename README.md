# keymouse — Keyboard Mouse

Move the mouse pointer, click, drag and scroll from the keyboard. Double-tap
**Right Shift** to switch mouse mode on and off.

keymouse reads keys straight from the kernel and drives a kernel virtual
mouse, so it keeps working inside remote-desktop clients (such as Citrix
Workspace) that capture the keyboard and swallow desktop shortcuts.

## Install

### Debian / Ubuntu (.deb)

```sh
sudo apt install ./keymouse_1.0.1_all.deb
```

`apt` pulls in the dependencies (`python3-evdev`, `python3-pyqt6`).

### Fedora (.rpm)

```sh
sudo dnf install ./keymouse-1.0.1-1.noarch.rpm
```

`dnf` pulls in the dependencies (`python3-evdev`, `python3-pyqt6`). Tested on
Fedora 44; it should also work on other dnf/rpm distributions that use the
same package names.

### Any distribution (tarball)

```sh
tar xzf keymouse-1.0.1.tar.gz
cd keymouse-1.0.1
sudo ./install.sh            # installs to /usr/local
```

Install the dependencies first if the installer reports them missing:

| Distribution  | Command |
|---------------|---------|
| Debian/Ubuntu | `sudo apt install python3-evdev python3-pyqt6` |
| Fedora        | `sudo dnf install python3-evdev python3-pyqt6` |
| Arch          | `sudo pacman -S python-evdev python-pyqt6` |

Remove with `sudo ./install.sh --uninstall` (from the same tarball).

### After installing

Each user turns it on for themselves: open **Keyboard Mouse** from the
application menu, press **Start**, and tick **Start automatically at login**.
Or, from a terminal (as yourself, not root):

```sh
systemctl --user daemon-reload
systemctl --user enable --now keymouse.service
```

## Keys in mouse mode

| Action | Default keys |
|---|---|
| Move | **Arrow keys** (always), and I / K / J / L |
| Slow, precise move | hold Shift while moving |
| Left click (hold to drag) | Space, F |
| Right click | R, Menu |
| Middle click | M |
| Double-click | Enter |
| Drag lock | G |
| Scroll | W / S, PageUp / PageDown; A / D sideways |
| Leave mouse mode | Esc, or the toggle again |

Shift, Ctrl, Alt and Meta still work, so Ctrl+click and Shift+click work.
Every other key is ignored while mouse mode is on.

Move and click keys can be reassigned in the settings window (two keys per
action, **Restore default keys** to undo). The arrows, Esc, the modifiers and
the toggle key can't be assigned.

## Settings

The settings window writes `~/.config/keymouse.conf` and the running service
picks changes up immediately. `keymouse.conf.example` (in
`/usr/share/doc/keymouse/` or `/usr/local/share/doc/keymouse/`) lists every
option with its default. Hand edits take effect with
`systemctl --user kill -s HUP keymouse.service`.

## Requirements and permissions

- Linux with systemd, a Wayland or X11 desktop, Python 3.9+, python-evdev, PyQt6.
- Access to keyboards (`/dev/input/event*`) and `/dev/uinput`. The package
  installs `70-keymouse.rules`, which gives this access to **the user logged in
  at the computer** through systemd-logind (`uaccess`). The access is granted
  per session and revoked on logout or user switch, and remote/SSH users never
  get it. No group membership is needed.

  **Security trade-off:** while that access is in place, any program the
  logged-in user runs can read keystrokes from the keyboard devices, because
  that is exactly what keymouse needs. Uninstalling removes the rule.

The settings window shows a warning if the access is missing.

## Desktop support

| | KDE Plasma | GNOME and others |
|---|---|---|
| Keys, pointer, clicks, scroll | ✓ | ✓ |
| Leaves mouse mode when the screen locks | ✓ | ✓ (via logind `LockedHint`) |
| On/off message | Plasma OSD | desktop notification |
| Flat pointer acceleration | ✓ (KWin) | the desktop's own setting applies |
| Tray icon | ✓ | needs a tray (e.g. the AppIndicator extension on GNOME) |

## Why no Snap or Flatpak?

keymouse is packaged as a `.deb` and a tarball only, on purpose. Snap and
Flatpak run apps in a sandbox that keeps them away from hardware, and
keymouse's whole job is hardware access: it reads the keyboard devices
directly and creates a kernel virtual mouse. That is what lets it work inside
remote-desktop windows that capture the keyboard. Each format also leaves out
two things keymouse has to install on the host:

- **The udev rule** (`70-keymouse.rules`) that lets the logged-in user read
  the keyboards and use `/dev/uinput`. Neither format can install udev rules,
  so users would need a separate manual step as root, which defeats the point
  of a one-step install.
- **Flatpak** can open up raw device access (`--device=all`), but it cannot
  install the systemd user service that runs the daemon at login. A
  sandboxed app that reads every keystroke is also unlikely to be accepted on
  Flathub.
- **Snap** has interfaces for raw input and for uinput, and snaps can run
  user services. But the uinput interface is privileged, so it needs manual
  Snap Store review or users connecting it by hand. Classic (unconfined)
  confinement would also need Store approval, and snaps are mainly used on
  Ubuntu.

Native packages can install the rule and the service properly, so they are
the supported route. An `.rpm` (Fedora/openSUSE) or an AUR package (Arch)
would fit the same way. Contributions are welcome.

## Safety

- Mouse mode switches off while the screen is locked and can't be switched
  on while it is locked.
- If the program stalls, a watchdog releases the keyboard within seconds, and
  systemd kills and restarts a frozen process.
- The keyboard grab is taken only once no key is held, so no key gets stuck.

## Troubleshooting

```sh
journalctl --user -u keymouse.service -n 50
```

- *"no permission to read …" / "cannot create virtual devices"*: the udev rule
  is missing or not applied yet. Reinstall, or log out and back in.
- Toggle does nothing: check that the service is running (settings window, or
  `systemctl --user status keymouse.service`).

## Building

```sh
packaging/build.sh       # runs the tests, then writes dist/*.tar.gz, *.deb and *.rpm
```

The `.rpm` is built with `rpmbuild` if it is installed, otherwise inside a
Fedora container (podman or docker). `SKIP_RPM=1` skips it.

## License

keymouse is free software, licensed under the **GNU General Public License,
version 3 or (at your option) any later version** (GPL-3.0-or-later). See
[LICENSE](LICENSE).

You may use, study, share and change it. If you distribute a modified
version, you must do so under the same license and make your source code
available. It comes with no warranty.

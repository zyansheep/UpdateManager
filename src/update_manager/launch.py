"""Toggling the resident panel, as drainhound does.

The panel (`update-manager panel serve`, the update-manager-panel.service unit,
Type=dbus) stays loaded and hidden; `update-manager panel` asks it over D-Bus
(GTK's org.gtk.Actions) to toggle, starting the unit first if it isn't running,
or else running the panel right here. Nothing here imports GTK.
"""
import ctypes.util
import os
import shutil
import subprocess
import sys
import time

from . import config

UNIT = 'update-manager-panel.service'
PAGES = ('machines', 'upstream')


def toggle(argv):
    if len(argv) > 1 or (argv and argv[0] not in PAGES):
        print(f"usage: update-manager panel [{'|'.join(PAGES)}] | serve [--open [PAGE]]", file=sys.stderr)
        return 2
    action, parameter = ('show', [argv[0]]) if argv else ('toggle', [])
    if not running() and not start():
        os.execv(config.self_command()[0], [*config.self_command(), 'panel', 'serve', '--open', *argv])
    for _ in range(50):  # A just-started panel takes a moment to export its actions.
        if activate(action, parameter):
            return 0
        time.sleep(0.1)
    print('update-manager: the panel did not answer on D-Bus', file=sys.stderr)
    return 1


def layer_shell_library():
    return os.environ.get('UPDATE_MANAGER_LAYER_SHELL_LIB') or ctypes.util.find_library('gtk4-layer-shell')


def preload(argv):
    """Run again with gtk4-layer-shell preloaded: it must load before libwayland-client."""
    library = layer_shell_library()
    loaded = os.environ.get('LD_PRELOAD', '')
    if not library or 'UPDATE_MANAGER_PRELOADED' in os.environ or library in loaded.replace(' ', ':').split(':'):
        return
    os.environ['UPDATE_MANAGER_PRELOADED'] = loaded
    os.environ['LD_PRELOAD'] = ':'.join(filter(None, (library, loaded)))
    command = config.self_command()
    os.execv(command[0], [*command, *argv])


def bus():
    from gi.repository import Gio
    return Gio.bus_get_sync(Gio.BusType.SESSION)


def running():
    from gi.repository import GLib
    try:
        reply = bus().call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                                'NameHasOwner', GLib.Variant('(s)', (config.APP_ID,)), None, 0, 1000, None)
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


def start():
    if shutil.which('systemctl') is None or os.environ.get('UPDATE_MANAGER_NO_UNIT'):
        return False
    return subprocess.run(['systemctl', '--user', 'start', UNIT], capture_output=True).returncode == 0


def activate(action, parameter):
    from gi.repository import GLib
    try:
        bus().call_sync(config.APP_ID, config.APP_PATH, 'org.gtk.Actions', 'Activate',
                        GLib.Variant('(sava{sv})', (action, [GLib.Variant('s', p) for p in parameter], {})),
                        None, 0, 2000, None)
        return True
    except GLib.Error:
        return False


def main(argv):
    if argv[:1] == ['serve']:
        preload(['panel', *argv])
        from . import panel
        return panel.main(argv)
    return toggle(argv)

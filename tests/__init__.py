"""Tests run with every state directory in a temporary place (set before update_manager is imported)."""
import os
import tempfile

_ROOT = tempfile.mkdtemp(prefix='update-manager-tests-')
for name in ('RUNTIME', 'CACHE', 'STATE'):
    os.environ[f'UPDATE_MANAGER_{name}_DIR'] = os.path.join(_ROOT, name.lower())
os.environ['UPDATE_MANAGER_SYSTEM_RUNTIME'] = os.path.join(_ROOT, 'system-run')
os.environ['UPDATE_MANAGER_SYSTEM_STATE'] = os.path.join(_ROOT, 'system-state')
os.environ['UPDATE_MANAGER_HOST'] = 'laptop'
os.environ['UPDATE_MANAGER_NO_SYSTEMD_RUN'] = '1'
os.environ['XDG_CONFIG_HOME'] = os.path.join(_ROOT, 'config')
os.environ.pop('WAYLAND_DISPLAY', None)
os.environ.pop('DISPLAY', None)
for name in ('DITHER_ASK_URL', 'DITHER_ASK_TOKEN_FILE', 'DITHER_ASK_KEYS'):  # never the real hub
    os.environ.pop(name, None)

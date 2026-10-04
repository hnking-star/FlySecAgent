"""Only local plugin paths. No user-global Codex configuration is changed."""
from pathlib import Path
import os
import json

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'


def state_dir():
    # Stable across CLI/Desktop and package updates. May be overridden by the
    # user for isolated testing. Never write keys or databases into the package.
    return Path(os.environ.get('FLYSEC_PLUGIN_STATE_DIR', str(Path.home()/'.local/share/flysecagent-codex'))).expanduser().resolve()


def settings():
    path = state_dir()/'settings.json'
    if not path.exists():
        return {'port':8790}
    data = json.loads(path.read_text())
    if not isinstance(data,dict):
        raise ValueError('Invalid private plugin settings')
    return data

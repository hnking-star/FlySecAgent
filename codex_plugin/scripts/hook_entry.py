"""Codex loads this command using PLUGIN_ROOT; fail open for the main Agent."""
from __future__ import annotations
import os
import sys
from paths import state_dir, settings, RUNTIME


def main():
    try:
        config = settings()
        data = state_dir()/'data'
        os.environ['FLYSEC_DATA_DIR'] = str(data)
        os.environ['FLYSEC_API_BASE'] = f"http://127.0.0.1:{int(config.get('port',8790))}"
        sys.path.insert(0,str(RUNTIME))
        from hook.codex import main as run
        return run()
    except (OSError,ValueError,ImportError):
        print('[flysec-plugin] local runtime unavailable; run plugin setup/start',file=sys.stderr)
        return 0


if __name__=='__main__':
    raise SystemExit(main())

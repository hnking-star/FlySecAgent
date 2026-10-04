"""Explicit setup/start/status/stop. Nothing is installed from a lifecycle hook."""
from __future__ import annotations
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
import venv
from pathlib import Path
from paths import ROOT, RUNTIME, state_dir, settings


def health(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=1) as response:
            return json.load(response)
    except (OSError,ValueError,urllib.error.URLError):
        return None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['setup','start','status','stop'])
    parser.add_argument('--port',type=int)
    parser.add_argument('--key-file',type=Path)
    args=parser.parse_args()
    state=state_dir(); config=settings(); port=args.port or int(config.get('port',8790))
    if not 1<=port<=65535: raise SystemExit('Invalid port')
    py=state/'.venv/bin/python'
    pidfile=state/'service.pid'
    if args.action=='setup':
        state.mkdir(parents=True,exist_ok=True,mode=0o700);state.chmod(0o700)
        if not py.exists():venv.EnvBuilder(with_pip=True).create(state/'.venv')
        subprocess.run([str(py),'-m','pip','install','-e',str(RUNTIME)],check=True)
        subprocess.run([str(py),'-m','service.export_schemas'],cwd=RUNTIME,check=True)
        subprocess.run(['npm','ci','--prefix',str(RUNTIME/'pi_ext')],check=True)
        subprocess.run(['npm','run','build','--prefix',str(RUNTIME/'pi_ext')],check=True)
        config['port']=port
        if args.key_file:config['api_key_file']=str(args.key_file.expanduser().resolve())
        path=state/'settings.json';path.write_text(json.dumps(config,indent=2)+'\n');path.chmod(0o600)
        print('Setup complete. Key is referenced by path, not copied.');return
    if args.action=='status':
        print(json.dumps({'state_dir':str(state),'health':health(port),'pid':pidfile.read_text().strip() if pidfile.exists() else None}));return
    if args.action=='stop':
        if not pidfile.exists(): print('No service PID owned by this plugin');return
        pid=int(pidfile.read_text())
        try:
            command=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True).strip()
        except subprocess.CalledProcessError:
            pidfile.unlink();print('Service already exited');return
        if str(py) not in command or not command.endswith('-u -m service'):
            raise SystemExit('PID identity does not match this plugin; not stopping it')
        # A private PID marker and exact interpreter identity are required.
        os.kill(pid,signal.SIGTERM)
        for _ in range(60):
            try:os.kill(pid,0)
            except ProcessLookupError:pidfile.unlink(missing_ok=True);print('Service stopped');return
            time.sleep(.1)
        raise SystemExit('Service did not exit gracefully; no forced termination')
    if health(port):raise SystemExit('Port already has a service. Not attaching to or replacing it.')
    if not py.exists() or not (RUNTIME/'pi_ext/dist/index.js').exists():raise SystemExit('Run setup first')
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    env={**os.environ,'FLYSEC_PORT':str(port),'FLYSEC_DATA_DIR':str(state/'data')}
    env.pop('FLYSEC_PI_COMMAND',None)
    if config.get('api_key_file'):env['FLYSEC_PI_API_KEY_FILE']=config['api_key_file']
    if not env.get('FLYSEC_PI_API_KEY') and not Path(env.get('FLYSEC_PI_API_KEY_FILE',str(state/'data/.deepseek-key'))).is_file():
        raise SystemExit('Configure --key-file during setup or a private data/.deepseek-key')
    with (state/'service.log').open('a') as log:
        proc=subprocess.Popen([str(py),'-u','-m','service'],cwd=RUNTIME,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    pidfile.write_text(str(proc.pid)+'\n');pidfile.chmod(0o600)
    for _ in range(50):
        if proc.poll() is not None:raise SystemExit('Service exited; inspect private service.log')
        if health(port):print(f'http://127.0.0.1:{port}/web/');return
        time.sleep(.2)
    proc.terminate();raise SystemExit('Service health timeout; newly created service stopped')


if __name__=='__main__':
    main()

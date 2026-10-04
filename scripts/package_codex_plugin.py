"""Build a standalone source plugin from an explicit, non-sensitive allowlist."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns('__pycache__','*.pyc','.DS_Store','node_modules','dist','data','*.egg-info')


def package(output: Path):
    output=output.resolve()
    if output.exists():
        raise ValueError('Output already exists; choose a fresh directory (no automatic deletion)')
    output.mkdir(parents=True)
    shutil.copytree(ROOT/'codex_plugin',output,dirs_exist_ok=True,ignore=IGNORE)
    runtime=output/'runtime';runtime.mkdir()
    for name in ['service','hook','web']:
        shutil.copytree(ROOT/name,runtime/name,ignore=IGNORE)
    pi=runtime/'pi_ext';pi.mkdir()
    for name in ['src','resources','scripts']:
        shutil.copytree(ROOT/'pi_ext'/name,pi/name,ignore=IGNORE)
    for name in ['package.json','package-lock.json','tsconfig.json']:
        shutil.copy2(ROOT/'pi_ext'/name,pi/name)
    shutil.copy2(ROOT/'pyproject.toml',runtime/'pyproject.toml')
    shutil.copy2(ROOT/'LICENSE',output/'LICENSE')
    shutil.copy2(ROOT/'LICENSE',runtime/'LICENSE')
    catalog=output/'.agents/plugins';catalog.mkdir(parents=True)
    marketplace={'name':'flysecagent-local','interface':{'displayName':'FlySecAgent Local'},'plugins':[{
        'name':'flysecagent','source':{'source':'local','path':'./'},
        'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Developer tools'}]}
    (catalog/'marketplace.json').write_text(json.dumps(marketplace,ensure_ascii=False,indent=2)+'\n')
    # A machine-verifiable package inventory. No runtime data or keys are read.
    files={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob('*')) if p.is_file()}
    (output/'PACKAGE_INVENTORY.json').write_text(json.dumps({'version':'0.2.1','files':files},indent=2)+'\n')
    archive=output.with_suffix('.zip')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(output.rglob('*')):
            if p.is_file():z.write(p,str(Path(output.name)/p.relative_to(output)))
    return output,archive


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'dist/flysecagent-codex')
    args=parser.parse_args();folder,archive=package(args.output)
    print(json.dumps({'folder':str(folder),'zip':str(archive)}))

"""Install only FlySecAgent hooks in a Coco workspace; preserve other entries."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SessionEnd")
MARKER = "FlySecAgent Coco integration"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--client", choices=["coco", "traex"], default="coco")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent.parent / "data")
    parser.add_argument("--api-base", default="http://127.0.0.1:8787")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    path = args.workspace.resolve() / ".trae" / ("cli/hooks.json" if args.client == "traex" else "hooks.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_text() if path.exists() else None
    doc = json.loads(before) if before else {"hooks": {}}
    hooks = doc.setdefault("hooks", {})
    root = Path(__file__).resolve().parent.parent
    command = shlex.join([sys.executable, "-m", "hook.coco", "--data-dir", str(args.data_dir.resolve()), "--api-base", args.api_base])
    command = "cd " + shlex.quote(str(root)) + " && " + command
    for event in (*EVENTS, "Interrupt") if args.client == "traex" else EVENTS:
        groups = [g for g in hooks.get(event, []) if g.get("description") != MARKER]
        if not args.uninstall:
            groups.append({"description": MARKER, "hooks": [{"type": "command", "command": command, "timeout": 4}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    after = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if before != after:
        if before is not None:
            backup = path.with_name("hooks.json.flysec-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
            backup.write_text(before)
        path.write_text(after)
    print(path)


if __name__ == "__main__":
    main()

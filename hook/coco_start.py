"""Start an opted-in Coco session without asking the user for a session ID."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    args, coco_args = parser.parse_known_args()
    env = os.environ.copy()
    env.update(FLYSEC_TARGET=args.target, FLYSEC_OBJECTIVE=args.objective)
    os.chdir(args.cwd.resolve())
    os.execvpe("coco", ["coco", *coco_args], env)


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# flysec-start.sh — 用户显式开启观察。
#
# 用法:
#   ./flysec-start.sh <session_id> <target> <objective>
#
# 环境变量:
#   FLYSEC_API_BASE  默认 http://127.0.0.1:8787
#   FLYSEC_DATA_DIR  默认 ./data（基于当前目录）

set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: $0 <session_id> <target> <objective>" >&2
  exit 2
fi

SESSION="$1"
TARGET="$2"
OBJECTIVE="$3"

API="${FLYSEC_API_BASE:-http://127.0.0.1:8787}"
DATA_DIR="${FLYSEC_DATA_DIR:-$(pwd)/data}"
SECRET_FILE="${DATA_DIR}/.secret"

if [ ! -f "$SECRET_FILE" ]; then
  echo "error: cannot read service token at $SECRET_FILE" >&2
  echo "       hint: start the service first with 'python -m service'" >&2
  exit 1
fi
TOKEN="$(cat "$SECRET_FILE")"

# 用 python 构造 JSON，避免 jq 依赖
BODY=$(python3 -c "
import json, sys
print(json.dumps({
    'session_id': sys.argv[1],
    'hint': {'target': sys.argv[2], 'objective': sys.argv[3]},
}))
" "$SESSION" "$TARGET" "$OBJECTIVE")

curl -sS -H "X-FlySec-Token: $TOKEN" \
     -H "Content-Type: application/json" \
     -X POST -d "$BODY" \
     "$API/hook/project.ensure"
printf '\n'

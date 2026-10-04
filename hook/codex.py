"""Passive Codex command hooks. Only explicitly enrolled sessions are captured."""
from __future__ import annotations

import os
import sqlite3
import sys
from .common import (read_event, load_config, event_session_id, http_post,
                     flush_queue, enqueue_ingest, deliver_pending_map,
                     write_additional_context)
from .enrollment import parse_start_prompt, session_enabled
from .post_tool_use import _build_payload


def main() -> int:
    event = read_event()
    cfg = load_config()
    sid = event_session_id(event)
    if not cfg or not sid:
        return 0
    kind = event.get('hook_event_name')
    if kind not in {'SessionStart', 'UserPromptSubmit', 'PostToolUse', 'Stop', 'SessionEnd', 'Interrupt'}:
        return 0
    enabled = session_enabled(cfg.data_dir, sid)
    activated = False
    if not enabled:
        hint = None
        if kind == 'UserPromptSubmit':
            hint = parse_start_prompt(event.get('prompt'))
        elif kind == 'SessionStart' and os.environ.get('FLYSEC_TARGET') and os.environ.get('FLYSEC_OBJECTIVE'):
            hint = (os.environ['FLYSEC_TARGET'], os.environ['FLYSEC_OBJECTIVE'])
        if hint:
            status, result = http_post(cfg, '/hook/project.ensure', {'session_id': sid, 'hint': {'target': hint[0], 'objective': hint[1]}})
            activated = status == 200 and bool(result and result.get('ok'))
            enabled = activated and session_enabled(cfg.data_dir, sid)
            if not enabled:
                print('[flysec-codex] enrollment unavailable; no session capture was enabled', file=sys.stderr)
    if not enabled:
        return 0
    flush_queue(cfg, max_items=10)
    if kind == 'UserPromptSubmit':
        http_post(cfg, '/control/agent-turn/begin', {'session_id': sid})
        if activated:
            write_additional_context(kind, 'FlySecAgent API 记忆已开启。只整理本会话工具证据，API 发现与测试记录分开；执行报错不代表目标安全。最终答复请区分已完成、证据、限制和未完成事项。')
        else:
            deliver_pending_map(cfg, kind, sid)
    elif kind == 'PostToolUse':
        # PostToolUse also covers Bash non-zero exits in Codex. Keep the raw
        # response unchanged; never manufacture a separate failure response.
        try:
            conn = sqlite3.connect((cfg.data_dir/'flysec.db').as_uri()+'?mode=ro', uri=True, timeout=.1)
            try:
                row = conn.execute('SELECT agent_activity_known FROM projects WHERE session_id=?', (sid,)).fetchone()
            finally:
                conn.close()
            if row and not row[0]:
                http_post(cfg, '/control/agent-turn/begin', {'session_id': sid})
        except sqlite3.Error:
            pass
        payload = _build_payload(event, sid)
        if payload:
            payload['metadata'].update({'hook_event_name': kind, 'source': 'codex-plugin'})
            for key in ('truncated', 'output_truncated', 'is_error'):
                if key in event:
                    payload['metadata'][key] = event[key]
            status, _ = http_post(cfg, '/hook/record.ingest', payload)
            if status == 0 or status >= 500:
                enqueue_ingest(cfg, payload)
        deliver_pending_map(cfg, kind, sid)
    elif kind in {'Stop', 'SessionEnd', 'Interrupt'}:
        http_post(cfg, '/control/agent-turn/stop', {'session_id': sid})
    elif kind == 'SessionStart':
        deliver_pending_map(cfg, kind, sid)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, sqlite3.Error):
        print('[flysec-codex] local hook unavailable; main Agent is not blocked', file=sys.stderr)
        raise SystemExit(0)

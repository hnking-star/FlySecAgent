"""Codex Hook 脚本包。

每个脚本是独立可执行的 Python 模块：

    python -m hook.on_session_start
    python -m hook.on_user_prompt
    python -m hook.post_tool_use
    python -m hook.on_stop

脚本从 stdin 读 Codex 事件 JSON，向服务（HTTP）转发；按需在 stdout
输出 hookSpecificOutput JSON 给 Codex。

设计约束：
- 1 秒 HTTP 超时，不阻塞主 Agent。
- 服务不可用时 record.ingest 落 queue 文件，下次 Hook 启动重放。
- 不采集用户 prompt / AI 回复 / thinking。
- 不自动创建项目；用户通过 scripts/flysec-start.sh 开启观察。
"""

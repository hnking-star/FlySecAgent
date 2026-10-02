# hook

Codex Hook 脚本在 **Task 8** 落到这里。

本任务（Task 1）只建占位目录。预计包含：

- `post_tool_use.py`：工具返回后入库 + 查新 map + ack
- `on_stop.py`：触发补整理
- `on_session_start.py`：恢复绑定
- `on_user_prompt_submit.py`：恢复计时 + 补交未交付新 map
- `config.example.yaml`：可回滚配置示例

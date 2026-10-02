# web

只读 Web 黑板在 **Task 11** 落到这里。

本任务（Task 1）只建占位目录。预计包含：

- 后端只读接口：`/web/project/<session_id>` 返回当前 observation 的 state_json + map_text
- 前端：项目概况 / 探索图（基于 `assessments[].dependsOn` 派生边）/ API 台账 / 记录详情
- 所有工具原文与模型文本按数据渲染，不执行 HTML / JS

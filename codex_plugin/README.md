# FlySecAgent · 本地 Codex Plugin

本插件收集**显式开启会话**的 Codex 工具事件，由本地 Memory Curator 整理 API、测试动作、返回和证据，并通过受支持的 Hook 向后续请求交付简短记忆。

## 包含什么

- `plugin.json`：便携插件清单与 Codex 扩展元数据。
- `hooks/hooks.json`：六个被动生命周期 Hook；不拦截或改写主 Agent 工具。
- `scripts/hook_entry.py`：接收 Codex JSON 事件并调用本地适配器。
- `scripts/service.py`：显式 setup / start / status / stop。
- `runtime/`：Python 服务、Hook 模块、Pi 桥源码与 Web 工作台。
- `.agents/plugins/marketplace.json`：本地测试目录清单，不会自动注册到你的账户。

包中不含 API Key、数据库、日志、原始测试记录、node_modules 或本机绝对路径。依赖需要在本机显式安装。

## 环境与私有数据

Python 3.11+、Node 22.19+、npm、macOS/Linux，以及能使用 Hook 的本地 Codex 环境。
默认私有目录为 `~/.local/share/flysecagent-codex`；可用 `FLYSEC_PLUGIN_STATE_DIR` 指定另一目录，setup、服务及 Codex 必须使用同一值。该目录与插件包、原项目数据库分开。

默认端口 `8790`，不接管原项目的 `8787` 服务。也不会在 Hook 内自动安装依赖、启动服务或修改全局 Codex 配置。

## 初始化与启动

从解压后的插件目录运行：

```bash
python3 scripts/service.py setup --key-file /absolute/private/model-api-key
python3 scripts/service.py start
python3 scripts/service.py status
```

setup 会创建私有虚拟环境、安装 Python/npm 依赖并构建 Pi。密钥只通过私有文件路径引用，不复制进包；模型参数仍可通过 `FLYSEC_PI_PROVIDER`、`FLYSEC_PI_MODEL`、`FLYSEC_PI_BASE_URL` 配置。
API Key 文件请设为 0600。可通过 `setup --port <port>` 选择其他本机端口。

工作台：`http://127.0.0.1:8790/web/`。

## 在 Codex 中安装

把当前文件夹作为本地 marketplace 来源，然后安装插件。下面的命令会修改使用者自己的 Codex 安装设置，**本次打包不会代你执行到全局配置**：

```bash
codex plugin marketplace add /absolute/path/to/flysecagent-codex
codex plugin add flysecagent@flysecagent-local
```

安装/启用后必须在 `/hooks` 审查并信任具体定义；插件安装成功不等于 Hook 已可信。桌面端也可从本地来源目录安装。
不要使用绕过 Hook 信任的启动参数。

## 明确开启

在新 Codex 会话中输入：

```text
开始进行测试 https://your-authorized-target.example，记录 API 与测试过程
```

插件用实际 `session_id` 创建项目。普通会话不自动采集。服务必须先启动。
之后查看 API 台账、测试历史与证据；回合结束只整理已有材料，不发起目标测试。

六个事件：SessionStart、UserPromptSubmit、PostToolUse、Stop、SessionEnd、Interrupt。
Codex 的 Bash 非零退出也通过 PostToolUse 到达；插件保留原始响应，不伪造额外失败响应。

## 停止与卸载

```bash
python3 scripts/service.py stop
codex plugin remove flysecagent@flysecagent-local
```

stop 只处理本插件记录且身份匹配的服务 PID，不强杀其他进程。若有正在进行的任务，先结束任务再停服务。
卸载插件不删除私有数据。不要公开私有目录。

## 验证与限制

打包和本机模拟事件验证不等于你的真实 Codex 会话已经接入成功。安装、启用及人工信任后，仍应先用本机合成任务验证真实工具捕获和摘要交付。
只支持宿主实际提供的事件；上游已省略的输出无法恢复。additionalContext 是补充上下文，不原位替换系统提示词，不证明模型一定采纳。

运行数据可能包含凭据和其他敏感信息，开启前应确认保存及向所配置模型发送材料的授权。
仅用于本地环境，不是公开网络部署方案。

参考：[OpenAI 插件打包](https://developers.openai.com/plugins/build/plugins)、[Codex Hooks](https://learn.chatgpt.com/docs/hooks)。
许可见随包 LICENSE；原许可证中的作者/年份信息需发布者核实后补齐，不在本次打包中伪造。

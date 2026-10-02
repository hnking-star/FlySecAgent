# FlySecAgent Hook 接入

已在本机 **Coco / Trae CLI 0.121.1** 实际安装并测试。Codex 尚未做真实安装验证；旧版 `codex-config.example.yaml` 只是历史草稿，不能用作安装说明。

## 安装到一个工作目录

在 FlySecAgent 根目录执行：

```bash
python3 -m hook.install_coco /absolute/path/to/workspace
```

安装器向该目录的 `.trae/hooks.json` 追加 FlySecAgent 条目，保留其他 Hook；已有文件修改前会备份。重复安装不会重复添加。
安装内容有固定的 Python 解释器、代码目录和数据目录，所以从别的目录启动 Coco 也可以找到脚本。

服务地址或数据目录不同，用 `--api-base http://127.0.0.1:8787 --data-dir /absolute/data/path` 指定。

## 使用

先启动后端：

```bash
cd /absolute/path/to/FlySecAgent
python3 -m service
```

Pi 读取 `FLYSEC_PI_API_KEY`，也支持 `FLYSEC_PI_API_KEY_FILE`；本地默认密钥文件是 `data/.deepseek-key`，权限为 0600。密钥只在本地配置，不写入 Hook 或提交到 Git。

安装后可以直接运行普通 `coco`，在对话中输入：

```text
开始进行测试 https://example.com
```

`UserPromptSubmit` Hook 会用 Coco 自动提供的 session ID 创建项目，默认目的为“记录授权测试过程，梳理攻击面、API 与测试结果”，并向当前模型上下文提示观察已经开启。输入内容只在 Hook 进程内用于匹配和提取目标，不会保存到数据库。

需要明确目的时，可以直接接在目标后面：

```text
开始进行测试 https://example.com，重点检查越权与敏感信息泄露
```

也可以在启动前就指定目标和目的：

```bash
python3 -m hook.coco_start --cwd /absolute/path/to/workspace \
  --target "测试目标" --objective "测试目的"
```

也可以在安装目录直接运行：

```bash
FLYSEC_TARGET="测试目标" FLYSEC_OBJECTIVE="测试目的" coco
```

**不用填写 session_id。** Coco 的 `SessionStart` 把真实 ID 交给 Hook，Hook 用这个 ID 创建项目。之后每次工具调用都按运行时提供的 ID 归属。
没有显式指定目标和目的的新会话不会自动开启采集。恢复已开启的会话可以用 `coco --resume=<真实会话ID>`；不用重新创建项目。

若默认模型提示配额用尽，可用 Coco 自带的可用模型，例如 `-c model.name=DeepSeek-V4-Flash`。这只改变主 Agent 的模型，Observer 仍使用已配置的 Pi + DeepSeek。

## 事件做什么

| 事件 | 行为 |
| --- | --- |
| SessionStart | 记录真实会话元信息；有显式目标时创建项目；补交离线记录。 |
| UserPromptSubmit | 恢复活动回合计时；交付未签收的新反馈。不保存聊天内容。 |
| PostToolUse | 保存工具输入与结果，交付新反馈。 |
| PostToolUseFailure | 保存实际工具错误，交付新反馈。 |
| Stop | 标记回合结束并请求后台整理。 |
| SessionEnd | 在异常退出没有 Stop 时补发回合结束；不会重复启动已结束回合。 |

Coco 0.121.1 会把 `hookSpecificOutput.additionalContext` 注入为额外上下文。Hook 不改变主 Agent 的权限或工具结果。

## 离线与反馈

- Hook 共享一次执行的时间预算，HTTP 默认单次 0.5 秒；安装配置给整次 Hook 4 秒上限。
- 服务离线时，已开启会话的工具记录保存在 `data/queue/pending.jsonl`，恢复后补交。
- 队列不按大小删除旧证据；重放次数与耗时有上限，HTTP 请求不持有队列写锁。
- 只有成功入库的条目从队列移除。冲突、关闭会话或失败条目保留供重试或检查。
- 反馈原样交付一次，输出成功后签收；签收表示 Hook 已输出，不证明模型一定采纳，也不承诺崩溃场景 exactly-once。

## 卸载

```bash
python3 -m hook.install_coco /absolute/path/to/workspace --uninstall
```

只移除 FlySecAgent 配置，原始记录、其他 Hook 保留。

## 验证证据

详见 [Coco 实测记录](../docs/details/09-coco-validation.md)。自动化测试使用本地临时数据库；真实 Coco 测试的记录与模型输出保存在该报告指向的独立数据目录。

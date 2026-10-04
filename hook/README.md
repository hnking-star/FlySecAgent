# FlySecAgent Hook 接入

FlySecAgent 使用宿主事件收集工具证据。Coco 0.121.1 旧接入已联调；Traex 0.207.1 支持项目级配置与信任审查。当前记忆协议为 V2；Codex 的旧 YAML 仅为历史草稿，不作为已验证安装说明。

## 安装到一个工作目录

在 FlySecAgent 根目录执行：

```bash
python3 -m hook.install_coco /absolute/path/to/workspace
```

安装器向该目录的 `.trae/hooks.json` 追加 FlySecAgent 条目，保留其他 Hook；已有文件修改前会备份。重复安装不会重复添加。
安装内容有固定的 Python 解释器、代码目录和数据目录，所以从别的目录启动 Coco 也可以找到脚本。

服务地址或数据目录不同，用 `--api-base http://127.0.0.1:8787 --data-dir /absolute/data/path` 指定。

## 使用

### Traex / TraeCode CLI 0.207.1

新版 TraeCode CLI 使用项目级 `.trae/cli/hooks.json`，不再读取旧的 `.trae/hooks.json`。
现有 Coco Hook 可以用当前 Traex 的官方迁移命令复制到新路径，旧文件保留给 Coco：

```bash
cd /absolute/path/to/FlySecAgent
traex migrate hooks --project
```

注意本机的 `traecli` 命令可能仍指向旧 Coco；先用 `traex --version` 核对版本，不要混用。

本机已通过 Traex 0.207.1 的 `hooks/list` 运行态接口确认：六个 FlySecAgent command Hook 均被发现，
source 为 project、路径为 `.trae/cli/hooks.json`，无解析错误。但当时信任状态为 `untrusted`，
因此不能把“配置已迁移”当作“Hook 已执行”。

V2 可直接安装当前项目配置（新增受支持的 Interrupt 收尾事件）：

```bash
python3 -m hook.install_coco /absolute/path/to/workspace --client traex
```

配置变化后需要重新审查信任，不绕过此门槛。安装器保留其他条目并备份旧文件；`--uninstall --client traex` 只移除 FlySec 条目。

迁移后重新进入 Traex，在 `/hooks` 中审查并信任对应的 FlySecAgent 条目，再做本地 fixture 联调。
不要用绕过 Hook 信任的启动参数代替审查。此前仅验证迁移和发现。本轮另有 Hook 子进程事件回归与真实 Pi 本机 fixture，详见 [V2 验收](../docs/10-v2-validation.md)；仍不把这些当作真实 Traex 当前会话采集、信任和主 Agent 采纳已验证。

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

若默认模型提示配额用尽，可用 Coco 自带的可用模型，例如 `-c model.name=DeepSeek-V4-Flash`。这只改变主 Agent 的模型，Memory Curator 仍使用已配置的 Pi + DeepSeek。

## 事件做什么

| 事件 | 行为 |
| --- | --- |
| SessionStart | 记录真实会话元信息；有显式目标时创建项目；补交离线记录。 |
| UserPromptSubmit | 恢复活动回合计时；交付未签收的新反馈。不保存聊天内容。 |
| PostToolUse | 保存工具输入与结果，交付新反馈。 |
| PostToolUseFailure | 保存实际工具错误，交付新反馈。 |
| Stop | 标记回合结束并请求后台整理。 |
| Interrupt（Traex） | 明确中断时结束活动回合并请求已有证据收尾。 |
| SessionEnd | 在异常退出没有 Stop 时补发回合结束；不会重复启动已结束回合。 |

Coco 0.121.1 会把 `hookSpecificOutput.additionalContext` 注入为额外上下文。Hook 不改变主 Agent 的权限或工具结果。

## 离线与反馈

- Hook 共享一次执行的时间预算，HTTP 默认单次 0.5 秒；安装配置给整次 Hook 4 秒上限。
- 服务离线时，已开启会话的工具记录保存在 `data/queue/pending.jsonl`，恢复后补交。并发写锁冲突时，使用私有 `data/queue/spool/` 逐条文件兜底。
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

## V2 记忆与验收

Memory Curator 只使用 curator_read / curator_commit；Python 模型生成工具 Schema。事实有范围，测试的 execution 与 outcome 分离；本地脚本错误不能作为目标阴性结果。

真实 Pi 验收使用本机 HTTP fixture（`scripts/validate_memory_v2.py`），不请求现有靶场。原始数据保持完整，反馈不复制凭据原文。主 Agent 输出约定仅通过已启用会话的补充上下文建议，不修改全局提示词，也不保证模型采纳。

仅显式启用观察才创建项目。支持单行或多行的“开始进行测试 URL”，以及“开始对 URL 进行测试”；引用这句示例的普通对话不会自动开启。

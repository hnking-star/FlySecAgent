# FlySecAgent · Evidence Memory

一个独立的执行证据记忆层。主 Agent 继续用自己的工具完成任务，FlySecAgent 只旁路记录和整理，不执行测试、不改变工具权限。

```text
宿主工具执行 → Hook 收集 → SQLite 原始证据
                            ↓ 每 5 分钟 / 回合结束
                       Pi 记忆整理器
                            ↓ curator_read / curator_commit
                       Python 校验与发布
                            ↓
                 Web 工作台 + 下一轮任务记忆摘要
```

## 当前实现

- Python / FastAPI / SQLite，持久化仍只有 projects、tool_records、observations 三张表。
- 五类记忆：主题、观察/假设、实际测试、API 元数据、未确认问题。
- 事实和测试保留历史，观察修正用新记录；API 复用身份，测试只存一份。
- 原始工具输入和返回完整保存；本地执行错误不等于目标测试未命中。
- 会话隔离、固定证据窗口、离线补交、发布事务与新版本签收。
- 浅色只读工作台：项目搜索筛选、探索图及页面全屏、API 历史、证据回查、历史版本和报告。

当前规格见 [开发结构](docs/README.md) 和 [记忆协议 V2](docs/09-memory-v2.md)。旧 V1 文档明确作为历史材料，不再指导当前实现。

## 本地启动

需要 Python 3.11+ 和能运行当前 Pi SDK 的 Node.js；项目使用 Python 服务与一个小型 TypeScript Pi 桥，不需要 Docker。

```bash
python3 -m pip install -e '.[dev]'
npm ci --prefix pi_ext
python3 -m service.export_schemas
npm run build --prefix pi_ext
python3 -m service
```

打开 <http://127.0.0.1:8787/web/>。服务只绑定本机，网页只读；Hook / Control / Memory 写接口继续鉴权。

Memory Curator 模型继续使用现有配置。可通过 FLYSEC_PI_PROVIDER、FLYSEC_PI_MODEL、FLYSEC_PI_BASE_URL 配置；API Key 使用 FLYSEC_PI_API_KEY_FILE 指向私有文件，或在环境中设置 FLYSEC_PI_API_KEY。不要把真实密钥写入源码或 Hook 配置。

## 宿主 Hook

Traex 项目级安装：

```bash
python3 -m hook.install_coco /absolute/path/to/workspace --client traex
```

安装器保留其他 Hook，修改前备份。配置变化后须由使用者审查宿主 Hook 信任；不绕过权限或信任检查。
Coco 旧版本的安装和事件差异见 [Hook 接入](hook/README.md)。通用 Codex Hook 的历史草稿不代表已完成当前 Codex 桌面运行态适配。

只有显式启用的会话才采集，例如：

```text
开始进行测试 https://your-authorized-target.example
```

反馈通过 additionalContext 交付下一次请求，不原位改写系统提示词，不自动启动主 Agent 回合。最终答复的 [输出约定](docs/agent-output-contract.md) 是建议，不能保证模型一定采纳。

## 验证

```bash
FLYSEC_PI_COMMAND='python3 -u -m service.pi_stub' python3 -m pytest -q
npm run typecheck --prefix pi_ext
node pi_ext/tests/memory-loop.mjs
python3 scripts/validate_memory_v2.py
```

前一项使用显式 stub，验证协议、事务和 Hook 回归，不冒充真实模型解题。后一项启动隔离的本机 HTTP fixture 与真实配置的 Pi 模型，只发送合成测试记录，不请求现有靶场。结果存入私有 data/v2-validation-*。

本轮具体结果和边界见 [本地验收报告](docs/10-v2-validation.md)。本地验收不等于所有 CTF 的解题率或成本已证明优于旧版本。

## 目录

| 目录 | 用途 |
|---|---|
| hook/ | 宿主事件、安装、离线队列与反馈交付 |
| service/ | 采集、存储、调度、协议校验、只读投影与报告 |
| pi_ext/ | 限制为两个记忆工具的 Pi 桥 |
| web/ | 无外部资源的响应式工作台 |
| tests/、scripts/ | 回归测试与真实模型本地验收 |
| docs/ | 当前开发规格、验收及明确标记的历史文档 |
| data/ | 私有运行数据、密钥引用、测试产物和可回滚备份，不提交 Git |

旧 main.py、RAG 和旧版配置是仓库历史功能，本次未删除，也不是当前运行入口。旧 README 收入 [历史归档](docs/archive/README-original.md)。

## 来源与许可

当前业务协议和展示已改为 FlySec 的证据记忆模型，但不能把重构说成“从未参考过其他项目”。[来源说明](SOURCE_NOTES.md) 和现有 LICENSE 保留，第三方依赖继续遵守其许可。

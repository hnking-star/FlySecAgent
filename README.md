# 项目名称: FLYsecAgent

这是一个基于大语言模型和MCP（Model-Controller-Plugin）和Rag架构的网络安全智能助手项目。它旨在通过自然语言交互，帮助用户执行渗透测试任务、查询安全信息、分析流量包等。

## 新版本开发文档

新版本的主要开发架构与已确认约定见 [docs/README.md](docs/README.md)。该文档描述待实现设计，不代表下方现有功能已完成新架构接入。

新版本独立重写，不依赖或迁移旧代码；下方旧版说明仅作为历史资料，不作为新版本实现规格。

- [01 · 项目与会话管理](docs/01-project-session.md)：创建、绑定、恢复、关闭及离线补交。
- [02 · Hook 接入与采集](docs/02-hook-collection.md)：HTTP 管理、工具记录范围、长输出和本机开发版。
- [03 · 执行记录与观察窗口](docs/03-execution-records.md)：单库隔离、去重、固定增量窗口及无新增时的检查。
- [04 · Observer](docs/04-observer.md)：持续 Pi 会话、判断与建议、持续修正及工具日志；工具协议与提示词待定。
- [05 · 黑板状态与 Web 展示](docs/05-blackboard.md)：只读视图、状态和增量合并草案、API 台账。
- [06 · 短反馈与 Hook 交付](docs/06-feedback-hook.md)：短 map、交付事件、版本控制与验收。

## 功能特性

- **自然语言交互**: 用户可以通过自然语言向AI助手提问和下达指令。
- **MCP服务器集成**: 通过 `mcp.json` 配置文件，可以灵活集成和管理多个MCP服务器，扩展助手的能力。
- **工具调用**: AI助手能够根据用户请求，调用配置的MCP服务器提供的工具（例如：nmap, gobuster, fofa, tavily-search等）。
- **对话历史记忆**: 支持多轮对话，能够记住之前的交互内容。
- **流式输出**: AI的回答可以流式输出，提供更好的用户体验。
- **知识库增强 (可选)**: 支持通过本地知识库Rag（`knowledge_base_docs`目录）来增强AI的回答质量。
- **可配置模型**: 支持配置不同的语言模型参数。
- 
  **启动效果**
![屏幕截图 2025-05-11 154031]( https://github.com/hnking-star/FlySecAgent/blob/main/images/%E5%B1%8F%E5%B9%95%E6%88%AA%E5%9B%BE%202025-05-11%20154031.png)

  
  **sqlmap调用效果**:

  ![屏幕截图 2025-05-11 154031](https://github.com/hnking-star/FlySecAgent/blob/main/images/%E5%B1%8F%E5%B9%95%E6%88%AA%E5%9B%BE%202025-05-11%20162500.png)

  ![屏幕截图 2025-05-11 154031](https://github.com/hnking-star/FlySecAgent/blob/main/images/360%E6%88%AA%E5%9B%BE20250511162615.png)
  此外还可以调用dirsearch，nmap等工具

## 安装指南

1.  **克隆仓库**:
    ```bash
    git clone https://github.com/hnking-star/FlySecAgent.git
    cd agent
    ```

2.  **创建并激活虚拟环境** (推荐):
    ```bash
    python -m venv .venv
    ```
    -   Windows:
        ```bash
        .venv\Scripts\acti![屏幕截图 2025-05-11 154031](https://github.com/user-attachments/assets/3991c18f-9e01-41f0-8de2-963ff1d14a87)
vate
        ```
    -   macOS/Linux:
        ```bash
        source .venv/bin/activate
        ```

3.  **安装依赖**:
    ```bash
    pip install -r requirements.txt
    ```

4.  **安装 `uv` (重要)**:
    本项目使用 `uv` 作为 Python 包的运行器和部分场景下的安装器。
    -   `start.bat` 脚本会自动尝试为您安装 `uv`。
    -   如果您希望手动安装或在其他环境中使用，可以运行：
        ```bash
        pip install uv
        ```
        或者参考 `uv` 的官方文档进行安装。
    确保 `uv` 已成功安装并可以从命令行调用。

## 使用方法

1.  **配置MCP服务器**: 
    修改 `mcp.json` 文件，根据您的环境和需求配置MCP服务器。确保每个服务器的启动命令和参数正确无误。例如，您可能需要更新 `TAVILY_API_KEY` 或其他服务器特定的路径/参数。

2.  **准备知识库 (可选)**:
    如果您希望使用知识库增强功能，请将相关的文本文件（例如 `.txt`）放入 `knowledge_base_docs` 文件夹中。

3.  **运行主程序**:
    ```bash
    python main.py
    ```
    程序启动后，您可以根据提示输入您的问题或指令。

## 文件结构

```
agent/
├── .venv/                  # Python虚拟环境 (被.gitignore忽略)
├── knowledge_base_docs/    # 知识库文档存放目录
│   └── ...
├── .gitignore              # Git忽略文件配置
├── main.py                 # 主程序入口
├── mcp.json                # MCP服务器配置文件
├── rag_embedding.py        # RAG嵌入相关 (如果使用)
├── rag_split.py            # RAG文本分割相关 (如果使用)
├── README.md               # 项目说明文件
├── requirements.txt        # Python依赖列表
├── LICENSE                 # 项目许可证
└── ... (其他脚本或配置文件)
```

## 配置文件 (`.env`)
```json
BASE_URL=https://api.302.ai
API_KEY=your_r_api_key
MODEL_NAME=gpt-4o


EMBEDDING_API_KEY=your_embedding_api_key
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```
上面对应的是所用模型api和key，下面对应的是embedding的key和url（使用阿里云的）



## 配置文件 (`mcp.json`)

此文件用于定义AI助手可以连接和使用的MCP服务器。每个服务器条目应包含:
-   `name`: 服务器的唯一名称。
-   `params`: 启动服务器所需的参数，通常包括 `command` 和 `args`。
-   `cache_tools_list`: 是否缓存工具列表。

**MCP示例服务器配置**:

**stdio**
```json
{
  "name": "tavily-search",
  "params": {
    "command": "uv",
    "args": [
      "--directory",
      "F:\\ai\\mcp\\mcp_tool\\mcp-server-tavily",
      "run",
      "tavily-search"
    ],
    "env": {
      "TAVILY_API_KEY": "your_tavily_api_key_here",
      "PYTHONIOENCODING": "utf-8"
    }
  },
  "cache_tools_list": true
}
```
请确保将示例中的路径和API密钥替换为您自己的配置。

**sse**
```json
{"name":"mcpname",
      "url":"http://127.0.0.1:8009/sse"
},
```


## 知识库配置
在knowledge_base_docs添加相应的文件即可







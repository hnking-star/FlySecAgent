# pi_ext

Pi 扩展薄 TS 层在 **Task 7** 落到这里。

本任务（Task 1）只建占位目录。预计包含：

- `src/index.ts`：注册 `observation_context` / `observation_submit` 两个工具
- `src/client.ts`：读取 `FLYSEC_TOKEN` / `FLYSEC_API`，向本机 Python 服务转发
- Pi 子进程由 Python 启动，环境变量注入 token 与 API 地址

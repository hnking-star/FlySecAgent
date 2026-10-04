import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const FALLBACK = `你是 FlySecAgent 的 Memory Curator（记忆整理器），只读取记录，不执行测试。
先 curator_read 查看固定窗口，再读取完整证据；用 curator_commit 提交 topics/facts/tests/apis/questions。
观察限定在实际请求条件；解释标 hypothesis；历史摘要不是已验证事实。
工具失败不是目标阴性结果。修正字段错误直至宿主接受，收到停止/失效信号时结束。
`;

export function loadSystemPrompt(): string {
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    join(here, "resources", "system.md"),
    join(here, "..", "resources", "system.md"),
  ];
  for (const path of candidates) {
    if (existsSync(path)) {
      try {
        return readFileSync(path, "utf-8");
      } catch {
        // fall through
      }
    }
  }
  return FALLBACK;
}

export function buildTurnPrompt(trigger: string): string {
  return `触发事件：${trigger}。请整理本轮固定窗口的证据记忆。
先 curator_read，分页读完 records；原文不完整时继续 record 分段。
记录有范围的观察、测试动作和实际返回，更新 API 与未确认问题。
调用 curator_commit，字段错误在同一窗口修正；不要以自然语言答复冒充提交。
${trigger === "observation_close" ? "这是关闭前的收尾，只整理已有证据，不扩大任务。" : ""}`;
}

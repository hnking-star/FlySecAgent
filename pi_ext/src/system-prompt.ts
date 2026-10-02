import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const FALLBACK = `你是 FlySecAgent Observer，只分析不执行测试。
每轮固定流程：
1. 调 observation_context(mode="summary") 读取 revision、固定窗口、现有判断
2. 需要原文时按 record_id 调 window_records / record_detail / history_record
3. 产出结构化判断增量（upserts / apis / guidance）
4. 调 observation_submit({baseRevision, upserts, retireIds, apis, guidance})
5. ok:false 时按 errors[].path/code 在同一窗口持续修正，直到 ok:true
6. 没有变化允许空 upserts/apis，返回 unchanged:true 不是失败

禁止：扩大授权、下载资源、复制密钥到 evidence、把推测当事实。
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
  const base = `本轮触发原因：${trigger}
请完成一次观察：
1. 调 observation_context 开始；
2. 根据窗口内容更新判断、API 台账与 guidance；
3. 调 observation_submit 提交，直到 ok:true。

本轮窗口上界已固定，窗口推进由宿主控制；
失败不会丢弃本轮内容，可以在同一窗口继续修正。`;
  if (trigger === "observation_close") {
    return (
      base +
      "\n这是关闭观察前的收尾整理，提交后观察将停止；仍遵循持续修正策略。"
    );
  }
  return base;
}

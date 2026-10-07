/**
 * 当前回答内的进度区域：阶段状态 + 本次连接的等待时长。
 * 耗时从本地进入阶段起算，仅代表等待时长，不代表后台进度；
 * 正常完成收起为摘要，等待审批与失败信息保持可见。
 */
import type { AssistantTurn } from "../session";
import { phaseLabel } from "../session";

function seconds(ms: number): string {
  return ms >= 60000
    ? `${Math.floor(ms / 60000)}分${Math.round((ms % 60000) / 1000)}秒`
    : `${(ms / 1000).toFixed(0)}秒`;
}

export function WorkTimeline(
  { turn, now, stalled = false }: { turn: AssistantTurn; now: number; stalled?: boolean },
) {
  const elapsed = Math.max(0, now - turn.phaseAt);
  if (turn.phase === "completed" && turn.done) {
    const { steps, usage, cost, duration_ms } = turn.done;
    return (
      <details className="process done">
        <summary>
          已完成 · 第 {steps} 轮 · {(duration_ms / 1000).toFixed(1)}s
          {usage ? ` · ${usage.prompt_tokens}+${usage.completion_tokens} tok` : ""}
          {cost != null ? ` · ≈¥${cost.toFixed(4)}` : ""}
        </summary>
        <div className="process-body">
          服务端计时 {seconds(duration_ms)}；本地展示计时仅供等待参考。
          {turn.done.trace && (
            <div className="trace">trace：{turn.done.trace}</div>
          )}
        </div>
      </details>
    );
  }
  if (turn.phase === "completed") {
    // 旧会话恢复没有计量数据：只显示已完成，不补造耗时
    return <div className="process done" role="status">已完成</div>;
  }
  if (turn.phase === "max_steps") {
    return (
      <div className="phase warn" role="status">
        达到执行轮次上限，未正常完成
        {turn.done ? ` · 共 ${turn.done.steps} 轮 · ${(turn.done.duration_ms / 1000).toFixed(1)}s` : ""}
      </div>
    );
  }
  if (turn.phase === "failed" || turn.phase === "disconnected") {
    return (
      <div className="phase warn" role="status">
        {phaseLabel(turn)}
        {turn.error ? ` · ${turn.error}` : ""}
      </div>
    );
  }
  return (
    <div className="phase" role="status">
      <span className="phase-text">{phaseLabel(turn)}</span>
      <span className="phase-elapsed">{seconds(elapsed)}</span>
      {stalled && <span className="phase-stalled">连接暂未收到响应，正在检查状态</span>}
    </div>
  );
}

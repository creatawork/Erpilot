import type { ApprovalPendingPayload, ApprovalResolvedPayload, DonePayload,
  SessionState, ToolFinishedPayload } from "./protocol";

export interface ToolItem {
  id: string;
  name: string;
  arguments: string;
  finished: ToolFinishedPayload | null;
  approval: ApprovalPendingPayload | null;
  approvalResolved: ApprovalResolvedPayload | null;
}

export interface Turn {
  role: "user" | "assistant";
  text: string;
  tools: ToolItem[];
  done?: DonePayload;
}

export function hydrateTurns(state: SessionState): Turn[] {
  const turns: Turn[] = [];
  for (const message of state.messages) {
    if (message.role === "user" || message.role === "assistant") {
      turns.push({ role: message.role, text: message.content ?? "", tools:
        (message.tool_calls ?? []).map(call => ({ id: call.id, name: call.function.name,
          arguments: call.function.arguments, finished: null, approval: null, approvalResolved: null })) });
    } else if (message.role === "tool") {
      continue;
    }
  }
  for (const result of state.tool_results) {
    for (const turn of turns) for (const tool of turn.tools) {
      if (tool.id === result.call_id && tool.finished === null) {
        tool.finished = {
          id: result.call_id,
          name: result.name,
          content: result.content,
          ok: result.ok,
        };
      }
    }
  }
  for (const pending of state.pending_approvals) {
    const tool = [...turns].reverse().flatMap(turn => turn.tools)
      .find(item => item.id === pending.call_id && item.finished === null);
    if (tool) {
      tool.approval = pending;
      tool.arguments = JSON.stringify(pending.arguments);
    }
  }
  return turns;
}

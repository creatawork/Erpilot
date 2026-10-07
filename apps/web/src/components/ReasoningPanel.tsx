/**
 * 思考过程面板（设计 5.1）：思考独立于正文，默认折叠、可展开实时阅读；
 * 轮次变化时分别保存。思考内容只保存在本次页面内存，刷新后不恢复。
 * 没有思考内容的轮次不渲染空面板。
 */
export function ReasoningPanel({ step, text }: { step: number; text: string }) {
  return (
    <details className="reasoning">
      <summary>思考过程{step > 0 ? ` · 第 ${step} 轮` : ""}</summary>
      <div className="reasoning-body">{text}</div>
    </details>
  );
}

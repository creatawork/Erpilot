/**
 * 业务结果卡片（设计 5.2）：三类明确契约（库存查询/库存调整/订单创建），
 * 数据取自后端展示适配器的 versioned display 字段——不通过模型正文提取，
 * 不重新计算金额。未知 kind/version 或字段校验失败返回 null，调用方回退通用详情。
 */
import type {
  BusinessDisplay,
  OrderCreationDisplay,
  StockAdjustmentDisplay,
  StockQueryDisplay,
} from "../protocol";

export function isUsableDisplay(display: BusinessDisplay | null | undefined): boolean {
  if (!display || display.version !== 1 || display.outcome !== "succeeded") return false;
  for (const field of ["sku", "message", "error_code", "order_id", "customer", "status"]) {
    const value = (display as unknown as Record<string, unknown>)[field];
    if (value !== undefined && typeof value !== "string") return false;
  }
  switch (display.kind) {
    case "stock_query":
      return Number.isSafeInteger(display.quantity) &&
        (display.available === undefined || typeof display.available === "boolean");
    case "stock_adjustment":
      return Number.isSafeInteger(display.delta) && Number.isSafeInteger(display.quantity_after);
    case "order_creation":
      return typeof display.order_id === "string" && display.order_id.length > 0 &&
        (display.total_amount === undefined ||
          (typeof display.total_amount === "number" && Number.isFinite(display.total_amount)));
    default: return false;
  }
}

const OUTCOME_LABEL: Record<string, string> = {
  succeeded: "成功",
  failed: "失败",
  denied: "未执行",
  unknown: "结果未知",
};

export function BusinessResultCard({ display }: { display: BusinessDisplay }) {
  if (!isUsableDisplay(display)) return null;
  switch (display.kind) {
    case "stock_query": {
      const { sku, quantity, available } = display as StockQueryDisplay;
      if (typeof quantity !== "number") return null;
      return (
        <div className={`card ${display.outcome}`}>
          <div className="card-head">
            <span className="card-title">库存查询{sku ? ` · ${sku}` : ""}</span>
            <Outcome outcome={display.outcome} />
          </div>
          <div className="card-body">
            当前库存 <strong>{quantity}</strong> 件
            {typeof available === "boolean" ? (available ? " · 有货" : " · 无货") : ""}
            {display.error_code ? ` · ${display.error_code}` : ""}
            {display.message ? ` · ${display.message}` : ""}
          </div>
        </div>
      );
    }
    case "stock_adjustment": {
      const { sku, delta, quantity_after } = display as StockAdjustmentDisplay;
      if (typeof delta !== "number" || typeof quantity_after !== "number") return null;
      return (
        <div className={`card ${display.outcome}`}>
          <div className="card-head">
            <span className="card-title">库存调整{sku ? ` · ${sku}` : ""}</span>
            <Outcome outcome={display.outcome} />
          </div>
          <div className="card-body">
            {delta > 0 ? `入库 ${delta}` : `出库 ${-delta}`} 件 ·
            调整后 <strong>{quantity_after}</strong> 件
            {display.message ? ` · ${display.message}` : ""}
          </div>
        </div>
      );
    }
    case "order_creation": {
      const { order_id, customer, status, total_amount } =
        display as OrderCreationDisplay;
      if (!order_id) return null;
      return (
        <div className={`card ${display.outcome}`}>
          <div className="card-head">
            <span className="card-title">订单创建</span>
            <Outcome outcome={display.outcome} />
          </div>
          <div className="card-body">
            订单 <strong>{order_id}</strong>
            {customer ? ` · ${customer}` : ""}
            {status ? ` · ${status}` : ""}
            {typeof total_amount === "number" ? ` · ¥${total_amount.toFixed(2)}` : ""}
            {display.error_code ? ` · ${display.error_code}` : ""}
            {display.message ? ` · ${display.message}` : ""}
          </div>
        </div>
      );
    }
    default:
      return null; // 未知 kind：回退通用结果详情
  }
}

function Outcome({ outcome }: { outcome: string }) {
  return <span className={`card-outcome ${outcome}`}>{OUTCOME_LABEL[outcome] ?? outcome}</span>;
}

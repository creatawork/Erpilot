/**
 * 业务结果卡片（设计 5.2）：三类明确契约（库存查询/库存调整/订单创建），
 * 数据取自后端展示适配器的 versioned display 字段——不通过模型正文提取，
 * 不重新计算金额。未知 kind/version 或字段校验失败返回 null，调用方回退通用详情。
 */
import { Card, Tag } from "antd";
import { CancellationAssessmentCard, isUsableAssessmentDisplay } from './CancellationAssessmentCard';
import type { ReactNode } from "react";
import type {
  BusinessDisplay,
  OrderCreationDisplay,
  StockAdjustmentDisplay,
  StockQueryDisplay,
} from "../protocol";

export function isUsableDisplay(display: BusinessDisplay | null | undefined): boolean {
  if (display?.kind === 'cancellation_assessment') return isUsableAssessmentDisplay(display);
  if (!display || display.version !== 1) return false;
  for (const field of ["sku", "message", "hint", "error_code", "order_id", "customer", "status"]) {
    const value = (display as unknown as Record<string, unknown>)[field];
    if (value !== undefined && typeof value !== "string") return false;
  }
  if ((display.kind==='order_cancellation'||display.kind==='stock_adjustment')&&['failed','denied'].includes(display.outcome))return typeof display.message==='string'&&display.message.length>0;
  if(display.outcome!=="succeeded")return false;
  switch (display.kind) {
    case "stock_query":
      return Number.isSafeInteger(display.quantity) &&
        (display.available === undefined || typeof display.available === "boolean");
    case "stock_adjustment":
      return Number.isSafeInteger(display.delta) && Number.isSafeInteger(display.quantity_after);
    case "order_creation":
    case "order_cancellation":
      return typeof display.order_id === "string" && display.order_id.length > 0 &&
        (display.total_amount === undefined ||
          (typeof display.total_amount === "number" && Number.isFinite(display.total_amount)));
    default: return false;
  }
}

const OUTCOME_LABEL: Record<string, string> = { succeeded: '成功', failed: '失败', denied: '未执行', unknown: '结果未知' };

export function BusinessResultCard({ display }: { display: BusinessDisplay }) {
  if (isUsableAssessmentDisplay(display)) return <CancellationAssessmentCard display={display}/>;
  if (!isUsableDisplay(display)) return null;
  let title = ''; let content: ReactNode;
  if (display.outcome === 'failed' || display.outcome === 'denied') {
    title = display.kind === 'order_cancellation' ? '取消订单' : '库存调整';
    content = <>{display.message}{display.hint && <p>{display.hint}</p>}</>;
  } else {
    switch (display.kind) {
      case 'stock_query': {
        const { sku, quantity, available } = display as StockQueryDisplay;
        title = '库存查询' + (sku ? ' · ' + sku : '');
        content = <>当前库存 <strong>{quantity}</strong> 件{typeof available === 'boolean' ? (available ? ' · 有货' : ' · 无货') : ''}
          {display.error_code ? ' · ' + display.error_code : ''}{display.message ? ' · ' + display.message : ''}</>;
        break;
      }
      case 'stock_adjustment': {
        const { sku, delta, quantity_after } = display as StockAdjustmentDisplay;
        title = '库存调整' + (sku ? ' · ' + sku : '');
        content = <>{delta > 0 ? '入库 ' + delta : '出库 ' + -delta} 件 · 调整后 <strong>{quantity_after}</strong> 件
          {display.message ? ' · ' + display.message : ''}</>;
        break;
      }
      case 'order_creation':
      case 'order_cancellation': {
        const { order_id, customer, status, total_amount } = display as unknown as OrderCreationDisplay;
        title = display.kind === 'order_creation' ? '订单创建' : '取消订单';
        content = <>订单 <strong>{order_id}</strong>{customer ? ' · ' + customer : ''}{status ? ' · ' + status : ''}
          {typeof total_amount === 'number' ? (display.kind === 'order_cancellation' ? ' · 原单金额 ¥' : ' · ¥') + total_amount.toFixed(2) : ''}
          {display.error_code ? ' · ' + display.error_code : ''}{display.message ? ' · ' + display.message : ''}</>;
        break;
      }
      default: return null;
    }
  }
  return <Card size="small" className={'card ' + display.outcome} title={<span className="card-title">{title}</span>}
    extra={<Tag color={display.outcome === 'succeeded' ? 'success' : display.outcome === 'failed' ? 'error' : undefined}>{OUTCOME_LABEL[display.outcome] ?? display.outcome}</Tag>}>
    <div className="card-body">{content}</div>
  </Card>;
}

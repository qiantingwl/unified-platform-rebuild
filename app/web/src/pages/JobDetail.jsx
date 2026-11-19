import React from "react";
import { Badge, Button, Notice, Table, tm, labels, useApp } from "../ui";
import { useQuery } from "../query";
import { Modal } from "../components/Management";
const reasons = {
  scheduled_backoff: "等待计划时间 / 退避",
  awaiting_claim: "等待任务执行器领取",
  credential_version_changed: "凭证版本已改变",
  lease_expired_pending_recovery: "租约到期，等待恢复核对",
  executing: "正在执行",
  receipt_pending: "等待回执核对",
};
export const waitReason = (v) => reasons[v] || labels[v] || v || "—";
export default function JobDetail({ id, onClose }) {
  const { data } = useApp();
  const query = useQuery(id ? `/api/jobs/${id}` : null, data.revision),
    j = query.result;
  return (
    <Modal
      open={!!id}
      onOpenChange={(v) => !v && onClose()}
      title="任务详情"
      description="区分排队、执行和待回执，历史结果不替代本次结果"
    >
      {query.error ? (
        <Notice>
          {query.error}
          <div className="row wrap topgap">
            <Button onClick={query.reload}>重新读取</Button>
          </div>
        </Notice>
      ) : null}
      {j ? (
        <>
          <div className="kvs">
            {[
              ["账户", j.account_display_name || j.label],
              ["身份", j.identity_hint],
              ["任务", labels[j.kind] || j.kind],
              ["状态", <Badge status={j.status} />],
              ["任务ID", j.id],
              [
                "凭证版本",
                `v${j.credential_version}${j.credential_current === false ? "（已过期）" : ""}`,
              ],
              ["安排时间", tm(j.created_at)],
              ["本次开始", tm(j.execution_started_at)],
              ["完成时间", tm(j.finished_at)],
              ["下次执行", tm(j.next_action_at)],
              [
                "排队时长",
                j.queue_age_seconds == null
                  ? "未记录"
                  : `${Math.round(j.queue_age_seconds)} 秒`,
              ],
              [
                "执行时长",
                j.execution_seconds == null
                  ? "未开始"
                  : `${Math.round(j.execution_seconds)} 秒`,
              ],
              ["等待原因", waitReason(j.wait_reason)],
              ["回执", j.receipt_pending ? "待核对" : "无待核对回执"],
              [
                "结果",
                labels[j.error_code || j.result_code] ||
                  j.error_code ||
                  j.result_code ||
                  "尚未结束",
              ],
            ].map(([k, v]) => (
              <div className="kv" key={k}>
                <span>{k}</span>
                <b>{v ?? "—"}</b>
              </div>
            ))}
          </div>
          <h3 className="topgap">尝试记录</h3>
          <Table
            heads={["次数", "开始", "结束", "结果"]}
            rows={(j.attempts || []).map((a) => [
              a.number,
              tm(a.started_at),
              tm(a.finished_at),
              labels[a.outcome] || a.outcome || "进行中",
            ])}
          />
        </>
      ) : query.error ? null : query.loading ? (
        <div className="noresults">正在读取任务…</div>
      ) : (
        <div className="noresults" data-testid="job-detail-empty">
          未找到该任务，可能已被清理或还未生成。
        </div>
      )}
    </Modal>
  );
}

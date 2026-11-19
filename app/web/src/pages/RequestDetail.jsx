import React from "react";
import { useRecords } from "../use-records";
import { tm, labels, Notice, Table, Panel, useApp, Button } from "../ui";

const selectionLabels = {
  strategy: "选择顺序",
  active_before: "选择时已占用",
  concurrency: "账户并发",
  credential_version: "凭证版本",
  balance_observed_at: "余额观察时间",
  account_id: "账户ID",
  group_id: "账户组",
  route_version: "路由版本",
  platform: "平台",
  affinity_hit: "会话亲和命中",
  affinity: "会话亲和",
  priority: "优先级",
  candidate_count: "候选账户数",
  selected_account_id: "所选账户",
  queue_wait_seconds: "并发等待秒数",
  reason: "选择原因",
  upstream_model: "上游模型",
};
export default function RequestDetail({ id }) {
  const { setDrawer } = useApp();
  const [result, error] = useRecords(`/api/requests/${encodeURIComponent(id)}`);
  if (error) return <Notice>{error}</Notice>;
  if (!result) return <Notice>正在读取请求记录…</Notice>;
  const r = result.request;
  return (
    <>
      <div className="kvs">
        {[
          ["请求 ID", r.id],
          ["平台 / 模型", `${r.platform} / ${r.model}`],
          ["状态", labels[r.status] || r.status],
          ["开始", tm(r.started_at)],
          ["结束", tm(r.ended_at)],
          ["快速占位", tm(r.placeholder_at)],
          ["有效输出", tm(r.first_content_at)],
          ["错误原因", labels[r.error_code] || r.error_code || "无"],
          ["输入 Token", r.input_tokens ?? "未上报"],
          ["缓存 Token", r.cached_tokens ?? "未上报"],
          ["输出 Token", r.output_tokens ?? "未上报"],
          ["推理 Token", r.reasoning_tokens ?? "未上报"],
          ["合计 Token", r.total_tokens ?? "未上报"],
          ["密钥归属", r.key_id || "未记录"],
          [
            "客户端来源",
            r.client_source && r.client_source !== "unknown"
              ? r.client_source
              : "未记录",
          ],
        ].map(([name, value]) => (
          <div className="kv" key={name}>
            <span>{name}</span>
            <b style={{ overflowWrap: "anywhere" }}>{value}</b>
          </div>
        ))}
      </div>
      <Panel name="账户尝试记录" className="topgap">
        <Table
          heads={[
            "账户 / 身份",
            "凭证版本",
            "开始 / 结束",
            "上游响应 / 首原始帧",
            "结果",
            "选择依据",
          ]}
          rows={result.attempts.map((a) => [
            <Button
              variant="link"
              onClick={() => setDrawer({ type: "account", id: a.account_id })}
            >
              {a.account_display_name || a.account_id.slice(0, 8)}
              <small>{a.identity_hint}</small>
            </Button>,
            `v${a.credential_version}`,
            <>
              {tm(a.started_at)}
              <small>{tm(a.ended_at)}</small>
            </>,
            <>
              {tm(a.upstream_response_at)}
              <small>{tm(a.first_frame_at)}</small>
            </>,
            labels[a.code] || a.code || "在途",
            a.selection_data ? (
              <div>
                {Object.entries(a.selection_data)
                  .filter(([k]) => k in selectionLabels)
                  .map(([k, v]) => (
                    <small key={k}>
                      {selectionLabels[k]}：
                      {v == null
                        ? "未记录"
                        : typeof v === "object"
                          ? "已记录"
                          : String(v)}
                    </small>
                  ))}
              </div>
            ) : (
              "未记录"
            ),
          ])}
        />
      </Panel>
    </>
  );
}

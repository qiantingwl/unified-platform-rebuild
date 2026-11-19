import React, { useState } from "react";
import { useApp, Button, Table, Notice, tm } from "../ui";
import { Pagination } from "../components/Management";
import { useQuery } from "../query";
const kinds = {
  proxy_binding: "账户固定绑定",
  proxy_pool: "代理池",
  proxy_route: "平台默认出口",
  maintenance_proxy_route: "维护出口",
  proxy_affinity: "账户池亲和",
  registration: "注册/重登会话",
  registration_batch: "注册批次",
};
export function ProxyReferences({ node }) {
  const { data } = useApp();
  const [page, setPage] = useState(1);
  const query = useQuery(
    `/api/proxy-directory/${node.id}/references?page=${page}&page_size=25`,
    data.revision,
  );
  return (
    <>
      {query.error && <Notice>{query.error}</Notice>}
      <p>
        “{node.name}
        ”的出口用途。注册历史可能保留已结束的引用；删除保护只针对仍需使用出口的会话。
      </p>
      <Table
        heads={["用途", "名称 / 关联ID", "状态"]}
        rows={(query.result?.items || []).map((r) => [
          kinds[r.kind] || r.kind,
          r.name,
          r.status || "已配置",
        ])}
      />
      <Pagination
        page={page}
        pageSize={25}
        total={query.result?.total}
        loading={query.loading}
        onPage={setPage}
      />
    </>
  );
}
export function ProxyBatches() {
  const { data, mutate } = useApp();
  const [page, setPage] = useState(1),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const query = useQuery(
    `/api/proxy-target-batches?page=${page}&page_size=25`,
    data.revision,
  );
  async function cancel(id) {
    setBusy(true);
    setError("");
    try {
      await mutate(
        `/api/proxy-target-batches/${id}/cancel`,
        "POST",
        {},
        "已停止该批次的后续检测。",
      );
      query.reload();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      {(error || query.error) && <Notice>{error || query.error}</Notice>}
      <p>
        取消后不再调度后续节点，已经发出的最多两项检查会自然结束；已取得的真实目标结果仍保留。
      </p>
      <Table
        heads={["创建时间", "进度", "状态", "结果计数", "操作"]}
        rows={(query.result?.batches || []).map((b) => [
          tm(b.data.created_at),
          `${b.data.cursor || 0} / ${b.data.total || 0}`,
          {
            queued: "排队中",
            running: "执行中",
            completed: "已完成",
            cancelled: "已取消",
          }[b.data.status] || b.data.status,
          Object.entries(b.data.counts || {})
            .map(([code, count]) => `${code}: ${count}`)
            .join("、") || "等待结果",
          <Button
            disabled={busy || !["queued", "running"].includes(b.data.status)}
            onClick={() => cancel(b.id)}
          >
            停止后续检测
          </Button>,
        ])}
      />
      <Pagination
        page={page}
        pageSize={25}
        total={query.result?.total}
        loading={query.loading}
        onPage={setPage}
      />
    </>
  );
}

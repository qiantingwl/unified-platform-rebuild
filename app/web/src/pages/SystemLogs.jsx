import React, { useEffect, useState } from "react";
import {
  useApp,
  Head,
  Table,
  Tabs,
  Notice,
  Button,
  tm,
  labels,
  eventLabels,
} from "../ui";
import { useQuery, useDebounced, queryString } from "../query";
import { Pagination, Modal, AccountPicker } from "../components/Management";
import JobDetail from "./JobDetail";
export default function SystemLogs() {
  const { data, setDrawer } = useApp();
  const [q, setQ] = useState(""),
    [component, setComponent] = useState(""),
    [account, setAccount] = useState(""),
    [hours, setHours] = useState(24),
    [page, setPage] = useState(1),
    [selected, setSelected] = useState(null),
    [jobDetail, setJobDetail] = useState(null);
  const search = useDebounced(q),
    end = Math.floor(data.as_of / 60) * 60 + 60,
    start = end - hours * 3600;
  useEffect(() => setPage(1), [search, component, account, hours]);
  // 事件模型没有严重级字段，后端也不支持按级别过滤；不发送假的 level 参数。
  const query = useQuery(
    `/api/system-events?${queryString({ q: search, component, account_id: account, start, end, page, page_size: 25 })}`,
    data.revision,
  );
  return (
    <>
      <Head
        name="系统日志"
        desc="查看任务、账户、配置与服务事件；敏感材料和请求正文不展示。"
      />
      <Tabs group="logs" />
      <div className="toolbar">
        <input
          aria-label="搜索系统日志"
          placeholder="事件码 / 关联ID"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select
          aria-label="事件组件"
          value={component}
          onChange={(e) => setComponent(e.target.value)}
        >
          <option value="">全部组件</option>
          {[
            "account",
            "job",
            "registration",
            "request",
            "settings",
            "proxy",
            "admin",
          ].map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
        <select
          aria-label="事件时间范围"
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
        >
          {[1, 24, 168, 720].map((h) => (
            <option key={h} value={h}>
              最近 {h === 168 ? "7天" : h === 720 ? "30天" : `${h}小时`}
            </option>
          ))}
        </select>
        <AccountPicker
          value={account}
          onChange={setAccount}
          label="筛选关联账户"
        />
      </div>
      {query.error && (
        <Notice>
          {query.error}
          <Button onClick={query.reload}>重试</Button>
        </Notice>
      )}
      <div className="panel">
        <Table
          heads={["时间", "组件", "事件", "关联账户 / 任务", "操作"]}
          rows={(query.result?.items || []).map((e) => [
            tm(e.created_at),
            e.component || e.kind,
            eventLabels[e.code] || labels[e.code] || e.code,
            <>
              {e.account_display_name || e.account_id || "—"}
              <small>{e.job_id || e.request_id}</small>
            </>,
            <Button onClick={() => setSelected(e)}>详情</Button>,
          ])}
        />
        <Pagination
          page={page}
          pageSize={25}
          total={query.result?.total}
          loading={query.loading}
          onPage={setPage}
        />
      </div>
      <Modal
        open={!!selected}
        onOpenChange={(v) => !v && setSelected(null)}
        title="事件详情"
        description="按持久化事件追溯关联操作"
      >
        {selected && (
          <>
            <div className="kvs">
              {[
                ["事件ID", selected.id],
                ["时间", tm(selected.created_at)],
                ["组件", selected.component || selected.kind],
                ["事件码", selected.code],
                ["账户", selected.account_display_name || selected.account_id],
                ["任务ID", selected.job_id],
                ["请求ID", selected.request_id],
                ["版本", selected.revision],
              ].map(([k, v]) => (
                <div className="kv" key={k}>
                  <span>{k}</span>
                  <b>{v ?? "—"}</b>
                </div>
              ))}
            </div>
            <div className="row wrap topgap">
              {selected.job_id && (
                <Button
                  onClick={() => {
                    const id = selected.job_id;
                    setSelected(null);
                    setJobDetail(id);
                  }}
                >
                  任务详情
                </Button>
              )}
              {selected.account_id && (
                <Button
                  onClick={() => {
                    setSelected(null);
                    setDrawer({ type: "account", id: selected.account_id });
                  }}
                >
                  账户详情
                </Button>
              )}
              {selected.request_id && (
                <Button
                  onClick={() => {
                    setSelected(null);
                    setDrawer({ type: "request", id: selected.request_id });
                  }}
                >
                  请求详情
                </Button>
              )}
            </div>
          </>
        )}
      </Modal>
      <JobDetail id={jobDetail} onClose={() => setJobDetail(null)} />
    </>
  );
}

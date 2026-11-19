import React, { useState } from "react";
import { useApp, tm, Badge, Button, Head, Table, Tabs, Notice } from "../ui";
import { useRecords } from "../use-records";
import { AccountPicker } from "../components/Management";
import { useDebounced } from "../query";
export default function Logs() {
  const { data, setDrawer } = useApp();
  const [drill] = useState(() => {
    try {
      const value = JSON.parse(
        sessionStorage.getItem("request-log-filters") || "null",
      );
      sessionStorage.removeItem("request-log-filters");
      return value;
    } catch {
      return null;
    }
  });
  const [drillWindow, setDrillWindow] = useState(
    drill && Number.isFinite(drill.start) && Number.isFinite(drill.end)
      ? { start: drill.start, end: drill.end }
      : null,
  );
  const [keyId, setKeyId] = useState(drill?.key_id || ""),
    [clientSource, setClientSource] = useState(drill?.client_source || "");
  const [account, setAccount] = useState(drill?.account_id || ""),
    [model, setModel] = useState(drill?.model || "");
  const searchModel = useDebounced(model);
  const [platform, setPlatform] = useState(drill?.platform || ""),
    [query, setQuery] = useState(""),
    [hours, setHours] = useState(24),
    [basis, setBasis] = useState(drill?.basis || "start"),
    [status, setStatus] = useState(""),
    [offset, setOffset] = useState(0);
  const searchQuery = useDebounced(query);
  const end = drillWindow?.end ?? Math.floor(data.as_of / 15) * 15 + 15,
    start = drillWindow?.start ?? end - hours * 3600;
  const params = new URLSearchParams({
    start,
    end,
    basis,
    platform,
    status,
    request_id: searchQuery,
    account_id: account,
    key_id: keyId,
    client_source: clientSource,
    model: searchModel,
    offset,
    limit: 50,
  });
  const [result, error, reload] = useRecords(`/api/requests?${params}`);
  const rows = result?.rows || [],
    counts = result?.counts || {};
  function change(set, value) {
    set(value);
    setOffset(0);
  }
  const duration = (a, b) =>
    a != null && b != null ? (b - a).toFixed(2) + " s" : "—";
  return (
    <>
      <Head
        name="请求日志"
        desc="按完整时间窗口查询数据库，换号尝试保留在同一请求明细中。"
      />
      <Tabs group="logs" />
      {error && (
        <Notice>
          {error}
          <Button onClick={reload}>重试</Button>
        </Notice>
      )}
      <div className="compactmetrics">
        {[
          ["窗口请求", result?.total ?? "—"],
          ["成功", result ? counts.succeeded || 0 : "—"],
          [
            "失败",
            result ? (counts.failed || 0) + (counts.cancelled || 0) : "—",
          ],
          ["在途", result ? counts.inflight || 0 : "—"],
        ].map(([name, n]) => (
          <span key={name}>
            {name}
            <b>{n}</b>
          </span>
        ))}
      </div>
      <div className="toolbar">
        <input
          aria-label="请求 ID"
          placeholder="按请求 ID 前缀查找"
          value={query}
          onChange={(e) => change(setQuery, e.target.value)}
        />
        <select
          aria-label="日志平台"
          value={platform}
          onChange={(e) => change(setPlatform, e.target.value)}
        >
          <option value="">全部平台</option>
          {data.platforms.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
        <select
          aria-label="日志时间范围"
          value={drillWindow ? "custom" : hours}
          onChange={(e) => {
            setDrillWindow(null);
            change(setHours, Number(e.target.value));
          }}
        >
          {drillWindow && <option value="custom">来自报表的时间范围</option>}
          <option value={1}>最近 1 小时</option>
          <option value={24}>最近 24 小时</option>
          <option value={168}>最近 7 天</option>
          <option value={720}>最近 30 天</option>
        </select>
        <select
          aria-label="日志时间口径"
          value={basis}
          onChange={(e) => change(setBasis, e.target.value)}
        >
          <option value="start">按开始时间</option>
          <option value="end">按结束时间</option>
        </select>
        <select
          aria-label="日志结果"
          value={status}
          onChange={(e) => change(setStatus, e.target.value)}
        >
          <option value="">全部结果</option>
          <option value="succeeded">成功</option>
          <option value="failed">失败</option>
          <option value="cancelled">已取消</option>
          <option value="inflight">在途</option>
        </select>
        <input
          aria-label="请求模型"
          placeholder="模型名称"
          value={model}
          onChange={(e) => change(setModel, e.target.value)}
        />
        <AccountPicker
          platform={platform}
          value={account}
          onChange={(v) => change(setAccount, v)}
          label="筛选请求账户"
        />
      </div>
      {(keyId || clientSource) && (
        <div className="toolbar">
          <span>
            来源范围：
            {keyId
              ? `API Key ${keyId === "unknown" ? "未记录密钥" : keyId}`
              : ""}{" "}
            {clientSource
              ? `客户端 ${clientSource === "unknown" ? "未记录" : clientSource}`
              : ""}
          </span>
          <Button
            onClick={() => {
              setKeyId("");
              setClientSource("");
              setOffset(0);
            }}
          >
            清除来源筛选
          </Button>
        </div>
      )}
      <p className="quietnote">
        {start === 0 ? "全部历史" : tm(start)} 至 {tm(end)} ·
        按结束时间查询不包含尚未结束的请求。
      </p>
      <div className="panel">
        <Table
          heads={[
            "请求 / 时间",
            "平台 / 模型",
            "结果",
            "占位",
            "有效内容",
            "耗时",
            "输入 / 输出 Token",
            "缓存 Token",
            "操作",
          ]}
          rows={rows.map((r) => [
            <>
              {r.id.slice(0, 8)}
              <small>{tm(r.started_at)}</small>
            </>,
            <>
              {r.platform}
              <small>{r.model}</small>
            </>,
            <Badge status={r.status} />,
            duration(r.started_at, r.placeholder_at),
            duration(r.started_at, r.first_content_at),
            duration(r.started_at, r.ended_at),
            `${r.input_tokens ?? "未上报"} / ${r.output_tokens ?? "未上报"}`,
            r.cached_tokens ?? "未上报",
            <Button
              variant="link"
              onClick={() => setDrawer({ type: "request", id: r.id })}
            >
              详情
            </Button>,
          ])}
        />
      </div>
      <div className="row wrap topgap">
        <Button
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - 50))}
        >
          上一批
        </Button>
        <span className="muted">
          数据库第 {result?.total ? offset + 1 : 0}—
          {Math.min(offset + rows.length, result?.total || 0)} 条，共{" "}
          {result?.total ?? "—"} 条
        </span>
        <Button
          disabled={!result || offset + 50 >= result.total}
          onClick={() => setOffset(offset + 50)}
        >
          下一批
        </Button>
      </div>
    </>
  );
}

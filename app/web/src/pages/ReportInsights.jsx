import React, { useState, useEffect } from "react";
import { Panel, Table, Button, Notice, nf, tm, useApp } from "../ui";
import { useQuery, queryString } from "../query";
import { Modal, Pagination } from "../components/Management";
const number = (v) => (v == null ? "未上报" : nf(v));
const percent = (v, d) =>
  v == null || !d ? "无样本" : `${((100 * v) / d).toFixed(1)}%`;
const sourceName = (r, kind) =>
  kind === "clients" && (!r.name || r.name === "unknown")
    ? "未记录客户端"
    : r.name;
export function openReportRequests(params) {
  sessionStorage.setItem("request-log-filters", JSON.stringify(params));
  location.hash = "logs";
}
function TimeSeries({
  rows = [],
  metric = "total_tokens",
  type = "line",
  title = "用量趋势",
}) {
  const observed = rows.filter((r) => r[metric] != null).map((r) => r[metric]);
  const peak = observed.length ? Math.max(...observed) : null;
  const max = Math.max(1, peak ?? 0);
  const x = (i) => 40 + (i * 840) / Math.max(1, rows.length - 1),
    y = (v) => 175 - (145 * v) / max;
  const path = rows
    .map((r, i) =>
      r[metric] == null
        ? ""
        : `${!i || rows[i - 1][metric] == null ? "M" : "L"}${x(i)},${y(r[metric])}`,
    )
    .join(" ");
  return rows.length ? (
    <div className="chart-frame">
      <svg
        className="trend-chart"
        viewBox="0 0 920 225"
        role="img"
        aria-label={title}
      >
        <line x1="40" x2="880" y1="175" y2="175" stroke="var(--line)" />
        <text x="40" y="15" fill="var(--muted)" fontSize="12">
          {metric === "cache_ratio"
            ? "缓存读取占有效样本输入比例"
            : metric === "total"
              ? "请求数"
              : "已上报Token"}{" "}
          · 峰值{" "}
          {metric === "cache_ratio"
            ? `${(100 * (peak ?? 0)).toFixed(1)}%`
            : number(peak)}
        </text>
        {type === "line" && (
          <path d={path} fill="none" stroke="var(--blue)" strokeWidth="2" />
        )}
        {rows.map((r, i) =>
          r[metric] == null ? null : type === "bar" ? (
            <rect
              key={i}
              x={x(i) - Math.min(12, 300 / rows.length)}
              y={y(r[metric])}
              width={Math.min(24, 600 / rows.length)}
              height={Math.max(1, 175 - y(r[metric]))}
              fill="var(--blue)"
            >
              <title>
                {tm(r.start)} · {number(r[metric])}
              </title>
            </rect>
          ) : (
            <circle
              key={i}
              cx={x(i)}
              cy={y(r[metric])}
              r="3"
              fill="var(--blue)"
            >
              <title>
                {tm(r.start)} ·{" "}
                {metric === "cache_ratio"
                  ? `${(r[metric] * 100).toFixed(1)}%`
                  : number(r[metric])}
              </title>
            </circle>
          ),
        )}
        <text x="40" y="210" fill="var(--muted)" fontSize="12">
          {tm(rows[0].start)}
        </text>
        <text
          x="880"
          y="210"
          textAnchor="end"
          fill="var(--muted)"
          fontSize="12"
        >
          {tm(rows.at(-1).start)}
        </text>
      </svg>
    </div>
  ) : (
    <div className="noresults">当前范围没有已记录样本</div>
  );
}
function Activity({ params }) {
  const { data } = useApp();
  const end = Math.floor((data.as_of + 28800) / 86400) * 86400 - 28800 + 86400;
  const query = useQuery(
    `/api/analytics?${queryString({ ...params, start: end - 365 * 86400, end, all_time: undefined })}`,
    data.revision,
  );
  const rows = query.result?.activity || [],
    lookup = new Map(rows.map((r) => [r.day, r]));
  const endDay = Math.floor((end + 28800) / 86400) - 1,
    max = Math.max(1, ...rows.map((r) => r.total_tokens || 0));
  return (
    <Panel
      name="活跃热力图"
      desc="近365天，不随上方日期变化；保留平台、模型、账户范围。"
    >
      {query.error && <Notice>{query.error}</Notice>}
      <div className="pad">
        <p className="quietnote">
          {query.loading
            ? "正在读取…"
            : `${rows.filter((r) => r.total > 0).length} 天有调用 · 当前连续 ${query.result?.summary?.current_streak ?? 0} 天 · 已上报 ${number(query.result?.summary?.total_tokens)} Token`}
        </p>
        <div className="heat-wrap">
          <div
            className="heat-grid"
            role="img"
            aria-label="近365天每日调用活跃度，详细读数见下方表格"
          >
            {Array.from({ length: 365 }, (_, i) => {
              const d = endDay - 364 + i,
                r = lookup.get(d),
                v = r?.total_tokens ?? 0,
                level = !r
                  ? 0
                  : v <= 0
                    ? 1
                    : Math.max(1, Math.ceil((4 * v) / max));
              return (
                <span
                  key={d}
                  className={`heat-cell heat-${level}`}
                  title={`${new Date(d * 86400000).toISOString().slice(0, 10)}：${r?.total ?? 0} 请求，${r ? number(r.total_tokens) : "0"} Token`}
                />
              );
            })}
          </div>
        </div>
        <div className="heat-legend">
          <span>少</span>
          {[0, 1, 2, 3, 4].map((n) => (
            <i key={n} className={`heat-cell heat-${n}`} />
          ))}
          <span>多 · 悬停查看每日读数</span>
        </div>
        <details>
          <summary>查看有调用日期明细</summary>
          <Table
            heads={["日期", "请求", "成功", "Token"]}
            rows={rows.map((r) => [
              tm(r.start),
              number(r.total),
              number(r.succeeded),
              number(r.total_tokens),
            ])}
          />
        </details>
      </div>
    </Panel>
  );
}
function CachePanels({ params }) {
  const { data } = useApp();
  const query = useQuery(
    `/api/analytics?${queryString({ ...params, start: params.end - 86400, all_time: undefined })}`,
    data.revision,
  );
  const result = query.result;
  return (
    <>
      <Panel
        name="缓存分时窗口"
        desc="截至所选结束时刻的1/6/24小时；仅有效缓存上报样本参与比例，历史缺字段不反推。"
      >
        {query.error && <Notice>{query.error}</Notice>}
        <div className="cache-window-grid">
          {(result?.cache_windows || []).map((r) => (
            <div key={r.hours}>
              <span>最近 {r.hours} 小时</span>
              <strong>
                {r.ratio == null ? "无样本" : `${(100 * r.ratio).toFixed(1)}%`}
              </strong>
              <small>
                缓存读取 {number(r.cached_tokens)} / 输入{" "}
                {number(r.input_tokens)}
              </small>
              <small>
                已上报 {nf(r.reported_requests)} · 未上报{" "}
                {nf(r.missing_requests)}
              </small>
            </div>
          ))}
        </div>
      </Panel>
      <Panel
        name="近24小时缓存趋势"
        desc="北京时间分时；折线为有效样本缓存比例，缺字段保留断点。"
      >
        <TimeSeries
          rows={result?.trend}
          metric="cache_ratio"
          title="24小时缓存比例"
        />
        <details className="pad">
          <summary>查看分时缓存与Token明细</summary>
          <Table
            heads={[
              "时间",
              "请求",
              "总Token",
              "输入 / 缓存读取",
              "缓存比例",
              "上报 / 缺失",
            ]}
            rows={(result?.trend || []).map((r) => [
              tm(r.start),
              number(r.total),
              number(r.total_tokens),
              `${number(r.cache_input_tokens)} / ${number(r.cached_tokens)}`,
              percent(r.cached_tokens, r.cache_input_tokens),
              `${nf(r.cache_reported_requests)} / ${nf(r.cache_missing_requests)}`,
            ])}
          />
        </details>
      </Panel>
    </>
  );
}
function SourcePicker({ kind, params, onSelect, onClose }) {
  const { data } = useApp();
  const [page, setPage] = useState(1);
  const query = useQuery(
    `/api/analytics/sources?${queryString({ ...params, kind, page, page_size: 25, sort: "total_tokens" })}`,
    data.revision,
  );
  return (
    <Modal
      open
      onOpenChange={(v) => !v && onClose()}
      title={kind === "keys" ? "选择统计密钥" : "选择统计客户端"}
      description="从当前统计范围按服务器分页选择，不读取密钥原文。"
    >
      {query.error && <Notice>{query.error}</Notice>}
      <Table
        heads={["名称", "请求", "总Token", "操作"]}
        rows={(query.result?.items || []).map((r) => [
          sourceName(r, kind),
          number(r.total),
          number(r.total_tokens),
          <Button
            onClick={() => {
              onSelect({
                id:
                  kind === "keys"
                    ? r.key_id || "unknown"
                    : r.client_source || "unknown",
                name: sourceName(r, kind),
              });
              onClose();
            }}
          >
            选择
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
    </Modal>
  );
}
function DailyTrend({ params, models = [] }) {
  const { data } = useApp();
  const [dimension, setDimension] = useState("models"),
    [metric, setMetric] = useState("total_tokens"),
    [type, setType] = useState("bar"),
    [selected, setSelected] = useState(null),
    [picker, setPicker] = useState(false);
  const query = useQuery(
    `/api/analytics?${queryString({ ...params, ...(selected ? { [dimension === "keys" ? "key_id" : dimension === "clients" ? "client_source" : "model"]: selected.id } : {}) })}`,
    data.revision,
  );
  const rows = query.result?.activity || [];
  return (
    <Panel
      name="每日用量趋势"
      desc="按日统计，可选择模型、客户端或API Key；来源未记录与零用量分别保留。"
    >
      <div className="pad">
        <div className="toolbar">
          <select
            aria-label="趋势统计指标"
            value={metric}
            onChange={(e) => setMetric(e.target.value)}
          >
            <option value="total_tokens">Token用量</option>
            <option value="total">请求数</option>
          </select>
          <select
            aria-label="趋势图表类型"
            value={type}
            onChange={(e) => setType(e.target.value)}
          >
            <option value="bar">柱状</option>
            <option value="line">折线</option>
          </select>
          <select
            aria-label="趋势统计维度"
            value={dimension}
            onChange={(e) => {
              setDimension(e.target.value);
              setSelected(null);
            }}
          >
            <option value="models">模型</option>
            <option value="clients">客户端</option>
            <option value="keys">API Key</option>
          </select>
          {dimension === "models" ? (
            <select
              aria-label="趋势模型"
              value={selected?.id || ""}
              onChange={(e) =>
                setSelected(
                  e.target.value
                    ? { id: e.target.value, name: e.target.value }
                    : null,
                )
              }
            >
              <option value="">全部模型</option>
              {models.map((m) => (
                <option key={m.name}>{m.name}</option>
              ))}
            </select>
          ) : (
            <Button onClick={() => setPicker(true)}>
              {selected?.name || "选择统计项目"}
            </Button>
          )}
          {selected && (
            <Button onClick={() => setSelected(null)}>清除项目</Button>
          )}
        </div>
        {query.error && <Notice>{query.error}</Notice>}
        <p className="quietnote">
          当前{metric === "total" ? "请求" : "Token"}合计{" "}
          {number(query.result?.summary?.[metric])} ·
          按有记录日期展示，完整明细见表格。
        </p>
      </div>
      <TimeSeries rows={rows} metric={metric} type={type} />
      <details className="pad">
        <summary>查看每日明细</summary>
        <Table
          heads={["日期", "请求", "成功 / 失败 / 在途", "总Token"]}
          rows={rows.map((r) => [
            tm(r.start),
            number(r.total),
            `${nf(r.succeeded)} / ${(r.failed || 0) + (r.cancelled || 0)} / ${nf(r.inflight)}`,
            number(r.total_tokens),
          ])}
        />
      </details>
      {picker && (
        <SourcePicker
          kind={dimension}
          params={params}
          onSelect={setSelected}
          onClose={() => setPicker(false)}
        />
      )}
    </Panel>
  );
}
function KeyUsage({ target, params, onClose }) {
  const { data } = useApp();
  const keyParams = { ...params, key_id: target.key_id || "unknown" };
  const query = useQuery(
    `/api/analytics?${queryString({ ...keyParams, top_limit: 100 })}`,
    data.revision,
  );
  const result = query.result;
  return (
    <Modal
      open
      size="large"
      onOpenChange={(v) => !v && onClose()}
      title={`API Key 用量详情 · ${target.name}`}
      description="仅显示真实用量与安全标识；不读取密钥值或请求正文。"
    >
      {query.error && <Notice>{query.error}</Notice>}
      <p className="quietnote">
        {params.start === 0 ? "全部历史" : tm(params.start)} 至 {tm(params.end)}{" "}
        · {number(result?.summary?.total)} 请求 ·{" "}
        {number(result?.summary?.total_tokens)} Token
      </p>
      {[
        ["客户端分布", result?.client_distribution, "clients"],
        ["模型分布", result?.model_distribution, "models"],
      ].map(([name, rows, kind]) => (
        <section key={kind} className="topgap">
          <h3>{name}</h3>
          <Table
            heads={[
              kind === "clients" ? "客户端" : "模型",
              "请求",
              "Token",
              "占比",
              "操作",
            ]}
            rows={(rows || []).map((r) => [
              sourceName(r, kind),
              number(r.total),
              number(r.total_tokens),
              percent(r.total_tokens, result?.summary?.total_tokens),
              <Button
                onClick={() =>
                  openReportRequests({
                    ...keyParams,
                    [kind === "clients" ? "client_source" : "model"]:
                      r.name || "unknown",
                  })
                }
              >
                请求日志
              </Button>,
            ])}
          />
        </section>
      ))}
      <p className="quietnote">历史未记录的来源保留为未知；不反推历史身份。</p>
    </Modal>
  );
}
function SourceStats({ kind, params, totalTokens }) {
  const { data } = useApp();
  const [page, setPage] = useState(1),
    [sort, setSort] = useState("total_tokens"),
    [target, setTarget] = useState(null);
  useEffect(
    () => setPage(1),
    [
      params.start,
      params.end,
      params.platform,
      params.model,
      params.account_id,
      sort,
    ],
  );
  const query = useQuery(
    `/api/analytics/sources?${queryString({ ...params, kind, page, page_size: 25, sort })}`,
    data.revision,
  );
  return (
    <Panel
      name={kind === "keys" ? "API Key统计" : "客户端统计"}
      desc={
        kind === "keys"
          ? "停用或撤销不会删除历史用量；密钥原文不进入报表。"
          : "软件标识统计，不代表设备或人数；历史未观测来源单列。"
      }
    >
      <div className="toolbar pad">
        <select
          aria-label={kind === "keys" ? "密钥统计排序" : "客户端统计排序"}
          value={sort}
          onChange={(e) => setSort(e.target.value)}
        >
          <option value="total_tokens">按Token</option>
          <option value="total">按请求数</option>
        </select>
      </div>
      {query.error && (
        <Notice>
          {query.error}
          <Button onClick={query.reload}>重试</Button>
        </Notice>
      )}
      <Table
        heads={[
          kind === "keys" ? "API Key" : "客户端",
          "请求",
          "成功 / 失败 / 在途",
          "成功率",
          "Token / 占比",
          "输入 / 输出",
          "缓存读取 / 比例",
          "覆盖",
          "最近调用",
          "操作",
        ]}
        rows={(query.result?.items || []).map((r) => [
          sourceName(r, kind),
          number(r.total),
          `${nf(r.succeeded)} / ${(r.failed || 0) + (r.cancelled || 0)} / ${nf(r.inflight)}`,
          percent(r.succeeded, r.total - (r.inflight || 0)),
          `${number(r.total_tokens)} / ${percent(r.total_tokens, totalTokens)}`,
          `${number(r.input_tokens)} / ${number(r.output_tokens)}`,
          `${number(r.cached_tokens)} / ${percent(r.cached_tokens, r.cache_input_tokens)}`,
          percent(r.cache_reported_requests, r.total),
          tm(r.last_used_at),
          <div className="row wrap">
            {kind === "keys" && (
              <Button onClick={() => setTarget(r)}>用量详情</Button>
            )}
            <Button
              onClick={() =>
                openReportRequests({
                  ...params,
                  [kind === "keys" ? "key_id" : "client_source"]:
                    kind === "keys"
                      ? r.key_id || "unknown"
                      : r.client_source || "unknown",
                })
              }
            >
              请求日志
            </Button>
          </div>,
        ])}
      />
      <Pagination
        page={page}
        pageSize={25}
        total={query.result?.total}
        loading={query.loading}
        onPage={setPage}
      />
      {target && (
        <KeyUsage
          target={target}
          params={params}
          onClose={() => setTarget(null)}
        />
      )}
    </Panel>
  );
}
export default function ReportInsights({ params, result }) {
  return (
    <>
      <Activity params={params} />
      <CachePanels params={params} />
      <DailyTrend params={params} models={result?.model_distribution || []} />
      <SourceStats
        kind="clients"
        params={params}
        totalTokens={result?.summary?.total_tokens}
      />
      <SourceStats
        kind="keys"
        params={params}
        totalTokens={result?.summary?.total_tokens}
      />
    </>
  );
}

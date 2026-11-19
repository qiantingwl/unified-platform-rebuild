import ReportInsights from "./ReportInsights";
import React, { useMemo, useState } from "react";
import {
  useApp,
  labels,
  Head,
  Panel,
  Table,
  Stats,
  Notice,
  tm,
  nf,
  Button,
} from "../ui";
import {
  AccountPicker,
  accountName,
  accountIdentity,
} from "../components/Management";
import { queryString, useQuery } from "../query";
const day = 86400;
const dateInput = (t) =>
  new Date((t + 8 * 3600) * 1000).toISOString().slice(0, 16);
const parseDate = (value) => Date.parse(`${value}+08:00`) / 1000;
const chartColors = [
  "#ff4d17",
  "#ffb800",
  "#43a653",
  "#2479cf",
  "#5148a4",
  "#15b9bf",
  "#8d99a8",
];
function Distribution({ rows = [] }) {
  const sorted = [...rows].sort(
    (a, b) => (b.total_tokens ?? -1) - (a.total_tokens ?? -1),
  );
  const total = sorted.reduce((n, r) => n + (r.total_tokens || 0), 0);
  let offset = 0;
  const slices = sorted.map((r, i) => {
    const start = offset;
    offset += total ? (100 * (r.total_tokens || 0)) / total : 0;
    return `${chartColors[i % chartColors.length]} ${start}% ${offset}%`;
  });
  return rows.length ? (
    <div className="report-distribution">
      <div
        className="report-donut"
        role="img"
        aria-label={`总Token ${nf(total)}，各项占比见完整图例`}
        style={{
          background: total
            ? `conic-gradient(${slices.join(",")})`
            : "var(--line)",
        }}
      >
        <div>
          <span>总 Token</span>
          <strong>{nf(total)}</strong>
          <small>已上报用量</small>
        </div>
      </div>
      <div className="report-legend">
        <div className="report-legend-row muted">
          <span>名称</span>
          <span>Token</span>
          <span>占比</span>
        </div>
        {sorted.map((r, i) => (
          <div className="report-legend-row" key={`${r.name}-${i}`}>
            <span>
              <i style={{ background: chartColors[i % chartColors.length] }} />
              {r.name || "未记录"}
            </span>
            <span>
              {r.total_tokens == null ? "未上报" : nf(r.total_tokens)}
            </span>
            <span>
              {r.total_tokens == null
                ? "未知"
                : total
                  ? `${((100 * r.total_tokens) / total).toFixed(1)}%`
                  : "0.0%"}
            </span>
          </div>
        ))}
      </div>
    </div>
  ) : (
    <div className="noresults">此范围没有分布记录</div>
  );
}
function Ranking({
  rows = [],
  total,
  limit,
  onLimit,
  accounts = false,
  onAccount,
}) {
  return (
    <>
      <div className="report-rank-toolbar">
        <span>按已上报总 Token 排名</span>
        <select
          aria-label={accounts ? "Top账户数量" : "Top提供商数量"}
          value={limit}
          onChange={(e) => onLimit(Number(e.target.value))}
        >
          {[5, 10, 20].map((n) => (
            <option key={n} value={n}>
              前 {n} 名
            </option>
          ))}
        </select>
      </div>
      <div className="report-ranking">
        {rows.slice(0, limit).map((r, i) => (
          <div className="report-rank-row" key={r.account_id || r.name || i}>
            <span className="muted">{String(i + 1).padStart(2, "0")}</span>
            <span>
              {accounts ? (
                <button
                  className="report-account-link"
                  onClick={() => onAccount(r)}
                >
                  {accountName(r)}
                  <small>{r.identity_hint}</small>
                </button>
              ) : (
                r.name || "未记录"
              )}
            </span>
            <div className="distribution-track">
              <span
                style={{
                  width: `${total ? (100 * (r.total_tokens || 0)) / total : 0}%`,
                }}
              />
            </div>
            <span>
              {r.total_tokens == null ? "未上报" : nf(r.total_tokens)}
            </span>
            <span className="muted">
              {r.total_tokens == null
                ? "未知"
                : total
                  ? `${((100 * r.total_tokens) / total).toFixed(1)}%`
                  : "0.0%"}
            </span>
          </div>
        ))}
        {!rows.length && <div className="noresults">此范围没有排行记录</div>}
      </div>
      <p className="report-rank-footer">
        前 {Math.min(limit, rows.length)} 名占已上报用量{" "}
        {total
          ? (
              (100 *
                rows
                  .slice(0, limit)
                  .reduce((n, r) => n + (r.total_tokens || 0), 0)) /
              total
            ).toFixed(1)
          : "0.0"}
        %<span>{total == null ? "未上报" : nf(total)} tokens</span>
      </p>
    </>
  );
}
function Trend({ rows = [] }) {
  const max = Math.max(1, ...rows.map((r) => r.total_tokens ?? 0));
  const requestMax = Math.max(1, ...rows.map((r) => r.total || 0));
  const requests = rows
    .map(
      (r, i) =>
        `${40 + (i * 840) / Math.max(1, rows.length - 1)},${175 - (150 * (r.total || 0)) / requestMax}`,
    )
    .join(" ");
  const points = rows
    .map((r, i) =>
      r.total_tokens == null
        ? ""
        : `${i === 0 || rows[i - 1].total_tokens == null ? "M" : "L"}${40 + (i * 840) / Math.max(1, rows.length - 1)},${175 - (150 * r.total_tokens) / max}`,
    )
    .join(" ");
  return rows.length ? (
    <div className="chart-frame">
      <svg
        className="trend-chart"
        viewBox="0 0 920 220"
        role="img"
        aria-label="请求与Token分别按各自峰值缩放，详细数值见下方表格"
      >
        <line x1="40" y1="175" x2="880" y2="175" stroke="var(--line)" />
        <path d={points} fill="none" stroke="var(--blue)" strokeWidth="2" />
        <polyline
          points={requests}
          fill="none"
          stroke="var(--green)"
          strokeWidth="2"
          strokeDasharray="5 3"
        />
        {rows.map((r, i) =>
          r.total_tokens == null ? null : (
            <circle
              key={i}
              cx={40 + (i * 840) / Math.max(1, rows.length - 1)}
              cy={175 - (150 * (r.total_tokens ?? 0)) / max}
              r="3"
              fill="var(--blue)"
            >
              <title>
                {tm(r.start)}：{nf(r.total)} 请求 /{" "}
                {r.total_tokens == null ? "未上报" : nf(r.total_tokens)} Token
              </title>
            </circle>
          ),
        )}
        <text x="40" y="205" fill="var(--muted)" fontSize="12">
          {tm(rows[0].start)}
        </text>
        <text
          x="880"
          y="205"
          textAnchor="end"
          fill="var(--muted)"
          fontSize="12"
        >
          {tm(rows.at(-1).start)}
        </text>
        <text x="40" y="15" fill="var(--muted)" fontSize="12">
          总 Token（蓝）· 峰值{" "}
          {rows.some((r) => r.total_tokens != null)
            ? nf(Math.max(...rows.map((r) => r.total_tokens || 0)))
            : "未上报"}
          　请求（绿虚线）· 峰值{" "}
          {nf(Math.max(...rows.map((r) => r.total || 0)))}
        </text>
      </svg>
    </div>
  ) : (
    <div className="noresults">此范围没有趋势记录</div>
  );
}
export default function Analytics() {
  const { data, openAccounts } = useApp();
  const [accountLimit, setAccountLimit] = useState(5),
    [providerLimit, setProviderLimit] = useState(5);
  const [range, setRange] = useState("today"),
    [basis, setBasis] = useState("start"),
    [platform, setPlatform] = useState(""),
    [model, setModel] = useState(""),
    [account, setAccount] = useState(""),
    [custom, setCustom] = useState({
      start: dateInput(data.as_of - day),
      end: dateInput(data.as_of),
    }),
    [applied, setApplied] = useState(null);
  const now = Math.floor(data.as_of / 60) * 60 + 60,
    midnight = Math.floor((data.as_of + 8 * 3600) / day) * day - 8 * 3600;
  const window =
    range === "custom"
      ? applied
      : range === "yesterday"
        ? { start: midnight - day, end: midnight }
        : {
            start:
              range === "all"
                ? 0
                : range === "month"
                  ? Date.parse(
                      new Date((data.as_of + 8 * 3600) * 1000)
                        .toISOString()
                        .slice(0, 7) + "-01T00:00:00+08:00",
                    ) / 1000
                  : midnight -
                    (range === "7d" ? 6 : range === "30d" ? 29 : 0) * day,
            end: now,
          };
  const valid =
    window &&
    Number.isFinite(window.start) &&
    Number.isFinite(window.end) &&
    window.end > window.start;
  const query = useQuery(
    valid
      ? `/api/analytics?${queryString({ ...window, all_time: range === "all" ? true : undefined, rank_by: "total_tokens", top_limit: accountLimit, basis, platform, model, account_id: account })}`
      : null,
    data.revision,
  );
  const result = query.result,
    groups = result?.groups || [],
    summary = result?.summary || {};
  const value = (n) => (n == null ? "未上报" : nf(n)),
    pct = (n, d) =>
      n == null || !d ? "无样本" : `${((100 * n) / d).toFixed(1)}%`;
  const ended =
    summary.total == null ? null : summary.total - (summary.inflight || 0);
  return (
    <>
      <Head
        name="报表"
        desc="按用户请求统计，换号尝试单独记录；时间使用北京时间。"
      />
      <div className="toolbar">
        <div className="segmented">
          {[
            ["today", "今日"],
            ["yesterday", "昨日"],
            ["7d", "近7天"],
            ["30d", "近30天"],
            ["month", "本月"],
            ["all", "全部"],
            ["custom", "自定义"],
          ].map(([v, n]) => (
            <button
              key={v}
              className={range === v ? "active" : ""}
              aria-pressed={range === v}
              onClick={() => setRange(v)}
            >
              {n}
            </button>
          ))}
        </div>
        <select
          aria-label="统计时间口径"
          value={basis}
          onChange={(e) => setBasis(e.target.value)}
        >
          <option value="start">按开始时间</option>
          <option value="end">按结束时间</option>
        </select>
        <select
          aria-label="报表平台"
          value={platform}
          onChange={(e) => {
            setPlatform(e.target.value);
            setAccount("");
          }}
        >
          <option value="">全部平台</option>
          {data.platforms.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
        <input
          aria-label="报表模型"
          placeholder="模型名称"
          value={model}
          onChange={(e) => setModel(e.target.value)}
        />
        <AccountPicker
          platform={platform}
          value={account}
          onChange={setAccount}
          label="筛选账户"
        />
      </div>
      {range === "custom" && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            setApplied({
              start: parseDate(custom.start),
              end: parseDate(custom.end),
            });
          }}
        >
          <label className="field">
            开始时间
            <input
              type="datetime-local"
              required
              value={custom.start}
              onChange={(e) => setCustom({ ...custom, start: e.target.value })}
            />
          </label>
          <label className="field">
            结束时间
            <input
              type="datetime-local"
              required
              value={custom.end}
              onChange={(e) => setCustom({ ...custom, end: e.target.value })}
            />
          </label>
          <Button
            type="submit"
            disabled={!(parseDate(custom.end) > parseDate(custom.start))}
          >
            查询
          </Button>
        </form>
      )}
      {query.error && (
        <Notice>
          {query.error}
          <Button onClick={query.reload}>重试</Button>
        </Notice>
      )}
      {!valid && <Notice>请选择开始和结束时间后查询。</Notice>}
      <p className="quietnote">
        {valid
          ? `${range === "all" ? "全部历史" : tm(window.start)} 至 ${tm(window.end)}（不含结束时刻）`
          : "自定义时间待查询"}
        。
        {basis === "end"
          ? "按结束时间不含在途请求。"
          : "在途请求不进入已结束成功率分母。"}
        {query.loading && valid ? " 正在统计…" : ""}
      </p>
      <Stats
        items={[
          [
            "总 Token",
            value(summary.total_tokens),
            `输入 ${value(summary.input_tokens)} / 输出 ${value(summary.output_tokens)}`,
          ],
          [
            "总请求数",
            value(summary.total),
            `在途 ${value(summary.inflight)} / 取消 ${value(summary.cancelled)}`,
          ],
          [
            "成功请求数",
            value(summary.succeeded),
            `已结束成功率 ${pct(summary.succeeded, ended)} / 失败 ${value(summary.failed)}`,
          ],
          [
            "缓存读取",
            value(summary.cached_tokens),
            `占有效样本输入 ${pct(summary.cached_tokens, summary.cache_input_tokens)} · 上报覆盖 ${pct(summary.cache_reported_requests, summary.total)}`,
          ],
        ]}
      />
      <div className="report-activity">
        <span>
          活跃天数 {value(summary.active_days)} · 当前连续天数{" "}
          {value(summary.current_streak)}
        </span>
        <span>
          Top 模型 {summary.top_model?.name || "暂无记录"} ·{" "}
          {value(summary.top_model?.total_tokens)} Token
        </span>
        <span>缓存未上报 {value(summary.cache_missing_requests)} 个请求</span>
      </div>
      <div className="grid equal report-rank-grid">
        <Panel name="Top 账户">
          <Ranking
            rows={result?.top_accounts || []}
            total={summary.total_tokens}
            limit={accountLimit}
            onLimit={setAccountLimit}
            accounts
            onAccount={(a) => {
              setPlatform(a.platform || "");
              setAccount(a.account_id);
            }}
          />
        </Panel>
        <Panel name="Top 提供商">
          <Ranking
            rows={[...(result?.platform_distribution || [])].sort(
              (a, b) => (b.total_tokens ?? -1) - (a.total_tokens ?? -1),
            )}
            total={summary.total_tokens}
            limit={providerLimit}
            onLimit={setProviderLimit}
          />
        </Panel>
      </div>
      <div className="grid equal">
        <Panel name="模型用量（Token）">
          <Distribution rows={result?.model_distribution} />
        </Panel>
        <Panel name="提供商用量（Token）">
          <Distribution rows={result?.platform_distribution} />
        </Panel>
      </div>
      <Panel name="请求与 Token 趋势">
        <Trend rows={result?.trend} />
        <details className="pad">
          <summary>查看趋势明细</summary>
          <Table
            heads={["时间", "请求", "成功 / 失败", "输入 / 输出 / 总Token"]}
            rows={(result?.trend || []).map((r) => [
              tm(r.start),
              nf(r.total),
              `${nf(r.succeeded)} / ${nf(r.failed)}`,
              `${value(r.input_tokens)} / ${value(r.output_tokens)} / ${value(r.total_tokens)}`,
            ])}
          />
        </details>
      </Panel>
      {valid && (
        <ReportInsights
          params={{
            ...window,
            all_time: range === "all" ? true : undefined,
            basis,
            platform,
            model,
            account_id: account,
          }}
          result={result}
        />
      )}
      <Panel name="平台与模型">
        <Table
          heads={[
            "平台 / 模型",
            "成功 / 失败 / 在途",
            "输入 / 输出 / 总Token",
            "有效输出 平均 / 最长",
            "缓存读取",
          ]}
          rows={groups.map((g) => {
            const dur = (v) => (v == null ? "无样本" : `${v.toFixed(2)} s`);
            return [
              <>
                {g.platform}
                <small>{g.model}</small>
              </>,
              `${g.statuses?.succeeded || 0} / ${(g.statuses?.failed || 0) + (g.statuses?.cancelled || 0)} / ${g.statuses?.inflight || 0}`,
              `${value(g.input_tokens)} / ${value(g.output_tokens)} / ${value(g.total_tokens)}`,
              `${dur(g.effective_output?.mean)} / ${dur(g.effective_output?.max)}`,
              g.cache?.ratio == null
                ? "未上报"
                : `${(100 * g.cache.ratio).toFixed(1)}%`,
            ];
          })}
        />
      </Panel>
      <Panel name="错误分类">
        <Table
          heads={["平台 / 模型", "原因", "请求数"]}
          rows={groups.flatMap((g) =>
            Object.entries(g.errors || {}).map(([code, count]) => [
              `${g.platform} / ${g.model}`,
              code === "unknown" ? "原因未记录" : labels[code] || code,
              count,
            ]),
          )}
        />
      </Panel>
    </>
  );
}

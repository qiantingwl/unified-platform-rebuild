import BatchAccountProxy from "./BatchAccountProxy";
import React, { useEffect, useState } from "react";
import { Search, Plus } from "lucide-react";

import {
  useApp,
  nf,
  tm,
  labels,
  Badge,
  Button,
  Head,
  Table,
  totals,
  balanceLabels,
  balanceState,
  BalanceValue,
  creditSummary,
  Switch,
  Notice,
} from "../ui";
import { queryString, useDebounced, useQuery } from "../query";
import { api } from "../api";
import {
  Modal,
  Pagination,
  accountName,
  accountIdentity,
} from "../components/Management";
function Accounts() {
  const { data, filter, setFilter, setDrawer, mutate, refresh } = useApp();
  const [saving, setSaving] = useState(null),
    [error, setError] = useState("");
  const [q, setQ] = useState(""),
    [page, setPage] = useState(1),
    [credit, setCredit] = useState("all"),
    [sort, setSort] = useState("default");
  const [selected, setSelected] = useState([]),
    [columnsOpen, setColumnsOpen] = useState(false),
    [columns, setColumns] = useState(() => {
      try {
        const saved = JSON.parse(localStorage.getItem("account-columns"));
        return Array.isArray(saved)
          ? [
              0,
              1,
              ...saved.filter((n) => Number.isInteger(n) && n > 1 && n < 9),
            ]
          : [0, 1, 2, 3, 4, 5, 6, 7, 8];
      } catch {
        return [0, 1, 2, 3, 4, 5, 6, 7, 8];
      }
    }),
    [batchResults, setBatchResults] = useState(null),
    [batchSettings, setBatchSettings] = useState(false),
    [batchProxy, setBatchProxy] = useState(false),
    [batchDraft, setBatchDraft] = useState({
      concurrency: "",
      priority: "",
      interval: "",
      reset: false,
    }),
    [moreFilters, setMoreFilters] = useState(false);
  useEffect(() => {
    try {
      localStorage.setItem("account-columns", JSON.stringify(columns));
    } catch {}
  }, [columns]);
  const columnNames = [
    "选择",
    "账户",
    "平台",
    "账号开关",
    "接单状态",
    "剩余积分 / 余额",
    "余额核验时间",
    "今日签到",
    "最近更新",
  ];
  async function batch(kind, settings) {
    setSaving("batch");
    setError("");
    const results = [];
    for (const id of selected) {
      const a = rows.find((a) => a.id === id);
      if (!a) continue;
      try {
        if (kind === "settings")
          await api(`/api/accounts/${id}/settings`, "PATCH", settings);
        else if (kind === "enable" || kind === "disable")
          await api(`/api/accounts/${id}/settings`, "PATCH", {
            enabled: kind === "enable",
          });
        else await api(`/api/accounts/${id}/jobs`, "POST", { kind });
        results.push({ id, label: accountName(a), ok: true });
      } catch (e) {
        results.push({
          id,
          label: accountName(a),
          ok: false,
          error: e.message,
        });
      }
    }
    setBatchResults(results);
    setSelected([]);
    setSaving(null);
    await refresh();
    directory.reload();
  }
  const [pageSize, setPageSize] = useState(25);
  const search = useDebounced(q);
  const directory = useQuery(
    `/api/accounts?${queryString({ q: search, page, page_size: pageSize, platform: filter.platform === "全部平台" ? "" : filter.platform, status: filter.status, balance_state: credit, sort })}`,
    data.revision,
    0,
    { retainPrevious: true },
  );
  const result = directory.result,
    rows = result?.items || [];
  const t = result?.summary || {},
    s = t;
  useEffect(
    () => setPage(1),
    [filter.platform, filter.status, search, credit, sort, pageSize],
  );
  useEffect(() => {
    if (
      !directory.loading &&
      !directory.isPrevious &&
      result?.total != null &&
      page > Math.max(1, Math.ceil(result.total / pageSize))
    )
      setPage(Math.max(1, Math.ceil(result.total / pageSize)));
  }, [result?.total, pageSize, page, directory.loading, directory.isPrevious]);
  useEffect(
    () => setSelected([]),
    [page, pageSize, search, filter.platform, filter.status, credit],
  );
  return (
    <>
      <Head name="账户" desc="账户开关、凭证、额度与当前接单状态分别记录。">
        <a className="btn" href="#imports">
          导入账户
        </a>
        <Button
          icon={Plus}
          variant="primary"
          onClick={() => setDrawer({ type: "account-create" })}
        >
          添加账户
        </Button>
      </Head>
      <div className="compactmetrics" data-testid="account-totals">
        {[
          ["当前账户", t.total],
          ["可接单", t.ready],
          ["使用中", t.busy],
          ["暂不可接单", t.unavailable],
          ["余额已核验", s.verified],
          [
            "余额过期 / 未查询 / 失败",
            `${s.stale == null ? "—" : nf(s.stale)} / ${s.unknown == null ? "—" : nf(s.unknown)} / ${s.failed == null ? "—" : nf(s.failed)}`,
          ],
          ...(filter.platform === "全部平台"
            ? []
            : [
                [
                  "最近已知余额合计",
                  s.known ? nf(Math.round(s.knownSum * 100) / 100) : "—",
                ],
                [
                  "新鲜核验余额",
                  s.verified ? nf(Math.round(s.sum * 100) / 100) : "—",
                ],
              ]),
        ].map(([n, v]) => (
          <span key={n}>
            {n}
            <b>{v == null ? "—" : typeof v === "number" ? nf(v) : v}</b>
          </span>
        ))}
      </div>
      {(error || directory.error) && (
        <Notice>
          {error || directory.error}
          <Button onClick={directory.reload}>重新查询</Button>
        </Notice>
      )}
      <div className="toolbar">
        <div className="searchbox">
          <Search size={15} />
          <input
            aria-label="搜索账户"
            placeholder="原名称 / UID / 脱敏身份"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <select
          aria-label="平台筛选"
          value={filter.platform}
          onChange={(e) => setFilter({ ...filter, platform: e.target.value })}
        >
          <option>全部平台</option>
          {data.platforms.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
        <Button
          aria-expanded={moreFilters}
          onClick={() => setMoreFilters(!moreFilters)}
        >
          {moreFilters ? "收起筛选" : "更多筛选"}
        </Button>
        <span className="spacer" />
        <Button
          variant="ghost"
          onClick={() => {
            setFilter({ platform: "全部平台", status: "all" });
            setQ("");
            setCredit("all");
            setSort("default");
          }}
        >
          清除筛选
        </Button>
        <Button onClick={() => setColumnsOpen(true)}>显示列</Button>
      </div>
      {moreFilters && (
        <div className="toolbar">
          <select
            aria-label="余额状态筛选"
            value={credit}
            onChange={(e) => setCredit(e.target.value)}
          >
            <option value="all">全部余额状态</option>
            {Object.entries({
              verified: "余额已核验",
              stale: "余额数据过期",
              unknown: "尚未查询",
              failed: "查询失败",
            }).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
          <select
            aria-label="排序"
            value={sort}
            onChange={(e) => setSort(e.target.value)}
          >
            <option value="default">默认顺序</option>
            <option value="high">余额从高到低</option>
            <option value="low">余额从低到高</option>
            <option value="checked">核验时间最早优先</option>
          </select>
        </div>
      )}
      <div className="chips">
        <button
          className={`chip ${filter.status === "all" ? "active" : ""}`}
          onClick={() => setFilter({ ...filter, status: "all" })}
        >
          全部{" "}
          <strong>
            {result
              ? Object.values(result.status_counts || {}).reduce(
                  (a, b) => a + b,
                  0,
                )
              : "—"}
          </strong>
        </button>
        {Object.entries(result?.status_counts || {}).map(([s, n]) => (
          <button
            className={`chip ${filter.status === s ? "active" : ""}`}
            key={s}
            onClick={() => setFilter({ ...filter, status: s })}
          >
            {labels[s] || s} <strong>{n}</strong>
          </button>
        ))}
      </div>
      {(filter.status !== "all" ||
        filter.platform !== "全部平台" ||
        credit !== "all" ||
        search ||
        sort !== "default") && (
        <div className="toolbar">
          <span className="muted">当前筛选：</span>
          {filter.status !== "all" && (
            <button
              className="chip active"
              title="清除原因筛选"
              onClick={() => setFilter({ ...filter, status: "all" })}
            >
              {labels[filter.status] || filter.status} ×
            </button>
          )}
          {filter.platform !== "全部平台" && (
            <button
              className="chip"
              title="清除平台筛选"
              onClick={() => setFilter({ ...filter, platform: "全部平台" })}
            >
              {filter.platform} ×
            </button>
          )}
          {credit !== "all" && (
            <button
              className="chip"
              title="清除余额状态筛选"
              onClick={() => setCredit("all")}
            >
              {{
                verified: "余额已核验",
                stale: "余额数据过期",
                unknown: "尚未查询",
                failed: "查询失败",
              }[credit] || credit}{" "}
              ×
            </button>
          )}
          {search && (
            <button className="chip" title="清除搜索" onClick={() => setQ("")}>
              搜索：{search} ×
            </button>
          )}
          {sort !== "default" && (
            <button
              className="chip"
              title="恢复默认排序"
              onClick={() => setSort("default")}
            >
              {{
                high: "余额从高到低",
                low: "余额从低到高",
                checked: "核验时间最早",
              }[sort] || sort}{" "}
              ×
            </button>
          )}
          <span className="spacer" />
          <Button
            variant="ghost"
            onClick={() => {
              setFilter({ platform: "全部平台", status: "all" });
              setQ("");
              setCredit("all");
              setSort("default");
            }}
          >
            清除全部
          </Button>
        </div>
      )}
      {selected.length > 0 && (
        <div className="toolbar">
          <span>仅当前页已选 {selected.length} 个（换页会清空）</span>
          {[
            ["enable", "批量启用"],
            ["disable", "批量停用"],
            ["checkin", "批量签到"],
            ["balance", "查询余额"],
          ].map(([k, n]) => (
            <Button
              key={k}
              disabled={!!saving || directory.isPrevious}
              onClick={() => batch(k)}
            >
              {n}
            </Button>
          ))}
          <Button
            disabled={!!saving || directory.isPrevious}
            onClick={() => {
              setBatchDraft({
                concurrency: "",
                priority: "",
                interval: "",
                reset: false,
              });
              setBatchSettings(true);
            }}
          >
            批量设置
          </Button>
          <Button
            disabled={!!saving || directory.isPrevious}
            onClick={() => setBatchProxy(true)}
          >
            批量出口
          </Button>
          <span className="spacer" />
        </div>
      )}
      <div className="panel">
        {directory.isPrevious && (
          <p className="quietnote" role="status">
            正在加载第 {page} 页，当前保留上一次查询结果。
          </p>
        )}
        <Table
          loading={directory.loading}
          heads={[
            <input
              type="checkbox"
              aria-label="选择当前页全部账户"
              checked={rows.length > 0 && selected.length === rows.length}
              disabled={!!saving || directory.isPrevious}
              onChange={(e) =>
                setSelected(e.target.checked ? rows.map((a) => a.id) : [])
              }
            />,
            "账户",
            "平台",
            "账号开关",
            "接单状态",
            "剩余积分 / 余额",
            "余额核验时间",
            "今日签到",
            "最近更新",
          ].filter((_, i) => columns.includes(i))}
          rows={rows.map((a) =>
            [
              <input
                type="checkbox"
                aria-label={`选择${accountName(a)}`}
                checked={selected.includes(a.id)}
                disabled={!!saving || directory.isPrevious}
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? [...selected, a.id]
                      : selected.filter((id) => id !== a.id),
                  )
                }
              />,
              <Button
                variant="link"
                onClick={() =>
                  setDrawer({ type: "account", id: a.id, account: a })
                }
              >
                {accountName(a)}
                <small>{accountIdentity(a)}</small>
              </Button>,
              a.platform,
              <Switch
                label={`${a.label} 账户开关`}
                checked={a.enabled}
                disabled={
                  saving === a.id || saving === "batch" || directory.isPrevious
                }
                onChange={async (e) => {
                  const enabled = e.target.checked;
                  setSaving(a.id);
                  setError("");
                  try {
                    await mutate(
                      `/api/accounts/${a.id}/settings`,
                      "PATCH",
                      { enabled },
                      enabled ? "账户已开启。" : "账户已由你手动关闭。",
                    );
                  } catch (e) {
                    setError(e.message);
                  } finally {
                    setSaving(null);
                  }
                }}
              />,
              <>
                <Badge status={a.status} />
                {a.model_blocks?.length > 0 && (
                  <small>
                    {a.model_blocks
                      .slice(0, 2)
                      .map(
                        (b) =>
                          `${b.scope === "account" ? "账户" : b.model === "*" ? "全部模型" : b.model}：${labels[b.reason] || b.reason}${b.reason === "quota" ? `（复核模型：${b.model}）` : ""}`,
                      )
                      .join("；")}
                    {a.model_blocks.length > 2
                      ? ` 等 ${a.model_blocks.length} 项`
                      : ""}
                  </small>
                )}
                {a.model_blocks?.length > 0 && (
                  <Button
                    variant="ghost"
                    disabled={
                      saving === `release-${a.id}` || directory.isPrevious
                    }
                    title="把该账户的限制放回候选池；下一次真实请求若仍被上游拒绝，会立刻重新限制"
                    onClick={async () => {
                      setSaving(`release-${a.id}`);
                      setError("");
                      try {
                        await mutate(
                          `/api/accounts/${a.id}/model-blocks/release`,
                          "POST",
                          undefined,
                          "限制已解除，下一次请求会重新验证。",
                        );
                      } catch (e) {
                        setError(e.message);
                      } finally {
                        setSaving(null);
                      }
                    }}
                  >
                    解除限制
                  </Button>
                )}
                {a.credential_status === "disabled" && (
                  <Button
                    variant="ghost"
                    disabled={
                      saving === `recheck-${a.id}` || directory.isPrevious
                    }
                    title="上游说该账号已停用。确认还想再试时点这里：排一次只读余额核验，能读到余额才回到候选池"
                    onClick={async () => {
                      setSaving(`recheck-${a.id}`);
                      setError("");
                      try {
                        await mutate(
                          `/api/accounts/${a.id}/recheck`,
                          "POST",
                          undefined,
                          "已排入核验，余额可读后会自动回到候选池。",
                        );
                      } catch (e) {
                        setError(e.message);
                      } finally {
                        setSaving(null);
                      }
                    }}
                  >
                    重新检测
                  </Button>
                )}
              </>,
              <BalanceValue account={a} now={result?.as_of || data.as_of} />,
              a.balance_at ? tm(a.balance_at) : "尚未查询",
              a.checkin_day === (result?.business_day || data.business_day) ? (
                <span className="green">已签到</span>
              ) : (
                "未完成"
              ),
              <>
                {tm(a.updated_at)}
                <small>
                  凭证 v{a.credential_version}
                  {a.relogin_failures
                    ? ` · 重登失败 ${a.relogin_failures} 次`
                    : ""}
                </small>
              </>,
            ].filter((_, i) => columns.includes(i)),
          )}
        />
        <Pagination
          page={
            result?.page ||
            (directory.isPrevious
              ? Number(
                  new URLSearchParams(
                    (directory.resultPath || "").split("?")[1],
                  ).get("page"),
                ) || 1
              : page)
          }
          pageSize={pageSize}
          total={result?.total}
          loading={directory.loading}
          onPage={setPage}
          onPageSize={setPageSize}
        />
      </div>
      {batchProxy && (
        <BatchAccountProxy
          accounts={rows.filter((a) => selected.includes(a.id))}
          onClose={() => setBatchProxy(false)}
          onComplete={() => directory.reload()}
        />
      )}
      <Modal
        open={batchSettings}
        onOpenChange={(v) => !saving && setBatchSettings(v)}
        title={`批量设置 · 当前页已选 ${selected.length} 个账户`}
        description="仅修改本次明确勾选的当前页账户；留空的项目保持原值。"
        footer={
          <>
            <Button
              disabled={!!saving || directory.isPrevious}
              onClick={() => setBatchSettings(false)}
            >
              取消
            </Button>
            <Button
              form="account-batch-settings"
              type="submit"
              variant="primary"
              disabled={
                !!saving ||
                !selected.length ||
                !(
                  batchDraft.concurrency !== "" ||
                  batchDraft.priority !== "" ||
                  batchDraft.interval !== "" ||
                  batchDraft.reset
                )
              }
            >
              保存到已选账户
            </Button>
          </>
        }
      >
        <form
          id="account-batch-settings"
          onSubmit={async (e) => {
            e.preventDefault();
            const patch = {
              ...(batchDraft.concurrency !== ""
                ? { concurrency: Number(batchDraft.concurrency) }
                : {}),
              ...(batchDraft.priority !== ""
                ? { priority: Number(batchDraft.priority) }
                : {}),
              ...(batchDraft.reset
                ? { reset_balance_interval: true }
                : batchDraft.interval !== ""
                  ? { balance_interval_seconds: Number(batchDraft.interval) }
                  : {}),
            };
            setBatchSettings(false);
            await batch("settings", patch);
          }}
        >
          <label className="field gap">
            业务并发
            <input
              aria-label="批量业务并发"
              type="number"
              min="1"
              max="1024"
              placeholder="不修改"
              value={batchDraft.concurrency}
              onChange={(e) =>
                setBatchDraft({ ...batchDraft, concurrency: e.target.value })
              }
            />
          </label>
          <label className="field gap">
            调度优先级
            <input
              aria-label="批量调度优先级"
              type="number"
              min="-100000"
              max="100000"
              step="1"
              placeholder="不修改"
              value={batchDraft.priority}
              onChange={(e) =>
                setBatchDraft({ ...batchDraft, priority: e.target.value })
              }
            />
          </label>
          <label className="field gap">
            余额查询间隔（秒）
            <input
              aria-label="批量余额查询间隔"
              type="number"
              min="60"
              max="2592000"
              step="1"
              disabled={batchDraft.reset}
              placeholder="不修改"
              value={batchDraft.interval}
              onChange={(e) =>
                setBatchDraft({ ...batchDraft, interval: e.target.value })
              }
            />
          </label>
          <Switch
            label="恢复全局余额查询周期"
            checked={batchDraft.reset}
            onChange={(e) =>
              setBatchDraft({ ...batchDraft, reset: e.target.checked })
            }
          />
        </form>
      </Modal>
      <Modal
        open={columnsOpen}
        onOpenChange={setColumnsOpen}
        title="账户显示列"
        description="只改变本页显示，不修改账户数据"
      >
        <div className="switchlist">
          {columnNames.map((n, i) => (
            <Switch
              key={n}
              label={n}
              checked={columns.includes(i)}
              disabled={i < 2}
              onChange={(e) =>
                setColumns(
                  e.target.checked
                    ? [...columns, i]
                    : columns.filter((x) => x !== i),
                )
              }
            />
          ))}
        </div>
      </Modal>
      <Modal
        open={batchResults !== null}
        onOpenChange={(v) => !v && setBatchResults(null)}
        title="批量操作结果"
        description="仅对明确勾选的当前页账户逐条提交，排队不代表执行成功"
      >
        <Table
          heads={["账户", "提交结果"]}
          rows={(batchResults || []).map((r) => [
            r.label,
            r.ok ? "已提交" : r.error,
          ])}
        />
      </Modal>
    </>
  );
}

export default Accounts;

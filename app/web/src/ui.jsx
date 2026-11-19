import React, {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { api } from "./api";
import {
  KeyRound,
  ChartNoAxesCombined,
  LayoutDashboard,
  Users,
  UserPlus,
  CalendarCheck,
  Network,
  Globe,
  ScrollText,
  Settings,
  AlertCircle,
} from "lucide-react";

const Context = createContext(null),
  useApp = () => useContext(Context);
const nf = (n) => (n ?? 0).toLocaleString("en-US");
const tm = (t) =>
  t !== null && t !== undefined && Number.isFinite(Number(t))
    ? new Date(t * 1000).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "尚无记录";
const labels = {
  ready: "可接单",
  busy: "使用中",
  checkin_pending: "待签到",
  balance_exhausted: "签到后额度用完",
  no_balance: "无余额",
  needs_relogin: "需重登",
  upstream_disabled: "上游停用",
  rate_limit: "模型限流",
  permission: "权限限制",
  needs_verification: "待调用验证",
  balance_pending: "余额待核验",
  credential_pending: "凭证待核验",
  missing_credential: "缺凭证",
  adapter_unavailable: "平台尚未接入",
  manually_disabled: "人工停用",
  queued: "排队中",
  running: "执行中",
  succeeded: "成功",
  failed: "失败",
  uncertain: "待核对回执",
  superseded: "旧版本结果已跳过",
  inflight: "在途",
  cancelled: "已取消",
  checkin: "签到",
  balance: "查询余额",
  renew: "续期",
  onboarding: "新手奖励核验",
  benefits: "平台权益任务",
  onboarding_uncertain: "新手奖励结果待核对",
  onboarding_incomplete: "新手奖励尚未全部确认",
  relogin: "重登",
  verify: "回答验证",
  invitation: "邀请码回读 / 绑定",
  fresh: "余额已核验",
  zero: "核验为零",
  stale: "余额数据过期",
  unknown: "尚未查询",
  failed_keep: "查询失败，保留最近余额",
  old_credential: "旧凭证余额",
  verified: "余额已核验",
  never_queried: "尚未查询",
  credential_changed: "凭证已更新，余额待核验",
  invalid_credentials: "凭证失效",
  no_sms: "未收到验证码",
  permission_denied: "无调用权限",
  executor_lost: "执行器失联",
  confirmed: "结果已确认",
  stale_credential_result: "旧凭证结果已忽略",
  credential_replaced: "凭证已替换",
  network: "连接错误",
  missing_material: "缺登录材料",
  internal_error: "执行异常",
  no_effective_output: "未收到有效输出",
  login_rejected: "登录被拒绝",
  unknown_403: "403，来源待核实",
  identity: "身份认证未完成",
  no_route: "尚未配置模型路由",
  startup_timeout: "首次有效输出等待超时",
  stream_idle: "输出中断超时",
  truncated_stream: "上游未正常结束",
  upstream_5xx: "上游服务错误",
  response_read_failed: "错误响应读取失败",
  network_execution_disabled: "外部执行未开启",
  proxy_retired: "绑定代理已从订阅移除",
  credential_review: "凭证待复核",
  suspect: "凭证待复核",
  request_rejected: "请求本身被平台拒绝（不换号）",
  content_filter: "内容审核拦截",
  unconfirmed_checkin: "签到结果未确认",
  checkin_inactive: "平台当前无签到活动",
  invalid_balance: "余额返回无法确认",
  quota: "额度不足，账户待验证",
  cooldown: "错误冷却中",
};
const eventLabels = {
  created: "建立账户",
  scheduled: "安排维护任务",
  claimed: "领取任务",
  balance_after_checkin: "签到成功，已安排余额查询",
  updated_pending_validation: "新凭证已保存，等待验证",
  safe_retry: "重新安排安全查询",
  lease_expired: "执行租约超时，已记录状态",
  acquired: "原子占用业务槽",
  placeholder_at: "发送快速占位",
  first_content_at: "收到有效内容",
  revoked: "撤销密钥",
  stale_job_skipped: "跳过旧凭证任务",
  credential_review_scheduled: "鉴权失败一次，已安排凭证复核",
  credential_confirmed_invalid: "复核确认凭证失效",
};
const nav = [
  ["overview", "运行总览", LayoutDashboard],
  ["accounts", "账户", Users],
  ["register", "注册与登录", UserPlus],
  ["maintenance", "签到中心", CalendarCheck],
  ["automaintenance", "自动维护", CalendarCheck],
  ["jobs", "任务记录", CalendarCheck],
  ["recovery", "重登与恢复", UserPlus],
  ["invite", "邀请码与权益", Users],
  ["keys", "API 密钥", KeyRound],
  ["models", "模型管理", Network],
  ["routing", "路由配置", Network],
  ["analytics", "报表", ChartNoAxesCombined],
  ["proxy", "代理", Globe],
  ["logs", "请求日志", ScrollText],
  ["systemlogs", "系统日志", ScrollText],
  ["settings", "设置", Settings],
];
const tabs = {
  settings: [
    ["settings", "规则与版本"],
    ["admin", "管理员登录"],
    ["monitor", "系统状态"],
    ["imports", "账户迁入"],
  ],
  logs: [
    ["logs", "请求日志"],
    ["systemlogs", "系统日志"],
  ],
  maintenance: [
    ["maintenance", "签到中心"],
    ["automaintenance", "自动维护"],
    ["jobs", "任务记录"],
  ],
};
const title = {
  ...Object.fromEntries(nav.map((x) => [x[0], x[1]])),
  recovery: "重登与恢复",
  invite: "邀请码与权益",
  keys: "API 密钥",
  analytics: "报表",
  systemlogs: "系统日志",
  monitor: "系统状态",
  imports: "账户迁入",
  admin: "管理员登录",
};
const parent = (id) =>
  ["monitor", "imports", "admin"].includes(id) ? "settings" : id;
const tone = (s) =>
  ["ready", "succeeded", "confirmed"].includes(s)
    ? "good"
    : ["failed", "permission", "needs_relogin"].includes(s)
      ? "bad"
      : ["busy", "running", "inflight"].includes(s)
        ? "info"
        : "warn";
function Badge({ status, children }) {
  return (
    <span className={`pill ${tone(status)}`}>
      {status && <i className="statusdot" />}
      {children || labels[status] || status}
    </span>
  );
}
function Button({
  children,
  icon: Icon,
  variant = "",
  type = "button",
  ...props
}) {
  return (
    <button type={type} className={`btn ${variant}`} {...props}>
      {Icon && <Icon size={15} />} {children}
    </button>
  );
}
function Switch({
  label,
  description,
  name,
  checked,
  onChange,
  disabled = false,
}) {
  return (
    <label className={`switchrow${disabled ? " disabled" : ""}`}>
      <span className="switchcopy">
        <strong>{label}</strong>
        {description && <small>{description}</small>}
      </span>
      <span className="switchcontrol">
        <input
          role="switch"
          type="checkbox"
          name={name}
          checked={checked}
          onChange={onChange}
          disabled={disabled}
          aria-label={label}
        />
        <span className="switchtrack" aria-hidden="true" />
        <span className="switchstatus">{checked ? "开启" : "关闭"}</span>
      </span>
    </label>
  );
}
function Head({ name, desc, children }) {
  return (
    <div className="pagehead">
      <div>
        <h1>{name}</h1>
        <p>{desc}</p>
      </div>
      <div className="row wrap">{children}</div>
    </div>
  );
}
function Panel({ name, desc, children, action, className = "" }) {
  return (
    <section className={`panel ${className}`}>
      <div className="panelhead">
        <div>
          <h2>{name}</h2>
          {desc && <p>{desc}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}
function Table({
  heads,
  rows,
  empty = "还没有记录",
  pageSize = Infinity,
  loading = false,
}) {
  const [page, setPage] = useState(1);
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const current = Math.min(page, pages);
  return (
    <div className="tablewrap" aria-busy={loading || undefined}>
      <table>
        <thead>
          <tr>
            {heads.map((v, i) => (
              <th key={i}>{v}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows
            .slice((current - 1) * pageSize, current * pageSize)
            .map((row, i) => (
              <tr key={i}>
                {row.map((v, j) => (
                  <td key={j}>{v}</td>
                ))}
              </tr>
            ))}
        </tbody>
      </table>
      {!rows.length && (
        <div className="noresults" role={loading ? "status" : undefined}>
          {loading ? "正在加载记录…" : empty}
        </div>
      )}
      {rows.length > pageSize && (
        <div className="tablefoot">
          <span>当前记录共 {rows.length} 条</span>
          <div className="row">
            <span>
              第 {current} / {pages} 页
            </span>
            <Button
              disabled={current <= 1}
              onClick={() => setPage(current - 1)}
            >
              上一页
            </Button>
            <Button
              disabled={current >= pages}
              onClick={() => setPage(current + 1)}
            >
              下一页
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
function Stats({ items }) {
  return (
    <div className={`stats ${items.length === 5 ? "stats-five" : ""}`}>
      {items.map(([name, value, note]) => (
        <div className="stat" key={name}>
          <div className="stat-label">{name}</div>
          <div className="stat-value">{value}</div>
          <div className="stat-note">{note}</div>
        </div>
      ))}
    </div>
  );
}
function Tabs({ group }) {
  const { page } = useApp();
  if (!tabs[group]) return null;
  return (
    <nav className="tabs" aria-label="页面分类">
      {tabs[group].map(([id, name]) => (
        <a href={"#" + id} className={page === id ? "active" : ""} key={id}>
          {name}
        </a>
      ))}
    </nav>
  );
}
function Notice({ children }) {
  return (
    <div className="callout">
      <AlertCircle size={17} />
      <div>{children}</div>
    </div>
  );
}
function Line({ name, desc, children }) {
  return (
    <div className="settingrow">
      <div>
        <h3>{name}</h3>
        <p>{desc}</p>
      </div>
      {children}
    </div>
  );
}
// Display only. Routing blocks on "never observed for this credential", not on age.
const BALANCE_FRESH_SECONDS = 3600;
const balanceLabels = {
  fresh: "已核验",
  zero: "已核验为零",
  stale: "数据过期",
  old_credential: "旧凭证时的观察",
  failed_keep: "最近查询失败，保留旧值",
  failed: "查询失败",
  unknown: "未查询",
};
// Unknown, failed, stale and a verified zero are different facts; never collapse them into 0.
function balanceState(a, now) {
  if (a.balance === null || a.balance === undefined)
    return a.balance_error ? "failed" : "unknown";
  if (
    a.balance_version !== undefined &&
    a.balance_version !== a.credential_version
  )
    return "old_credential";
  if (a.balance_error) return "failed_keep";
  if (!a.balance_at || now - a.balance_at > BALANCE_FRESH_SECONDS)
    return "stale";
  return a.balance <= 0 ? "zero" : "fresh";
}
function BalanceValue({ account, now }) {
  const state = balanceState(account, now);
  const known = account.balance !== null && account.balance !== undefined;
  return (
    <>
      {known ? nf(account.balance) : "—"}
      <small className={["failed", "failed_keep"].includes(state) ? "red" : ""}>
        {balanceLabels[state]}
      </small>
    </>
  );
}
// Per-platform only: units and billing rules differ, so platforms are never added together.
function creditSummary(rows, now) {
  const out = {
    total: rows.length,
    verified: 0,
    sum: 0,
    known: 0,
    knownSum: 0,
    zero: 0,
    stale: 0,
    unknown: 0,
    failed: 0,
    oldest: null,
  };
  for (const a of rows) {
    const state = balanceState(a, now);
    if (a.balance !== null && a.balance !== undefined) {
      out.known += 1;
      out.knownSum += a.balance;
    }
    if (state === "fresh" || state === "zero") {
      out.verified += 1;
      out.sum += a.balance;
      if (state === "zero") out.zero += 1;
      if (out.oldest === null || a.balance_at < out.oldest)
        out.oldest = a.balance_at;
    } else if (state === "unknown") out.unknown += 1;
    else if (state === "failed" || state === "failed_keep") out.failed += 1;
    else out.stale += 1;
  }
  return out;
}
function totals(rows) {
  const ready = rows.filter((a) => a.status === "ready").length,
    busy = rows.filter((a) => a.status === "busy").length;
  return {
    total: rows.length,
    ready,
    busy,
    unavailable: rows.length - ready - busy,
  };
}

function JobTable({ jobs, onDetail }) {
  const { data, mutate: action } = useApp();
  const [phase, setPhase] = useState({});
  const [confirmedJobs, setConfirmedJobs] = useState({});
  const inflight = useRef(new Set());
  useEffect(() => {
    setConfirmedJobs((old) => {
      const next = { ...old };
      for (const job of jobs) {
        if (
          next[job.id] &&
          !["failed", "uncertain", "superseded"].includes(job.status)
        )
          delete next[job.id];
      }
      return Object.keys(next).length === Object.keys(old).length ? old : next;
    });
  }, [jobs]);
  function setJobPhase(id, value) {
    setPhase((old) => {
      if (value === null) {
        if (!(id in old)) return old;
        const next = { ...old };
        delete next[id];
        return next;
      }
      return old[id] === value ? old : { ...old, [id]: value };
    });
  }
  async function confirmUpdated(id) {
    // Re-read the specific job: the action is only "done" once its fresh
    // record is confirmed, not on any global revision bump.
    try {
      const fresh = await api(`/api/jobs/${id}`);
      if (
        !fresh ||
        fresh.id !== id ||
        ["failed", "uncertain", "superseded"].includes(fresh.status)
      ) {
        setJobPhase(id, "checkfail");
        return;
      }
      setConfirmedJobs((old) => ({ ...old, [id]: fresh }));
      inflight.current.delete(id);
      setJobPhase(id, null);
    } catch {
      setJobPhase(id, "checkfail");
    }
  }
  async function run(id, task) {
    if (inflight.current.has(id)) return; // synchronous guard: same tick cannot double submit
    inflight.current.add(id);
    setJobPhase(id, "inflight");
    try {
      await task();
      setJobPhase(id, "awaiting");
      await confirmUpdated(id);
    } catch {
      inflight.current.delete(id);
      setJobPhase(id, null);
    }
  }
  return (
    <Table
      heads={[
        "账户 / 任务",
        "状态",
        "尝试次数",
        "安排 / 本次 / 下次",
        "排队 / 执行",
        "结果 / 下一步",
        "",
      ]}
      rows={jobs
        .map((job) =>
          confirmedJobs[job.id] &&
          ["failed", "uncertain", "superseded"].includes(job.status)
            ? { ...job, ...confirmedJobs[job.id] }
            : job,
        )
        .map((j) => [
          <>
            {j.account_display_name ||
              j.account_label ||
              j.account_id?.slice(0, 8) ||
              "账户未记录"}
            <small>{labels[j.kind] || j.kind}</small>
          </>,
          <>
            <Badge status={j.status} />
            <small>{j.receipt_pending ? "待回执核对" : ""}</small>
          </>,
          j.attempt,
          <>
            {tm(j.created_at)}
            <small>
              本次：
              {j.execution_started_at ? tm(j.execution_started_at) : "未开始"}
            </small>
            <small>
              {j.finished_at
                ? `完成：${tm(j.finished_at)}`
                : j.next_action_at
                  ? `下次：${tm(j.next_action_at)}`
                  : "尚未结束"}
            </small>
          </>,
          <>
            {j.queue_age_seconds == null
              ? "—"
              : `${Math.round(j.queue_age_seconds)}秒`}
            <small>
              {j.execution_seconds == null
                ? "未开始"
                : `执行 ${Math.round(j.execution_seconds)}秒`}
            </small>
          </>,
          <>
            {labels[j.error_code || j.result_code] ||
              j.error_code ||
              j.result_code ||
              "等待执行"}
            <small>
              凭证 v{j.credential_version} · 业务日 {j.business_day}
            </small>
          </>,
          <div className="row wrap">
            {onDetail && <Button onClick={() => onDetail(j.id)}>详情</Button>}
            {["failed", "uncertain"].includes(j.status) &&
            (["balance", "verify"].includes(j.kind) ||
              (j.kind === "renew" && j.has_renewal_candidate)) ? (
              <Button
                disabled={!!phase[j.id]}
                onClick={() =>
                  run(j.id, () =>
                    action(
                      `/api/jobs/${j.id}/retry`,
                      "POST",
                      undefined,
                      j.kind === "renew"
                        ? "已安排核验已收到的续期凭证。"
                        : j.kind === "verify"
                          ? "已重新安排模型回答验证。"
                          : "已重新安排查询任务。",
                    ),
                  )
                }
              >
                {phase[j.id] === "awaiting"
                  ? "等待更新…"
                  : phase[j.id] === "inflight"
                    ? "提交中…"
                    : j.kind === "renew"
                      ? "继续核验凭证"
                      : j.kind === "verify"
                        ? "重试模型验证"
                        : "重试查询"}
              </Button>
            ) : j.kind === "checkin" &&
              ["failed", "uncertain", "superseded"].includes(j.status) ? (
              <Button
                disabled={!!phase[j.id]}
                onClick={() =>
                  run(j.id, () =>
                    action(
                      `/api/jobs/${j.id}/reconcile-checkin`,
                      "POST",
                      undefined,
                      "已安排读取签到回执，不会再次领取。",
                    ),
                  )
                }
              >
                {phase[j.id] === "awaiting"
                  ? "等待更新…"
                  : phase[j.id] === "inflight"
                    ? "提交中…"
                    : "核对签到结果"}
              </Button>
            ) : null}
            {phase[j.id] === "checkfail" && (
              <Button onClick={() => confirmUpdated(j.id)}>重新查询</Button>
            )}
          </div>,
        ])}
    />
  );
}

export {
  Context,
  useApp,
  nf,
  tm,
  labels,
  eventLabels,
  nav,
  tabs,
  title,
  parent,
  tone,
  Badge,
  Button,
  Switch,
  Head,
  Panel,
  Table,
  Stats,
  Tabs,
  Notice,
  Line,
  totals,
  JobTable,
  balanceLabels,
  balanceState,
  BalanceValue,
  creditSummary,
};

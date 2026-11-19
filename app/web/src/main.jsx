import React, { lazy, Suspense, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import * as Dialog from "@radix-ui/react-dialog";
import {
  ChevronRight,
  RefreshCw,
  PanelLeft,
  Command,
  Sun,
  Moon,
  X,
  Database,
} from "lucide-react";
import { api, errors } from "./api";
import { createSnapshotLoader } from "./snapshot-loader";
import "./style.css";

import {
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
  Badge,
  Button,
  Panel,
  Notice,
  BalanceValue,
  Switch,
} from "./ui";
import Login from "./pages/Login";
import AdminCredentials from "./pages/AdminCredentials";
import AccountSettings from "./pages/AccountSettings";
import { useQuery } from "./query";
import { accountName, accountIdentity } from "./components/Management";
import Overview from "./pages/Overview";
import Accounts from "./pages/Accounts";
import Maintenance from "./pages/Maintenance";
import JobHistory from "./pages/JobHistory";
import Recovery from "./pages/Recovery";
import Register from "./pages/Register";
import Routing from "./pages/Routing";
import Keys from "./pages/Keys";
import AccountCreateDialog from "./pages/AccountCreateDialog";
import KeyCreateDialog from "./pages/KeyCreateDialog";
import SystemLogs from "./pages/SystemLogs";
import Logs from "./pages/Logs";
import RequestDetail from "./pages/RequestDetail";
import Wallet from "./pages/Wallet";
import KeyPolicy from "./pages/KeyPolicy";
const Analytics = lazy(() => import("./pages/Analytics"));
import Monitor from "./pages/Monitor";
import SettingsPage from "./pages/SettingsPage";
import Pending from "./pages/Pending";
import Imports from "./pages/Imports";
const Proxies = lazy(() =>
  import("./pages/Connections").then((module) => ({ default: module.Proxies })),
);
import Invitations from "./pages/Invitations";
class PageBoundary extends React.Component {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (this.state.failed)
      return (
        <section role="alert" className="noresults">
          <h2>页面暂时无法显示</h2>
          <p>
            可能是页面资源加载失败或运行异常。可以切换其他页面，或重新加载当前页面。
          </p>
          <p>重新加载会丢失当前未保存的输入。</p>
          <Button onClick={() => window.location.reload()}>重新加载页面</Button>
        </section>
      );
    return this.props.children;
  }
}
function JobsPage() {
  return (
    <>
      <h1 className="jobs-title">任务记录</h1>
      <Panel name="执行历史">
        <JobHistory />
      </Panel>
    </>
  );
}
function DrawerContent({ value }) {
  const { data, mutate, setDrawer } = useApp();
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [verificationModel, setVerificationModel] = useState(""),
    [accountSettings, setAccountSettings] = useState(false),
    [accountPane, setAccountPane] = useState("summary");
  const detail = useQuery(
    ["account", "credential"].includes(value.type)
      ? `/api/accounts/${encodeURIComponent(value.id)}`
      : null,
    data.revision,
  );
  const accountEvents = useQuery(
    value.type === "account"
      ? `/api/accounts/${encodeURIComponent(value.id)}/events?page=1&page_size=12`
      : null,
    data.revision,
  );
  const a =
    value.type === "account" ? detail.result?.account || detail.result : null;
  async function submit(e, path, method, body, message) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await mutate(path, method, body, message);
      setDrawer(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  if (value.type === "account" && !a)
    return <Notice>{detail.error || "正在读取账户详情…"}</Notice>;
  if (a)
    return (
      <>
        <div
          className="segmented account-detail-tabs"
          role="tablist"
          aria-label="账户详情分组"
        >
          {[
            ["summary", "概况"],
            ["wallet", "余额与积分"],
            ["maintenance", "维护与限制"],
            ["activity", "最近活动"],
          ].map(([v, n]) => (
            <button
              role="tab"
              aria-selected={accountPane === v}
              className={accountPane === v ? "active" : ""}
              key={v}
              onClick={() => setAccountPane(v)}
            >
              {n}
            </button>
          ))}
        </div>
        {accountPane === "summary" && (
          <>
            <div className="row wrap gap">
              <Badge status={a.status} />
              <Badge>{a.platform}</Badge>
            </div>
            <div className="kvs">
              {[
                ["原始身份", accountIdentity(a)],
                ["账户名称", accountName(a)],
                ["账户开关", a.enabled ? "开启" : "人工关闭"],
                [
                  "剩余积分/余额",
                  <BalanceValue account={a} now={data.as_of} />,
                ],
                ["凭证版本", `v${a.credential_version}`],
                ["业务占用", `${a.busy} / ${a.concurrency}`],
                [
                  "今日签到",
                  a.checkin_day === data.business_day ? "已成功" : "未完成",
                ],
                ["连续未收到验证码", a.relogin_failures],
                ["余额核查时间", a.balance_at ? tm(a.balance_at) : "尚未查询"],
                [
                  "最近余额查询",
                  a.balance_error
                    ? `失败：${errors[a.balance_error] || a.balance_error}`
                    : a.balance_at &&
                        a.balance !== null &&
                        a.balance !== undefined
                      ? "成功"
                      : "无记录",
                ],
                ["最后更新", tm(a.updated_at)],
              ].map(([n, v]) => (
                <div className="kv" key={n}>
                  <span>{n}</span>
                  <b>{v}</b>
                </div>
              ))}
            </div>
            {error && <Notice>{error}</Notice>}
          </>
        )}
        {accountSettings && (
          <AccountSettings
            account={a}
            open
            onClose={() => setAccountSettings(false)}
          />
        )}
        <Button onClick={() => setAccountSettings(true)}>账户设置</Button>
        {accountPane !== "summary" && error && <Notice>{error}</Notice>}
        {accountPane === "maintenance" && (
          <>
            {a.model_blocks?.length > 0 && (
              <div className="drawersection">
                <h3>模型与账户限制</h3>
                {a.model_blocks.map((b, i) => (
                  <div key={i} className="event">
                    <strong>{b.model === "*" ? "账户身份" : b.model}</strong>
                    <small>
                      {labels[b.reason] || b.reason} ·{" "}
                      {b.scope === "account" ? "影响整个账户" : "影响该模型"}
                    </small>
                  </div>
                ))}
                <Button
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    setError("");
                    try {
                      await mutate(
                        `/api/accounts/${a.id}/model-blocks/release`,
                        "POST",
                        undefined,
                        "限制已解除，下次真实请求将重新验证。",
                      );
                    } catch (e) {
                      setError(e.message);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  解除当前限制
                </Button>
              </div>
            )}
            {a.credential_status === "disabled" && (
              <Button
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError("");
                  try {
                    await mutate(
                      `/api/accounts/${a.id}/recheck`,
                      "POST",
                      undefined,
                      "已安排只读核验，等待实际回执。",
                    );
                  } catch (e) {
                    setError(e.message);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                重新核查停用状态
              </Button>
            )}
          </>
        )}
        {accountPane === "wallet" && <Wallet account={a} />}
        {accountPane === "maintenance" && (
          <>
            <div className="drawersection topgap">
              <h3>维护操作</h3>
              <div className="row wrap">
                {
                  <input
                    aria-label="回答验证模型"
                    placeholder="填写要验证的模型"
                    value={verificationModel}
                    onChange={(e) => setVerificationModel(e.target.value)}
                  />
                }
                {["checkin", "balance", "verify", "renew"].map((kind) => (
                  <Button
                    disabled={
                      (kind === "verify" && !verificationModel.trim()) ||
                      !data.adapter_catalog?.[a.platform]?.network_enabled ||
                      (kind !== "verify" &&
                        !data.adapter_catalog?.[
                          a.platform
                        ]?.implemented?.includes(kind))
                    }
                    key={kind}
                    onClick={() =>
                      mutate(
                        `/api/accounts/${a.id}/jobs`,
                        "POST",
                        {
                          kind,
                          ...(kind === "verify"
                            ? { model: verificationModel.trim() }
                            : {}),
                        },
                        "维护任务已保存。",
                      ).catch(() => {})
                    }
                  >
                    {labels[kind]}
                  </Button>
                ))}
                <Button
                  onClick={() => setDrawer({ type: "credential", id: a.id })}
                >
                  更新凭证
                </Button>
              </div>
              <p className="quietnote">
                回答验证会实际调用所填模型；余额充足不代表模型可调用，权限恢复以正常回答并结束为准。
              </p>
            </div>
          </>
        )}
        {accountPane === "activity" && (
          <div className="drawersection">
            <h3>最近活动</h3>
            {accountEvents.error && <Notice>{accountEvents.error}</Notice>}
            {(accountEvents.result?.items || []).map((e) => (
              <div className="event" key={e.id}>
                <time>{tm(e.created_at)}</time>
                <p>{eventLabels[e.code] || labels[e.code] || e.code}</p>
              </div>
            ))}
          </div>
        )}
      </>
    );
  if (value.type === "request") return <RequestDetail id={value.id} />;
  if (value.type === "key-policy") return <KeyPolicy id={value.id} />;
  const credAccount = detail.result?.account || detail.result;
  if (value.type === "credential") {
    if (!credAccount)
      return <Notice>{detail.error || "正在读取凭证版本…"}</Notice>;
    return (
      <form
        onSubmit={(e) => {
          const f = new FormData(e.currentTarget);
          submit(
            e,
            `/api/accounts/${value.id}/credential`,
            "PUT",
            {
              credential: f.get("credential"),
              expected_version: credAccount.credential_version,
            },
            "凭证已加密保存，等待验证。",
          );
        }}
      >
        {error && <Notice>{error}</Notice>}
        <Notice>
          更新基于凭证 v{credAccount?.credential_version}
          ；并发更新会拒绝覆盖。保存后加密入库，并按新版本重新核验。
        </Notice>
        <div className="field gap">
          <label htmlFor="credential">凭证内容</label>
          <input
            id="credential"
            name="credential"
            type="password"
            autoComplete="new-password"
            required
            maxLength={16384}
          />
        </div>
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? "保存中…" : "保存"}
        </Button>
      </form>
    );
  }
  return null;
}

function App() {
  const [page, setPage] = useState(location.hash.slice(1) || "overview"),
    [data, setData] = useState(null),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [login, setLogin] = useState(false),
    [sessionReady, setSessionReady] = useState(false),
    [manualRefresh, setManualRefresh] = useState(0),
    [mobile, setMobile] = useState(false),
    [mobileControls, setMobileControls] = useState(false),
    [dark, setDark] = useState(() => {
      try {
        return localStorage.getItem("console-theme") === "dark";
      } catch {
        return false;
      }
    }),
    [filter, setFilter] = useState({ platform: "全部平台", status: "all" }),
    [drawer, updateDrawer] = useState(null);
  useEffect(() => {
    if (!mobile) return;
    const close = (e) => {
      if (e.key === "Escape") setMobile(false);
    };
    addEventListener("keydown", close);
    return () => removeEventListener("keydown", close);
  }, [mobile]);
  const trigger = useRef(null),
    mounted = useRef(true);
  const setDrawer = (d) => {
    if (d && !drawer) trigger.current = document.activeElement;
    updateDrawer(d);
  };
  const loader = useRef(null);
  if (!loader.current)
    loader.current = createSnapshotLoader({
      fetchSnapshot: (signal) =>
        api("/api/snapshot", "GET", undefined, { signal }),
      onSuccess: (next) => {
        if (!mounted.current) return;
        setData((old) => (!old || next.revision >= old.revision ? next : old));
        setError("");
        setLogin(false);
        setSessionReady(true);
      },
      onError: (e) => {
        if (!mounted.current) return;
        setError(
          e.name === "AbortError" ? "读取数据超时，请刷新重试。" : e.message,
        );
        if (e.code === "login_required") {
          setLogin(true);
          setSessionReady(false);
        }
      },
    });
  const refresh = () => loader.current.refresh();
  useEffect(() => {
    mounted.current = true;
    refresh();

    const navigate = () => {
      setPage(location.hash.slice(1) || "overview");
      setMobile(false);
      updateDrawer(null);
      window.scrollTo(0, 0);
    };
    addEventListener("hashchange", navigate);
    return () => {
      mounted.current = false;

      loader.current.cancel();
      removeEventListener("hashchange", navigate);
    };
  }, []);
  useEffect(() => {
    if (!sessionReady) return;
    const poll = setInterval(refresh, 60_000);
    return () => clearInterval(poll);
  }, [sessionReady]);
  useEffect(() => {
    if (!sessionReady) return;
    const source = new EventSource("/api/events");
    // Busy systems bump the revision many times a second; one snapshot per 2s is enough.
    let last = 0,
      queued = null;
    source.addEventListener("revision", () => {
      const wait = last + 2000 - Date.now();
      if (wait <= 0) {
        last = Date.now();
        refresh();
      } else if (!queued)
        queued = setTimeout(() => {
          queued = null;
          last = Date.now();
          refresh();
        }, wait);
    });
    source.addEventListener("unavailable", () =>
      setError("数据库暂时不可用，显示的是上次收到的数据。"),
    );
    source.onerror = () =>
      setError("实时连接暂时断开，正在重连，后台仍会每 60 秒刷新。");
    return () => {
      clearTimeout(queued);
      source.close();
    };
  }, [sessionReady]);
  useEffect(() => {
    const expired = () => {
      loader.current.cancel();
      setSessionReady(false);
      setLogin(true);
      setData(null);
      updateDrawer(null);
    };
    addEventListener("console-session-expired", expired);
    return () => removeEventListener("console-session-expired", expired);
  }, []);
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("console-theme", dark ? "dark" : "light");
    } catch {}
  }, [dark]);
  useEffect(() => {
    if (message) {
      const t = setTimeout(() => setMessage(""), 5000);
      return () => clearTimeout(t);
    }
  }, [message]);
  async function mutate(path, method, body, notice) {
    try {
      const r = await api(path, method, body);
      // Refresh queues a background snapshot read; it does not confirm list freshness.
      refresh();
      setMessage(
        typeof notice === "function" ? notice(r) : notice || "已保存。",
      );
      return r;
    } catch (e) {
      setMessage(e.message);
      throw e;
    }
  }
  const openAccounts = (platform, status) => {
    setFilter({ platform, status });
    location.hash = "accounts";
  };
  const screens = {
    proxy: Proxies,
    invite: Invitations,
    overview: Overview,
    accounts: Accounts,
    register: Register,
    maintenance: Maintenance,
    automaintenance: SettingsPage,
    jobs: JobsPage,
    models: Routing,
    recovery: Recovery,
    routing: Routing,
    keys: Keys,
    logs: Logs,
    systemlogs: SystemLogs,
    analytics: Analytics,
    monitor: Monitor,
    settings: SettingsPage,
    admin: AdminCredentials,
    imports: Imports,
  };
  const Screen = screens[page];
  const drawerTitle =
    drawer?.type === "account"
      ? drawer.account
        ? accountName(drawer.account)
        : "账户详情"
      : drawer?.type === "key-policy"
        ? "密钥权限与配额"
        : drawer?.type === "request"
          ? "请求详情"
          : drawer?.type === "credential"
            ? "更新凭证"
            : drawer?.type === "key"
              ? "创建密钥"
              : "建立账户记录";
  if (login)
    return (
      <Login
        error={error === errors.login_required ? "" : error}
        onLogin={async () => {
          loader.current.cancel();
          setError("");
          setLogin(false);
          await refresh();
        }}
      />
    );
  return (
    <Context.Provider
      value={{
        page,
        message,
        data,
        filter,
        setFilter,
        setDrawer,
        mutate,
        action: (...args) => mutate(...args).catch(() => undefined),
        openAccounts,
        refresh,
        manualRefresh,
      }}
    >
      {mobile && (
        <button
          className="nav-scrim"
          aria-label="关闭导航遮罩"
          onClick={() => setMobile(false)}
        />
      )}
      <aside className={`sidebar ${mobile ? "mobileopen" : ""}`}>
        <button
          className="btn icon ghost nav-close"
          aria-label="关闭导航"
          onClick={() => setMobile(false)}
        >
          <X size={18} />
        </button>
        <a className="brand row" href="#overview">
          <span className="brandmark">
            <Command size={20} />
          </span>
          <strong>统一账户平台</strong>
        </a>
        <nav aria-label="主导航">
          {[
            ["工作台", ["overview", "accounts", "register", "invite"]],
            ["网关与网络", ["keys", "models", "routing", "proxy"]],
            [
              "任务与维护",
              ["maintenance", "automaintenance", "jobs", "recovery"],
            ],
            ["报表与诊断", ["analytics", "logs", "systemlogs"]],
            ["系统", ["settings"]],
          ].map(([group, ids]) => (
            <section key={group} aria-label={group}>
              <div className="navlabel">{group}</div>
              {ids
                .map((navId) => nav.find(([id]) => id === navId))
                .filter(Boolean)
                .map(([id, name, Icon]) => (
                  <React.Fragment key={id}>
                    <a
                      href={"#" + id}
                      onClick={() => {
                        setMobile(false);
                        updateDrawer(null);
                      }}
                      className={`navitem ${parent(page) === id ? "active" : ""}`}
                      aria-current={parent(page) === id ? "page" : undefined}
                    >
                      <Icon size={18} />
                      {name}
                    </a>
                  </React.Fragment>
                ))}
            </section>
          ))}
        </nav>
        <div className="sidebottom">
          <div className="row">
            <Database size={18} />
            <div>
              <strong>
                {data?.runtime?.database === "postgresql"
                  ? "PostgreSQL"
                  : data?.runtime?.database === "sqlite"
                    ? "SQLite"
                    : "数据库"}
              </strong>
              <small className="muted" style={{ display: "block" }}>
                {data?.runtime?.upstream_enabled === false
                  ? "外部执行已停用"
                  : "外部执行已开启"}
              </small>
            </div>
          </div>
        </div>
      </aside>
      <div className="shell">
        <header className="topbar">
          <div className="row">
            <button
              className="btn icon ghost mobilemenu"
              aria-label="切换导航"
              onClick={() => setMobile(!mobile)}
            >
              <PanelLeft size={18} />
            </button>
            <span className="breadcrumb">工作空间</span>
            <ChevronRight size={13} />
            <span>{title[page] || "运行总览"}</span>
          </div>
          <button
            className="btn mobile-controls-toggle"
            aria-expanded={mobileControls}
            aria-label="展开工作台控制"
            onClick={() => setMobileControls(!mobileControls)}
          >
            控制
          </button>
          <div
            className={`row topbar-actions ${mobileControls ? "controls-open" : ""}`}
          >
            {data?.runtime?.upstream_enabled === false && (
              <Badge status="failed">外部执行已停用</Badge>
            )}
            <Button
              onClick={async () => {
                try {
                  await api("/api/session", "DELETE");
                  loader.current.cancel();
                  setSessionReady(false);
                  setData(null);
                  setDrawer(null);
                  setLogin(true);
                  setError("");
                } catch (e) {
                  setError(e.message);
                }
              }}
            >
              退出登录
            </Button>
            <Button
              icon={RefreshCw}
              onClick={() => {
                setManualRefresh((n) => n + 1);
                refresh();
              }}
            >
              刷新
            </Button>
            <button
              className="btn icon ghost"
              aria-label={dark ? "切换浅色" : "切换深色"}
              onClick={() => setDark(!dark)}
            >
              {dark ? <Sun size={17} /> : <Moon size={17} />}
            </button>
          </div>
        </header>
        <main>
          {error && (
            <div role="alert">
              <Notice>{error}</Notice>
            </div>
          )}
          {data ? (
            Screen ? (
              <PageBoundary key={page}>
                <Suspense
                  fallback={
                    <div className="noresults" role="status">
                      正在加载页面…
                    </div>
                  }
                >
                  <Screen key={page} />
                </Suspense>
              </PageBoundary>
            ) : (
              <Pending kind={page} />
            )
          ) : (
            <div className="noresults">正在读取数据…</div>
          )}
          <footer className="pagefoot">
            <span>
              {data
                ? `快照 #${data.revision} · ${tm(data.as_of)}（北京时间）`
                : "等待数据"}
              {error ? " · 数据可能过期" : ""}
            </span>
            <span>{data ? `版本 ${data.runtime.version}` : ""}</span>
          </footer>
        </main>
      </div>
      {data && drawer?.type === "account-create" && (
        <AccountCreateDialog
          initialPlatform={drawer.platform}
          initialMode={drawer.mode}
          onClose={() => setDrawer(null)}
        />
      )}
      {data && drawer?.type === "key" && (
        <KeyCreateDialog onClose={() => setDrawer(null)} />
      )}
      <Dialog.Root
        open={
          !!drawer && drawer.type !== "key" && drawer.type !== "account-create"
        }
        onOpenChange={(open) => !open && setDrawer(null)}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="drawerOverlay" />
          <Dialog.Content
            className="drawer"
            onCloseAutoFocus={(e) => {
              if (trigger.current?.isConnected) {
                e.preventDefault();
                trigger.current.focus();
              }
            }}
          >
            <div className="drawerhead">
              <div>
                <Dialog.Title>{drawerTitle}</Dialog.Title>
                <Dialog.Description className="drawerDesc">
                  账户数据与执行记录
                </Dialog.Description>
              </div>
              <Dialog.Close asChild>
                <button className="btn icon ghost" aria-label="关闭详情">
                  <X size={20} />
                </button>
              </Dialog.Close>
            </div>
            {drawer && data && (
              <DrawerContent
                value={drawer}
                key={drawer.type + (drawer.id || "")}
              />
            )}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      {message && (
        <div className="toast" role="status">
          {message}
        </div>
      )}
    </Context.Provider>
  );
}

createRoot(document.getElementById("root")).render(<App />);

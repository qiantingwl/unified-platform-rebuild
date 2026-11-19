import React, { useEffect, useState, useRef } from "react";
import { api, errors } from "../api";
import { createRecordLoader } from "../record-loader";
import { useApp, Head, Panel, Button, Table, Notice, tm } from "../ui";
import {
  Modal,
  Pagination,
  ActionMenu,
  subscriptionLabel,
} from "../components/Management";
import { queryString, useDebounced, useQuery } from "../query";
import ProxyPools from "./ProxyPools";
import AccountProxyList from "./AccountProxyList";
import SubscriptionManager from "./SubscriptionManager";
import "./proxy.css";
import { ProxyReferences, ProxyBatches } from "./ProxyDetails";
import { useProxyColumns } from "./ProxyColumns";

export function useRecords(path) {
  const { data } = useApp();
  const [result, setResult] = useState({
    path: null,
    records: null,
    error: "",
  });
  const loader = useRef(null);
  useEffect(() => {
    const current = createRecordLoader({
      fetchRecords: (signal) => api(path, "GET", undefined, { signal }),
      onSuccess: (records) => setResult({ path, records, error: "" }),
      onError: (e) =>
        setResult((old) => ({
          path,
          records: old.path === path ? old.records : null,
          error: e.message,
        })),
    });
    loader.current = current;
    return () => {
      current.stop();
      if (loader.current === current) loader.current = null;
    };
  }, [path]);
  useEffect(() => {
    loader.current?.refresh();
  }, [path, data.revision]);
  return result.path === path ? [result.records, result.error] : [null, ""];
}
const statusNames = {
  unknown: "待确认",
  disabled: "已停用",
  retired: "已退役",
  pending_kernel: "等待内核",
  untested: "未做通用检测",
  ready: "就绪",
  active: "启用",
};
const resultNames = {
  target_forbidden: "目标拒绝（403）",
  target_rate_limited: "目标限流（429）",
  target_auth_required: "目标要求认证",
  target_external_redirect: "站外跳转",
  target_root_not_found: "目标根路径404",
  target_timeout: "目标超时",
  proxy_connect_timeout: "代理连接超时",
  network_error: "网络失败",
  target_http_ok: "目标网站可达",
};

function NodeForm({ node, onSaved, onBusyChange }) {
  const sourceManaged = !!(
    node?.data.subscription_id || node?.data.subscription_ids?.length
  );
  const { mutate } = useApp();
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  return (
    <form
      className="proxy-form"
      onSubmit={async (e) => {
        e.preventDefault();
        if (busy || submitting.current) return;
        submitting.current = true;
        const f = new FormData(e.currentTarget);
        setBusy(true);
        onBusyChange?.(true);
        setError("");
        try {
          await mutate(
            node ? `/api/proxy-directory/${node.id}` : "/api/proxies",
            node ? "PUT" : "POST",
            {
              name: f.get("name"),
              ...(node ? { expected_version: node.version } : {}),
              ...(f.get("url") ? { url: f.get("url") } : {}),
            },
            "节点已保存。",
          );
          onSaved();
        } catch (e) {
          setError(e.message);
        } finally {
          submitting.current = false;
          setBusy(false);
          onBusyChange?.(false);
        }
      }}
    >
      {" "}
      <label className="field">
        节点名称
        <input
          name="name"
          required
          maxLength={100}
          defaultValue={node?.name || ""}
        />
      </label>
      <label className="field">
        {node ? "替换连接地址（留空保留）" : "连接地址或单个节点链接"}
        <input
          name="url"
          type="password"
          autoComplete="new-password"
          disabled={sourceManaged}
          required={!node}
          placeholder="HTTP / SOCKS / VMess / VLESS / SS / Trojan / Hysteria2 / WireGuard JSON"
        />
      </label>
      <small>
        {sourceManaged
          ? "此节点的连接材料由订阅管理；名称编辑会保留，刷新不会覆盖。"
          : "地址只写入，不回显。已有出口引用时不能替换连接材料。"}
      </small>
      {error && <Notice>{error}</Notice>}
      <Button type="submit" disabled={busy}>
        保存节点
      </Button>
    </form>
  );
}
export function Proxies() {
  const columnLayout = useProxyColumns();
  const { data, mutate } = useApp();
  const [q, setQ] = useState(""),
    [source, setSource] = useState(""),
    [sort, setSort] = useState("name"),
    [protocol, setProtocol] = useState(""),
    [status, setStatus] = useState(""),
    [page, setPage] = useState(1),
    [size, setSize] = useState(50),
    [selected, setSelected] = useState([]),
    [dialog, setDialog] = useState(""),
    [editing, setEditing] = useState(null),
    [referencing, setReferencing] = useState(null),
    [confirm, setConfirm] = useState(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [nodeBusy, setNodeBusy] = useState(false),
    [platform, setPlatform] = useState(data.platforms[0] || "");
  const inflight = useRef(false);
  const [awaiting, setAwaiting] = useState(false),
    [refreshGeneration, setRefreshGeneration] = useState(0);
  const search = useDebounced(q);
  const dirPath = `/api/proxy-directory?${queryString({ q: search, source, protocol, status, sort, page, page_size: size })}`;
  const directory = useQuery(dirPath, data.revision, refreshGeneration, {
    retainPrevious: true,
  });
  useEffect(() => {
    if (
      awaiting &&
      !directory.loading &&
      !directory.error &&
      directory.generation === refreshGeneration
    ) {
      inflight.current = false;
      setAwaiting(false);
    }
  }, [
    awaiting,
    directory.loading,
    directory.error,
    directory.generation,
    refreshGeneration,
  ]);
  const [subs, subError] = useRecords("/api/proxy-subscriptions"),
    [kernel, kernelError] = useRecords("/api/proxy-kernel");
  const nodes = directory.result?.nodes || [],
    subscriptions = subs?.subscriptions || [];
  const checks = useQuery(
    nodes.length
      ? `/api/proxy-target-checks?${queryString({ platform, proxy_ids: nodes.map((n) => n.id).join(",") })}`
      : null,
    data.revision,
  );
  const checkMap = new Map(
    (checks.result?.checks || []).map((r) => [r.data.proxy_id, r.data]),
  );
  useEffect(() => {
    setSelected([]);
  }, [search, source, protocol, status, sort, page, size]);
  const change = (setter, value) => {
    setter(value);
    setPage(1);
    setSelected([]);
  };
  async function run(path, method, body) {
    if (inflight.current) return;
    inflight.current = true;
    setBusy(true);
    setError("");
    try {
      await mutate(path, method, body, "节点操作已保存。");
      setSelected([]);
      setConfirm(null);
      // Restart the rendered query after the write; discard pre-write reads.
      setAwaiting(true);
      setRefreshGeneration((value) => value + 1);
    } catch (e) {
      inflight.current = false;
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  function result(node) {
    const c = checkMap.get(node.id);
    if (!c) return "未检测";
    if (c.node_version !== node.version) return "节点变更，需重新检测";
    if (c.status !== "completed")
      return c.status === "running" ? "检测中" : "结果待更新";
    if (!c.checked_at || Date.now() / 1000 - c.checked_at > 86400)
      return "结果已过期";
    return c.eligible
      ? "目标网站已响应 · 未测模型"
      : resultNames[c.code] || c.code;
  }
  return (
    <div className="ops-page ops-proxy">
      <Head name="代理节点" desc="管理节点与订阅，按平台检测真实目标网站。" />
      <div className="proxy-kernel" role="status">
        <strong>Mihomo 内核</strong>
        <span>
          {!kernel
            ? "正在读取"
            : kernel.enabled
              ? kernel.running
                ? "运行中"
                : "未就绪"
              : "未启用"}
        </span>
        <span>{kernel?.nodes ?? "—"} 个内核节点</span>
        <span>
          版本 {kernel?.version || "未知"} · 配置代次 {kernel?.generation ?? 0}
        </span>
        <span>
          {!kernel
            ? "等待状态"
            : kernel.checking
              ? "正在检查监听"
              : kernel.listeners_ready
                ? "监听已核实"
                : "监听未就绪"}
        </span>
        <span>最近轮询 {tm(kernel?.last_tick)}</span>
        {kernel?.last_error && <span>{kernel.last_error}</span>}
      </div>
      {(error ||
        directory.error ||
        subError ||
        kernelError ||
        checks.error) && (
        <Notice>
          {error || directory.error || subError || kernelError || checks.error}
        </Notice>
      )}
      {awaiting && directory.error && (
        <Notice>
          操作已保存，列表尚未更新；暂不允许再次提交。
          <Button onClick={() => directory.reload()}>重新查询</Button>
        </Notice>
      )}
      {awaiting && (
        <div className="proxy-kernel" role="status">
          <strong>操作已提交</strong>
          <span>正在确认列表已更新…</span>
        </div>
      )}
      <Panel name="节点">
        <div className="proxy-toolbar proxy-toolbar-filters">
          <input
            aria-label="搜索节点"
            placeholder="搜索节点名称或ID"
            value={q}
            onChange={(e) => change(setQ, e.target.value)}
          />
          <select
            aria-label="节点来源"
            value={source}
            onChange={(e) => change(setSource, e.target.value)}
          >
            <option value="">全部来源</option>
            <option value="manual">手动</option>
            <option value="subscription">全部订阅</option>
            {subscriptions.map((s) => (
              <option key={s.id} value={s.id}>
                {subscriptionLabel(s)}
              </option>
            ))}
          </select>
          <select
            aria-label="协议"
            value={protocol}
            onChange={(e) => change(setProtocol, e.target.value)}
          >
            <option value="">全部协议</option>
            {[
              "http",
              "https",
              "socks5",
              "socks5h",
              "vmess",
              "vless",
              "ss",
              "trojan",
              "hysteria2",
              "wireguard",
            ].map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
          <select
            aria-label="节点状态"
            value={status}
            onChange={(e) => change(setStatus, e.target.value)}
          >
            <option value="">全部状态</option>
            {Object.entries(statusNames).map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
          <select
            aria-label="节点排序"
            value={sort}
            onChange={(e) => change(setSort, e.target.value)}
          >
            <option value="name">按名称</option>
            <option value="updated">最近变更</option>
            <option value="protocol">按协议</option>
          </select>
          <Button
            onClick={() => {
              directory.reload();
              checks.reload();
            }}
          >
            刷新
          </Button>
        </div>
        <div className="proxy-toolbar proxy-toolbar-primary">
          <Button
            variant="primary"
            onClick={() => {
              setEditing(null);
              setDialog("node");
            }}
          >
            添加节点
          </Button>
          <Button onClick={() => setDialog("subscriptions")}>订阅管理</Button>
          <select
            aria-label="检测平台"
            value={platform}
            onChange={(e) => setPlatform(e.target.value)}
          >
            {data.platforms.map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
          <ActionMenu title="代理管理" triggerLabel="更多管理">
            <Button onClick={() => setDialog("accounts")}>账户绑定</Button>
            <Button onClick={() => setDialog("routes")}>
              代理池与出口策略
            </Button>
            <Button onClick={() => setDialog("batches")}>检测进度</Button>
            <Button onClick={() => setDialog("columns")}>显示列</Button>
          </ActionMenu>
        </div>
        {selected.length > 0 && (
          <div className="proxy-toolbar proxy-toolbar-batch">
            <Button
              disabled={
                busy || awaiting || directory.isPrevious || !selected.length
              }
              onClick={() =>
                run("/api/proxy-target-checks", "POST", {
                  proxy_ids: selected,
                  platforms: [platform],
                })
              }
            >
              检测所选（{selected.length}）
            </Button>
            <Button
              disabled={
                busy || awaiting || directory.isPrevious || !selected.length
              }
              onClick={() => setConfirm({ action: "enable", ids: selected })}
            >
              启用所选
            </Button>
            <Button
              disabled={
                busy || awaiting || directory.isPrevious || !selected.length
              }
              onClick={() => setConfirm({ action: "disable", ids: selected })}
            >
              停用所选
            </Button>
            <Button
              disabled={
                busy || awaiting || directory.isPrevious || !selected.length
              }
              onClick={() => setConfirm({ action: "delete", ids: selected })}
            >
              删除所选
            </Button>
            <small>
              仅操作勾选ID；换页或筛选后清空选择，不自动选择整个筛选集。
            </small>
          </div>
        )}
        <Table
          loading={directory.loading}
          pageSize={100}
          heads={columnLayout.arrange([
            <input
              aria-label="选择当前页节点"
              type="checkbox"
              checked={
                nodes.length > 0 && nodes.every((n) => selected.includes(n.id))
              }
              onChange={(e) =>
                setSelected(e.target.checked ? nodes.map((n) => n.id) : [])
              }
            />,
            "节点 / 来源",
            "协议",
            "节点状态",
            `${platform} · 网站检测`,
            "检测时间",
            "操作",
          ])}
          rows={nodes.map((n) =>
            columnLayout.arrange([
              <input
                aria-label={`选择 ${n.name}`}
                type="checkbox"
                checked={selected.includes(n.id)}
                onChange={(e) =>
                  setSelected((old) =>
                    e.target.checked
                      ? [...old, n.id]
                      : old.filter((id) => id !== n.id),
                  )
                }
              />,
              <>
                {n.name}
                <small className="proxy-source">
                  {(n.data.subscription_ids || [])
                    .map((id) =>
                      subscriptionLabel(subscriptions.find((s) => s.id === id)),
                    )
                    .join("、") || "手动"}
                </small>
                {n.address &&
                  !["disabled", "retired", "pending_kernel"].includes(
                    n.data.status,
                  ) &&
                  (!n.data.kernel_state || kernel?.listeners_ready) && (
                    <small className="proxy-source">{n.address}</small>
                  )}
              </>,
              n.data.scheme,
              statusNames[n.data.status] || "待确认",
              <>
                {result(n)}
                {checkMap.has(n.id) && (
                  <small className="proxy-source">
                    HTTP {checkMap.get(n.id).http_status ?? "—"} ·{" "}
                    {checkMap.get(n.id).latency_ms ?? "—"} ms
                  </small>
                )}
              </>,
              tm(checkMap.get(n.id)?.checked_at),
              <ActionMenu
                title={`${n.name} · 节点操作`}
                disabled={busy || awaiting || directory.isPrevious}
              >
                {(busy || awaiting) && <small>操作进行中…</small>}
                {n.address &&
                  !["disabled", "retired", "pending_kernel"].includes(
                    n.data.status,
                  ) &&
                  (!n.data.kernel_state || kernel?.listeners_ready) && (
                    <Button
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(n.address);
                        } catch {
                          setError("复制失败，请手动选择地址。");
                        }
                      }}
                    >
                      复制无认证地址
                    </Button>
                  )}
                <Button
                  disabled={busy || awaiting || directory.isPrevious}
                  onClick={() => {
                    setReferencing(n);
                    setDialog("references");
                  }}
                >
                  用途（账户 {n.account_bindings || 0}）
                </Button>
                <Button
                  disabled={busy || awaiting || directory.isPrevious}
                  onClick={() => {
                    setEditing(n);
                    setDialog("node");
                  }}
                >
                  编辑
                </Button>
                <Button
                  disabled={busy || awaiting || directory.isPrevious}
                  onClick={() =>
                    setConfirm({
                      action:
                        n.data.status === "disabled" ||
                        n.data.status === "retired"
                          ? "enable"
                          : "disable",
                      ids: [n.id],
                    })
                  }
                >
                  {["disabled", "retired"].includes(n.data.status)
                    ? "启用"
                    : "停用"}
                </Button>
                <Button
                  disabled={
                    busy ||
                    awaiting ||
                    ["disabled", "retired", "pending_kernel"].includes(
                      n.data.status,
                    )
                  }
                  onClick={() =>
                    run("/api/proxy-target-checks", "POST", {
                      proxy_ids: [n.id],
                      platforms: [platform],
                    })
                  }
                >
                  检测
                </Button>
                <Button
                  disabled={busy || awaiting || directory.isPrevious}
                  onClick={() => setConfirm({ action: "delete", ids: [n.id] })}
                >
                  删除
                </Button>
              </ActionMenu>,
            ]),
          )}
        />
        <Pagination
          page={page}
          pageSize={size}
          total={directory.result?.total}
          loading={directory.loading}
          onPage={setPage}
          onPageSize={(n) => change(setSize, n)}
        />
        <p className="quietnote">
          目标网址：
          {(checks.result?.target_groups?.[platform] || []).join("、") ||
            "等待检测配置"}
          。检测只说明所选目标网站的连通性，不代表账户或模型权限；固定节点停用后不会回退直连。
        </p>
      </Panel>
      <Modal
        open={dialog === "batches"}
        onOpenChange={(open) => !open && setDialog("")}
        title="检测批次进度"
      >
        <ProxyBatches />
      </Modal>
      <Modal
        open={dialog === "references"}
        onOpenChange={(open) => !open && setDialog("")}
        title="节点出口用途"
      >
        {referencing && (
          <ProxyReferences key={referencing.id} node={referencing} />
        )}
      </Modal>
      <Modal
        open={dialog === "columns"}
        onOpenChange={(open) => !open && setDialog("")}
        title="节点显示列"
      >
        {columnLayout.settings}
      </Modal>
      <Modal
        open={dialog === "node"}
        onOpenChange={(open) => !open && !nodeBusy && setDialog("")}
        busy={nodeBusy}
        title={editing ? "编辑节点" : "添加节点"}
      >
        <NodeForm
          key={editing?.id || "new"}
          node={editing}
          onBusyChange={setNodeBusy}
          onSaved={() => {
            setDialog("");
            directory.reload();
          }}
        />
      </Modal>
      {dialog === "subscriptions" && (
        <SubscriptionManager onClose={() => setDialog("")} />
      )}
      {dialog === "accounts" && (
        <AccountProxyList onClose={() => setDialog("")} />
      )}
      <Modal
        open={dialog === "routes"}
        onOpenChange={(open) => !open && setDialog("")}
        title="代理池与出口策略"
      >
        <ProxyPools subscriptions={subscriptions} />
      </Modal>
      <Modal
        open={!!confirm}
        onOpenChange={(open) => !open && !busy && setConfirm(null)}
        busy={busy}
        title="确认节点操作"
      >
        <p>
          将
          {{ enable: "启用", disable: "停用", delete: "删除" }[confirm?.action]}
          明确选择的 {confirm?.ids.length || 0}{" "}
          个节点。删除有引用的节点会拒绝整批操作；订阅节点保留退役记录防止刷新重建；停用不修改绑定。
        </p>
        {error && <Notice>{error}</Notice>}
        <Button
          disabled={busy}
          onClick={() => run("/api/proxy-directory/batch", "POST", confirm)}
        >
          确认执行
        </Button>
      </Modal>
    </div>
  );
}
export { default as Invitations } from "./Invitations";

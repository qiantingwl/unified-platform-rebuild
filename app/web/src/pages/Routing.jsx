import React, { useEffect, useRef, useState } from "react";
import { useApp, Head, Panel, Notice, Button, Table, Switch, tm } from "../ui";
import { queryString, useDebounced, useQuery } from "../query";
import {
  AccountPicker,
  Modal,
  Pagination,
  accountName,
  accountIdentity,
} from "../components/Management";
const strategies = {
  lru: "最久未使用",
  balance_high: "余额从高到低",
  balance_low: "余额从低到高",
  added_new: "最近加入",
  added_old: "最早加入",
};
function MemberRow({ id, account, onRemove }) {
  const detail = useQuery(
    account ? null : `/api/accounts/${encodeURIComponent(id)}`,
  );
  const a = account || detail.result?.account || detail.result;
  return (
    <div className="group-member-row">
      <span>
        {a ? accountName(a) : detail.error ? "账户不可读取" : "正在读取账户…"}
        <small>{a ? accountIdentity(a) : id}</small>
      </span>
      <Button variant="ghost" onClick={() => onRemove(id)}>
        移除
      </Button>
    </div>
  );
}
function GroupMembers({
  ids,
  platform,
  groupId,
  savedIds = [],
  known = {},
  onRemove,
}) {
  const [page, setPage] = useState(1),
    [q, setQ] = useState("");
  const search = useDebounced(q);
  useEffect(() => setPage(1), [search]);
  const query = useQuery(
    groupId
      ? `/api/model-routes/${encodeURIComponent(platform)}/groups/${encodeURIComponent(groupId)}/accounts?${queryString({ page, page_size: 25, q: search })}`
      : null,
  );
  const current = Math.min(page, Math.max(1, Math.ceil(ids.length / 25)));
  const pending = groupId ? ids.filter((id) => !savedIds.includes(id)) : [];
  const matches = (id) =>
    !search ||
    [id, known[id]?.display_name, known[id]?.identity_hint]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(search.toLowerCase());
  const local = ids.filter(matches);
  return (
    <>
      {groupId && (
        <div className="toolbar">
          <input
            aria-label="搜索组内账户"
            placeholder="原名称 / UID / 脱敏身份"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      )}
      {query.error && (
        <Notice>
          {query.error}
          <Button onClick={query.reload}>重试</Button>
        </Notice>
      )}
      <div>
        {(groupId
          ? (query.result?.items || []).filter((a) => ids.includes(a.id))
          : local
              .slice((current - 1) * 25, current * 25)
              .map((id) => known[id] || { id })
        ).map((a) => (
          <MemberRow
            key={a.id}
            id={a.id}
            account={a.display_name ? a : known[a.id]}
            onRemove={onRemove}
          />
        ))}
      </div>
      {groupId && pending.filter(matches).length > 0 && (
        <>
          <p className="quietnote">本次新增，保存后纳入组内查询</p>
          {pending
            .filter(matches)
            .slice(0, 25)
            .map((id) => (
              <MemberRow
                key={id}
                id={id}
                account={known[id]}
                onRemove={onRemove}
              />
            ))}
        </>
      )}
      <Pagination
        page={groupId ? page : current}
        pageSize={25}
        total={groupId ? query.result?.total : local.length}
        loading={groupId && query.loading}
        onPage={setPage}
      />
    </>
  );
}
function RouteEditor({ platform, open, onClose }) {
  const { data, mutate } = useApp();
  const query = useQuery(
    open ? `/api/model-routes/${encodeURIComponent(platform)}` : null,
    data.revision,
  );
  const [draft, setDraft] = useState(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [member, setMember] = useState(""),
    [memberNames, setMemberNames] = useState({});
  const submitting = useRef(false);
  useEffect(() => {
    setDraft(null);
    setError("");
  }, [open, platform]);
  useEffect(() => {
    if (query.result && !draft) setDraft(structuredClone(query.result));
  }, [query.result, draft]);
  const update = (section, index, patch) =>
    setDraft((old) => ({
      ...old,
      [section]: old[section].map((r, i) =>
        i === index ? { ...r, ...patch } : r,
      ),
    }));
  return (
    <Modal
      open={open}
      busy={busy}
      onOpenChange={(v) => !v && !submitting.current && onClose()}
      title={`${platform} · 模型映射与账户组`}
      description="仅在该平台内路由，保存时校验版本与账户归属"
    >
      {(error || query.error) && <Notice>{error || query.error}</Notice>}
      {draft ? (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            if (submitting.current) return;
            submitting.current = true;
            setBusy(true);
            setError("");
            try {
              await mutate(
                `/api/model-routes/${encodeURIComponent(platform)}`,
                "PUT",
                {
                  expected_version: draft.version,
                  groups: draft.groups,
                  mappings: draft.mappings,
                },
                "模型与账户组已保存。",
              );
              onClose();
            } catch (e) {
              setError(e.message);
            } finally {
              submitting.current = false;
              setBusy(false);
            }
          }}
        >
          <h3>账户组</h3>
          {draft.groups.map((g, i) => (
            <fieldset className="route-editor-group" key={i}>
              <div className="row wrap">
                <label className="field">
                  组ID
                  <input
                    required
                    pattern="[A-Za-z0-9_-]+"
                    value={g.id}
                    onChange={(e) =>
                      update("groups", i, { id: e.target.value })
                    }
                  />
                </label>
                <label className="field">
                  名称
                  <input
                    required
                    value={g.name}
                    onChange={(e) =>
                      update("groups", i, { name: e.target.value })
                    }
                  />
                </label>
                <Button
                  onClick={() =>
                    setDraft({
                      ...draft,
                      groups: draft.groups.filter((_, j) => i !== j),
                    })
                  }
                >
                  删除组
                </Button>
              </div>
              <div className="row wrap topgap">
                <AccountPicker
                  platform={platform}
                  value={member}
                  onChange={(id, a) => {
                    setMemberNames((old) => ({ ...old, [id]: a }));
                    update("groups", i, {
                      account_ids: [...new Set([...g.account_ids, id])],
                    });
                    setMember("");
                  }}
                  label="添加组内账户"
                />
                <span>{g.account_ids.length} 个账户</span>
              </div>
              <details className="topgap">
                <summary>查看与移除组内账户</summary>
                <GroupMembers
                  ids={g.account_ids}
                  platform={platform}
                  groupId={
                    query.result?.groups?.some((saved) => saved.id === g.id)
                      ? g.id
                      : null
                  }
                  savedIds={
                    query.result?.groups?.find((saved) => saved.id === g.id)
                      ?.account_ids || []
                  }
                  known={memberNames}
                  onRemove={(id) =>
                    update("groups", i, {
                      account_ids: g.account_ids.filter((x) => x !== id),
                    })
                  }
                />
              </details>
            </fieldset>
          ))}
          <Button
            onClick={() =>
              setDraft({
                ...draft,
                groups: [
                  ...draft.groups,
                  {
                    id: `group_${Date.now()}`,
                    name: "新账户组",
                    account_ids: [],
                  },
                ],
              })
            }
          >
            添加账户组
          </Button>
          <h3 className="topgap">模型映射</h3>
          {draft.mappings.map((m, i) => (
            <div className="route-editor-group" key={i}>
              <div className="grid">
                <label className="field">
                  调用模型名
                  <input
                    required
                    value={m.model}
                    onChange={(e) =>
                      update("mappings", i, { model: e.target.value })
                    }
                  />
                </label>
                <label className="field">
                  上游模型名
                  <input
                    required
                    value={m.upstream_model}
                    onChange={(e) =>
                      update("mappings", i, { upstream_model: e.target.value })
                    }
                  />
                </label>
                <label className="field">
                  账户范围
                  <select
                    value={m.group_id || ""}
                    onChange={(e) =>
                      update("mappings", i, {
                        group_id: e.target.value || null,
                      })
                    }
                  >
                    <option value="">该平台公共账户池</option>
                    {draft.groups.map((g) => (
                      <option key={g.id} value={g.id}>
                        {g.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="row wrap">
                <Switch
                  label="启用此映射"
                  checked={m.enabled}
                  onChange={(e) =>
                    update("mappings", i, { enabled: e.target.checked })
                  }
                />
                <Button
                  onClick={() =>
                    setDraft({
                      ...draft,
                      mappings: draft.mappings.filter((_, j) => j !== i),
                    })
                  }
                >
                  删除映射
                </Button>
              </div>
            </div>
          ))}
          <div className="row wrap topgap">
            <Button
              onClick={() =>
                setDraft({
                  ...draft,
                  mappings: [
                    ...draft.mappings,
                    {
                      model: "",
                      upstream_model: "",
                      group_id: null,
                      enabled: true,
                    },
                  ],
                })
              }
            >
              添加映射
            </Button>
            <Button type="submit" variant="primary" disabled={busy}>
              {busy ? "正在保存…" : "保存模型路由"}
            </Button>
          </div>
        </form>
      ) : query.error ? (
        <div className="noresults">
          <Button onClick={query.reload}>重新读取路由</Button>
        </div>
      ) : (
        <div className="noresults">正在读取路由…</div>
      )}
    </Modal>
  );
}
export default function Routing() {
  const { data, mutate, setDrawer, page: currentPage } = useApp();
  const modelsOnly = currentPage === "models";
  const [platform, setPlatform] = useState(data.platforms[0]),
    [q, setQ] = useState(""),
    [page, setPage] = useState(1),
    [source, setSource] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [edit, setEdit] = useState(false),
    [sync, setSync] = useState(false),
    [policyOpen, setPolicyOpen] = useState(false),
    [policyDraft, setPolicyDraft] = useState(null),
    [modelDetail, setModelDetail] = useState(null);
  const submitting = useRef(false);
  const search = useDebounced(q),
    query = useQuery(
      modelsOnly
        ? `/api/model-directory?${queryString({ platform, q: search, page, page_size: 25 })}`
        : null,
      data.revision,
    ),
    routes = useQuery(
      `/api/model-routes/${encodeURIComponent(platform)}`,
      data.revision,
    ),
    policies = useQuery("/api/dispatch-policies", data.revision);
  useEffect(() => {
    setPage(1);
    setSource("");
  }, [platform, search]);
  const policy = policies.result?.policies?.find(
    (p) => p.platform === platform,
  );
  return (
    <div className="ops-page ops-routing">
      <Head
        name={modelsOnly ? "模型管理" : "路由配置"}
        desc={
          modelsOnly
            ? "按平台查询模型目录与能力，同步官方目录。"
            : "配置平台模型映射、账户组、选择顺序与会话亲和。"
        }
      >
        {modelsOnly ? (
          <Button
            onClick={() => {
              setSource("");
              setError("");
              setSync(true);
            }}
          >
            同步模型
          </Button>
        ) : (
          <>
            <Button
              onClick={() => {
                setPolicyDraft(policy ? { ...policy } : null);
                setPolicyOpen(true);
              }}
            >
              选号策略
            </Button>
            <Button variant="primary" onClick={() => setEdit(true)}>
              模型映射与分组
            </Button>
          </>
        )}
      </Head>
      <div className="platformtabs">
        {data.platforms.map((p) => (
          <button
            key={p}
            className={platform === p ? "active" : ""}
            onClick={() => setPlatform(p)}
          >
            {p}
          </button>
        ))}
      </div>
      {(error || query.error || routes.error || policies.error) && (
        <Notice>
          {error || query.error || routes.error || policies.error}
        </Notice>
      )}
      {modelsOnly && (
        <div className="toolbar">
          <input
            type="search"
            aria-label="搜索模型"
            placeholder="模型ID / 显示名称"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <span className="muted">
            选择顺序：{strategies[policy?.strategy] || "正在读取"}；映射{" "}
            {routes.result?.mappings?.length ?? "—"} 条 / 账户组{" "}
            {routes.result?.groups?.length ?? "—"} 个
          </span>
        </div>
      )}
      {modelsOnly && (
        <Panel name="模型目录">
          <Table
            loading={query.loading}
            empty={
              query.error
                ? "暂未读取到模型目录"
                : search
                  ? "没有匹配的模型"
                  : "尚未同步模型。点击「同步模型」读取当前平台的官方目录。"
            }
            heads={["模型", "上下文 / 最大输出", "能力", "同步时间", "操作"]}
            rows={(query.result?.items || []).map((m) => {
              const size = (v) =>
                Number.isFinite(v) ? v.toLocaleString("zh-CN") : "未提供";
              return [
                <>
                  {m.id}
                  <small>{m.display_name}</small>
                </>,
                `${size(m.context_window)} / ${size(m.max_output_tokens)}`,
                [
                  ["tools", "工具"],
                  ["vision", "视觉"],
                  ["reasoning", "推理"],
                ]
                  .map(
                    ([k, n]) =>
                      `${n}：${m.capabilities?.[k] === true ? "支持" : m.capabilities?.[k] === false ? "不支持" : "未提供"}`,
                  )
                  .join("；"),
                tm(m.observed_at),
                <div className="row wrap">
                  <Button onClick={() => setModelDetail(m)}>能力详情</Button>
                  <Button
                    onClick={() =>
                      setDrawer({ type: "key", platform, model: m.id })
                    }
                  >
                    创建模型密钥
                  </Button>
                </div>,
              ];
            })}
          />
          {query.result?.total > 0 && (
            <Pagination
              page={page}
              pageSize={25}
              total={query.result.total}
              onPage={setPage}
            />
          )}
        </Panel>
      )}
      {!modelsOnly && (
        <Panel name="平台选号策略">
          <div className="routing-summary">
            <p>选择顺序：{strategies[policy?.strategy] || "正在读取"}</p>
            <p>
              并发等待 {policy?.queue_wait_seconds ?? "—"} 秒 · 会话亲和{" "}
              {policy?.affinity_ttl_seconds ?? "—"} 秒
            </p>
            <p>
              账户组 {routes.result?.groups?.length ?? "—"}{" "}
              个；仅使用所属平台范围内账户。
            </p>
          </div>
        </Panel>
      )}
      <Panel
        name={modelsOnly ? "模型别名" : "当前模型映射"}
        action={
          modelsOnly ? (
            <a className="btn" href="#routing">
              管理模型映射
            </a>
          ) : undefined
        }
      >
        <Table
          loading={routes.loading}
          empty="尚未设置模型映射，调用时使用平台原始模型名。"
          heads={["调用模型", "上游模型", "账户范围", "状态"]}
          rows={(routes.result?.mappings || []).map((m) => [
            m.model,
            m.upstream_model,
            m.group_id
              ? routes.result?.groups?.find((g) => g.id === m.group_id)?.name ||
                m.group_id
              : "本平台公共池",
            m.enabled ? "启用" : "停用",
          ])}
        />
      </Panel>
      <Modal
        open={!!modelDetail}
        onOpenChange={(v) => !v && setModelDetail(null)}
        title={modelDetail ? `${modelDetail.id} · 模型能力` : "模型能力"}
        description="仅展示当前通道上游实际声明；未知项不推断为支持。"
      >
        {modelDetail && (
          <>
            <Table
              heads={["能力", "声明", "来源"]}
              rows={[
                ["tools", "工具"],
                ["vision", "视觉"],
                ["multimodal", "多模态"],
                ["reasoning", "推理"],
              ].map(([k, n]) => [
                n,
                modelDetail.capabilities?.[k] === true
                  ? "支持"
                  : modelDetail.capabilities?.[k] === false
                    ? "不支持"
                    : "未提供",
                modelDetail.capability_sources?.[k] === "upstream"
                  ? "上游声明"
                  : "未记录",
              ])}
            />
            <h3 className="topgap">推理档位</h3>
            <Table
              heads={["字段", "声明"]}
              rows={[
                [
                  "可用档位",
                  modelDetail.reasoning?.supported_efforts?.join(" / ") ||
                    "未提供",
                ],
                ["默认档位", modelDetail.reasoning?.default_effort || "未提供"],
                ["固定档位", modelDetail.reasoning?.fixed_effort || "未提供"],
                [
                  "允许绑定档位",
                  modelDetail.reasoning?.binding_supported === true
                    ? "支持"
                    : modelDetail.reasoning?.binding_supported === false
                      ? "不可绑定"
                      : "未提供",
                ],
                [
                  "允许关闭思考",
                  modelDetail.reasoning?.can_disable_thinking === true
                    ? "支持"
                    : modelDetail.reasoning?.can_disable_thinking === false
                      ? "不支持"
                      : "未提供",
                ],
                [
                  "推理信息来源",
                  modelDetail.reasoning_source === "upstream"
                    ? "上游声明"
                    : "未记录",
                ],
              ]}
            />
            <p className="quietnote">
              目录能力不等同于账户已有权限，实际调用结果仍单独记录。
            </p>
          </>
        )}
      </Modal>
      <Modal
        open={sync}
        busy={busy}
        onOpenChange={(open) => {
          if (!submitting.current) setSync(open);
        }}
        title={`${platform} · 同步模型目录`}
        size="small"
        description="选择一个凭证有效的账户读取官方目录。"
      >
        <AccountPicker
          inline
          disabled={busy}
          platform={platform}
          capability="models"
          value={source}
          onChange={setSource}
          label="选择读取目录的账户"
          required
        />
        {error && <Notice>{error}</Notice>}
        <div className="ops-dialog-actions">
          <Button
            variant="primary"
            disabled={
              busy ||
              !source ||
              !data.adapter_catalog?.[platform]?.network_enabled
            }
            onClick={async () => {
              if (submitting.current) return;
              submitting.current = true;
              setBusy(true);
              setError("");
              try {
                await mutate(
                  `/api/accounts/${source}/models/sync`,
                  "POST",
                  {},
                  "模型目录已同步。",
                );
                query.reload();
                setSync(false);
              } catch (e) {
                setError(e.message);
              } finally {
                submitting.current = false;
                setBusy(false);
              }
            }}
          >
            {busy ? "同步中…" : "同步官方模型"}
          </Button>
        </div>
      </Modal>
      <Modal
        open={policyOpen}
        busy={busy}
        onOpenChange={(open) => {
          if (!submitting.current) setPolicyOpen(open);
        }}
        title={`${platform} · 选号策略`}
        description="会话亲和优先；只影响本平台内新选择，不修改账户并发"
      >
        {policyDraft ? (
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              if (submitting.current) return;
              submitting.current = true;
              setBusy(true);
              setError("");
              try {
                await mutate(
                  `/api/dispatch-policies/${encodeURIComponent(platform)}`,
                  "PUT",
                  {
                    expected_version: policyDraft.version,
                    strategy: policyDraft.strategy,
                    queue_wait_seconds: policyDraft.queue_wait_seconds,
                    affinity_ttl_seconds: policyDraft.affinity_ttl_seconds,
                  },
                  "选号策略已保存。",
                );
                setPolicyOpen(false);
              } catch (e) {
                setError(e.message);
              } finally {
                submitting.current = false;
                setBusy(false);
              }
            }}
          >
            <label className="field gap">
              选择顺序
              <select
                value={policyDraft.strategy}
                onChange={(e) =>
                  setPolicyDraft({ ...policyDraft, strategy: e.target.value })
                }
              >
                {Object.entries(strategies).map(([v, n]) => (
                  <option key={v} value={v}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
            <label className="field gap">
              并发等待（秒）
              <input
                type="number"
                min="0"
                max="10"
                required
                value={policyDraft.queue_wait_seconds}
                onChange={(e) =>
                  setPolicyDraft({
                    ...policyDraft,
                    queue_wait_seconds: Number(e.target.value),
                  })
                }
              />
            </label>
            <label className="field gap">
              会话亲和（秒）
              <input
                type="number"
                min="0"
                max="86400"
                required
                value={policyDraft.affinity_ttl_seconds}
                onChange={(e) =>
                  setPolicyDraft({
                    ...policyDraft,
                    affinity_ttl_seconds: Number(e.target.value),
                  })
                }
              />
            </label>
            {error && <Notice>{error}</Notice>}
            <Button type="submit" variant="primary" disabled={busy}>
              保存策略
            </Button>
          </form>
        ) : (
          <Notice>策略尚未读取，请关闭后重试。</Notice>
        )}
      </Modal>
      <RouteEditor
        platform={platform}
        open={edit}
        onClose={() => setEdit(false)}
      />
    </div>
  );
}

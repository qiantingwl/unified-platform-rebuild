import React, { useEffect, useState } from "react";
import { Plus, Search } from "lucide-react";
import { useApp, Badge, Button, Head, Table, Switch, Notice, tm } from "../ui";
import { queryString, useDebounced, useQuery } from "../query";
import { Modal, Pagination } from "../components/Management";
export default function Keys() {
  const { data, setDrawer, mutate } = useApp();
  const [q, setQ] = useState(""),
    [platform, setPlatform] = useState(""),
    [status, setStatus] = useState("all"),
    [page, setPage] = useState(1),
    [pageSize, setPageSize] = useState(25),
    [pending, setPending] = useState(null),
    [error, setError] = useState(""),
    [revoke, setRevoke] = useState(null);
  const search = useDebounced(q);
  useEffect(() => setPage(1), [search, platform, status, pageSize]);
  const query = useQuery(
    `/api/keys?${queryString({ q: search, platform, status, page, page_size: pageSize })}`,
    data.revision,
  );
  async function update(k, method, body, message) {
    setPending(k.id);
    setError("");
    try {
      await mutate(`/api/keys/${k.id}`, method, body, message);
      query.reload();
      setRevoke(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setPending(null);
    }
  }
  return (
    <>
      <Head
        name="API 密钥"
        desc="用于模型调用的独立密钥；完整内容仅在创建时展示一次。"
      >
        <Button
          icon={Plus}
          variant="primary"
          onClick={() => setDrawer({ type: "key" })}
        >
          创建密钥
        </Button>
      </Head>
      <div className="toolbar">
        <div className="searchbox">
          <Search size={15} />
          <input
            aria-label="搜索密钥"
            placeholder="密钥名称 / 前缀"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <select
          aria-label="密钥平台"
          value={platform}
          onChange={(e) => setPlatform(e.target.value)}
        >
          <option value="">全部平台</option>
          {data.platforms.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
        <div className="segmented" aria-label="密钥状态">
          {[
            ["all", "全部"],
            ["enabled", "启用"],
            ["disabled", "停用"],
            ["revoked", "撤销"],
          ].map(([v, n]) => (
            <button
              key={v}
              className={status === v ? "active" : ""}
              aria-pressed={status === v}
              onClick={() => setStatus(v)}
            >
              {n}
            </button>
          ))}
        </div>
      </div>
      {(error || query.error) && (
        <Notice>
          {error || query.error}
          <Button onClick={query.reload}>重新查询</Button>
        </Notice>
      )}
      <div className="panel">
        <Table
          heads={[
            "名称 / 前缀",
            "平台",
            "允许模型",
            "配额 / 到期",
            "状态",
            "操作",
          ]}
          rows={(query.result?.items || []).map((k) => {
            const revoked = !!(k.revoked || k.revoked_at);
            return [
              <>
                {k.name}
                <small>{k.prefix}… · 仅保存摘要</small>
              </>,
              k.platform,
              (k.policy?.models || [k.model]).join("、"),
              <>
                {k.policy?.max_concurrency
                  ? `并发 ${k.policy.max_concurrency}`
                  : "并发不另设限"}
                <small>
                  {k.policy?.rpm ? `${k.policy.rpm} RPM` : "RPM 不另设限"} ·{" "}
                  {k.policy?.daily_requests
                    ? `每日 ${k.policy.daily_requests} 次`
                    : "每日不另设限"}
                </small>
                <small>
                  {k.policy?.expires_at ? tm(k.policy.expires_at) : "永不过期"}
                </small>
              </>,
              revoked ? (
                <Badge>已撤销</Badge>
              ) : (
                <Switch
                  label={`${k.name}密钥启用`}
                  checked={k.enabled}
                  disabled={pending === k.id}
                  onChange={(e) =>
                    update(
                      k,
                      "PATCH",
                      { enabled: e.target.checked },
                      e.target.checked
                        ? "密钥已启用。"
                        : "密钥已停用，可随时启用。",
                    )
                  }
                />
              ),
              <div className="row wrap">
                <Button
                  disabled={revoked}
                  onClick={() => setDrawer({ type: "key-policy", id: k.id })}
                >
                  权限与配额
                </Button>
                <Button
                  disabled={revoked || pending === k.id}
                  onClick={() => setRevoke(k)}
                >
                  撤销
                </Button>
              </div>,
            ];
          })}
        />
        <Pagination
          page={page}
          pageSize={pageSize}
          total={query.result?.total}
          loading={query.loading}
          onPage={setPage}
          onPageSize={setPageSize}
        />
      </div>
      <p className="quietnote topgap">
        旧密钥只有摘要，无法恢复或复制原文。需要替换时创建新密钥，更新调用端后撤销旧密钥。
      </p>
      <Modal
        open={!!revoke}
        onOpenChange={(open) => !open && setRevoke(null)}
        title="撤销密钥"
        description="撤销后无法恢复；临时停用请使用列表开关。"
      >
        <p>撤销「{revoke?.name}」后，使用它的模型调用将被拒绝。</p>
        {error && <Notice>{error}</Notice>}
        <div className="row topgap">
          <Button disabled={!!pending} onClick={() => setRevoke(null)}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={!!pending}
            onClick={() => update(revoke, "DELETE", undefined, "密钥已撤销。")}
          >
            {pending ? "正在撤销…" : "确认撤销"}
          </Button>
        </div>
      </Modal>
    </>
  );
}

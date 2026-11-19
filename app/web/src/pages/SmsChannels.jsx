import React, { useRef, useState } from "react";
import { useRecords } from "../use-records";
import ProxyPicker from "./ProxyPicker";
import SmsChannelEditor from "./SmsChannelEditor";
import { useApp, Panel, Button, Table, Notice, tm, nf } from "../ui";

import { Modal, Pagination } from "../components/Management";
export default function SmsChannels({ platform }) {
  const { mutate } = useApp();
  const submitLock = useRef(false);
  const [batchPage, setBatchPage] = useState(1),
    [batchStatus, setBatchStatus] = useState("");
  const [channelsOpen, setChannelsOpen] = useState(false);
  const [editing, setEditing] = useState(null),
    [configOpen, setConfigOpen] = useState(false),
    [batchOpen, setBatchOpen] = useState(false),
    [loginMode, setLoginMode] = useState("token");
  const [proxy, setProxy] = useState("");
  const [data, error] = useRecords("/api/sms/channels"),
    [batch] = useRecords(
      `/api/registration-batches?page=${batchPage}&page_size=25&platform=${encodeURIComponent(platform)}&status=${encodeURIComponent(batchStatus)}`,
    );
  const [provider, setProvider] = useState("haozhuma"),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false);
  async function submit(e, path, body) {
    e.preventDefault();
    if (submitLock.current) return;
    submitLock.current = true;
    setBusy(true);
    setMessage("");
    const form = e.currentTarget;
    try {
      await mutate(path, "POST", body, "配置已保存。");
      form.reset();
      setConfigOpen(false);
      setBatchOpen(false);
    } catch (e) {
      setMessage(e.message);
    } finally {
      submitLock.current = false;
      setBusy(false);
    }
  }
  return (
    <Panel name={`${platform} · 注册批次`}>
      <div className="pad">
        {(error || message) && <Notice>{error || message}</Notice>}
        <div className="row wrap">
          <Button onClick={() => setChannelsOpen(true)}>
            管理共享接码渠道
          </Button>
          <Button onClick={() => setConfigOpen(true)}>添加接码渠道</Button>
        </div>
        <Modal
          open={configOpen}
          busy={busy}
          onOpenChange={setConfigOpen}
          title="添加接码渠道"
          description="保存配置与登录均不购号、不发短信"
        >
          {message && <Notice>{message}</Notice>}
          <form
            className="topgap"
            onSubmit={(e) => {
              const f = new FormData(e.currentTarget);
              submit(
                e,
                provider === "haozhuma" && loginMode === "password"
                  ? "/api/sms/channels/credential-login"
                  : "/api/sms/channels",
                {
                  ...(provider === "haozhuma" && loginMode === "password"
                    ? { user: f.get("user"), password: f.get("password") }
                    : {}),
                  name: f.get("name"),
                  provider,
                  base_url: f.get("base"),
                  tokens: String(f.get("tokens") || "")
                    .split(/\r?\n/)
                    .map((x) => x.trim())
                    .filter(Boolean),
                  projects: String(f.get("projects") || "")
                    .split(/[,，\s]+/)
                    .filter(Boolean),
                  project: f.get("project") || "",
                  keyword: f.get("keyword") || "",
                  keyword_get: f.get("keyword_get") || "",
                  keyword_msg: f.get("keyword_msg") || "",
                  isp: f.get("isp") || "",
                  card_type: "全部",
                },
              );
            }}
          >
            <div className="grid">
              <div className="field gap">
                <label htmlFor="channel-name">名称</label>
                <input id="channel-name" name="name" required />
              </div>
              <div className="field gap">
                <label htmlFor="channel-provider">平台</label>
                <select
                  id="channel-provider"
                  value={provider}
                  onChange={(e) => {
                    setProvider(e.target.value);
                    setLoginMode("token");
                  }}
                >
                  <option value="haozhuma">豪猪</option>
                  <option value="eomsg">eomsg</option>
                </select>
              </div>
              <div className="field gap">
                <label htmlFor="channel-base">API 地址</label>
                <input
                  key={provider}
                  id="channel-base"
                  name="base"
                  defaultValue={
                    provider === "haozhuma"
                      ? "https://api.haozhuma.com/sms/"
                      : "https://api.eomsg.com/zc/data.php"
                  }
                  required
                />
              </div>
              <div className="field gap">
                <label>认证方式</label>
                <div className="segmented">
                  <button
                    type="button"
                    className={loginMode === "token" ? "active" : ""}
                    onClick={() => setLoginMode("token")}
                  >
                    Token
                  </button>
                  {provider === "haozhuma" && (
                    <button
                      type="button"
                      className={loginMode === "password" ? "active" : ""}
                      onClick={() => setLoginMode("password")}
                    >
                      账号密码换取 Token
                    </button>
                  )}
                </div>
              </div>
              {loginMode === "token" ? (
                <label className="field gap">
                  Token 池（每行一个）
                  <textarea
                    className="secretfield"
                    name="tokens"
                    autoComplete="off"
                    required
                  />
                </label>
              ) : (
                <>
                  <label className="field gap">
                    供应商账号
                    <input name="user" required autoComplete="off" />
                  </label>
                  <label className="field gap">
                    供应商密码
                    <input
                      type="password"
                      name="password"
                      required
                      autoComplete="new-password"
                    />
                  </label>
                  <p className="quietnote">
                    登录成功后保存换取的 Token，不回显 Token。
                  </p>
                </>
              )}
              <div className="field gap">
                <label htmlFor="channel-project">项目 SID（豪猪）</label>
                <input
                  id="channel-project"
                  name="project"
                  required={provider === "haozhuma"}
                />
              </div>
              <div className="field gap">
                <label htmlFor="channel-projects">
                  附加项目 SID（逗号分隔）
                </label>
                <input id="channel-projects" name="projects" />
              </div>
              <div className="field gap">
                <label htmlFor="channel-keyword">短信关键字（eomsg）</label>
                <input
                  id="channel-keyword"
                  name="keyword"
                  required={provider === "eomsg"}
                />
              </div>
              <label className="field gap">
                取号关键字（可选）
                <input name="keyword_get" />
                <small>留空使用上方通用关键字。</small>
              </label>
              <label className="field gap">
                收码关键字（可选）
                <input name="keyword_msg" />
                <small>留空使用上方通用关键字。</small>
              </label>
            </div>
            <Button type="submit" disabled={busy}>
              保存接码配置
            </Button>
            <p className="quietnote">
              保存配置不申请号码、不发送短信。材料加密保存。
            </p>
          </form>
        </Modal>
        <Modal
          open={channelsOpen}
          onOpenChange={setChannelsOpen}
          title="共享接码渠道"
          size="large"
          description="渠道是跨平台共享的供应商钱包，余额不按项目累加。"
        >
          <Table
            pageSize={25}
            loading={!data && !error}
            heads={[
              "渠道",
              "供应商",
              "令牌 / 项目",
              "渠道余额",
              "记录",
              "操作",
            ]}
            rows={(data?.channels || []).map((c) => [
              c.name,
              `${c.data.provider} · ${c.data.enabled === false ? "停止新任务" : "开启"}`,
              `${c.data.token_count ?? (c.has_secret ? 1 : 0)} 个令牌 / ${new Set([c.data.project, ...(c.data.projects || []), ...(c.data.paired_projects || [])].filter(Boolean)).size} 个项目${c.data.pair_count ? ` · ${c.data.pair_count} 组明确配对` : ""}`,
              (() => {
                const b = data?.balances?.find((b) => b.name === c.id)?.data;
                return b ? (
                  <>
                    {b.status === "verified" ? (
                      <strong>{nf(b.balance)}</strong>
                    ) : b.status === "partial" ? (
                      <>部分查询成功：{nf(b.verified_subtotal)}</>
                    ) : b.status === "stale" ? (
                      "渠道已更新，待查询"
                    ) : b.status === "failed" ? (
                      "查询失败"
                    ) : (
                      "旧单令牌结果，需重新查询"
                    )}
                    <small>
                      {b.verified_tokens ?? "—"} / {b.token_count ?? "—"}{" "}
                      个令牌已核验 · {tm(b.checked_at)}
                    </small>
                    {b.tokens?.length > 0 && (
                      <details>
                        <summary>令牌余额明细</summary>
                        {b.tokens.map((t) => (
                          <div key={t.index}>
                            令牌 {t.index}：
                            {t.status === "verified"
                              ? nf(t.balance)
                              : "查询失败"}
                            {t.error ? `（${t.error}）` : ""}
                          </div>
                        ))}
                      </details>
                    )}
                  </>
                ) : (
                  "尚未查询"
                );
              })(),
              tm(c.updated_at),
              <div className="row wrap">
                <Button onClick={() => setEditing(c)}>编辑</Button>
                <Button
                  onClick={() =>
                    mutate(
                      `/api/sms/channels/${c.id}/balance`,
                      "POST",
                      {},
                      "渠道余额已回读。",
                    ).catch((e) => setMessage(e.message))
                  }
                >
                  查询渠道余额
                </Button>
              </div>,
            ])}
          />
        </Modal>
        <Modal
          open={!!editing}
          onOpenChange={(v) => !v && setEditing(null)}
          title="编辑接码渠道"
          description="令牌和密码不会回显"
        >
          {editing && (
            <SmsChannelEditor
              key={editing.id + ":" + editing.version}
              channel={editing}
              onClose={() => setEditing(null)}
            />
          )}
        </Modal>
        {["Loomy", "WorkBuddy 国内", "WPS 灵犀", "小浣熊", "CodeArts"].includes(
          platform,
        ) && (
          <>
            <Button onClick={() => setBatchOpen(true)}>
              创建 {platform} 注册批次
            </Button>
            <Modal
              open={batchOpen}
              busy={busy}
              onOpenChange={setBatchOpen}
              title={`${platform} · 注册批次`}
              description="创建后会按已选渠道执行真实注册，可能产生接码费用"
            >
              {message && <Notice>{message}</Notice>}
              <form
                className="topgap"
                onSubmit={(e) => {
                  const f = new FormData(e.currentTarget);
                  submit(e, "/api/registration-batches", {
                    name: f.get("name"),
                    platform,
                    count: Number(f.get("count")),
                    sms_channel_id: f.get("channel"),
                    proxy_id: f.get("proxy") || null,
                    number_budget: f.get("number_budget")
                      ? Number(f.get("number_budget"))
                      : null,
                    max_consecutive_failures: f.get("max_consecutive_failures")
                      ? Number(f.get("max_consecutive_failures"))
                      : null,
                    max_consecutive_skips: f.get("max_consecutive_skips")
                      ? Number(f.get("max_consecutive_skips"))
                      : null,
                    parallel: Number(f.get("parallel")),
                  });
                }}
              >
                <div className="grid">
                  <div className="field gap">
                    <label htmlFor="batch-name">批次名称</label>
                    <input id="batch-name" name="name" required />
                  </div>
                  <div className="field gap">
                    <label htmlFor="batch-count">目标成功数</label>
                    <input
                      id="batch-count"
                      name="count"
                      type="number"
                      min={1}
                      max={10000}
                      defaultValue={1}
                      required
                    />
                  </div>
                  <div className="field gap">
                    <label htmlFor="batch-channel">接码渠道</label>
                    <select id="batch-channel" name="channel" required>
                      <option value="">请选择</option>
                      {(data?.channels || [])
                        .filter((c) => c.data.enabled !== false)
                        .map((c) => (
                          <option value={c.id} key={c.id}>
                            {c.name}
                          </option>
                        ))}
                    </select>
                  </div>
                  <div className="field gap">
                    <label>注册出口</label>
                    <input type="hidden" name="proxy" value={proxy} />
                    <Button onClick={() => setProxy("")}>
                      使用平台注册出口{!proxy ? "（已选）" : ""}
                    </Button>
                    <ProxyPicker value={proxy} onChange={setProxy} />
                  </div>
                </div>
                <div className="grid">
                  <label className="field gap">
                    注册执行并发
                    <input
                      name="parallel"
                      type="number"
                      min={1}
                      max={64}
                      defaultValue={4}
                      required
                    />
                  </label>
                  <label className="field gap">
                    注册尝试次数上限（可选）
                    <input
                      name="number_budget"
                      type="number"
                      min={1}
                      max={200000}
                      placeholder="留空不设限"
                    />
                  </label>
                  <label className="field gap">
                    连续失败暂停阈值（可选）
                    <input
                      name="max_consecutive_failures"
                      type="number"
                      min={1}
                      max={1000}
                      placeholder="留空不设限"
                    />
                  </label>
                  <label className="field gap">
                    连续已用号跳过暂停阈值（可选）
                    <input
                      name="max_consecutive_skips"
                      type="number"
                      min={1}
                      max={10000}
                      placeholder="留空不设限"
                    />
                  </label>
                </div>
                <Notice>
                  创建并执行批次会申请号码并产生接码费用。等待短信不占执行槽；遇到人机验证停留在该账户的交互步骤。
                </Notice>
                <Button type="submit" variant="primary" disabled={busy}>
                  创建批次
                </Button>
              </form>
            </Modal>
          </>
        )}
        <div className="toolbar">
          <select
            aria-label="注册批次状态"
            value={batchStatus}
            onChange={(e) => {
              setBatchStatus(e.target.value);
              setBatchPage(1);
            }}
          >
            <option value="">全部批次</option>
            <option value="active">运行中</option>
            <option value="paused">暂停</option>
            <option value="completed">完成</option>
            <option value="cancelled">结束</option>
          </select>
        </div>
        <Table
          loading={!batch && !error}
          heads={[
            "批次",
            "平台",
            "成功 / 目标",
            "注册尝试 / 自设上限",
            "失败 · 已用号跳过 · 进行中",
            "状态",
            "操作",
          ]}
          rows={(batch?.batches || []).map((b) => [
            b.name,
            b.data.platform,
            `${b.counts.succeeded || 0} / ${b.data.target}`,
            `${b.progress?.attempts ?? (b.data.sessions || []).length} / ${b.data.number_budget ?? "不设限"}`,
            `${(b.counts.failed || 0) - (b.skipped || 0)} · ${b.skipped || 0} · ${b.progress?.inflight ?? 0}`,
            {
              active: "运行中",
              paused: "已暂停",
              completed: "已达成目标",
              cancelled: "已结束",
            }[b.data.status] +
              (b.data.pause_reason
                ? `（${{ consecutive_failures: "达到自设连续失败阈值", consecutive_used_numbers: "达到自设已用号阈值", number_budget_exhausted: "达到自设注册尝试上限" }[b.data.pause_reason] || b.data.pause_reason}）`
                : ""),
            <div className="row wrap">
              <Button
                disabled={["cancelled", "completed"].includes(b.data.status)}
                onClick={() =>
                  mutate(
                    `/api/registration-batches/${b.id}/${b.data.status === "active" ? "pause" : "resume"}`,
                    "POST",
                    {},
                    "批次状态已更新。",
                  ).catch((e) => setMessage(e.message))
                }
              >
                {["cancelled", "completed"].includes(b.data.status)
                  ? "已结束"
                  : b.data.status === "active"
                    ? "暂停新任务"
                    : "继续"}
              </Button>
              <Button
                disabled={b.data.status === "cancelled"}
                onClick={() =>
                  mutate(
                    `/api/registration-batches/${b.id}/cancel`,
                    "POST",
                    {},
                    "批次未开始任务已结束，正在处理的任务继续保存回执。",
                  ).catch((e) => setMessage(e.message))
                }
              >
                结束未开始任务
              </Button>
            </div>,
          ])}
        />
        <Pagination
          page={batchPage}
          pageSize={25}
          total={batch?.total}
          loading={!batch}
          onPage={setBatchPage}
        />
      </div>
    </Panel>
  );
}

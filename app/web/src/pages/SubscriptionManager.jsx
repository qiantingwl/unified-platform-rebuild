import React, { useEffect, useId, useState } from "react";
import { api, errors } from "../api";
import { Button, Notice, Table, tm, useApp } from "../ui";
import { Modal } from "../components/Management";
import { useQuery } from "../query";
import SubscriptionSchedule from "./SubscriptionSchedule";

const errorNames = {
  subscription_source_duplicate: "相同订阅链接或本地内容已存在，请编辑原来源。",
  subscription_source_conflict:
    "此来源类型不能直接转换，请使用原来源对应的编辑方式。",
  proxy_protocol_unsupported: "不支持此协议",
  proxy_options_unsupported: "含不支持的节点参数",
  invalid_node: "节点字段不完整",
  invalid_proxy_node: "节点字段无效",
  proxy_insecure_tls_rejected: "节点要求跳过TLS验证，未导入",
  subscription_empty: "没有解析到支持的节点，请检查订阅内容。",
  invalid_subscription: "内容不是有效的节点订阅。",
  subscription_fetch_failed: "获取订阅失败，请检查来源服务和网络。",
  subscription_invalid_response: "订阅响应编码错误或读取超时。",
  subscription_too_large: "订阅内容超过2MB。",
  subscription_sync_in_progress: "此订阅已排队或正在刷新。",
  subscription_yaml_alias_unsupported: "不接受含锚点或引用的YAML。",
  invalid_subscription_url: "请填写不含账号密码或片段的HTTP/HTTPS订阅地址。",
  subscription_url_missing: "本地内容没有远程地址，请编辑后重新导入。",
};
const phaseNames = {
  queued: "等待任务服务读取",
  fetching: "正在获取内容",
  parsing: "正在解析节点",
  saving: "正在合并节点",
  completed: "最近一轮已完成",
  failed: "最近一轮失败",
  stopped: "已停止来源",
  idle: "等待刷新",
};
const describe = (code) => errorNames[code] || errors[code] || code;
function fileSize(size) {
  return `${(size / 1024).toFixed(1)} KB`;
}

function SubscriptionEditor({ item, onClose, onSaved }) {
  const { mutate } = useApp(),
    formId = useId();
  const [name, setName] = useState(item?.name || ""),
    [mode, setMode] = useState(
      item?.has_secret
        ? "url"
        : item?.data.source === "file"
          ? "file"
          : item
            ? "paste"
            : "url",
    );
  const [url, setUrl] = useState(""),
    [content, setContent] = useState(""),
    [fileName, setFileName] = useState(item?.data.file_name || "");
  const [busy, setBusy] = useState(""),
    [error, setError] = useState(""),
    [preview, setPreview] = useState(null),
    [feedback, setFeedback] = useState("");
  const [auto, setAuto] = useState(item?.data.auto_update ?? true),
    [interval, setIntervalValue] = useState(item?.data.interval_minutes || 360);
  const editing = !!item,
    changedSource = mode === "url" ? !!url : !!content;
  const sourcePayload = () =>
    mode === "url"
      ? url
        ? { url }
        : { subscription_id: item?.id }
      : { content };
  function change(setter, value) {
    setter(value);
    setPreview(null);
    setError("");
    setFeedback("");
  }
  async function readFile(file) {
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
      setError("节点文件不能超过2MB。");
      return;
    }
    setBusy("reading");
    setError("");
    try {
      setContent(await file.text());
      setFileName(file.name);
      setPreview(null);
      setFeedback(
        `已读取 ${file.name}（${fileSize(file.size)}），请解析预览。`,
      );
    } catch {
      setError("文件读取失败，请重新选择。");
    } finally {
      setBusy("");
    }
  }
  async function decodeImage(file) {
    if (!file || busy) return;
    if (file.size > 6 * 1024 * 1024) {
      setError("二维码图片不能超过6MB。");
      return;
    }
    setBusy("decoding");
    setError("");
    setFeedback("正在识别图片中的二维码…");
    let bitmap;
    try {
      bitmap = await createImageBitmap(file);
      if (bitmap.width * bitmap.height > 16000000)
        throw new Error("图片尺寸过大，请裁剪到二维码区域。");
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(bitmap, 0, 0);
      const { default: jsQR } = await import("jsqr");
      const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height),
        decoded = jsQR(pixels.data, pixels.width, pixels.height);
      if (!decoded) throw new Error("未识别到二维码，请使用更清晰的图片。");
      let parsed;
      try {
        parsed = new URL(decoded.data);
      } catch {
        throw new Error("二维码内容不是有效的订阅链接。");
      }
      if (
        !["http:", "https:"].includes(parsed.protocol) ||
        parsed.username ||
        parsed.password ||
        parsed.hash
      )
        throw new Error(
          "二维码必须是HTTP/HTTPS订阅链接；单节点分享请使用粘贴内容导入。",
        );
      setUrl(decoded.data);
      setPreview(null);
      setFeedback("已识别订阅链接。尚未访问或保存，请解析预览后确认。");
    } catch (e) {
      setError(e.message || "图片无法读取。");
      setFeedback("");
    } finally {
      bitmap?.close();
      setBusy("");
    }
  }
  async function parse() {
    setBusy("preview");
    setError("");
    setFeedback("");
    try {
      const result = await api(
        "/api/proxy-subscriptions/preview",
        "POST",
        sourcePayload(),
      );
      setPreview(result);
    } catch (e) {
      setError(describe(e.code) || e.message);
    } finally {
      setBusy("");
    }
  }
  async function save(e) {
    e.preventDefault();
    if (busy) return;
    setBusy("saving");
    setError("");
    try {
      const body = {
        name,
        ...(editing ? { expected_version: item.version } : {}),
        ...(mode === "url"
          ? url
            ? { url }
            : {}
          : content
            ? {
                content,
                source_kind: mode,
                file_name: mode === "file" ? fileName : "",
              }
            : {}),
        ...(!editing
          ? {
              auto_update: mode === "url" && auto,
              interval_minutes: Number(interval),
            }
          : {}),
      };
      await mutate(
        editing
          ? `/api/proxy-subscriptions/${item.id}`
          : "/api/proxy-subscriptions",
        editing ? "PUT" : "POST",
        body,
        "订阅已保存。",
      );
      onSaved();
      onClose();
    } catch (e) {
      setError(describe(e.code) || e.message);
    } finally {
      setBusy("");
    }
  }
  const needsPreview = changedSource || !editing;
  const validSource = mode === "url" ? !!url || !!item?.has_secret : !!content;
  const footer = (
    <div className="proxy-dialog-actions">
      <span>
        {busy
          ? {
              reading: "正在读取文件…",
              decoding: "正在识别二维码…",
              preview: "正在解析预览…",
              saving: "正在保存…",
            }[busy]
          : "预览和识别不会保存或执行节点请求"}
      </span>
      <div className="row">
        <Button disabled={!!busy} onClick={onClose}>
          取消
        </Button>
        <Button
          type="submit"
          form={formId}
          variant="primary"
          disabled={!!busy || (needsPreview && (!preview || !preview.total))}
        >
          保存订阅
        </Button>
      </div>
    </div>
  );
  return (
    <Modal
      open
      onOpenChange={(open) => !open && !busy && onClose()}
      title={editing ? "编辑订阅" : "添加订阅"}
      description="先选择来源并解析预览，再确认保存；原位更新保留订阅身份及已有账户绑定。"
      footer={footer}
    >
      <form
        id={formId}
        onSubmit={save}
        className="proxy-binding-content"
        onPaste={(e) => {
          if (mode !== "url") return;
          const file = Array.from(e.clipboardData.files || []).find((f) =>
            f.type.startsWith("image/"),
          );
          if (file) {
            e.preventDefault();
            void decodeImage(file);
          }
        }}
      >
        <label className="field">
          订阅名称
          <input
            required
            maxLength={100}
            value={name}
            disabled={!!busy}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <fieldset disabled={!!busy} className="proxy-fieldset">
          <legend>订阅来源</legend>
          <div className="row wrap">
            {[
              ["url", "订阅链接"],
              ["paste", "粘贴内容"],
              ["file", "本地文件"],
            ]
              .filter(
                ([key]) =>
                  !editing || (item.has_secret ? key === "url" : key !== "url"),
              )
              .map(([key, label]) => (
                <label className="row gap" key={key}>
                  <input
                    type="radio"
                    name={`${formId}-source`}
                    checked={mode === key}
                    onChange={() => change(setMode, key)}
                  />
                  {label}
                </label>
              ))}
          </div>
        </fieldset>
        {mode === "url" ? (
          <>
            <label className="field">
              {editing ? "替换订阅链接（留空保留）" : "订阅链接"}
              <input
                type="password"
                autoComplete="new-password"
                value={url}
                required={!editing}
                disabled={!!busy}
                placeholder="https://…"
                onChange={(e) => change(setUrl, e.target.value)}
              />
            </label>
            <label className="field">
              从图片识别二维码
              <input
                type="file"
                accept="image/png,image/jpeg,image/webp"
                disabled={!!busy}
                onChange={(e) => {
                  void decodeImage(e.target.files?.[0]);
                  e.target.value = "";
                }}
              />
            </label>
            <small>
              支持粘贴二维码截图。识别只在浏览器内执行，不上传图片，不自动访问链接。
            </small>
          </>
        ) : (
          <>
            {mode === "file" ? (
              <label className="field">
                {editing ? "重新选择节点文件（不选择则保留）" : "节点文件"}
                <input
                  type="file"
                  accept=".yaml,.yml,.json,.txt,.conf"
                  disabled={!!busy}
                  onChange={(e) => {
                    void readFile(e.target.files?.[0]);
                    e.target.value = "";
                  }}
                />
                <small>{fileName || "未选择文件"}</small>
              </label>
            ) : (
              <label className="field">
                {editing ? "替换订阅内容（留空保留）" : "订阅内容"}
                <textarea
                  rows={5}
                  value={content}
                  required={!editing}
                  disabled={!!busy}
                  onChange={(e) => change(setContent, e.target.value)}
                />
              </label>
            )}
            <small>
              支持Clash
              YAML/JSON节点配置、分享链接列表及Base64内容。只导入受支持的节点，不执行来源中的规则、控制器或监听器。本地内容不会自动拉取，更新时在此重新导入。
            </small>
          </>
        )}
        {!editing && mode === "url" && (
          <div className="proxy-binding-section">
            <label className="row gap">
              <input
                type="checkbox"
                checked={auto}
                disabled={!!busy}
                onChange={(e) => setAuto(e.target.checked)}
              />
              自动刷新
            </label>
            <label className="field">
              刷新间隔（分钟）
              <input
                type="number"
                min="5"
                max="10080"
                required
                value={interval}
                disabled={!!busy}
                onChange={(e) => setIntervalValue(e.target.value)}
              />
            </label>
          </div>
        )}
        <div>
          <Button disabled={!!busy || !validSource} onClick={parse}>
            {busy === "preview" ? "正在解析…" : "解析预览"}
          </Button>
          {editing && !changedSource && (
            <small> 留空保存只更新名称，保留原内容。</small>
          )}
        </div>
        {feedback && (
          <div className="proxy-success" role="status">
            {feedback}
          </div>
        )}
        {error && (
          <div role="alert">
            <Notice>{error}</Notice>
          </div>
        )}
        {preview && (
          <section className="proxy-binding-section" aria-label="订阅解析预览">
            <strong>
              可导入 {preview.total} 个节点 · 拒绝 {preview.rejected} 条
            </strong>
            <div className="row wrap">
              {Object.entries(preview.protocols).map(([p, n]) => (
                <span key={p}>
                  {p} {n}
                </span>
              ))}
            </div>
            <Table
              heads={["节点名称", "协议"]}
              rows={preview.nodes.map((n) => [n.name, n.scheme])}
            />
            {preview.rejected > 0 && (
              <Notice>
                未支持或无效的条目不会导入；部分解析会保留原节点，避免错误退役。
                {preview.rejections.map((r, i) => (
                  <div key={i}>
                    第 {r.index + 1} 项：{describe(r.reason)}
                  </div>
                ))}
              </Notice>
            )}
            {preview.truncated && (
              <small>预览最多显示前100项，汇总按完整内容计算。</small>
            )}
          </section>
        )}
      </form>
    </Modal>
  );
}

export default function SubscriptionManager({ onClose }) {
  const { data, mutate } = useApp();
  const query = useQuery("/api/proxy-subscriptions", data.revision);
  const [editor, setEditor] = useState(null),
    [schedule, setSchedule] = useState(null),
    [deleting, setDeleting] = useState(null),
    [busy, setBusy] = useState(""),
    [error, setError] = useState("");
  const subscriptions = query.result?.subscriptions || [];
  const active = subscriptions.some(
    (s) => s.data.syncing || s.data.sync_requested,
  );
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(query.reload, 1500);
    return () => clearInterval(timer);
  }, [active]);
  async function run(id, path, method, body) {
    setBusy(id);
    setError("");
    try {
      await mutate(path, method, body, "订阅操作已提交。");
      query.reload();
      return true;
    } catch (e) {
      setError(describe(e.code) || e.message);
      return false;
    } finally {
      setBusy("");
    }
  }
  return (
    <>
      <Modal
        open
        onOpenChange={(open) => !open && onClose()}
        title="订阅管理"
        size="large"
        description="来源各自管理，共享节点去重保留，账户按稳定节点身份绑定。"
        footer={
          <div className="row wrap">
            <Button onClick={() => setEditor({ item: null })}>添加订阅</Button>
            <Button onClick={query.reload}>刷新列表</Button>
            <Button onClick={onClose}>完成</Button>
          </div>
        }
      >
        <div className="proxy-binding-content">
          {(error || query.error) && (
            <Notice>
              {error || query.error}
              <Button onClick={query.reload}>重试</Button>
            </Notice>
          )}
          {query.loading && !query.result ? (
            <p>正在读取订阅…</p>
          ) : !subscriptions.length ? (
            <div className="proxy-empty-state">
              <strong>还没有订阅</strong>
              <p>添加订阅链接、粘贴节点内容，或选择本地Clash节点配置文件。</p>
              <Button onClick={() => setEditor({ item: null })}>
                添加订阅
              </Button>
            </div>
          ) : (
            <Table
              pageSize={100}
              heads={[
                "订阅 / 来源",
                "节点与解析结果",
                "刷新状态",
                "时间",
                "操作",
              ]}
              rows={subscriptions.map((s) => {
                const p = s.data.sync_progress || {};
                const running = s.data.syncing || s.data.sync_requested;
                return [
                  <>
                    <strong>{subscriptionLabel(s)}</strong>
                    <small className="proxy-source">
                      {s.has_secret
                        ? `订阅链接 · ${s.data.source_host || "地址已加密保存"}`
                        : s.data.source === "file"
                          ? `本地文件 · ${s.data.file_name || "节点配置"}`
                          : "粘贴内容"}{" "}
                      · {s.data.enabled === false ? "来源已停用" : "来源启用"}
                    </small>
                  </>,
                  <>
                    {s.data.node_count ?? "—"} 个
                    <small className="proxy-source">
                      最近合并：新增 {s.data.added ?? "—"} / 更新{" "}
                      {s.data.updated ?? "—"} / 共享 {s.data.shared ?? "—"} /
                      退役 {s.data.retired ?? "—"} / 拒绝{" "}
                      {s.data.rejected ?? "—"}
                    </small>
                    {s.data.partial_retained && (
                      <small>部分解析，旧节点已保留</small>
                    )}
                  </>,
                  <>
                    <strong>
                      {phaseNames[p.phase] ||
                        (s.data.last_error
                          ? "最近一轮失败"
                          : s.data.last_sync_at
                            ? "最近一轮已完成"
                            : "尚未同步")}
                    </strong>
                    {p.received_bytes != null && (
                      <small className="proxy-source">
                        已读取 {fileSize(p.received_bytes)}
                        {p.total_bytes ? ` / ${fileSize(p.total_bytes)}` : ""}
                      </small>
                    )}
                    {s.data.last_error && (
                      <small className="proxy-source bad">
                        {describe(s.data.last_error)}
                      </small>
                    )}
                    {s.data.last_failure_code && !s.data.last_error && (
                      <small className="proxy-source">
                        上次失败：{describe(s.data.last_failure_code)}
                      </small>
                    )}
                  </>,
                  <>
                    最近尝试：{tm(s.data.last_attempt_at)}
                    <br />
                    最近成功：{tm(s.data.last_sync_at)}
                    <br />
                    最近失败：{tm(s.data.last_failure_at)}
                    <br />
                    下次更新：
                    {s.data.auto_update ? tm(s.data.next_sync_at) : "手动更新"}
                  </>,
                  <div className="row wrap">
                    <Button
                      disabled={
                        !!busy ||
                        !s.has_secret ||
                        s.data.enabled === false ||
                        running
                      }
                      onClick={() =>
                        run(
                          s.id,
                          `/api/proxy-subscriptions/${s.id}/sync-start`,
                          "POST",
                          {},
                        )
                      }
                    >
                      {running ? "刷新进行中…" : "立即刷新"}
                    </Button>
                    <Button
                      disabled={!!busy}
                      onClick={() => setEditor({ item: s })}
                    >
                      编辑
                    </Button>
                    <Button disabled={!!busy} onClick={() => setSchedule(s)}>
                      定时与检测
                    </Button>
                    <Button
                      disabled={!!busy}
                      onClick={() =>
                        run(s.id, `/api/proxy-subscriptions/${s.id}`, "PUT", {
                          expected_version: s.version,
                          name: s.name,
                          enabled: s.data.enabled === false,
                        })
                      }
                    >
                      {s.data.enabled === false ? "启用来源" : "停用来源"}
                    </Button>
                    <Button disabled={!!busy} onClick={() => setDeleting(s)}>
                      删除
                    </Button>
                  </div>,
                ];
              })}
            />
          )}
          <p className="quietnote">
            立即刷新会交给任务服务持续执行；关闭窗口不取消。等待阶段若长期不变，请在任务状态检查任务服务。节点数量和合并结果来自最近一轮成功解析；失败不覆盖最近成功时间。内核运行状态在代理页面上方显示。
          </p>
        </div>
      </Modal>
      {editor && (
        <SubscriptionEditor
          item={editor.item}
          onClose={() => setEditor(null)}
          onSaved={query.reload}
        />
      )}
      <Modal
        open={!!schedule}
        onOpenChange={(open) => !open && setSchedule(null)}
        title="订阅定时与检测"
      >
        {schedule && (
          <SubscriptionSchedule
            key={`${schedule.id}:${subscriptions.find((s) => s.id === schedule.id)?.version}`}
            subscription={
              subscriptions.find((s) => s.id === schedule.id) || schedule
            }
          />
        )}
      </Modal>
      <Modal
        open={!!deleting}
        onOpenChange={(open) => !open && !busy && setDeleting(null)}
        title="删除订阅"
        footer={
          <div className="row">
            <Button disabled={!!busy} onClick={() => setDeleting(null)}>
              取消
            </Button>
            <Button
              disabled={!!busy}
              onClick={async () => {
                if (
                  await run(
                    deleting.id,
                    `/api/proxy-subscriptions/${deleting.id}?expected_version=${deleting.version}`,
                    "DELETE",
                  )
                )
                  setDeleting(null);
              }}
            >
              确认删除来源
            </Button>
          </div>
        }
      >
        <p>
          删除“{deleting?.name}
          ”来源后，共享节点保留，失去最后来源的节点退役；已有账户绑定保留并明确报告不可用。代理池引用该来源时拒绝删除。
        </p>
        {error && <Notice>{error}</Notice>}
      </Modal>
    </>
  );
}

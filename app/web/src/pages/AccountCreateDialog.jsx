import React, { useRef, useState } from "react";
import { api } from "../api";
import { useApp, Button, Notice } from "../ui";
import { Modal } from "../components/Management";

function RecordCreateDialog({ onClose }) {
  const { data, refresh } = useApp();
  const [step, setStep] = useState(0);
  const [platform, setPlatform] = useState("");
  const [label, setLabel] = useState("");
  const [externalId, setExternalId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState(false);
  const submitting = useRef(false);
  const platforms = data.platforms || [];

  async function submit(event) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      await api("/api/accounts", "POST", {
        platform,
        external_id: externalId.trim(),
        label: label.trim(),
      });
      setCreated(true);
      refresh();
    } catch (submitError) {
      setError(submitError.message);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      busy={busy}
      onOpenChange={(open) => {
        if (!open && !submitting.current) onClose();
      }}
      title="添加账户"
      description={
        created
          ? "账户记录已建立"
          : `第 ${step + 1} 步，共 2 步 · 仅建立账户记录`
      }
      size="medium"
    >
      {error && (
        <div role="alert">
          <Notice>{error}</Notice>
        </div>
      )}
      {created ? (
        <>
          <Notice>
            账户记录已建立。还需配置凭证并完成验证，不能仅凭此记录接入业务。
          </Notice>
          <div className="row topgap">
            <Button onClick={onClose}>完成</Button>
          </div>
        </>
      ) : step === 0 ? (
        <>
          <p className="quietnote">
            仅列出本平台支持的账户平台；选择后填写身份标识。
          </p>
          <div className="field gap">
            <label htmlFor="create-account-platform">平台</label>
            <select
              id="create-account-platform"
              value={platform}
              onChange={(e) => setPlatform(e.target.value)}
            >
              <option value="">请选择平台</option>
              {platforms.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </div>
          <div className="row wrap topgap">
            <Button onClick={onClose}>取消</Button>
            <Button
              variant="primary"
              disabled={!platform}
              onClick={() => setStep(1)}
            >
              下一步
            </Button>
          </div>
        </>
      ) : (
        <form onSubmit={submit}>
          <p className="quietnote">
            {platform} · 创建记录后再单独配置凭证，不会在这一步触发注册或登录。
          </p>
          <div className="field gap">
            <label htmlFor="create-account-label">账户名称</label>
            <input
              id="create-account-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              required
              maxLength={100}
            />
          </div>
          <div className="field gap">
            <label htmlFor="create-account-external-id">平台账户 ID</label>
            <input
              id="create-account-external-id"
              value={externalId}
              onChange={(e) => setExternalId(e.target.value)}
              required
              pattern="[a-zA-Z0-9_.:\-]+"
              maxLength={100}
            />
            <small>不在此输入手机号、密码或令牌。</small>
          </div>
          <div className="row wrap topgap">
            <Button disabled={busy} onClick={() => setStep(0)}>
              上一步
            </Button>
            <Button disabled={busy} onClick={onClose}>
              取消
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={busy || !label.trim() || !externalId.trim()}
            >
              {busy ? "创建中…" : "创建账户记录"}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}

export default function AccountCreateDialog({
  onClose,
  initialPlatform = "",
  initialMode = "import",
}) {
  const { data, refresh } = useApp();
  const [mode, setMode] = useState(initialMode),
    [platform, setPlatform] = useState(initialPlatform);
  const [content, setContent] = useState(""),
    [preview, setPreview] = useState(null),
    [imported, setImported] = useState(false);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const lock = useRef(false);
  const hints = {
    Loomy: "userid + session（或 token）",
    小浣熊: "userId + access_token，可附 refresh_token",
    "WPS 灵犀": "uid + cookie（包含完整 wps_sid）",
    "WorkBuddy 国内": "account（uid / 企业身份）+ auth（accessToken）",
    "WorkBuddy 国际": "account（uid / 企业身份）+ auth（accessToken）",
    CodeArts: "verified_identity + access_key + secret_key + security_token",
    Marvis: "openid + access_token",
  };
  async function submit(commit = false) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      if (commit) {
        await api(`/api/imports/${preview.id}/commit`, "POST", {
          expected_version: preview.version,
        });
        setContent("");
        setImported(true);
        refresh();
      } else
        setPreview(
          await api("/api/imports/preview", "POST", {
            content,
            platform,
            source: `${platform} 账户导入`,
            mode: "accounts",
          }),
        );
    } catch (e) {
      setError(e.message);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  if (mode === "record") return <RecordCreateDialog onClose={onClose} />;
  const counts = preview?.counts || preview?.data?.counts || {};
  return (
    <Modal
      open
      busy={busy}
      onOpenChange={(open) => !open && !lock.current && onClose()}
      title="添加账户"
      description="选择平台与接入方式"
      footer={
        !imported && (
          <Button disabled={busy} onClick={onClose}>
            取消
          </Button>
        )
      }
    >
      {error && <Notice>{error}</Notice>}
      {imported ? (
        <>
          <Notice>
            账户及凭证已保存，尚需身份核验。请到导入记录继续处理。
          </Notice>
          <div className="row wrap topgap">
            <a className="btn primary" href="#imports" onClick={onClose}>
              查看导入记录
            </a>
            <Button onClick={onClose}>完成</Button>
          </div>
        </>
      ) : (
        <>
          <div className="segmented section-tabs" aria-label="账户接入方式">
            {[
              ["import", "导入凭证"],
              ["login", "注册 / 登录"],
            ].map(([key, name]) => (
              <button
                type="button"
                key={key}
                disabled={busy || !!preview}
                className={mode === key ? "active" : ""}
                aria-pressed={mode === key}
                onClick={() => {
                  setMode(key);
                  setError("");
                }}
              >
                {name}
              </button>
            ))}
          </div>
          <label className="field gap">
            平台
            <select
              aria-label="平台"
              value={platform}
              disabled={busy || !!preview}
              onChange={(e) => setPlatform(e.target.value)}
            >
              <option value="">请选择平台</option>
              {(data.platforms || []).map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </label>
          {mode === "login" ? (
            <>
              <p className="quietnote">
                使用所选平台的登录方式，取得凭证后自动入库并衔接维护。
              </p>
              <Button
                variant="primary"
                disabled={!platform}
                onClick={() => {
                  sessionStorage.setItem("registration-platform", platform);
                  onClose();
                  location.hash = "register";
                }}
              >
                前往注册 / 登录
              </Button>
            </>
          ) : preview ? (
            <>
              <div className="import-preview-counts">
                {Object.entries(counts).map(([key, value]) => (
                  <span key={key}>
                    {{
                      new: "可新增",
                      invalid: "材料不完整",
                      existing_account: "已有账户，保留原凭证",
                      duplicate_in_file: "重复记录",
                    }[key] || key}
                    ：<b>{value}</b>
                  </span>
                ))}
              </div>
              <p className="quietnote">
                只新增格式正确的账户；已有账户的凭证不会覆盖。
              </p>
              <div className="row wrap">
                <Button disabled={busy} onClick={() => setPreview(null)}>
                  修改材料
                </Button>
                <Button
                  variant="primary"
                  disabled={busy || !counts.new}
                  onClick={() => submit(true)}
                >
                  {busy ? "正在保存…" : "确认导入"}
                </Button>
              </div>
            </>
          ) : (
            <>
              <label className="field gap">
                凭证 JSON
                <textarea
                  aria-label="凭证 JSON"
                  rows={7}
                  value={content}
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(e) => setContent(e.target.value)}
                  placeholder="粘贴官方导出的账户 JSON 或 JSONL"
                />
              </label>
              <p className="quietnote">
                {platform
                  ? hints[platform] || "使用该平台官方导出的完整账户材料"
                  : "先选择平台，查看所需材料"}
                。材料不会在列表回显。
              </p>
              <div className="row wrap">
                <Button
                  disabled={busy || !platform || !content.trim()}
                  variant="primary"
                  onClick={() => submit()}
                >
                  {busy ? "正在检查…" : "检查导入内容"}
                </Button>
                <a className="btn" href="#imports" onClick={onClose}>
                  批量文件导入
                </a>
              </div>
            </>
          )}
          {!preview && (
            <details className="secondary-choice">
              <summary>其他方式</summary>
              <Button disabled={busy} onClick={() => setMode("record")}>
                仅手动建立账户记录
              </Button>
              <small>不包含凭证，不能直接用于转发。</small>
            </details>
          )}
        </>
      )}
    </Modal>
  );
}

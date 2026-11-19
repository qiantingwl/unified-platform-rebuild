import React, { useRef, useState } from "react";
import { api } from "../api";
import { useApp, Button, Notice } from "../ui";
import { Modal } from "../components/Management";

export default function KeyCreateDialog({ onClose }) {
  const { data, refresh } = useApp();
  const [platform, setPlatform] = useState("");
  const [name, setName] = useState("");
  const [model, setModel] = useState("");
  const [otherModels, setOtherModels] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const platforms = data.platforms || [];

  async function submit(event) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    const primary = model.trim();
    const models = [
      ...new Set([primary, ...otherModels.split(/[,，\s]+/).filter(Boolean)]),
    ];
    if (models.length > 200) {
      setError("允许模型最多 200 个，请减少后重试。");
      submitting.current = false;
      setBusy(false);
      return;
    }
    try {
      const result = await api("/api/keys", "POST", {
        name: name.trim(),
        platform,
        model: primary,
        models,
      });
      setToken(result.token);
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
      title="创建 API 密钥"
      description={
        token ? "完整密钥仅展示这一次" : "为指定平台与模型创建独立密钥"
      }
      size="medium"
    >
      {error && (
        <div role="alert">
          <Notice>{error}</Notice>
        </div>
      )}
      {token ? (
        <>
          <Notice>请现在保存密钥。关闭后无法再次读取完整内容。</Notice>
          <textarea
            aria-label="新密钥"
            readOnly
            value={token}
            className="secretfield"
            onFocus={(e) => e.target.select()}
          />
          <div className="row wrap topgap">
            <Button
              onClick={() => {
                if (!navigator.clipboard?.writeText) {
                  setError("复制不可用，请手动选择并复制。");
                  return;
                }
                navigator.clipboard.writeText(token).then(
                  () => setError(""),
                  () => setError("复制失败，请手动选择并复制。"),
                );
              }}
            >
              复制密钥
            </Button>
            <Button onClick={onClose}>完成</Button>
          </div>
        </>
      ) : (
        <form onSubmit={submit}>
          <div className="field gap">
            <label htmlFor="create-key-platform">平台</label>
            <select
              id="create-key-platform"
              value={platform}
              required
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
          <div className="field gap">
            <label htmlFor="create-key-name">密钥名称</label>
            <input
              id="create-key-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              maxLength={100}
            />
          </div>
          <div className="field gap">
            <label htmlFor="create-key-model">默认模型</label>
            <input
              id="create-key-model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              required
              maxLength={100}
              placeholder="填写上游模型名"
            />
          </div>
          <div className="field gap">
            <label htmlFor="create-key-other-models">其他允许模型</label>
            <textarea
              id="create-key-other-models"
              value={otherModels}
              onChange={(e) => setOtherModels(e.target.value)}
              className="secretfield"
              placeholder="可留空；每行一个模型"
            />
            <small>
              密钥始终隔离在所选平台；默认模型必填。创建后可设置模型别名、有效期与配额。
            </small>
          </div>
          <div className="row wrap topgap">
            <Button disabled={busy} onClick={onClose}>
              取消
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={busy || !platform || !name.trim() || !model.trim()}
            >
              {busy ? "创建中…" : "创建密钥"}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}

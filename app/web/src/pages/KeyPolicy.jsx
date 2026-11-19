import React, { useState } from "react";
import { useRecords } from "../use-records";
import { useApp, Notice, Button } from "../ui";

export default function KeyPolicy({ id }) {
  const { mutate } = useApp();
  const [policy, error] = useRecords(`/api/keys/${id}/policy`);
  const [failure, setFailure] = useState(""),
    [busy, setBusy] = useState(false);
  if (!policy) return <Notice>{error || "正在读取密钥权限…"}</Notice>;
  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        const f = new FormData(e.currentTarget);
        setBusy(true);
        setFailure("");
        try {
          const aliases = JSON.parse(f.get("aliases") || "{}");
          if (!aliases || Array.isArray(aliases) || typeof aliases !== "object")
            throw new Error("模型别名需填写 JSON 对象。");
          const body = {
            models: String(f.get("models"))
              .split(/[,，\s]+/)
              .filter(Boolean),
            aliases,
            expires_at: f.get("expires_at")
              ? new Date(`${f.get("expires_at")}+08:00`).getTime() / 1000
              : null,
            max_concurrency: Number(f.get("max_concurrency")),
            rpm: Number(f.get("rpm")),
            daily_requests: Number(f.get("daily_requests")),
            allowed_ips: String(f.get("allowed_ips") || "")
              .split(/[,，\s]+/)
              .filter(Boolean),
          };
          await mutate(
            `/api/keys/${id}/policy`,
            "PUT",
            body,
            "密钥权限已保存。",
          );
        } catch (e) {
          setFailure(e.message);
        } finally {
          setBusy(false);
        }
      }}
    >
      {(error || failure) && <Notice>{error || failure}</Notice>}
      <p className="quietnote">
        每把密钥仍绑定一个平台。下列配额由你手工设置，0 表示不另设该项上限。
      </p>
      <label className="field gap">
        允许的上游模型
        <textarea
          className="secretfield"
          name="models"
          aria-label="允许的上游模型"
          required
          defaultValue={(policy.models || []).join("\n")}
          placeholder="每行一个模型"
        />
      </label>
      <label className="field gap">
        模型别名
        <textarea
          className="secretfield"
          name="aliases"
          aria-label="模型别名"
          defaultValue={JSON.stringify(policy.aliases || {}, null, 2)}
          placeholder={'{"default": "upstream-model"}'}
        />
        <small>别名目标必须在允许模型范围内。</small>
      </label>
      <div className="grid">
        {[
          ["max_concurrency", "密钥并发上限"],
          ["rpm", "每分钟请求上限"],
          ["daily_requests", "每日请求上限"],
        ].map(([key, label]) => (
          <label className="field gap" key={key}>
            {label}
            <input
              name={key}
              aria-label={label}
              type="number"
              min={0}
              step={1}
              defaultValue={policy[key] || 0}
              required
            />
          </label>
        ))}
      </div>
      <label className="field gap">
        到期时间（北京时间）
        <input
          name="expires_at"
          aria-label="到期时间（北京时间）"
          type="datetime-local"
          defaultValue={
            policy.expires_at
              ? new Date((policy.expires_at + 28800) * 1000)
                  .toISOString()
                  .slice(0, 16)
              : ""
          }
        />
        <small>留空永不过期。</small>
      </label>
      <label className="field gap">
        允许的来源 IP / CIDR
        <textarea
          className="secretfield"
          name="allowed_ips"
          aria-label="允许的来源 IP / CIDR"
          defaultValue={(policy.allowed_ips || []).join("\n")}
          placeholder="留空不限制来源；每行一个地址或网段"
        />
      </label>
      <Button type="submit" variant="primary" disabled={busy}>
        {busy ? "保存中…" : "保存密钥权限"}
      </Button>
    </form>
  );
}

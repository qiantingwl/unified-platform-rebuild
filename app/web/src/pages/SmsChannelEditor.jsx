import React, { useState } from "react";
import { useApp, Button, Notice, Switch } from "../ui";

export default function SmsChannelEditor({ channel, onClose }) {
  const { mutate } = useApp();
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [loginMode, setLoginMode] = useState("token"),
    [remember, setRemember] = useState(false),
    [enabled, setEnabled] = useState(channel.data.enabled !== false);
  return (
    <form
      className="topgap"
      onSubmit={async (e) => {
        e.preventDefault();
        const form = new FormData(e.currentTarget);
        setBusy(true);
        setError("");
        try {
          if (loginMode === "password") {
            await mutate(
              `/api/sms/channels/${channel.id}/credential-login`,
              "POST",
              {
                user: form.get("user"),
                password: form.get("password"),
                expected_version: channel.version,
                remember: form.has("remember"),
              },
              "渠道登录成功，Token 已更新。",
            );
            onClose();
            return;
          }
          const rawPairs = String(form.get("pairs") || "").trim();
          const pairs = rawPairs
            ? rawPairs.split(/\r?\n/).map((line) => {
                const [project, token, ...extra] = line.trim().split(/\s+/);
                if (!project || !token || extra.length)
                  throw new Error(
                    "每行填写一个项目 SID 和 Token，以空格分隔。",
                  );
                return { project, token };
              })
            : null;
          await mutate(
            `/api/sms/channels/${channel.id}`,
            "PUT",
            {
              name: form.get("name"),
              provider: channel.data.provider,
              base_url: form.get("base"),
              ...(String(form.get("tokens") || "").trim()
                ? {
                    tokens: String(form.get("tokens"))
                      .split(/\r?\n/)
                      .map((x) => x.trim())
                      .filter(Boolean),
                  }
                : {}),
              projects: String(form.get("projects") || "")
                .split(/[,，\s]+/)
                .filter(Boolean),
              project: form.get("project") || "",
              keyword: form.get("keyword") || "",
              keyword_get: form.get("keyword_get") || "",
              keyword_msg: form.get("keyword_msg") || "",
              ...(pairs ? { pairs } : {}),
              isp: form.get("isp") || "",
              card_type: form.get("card_type") || "全部",
              expected_version: channel.version,
              enabled,
            },
            "接码配置已更新。",
          );
          onClose();
        } catch (e) {
          setError(e.message);
        } finally {
          setBusy(false);
        }
      }}
    >
      {error && <Notice>{error}</Notice>}
      {channel.data.provider === "haozhuma" && (
        <div className="segmented">
          <button
            type="button"
            className={loginMode === "token" ? "active" : ""}
            onClick={() => setLoginMode("token")}
          >
            配置与 Token
          </button>
          <button
            type="button"
            disabled={
              !!channel.data.pair_count || (channel.data.token_count || 1) > 1
            }
            className={loginMode === "password" ? "active" : ""}
            onClick={() => setLoginMode("password")}
          >
            账号密码登录
          </button>
        </div>
      )}
      {loginMode === "password" ? (
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
          <Switch
            name="remember"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
            label="保存加密登录材料"
            description="默认不保存供应商密码，只更新换取的 Token。"
          />
          <p className="quietnote">仅支持单 Token 豪猪渠道，登录不购号。</p>
        </>
      ) : (
        <div className="grid">
          <label className="field gap">
            名称
            <input
              name="name"
              defaultValue={channel.name}
              required
              maxLength={100}
            />
          </label>
          <label className="field gap">
            API 地址
            <input
              name="base"
              defaultValue={
                channel.data.base_url ||
                (channel.data.provider === "haozhuma"
                  ? "https://api.haozhuma.com/sms/"
                  : "https://api.eomsg.com/zc/data.php")
              }
              required
            />
          </label>
          <label className="field gap">
            更新 Token 池
            <textarea
              className="secretfield"
              name="tokens"
              disabled={!!channel.data.pair_count}
              autoComplete="off"
              placeholder="每行一个 Token；留空保留全部已有令牌"
            />
            <small>
              当前保存{" "}
              {channel.data.token_count ?? (channel.has_secret ? 1 : 0)}{" "}
              个令牌。输入后替换全部令牌，内容不会回显。
            </small>
          </label>
          {channel.data.provider === "haozhuma" && (
            <label className="field gap">
              更新明确配对（项目 SID + Token）
              <textarea
                className="secretfield"
                name="pairs"
                autoComplete="off"
                placeholder="每行：项目SID Token；留空保留已有配对"
              />
              <small>
                现有 {channel.data.pair_count || 0}{" "}
                组。填入后替换全部配对；不生成令牌与项目交叉组合。
              </small>
            </label>
          )}
          <label className="field gap">
            项目 SID
            <input name="project" defaultValue={channel.data.project || ""} />
          </label>
          <label className="field gap">
            附加项目 SID
            <input
              name="projects"
              defaultValue={(channel.data.projects || []).join(", ")}
              placeholder="逗号分隔，可留空"
            />
          </label>
          <label className="field gap">
            短信关键字
            <input name="keyword" defaultValue={channel.data.keyword || ""} />
          </label>
          <label className="field gap">
            取号关键字（可选）
            <input
              name="keyword_get"
              defaultValue={channel.data.keyword_get || ""}
            />
          </label>
          <label className="field gap">
            收码关键字（可选）
            <input
              name="keyword_msg"
              defaultValue={channel.data.keyword_msg || ""}
            />
          </label>
          <label className="field gap">
            运营商
            <input name="isp" defaultValue={channel.data.isp || ""} />
          </label>
          <label className="field gap">
            号码类型
            <input
              name="card_type"
              defaultValue={channel.data.card_type || "全部"}
            />
          </label>
        </div>
      )}
      <Switch
        name="enabled"
        label="允许新任务使用此渠道"
        description="关闭只停止新取号，已有号码仍继续收码。"
        checked={enabled}
        onChange={(e) => setEnabled(e.target.checked)}
      />
      <div className="row gap">
        <Button type="submit" disabled={busy}>
          {loginMode === "password" ? "登录并更新 Token" : "保存修改"}
        </Button>
        <Button disabled={busy} onClick={onClose}>
          取消编辑
        </Button>
      </div>
    </form>
  );
}

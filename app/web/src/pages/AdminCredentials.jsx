import React, { useState } from "react";
import { Head, Panel, Tabs, Button, Notice } from "../ui";
import { api } from "../api";
import { useQuery } from "../query";
export default function AdminCredentials() {
  const query = useQuery("/api/session/config");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [password, setPassword] = useState(""),
    [confirmation, setConfirmation] = useState("");
  return (
    <>
      <Head
        name="管理员登录"
        desc="修改当前控制台的管理员登录凭据。"
      />
      <Tabs group="settings" />
      <Panel name="独立管理员账号与密码">
        <div className="pad">
          {query.error && <Notice>{query.error}</Notice>}
          <p className="quietnote">
            当前登录方式：
            {query.result?.mode === "password"
              ? "账号与密码"
              : query.result?.mode === "token"
                ? "管理令牌"
                : "正在读取"}
            。保存后切换为账号密码登录，现有管理会话失效，需要重新登录。
          </p>
          {error && <Notice>{error}</Notice>}
          <form
            className="admin-credentials-form"
            onSubmit={async (e) => {
              e.preventDefault();
              if (password !== confirmation) return;
              setBusy(true);
              setError("");
              try {
                await api("/api/admin/credentials", "PUT", {
                  username: new FormData(e.currentTarget).get("username"),
                  password,
                });
                setPassword("");
                setConfirmation("");
                dispatchEvent(new Event("console-session-expired"));
              } catch (e) {
                setError(e.message);
              } finally {
                setBusy(false);
              }
            }}
          >
            <label className="field gap">
              新管理员账号
              <input
                name="username"
                required
                maxLength="200"
                autoComplete="username"
                disabled={busy}
              />
            </label>
            <label className="field gap">
              新密码
              <input
                type="password"
                minLength={12}
                maxLength={1024}
                required
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={busy}
              />
            </label>
            <label className="field gap">
              确认新密码
              <input
                type="password"
                minLength={12}
                maxLength={1024}
                required
                autoComplete="new-password"
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
                disabled={busy}
              />
            </label>
            <p className="quietnote">密码至少12个字符，不回显已保存密码。</p>
            {confirmation && password !== confirmation && (
              <p role="alert">两次密码不一致。</p>
            )}
            <Button
              type="submit"
              variant="primary"
              disabled={busy || !password || password !== confirmation}
            >
              {busy ? "正在保存…" : "保存并重新登录"}
            </Button>
          </form>
        </div>
      </Panel>
    </>
  );
}

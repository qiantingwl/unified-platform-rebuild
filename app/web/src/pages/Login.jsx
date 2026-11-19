import React, { useEffect, useState } from "react";
import { Button, Notice } from "../ui";
import { api } from "../api";
export default function Login({ onLogin, error }) {
  const [busy, setBusy] = useState(false),
    [failure, setFailure] = useState(""),
    [mode, setMode] = useState(null),
    [configError, setConfigError] = useState("");
  async function loadConfig() {
    setConfigError("");
    try {
      const c = await api("/api/session/config");
      if (!["password", "token"].includes(c.mode))
        throw new Error("登录方式尚未配置。");
      setMode(c.mode);
    } catch (e) {
      setConfigError(e.message);
    }
  }
  useEffect(() => {
    loadConfig();
  }, []);
  return (
    <div className="login-screen">
      <section className="login-panel">
        <h1>统一账户平台</h1>
        <p>登录管理后台</p>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            const form = e.currentTarget;
            if (busy) return;
            setBusy(true);
            setFailure("");
            try {
              const values = new FormData(form);
              await api(
                "/api/session",
                "POST",
                mode === "password"
                  ? {
                      username: values.get("username"),
                      password: values.get("password"),
                    }
                  : { token: values.get("token") },
              );
              form.reset();
              await onLogin();
            } catch (e) {
              setFailure(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          {(failure || error) && <Notice>{failure || error}</Notice>}
          {configError && (
            <Notice>
              {configError}
              <Button onClick={loadConfig}>重试</Button>
            </Notice>
          )}
          {!mode && !configError && <p>正在读取登录方式…</p>}
          {mode === "password" ? (
            <>
              <label className="field gap">
                管理员账号
                <input
                  name="username"
                  required
                  autoComplete="username"
                  autoFocus
                  disabled={busy}
                />
              </label>
              <label className="field gap">
                密码
                <input
                  name="password"
                  type="password"
                  required
                  autoComplete="current-password"
                  disabled={busy}
                />
              </label>
            </>
          ) : mode === "token" ? (
            <>
              <label className="field gap">
                管理令牌
                <input
                  name="token"
                  type="password"
                  required
                  autoComplete="current-password"
                  autoFocus
                  disabled={busy}
                />
              </label>
              <p className="quietnote">
                使用管理员设置的管理令牌，与模型调用的 API 密钥分开。
              </p>
            </>
          ) : null}
          <Button type="submit" variant="primary" disabled={busy || !mode}>
            {busy ? "正在登录…" : "登录"}
          </Button>
        </form>
      </section>
    </div>
  );
}

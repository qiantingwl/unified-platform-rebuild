import React, { useEffect, useState, useRef } from "react";
import { api } from "../api";
import { Button, Notice, useApp } from "../ui";

export default function BrowserView({ sessionId, onClose }) {
  const { mutate } = useApp();
  const [ready, setReady] = useState(false),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [text, setText] = useState(""),
    [frame, setFrame] = useState(0);
  const path = `/api/registration/${sessionId}/browser`;
  const pointer = useRef(null);
  const point = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(1280, ((e.clientX - r.left) / r.width) * 1280)),
      y: Math.max(0, Math.min(900, ((e.clientY - r.top) / r.height) * 900)),
    };
  };
  useEffect(() => {
    let active = true;
    api(path + "/start", "POST", {})
      .then(() => {
        if (active) setReady(true);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [path]);
  useEffect(() => {
    if (!ready || busy) return;
    const timer = setInterval(() => setFrame(Date.now()), 3000);
    return () => clearInterval(timer);
  }, [ready, busy]);
  async function action(body) {
    setBusy(true);
    setError("");
    try {
      const r = await api(path + "/action", "POST", body);
      if (r.waiting_code) setError("短信尚未收到，可稍后再次读取。");
      setFrame(Date.now());
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="registration-browser">
      <strong>独立登录窗口</strong>
      <p>
        点击画面操作官方页面；手机号、验证码与人机校验都在当前会话完成。收到授权回调后自动核验并回写；也可点击“完成核验”继续处理。
      </p>
      {error && <Notice>{error}</Notice>}
      {ready && (
        <img
          alt="当前官方登录页面，可点击或拖动"
          draggable={false}
          style={{ touchAction: "none" }}
          src={path + "/image?frame=" + frame}
          onPointerDown={(e) => {
            if (busy) return;
            e.currentTarget.setPointerCapture(e.pointerId);
            pointer.current = point(e);
          }}
          onPointerCancel={() => {
            pointer.current = null;
          }}
          onPointerUp={(e) => {
            if (!pointer.current) return;
            const start = pointer.current,
              end = point(e);
            pointer.current = null;
            action(
              Math.hypot(end.x - start.x, end.y - start.y) > 6
                ? { action: "drag", ...start, end_x: end.x, end_y: end.y }
                : { action: "click", ...end },
            );
          }}
        />
      )}
      <div className="row wrap gap">
        <input
          aria-label="输入到登录窗口"
          type="password"
          autoComplete="off"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="先点击画面里的输入框，再输入内容"
        />
        <Button
          disabled={busy || !ready || !text}
          onClick={() => {
            action({ action: "type", text });
            setText("");
          }}
        >
          输入
        </Button>
        <Button
          disabled={busy || !ready}
          onClick={() => action({ action: "key", text: "Control+A" })}
        >
          全选
        </Button>
        <Button
          disabled={busy || !ready}
          onClick={() => action({ action: "key", text: "Enter" })}
        >
          回车
        </Button>
        <Button
          disabled={busy || !ready}
          onClick={() => action({ action: "scroll", delta: 500 })}
        >
          向下滚动
        </Button>
        <Button disabled={busy || !ready} onClick={() => setFrame(Date.now())}>
          更新画面
        </Button>
      </div>
      <div className="row wrap gap">
        <Button
          disabled={busy || !ready}
          onClick={() => action({ action: "phone" })}
        >
          填入关联手机号
        </Button>
        <Button
          disabled={busy || !ready}
          onClick={() => action({ action: "sms" })}
        >
          读取短信并填入
        </Button>
      </div>
      <div className="row wrap gap">
        <Button
          variant="primary"
          disabled={busy || !ready}
          onClick={async () => {
            setBusy(true);
            try {
              const r = await mutate(
                path + "/collect",
                "POST",
                {},
                "授权回执已核对。",
              );
              if (r.status === "succeeded") onClose();
              else setFrame(Date.now());
            } catch (e) {
              setError(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          完成核验
        </Button>
        <Button
          disabled={busy}
          onClick={async () => {
            try {
              await api(path, "DELETE");
              onClose();
            } catch (e) {
              setError(e.message);
            }
          }}
        >
          保存会话并关闭窗口
        </Button>
      </div>
    </div>
  );
}

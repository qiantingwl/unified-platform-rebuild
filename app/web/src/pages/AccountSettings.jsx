import AccountProxyList from "./AccountProxyList";
import React, { useState } from "react";
import { useApp, Button, Notice, Switch } from "../ui";
import { Modal, accountName } from "../components/Management";
export default function AccountSettings({ account: a, open, onClose }) {
  const { mutate } = useApp();
  const initial = {
    enabled: a.enabled,
    concurrency: a.concurrency,
    priority: a.priority ?? 0,
    interval: a.balance_interval_seconds ?? "",
    dailyEnabled: a.daily_checkin_enabled !== false,
    dailyMinute: a.daily_checkin_minute ?? "",
  };
  const [draft, setDraft] = useState(initial),
    [baseline, setBaseline] = useState(initial),
    [pane, setPane] = useState("basic"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [discard, setDiscard] = useState(false),
    [networkOpen, setNetworkOpen] = useState(false);
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);
  const update = (key, value) => setDraft((old) => ({ ...old, [key]: value }));
  const close = () => {
    if (busy) return;
    if (dirty) setDiscard(true);
    else onClose();
  };
  const formId = `account-settings-${a.id}`;
  return (
    <>
      <Modal
        open={open}
        onOpenChange={(v) => !v && close()}
        title={`${accountName(a)} · 账户设置`}
        description="所有更改仅作用于当前账户；平台共享规则在自动维护及路由页面配置。"
        footer={
          <>
            <span className="modal-save-state">
              {busy
                ? "正在保存…"
                : dirty
                  ? "有未保存的更改"
                  : "没有未保存的更改"}
            </span>
            <Button disabled={busy} onClick={close}>
              取消
            </Button>
            <Button
              form={formId}
              type="submit"
              variant="primary"
              disabled={busy || !dirty}
            >
              保存更改
            </Button>
          </>
        }
      >
        {error && <Notice>{error}</Notice>}
        <div className="segmented" role="tablist" aria-label="账户设置分组">
          {[
            ["basic", "基本与调度"],
            ["query", "余额查询"],
            ["maintenance", "签到计划"],
            ["network", "网络出口"],
          ].map(([v, n]) => (
            <button
              type="button"
              role="tab"
              aria-selected={pane === v}
              key={v}
              className={pane === v ? "active" : ""}
              onClick={() => setPane(v)}
            >
              {n}
              {(
                v === "basic"
                  ? draft.enabled !== baseline.enabled ||
                    draft.concurrency !== baseline.concurrency ||
                    draft.priority !== baseline.priority
                  : v === "query"
                    ? draft.interval !== baseline.interval
                    : v === "maintenance" &&
                      (draft.dailyEnabled !== baseline.dailyEnabled ||
                        draft.dailyMinute !== baseline.dailyMinute)
              )
                ? " · 未保存"
                : ""}
            </button>
          ))}
        </div>
        <form
          id={formId}
          onInvalid={(e) => {
            const target = e.target;
            setPane(
              target.name === "balance_interval_seconds"
                ? "query"
                : target.name === "daily_checkin_minute"
                  ? "maintenance"
                  : "basic",
            );
            setTimeout(() => target.focus(), 0);
          }}
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            try {
              await mutate(
                `/api/accounts/${a.id}/settings`,
                "PATCH",
                {
                  ...(draft.enabled !== baseline.enabled
                    ? { enabled: draft.enabled }
                    : {}),
                  ...(draft.concurrency !== baseline.concurrency
                    ? { concurrency: Number(draft.concurrency) }
                    : {}),
                  ...(draft.priority !== baseline.priority
                    ? { priority: Number(draft.priority) }
                    : {}),
                  ...(draft.interval !== baseline.interval
                    ? draft.interval !== ""
                      ? { balance_interval_seconds: Number(draft.interval) }
                      : { reset_balance_interval: true }
                    : {}),
                  ...(draft.dailyEnabled !== baseline.dailyEnabled
                    ? { daily_checkin_enabled: draft.dailyEnabled }
                    : {}),
                  ...(draft.dailyMinute !== baseline.dailyMinute
                    ? draft.dailyMinute !== ""
                      ? { daily_checkin_minute: Number(draft.dailyMinute) }
                      : { reset_daily_checkin_minute: true }
                    : {}),
                },
                "账户设置已保存。",
              );
              setBaseline({ ...draft });
              onClose();
            } catch (e) {
              setError(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <section hidden={pane !== "basic"} className="account-settings-pane">
            <h3>基本与调度</h3>
            <Switch
              label="允许调度此账户"
              description="仅手动保存改变开关，自动维护不修改。"
              checked={draft.enabled}
              disabled={busy}
              onChange={(e) => update("enabled", e.target.checked)}
            />
            <label className="field gap">
              账户业务并发
              <input
                name="concurrency"
                required
                type="number"
                min="1"
                max="1024"
                value={draft.concurrency}
                onChange={(e) => update("concurrency", Number(e.target.value))}
              />
              <small>
                当前账户同时承接的业务请求上限，不改变共享任务容量。
              </small>
            </label>
            <label className="field gap">
              调度优先级
              <input
                name="priority"
                required
                type="number"
                min="-100000"
                max="100000"
                step="1"
                value={draft.priority}
                onChange={(e) => update("priority", Number(e.target.value))}
              />
              <small>仅在所属平台和路由账户范围内参与选择。</small>
            </label>
          </section>
          <section hidden={pane !== "query"} className="account-settings-pane">
            <h3>余额查询</h3>
            <label className="field gap">
              余额查询间隔（秒）
              <input
                name="balance_interval_seconds"
                type="number"
                min="60"
                max="2592000"
                step="1"
                placeholder="留空沿用全局设置"
                value={draft.interval}
                onChange={(e) => update("interval", e.target.value)}
              />
              <small>
                留空跟随自动维护的全局周期；仅覆盖当前账户的查询间隔。
              </small>
            </label>
          </section>
          <section
            hidden={pane !== "maintenance"}
            className="account-settings-pane"
          >
            <h3>每日签到计划</h3>
            <Switch
              label="参与每日自动签到"
              checked={draft.dailyEnabled}
              onChange={(e) => update("dailyEnabled", e.target.checked)}
              description="仅控制本账户自动签到；不改变业务启用或并发。"
            />
            <label className="field gap">
              每日签到时刻（北京时间）
              <input
                type="time"
                name="daily_checkin_minute"
                value={
                  draft.dailyMinute === ""
                    ? ""
                    : `${String(Math.floor(draft.dailyMinute / 60)).padStart(2, "0")}:${String(draft.dailyMinute % 60).padStart(2, "0")}`
                }
                onChange={(e) =>
                  update(
                    "dailyMinute",
                    e.target.value === ""
                      ? ""
                      : Number(e.target.value.split(":")[0]) * 60 +
                          Number(e.target.value.split(":")[1]),
                  )
                }
              />
              <small>留空继承自动维护的全局每日签到时刻。</small>
            </label>
            <Button onClick={() => update("dailyMinute", "")}>
              继承全局签到时刻
            </Button>
          </section>
          <section
            hidden={pane !== "network"}
            className="account-settings-pane"
          >
            <h3>当前账户网络出口</h3>
            <p className="quietnote">
              出口变更在独立绑定窗口保存，仅影响当前账户；已创建注册会话保持其固定出口。
            </p>
            <Button onClick={() => setNetworkOpen(true)}>
              编辑当前账户出口
            </Button>
          </section>
        </form>
      </Modal>
      {networkOpen && (
        <AccountProxyList
          key={a.id}
          initialAccount={a}
          onClose={() => setNetworkOpen(false)}
        />
      )}
      <Modal
        open={discard}
        onOpenChange={setDiscard}
        size="small"
        title="放弃未保存的修改？"
        description="当前账户各分组的未保存更改将被放弃。"
        footer={
          <>
            <Button onClick={() => setDiscard(false)}>继续编辑</Button>
            <Button
              onClick={() => {
                setDiscard(false);
                onClose();
              }}
            >
              放弃修改
            </Button>
          </>
        }
      />
    </>
  );
}

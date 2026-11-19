import React, { useEffect, useRef, useState } from "react";
import {
  useApp,
  Head,
  Panel,
  Tabs,
  Notice,
  Button,
  Switch,
  Line,
  tm,
} from "../ui";
import { useRecords } from "../use-records";
import { api } from "../api";

const groups = [
  {
    title: "签到与余额",
    desc: "持续任务按开关安排，已确认的签到不会重复提交。",
    switches: [
      ["daily_checkin", "每日自动签到", "签到成功后立即安排该账户查询余额。"],
      [
        "refresh_balances",
        "定期查询余额",
        "查询失败保留最近成功的余额，不显示为零。",
      ],
    ],
    fields: [
      ["daily_checkin_minute", "每日签到时刻（北京时间）", 0, 1439],
      ["balance_interval", "余额查询间隔（秒）", 60, 86400],
      ["checkin_max_attempts", "当天签到最多尝试次数", 1, 10],
      ["checkin_retry_interval", "签到失败重试间隔（秒）", 60, 86400],
    ],
  },
  {
    title: "凭证与恢复",
    desc: "自动维护只更新凭证和异常状态，不改变账户人工开关。",
    switches: [
      ["auto_renew", "自动续期", "在凭证到期前安排续期并核验新凭证。"],
      [
        "auto_relogin",
        "自动重登",
        "每账户每天一次，连续三次未收到验证码后停止自动重登。",
      ],
      [
        "recovery_tests",
        "异常模型回答验证",
        "调用对应模型，只有有效回答并正常结束才恢复。",
      ],
    ],
    fields: [
      ["renewal_lead_seconds", "到期前续期（秒）", 60, 86400],
      ["recovery_interval", "模型验证间隔（秒）", 60, 86400],
    ],
  },
  {
    title: "转发与重试",
    desc: "重试始终在请求所属平台内；收到有效输出后不重放请求。",
    switches: [
      [
        "placeholder_enabled",
        "首字占位",
        "开启时先向客户端发送快速空白占位；有效输出时间另行记录。",
      ],
    ],
    fields: [
      ["startup_seconds", "首次有效输出总等待（秒）", 5, 600],
      ["idle_seconds", "有效输出后静默等待（秒）", 5, 600],
      ["max_attempts", "单请求最多尝试账户数", 1, 10],
    ],
  },
  {
    title: "邀请与任务资源",
    desc: "注册与维护共享执行资源，账户业务并发单独配置。",
    switches: [
      [
        "auto_invitation",
        "Loomy 自动邀请",
        "自动选择可用邀请码并提交绑定，回读确认后查询余额。",
      ],
    ],
    fields: [
      ["invitation_interval", "邀请检查间隔（秒）", 300, 86400],
      ["maintenance_capacity", "共享任务执行容量", 1, 1024],
    ],
  },
];

export default function SettingsPage() {
  const { data, mutate, page } = useApp();
  const automatic = page === "automaintenance";
  const visibleGroups = automatic
    ? groups.filter((g) => g.title !== "转发与重试")
    : groups.filter((g) => g.title === "转发与重试");
  const [section, setSection] = useState(visibleGroups[0].title);
  const [record, loadError, reloadSettings] = useRecords("/api/settings");
  const [draft, setDraft] = useState(null),
    [version, setVersion] = useState(null);
  const [dirty, setDirty] = useState(false),
    [busy, setBusy] = useState(false),
    [savedPendingRead, setSavedPendingRead] = useState(false),
    [error, setError] = useState("");
  const submitting = useRef(false);
  const savedPendingRef = useRef(false);
  const expectedSavedVersion = useRef(null);
  useEffect(() => {
    if (record && !dirty && !savedPendingRef.current && (version === null || record.version >= version)) {
      setDraft({ ...record.values });
      setVersion(record.version);
    }
  }, [record, dirty, version]);
  function change(key, value) {
    if (savedPendingRef.current) return;
    setDraft((old) => ({ ...old, [key]: value }));
    setDirty(true);
  }
  async function readCommitted() {
    const committed = await api("/api/settings");
    if (!committed || !Number.isInteger(committed.version) || !committed.values || (expectedSavedVersion.current != null && committed.version < expectedSavedVersion.current)) throw new Error("最新设置尚未读取完整，请重新查询。");
    setDraft({ ...committed.values });
    setVersion(committed.version);
    setDirty(false);
    savedPendingRef.current = false;
    expectedSavedVersion.current = null;
    setSavedPendingRead(false);
    setError("");
  }
  async function retryCommitted() {
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    try { await readCommitted(); }
    catch (e) { setError(`设置已保存，但读取最新设置失败：${e.message}`); }
    finally { submitting.current = false; setBusy(false); }
  }
  async function save(e) {
    e.preventDefault();
    if (submitting.current || savedPendingRef.current || !draft || version == null || record?.version > version) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try {
      await mutate(
        "/api/settings",
        "PUT",
        { ...draft, expected_version: version },
        "设置已保存，将在 15 秒内应用。",
      );
      // The write is committed. Keep the old version locked until a fresh GET
      // confirms the new values; a failed GET must never enable a second PUT.
      savedPendingRef.current = true;
      expectedSavedVersion.current = version + 1;
      setSavedPendingRead(true);
      try { await readCommitted(); }
      catch (readError) { setError(`设置已保存，但读取最新设置失败：${readError.message}`); }
    } catch (e) {
      setError(e.message);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <>
      <Head
        name={automatic ? "自动维护" : "设置"}
        desc="开关控制持续行为；保存后统一应用，页面刷新不会覆盖尚未保存的编辑。"
      />
      {!automatic && <Tabs group="settings" />}
      {(loadError || error) && <Notice>{error || loadError}</Notice>}
      {savedPendingRead && <Notice>设置已保存，正在确认最新配置；请勿重复提交。{error && <Button disabled={busy} onClick={retryCommitted}>仅重新读取设置</Button>}</Notice>}
      {dirty && !savedPendingRead && record?.version > version && (
        <Notice>
          设置已被其他操作更新。重新载入会丢弃本页未保存的修改。
          <Button disabled={busy} onClick={() => {
            if (!record || !window.confirm("重新载入会丢弃未保存的修改，继续吗？")) return;
            setDraft({ ...record.values });
            setVersion(record.version);
            setDirty(false);
            setError("");
          }}>重新载入最新设置</Button>
        </Notice>
      )}
      {!draft ? (
        <Notice>{loadError ? "设置读取失败，请重新查询。" : "正在读取设置…"}{loadError && <Button onClick={reloadSettings}>重新查询设置</Button>}</Notice>
      ) : (
        <form onSubmit={save}>
          <div className="toolbar">
            <div className="segmented">
              {visibleGroups.map((g) => (
                <button
                  type="button"
                  key={g.title}
                  className={section === g.title ? "active" : ""}
                  aria-pressed={section === g.title}
                  onClick={() => setSection(g.title)}
                >
                  {g.title}
                </button>
              ))}
            </div>
          </div>
          <div>
            {groups
              .filter((g) => g.title === section)
              .map((group) => (
                <Panel key={group.title} name={group.title} desc={group.desc}>
                  <div className="pad">
                    <div className="switchlist">
                      {group.switches.map(([key, label, description]) => (
                        <Switch
                          key={key}
                          name={key}
                          label={label}
                          description={description}
                          checked={!!draft[key]}
                          disabled={busy || savedPendingRead || !(key in draft)}
                          onChange={(e) => change(key, e.target.checked)}
                        />
                      ))}
                    </div>
                    {group.switches.some(([key]) => !(key in draft)) && (
                      <p className="quietnote">
                        当前服务尚未提供该开关，请先完成后端更新。
                      </p>
                    )}
                    <div className="settings-fields">
                      {group.fields.map(([key, label, min, max]) => (
                        <label className="field" key={key}>
                          {label}
                          <input
                            name={key}
                            type={
                              key === "daily_checkin_minute" ? "time" : "number"
                            }
                            min={
                              key === "daily_checkin_minute" ? undefined : min
                            }
                            max={
                              key === "daily_checkin_minute" ? undefined : max
                            }
                            required
                            value={
                              key === "daily_checkin_minute"
                                ? draft[key] == null
                                  ? ""
                                  : `${String(Math.floor(draft[key] / 60)).padStart(2, "0")}:${String(draft[key] % 60).padStart(2, "0")}`
                                : (draft[key] ?? "")
                            }
                            disabled={busy || savedPendingRead}
                            onChange={(e) =>
                              change(
                                key,
                                e.target.value === ""
                                  ? ""
                                  : key === "daily_checkin_minute"
                                    ? Number(e.target.value.split(":")[0]) *
                                        60 +
                                      Number(e.target.value.split(":")[1])
                                    : Number(e.target.value),
                              )
                            }
                          />
                        </label>
                      ))}
                    </div>
                  </div>
                </Panel>
              ))}
          </div>
          <div className="settings-savebar">
            <span>{savedPendingRead ? "已保存，等待读取最新配置" : dirty ? "有尚未保存的修改" : "已与服务配置同步"}</span>
            <div className="row">
              <Button
                disabled={busy || savedPendingRead || !dirty}
                onClick={() => {
                  if (!record) return;
                  setDirty(false);
                  setDraft({ ...record.values });
                  setVersion(record.version);
                  setError("");
                }}
              >
                放弃修改
              </Button>
              <Button
                type="submit"
                variant="primary"
                disabled={busy || savedPendingRead || !dirty || !record || record.version > version}
              >
                {busy ? "保存中…" : "保存设置"}
              </Button>
            </div>
          </div>
        </form>
      )}
      <Panel name="运行信息" className="topgap">
        <div className="pad">
          <Line
            name={`版本 ${data.runtime.version}`}
            desc="主程序、任务服务、浏览器执行器、PostgreSQL 独立运行。"
          />
          <Line
            name="任务服务"
            desc={
              data.runtime.task_service?.online
                ? `在线，最近心跳 ${tm(data.runtime.task_service.seen_at)}`
                : "离线或尚未收到心跳，请到系统状态查看。"
            }
          />
        </div>
      </Panel>
    </>
  );
}

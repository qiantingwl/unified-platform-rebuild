import React, { useEffect, useRef, useState } from "react";
import { api, errors } from "../api";
import { Panel, Button, Notice, Switch, useApp, tm } from "../ui";
import { useRecords } from "../use-records";

import ProxyPicker from "./ProxyPicker";
import { Modal } from "../components/Management";
const supported = ["Loomy", "WorkBuddy 国内", "WPS 灵犀", "小浣熊", "CodeArts"];
const reasons = {
  ready: "满足创建条件",
  disabled: "未启用",
  upstream_disabled: "外部执行未开启",
  existing_batch_active: "同平台已有执行中的批次",
  existing_batch_paused: "同平台批次已暂停，先处理原批次",
  automatic_batch_cancelled:
    "上次自动批次已取消；若需恢复未来批次，请先关闭保存，再开启保存",
  registration_result_unresolved: "注册回执尚未确认，保留名额等待核实",
  registration_in_progress: "同平台注册正在进行，等待原任务结束",
  inventory_sufficient: "库存已满足目标",
};
const defaults = (platform) => ({
  name: `${platform} 自动注册`,
  platform,
  enabled: false,
  mode: "interval",
  interval_minutes: 60,
  inventory_target: 10,
  count: 1,
  sms_channel_id: "",
  proxy_id: null,
  number_budget: null,
  max_consecutive_failures: null,
  max_consecutive_skips: null,
  parallel: 4,
});

export default function RegistrationAutomation({ platform }) {
  const { mutate } = useApp();
  const submitLock = useRef(false);
  const [open, setOpen] = useState(false);
  const [records, loadError] = useRecords(
    `/api/registration-automation?platform=${encodeURIComponent(platform)}`,
  );
  const [channels] = useRecords("/api/sms/channels");

  const policy = records?.policies?.[0];
  const [form, setForm] = useState(() => defaults(platform));
  const [version, setVersion] = useState(0),
    [dirty, setDirty] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [preview, setPreview] = useState(null);
  useEffect(() => {
    setForm(defaults(platform));
    setVersion(0);
    setDirty(false);
    setPreview(null);
    setError("");
  }, [platform]);
  useEffect(() => {
    if (!dirty && policy && policy.version >= version) {
      setForm(policy.config);
      setVersion(policy.version);
    }
  }, [policy?.version, platform, dirty, version]);
  const update = (key, value) => {
    setDirty(true);
    setPreview(null);
    setForm((old) => ({ ...old, [key]: value }));
  };
  if (!supported.includes(platform)) return null;
  const payload = { ...form, expected_version: version };
  const state = preview || policy?.preview;
  return (
    <Panel name="自动创建注册批次">
      <div className="pad">
        <p className="quietnote">
          {policy?.config?.enabled ? "已开启自动注册" : "尚未开启自动注册"} ·{" "}
          {platform}
        </p>
        {(error || loadError) && <Notice>{error || loadError}</Notice>}
        <Button onClick={() => setOpen(true)}>配置自动注册</Button>
        <Modal
          open={open}
          busy={busy}
          onOpenChange={setOpen}
          title={`${platform} · 自动注册策略`}
          description="保存后按周期创建真实注册任务"
        >
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              if (submitLock.current) return;
              submitLock.current = true;
              setBusy(true);
              setError("");
              try {
                const result = await mutate(
                  "/api/registration-automation",
                  "PUT",
                  payload,
                  "自动注册策略已保存。",
                );
                setForm(result.config);
                setVersion(result.version);
                setDirty(false);
                setPreview(null);
                setOpen(false);
              } catch (e) {
                setError(e.message);
              } finally {
                submitLock.current = false;
                setBusy(false);
              }
            }}
          >
            <Switch
              label="启用此平台的自动注册"
              checked={form.enabled}
              onChange={(e) => update("enabled", e.target.checked)}
              description="按已保存的周期和目标创建批次。"
            />
            <div className="automation-fields">
              <label className="field gap">
                批次名称
                <input
                  value={form.name}
                  maxLength={80}
                  required
                  onChange={(e) => update("name", e.target.value)}
                />
              </label>
              <label className="field gap">
                触发方式
                <select
                  aria-label="触发方式"
                  value={form.mode}
                  onChange={(e) => update("mode", e.target.value)}
                >
                  <option value="interval">按间隔创建新批</option>
                  <option value="inventory">按账户库存补充</option>
                </select>
              </label>
              <label className="field gap">
                检查 / 创建间隔（分钟）
                <input
                  type="number"
                  min={1}
                  max={44640}
                  required
                  value={form.interval_minutes}
                  onChange={(e) =>
                    update("interval_minutes", Number(e.target.value))
                  }
                />
              </label>
              {form.mode === "inventory" && (
                <label className="field gap">
                  库存目标
                  <input
                    aria-label="库存目标"
                    type="number"
                    min={1}
                    max={1000000}
                    required
                    value={form.inventory_target}
                    onChange={(e) =>
                      update("inventory_target", Number(e.target.value))
                    }
                  />
                </label>
              )}
              <label className="field gap">
                {form.mode === "inventory"
                  ? "每批最多补充成功数"
                  : "每批目标成功数"}
                <input
                  type="number"
                  min={1}
                  max={10000}
                  required
                  value={form.count}
                  onChange={(e) => update("count", Number(e.target.value))}
                />
              </label>
              <label className="field gap">
                批次并行数
                <input
                  type="number"
                  min={1}
                  max={64}
                  required
                  value={form.parallel}
                  onChange={(e) => update("parallel", Number(e.target.value))}
                />
              </label>
              <label className="field gap">
                接码渠道
                <select
                  aria-label="自动注册接码渠道"
                  required={form.enabled}
                  value={form.sms_channel_id}
                  onChange={(e) => update("sms_channel_id", e.target.value)}
                >
                  <option value="">请选择</option>
                  {(channels?.channels || [])
                    .filter((c) => c.data.enabled !== false)
                    .map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                </select>
              </label>
              <div className="field gap">
                <label>注册固定出口</label>
                <Button onClick={() => update("proxy_id", null)}>
                  沿用平台代理规则{!form.proxy_id ? "（已选）" : ""}
                </Button>
                <ProxyPicker
                  value={form.proxy_id || ""}
                  onChange={(id) => update("proxy_id", id)}
                />
              </div>

              {[
                ["number_budget", "每批最大注册尝试数", 200000],
                ["max_consecutive_failures", "连续失败暂停阈值", 1000],
                ["max_consecutive_skips", "连续旧号码跳过暂停阈值", 10000],
              ].map(([key, label, max]) => (
                <label className="field gap" key={key}>
                  {label}（留空不限）
                  <input
                    type="number"
                    min={1}
                    max={max}
                    value={form[key] ?? ""}
                    onChange={(e) =>
                      update(
                        key,
                        e.target.value === "" ? null : Number(e.target.value),
                      )
                    }
                  />
                </label>
              ))}
            </div>
            <div className="row wrap topgap">
              <Button type="submit" disabled={busy || !records}>
                保存策略
              </Button>
              {dirty && policy && (
                <Button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setForm(policy.config);
                    setVersion(policy.version);
                    setDirty(false);
                    setPreview(null);
                    setError("");
                  }}
                >
                  重新加载已保存配置
                </Button>
              )}
              <Button
                type="button"
                disabled={busy || !records}
                onClick={async () => {
                  setBusy(true);
                  setError("");
                  try {
                    setPreview(
                      await api(
                        "/api/registration-automation/preview",
                        "POST",
                        payload,
                      ),
                    );
                  } catch (e) {
                    setError(e.message);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                只读预览
              </Button>
            </div>
          </form>
        </Modal>
        {state && (
          <p>
            {reasons[state.reason] || errors[state.reason] || state.reason}
            ；库存 {state.inventory}，注册在途 {state.pending}，未决{" "}
            {state.unresolved}。
            {state.would_create
              ? `下次满足时间条件时计划创建目标 ${state.count} 个成功账户的批次。`
              : "本次不创建新批。"}
          </p>
        )}
        <details className="secondary-choice">
          <summary>库存与执行规则</summary>
          <small>
            库存口径：已开启、具有有效或待核验凭证的真实账户；不按余额、临时冷却或模型限制扣减，因此不等同于当前可接单数。预览不会购号或发送短信。关闭策略只停止创建新批，当前批次需单独结束。
          </small>
        </details>
        {policy?.runtime?.next_run_at && (
          <p>
            下次检查：{tm(policy.runtime.next_run_at)}；最近检查：
            {tm(policy.runtime.last_checked_at)}；最近结果：
            {reasons[policy.runtime.last_reason] ||
              errors[policy.runtime.last_reason] ||
              policy.runtime.last_reason ||
              "尚未运行"}
          </p>
        )}
      </div>
    </Panel>
  );
}

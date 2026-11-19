import React, { useRef, useState } from "react";
import { useRecords } from "../use-records";
import { useApp, Panel, Table, Button, Notice, labels, tm } from "../ui";
import { errors } from "../api";

const materialNames = {
  refresh_token: "刷新凭证",
  pkce_code_verifier: "本次官方授权的PKCE材料",
  dpop_private_key: "本次官方授权的DPoP材料",
  original_phone: "原手机号",
  password_or_sms_channel: "密码或接码渠道",
};
const guidanceNames = {
  official_authorization_recreates_pkce_dpop:
    "请重新完成官方授权，生成相互匹配的 PKCE 与 DPoP 材料。",
  iam_requires_official_authorization: "此登录身份需要重新完成官方授权。",
  official_sso_required_balance_is_not_renewal:
    "需完成 WPS 官方 SSO；读取余额不代表凭证已续期。",
  official_authorization_or_original_number:
    "完成官方授权，或使用原手机号登录。",
  official_international_authorization: "请完成 WorkBuddy 国际版官方授权。",
};
const names = {
  healthy: "凭证有效，无需重登",
  renew_verify: "继续核验续期凭证",
  renew: "可自动续期",
  renew_wait: "等待续期重试",
  renew_pending: "续期处理中",
  login: "可自动重登",
  authorize: "需要官方授权",
  material: "缺登录材料",
  pending: "已有登录任务",
  tomorrow: "今日已安排",
  stopped: "自动恢复已暂停",
};
import {
  Modal,
  Pagination,
  accountName,
  accountIdentity,
} from "../components/Management";
import { queryString, useDebounced } from "../query";
export default function RecoveryMaterials() {
  const { mutate, data } = useApp();
  const [page, setPage] = useState(1),
    [q, setQ] = useState(""),
    [platform, setPlatform] = useState("");
  const search = useDebounced(q);
  const [records, error] = useRecords(
      `/api/recovery/accounts?${queryString({ page, page_size: 25, q: search, platform })}`,
    ),
    [channels] = useRecords("/api/sms/channels");
  const [editing, setEditing] = useState(null),
    [failure, setFailure] = useState(""),
    [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  async function run(task) {
    if (submitting.current || busy) return; // synchronous guard: same tick cannot double submit
    submitting.current = true;
    setBusy(true);
    setFailure("");
    try {
      await task();
    } catch (e) {
      setFailure(e.message);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <Panel name="重登方式与登录材料">
      <div className="pad">
        {(error || failure) && <Notice>{error || failure}</Notice>}
        <div className="toolbar">
          <input
            aria-label="搜索恢复账户"
            placeholder="账户原名 / UID"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setPage(1);
            }}
          />
          <select
            aria-label="恢复平台"
            value={platform}
            onChange={(e) => {
              setPlatform(e.target.value);
              setPage(1);
            }}
          >
            <option value="">全部平台</option>
            {data.platforms.map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
        </div>
        <Table
          heads={[
            "账户",
            "恢复阶段 / 最近登录",
            "处理方式",
            "已保存材料",
            "操作",
          ]}
          rows={(records?.accounts || []).map((a) => [
            <>
              {accountName(a)}
              <small>
                {a.platform} · {accountIdentity({ ...a, id: a.account_id })}
              </small>
            </>,
            <>
              {{
                not_started: "尚未开始恢复",
                verified: "余额已核验",
                checkin_queued: "等待签到",
                checkin_running: "签到执行中",
                balance_queued: "等待查询余额",
                balance_running: "余额核验中",
                verification_failed: "恢复核验失败",
                credential_pending: "新凭证待核验",
                prepared: "等待登录",
                sending: "提交登录中",
                waiting_code: "等待验证码",
                waiting_authorization: "待官方授权",
                waiting_identity: "待选择身份",
                waiting_captcha: "待人机验证",
                waiting_ticket: "等待登录票据",
                verifying: "核验登录结果",
                succeeded: "登录凭证已保存",
                uncertain: "登录结果待核对",
                failed: "登录失败",
              }[a.recovery_stage] ||
                a.recovery_stage ||
                "暂无恢复记录"}
              <small>
                {a.last_login_at ? tm(a.last_login_at) : "暂无登录时间"}
              </small>
              {a.last_login_error && (
                <small className="red">
                  {errors[a.last_login_error] ||
                    labels[a.last_login_error] ||
                    a.last_login_error}
                </small>
              )}
            </>,
            <>
              {names[a.action] || a.action}
              <small>
                {errors[a.reason] ||
                  labels[a.reason] ||
                  {
                    credential_valid: "按正常维护计划处理",
                    official_authorization_required:
                      "完成官方页面授权后继续回写",
                    refresh_material_present: "核验新凭证后激活",
                    automatic_login: "按原手机号及材料登录",
                    renew_backoff: "等待本次失败的重试间隔",
                  }[a.reason] ||
                  a.reason}
              </small>
            </>,
            <>
              {`${a.has_phone ? "有手机号" : "缺手机号"} · ${a.has_password ? "有密码" : "无密码"}`}
              <small>
                {Array.isArray(a.missing_material)
                  ? a.missing_material
                      .map((k) => materialNames[k] || k)
                      .join("、")
                  : a.missing_material}
              </small>
              <small>
                {Array.isArray(a.material_guidance)
                  ? a.material_guidance.join("；")
                  : guidanceNames[a.material_guidance] || a.material_guidance}
              </small>
            </>,
            <div className="row wrap">
              <Button onClick={() => setEditing(a)}>编辑材料</Button>
              {["renew", "renew_verify", "login", "authorize"].includes(
                a.action,
              ) && (
                <Button
                  disabled={busy}
                  onClick={() =>
                    run(() =>
                      mutate(
                        `/api/recovery/accounts/${a.account_id}/prepare`,
                        "POST",
                        {},
                        "恢复任务已保存。",
                      ),
                    )
                  }
                >
                  {busy ? "提交中…" : "安排恢复"}
                </Button>
              )}
              {a.session_id && (
                <a className="btn" href="#register">
                  查看登录任务
                </a>
              )}
            </div>,
          ])}
        />
        <Pagination
          page={page}
          pageSize={25}
          total={records?.total}
          loading={!records && !error}
          onPage={setPage}
        />
        <Modal
          open={!!editing}
          busy={busy}
          onOpenChange={(v) => !v && !busy && setEditing(null)}
          title="编辑登录材料"
          description="敏感材料加密保存，不回显原始内容"
        >
          {editing && (
            <form
              className="topgap"
              key={editing.account_id}
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                run(async () => {
                  await mutate(
                    `/api/recovery/accounts/${editing.account_id}/material`,
                    "PUT",
                    {
                      expected_version: editing.profile_version,
                      phone: f.get("phone") || null,
                      password: f.get("password") || null,
                      sms_channel_id: f.get("channel") || null,
                      clear_phone: f.has("clear_phone"),
                      clear_password: f.has("clear_password"),
                    },
                    "登录材料已加密保存。",
                  );
                  setEditing(null);
                });
              }}
            >
              {failure && <Notice>{failure}</Notice>}
              <h3>
                {accountName({ ...editing, id: editing.account_id })} · 登录材料
              </h3>
              <div className="grid">
                <label className="field gap">
                  原手机号
                  <input
                    name="phone"
                    type="password"
                    autoComplete="off"
                    placeholder="留空保留已保存号码"
                    maxLength={30}
                  />
                </label>
                <label className="field gap">
                  登录密码
                  <input
                    name="password"
                    type="password"
                    autoComplete="new-password"
                    placeholder="留空保留已保存密码"
                  />
                </label>
                <label className="field gap">
                  接码渠道
                  <select
                    name="channel"
                    defaultValue={editing.sms_channel_id || ""}
                  >
                    <option value="">不使用接码渠道</option>
                    {(channels?.channels || []).map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="row wrap gap">
                <label>
                  <input type="checkbox" name="clear_phone" />
                  停止使用旧号码
                </label>
                <label>
                  <input type="checkbox" name="clear_password" />
                  停止使用旧密码
                </label>
              </div>
              <div className="row gap">
                <Button type="submit" disabled={busy}>
                  保存材料
                </Button>
                <Button onClick={() => setEditing(null)}>关闭</Button>
              </div>
            </form>
          )}
        </Modal>
      </div>
    </Panel>
  );
}

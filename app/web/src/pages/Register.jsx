import RegistrationAutomation from "./RegistrationAutomation";
import React, { useEffect, useRef, useState } from "react";
import BrowserView from "./RegistrationBrowser";
import ProxyPicker from "./ProxyPicker";
import SmsChannels from "./SmsChannels";
import { Plus } from "lucide-react";
import {
  useApp,
  Badge,
  Button,
  Head,
  Panel,
  Table,
  Notice,
  Line,
  tm,
} from "../ui";
import { api, errors } from "../api";
import { useRecords } from "../use-records";
import {
  Modal,
  AccountPicker,
  Pagination,
  accountName,
} from "../components/Management";
import { queryString } from "../query";
const modes = {
  browser: "官方浏览器登录",
  sms: "短信登录 / 注册",
  password: "密码登录",
  register: "短信注册",
  official: "官方网页登录",
  oauth_qq: "QQ 授权",
  oauth_wechat: "微信授权",
  verified_import: "官方登录后导入核验",
};
const states = {
  prepared: "待开始",
  sending: "执行中",
  waiting_code: "等待短信验证码",
  waiting_authorization: "等待官方授权",
  waiting_identity: "选择账户身份",
  waiting_captcha: "等待人机验证",
  waiting_ticket: "等待授权凭证",
  verifying: "核对登录结果",
  succeeded: "已入库",
  failed: "失败",
  uncertain: "结果待核实",
  cancelled: "已结束",
};
function LoginAction({ record, enabled }) {
  const { mutate } = useApp();
  const [value, setValue] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [detail, setDetail] = useState(null),
    [showBrowser, setShowBrowser] = useState(false);
  const status = record.data.status;
  const actionLock = useRef(false);
  useEffect(() => {
    if (["succeeded", "cancelled"].includes(status)) setShowBrowser(false);
  }, [status]);
  async function act(action, body = {}) {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy(true);
    setError("");
    try {
      await mutate(
        `/api/registration/${record.id}/${action}`,
        "POST",
        body,
        "登录记录已更新。",
      );
      setValue("");
      setDetail(null);
    } catch (e) {
      setError(e.message);
    } finally {
      actionLock.current = false;
      setBusy(false);
    }
  }
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button onClick={() => setOpen(true)}>处理登录任务</Button>
      <Modal
        open={open}
        busy={busy}
        onOpenChange={setOpen}
        title={`${record.data.platform} · ${record.name}`}
        description="查看当前步骤并继续真实登录流程"
      >
        <div className="registration-action">
          {status === "prepared" && (
            <Button disabled={busy || !enabled} onClick={() => act("start")}>
              开始注册 / 登录
            </Button>
          )}
          {[
            "waiting_authorization",
            "waiting_identity",
            "waiting_captcha",
            "waiting_ticket",
            "uncertain",
            "failed",
          ].includes(status) && (
            <Button
              disabled={busy}
              onClick={async () => {
                try {
                  setDetail(
                    await api(`/api/registration/${record.id}/interaction`),
                  );
                } catch (e) {
                  setError(e.message);
                }
              }}
            >
              查看当前步骤
            </Button>
          )}
          {detail?.authorization_url &&
            (record.data.platform === "CodeArts" ? (
              <Button
                disabled={busy || !enabled}
                onClick={async () => {
                  setBusy(true);
                  setError("");
                  const popup = window.open("about:blank", "_blank");
                  if (popup) popup.opener = null;
                  try {
                    const r = await api(
                      `/api/registration/${record.id}/external-link`,
                      "POST",
                      {},
                    );
                    if (popup) popup.location.href = r.url;
                    else
                      setError("浏览器未打开授权窗口，请使用面板内登录窗口。");
                  } catch (e) {
                    popup?.close();
                    setError(e.message);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                打开官方授权页
              </Button>
            ) : (
              <a
                className="btn"
                href={detail.authorization_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                打开官方授权页
              </a>
            ))}
          {detail?.identities?.length > 0 && (
            <select
              aria-label={`${record.name} 账户身份`}
              value={value}
              onChange={(e) => setValue(e.target.value)}
            >
              <option value="">请选择</option>
              {detail.identities.map((x) => (
                <option
                  key={x.selection_key || x.id + ":" + x.enterprise_id}
                  value={x.selection_key || x.id}
                >
                  {x.id}
                  {x.enterprise_id ? ` · 企业 ${x.enterprise_id}` : " · 个人"}
                </option>
              ))}
            </select>
          )}
          {(status === "waiting_code" ||
            status === "waiting_captcha" ||
            (status === "waiting_authorization" &&
              ["Marvis", "小浣熊", "CodeArts"].includes(
                record.data.platform,
              ))) && (
            <>
              <input
                type="password"
                autoComplete="off"
                aria-label={`${record.name} 验证材料`}
                placeholder={
                  status === "waiting_captcha"
                    ? "人机验证结果 JSON"
                    : status === "waiting_code"
                      ? "短信验证码"
                      : "完整授权回调地址"
                }
                value={value}
                onChange={(e) => setValue(e.target.value)}
              />
              <Button
                disabled={busy || !enabled || !value}
                onClick={() => act("code", { code: value })}
              >
                提交核验
              </Button>
              {status === "waiting_code" &&
                record.data.expires_at * 1000 < Date.now() && (
                  <Button disabled={busy} onClick={() => act("no-code")}>
                    登记未收到验证码
                  </Button>
                )}
            </>
          )}
          {record.data.platform.startsWith("WorkBuddy") &&
            [
              "waiting_authorization",
              "waiting_identity",
              "waiting_ticket",
              "uncertain",
            ].includes(status) && (
              <Button
                disabled={
                  busy || !enabled || (status === "waiting_identity" && !value)
                }
                onClick={() =>
                  act("continue", { selected_identity: value || null })
                }
              >
                继续核对结果
              </Button>
            )}
          {status === "waiting_authorization" && (
            <Button
              disabled={!enabled}
              onClick={() => setShowBrowser(!showBrowser)}
            >
              在面板内完成官方登录
            </Button>
          )}
          {showBrowser && (
            <BrowserView
              sessionId={record.id}
              onClose={() => setShowBrowser(false)}
            />
          )}
          {detail?.has_issued_result && (
            <Button onClick={() => act("finalize")}>
              继续保存已收到的凭证
            </Button>
          )}
          {(detail?.matching_accounts || []).map((a) => (
            <Button
              key={a.id}
              disabled={busy}
              onClick={() =>
                act("attach", { account_id: a.id, expected_version: a.version })
              }
            >
              更新已有账户 {accountName(a)} 的凭证
            </Button>
          ))}
          {detail?.can_verify_candidate && !detail.has_issued_result && (
            <Button
              disabled={busy || !enabled}
              onClick={() =>
                act("continue", { selected_identity: value || null })
              }
            >
              核验已收到的凭证
            </Button>
          )}
          {[
            "prepared",
            "waiting_code",
            "waiting_authorization",
            "waiting_identity",
            "waiting_captcha",
            "waiting_ticket",
            "failed",
          ].includes(status) && (
            <Button disabled={busy} onClick={() => act("cancel")}>
              结束此任务
            </Button>
          )}
          {status === "failed" &&
            [
              "invalid_credentials",
              "identity_mismatch",
              "invalid_credential_format",
              "wps_sso_incomplete",
            ].includes(record.data.error) && (
              <Button disabled={busy} onClick={() => act("close-rejected")}>
                归档已拒绝材料并结束任务
              </Button>
            )}
          {status === "uncertain" && (
            <small>
              结果尚未确认，保留原记录和材料；不会自动重发短信或增加失败次数。
            </small>
          )}
          {status === "waiting_code" && record.data.sms_lease_id && (
            <Button disabled={busy || !enabled} onClick={() => act("poll-sms")}>
              读取接码平台
            </Button>
          )}
          {detail?.step && <small>当前步骤：{detail.step}</small>}
          {record.data.browser_stage && (
            <small>
              浏览器进度：
              {{
                waiting_captcha: "等待人工完成验证",
                ready_to_send: "准备发送验证码",
                sms_send_attempted: "已提交发送，等待回读",
                sms_send_rejected: "官方拒绝发送验证码",
                sms_send_unconfirmed: "发码结果未确认",
                waiting_code: "等待短信验证码",
                sms_verify_attempted: "验证码已提交",
                waiting_authorization: "等待官方授权",
                waiting_official_page: "等待官方页面",
                waiting_page_transition: "等待页面跳转",
                ready_phone_check: "待校验手机号",
                phone_check_pending: "官方校验手机号中",
                waiting_official_review: "等待处理官方风险或号码提示",
                registration_confirmed: "注册已确认，待授权",
                waiting_codearts_oauth: "等待 CodeArts 官方授权",
              }[record.data.browser_stage] || record.data.browser_stage}
            </small>
          )}
          {record.data.browser_error && (
            <small className="red">
              {errors[record.data.browser_error] || record.data.browser_error}
              {record.data.browser_sms_http_status
                ? `（HTTP ${record.data.browser_sms_http_status}）`
                : ""}
            </small>
          )}
          {status === "waiting_captcha" && (
            <small>先完成真实人机验证；未通过前不会发送短信。</small>
          )}
          {error && (
            <small role="alert" className="red">
              {error}
            </small>
          )}
        </div>
      </Modal>
    </>
  );
}
export default function Register() {
  const { data, setDrawer, mutate } = useApp();
  const submitLock = useRef(false);
  const [platform, setPlatform] = useState(() => {
      const selected = sessionStorage.getItem("registration-platform");
      sessionStorage.removeItem("registration-platform");
      return data.platforms.includes(selected) ? selected : data.platforms[0];
    }),
    [mode, setMode] = useState("sms");
  const [section, setSection] = useState("records");
  const [formOpen, setFormOpen] = useState(false),
    [intent, setIntent] = useState("new"),
    [account, setAccount] = useState(""),
    [page, setPage] = useState(1),
    [historyStatus, setHistoryStatus] = useState("");
  const [records, loadError] = useRecords(
    `/api/registration?${queryString({ platform, page, page_size: 25, status: historyStatus })}`,
  );
  useEffect(() => {
    setPage(1);
    setAccount("");
    setFormOpen(false);
  }, [platform]);
  const [channels] = useRecords("/api/sms/channels");
  const [proxy, setProxy] = useState("");
  const [channel, setChannel] = useState("");
  const [preflight, setPreflight] = useState(null);
  useEffect(() => setPreflight(null), [platform, mode, channel]);
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const available = records?.supported?.[platform] || [];
  const selected = available.includes(mode) ? mode : available[0];
  const manual = selected === "verified_import";
  const smsMode = ["sms", "register"].includes(selected);
  const browserSms =
    ["WPS 灵犀", "小浣熊", "CodeArts"].includes(platform) &&
    ["browser", "official"].includes(selected);
  return (
    <>
      <Head
        name="注册与登录"
        desc="分平台处理新账户和已有账户，身份核验后保存凭证。"
      >
        <Button
          onClick={() => {
            setIntent("relogin");
            setAccount("");
            setFormOpen(true);
          }}
        >
          已有账户重登
        </Button>
        <Button
          variant="primary"
          onClick={() => {
            setIntent("new");
            setAccount("");
            setFormOpen(true);
          }}
        >
          添加新账户
        </Button>
      </Head>
      <div className="platformtabs">
        {data.platforms.map((p) => (
          <button
            key={p}
            className={platform === p ? "active" : ""}
            onClick={() => {
              setPlatform(p);
              setError("");
            }}
          >
            {p}
          </button>
        ))}
      </div>
      <Modal
        open={formOpen}
        busy={busy}
        onOpenChange={setFormOpen}
        title={`${platform} · ${intent === "new" ? "新账户注册 / 登录" : "已有账户重登"}`}
        description="材料只提交到当前平台的原生登录流程"
      >
        <div>
          {(error || loadError) && <Notice>{error || loadError}</Notice>}
          <Notice>
            {records?.network_enabled
              ? "外部执行已开启，提交后按所选方式启动。"
              : "外部执行已停用（UNIFIED_ENABLE_UPSTREAM=0）：可保存任务，但不会发起注册、短信或授权请求。"}
          </Notice>
          {manual && (
            <Notice>
              当前支持官方客户端登录后的凭证接入与身份核验，自动短信注册仍待接齐。WPS
              必须提供完成 SSO 的 wps_sid，不能使用 ssid 代替。
            </Notice>
          )}
          {available.length > 0 && (
            <form
              key={platform}
              onChange={() => setPreflight(null)}
              onSubmit={async (e) => {
                e.preventDefault();
                if (submitLock.current) return;
                submitLock.current = true;
                const form = e.currentTarget,
                  f = new FormData(form);
                setBusy(true);
                setError("");
                try {
                  const r = await mutate(
                    "/api/registration",
                    "POST",
                    {
                      platform,
                      label: f.get("label"),
                      mode: selected,
                      phone: f.get("phone") || null,
                      password: f.get("password") || null,
                      account_id: f.get("account_id") || null,
                      credential: f.get("credential") || null,
                      identity_field: f.get("identity_field") || null,
                      sms_channel_id:
                        smsMode || browserSms ? channel || null : null,
                      auto_run: f.has("auto_run"),
                      proxy_id: f.get("proxy") || null,
                    },
                    "任务已保存。",
                  );
                  if (records.network_enabled)
                    await mutate(
                      `/api/registration/${r.id}/start`,
                      "POST",
                      {},
                      "注册 / 登录进度已更新。",
                    );
                  form.reset();
                  setFormOpen(false);
                } catch (e) {
                  setError(e.message);
                } finally {
                  submitLock.current = false;
                  setBusy(false);
                }
              }}
            >
              <div className="field gap">
                <label htmlFor="login-label">任务名称</label>
                <input id="login-label" name="label" required maxLength={100} />
              </div>
              <div className="field gap">
                <label htmlFor="login-mode">方式</label>
                <div className="segmented">
                  {available.map((m) => (
                    <button
                      type="button"
                      key={m}
                      className={selected === m ? "active" : ""}
                      aria-pressed={selected === m}
                      onClick={() => setMode(m)}
                    >
                      {modes[m] || m}
                    </button>
                  ))}
                </div>
              </div>
              {intent === "relogin" && (
                <div className="field gap">
                  <label>重登账户</label>
                  <AccountPicker
                    platform={platform}
                    value={account}
                    onChange={setAccount}
                    required
                    label="选择需要重登的账户"
                  />
                  <small>
                    每账户每天一次，连续三次明确未收到验证码后停止。
                  </small>
                </div>
              )}
              <input
                type="hidden"
                name="account_id"
                value={intent === "relogin" ? account : ""}
              />

              {(["sms", "password", "register"].includes(selected) ||
                manual ||
                browserSms) && (
                <div className="field gap">
                  <label htmlFor="login-phone">
                    手机号{manual || browserSms ? "（用于后续关联，可选）" : ""}
                  </label>
                  <input
                    id="login-phone"
                    name="phone"
                    type="password"
                    required={!manual && !browserSms && !channel}
                    autoComplete="off"
                    minLength={5}
                    maxLength={30}
                  />
                </div>
              )}
              {["password", "register"].includes(selected) && (
                <div className="field gap">
                  <label htmlFor="login-password">密码</label>
                  <input
                    id="login-password"
                    name="password"
                    type="password"
                    required={selected === "password"}
                    autoComplete="new-password"
                  />
                </div>
              )}
              {manual && (
                <div className="field gap">
                  <label htmlFor="login-credential">
                    登录凭证 JSON（加密保存）
                  </label>
                  <textarea
                    id="login-credential"
                    className="secretfield"
                    name="credential"
                    required
                    autoComplete="off"
                    maxLength={32768}
                  />
                </div>
              )}
              {(smsMode || browserSms) && (
                <div className="field gap">
                  <label htmlFor="sms-channel">接码平台</label>
                  <select
                    id="sms-channel"
                    value={channel}
                    onChange={(e) => setChannel(e.target.value)}
                  >
                    <option value="">自行填写手机号与验证码</option>
                    {(channels?.channels || [])
                      .filter((c) => c.data.enabled !== false)
                      .map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name}
                        </option>
                      ))}
                  </select>
                  <small>
                    新注册未填号码时才申请新号；重登使用账户原号码。
                    {browserSms
                      ? "在登录窗口点击对应输入框，可填入已保存号码或读取短信；发送短信与人机校验在官方页面完成。"
                      : ""}
                  </small>
                  {(smsMode || browserSms) && (
                    <label className="row">
                      <input type="checkbox" name="auto_run" />{" "}
                      自动推进登录并读取短信（遇到人机验证等待人工完成）
                    </label>
                  )}
                </div>
              )}
              {intent === "new" && (
                <div className="field gap">
                  <label>新账户注册出口</label>
                  <input type="hidden" name="proxy" value={proxy} />
                  <Button onClick={() => setProxy("")}>
                    沿用平台默认出口{!proxy ? "（已选）" : ""}
                  </Button>
                  <ProxyPicker value={proxy} onChange={setProxy} />
                  <small>会话开始后固定出口，已有账户使用已绑定出口。</small>
                </div>
              )}

              <div className="registration-submit">
                <Button
                  type="button"
                  disabled={busy}
                  onClick={async (e) => {
                    const f = new FormData(e.currentTarget.closest("form"));
                    setBusy(true);
                    setError("");
                    try {
                      setPreflight(
                        await api("/api/registration/preflight", "POST", {
                          platform,
                          mode: selected,
                          sms_channel_id:
                            smsMode || browserSms ? channel || null : null,
                          proxy_id: f.get("proxy") || null,
                          account_id: f.get("account_id") || null,
                        }),
                      );
                    } catch (e) {
                      setError(e.message);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  检查注册配置
                </Button>
                <Button
                  type="submit"
                  variant="primary"
                  disabled={busy || (intent === "relogin" && !account)}
                >
                  {busy
                    ? "正在提交…"
                    : intent === "relogin"
                      ? "开始重登"
                      : records?.network_enabled
                        ? "开始注册 / 登录"
                        : "保存注册任务"}
                </Button>
              </div>
              {preflight && (
                <div className="topgap">
                  <p className="quietnote">
                    仅检查本地配置，不取号、不发短信；通过不代表号源、出口或官方验证一定可用。
                  </p>
                  <Table
                    heads={["检查结果", "说明"]}
                    rows={preflight.checks.map((check) => [
                      {
                        pass: "已配置",
                        fail: "待补齐",
                        warning: "需留意",
                        unverified: "未验证",
                        not_required: "不需要",
                      }[check.status] || check.status,
                      check.label,
                    ])}
                  />
                </div>
              )}
            </form>
          )}
          <div className="registration-secondary">
            <p>已有账户、暂时没有登录材料？</p>
            <Button
              icon={Plus}
              onClick={() =>
                setDrawer({ type: "account-create", platform, mode: "record" })
              }
            >
              仅手动建立账户记录
            </Button>
          </div>
          <Line
            name="成功后自动衔接"
            desc="身份核验 → 凭证落库 → 当日签到（支持的平台）→ 查询余额 → 新手任务（支持的新注册账户）→ 再查余额 → 调度判断"
          />
          <Line
            name="注册成功与可接单分开"
            desc="注册回执不代表模型权限恢复；权限异常仍需模型正常回答后恢复。"
          />
        </div>
      </Modal>
      <div
        className="segmented section-tabs"
        role="tablist"
        aria-label="注册管理内容"
      >
        {[
          ["records", "注册记录"],
          ["batches", "接码与批次"],
          ["automation", "自动注册"],
        ].map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={section === key}
            className={section === key ? "active" : ""}
            onClick={() => setSection(key)}
          >
            {label}
          </button>
        ))}
      </div>
      {section === "automation" && (
        <RegistrationAutomation key={platform} platform={platform} />
      )}
      {section === "batches" && (
        <SmsChannels key={platform} platform={platform} />
      )}
      {section === "records" && (
        <>
          <div className="toolbar">
            <select
              aria-label="注册记录状态"
              value={historyStatus}
              onChange={(e) => {
                setHistoryStatus(e.target.value);
                setPage(1);
              }}
            >
              <option value="">全部状态</option>
              {Object.entries(states).map(([v, n]) => (
                <option value={v} key={v}>
                  {n}
                </option>
              ))}
            </select>
          </div>
          <Panel name="注册与登录记录">
            <Table
              loading={!records && !loadError}
              heads={["任务", "状态", "最后更新", "下一步"]}
              rows={(records?.sessions || []).map((r) => [
                r.name,
                <>
                  {states[r.data.status] || r.data.status}
                  {r.data.error && (
                    <small>{errors[r.data.error] || r.data.error}</small>
                  )}
                </>,
                tm(r.updated_at),
                <LoginAction
                  key={r.id}
                  record={r}
                  enabled={records?.network_enabled}
                />,
              ])}
            />
            <Pagination
              page={page}
              pageSize={25}
              total={records?.total}
              loading={!records && !loadError}
              onPage={setPage}
            />
          </Panel>
        </>
      )}
    </>
  );
}

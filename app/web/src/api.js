export const errors = {
  invalid_admin_credentials: "管理员账号或密码不正确。",
  invalid_credentials_format: "请输入管理员账号和密码。",
  proxy_in_use: "节点仍被账户、代理池或任务引用，请先调整关联用途。",
  proxy_source_managed: "订阅节点的连接材料由来源管理，请编辑订阅。",
  subscription_in_use: "订阅仍被动态代理池引用，请先调整代理池。",
  subscription_disabled: "此订阅来源已停用，请先启用来源。",
  proxy_unavailable: "节点未就绪或已停用，请选择可用节点。",
  single_proxy_required: "请填写一个有效节点链接。",
  proxy_batch_not_found: "检测批次已不存在，请刷新列表。",
  account_concurrency_busy: "合格账户的并发暂时已满，请稍后重试。",
  onboarding_uncertain:
    "领取结果尚不确定。支持回读的平台可先只读核对，不会重复提交。",
  onboarding_incomplete: "仍有新手任务未确认完成，余额会单独查询。",
  onboarding_ledger_required: "缺少领取回执账本，本次没有提交领取。",
  unconfirmed_onboarding: "上游没有给出明确领取结果，原记录已保留。",
  invitation_busy: "该账户的邀请码操作正在执行，请等待回读。",
  invitation_requires_readback:
    "已有绑定提交记录，请先只回读结果，不重复绑定。",
  stale_invitation_receipt: "回读已由另一个执行器接手，请刷新记录。",
  import_plain_json_required:
    "请选择明文 JSON / JSONL backup 文件，不接受加密客户端文件。",
  import_platform_required: "记录缺少明确的平台；WorkBuddy 请区分国内和国际。",
  import_platform_mismatch: "文件中的平台与所选平台不一致。",
  import_identity_required:
    "缺少明确的平台账户 ID，请补齐，不能用凭证尾号代替。",
  import_full_phone_required:
    "手机号被打码或格式不完整，请提供明确关联的完整号码。",
  import_count_invalid: "每批支持 1 至 10000 条记录。",
  import_credential_too_large: "单条凭证超过 64 KB，请检查是否误选了其他文件。",
  import_official_login_required: "该平台迁入凭证需要通过官方登录核验身份。",
  import_verification_busy: "该条凭证正在核验，请等待结果。",
  oauth_launch_required: "请从注册任务的官方授权按钮重新打开当前授权页面。",
  missing_relogin_material:
    "缺少原手机号、密码或有效接码渠道，请补齐登录材料。",

  network_unavailable: "无法连接服务，当前显示的是上次收到的数据。",
  login_required: "请先登录管理后台。",
  login_rate_limited: "登录尝试过多，请一分钟后重试。",
  invalid_admin_token: "管理令牌不正确。",
  already_checked_in: "这个账户今天已经签到成功，不会重复安排。",
  credential_version_conflict: "凭证已被更新，请刷新后再提交。",
  relogin_attempts_exhausted: "该账户连续三次未收到验证码，需要人工处理。",
  relogin_already_scheduled_today: "该账户今天已安排过重登。",
  relogin_result_unresolved: "上一笔重登结果尚不确定，请先核对回执。",
  adapter_unavailable: "该平台真实适配器尚未接入，不会执行外部操作。",
  job_not_safely_retryable: "此任务不能直接重试，需要先核对执行结果。",
  invalid_input: "请检查必填项和输入格式。",
  no_available_account: "当前分组没有可用的空闲账户。",
  model_not_found: "模型不在此密钥的允许范围内。",
  network_execution_disabled: "外部执行已停用（UNIFIED_ENABLE_UPSTREAM=0）。",
  registration_adapter_unavailable: "此平台的注册协议还未接齐。",
  registration_state_conflict: "这笔操作已执行或状态改变，请刷新后查看。",
  registration_execution_busy: "这笔任务正在执行，结果会自动更新。",
  registration_execution_lost: "执行连接已中断，已保留记录，请核对当前步骤。",
  account_maintenance_busy: "该账户正在维护，完成后再继续登录。",
  maintenance_capacity_busy: "共享执行槽正在使用，自动任务会继续排队。",
  maintenance_priority_pending: "正在优先处理签到和余额，注册任务已保留。",
  registration_result_unresolved:
    "已取得材料或结果尚未确认，请先继续核验，不能直接丢弃。",
  registration_result_not_available:
    "还没有可保存的登录回执，请先完成授权核验。",
  verification_expired:
    "授权等待已过期，原记录已保留；收到的凭证仍可继续核验。",
  batch_already_cancelled: "该批次已结束，不会重新启动。",
  sms_channel_disabled: "该接码渠道已停用新任务，已有号码仍可收取短信。",
  sms_channel_has_active_numbers:
    "渠道还有未收尾的号码。可更新 Token；更换项目或接口需先完成这些号码。",
  sms_acquisition_requires_review:
    "申请号码的回执不完整，请先核对接码平台订单，不能重复申请。",
  sms_lease_not_active: "该任务没有可以继续收码的号码记录。",
  sms_channel_credentials_invalid: "接码渠道的登录材料已失效，请更新 Token。",
  sms_provider_rejected:
    "接码平台拒绝了请求，请检查渠道余额、项目配置和订单状态。",
  sms_send_rejected:
    "官方平台拒绝发送验证码，本次任务已结束；不会重复发码，也不计入未收到短信次数。",
  sms_send_unconfirmed:
    "尚未确认官方发码成功，已保留原任务等待核实；不会重复发码或计入未收到短信次数。",
  missing_material: "缺少完成此步骤所需的登录材料，请补齐后再继续。",
  relogin_phone_required: "重登需要该账户原来的手机号，不能另外申请新号码。",
  no_sms: "验证码等待结束，未收到可用验证码。",
  verification_code_incorrect:
    "验证码不正确，请输入新验证码；不会反复提交同一条。",
  invalid_captcha_result: "人机验证尚未通过，请在官方页面完成验证。",
  oauth_state_mismatch:
    "授权回调与当前登录任务不匹配，请使用本次任务的授权页面。",
  invalid_oauth_callback: "授权回调不完整，请提交本次登录的完整回调地址。",
  issued_credential_pending: "已经收到凭证，请继续核验和保存，无需重新登录。",
  executor_lost: "执行连接中断，原记录保留，等待核对结果。",
  account_exists_use_relogin:
    "这个平台身份已在账户库中，可查看当前步骤并更新已有账户凭证。",
  identity_mismatch: "返回的账户身份不匹配，凭证未覆盖原账户。",
  wps_sso_incomplete: "WPS 登录尚未完成跳转，请在同一窗口进入灵犀后再核验。",
  invalid_balance: "平台没有返回可确认的余额，保留待核验状态。",
  network: "上游连接中断，结果尚未确认。",
  upstream_timeout: "上游响应超时，结果尚未确认。",
  browser_runtime_unavailable:
    "登录浏览器组件尚未安装，请先安装项目所需的 Chromium。",
  browser_page_closed: "官方登录页面已关闭，请关闭窗口后重新打开。",
  checkin_inactive: "平台当前没有可领取的签到活动。",
  resource_version_conflict: "记录已更新，请刷新后再保存。",
  invalid_proxy_url: "请填写带端口的 HTTP、HTTPS 或 SOCKS5 代理地址。",
  maintenance_proxy_route_invalid:
    "维护任务出口配置损坏，请重新保存该任务的出口。",
  unsupported_maintenance_task:
    "该任务不能单独设置出口；模型恢复检测沿用转发出口，注册和重登沿用会话出口。",
  duplicate_model_route: "模型映射或账户组ID重复，请检查后保存。",
  model_route_group_not_found: "映射引用的账户组不存在，请重新选择。",
  model_route_account_platform_mismatch: "账户组包含其他平台或已不存在的账户。",
  sms_login_requires_single_token_channel:
    "账号密码登录仅支持豪猪单Token渠道，请另建单Token渠道。",
  sms_login_token_missing: "供应商未返回可用Token，配置未保存。",
  invalid_time_window: "时间范围无效，请检查开始和结束时间。",
  api_key_revoked: "该密钥已撤销，无法重新启用。",
  no_active_block: "当前没有可解除的限制，请刷新账户详情。",
  invalid_api_key: "密钥无效或已撤销。",
};

export async function api(path, method = "GET", body, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      signal: options.signal,
      headers: {
        "Content-Type": "application/json",
        "X-Console-Client": "unified-local",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new Error(errors.network_unavailable);
  }
  let data;
  try {
    data = await response.json();
  } catch {
    if (response.status === 401) {
      dispatchEvent(new Event("console-session-expired"));
      const error = new Error(errors.login_required);
      error.code = "login_required";
      throw error;
    }
    throw new Error(
      `服务返回了无法读取的响应（HTTP ${response.status}），请重试。`,
    );
  }
  if (!response.ok) {
    const e = new Error(
      errors[data.error?.code] ||
        `操作失败（${data.error?.code || response.status}）`,
    );
    e.code = data.error?.code;
    if (e.code === "login_required")
      dispatchEvent(new Event("console-session-expired"));
    throw e;
  }
  return data;
}

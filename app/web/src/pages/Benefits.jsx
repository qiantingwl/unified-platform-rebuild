import React, { useState } from "react";
import { useApp, Button, Notice, Table, tm, labels } from "../ui";
import { AccountPicker, Modal } from "../components/Management";
import { useQuery } from "../query";
const taskName = (key) =>
  key?.startsWith("redeem:")
    ? `${key.slice(7).replace("d", "天")}连续奖励`
    : key?.startsWith("makeup:")
      ? `${key.slice(7)} 补签`
      : taskNames[key] || key;
const taskNames = {
  streak: "连续奖励",
  travel: "旅行权益",
  travel_depart: "出发",
  travel_claim: "领取旅行奖励",
};
const states = {
  confirmed: "官方已确认",
  failed: "失败",
  uncertain: "结果待核对",
  submitting: "正在提交",
  submitted: "已提交，待确认",
  material_required: "缺少材料",
  unavailable: "当前不可用",
  daily_limit: "今日已达上限",
  traveling: "旅行中",
  not_started: "尚未查询",
  available: "已有回执",
  already_claimed: "已领取",
  claimable: "可领取",
  unlocked: "已解锁",
  locked: "未解锁",
  claimed: "已领取",
  unknown: "未确定",
  idle: "尚未出发",
  arrived: "已抵达",
  verified: "已核实",
};
export default function Benefits() {
  const { data, mutate } = useApp();
  const [open, setOpen] = useState(false),
    [account, setAccount] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const query = useQuery(
      open && account ? `/api/accounts/${account}/benefits` : null,
      data.revision,
    ),
    r = query.result;
  const inventory = useQuery(
    open && account ? `/api/accounts/${account}/benefits/status` : null,
    data.revision,
  );
  return (
    <>
      <Button onClick={() => setOpen(true)}>平台权益</Button>
      <Modal
        open={open}
        onOpenChange={setOpen}
        title="平台权益任务"
        description="适用 WorkBuddy 国内个人账户；领取、官方回执与余额更新分别记录"
      >
        <AccountPicker
          platform="WorkBuddy 国内"
          capability="benefits"
          value={account}
          onChange={setAccount}
          required
          label="选择权益账户"
        />
        {(error || query.error || inventory.error) && (
          <Notice>{error || query.error || inventory.error}</Notice>
        )}
        {account && (
          <>
            <h3 className="topgap">只读实时盘点</h3>
            <p className="quietnote">
              仅回读官方状态，不领取、不出发；未知不代表可领取。最近观察{" "}
              {tm(inventory.result?.observed_at)} ·{" "}
              {inventory.result?.credential_current === false
                ? "旧凭证回执"
                : inventory.result?.credential_version == null
                  ? "尚无凭证回执"
                  : `凭证v${inventory.result.credential_version}`}
            </p>
            <Table
              heads={["权益", "盘点状态", "是否适用", "可用卡片 / 说明"]}
              rows={(inventory.result?.tasks || []).map((t) => [
                taskName(t.key),
                {
                  not_started: "尚未盘点",
                  verified: "已核实",
                  unknown: "未确定",
                  failed: "失败",
                }[t.status] ||
                  states[t.status] ||
                  t.status,
                t.eligible === true
                  ? "适用"
                  : t.eligible === false
                    ? "不适用"
                    : "未确定",
                `${t.cards == null ? "未提供卡片数" : typeof t.cards === "number" ? t.cards : "已记录卡片"} · ${labels[t.error || t.reason] || t.error || t.reason || "—"}`,
              ])}
            />
            <Button
              disabled={busy || !data.runtime.upstream_enabled}
              onClick={async () => {
                setBusy(true);
                setError("");
                try {
                  await mutate(
                    `/api/accounts/${account}/benefits/inspect`,
                    "POST",
                    {},
                    "已安排只读盘点，等待官方状态回执。",
                  );
                  inventory.reload();
                } catch (e) {
                  setError(e.message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              只读实时盘点
            </Button>
            <h3 className="topgap">领取与处理回执</h3>
          </>
        )}

        {r && (
          <>
            <p className="quietnote">
              最近回执：{tm(r.observed_at)}；
              {r.credential_version == null
                ? "尚无凭证回执"
                : r.credential_current === false
                  ? "来自旧凭证，不能表示当前状态"
                  : "对应凭证 v" + r.credential_version}
              ；{r.settled ? "本次已核对完成" : "本次仍有未确认项"}。
            </p>
            <Table
              heads={["权益任务", "状态", "本次变更", "说明"]}
              rows={(r.tasks || []).map((t) => [
                taskName(t.key),
                states[t.status] || labels[t.status] || t.status,
                t.changed ? "官方确认发生变更" : "未确认变更",
                labels[t.error || t.reason] || t.error || t.reason || "—",
              ])}
            />
            {r.error_code && (
              <Notice>{labels[r.error_code] || r.error_code}</Notice>
            )}
            <p className="quietnote">
              官方确认变更 {r.claimed ?? 0} 项。
              {r.needs_balance
                ? "已要求另行查询余额，以余额回执为准。"
                : "权益回执不直接改写余额。"}
            </p>
          </>
        )}
        <Button
          variant="primary"
          disabled={!account || busy || !data.runtime.upstream_enabled}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              await mutate(
                `/api/accounts/${account}/jobs`,
                "POST",
                { kind: "benefits" },
                "平台权益任务已排队，等待官方回执。",
              );
              query.reload();
            } catch (e) {
              setError(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "正在安排…" : "核验并处理适用权益"}
        </Button>
      </Modal>
    </>
  );
}

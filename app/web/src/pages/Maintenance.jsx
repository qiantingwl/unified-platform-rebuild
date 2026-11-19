import React, { useRef, useState } from "react";
import { Play } from "lucide-react";
import Benefits from "./Benefits";
import JobHistory from "./JobHistory";
import { useRecords } from "../use-records";

import {
  useApp,
  nf,
  labels,
  Badge,
  Button,
  Head,
  Panel,
  Table,
  Stats,
  Tabs,
  Notice,
  Line,
  JobTable,
  tm,
} from "../ui";

import { AccountPicker, Modal } from "../components/Management";
function OnboardingReceipt({ accountId }) {
  const { action } = useApp();
  const [result, error] = useRecords(`/api/accounts/${accountId}/onboarding`);
  const submitting = useRef(false);
  const [busy, setBusy] = useState(false);
  async function run(task) {
    if (submitting.current || busy) return; // synchronous guard: same tick cannot double submit
    submitting.current = true;
    setBusy(true);
    try {
      await task();
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <>
      {error && <Notice>{error}</Notice>}
      <p className="quietnote">
        最近核验：{tm(result?.last_checked_at)}
        。领取记录不直接计入余额，完成后会重新查询余额。
      </p>
      <Table
        heads={["任务", "结果", "奖励记录"]}
        rows={(result?.tasks || []).map((t) => [
          t.title || t.key,
          t.done
            ? "已确认"
            : t.status === "uncertain" || t.status === "claiming"
              ? "结果待核对，不重复提交"
              : t.error || "尚未确认",
          t.points == null ? "未提供" : nf(t.points),
        ])}
      />
      <Button
        disabled={
          busy ||
          !result?.supported ||
          result?.settled === true ||
          (result?.tasks || []).some((t) =>
            ["claiming", "uncertain"].includes(t.status),
          )
        }
        onClick={() =>
          run(() =>
            action(
              `/api/accounts/${accountId}/jobs`,
              "POST",
              { kind: "onboarding" },
              "已安排新手奖励核验与领取，随后查询余额。",
            ),
          )
        }
      >
        {busy ? "提交中…" : "核验并领取未完成奖励"}
      </Button>
      {result?.readback_supported &&
        result?.job_id &&
        ["uncertain", "failed"].includes(result.job_status) && (
          <Button
            disabled={busy}
            onClick={() =>
              run(() =>
                action(
                  `/api/jobs/${result.job_id}/reconcile-onboarding`,
                  "POST",
                  {},
                  "已安排只读核对，不会重复领取。",
                ),
              )
            }
          >
            {busy ? "提交中…" : "只读核对领取结果"}
          </Button>
        )}
    </>
  );
}
function Maintenance() {
  const { data, action } = useApp();
  const [onboardingAccount, setOnboardingAccount] = useState(""),
    [rewardOpen, setRewardOpen] = useState(false);
  const planning = useRef(false);
  const [planBusy, setPlanBusy] = useState(false);
  const c = data.today.checkin,
    b = data.today.balance,
    followup = data.after_checkin_balance;
  return (
    <>
      <Head name="签到中心" desc="成功回执和后续任务在同一事务中保存。">
        <Benefits />
        <Button
          icon={Play}
          variant="primary"
          disabled={planBusy}
          onClick={async () => {
            if (planning.current || planBusy) return; // synchronous guard
            planning.current = true;
            setPlanBusy(true);
            try {
              await action(
                "/api/maintenance/plan-today",
                "POST",
                undefined,
                (r) => `已安排 ${r.scheduled} 个今日签到任务。`,
              );
            } finally {
              planning.current = false;
              setPlanBusy(false);
            }
          }}
        >
          {planBusy ? "安排中…" : "安排今日签到"}
        </Button>
      </Head>
      <Tabs group="maintenance" />
      <Stats
        items={[
          ["今日签到成功", nf(c.succeeded), "当天成功不重复"],
          ["签到失败", nf(c.failed), "不关闭账户开关"],
          [
            "排队 / 执行中",
            `${c.queued || 0} / ${c.running || 0}`,
            "平台之间共享空闲执行容量",
          ],
          [
            "余额查询成功",
            nf(b.succeeded),
            `排队 ${b.queued || 0} · 失败 ${b.failed || 0}`,
          ],
        ]}
      />
      <Notice>
        维护共用执行容量，签到成功后立即接续余额查询；当天确认成功的账户不再安排签到。
      </Notice>
      <div className="grid gap">
        <Panel name="执行状态">
          <div className="pad">
            <Line
              name="共享维护执行容量"
              desc="不按平台固定分配；与业务并发分开。"
            >
              <strong>
                {data.runtime.active} / {data.runtime.capacity}
              </strong>
            </Line>
            <Line name="签到后查询余额" desc="查询失败只重试余额，不重复签到。">
              <Badge
                status={
                  followup.running
                    ? "running"
                    : followup.failed
                      ? "failed"
                      : followup.queued
                        ? "queued"
                        : followup.succeeded
                          ? "succeeded"
                          : undefined
                }
              />
            </Line>
            <Line name="失联任务" desc="结果不确定的操作保留回执核对状态。">
              <strong>{data.job_counts.uncertain || 0}</strong>
            </Line>
          </div>
        </Panel>
        <Panel name="今天的任务">
          <Table
            heads={["任务", "成功", "失败", "排队", "执行中"]}
            rows={Object.entries(data.today).map(([kind, c]) => [
              labels[kind] || kind,
              c.succeeded || 0,
              c.failed || 0,
              c.queued || 0,
              c.running || 0,
            ])}
          />
        </Panel>
      </div>
      <Panel name="新手奖励记录">
        <div className="pad">
          <p className="quietnote">
            新注册账户在签到、余额核验后自动接续；迁入旧账户仅在你手动操作时执行。结果不明时保留记录，不自动重复领取。
          </p>
          <AccountPicker
            value={onboardingAccount}
            onChange={setOnboardingAccount}
            capability="onboarding"
            label="选择账户查看奖励"
          />

          {onboardingAccount && (
            <OnboardingReceipt
              key={onboardingAccount}
              accountId={onboardingAccount}
            />
          )}
        </div>
      </Panel>
      <Panel
        name="任务记录"
        desc="按业务日和状态查询完整数据库；上方统计为今天全部任务。"
      >
        <JobHistory kinds="checkin,balance" />
      </Panel>
    </>
  );
}

export default Maintenance;

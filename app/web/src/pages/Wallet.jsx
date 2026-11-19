import CreditPackages from "./CreditPackages";
import React, { useState } from "react";
import { useRecords } from "../use-records";
import { useApp, Panel, Table, Notice, Button, tm, nf, labels } from "../ui";

export default function Wallet({ account }) {
  const { mutate } = useApp();
  const [view, setView] = useState("observations");
  const [result, error] = useRecords(`/api/accounts/${account.id}/wallet`);
  return (
    <Panel
      name="积分与余额记录"
      className="topgap"
      action={
        <Button
          onClick={() =>
            mutate(
              `/api/accounts/${account.id}/jobs`,
              "POST",
              { kind: "balance" },
              "余额查询已排队，结果确认后更新。",
            ).catch(() => {})
          }
        >
          查询余额
        </Button>
      }
    >
      <div className="pad">
        {error && <Notice>{error}</Notice>}
        <div className="segmented">
          <button
            className={view === "observations" ? "active" : ""}
            onClick={() => setView("observations")}
          >
            余额观察
          </button>
          <button
            className={view === "packages" ? "active" : ""}
            onClick={() => setView("packages")}
          >
            积分包与到期
          </button>
        </div>
        {view === "packages" ? (
          <CreditPackages account={account} />
        ) : !result ? (
          <p className="muted">正在读取余额记录…</p>
        ) : (
          <>
            <p>
              <strong>
                {result.balance == null ? "尚无成功查询" : nf(result.balance)}
              </strong>{" "}
              <span className="muted">
                {labels[result.balance_status] || result.balance_status} ·{" "}
                {tm(result.balance_at)}
              </span>
            </p>
            <p className="quietnote">
              {result.note ||
                "以下为余额查询观察值；两次差额可能同时包含赠送与消耗，不等于充值或单请求扣费。"}
            </p>
            <Table
              heads={["核验时间", "余额", "相比前次", "关联任务"]}
              rows={(result.observations || []).map((o) => [
                tm(o.created_at),
                nf(o.amount),
                o.delta == null
                  ? "—"
                  : `${o.delta > 0 ? "+" : ""}${nf(o.delta)}`,
                o.job_id?.slice(0, 8) || "—",
              ])}
              empty="尚无余额查询成功记录"
            />
          </>
        )}
      </div>
    </Panel>
  );
}

import React, { useState } from "react";
import { useRecords } from "../use-records";
import { useApp, Button, JobTable, Notice, labels } from "../ui";

import JobDetail from "./JobDetail";
import { AccountPicker } from "../components/Management";
import { useDebounced } from "../query";
export default function JobHistory({ kinds = "" }) {
  const { data } = useApp();
  const [detail, setDetail] = useState(null),
    [platform, setPlatform] = useState("");
  const [day, setDay] = useState(data.business_day),
    [kind, setKind] = useState(""),
    [status, setStatus] = useState(""),
    [account, setAccount] = useState(""),
    [q, setQ] = useState(""),
    [offset, setOffset] = useState(0);
  const search = useDebounced(q);
  const params = new URLSearchParams({
    platform,
    day,
    kind: kind || kinds,
    status,
    account_id: account,
    q: search,
    offset,
    limit: 50,
  });
  const [result, error] = useRecords(`/api/jobs?${params}`);
  function change(set, value) {
    set(value);
    setOffset(0);
  }
  return (
    <>
      <div className="toolbar">
        <select
          aria-label="任务平台"
          value={platform}
          onChange={(e) => {
            change(setPlatform, e.target.value);
            setAccount("");
          }}
        >
          <option value="">全部平台</option>
          {data.platforms.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
        <input
          type="date"
          aria-label="任务业务日，清空查询全部"
          value={day}
          onChange={(e) => change(setDay, e.target.value)}
        />
        <Button onClick={() => change(setDay, "")}>全部日期</Button>
        <select
          aria-label="任务类型"
          value={kind}
          onChange={(e) => change(setKind, e.target.value)}
        >
          <option value="">全部类型</option>
          {(kinds
            ? kinds.split(",")
            : [
                "checkin",
                "balance",
                "onboarding",
                "renew",
                "relogin",
                "verify",
                "invitation",
                "benefits",
              ]
          ).map((k) => (
            <option key={k} value={k}>
              {labels[k] || k}
            </option>
          ))}
        </select>
        <select
          aria-label="任务状态"
          value={status}
          onChange={(e) => change(setStatus, e.target.value)}
        >
          <option value="">全部状态</option>
          {[
            "queued",
            "running",
            "succeeded",
            "failed",
            "uncertain",
            "superseded",
          ].map((s) => (
            <option key={s} value={s}>
              {labels[s] || s}
            </option>
          ))}
        </select>
        <input
          aria-label="搜索任务账户"
          placeholder="搜索账户名称 / 平台账户标识"
          value={q}
          onChange={(e) => change(setQ, e.target.value)}
        />
        <AccountPicker
          platform={platform}
          value={account}
          onChange={(v) => change(setAccount, v)}
          label="筛选任务账户"
        />
      </div>
      {error && <Notice>{error}</Notice>}
      <p className="quietnote">
        筛选范围内共 {result?.total ?? "—"} 条任务 · 成功{" "}
        {result?.counts?.succeeded || 0} · 失败 {result?.counts?.failed || 0} ·
        待核对 {result?.counts?.uncertain || 0}
      </p>
      <JobTable jobs={result?.rows || []} onDetail={setDetail} />
      <JobDetail id={detail} onClose={() => setDetail(null)} />
      <div className="row wrap topgap">
        <Button
          disabled={!offset}
          onClick={() => setOffset(Math.max(0, offset - 50))}
        >
          上一批
        </Button>
        <span>
          第 {result?.total ? offset + 1 : 0}—
          {Math.min(offset + (result?.rows?.length || 0), result?.total || 0)}{" "}
          条
        </span>
        <Button
          disabled={!result || offset + 50 >= result.total}
          onClick={() => setOffset(offset + 50)}
        >
          下一批
        </Button>
      </div>
    </>
  );
}

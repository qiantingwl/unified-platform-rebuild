import React, { useState } from "react";
import { useApp, Table, Notice, tm, nf } from "../ui";
import { useQuery } from "../query";
import { Pagination } from "../components/Management";
const statuses = {
  verified: "已读取官方积分包",
  failed: "最近查询失败",
  stale: "上次观察已过期",
  unavailable: "平台未提供积分包",
};
export default function CreditPackages({ account }) {
  const { data } = useApp();
  const [page, setPage] = useState(1);
  const query = useQuery(
      `/api/accounts/${account.id}/credit-packages?page=${page}&page_size=25`,
      data.revision,
    ),
    r = query.result;
  const date = (v) =>
    v == null ? "未提供" : typeof v === "number" ? tm(v) : String(v);
  return (
    <>
      {query.error && <Notice>{query.error}</Notice>}
      {r && (
        <>
          <p className="quietnote">
            {statuses[r.status] || r.status} · {tm(r.observed_at)}
            {r.credential_current === false && r.credential_version!=null ? " · 旧凭证观察值" : ""}
            。以下为平台原始积分包，不从余额差生成交易流水。
          </p>
          <Table
            heads={[
              "积分包 / 来源",
              "剩余 / 总量 / 已用",
              "周期",
              "到期时间",
              "到期依据",
            ]}
            rows={(r.packages || []).map((p) => [
              <>
                {p.name || p.package_code}
                <small>{p.source}</small>
              </>,
              `${p.remaining == null ? "未提供" : nf(p.remaining)} / ${p.size == null ? "未提供" : nf(p.size)} / ${p.used == null ? "未提供" : nf(p.used)}`,
              p.cycle ?? "未提供",
              date(p.expires_at),
              p.expiry_source || "未提供",
            ])}
          />
          <Pagination
            page={page}
            pageSize={25}
            total={r.total}
            loading={query.loading}
            onPage={setPage}
          />
          {r.transactions_available === false && (
            <p className="quietnote">当前平台没有提供可展示的交易流水。</p>
          )}
        </>
      )}
    </>
  );
}

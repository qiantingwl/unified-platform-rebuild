import React from "react";
import { ChevronRight, ArrowUpRight, CheckCircle2 } from "lucide-react";

import {
  useApp,
  nf,
  tm,
  creditSummary,
  labels,
  Button,
  Head,
  Panel,
  Table,
  Stats,
  Notice,
  Line,
  totals,
} from "../ui";
import { useRecords } from "../use-records";
function Overview() {
  const { data, openAccounts } = useApp();
  const [overview, error] = useRecords("/api/overview");
  const summaries = overview?.platform_summaries || [];
  const t = data.totals,
    c = data.today.checkin,
    b = data.after_checkin_balance;
  return (
    <>
      <Head name="运行总览" desc="账户、调用和维护共用同一份数据库快照。">
        <Button icon={ArrowUpRight} onClick={() => (location.hash = "logs")}>
          查看调用
        </Button>
      </Head>
      {error && <Notice>{error}</Notice>}
      {!t.total && (
        <Notice>
          账户库还是空的。可以迁入已有账户与凭证，或从注册页新建账户。
          <div className="row wrap" style={{ marginTop: 12 }}>
            <a className="btn primary" href="#imports">
              迁入账户
            </a>
            <a className="btn" href="#register">
              注册新账户
            </a>
          </div>
        </Notice>
      )}
      {data.runtime.upstream_enabled === false && (
        <Notice>
          外部执行已停用（UNIFIED_ENABLE_UPSTREAM=0）：签到、余额、注册和转发都不会访问上游平台。
        </Notice>
      )}
      <Stats
        items={[
          ["全部账户", nf(t.total), `${data.platforms.length} 个平台入口`],
          ["可接单", nf(t.ready), "按当前模型与空闲槽判断"],
          ["正在使用", nf(t.busy), "选择账户与占用槽原子执行"],
          ["暂不可接单", nf(t.unavailable), "按具体原因分别统计"],
        ]}
      />
      <Panel
        name="各平台剩余积分 / 额度"
        desc={`最近已知余额保留历史成功查询值；新鲜核验只统计一小时内且凭证版本一致的结果。各平台不跨单位相加。数据时点 ${tm(data.as_of)}`}
      >
        <Table
          pageSize={20}
          heads={[
            "平台",
            "最近已知余额",
            "新鲜核验 / 全部",
            "新鲜核验余额",
            "数据过期",
            "未查询",
            "查询失败",
          ]}
          rows={data.platforms.map((p) => {
            const s = summaries.find((s) => s.platform === p) || {};
            return [
              <Button variant="link" onClick={() => openAccounts(p, "all")}>
                {p}
              </Button>,
              s.known ? (
                <>
                  {nf(Math.round(s.knownSum * 100) / 100)}
                  <small>{nf(s.known)} 个账户有历史余额</small>
                </>
              ) : (
                "尚未查询"
              ),
              `${s.verified == null ? "—" : nf(s.verified)} / ${s.total == null ? "—" : nf(s.total)}${s.total ? `（${Math.round((s.verified / s.total) * 100)}%）` : ""}`,
              s.verified ? (
                <strong>{nf(Math.round(s.sum * 100) / 100)}</strong>
              ) : (
                "—"
              ),
              nf(s.stale),
              nf(s.unknown),
              s.failed ? <span className="red">{nf(s.failed)}</span> : "0",
            ];
          })}
        />
      </Panel>
      <div className="grid">
        <Panel name="各平台状态" desc="可接单 + 使用中 + 暂不可接单 = 账户总数">
          <Table
            heads={["平台", "全部", "可接单", "使用中", "暂不可接单"]}
            rows={data.platforms.map((p) => {
              const t = summaries.find((s) => s.platform === p) || {};
              return [
                p,
                t.total == null ? "—" : nf(t.total),
                <span className="green">
                  {t.ready == null ? "—" : nf(t.ready)}
                </span>,
                t.busy == null ? "—" : nf(t.busy),
                <Button
                  variant="link"
                  onClick={() => openAccounts(p, "unavailable")}
                >
                  {t.unavailable == null ? "—" : nf(t.unavailable)}
                </Button>,
              ];
            })}
          />
        </Panel>
        <div className="stack">
          <Panel
            name="今日签到"
            action={
              <Button
                variant="link"
                onClick={() => (location.hash = "maintenance")}
              >
                查看
              </Button>
            }
          >
            <div className="pad">
              <Line
                name="签到成功"
                desc={`失败 ${c.failed || 0} · 执行中 ${c.running || 0} · 排队 ${c.queued || 0}`}
              >
                <strong className="green">{nf(c.succeeded)}</strong>
              </Line>
              <Line
                name="签到后余额查询"
                desc={`查询任务：成功 ${b.succeeded || 0} · 排队 ${b.queued || 0} · 失败 ${b.failed || 0}`}
              >
                <CheckCircle2 size={18} />
              </Line>
            </div>
          </Panel>
          <Panel name="需要处理">
            <div className="pad">
              {Object.entries(data.reasons)
                .filter(([r]) => !["ready", "busy"].includes(r))
                .map(([r, n]) => (
                  <Line key={r} name={labels[r] || r}>
                    <Button
                      variant="link"
                      onClick={() => openAccounts("全部平台", r)}
                    >
                      {nf(n)} <ChevronRight size={15} />
                    </Button>
                  </Line>
                ))}
              {!t.unavailable && <p className="muted">没有待处理账户</p>}
            </div>
          </Panel>
        </div>
      </div>
    </>
  );
}

export default Overview;

import React from "react";

import {
  useApp,
  tm,
  labels,
  Badge,
  Head,
  Panel,
  Stats,
  Tabs,
  Line,
  Table,
  Notice,
} from "../ui";
import { useRecords } from "../use-records";
function Monitor() {
  const { data } = useApp();
  const [record, error] = useRecords("/api/monitor");
  const samples = (record?.samples || []).map((r) => r.data),
    recent = samples[0];
  const cpus = samples
    .map((r) => r.process_cpu_percent)
    .filter((v) => typeof v === "number" && Number.isFinite(v));
  const bytes = (n) =>
    n == null ? "未采集" : (n / 1024 / 1024).toFixed(1) + " MiB";
  return (
    <>
      <Head
        name="系统状态"
        desc="数据库和执行器的当前状态，未采集的指标不显示假数值。"
      />
      <Tabs group="settings" />
      {error && <Notice>{error}</Notice>}
      <Stats
        items={[
          [
            "共享执行中",
            data.runtime.active,
            `维护 ${data.execution?.maintenance_active ?? "—"} · 注册 ${data.execution?.registration_active ?? "—"} / 容量 ${data.runtime.capacity}`,
          ],
          [
            "任务待核对",
            data.job_counts?.uncertain ?? "—",
            "不确定结果不自动重做",
          ],
          ["数据库", data.runtime.database, `快照版本 ${data.revision}`],
          [
            "进程运行时间",
            Math.floor((data.as_of - data.runtime.started_at) / 60) + " 分钟",
            "仅当前应用进程",
          ],
        ]}
      />
      <Panel name="组件状态">
        <div className="pad">
          <Line
            name="共享维护执行器"
            desc={`最近心跳 ${tm(data.runtime.last_tick)}`}
          >
            <Badge
              status={data.runtime.worker_running ? "running" : "uncertain"}
            />
          </Line>
          <Line
            name="浏览器执行服务"
            desc={(() => {
              const b = data.runtime.browser_executor || {};
              if (b.status === "not_configured")
                return "未配置 UNIFIED_BROWSER_URL / UNIFIED_BROWSER_TOKEN，面板内官方登录不可用";
              if (b.status !== "ok")
                return "无法连接独立浏览器容器；已保存的登录状态在恢复后自动重新打开";
              return `会话 ${b.sessions} / ${b.max_sessions} · Chromium ${b.browser_connected ? "运行中" : "按需启动"} · 重启 ${b.restarts || 0} 次${b.last_error ? ` · 最近异常 ${b.last_error}` : ""}`;
            })()}
          >
            <Badge
              status={
                data.runtime.browser_executor?.status === "ok"
                  ? "running"
                  : "failed"
              }
            >
              {data.runtime.browser_executor?.status === "ok"
                ? "在线"
                : data.runtime.browser_executor?.status === "not_configured"
                  ? "未配置"
                  : "离线"}
            </Badge>
          </Line>
          <Line
            name="最近执行异常"
            desc={
              labels[data.runtime.last_error] ||
              data.runtime.last_error ||
              "无记录"
            }
          />
          <Line
            name="任务服务进程 CPU"
            desc={
              cpus.length
                ? `${Math.min(...cpus).toFixed(1)}%–${Math.max(...cpus).toFixed(1)}%，${cpus.length} 次采样；多核进程可超过 100%`
                : "等待采样"
            }
          />
          <Line
            name="任务服务内存"
            desc={bytes(recent?.process_memory_bytes)}
          />
          <Line
            name="磁盘剩余"
            desc={
              recent && recent.disk_free_bytes != null
                ? (recent.disk_free_bytes / 1024 ** 3).toFixed(1) + " GiB"
                : "未采集"
            }
          />
          <Line name="自动调度心跳" desc={tm(record?.last_tick)}>
            <Badge
              status={record?.supervisor_running ? "running" : "uncertain"}
            />
          </Line>
          <Line
            name="邀请码回读心跳"
            desc={`${tm(record?.invitation?.last_tick)} · ${record?.invitation?.last_error || "暂无回读调度异常"}`}
          />
          <Line
            name="注册调度心跳"
            desc={tm(record?.registration?.last_tick)}
          />
          <Line
            name="注册待核对"
            desc={`${data.registration_counts?.uncertain ?? "—"} 笔 · ${record?.registration?.last_error || "暂无调度异常记录"}`}
          />
          <Line
            name="监控范围"
            desc="资源采样来自本项目任务服务进程，每 15 秒采样；不代表主程序、数据库和浏览器容器总占用。"
          />
          <Line name="服务启动时间" desc={tm(data.runtime.started_at)} />
        </div>
      </Panel>
      <Panel name="系统告警">
        <Table
          heads={["项目", "状态", "发生时间", "数量"]}
          rows={(record?.alerts || []).map((a) => [
            {
              disk_low: "磁盘空间不足",
              uncertain_jobs: "任务回执待核对",
              queue_waiting: "队列等待超过 30 分钟",
            }[a.name] || a.name,
            a.data.active ? "待处理" : "已恢复",
            tm(a.data.changed_at),
            a.data.count ?? "—",
          ])}
        />
      </Panel>
    </>
  );
}

export default Monitor;

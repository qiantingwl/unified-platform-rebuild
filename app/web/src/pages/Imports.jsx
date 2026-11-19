import React, { useEffect, useState } from "react";
import { api, errors } from "../api";
import {
  useApp,
  Head,
  Tabs,
  Panel,
  Button,
  Notice,
  Table,
  Badge,
  tm,
} from "../ui";
import { useRecords } from "../use-records";

import { Modal, Pagination } from "../components/Management";
const names = {
  new_mapping: "可补手机号",
  same_phone: "号码相同，跳过",
  phone_conflict: "已有号码或版本冲突",
  mapped: "号码已关联",
  discarded: "预览已放弃",
  preview: "等待确认",
  committed: "已迁入",
  new: "可新增",
  existing_account: "已有账户，保留原凭证",
  duplicate_in_file: "文件内重复",
  invalid: "格式或材料不完整",
  imported: "已迁入",
  pending: "等待身份核验",
  needs_login: "需要官方登录",
  verifying: "核验中",
  activated: "凭证已激活",
  failed: "核验失败",
  uncertain: "核验中断，可重查",
  superseded: "已通过其他方式更新",
};
export default function Imports() {
  const { data, mutate } = useApp();
  const [open, setOpen] = useState(false),
    [historyPage, setHistoryPage] = useState(1);
  const [history, loadError] = useRecords(
    `/api/imports?page=${historyPage}&page_size=25`,
  );
  const [selected, setSelected] = useState(""),
    [detail, setDetail] = useState(null),
    [page, setPage] = useState(1),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [platform, setPlatform] = useState(""),
    [mode, setMode] = useState("accounts");
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let active = true;
    api(`/api/imports/${selected}?page=${page}`)
      .then((r) => {
        if (active) setDetail(r);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [selected, page, data.revision]);
  async function action(fn) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  function template() {
    const blob = new Blob(
      [
        JSON.stringify(
          mode === "phone_mapping"
            ? {
                accounts: [
                  {
                    platform: "WPS 灵犀",
                    external_id: "",
                    phone: "",
                  },
                ],
              }
            : {
                accounts: [
                  {
                    platform: "WPS 灵犀",
                    external_id: "",
                    credential: {
                      uid: "",
                      cookie: "",
                    },
                    phone: "",
                  },
                ],
              },
          null,
          2,
        ),
      ],
      { type: "application/json" },
    );
    const url = URL.createObjectURL(blob),
      a = document.createElement("a");
    a.href = url;
    a.download = "account-import-template.json";
    a.click();
    URL.revokeObjectURL(url);
  }
  return (
    <>
      <Head
        name="账户导入"
        desc="先检查数据，再写入账户库；新凭证通过身份核验后衔接维护。"
      >
        <Button variant="primary" onClick={() => setOpen(true)}>
          导入账户文件
        </Button>
      </Head>
      <Tabs group="settings" />
      {(error || loadError) && <Notice>{error || loadError}</Notice>}
      <Modal
        open={open}
        onOpenChange={setOpen}
        title="导入 JSON / JSONL"
        description="生成脱敏预览后再确认写入"
      >
        {error && <Notice>{error}</Notice>}
        <form
          className="pad"
          onSubmit={(e) => {
            e.preventDefault();
            const form = e.currentTarget,
              f = new FormData(form),
              file = f.get("file");
            action(async () => {
              if (!file?.size || file.size > 8000000)
                throw new Error("请选择不超过 8 MB 的 JSON / JSONL 文件。");
              const result = await mutate(
                "/api/imports/preview",
                "POST",
                {
                  content: await file.text(),
                  platform: platform || null,
                  mode,
                  source: f.get("source") || "账户迁移",
                },
                "脱敏预览已保存，尚未写入账户。",
              );
              setPage(1);
              setSelected(result.id);
              form.reset();
              setOpen(false);
            });
          }}
        >
          <div className="grid">
            <label className="field gap">
              迁入内容
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="accounts">账户及凭证</option>
                <option value="phone_mapping">已有账户的手机号对应表</option>
              </select>
            </label>
            <label className="field gap">
              来源备注
              <input
                name="source"
                placeholder="例如：账户来源或批次名称"
                maxLength={80}
              />
            </label>
            <label className="field gap">
              所属平台
              <select
                value={platform}
                onChange={(e) => setPlatform(e.target.value)}
              >
                <option value="">使用每条记录的平台字段</option>
                {data.platforms.map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
            </label>
            <label className="field gap">
              账户文件
              <input
                name="file"
                type="file"
                accept=".json,.jsonl,application/json"
                required
              />
            </label>
          </div>
          <p className="quietnote">
            支持标准格式及 MiraiHub 明文 backup 导出。WorkBuddy
            国内、国际须明确选择；加密客户端 .info 需先在原程序导出
            backup。不会按文件顺序猜手机号归属。
          </p>
          <div className="row gap">
            <Button type="submit" disabled={busy}>
              生成脱敏预览
            </Button>
            <Button onClick={template}>下载格式示例</Button>
          </div>
        </form>
      </Modal>
      <Panel name="导入批次">
        <Table
          heads={["来源", "创建时间", "状态", "数量", "操作"]}
          rows={(history?.batches || []).map((b) => [
            b.name,
            tm(b.data.created_at),
            names[b.data.status],
            Object.entries(b.data.counts || {})
              .map(([k, n]) => `${names[k] || k} ${n}`)
              .join(" · "),
            <Button
              onClick={() => {
                setPage(1);
                setSelected(b.id);
              }}
            >
              查看详情
            </Button>,
          ])}
        />
        <Pagination
          page={historyPage}
          pageSize={25}
          total={history?.total}
          loading={!history && !loadError}
          onPage={setHistoryPage}
        />
      </Panel>
      {detail && (
        <Panel name="本批明细">
          <div className="pad">
            <p>
              {Object.entries(detail.data.counts || {})
                .map(([k, n]) => `${names[k] || k} ${n}`)
                .join(" · ")}
            </p>
            {detail.data.status === "preview" && (
              <>
                <Notice>
                  仅新增无冲突的记录，重复项和错误项保留明细。不会覆盖已有账户、重登次数、当前业务并发或手机号。
                </Notice>
                <div className="row gap">
                  <Button
                    disabled={
                      busy ||
                      !(
                        detail.data.counts?.new ||
                        detail.data.counts?.new_mapping
                      )
                    }
                    onClick={() =>
                      action(async () => {
                        await mutate(
                          `/api/imports/${selected}/commit`,
                          "POST",
                          { expected_version: detail.version },
                          "账户已迁入，凭证仍等待核验。",
                        );
                      })
                    }
                  >
                    确认写入{" "}
                    {detail.data.counts?.new ||
                      detail.data.counts?.new_mapping ||
                      0}{" "}
                    条
                  </Button>
                  <Button
                    disabled={busy}
                    onClick={() =>
                      action(async () => {
                        await mutate(
                          `/api/imports/${selected}/discard`,
                          "POST",
                          { expected_version: detail.version },
                          "预览材料已移除，保留批次记录。",
                        );
                      })
                    }
                  >
                    放弃预览
                  </Button>
                </div>
              </>
            )}
            {detail.data.status === "committed" && (
              <Notice>
                迁入不触发短信或模型测试。身份核验是单账户操作；核验后自动按平台能力安排签到及余额。Loomy、Marvis
                暂走官方登录，不把余额成功当成身份核验。
              </Notice>
            )}
          </div>
          <Table
            pageSize={50}
            heads={["行号", "平台", "迁入结果", "身份核验", "操作"]}
            rows={detail.data.entries.map((e) => {
              const c = detail.candidates.find(
                (c) => c.data.account_id === e.account_id,
              );
              return [
                e.line,
                e.platform || "待确认",
                names[e.status] || e.status,
                <>
                  {c ? (
                    <Badge>{names[c.data.status] || c.data.status}</Badge>
                  ) : null}
                  <small>
                    {errors[e.error || c?.data.error] ||
                      e.error ||
                      c?.data.error ||
                      ""}
                  </small>
                </>,
                c ? (
                  <div className="row wrap">
                    {["pending", "failed", "uncertain"].includes(
                      c.data.status,
                    ) && (
                      <Button
                        disabled={
                          busy ||
                          !data.adapter_catalog?.[e.platform]?.network_enabled
                        }
                        onClick={() =>
                          action(async () => {
                            const result = await mutate(
                              `/api/imports/candidates/${c.id}/verify`,
                              "POST",
                              {},
                              "核验记录已更新。",
                            );
                            if (result.error)
                              setError(errors[result.error] || result.error);
                          })
                        }
                      >
                        核验凭证
                      </Button>
                    )}
                    {c.data.status !== "activated" && (
                      <a href="#recovery" className="btn">
                        登录与材料
                      </a>
                    )}
                  </div>
                ) : (
                  "—"
                ),
              ];
            })}
          />
          <div className="pad row">
            <Button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              上一页
            </Button>
            <span>
              第 {page} 页 · 共 {detail.total} 条
            </span>
            <Button
              disabled={page * 50 >= detail.total}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页
            </Button>
          </div>
        </Panel>
      )}
    </>
  );
}

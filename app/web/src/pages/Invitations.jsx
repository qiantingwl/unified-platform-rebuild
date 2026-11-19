import React, { useRef, useState } from "react";
import { useRecords } from "../use-records";
import { errors } from "../api";
import {
  useApp,
  Head,
  Panel,
  Button,
  Table,
  Notice,
  Tabs,
  tm,
  Badge,
} from "../ui";
import { AccountPicker, Modal, Pagination } from "../components/Management";
export default function Invitations() {
  const { data, mutate } = useApp();
  const [page,setPage]=useState(1),[platform,setPlatform]=useState(""),[status,setStatus]=useState("");
  const [records, error] = useRecords(`/api/invitations?page=${page}&page_size=25&platform=${encodeURIComponent(platform)}&status=${encodeURIComponent(status)}`);
  const [account, setAccount] = useState(""),
    [failure, setFailure] = useState(""),
    [busy, setBusy] = useState(false),
    [open, setOpen] = useState(false);
  const submitting = useRef(false);
  async function submit(task) {
    if (submitting.current || busy) return; // synchronous guard: same tick cannot double submit
    submitting.current = true;
    setBusy(true);
    setFailure("");
    try {
      await task();
    } catch (e) {
      setFailure(e.message);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  function read(id) {
    return submit(() =>
      mutate(
        `/api/accounts/${id}/invitation/read`,
        "POST",
        {},
        "回读任务已排队，确认后更新记录。",
      ),
    );
  }
  return (
    <>
      <Head name="邀请码与权益" desc="绑定提交、回读确认与权益到账分别记录。">
        <Button variant="primary" onClick={() => setOpen(true)}>
          绑定邀请码
        </Button>
      </Head>
      <Tabs group="maintenance" />
      {(error || failure) && <Notice>{error || failure}</Notice>}
      <div className="toolbar"><select aria-label="邀请记录平台" value={platform} onChange={e=>{setPlatform(e.target.value);setPage(1);}}><option value="">全部平台</option>{data.platforms.map(p=><option key={p}>{p}</option>)}</select><select aria-label="邀请记录状态" value={status} onChange={e=>{setStatus(e.target.value);setPage(1);}}><option value="">全部状态</option>{[["queued","排队"],["running","读取中"],["bound","绑定已确认"],["uncertain","待核对"],["failed","失败"]].map(([v,n])=><option value={v} key={v}>{n}</option>)}</select></div>
      <Panel name="执行记录">
        <Table
          heads={["账户", "提交 / 回读", "绑定", "权益", "结果", "操作"]}
          rows={(records?.records || []).map((r) => [
            <>
              {r.account_display_name || r.data.account_id}
              <small>{r.identity_hint}</small>
            </>,
            <>
              {tm(r.data.submitted_at)}
              <small>{tm(r.data.read_at)}</small>
            </>,
            r.data.binding_confirmed ? "已确认" : "未确认",
            r.data.credit_confirmed ? "已确认" : "缺到账凭据",
            <Badge>
              {{
                bound: "绑定已核实",
                bound_other_code: "已绑定其他邀请码",
                not_bound: "未绑定",
                binding_unconfirmed: "绑定尚未核实",
                uncertain: "等待回读",
                submitting: "提交中",
                running: "读取中",
                read_completed: "回读完成",
                waiting_donor: "等待邀请码",
                queued: "排队中",
                failed: "读取失败",
              }[r.data.status] || r.data.status}
            </Badge>,
            <>
              <small>{errors[r.data.error] || r.data.error}</small>
              <Button disabled={busy} onClick={() => read(r.data.account_id)}>
                重新回读
              </Button>
            </>,
          ])}
        />
        <Pagination page={page} pageSize={25} total={records?.total} loading={!records&&!error} onPage={setPage}/>
      </Panel>
      <Modal
        open={open}
        busy={busy}
        onOpenChange={(v) => !v && !busy && setOpen(v)}
        title="绑定邀请码"
        description="仅操作支持该能力的平台，提交不等于权益到账"
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const form = e.currentTarget;
            submit(async () => {
              await mutate(
                `/api/accounts/${account}/invitation/bind`,
                "POST",
                { code: new FormData(form).get("code") },
                "绑定任务已排队，等待回读确认。",
              );
              form.reset();
              setOpen(false);
            });
          }}
        >
          <div className="field gap">
            <label>账户</label>
            <AccountPicker
              value={account}
              onChange={setAccount}
              capability="invite_read"
              label="选择账户"
              required
            />
          </div>
          <label className="field gap">
            邀请码
            <input name="code" type="password" required maxLength={128} />
          </label>
          {failure && <Notice>{failure}</Notice>}
          <div className="row">
            <Button type="submit" disabled={!account || busy}>
              {busy ? "提交中…" : "绑定并回读"}
            </Button>
            <Button disabled={!account || busy} onClick={() => read(account)}>
              {busy ? "提交中…" : "只回读"}
            </Button>
          </div>
        </form>
      </Modal>
    </>
  );
}

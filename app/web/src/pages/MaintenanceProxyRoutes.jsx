import React, { useState } from "react";
import ProxyPicker from "./ProxyPicker";
import { useRecords } from "./Connections";
import { useApp, Panel, Button, Notice } from "../ui";

const kinds={checkin:"签到",balance:"余额查询",renew:"凭据续期",onboarding:"新手任务",invitation:"邀请任务",benefits:"平台权益"};
function TaskRoute({route,nodes,pools}) {
  const {mutate}=useApp();
  const [mode,setMode]=useState(route.mode),[target,setTarget]=useState(route.proxy_id||route.pool_id||"");
  const [search,setSearch]=useState('');
  const [busy,setBusy]=useState(false),[error,setError]=useState("");
  const title=`${route.platform}${kinds[route.kind]}`;
  return <form className="gap" onSubmit={async e=>{
    e.preventDefault();setBusy(true);setError("");
    try {await mutate(`/api/maintenance-proxy-routes/${encodeURIComponent(route.platform)}/${route.kind}`,"PUT",
      {expected_version:route.version,mode,...(mode==="fixed"?{proxy_id:target}:mode==="pool"?{pool_id:target}:{})},`${kinds[route.kind]}出口已保存。`);}
    catch(e){setError(e.message);}finally{setBusy(false);}
  }}>
    <strong>{kinds[route.kind]}</strong>
    <div className="row wrap topgap">
      <select aria-label={`${title}出口类型`} value={mode} disabled={busy} onChange={e=>{setMode(e.target.value);setTarget("");}}>
        <option value="inherit">沿用账户／平台出口</option><option value="direct">直连</option><option value="fixed">固定节点</option><option value="pool">代理池</option>
      </select>
      {mode==="pool"&&<input aria-label={`${title}搜索出口`} placeholder="搜索出口名称" value={search} onChange={e=>setSearch(e.target.value)}/>}
      {mode==="fixed"&&<ProxyPicker value={target} onChange={setTarget}/>}
      {mode==="pool"&&<select aria-label={`${title}出口目标`} value={target} required disabled={busy} onChange={e=>setTarget(e.target.value)}>
        <option value="">请选择</option>{(mode==="fixed"?nodes:pools).filter(n=>n.id===target||n.name.toLowerCase().includes(search.toLowerCase())).sort((a,b)=>a.id===target?-1:b.id===target?1:0).slice(0,100).map(n=><option value={n.id} key={n.id}>{n.name}</option>)}
      </select>}
      <Button type="submit" disabled={busy}>保存{kinds[route.kind]}出口</Button>
    </div>{error&&<Notice>{error}</Notice>}
  </form>;
}
export default function MaintenanceProxyRoutes({nodes=[],pools=[]}) {
  const {data}=useApp();
  const [records,error]=useRecords("/api/maintenance-proxy-routes");
  const [platform,setPlatform]=useState("Loomy");
  return <Panel name="维护任务出口" desc="默认沿用账户出口。可分别设置签到、余额、续期和邀请任务使用的代理；已开始的任务保持原出口。">
    <div className="pad">
      <label className="field gap">选择平台<select aria-label="维护出口平台" value={platform} onChange={e=>setPlatform(e.target.value)}>{data.platforms.map(p=><option key={p}>{p}</option>)}</select></label>
      {error&&<Notice>{error}</Notice>}
      {!records?<p>正在读取任务出口…</p>:Object.keys(kinds).map(kind=>{
        const route=records.routes.find(r=>r.platform===platform&&r.kind===kind);
        return route&&<TaskRoute key={`${platform}:${kind}:${route.version}`} route={route} nodes={nodes} pools={pools}/>;
      })}
      <p className="quietnote">模型恢复检测使用转发出口，正常回答才恢复对应调度。注册和重登在会话期间保持同一出口。维护出口的成功不代表转发出口已验证。</p>
    </div>
  </Panel>;
}

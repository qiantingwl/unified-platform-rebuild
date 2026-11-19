import React, { useState } from "react";
import { Button, Notice, Switch, useApp, tm } from "../ui";

export function SubscriptionTargets({platforms, selected, onChange, prefix}) {
  return <fieldset className="gap"><legend>检测目标平台</legend><div className="row wrap">
    {platforms.map(platform=><label key={platform} className="row gap"><input type="checkbox" aria-label={`${prefix}${platform}`} checked={selected.includes(platform)} onChange={e=>onChange(e.target.checked?[...selected,platform]:selected.filter(p=>p!==platform))}/>{platform}</label>)}
  </div><small>只检查目标网站连通性，不发送账户凭据或模型请求。</small></fieldset>;
}

export default function SubscriptionSchedule({subscription}) {
  const {data,mutate}=useApp(), config=subscription.data;
  const [enabled,setEnabled]=useState(config.auto_update===true),[interval,setInterval]=useState(config.interval_minutes||360);
  const [targets,setTargets]=useState(config.target_platforms||[]),[checks,setChecks]=useState(config.auto_target_check===true);
  const [profile,setProfile]=useState(config.source_profile||'standard'),[limit,setLimit]=useState(config.recommendation_limit||100);
  const [busy,setBusy]=useState(false),[error,setError]=useState("");
  const title=subscription.name;
  return <form className="pad" onSubmit={async e=>{e.preventDefault();setBusy(true);setError("");try{
    await mutate(`/api/proxy-subscriptions/${subscription.id}/schedule`,"PUT",{expected_version:subscription.version,auto_update:enabled,interval_minutes:Number(interval),target_platforms:targets,auto_target_check:checks,source_profile:profile,recommendation_limit:Number(limit)},"订阅更新与检测设置已保存。");
  }catch(e){setError(e.message);}finally{setBusy(false);}}}>
    <h3>{title}</h3>
    {config.source_metadata&&<p className="quietnote">来源更新时间：{config.source_metadata.source_updated_at||'未提供'}；已测速 {config.source_metadata.speed_tested??'—'}，支持 {config.source_metadata.supported??'—'}，本轮入选 {config.source_metadata.selected??'—'} / 考察 {config.source_metadata.considered??'—'}，上限 {config.source_metadata.limit??'—'}。</p>}
    <label className="field gap">来源方式<select aria-label={`${title}来源方式`} value={profile} disabled={busy||!subscription.has_secret} onChange={e=>setProfile(e.target.value)}><option value="standard">标准订阅</option><option value="leidahub_fast">节点雷达测速推荐</option></select></label>
    {profile==='leidahub_fast'&&<><Notice>使用节点雷达网站已有测速的推荐节点；来源测速不等于 OVH 服务器可用，仍需通过所选目标平台检测。</Notice><label className="field gap">本轮推荐上限<input aria-label={`${title}推荐上限`} type="number" min="1" max="500" required value={limit} onChange={e=>setLimit(e.target.value)}/></label></>}
    <Switch label={`${title}自动更新`} checked={enabled} disabled={!subscription.has_secret||busy} onChange={e=>setEnabled(e.target.checked)} description={subscription.has_secret?"完成一轮后按设定间隔安排下一轮。":"粘贴内容没有远程地址，不能自动更新。"}/>
    <label className="field gap">更新间隔（分钟）<input aria-label={`${title}更新间隔`} type="number" min="5" max="10080" required value={interval} disabled={busy} onChange={e=>setInterval(e.target.value)}/></label>
    <Switch label={`${title}自动检测目标`} checked={checks} disabled={busy} onChange={e=>setChecks(e.target.checked)} description="按已选平台检查新节点及到期结果；通过后才供对应平台使用。"/>
    <SubscriptionTargets platforms={data.platforms} selected={targets} onChange={setTargets} prefix={`${title}目标`}/>
    {error&&<Notice>{error}</Notice>}
    {checks&&!targets.length&&<Notice>开启自动检测时，请至少选择一个目标平台。</Notice>}
    <Button type="submit" disabled={busy||(checks&&!targets.length)}>保存更新与检测设置</Button>
    <p className="quietnote">下次更新：{tm(config.next_sync_at)}；最近尝试：{tm(config.last_attempt_at)}。刷新失败会保留原节点；共享节点只保留一份，人工停用不会被刷新撤销。</p>
  </form>;
}

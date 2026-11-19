import React, { useEffect, useId, useState } from 'react';
import { Button, Notice, useApp } from '../ui';
import { AccountPicker, Modal, accountName, accountIdentity } from '../components/Management';
import { useQuery } from '../query';
import ProxyPicker from './ProxyPicker';

const statusNames={disabled:'已停用',retired:'已退役',pending_kernel:'等待内核',untested:'待检测',ready:'就绪'};
export default function AccountProxyList({onClose,initialAccount=null,accountId=''}) {
  const {data,mutate}=useApp();
  const formId=useId();
  const [account,setAccount]=useState(initialAccount?.id||accountId||''),[identity,setIdentity]=useState(initialAccount),[draft,setDraft]=useState(null);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[success,setSuccess]=useState(''),[acknowledged,setAcknowledged]=useState(null);
  const locked=!!(initialAccount?.id||accountId);
  const accountDetail=useQuery(locked&&!identity&&account?`/api/accounts/${account}`:null,data.revision);
  const visibleIdentity=identity||accountDetail.result?.account||accountDetail.result;
  const binding=useQuery(account?`/api/accounts/${account}/proxy`:null,data.revision);
  useEffect(()=>{if(acknowledged&&binding.result?.proxy_id===acknowledged.proxy_id)setAcknowledged(null);},[binding.result,acknowledged]);
  const current=acknowledged||binding.result;
  const selected=draft??(current?{proxy_id:current.proxy_id||'',proxy_name:current.proxy_name||''}:null);
  const mode=selected?.proxy_id||selected?.fixed?'fixed':'inherit';
  const changed=!!selected&&(mode!=='fixed'||!!selected.proxy_id)&&selected.proxy_id!==(current?.proxy_id||'');
  const blocked=!account||!current||!!binding.error||busy;
  function chooseAccount(id,record){setAccount(id);setIdentity(record||null);setDraft(null);setAcknowledged(null);setError('');setSuccess('');}
  function chooseTarget(id,node){setDraft({proxy_id:id,proxy_name:node?.name||''});setError('');setSuccess('');}
  async function save(e){
    e.preventDefault();
    if(blocked||!changed)return;
    setBusy(true);setError('');setSuccess('');
    const saved={proxy_id:selected.proxy_id||null,proxy_name:selected.proxy_name||null,status:selected.proxy_id?'untested':null};
    try{
      await mutate(`/api/accounts/${account}/proxy`,'PUT',{proxy_id:saved.proxy_id},'账户出口已保存。');
      setAcknowledged(saved);setDraft(null);
      setSuccess(saved.proxy_id?`已将此账户固定到“${saved.proxy_name||saved.proxy_id}”。`:'已设为跟随平台默认出口。');
      binding.reload();
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  const footer=<div className="proxy-dialog-actions"><span>{busy?'正在保存，请等待结果…':!account?'尚未选择账户':mode==='fixed'&&!selected?.proxy_id?'请选择固定出口节点':changed?'有未保存的出口变更':success?'出口设置已保存':'当前未修改出口'}</span><div className="row"><Button disabled={busy} onClick={()=>onClose?.()}>{success?'完成':'取消'}</Button><Button type="submit" form={formId} variant="primary" disabled={blocked||!changed}>{busy?'正在保存…':'保存账户出口'}</Button></div></div>;
  return <Modal open onOpenChange={open=>!open&&!busy&&onClose?.()} size="medium" title="账户出口绑定" description="选择账户，核对当前出口，再确认绑定用途和目标节点。" footer={footer}><form id={formId} className="proxy-binding" onSubmit={save} aria-busy={busy}>
    <div className="proxy-binding-content">
      <section className="proxy-binding-section" aria-labelledby="proxy-binding-account">
        <div className="proxy-step"><span>1</span><h3 id="proxy-binding-account">选择账户</h3></div>
        <p className="proxy-help">{locked?'正在配置当前账户，绑定只作用于该账户。':'按原名称、平台或脱敏身份搜索，绑定只作用于选中的一个账户。'}</p>
        {!locked&&<fieldset disabled={busy} className="proxy-fieldset"><AccountPicker value={account} onChange={chooseAccount} revision={data.revision} label={account?'更换账户':'选择要绑定的账户'} required/></fieldset>}
        {visibleIdentity&&<p className="proxy-identity"><strong>{accountName(visibleIdentity)}</strong><span>{visibleIdentity.platform} · {accountIdentity(visibleIdentity)}</span></p>}{accountDetail.error&&<Notice>{accountDetail.error}<Button onClick={accountDetail.reload}>重试读取账户</Button></Notice>}
      </section>
      <section className="proxy-binding-section" aria-labelledby="proxy-binding-current">
        <div className="proxy-step"><span>2</span><h3 id="proxy-binding-current">当前出口</h3></div>
        <div className="proxy-current-route" role="status">{!account?'选择账户后读取当前出口，不会自动修改绑定。':binding.loading&&!current?'正在读取账户出口…':binding.error?'当前出口读取失败，请重试。':<><strong>{current?.proxy_id?(current.proxy_name||'原绑定节点已不可用'):'跟随平台默认出口'}</strong>{current?.status&&<span>{statusNames[current.status]||current.status}</span>}</>}</div>
        {binding.error&&<Notice>{binding.error}<Button onClick={binding.reload}>重试读取出口</Button></Notice>}
      </section>
      <section className="proxy-binding-section" aria-labelledby="proxy-binding-target">
        <div className="proxy-step"><span>3</span><h3 id="proxy-binding-target">目标出口</h3></div>
        {!account?<div className="proxy-empty-state"><strong>先选择账户，再配置出口</strong><p>可跟随平台默认策略，或固定到一个节点。选择和搜索不会保存，确认后点击下方“保存账户出口”。</p></div>:<fieldset disabled={blocked} className="proxy-fieldset">
          <div className="proxy-route-options">
            <label className={`proxy-route-option ${mode==='inherit'?'is-selected':''}`}><input type="radio" name="account-egress-mode" checked={mode==='inherit'} onChange={()=>chooseTarget('')}/><span><strong>跟随平台默认出口</strong><small>使用该平台已配置的直连、固定节点或代理池。</small></span></label>
            <label className={`proxy-route-option ${mode==='fixed'?'is-selected':''}`}><input type="radio" name="account-egress-mode" checked={mode==='fixed'} onChange={()=>{if(!selected?.proxy_id)setDraft({proxy_id:'',proxy_name:'',fixed:true});}}/><span><strong>固定到指定节点</strong><small>该节点停用或不可用时明确失败，不自动改为直连。</small></span></label>
          </div>
          {mode==='fixed'&&<ProxyPicker value={selected?.proxy_id||''} valueLabel={selected?.proxy_name||''} onChange={chooseTarget} disabled={blocked}/>}
        </fieldset>}
      </section>
      <aside className="proxy-binding-scope"><strong>绑定用途</strong><p>此设置用于该账户的新转发请求，以及选择“沿用账户出口”的维护任务。已有注册或重登会话继续保持建立时的固定出口；独立维护出口策略不在这里修改。</p></aside>
      {error&&<div role="alert"><Notice>{error}</Notice></div>}{success&&<div className="proxy-success" role="status">{success}</div>}
    </div>
  </form></Modal>;
}

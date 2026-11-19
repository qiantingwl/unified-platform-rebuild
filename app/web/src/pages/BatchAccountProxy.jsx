import React,{useId,useState} from 'react';
import {api} from '../api';
import {Button,Notice,useApp} from '../ui';
import {Modal,accountName,accountIdentity} from '../components/Management';
import ProxyPicker from './ProxyPicker';
import './proxy.css';

export default function BatchAccountProxy({accounts=[],onClose,onComplete}) {
  const {refresh}=useApp(),formId=useId();
  const [targets]=useState(()=>Array.from(new Map(accounts.filter(a=>a.id).map(a=>[a.id,a])).values()));
  const [mode,setMode]=useState('inherit'),[node,setNode]=useState(null),[confirmed,setConfirmed]=useState(false);
  const [busy,setBusy]=useState(false),[results,setResults]=useState({}),[error,setError]=useState('');
  const completed=Object.values(results).filter(r=>r.state==='success').length;
  const failed=Object.values(results).filter(r=>r.state==='failed').length;
  const remaining=targets.filter(a=>results[a.id]?.state!=='success');
  async function save(e){
    e.preventDefault();if(busy||!confirmed||!remaining.length||(mode==='fixed'&&!node?.id))return;
    setBusy(true);setError('');
    const next={...results};
    try{
      for(const account of remaining){
        next[account.id]={state:'running',text:'正在保存…'};setResults({...next});
        try{
          await api(`/api/accounts/${encodeURIComponent(account.id)}/proxy`,'PUT',{proxy_id:mode==='fixed'?node.id:null});
          next[account.id]={state:'success',text:mode==='fixed'?`已固定到 ${node.name}`:'已跟随平台默认出口'};
        }catch(e){
          next[account.id]={state:'failed',text:e.message};
          if(e.code==='login_required'){
            for(const pending of remaining.filter(a=>!next[a.id]||next[a.id].state==='running'))next[pending.id]={state:'failed',text:'登录已过期，本次未提交'};
            setResults({...next});break;
          }
        }
        setResults({...next});
      }
      await refresh();onComplete?.(next);
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  const started=Object.keys(results).length>0;
  const footer=<div className="proxy-dialog-actions"><span role="status">{busy?`正在保存 · 成功 ${completed} / ${targets.length}`:started?`成功 ${completed} · 失败 ${failed} · 总计 ${targets.length}`:`仅操作已勾选的 ${targets.length} 个账户`}</span><div className="row"><Button disabled={busy} onClick={onClose}>{started?'完成':'取消'}</Button><Button type="submit" form={formId} variant="primary" disabled={busy||!confirmed||!remaining.length||(mode==='fixed'&&!node?.id)}>{busy?'正在逐条保存…':started?'重试未成功项':'保存这些账户的出口'}</Button></div></div>;
  return <Modal open onOpenChange={open=>!open&&!busy&&onClose?.()} title="批量账户出口" description="仅修改当前明确勾选的账户，每条账户分别保存并反馈结果。" footer={footer}>
    <form id={formId} onSubmit={save} className="proxy-binding-content" aria-busy={busy}>
      <fieldset disabled={busy||started} className="proxy-fieldset"><div className="proxy-route-options">
        <label className={`proxy-route-option ${mode==='inherit'?'is-selected':''}`}><input type="radio" name={formId} checked={mode==='inherit'} onChange={()=>setMode('inherit')}/><span><strong>跟随平台默认出口</strong><small>分别沿用各账户所属平台的出口策略。</small></span></label>
        <label className={`proxy-route-option ${mode==='fixed'?'is-selected':''}`}><input type="radio" name={formId} checked={mode==='fixed'} onChange={()=>setMode('fixed')}/><span><strong>固定到指定节点</strong><small>节点不可用时明确失败，不自动改为直连。</small></span></label>
      </div>{mode==='fixed'&&<ProxyPicker value={node?.id||''} valueLabel={node?.name||''} disabled={busy||started} onChange={(id,record)=>setNode({id,name:record?.name||id})}/>}</fieldset>
      <aside className="proxy-binding-scope"><strong>绑定用途</strong><p>只影响所列账户的新转发请求及选择沿用账户出口的维护任务。已有注册或重登会话保持原出口，独立维护策略保持原设置。</p></aside>
      <div className="proxy-batch-accounts" aria-label="本次账户与逐条结果">{targets.map(a=><div className="proxy-batch-account" key={a.id}><div><strong>{accountName(a)}</strong><small>{a.platform} · {accountIdentity(a)}</small></div><span role="status" className={results[a.id]?.state==='failed'?'bad':''}>{results[a.id]?.text||'尚未提交'}</span></div>)}</div>
      {!targets.length&&<Notice>未选择账户，请返回账户列表勾选后再操作。</Notice>}
      <label className="row gap"><input type="checkbox" checked={confirmed} disabled={busy||started} onChange={e=>setConfirmed(e.target.checked)}/>确认只修改以上 {targets.length} 个已勾选账户</label>
      {error&&<Notice>{error}</Notice>}
    </form>
  </Modal>;
}

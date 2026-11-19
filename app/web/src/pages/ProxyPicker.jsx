import React,{useId,useState} from 'react';
import {useApp,Notice,Button} from '../ui';
import {Pagination} from '../components/Management';
import {useDebounced,useQuery,queryString} from '../query';
const statusNames={disabled:'已停用',retired:'已退役',pending_kernel:'等待内核',untested:'待检测',ready:'就绪'};
export default function ProxyPicker({value='',valueLabel='',onChange,multiple=false,selected=[],onSelected,disabled=false}) {
 const {data}=useApp();const radioGroup=useId();const [q,setQ]=useState(''),[page,setPage]=useState(1);const search=useDebounced(q);
 const query=useQuery(`/api/proxy-directory?${queryString({q:search,page,page_size:25})}`,data.revision);
 const nodes=query.result?.nodes||[];
 const selectedNode=nodes.find(n=>n.id===value);
 return <div className="proxy-node-picker">
  {!multiple&&<div className="proxy-picker-selection"><span>已选节点</span><strong>{value?(selectedNode?.name||valueLabel||`节点 ${value}`):'未固定节点'}</strong>{value&&!selectedNode&&<small>已选节点不在当前搜索页，仍保留此选择。</small>}</div>}
  <label className="field proxy-node-search">搜索节点<input aria-label="搜索出口节点" placeholder="输入节点名称" value={q} disabled={disabled} onChange={e=>{setQ(e.target.value);setPage(1);}}/></label>
  {query.error&&<Notice>{query.error}<Button onClick={query.reload}>重试</Button></Notice>}
  <div className="proxy-picker" aria-busy={query.loading}>
   {query.loading&&!query.result?<div className="proxy-empty-state">正在加载节点…</div>:nodes.length?nodes.map(n=>{const unavailable=!n.has_secret||['disabled','retired','pending_kernel'].includes(n.data.status);return <label className={`proxy-picker-row ${(multiple?selected.includes(n.id):value===n.id)?'is-selected':''}`} key={n.id}><input type={multiple?'checkbox':'radio'} name={multiple?undefined:radioGroup} value={n.id} checked={multiple?selected.includes(n.id):value===n.id} disabled={disabled||(unavailable&&!(multiple?selected.includes(n.id):value===n.id))} onChange={e=>multiple?onSelected(e.target.checked?[...selected,n.id]:selected.filter(id=>id!==n.id)):onChange(n.id,n)}/><span><strong>{n.name}</strong><small>{n.data.scheme} · {statusNames[n.data.status]||n.data.status||'状态未知'}{unavailable?'（不可选择）':''}</small></span></label>;}):!query.error&&<div className="proxy-empty-state"><strong>{q?'没有匹配的节点':'还没有节点'}</strong><p>{q?'请调整搜索词；现有选择不会因搜索而被清除。':'请先在节点页面添加节点或导入订阅，再回来选择固定出口。'}</p></div>}
  </div>
  {multiple&&<small>已选 {selected.length} 个；跨页保留明确勾选的节点ID。</small>}
  <Pagination page={page} pageSize={25} total={query.result?.total} loading={query.loading} onPage={setPage}/>
 </div>;
}

/* Same rule contract and golden-test vectors as opportunity.py; no network or storage. */
const Opportunity = (() => {
function officialReview(batch,number){
 const r=(batch.review_rows||{})[(number||'').toUpperCase()];
 if(r)return r;
 if(batch.qualification)return {status:'not_found',label:'未见于已核实名单，不能认定不合格',queue:'',published_at:batch.qualification.published_at||'',url:batch.qualification.url||''};
 return null;
}
function qualifiedReference(bundle,match,number){
 const batch=bundle.batches[match[0]],empty={available:false,total:null,ahead:null,rank:null,message:'本轮合格名单与原摇号记录尚未完整对应'};
 if(batch.access_context)return batch.access_context.qualified_reference||empty;
 const category=batch.c[match[1]];if(batch.review_only||batch.stale||category.issue||category.rank_scope==='multiple-records')return empty;
 const own=officialReview(batch,number);if(!own||own.status!=='qualified'||batch.qualification?.pending)return empty;
 const orders=[];
 for(const [no,r] of Object.entries(batch.review_rows||{})){
  if(r.status!=='qualified'||r.notice_id!==own.notice_id||r.band!==own.band)continue;
  const ms=(bundle.rows[no]||[]).filter(m=>m[0]===match[0]&&m[1]===match[1]);if(ms.length!==1)return empty;orders.push(ms[0][3]);
 }
 const stat=(batch.qualification?.bands||[]).find(b=>b.band===own.band)||{};
 if(stat.qualified!==orders.length||new Set(orders).size!==orders.length||!orders.includes(match[3]))return empty;
 const ahead=orders.filter(o=>o<match[3]).length;
 return {available:true,total:orders.length,ahead,rank:ahead+1,message:'本轮同档合格家庭按原摇号顺序的参考位置，未计入最新队列先后及优先规则，不是当前梯队或正式选房排名'};
}
function intentionContext(bundle,match,number){
 const batch=bundle.batches[match[0]],empty={complete:false,patterns:[],qualified_households:null},own=officialReview(batch,number);
 if(batch.access_context)return batch.access_context.intention_context||empty;
 if(!own||batch.qualification?.pending)return empty;
 const rows=Object.values(batch.review_rows||{}).filter(r=>r.status==='qualified'&&r.notice_id===own.notice_id&&r.band===own.band),known=rows.filter(r=>r.intentions_verified),stat=(batch.qualification?.bands||[]).find(b=>b.band===own.band)||{},counts={};
 for(const r of known){const key=JSON.stringify([...new Set(r.intentions||[])].sort());counts[key]=(counts[key]||0)+1;}
 return {complete:!!rows.length&&known.length===rows.length&&rows.length===stat.qualified,qualified_households:stat.qualified??null,patterns:Object.keys(counts).sort().map(k=>({projects:JSON.parse(k),households:counts[k]}))};
}
function intentionUnion(bundle,match,number,ids){
 const batch=bundle.batches[match[0]],ctx=intentionContext(bundle,match,number),names=new Set((batch.opportunity?.projects||[]).filter(p=>ids.includes(p.id)).map(p=>p.name));
 return {verified:ctx.complete&&!!names.size,households:ctx.complete&&names.size?ctx.patterns.filter(p=>p.projects.some(n=>names.has(n))).reduce((n,p)=>n+p.households,0):null,message:'按家庭去重计算所选项目意向并集，不是各项目人数相加，也不代表最终选房人数'};
}
function filteredReference(bundle,match,number,queue){
 const batch=bundle.batches[match[0]],empty={available:false,before:null,excluded:null,ahead:null,unreviewed_ahead:null,message:'本批核查资料或原队列顺序尚未完整核实，不能进行核查过滤假设'};
 if(batch.access_context)return batch.access_context.queue===queue?(batch.access_context.filtered_reference||empty):empty;
 const own=officialReview(batch,number);if(!own||own.status!=='qualified'||batch.qualification?.pending||batch.stale)return empty;
 const pos=queueReference(bundle,match,queue),rule=batch.opportunity?.rule||{};
 if(pos.ahead==null||!rule.verified)return empty;
 const ordering=rule.queue_order||batch.Q,earlier=new Set(),excluded=new Set(),unreviewed=new Set();
 for(const [no,ms] of Object.entries(bundle.rows)){
  if(no===number.toUpperCase())continue;
  const rr=ms.filter(m=>m[0]===match[0]&&m[1]===match[1]);if(rr.length>1)return empty;if(!rr.length)continue;
  const m=rr[0],q=batch.Q[m[2]];
  if(ordering.indexOf(q)<ordering.indexOf(queue)||(q===queue&&m[3]<match[3])){
   earlier.add(no);const r=(batch.review_rows||{})[no];
   if(r&&r.band===batch.c[match[1]].key&&['ineligible','withdrawn'].includes(r.status))excluded.add(no);
   if(!r||['not_reviewed','not_found','conflict'].includes(r.status))unreviewed.add(no);
  }
 }
 if(earlier.size!==pos.ahead)return empty;
 return {available:true,before:earlier.size,excluded:excluded.size,ahead:earlier.size-excluded.size,unreviewed_ahead:unreviewed.size,message:'沿用其他家庭原摇号队列，仅按本人确认队列调整本人；按本轮公示暂剔除不合格和撤销，未审核及未知家庭保留，公示更正可改变结果；非最新正式排序'};
}
function reviewPosition(bundle,match,number){
 const batch=bundle.batches[match[0]];
 if(batch.access_context)return {...batch.access_context.review_position};
 const result={verified:false,rank:null,ahead:null,queue:'',message:'最新完整队列及排序资料尚未公开或未核实，不能计算当前梯队排位'},own=officialReview(batch,number);
 if(batch.review_only)return result;
 if(!own)return result;result.queue=own.queue||'';
 if(own.status!=='qualified'||batch.qualification?.pending)return result;
 const current=[];
 for(const [no,matches] of Object.entries(bundle.rows)){
  const relevant=matches.filter(m=>m[0]===match[0]&&m[1]===match[1]);if(!relevant.length)continue;if(relevant.length!==1)return result;
  const r=officialReview(batch,no);if(!r||!['qualified','ineligible','withdrawn'].includes(r.status))return result;
  if(r.status==='qualified'){if(!batch.Q.includes(r.queue))return result;current.push([r.queue,relevant[0][3]]);}
 }
 const rule=batch.opportunity?.rule||{},ordering=rule.queue_order||batch.Q;
 if(!rule.verified||ordering.length!==new Set(ordering).size||batch.Q.some(q=>!ordering.includes(q))||ordering.length!==batch.Q.length)return result;
 const rank=1+current.filter(([q,o])=>q===own.queue&&o<match[3]).length,ahead=current.filter(([q,o])=>ordering.indexOf(q)<ordering.indexOf(own.queue)||(q===own.queue&&o<match[3])).length;
 return {...result,verified:true,rank,ahead,message:'按完整核查队列及原摇号顺序的参考位置，未替代正式选房排序'};
}
function priorityCandidate(rule,people,children){
 let conditions=rule.special_priority_conditions;
 if(conditions==null&&rule.id==='gm-2026-038-v2')conditions=[{min_children:3},{min_people:5,min_children:2},{people:4,min_children:2}];
 return (conditions||[]).some(c=>people>=(c.min_people??1)&&children>=(c.min_children??0)&&(c.people==null||people===c.people));
}
function queueReference(bundle,match,target){
 const batch=bundle.batches[match[0]],cat=batch.c[match[1]],original=match[2];target=target||batch.Q[original];
 if(batch.access_context){
  const ctx=batch.access_context;
  if(ctx.queue===target&&JSON.stringify(ctx.match)===JSON.stringify(match))return {...ctx.position};
  return {status:'pending',queue:target,rank:null,ahead:null,households:null,changed:target!==batch.Q[original],message:'请按当前队列重新查询，不能沿用其他查询的分析资料'};
 }
 const r={status:'pending',queue:target,rank:null,ahead:null,households:null,changed:target!==batch.Q[original],message:''};
 if(batch.review_only)return {...r,message:'本批仅有核查记录，尚无可核实的原摇号或队内排位'};
 if(!batch.Q.includes(target))return {...r,status:'invalid',message:'所选队列不属于本批公开队列'};
 if((cat.issue&&!cat.queue_complete)||cat.rank_scope==='multiple-records')return {...r,message:'名单存在异常或多轮歧义，暂不计算参考排位'};
 const index=batch.Q.indexOf(target);let rank;
 const rule=batch.opportunity?.rule||{},ordering=rule.verified?(rule.queue_order||batch.Q):batch.Q;
 if(ordering.length!==batch.Q.length||new Set(ordering).size!==batch.Q.length||batch.Q.some(q=>!ordering.includes(q)))return {...r,message:'本批队列先后规则尚未完整核实'};
 if(r.changed){const orders=(batch.R||{})[match[1]+'|'+index];
  if(!orders||orders.length!==(cat.q[target]||0)||orders.some((v,i)=>i&&v<orders[i-1]))return {...r,message:'目标队列顺序数据未完整核实，不能生成参考排位'};
  if(orders.includes(match[3]))return {...r,message:'目标队列存在相同顺序号，排序口径需核实'};
  let lo=0,hi=orders.length;while(lo<hi){const mid=(lo+hi)>>1;if(orders[mid]<match[3])lo=mid+1;else hi=mid;}rank=lo+1;
 }else rank=match[4];
 let higher=ordering.slice(0,ordering.indexOf(target)).reduce((n,q)=>n+(cat.q[q]||0),0);if(r.changed&&ordering.indexOf(batch.Q[original])<ordering.indexOf(target))higher--;
 return {...r,status:r.changed?'reference':'recorded',rank,ahead:higher+rank-1,households:(cat.q[target]||0)+(r.changed?1:0),message:r.changed?'仅调整本人队列，原摇号顺序号不变，其他家庭队列不变；参考排位不是最新正式排序':'公证名单当时的队内排位，非最终选房排序'};
}
function analyze(bundle, match, values) {
 const batch=bundle.batches[match[0]],cat=batch.c[match[1]],op=batch.opportunity||{},rule=op.rule||{},band=cat.key||'';
 const r={engine:'opportunity-v3',status:'pending',facts:{band:cat.l,queue:batch.Q[match[2]],order:match[3],rank:match[4],rank_stage:'公证摇号名单',initial_units:null},self_report:{},assumptions:{},missing:[],messages:[],scenario:null,
 sources:[...new Set([...(batch.sources||[batch.u||'']),rule.source||'',rule.priority_source||''])]};
 const review=officialReview(batch,values.apply_no||''),current=reviewPosition(bundle,match,values.apply_no||'');
 if(review){
  r.facts.review=review;
  const f=(batch.final_rows||{})[(values.apply_no||'').toUpperCase()],finalNewer=!!(f?.scope_verified&&(f.published_at||'')>=(review.published_at||''));
  const blocked={ineligible:'最新名单列为不合格，当前不按合格家庭演算',withdrawn:'最新名单列为撤销认租，当前不继续演算',not_reviewed:'尚未纳入本轮审核，请等待官方后续启动条件及安排',conflict:'最新名单记录存在差异，需先核对官方原文',not_found:'未见于已核实的最新名单，不能据此判断合格或不合格'};
  if(!finalNewer&&blocked[review.status]){r.messages.push(blocked[review.status]);return r;}
  if(!finalNewer&&batch.qualification?.pending&&!values.use_original_lottery){r.missing.push('最新核查资料尚未完整核实，暂不计算当前排位');return r;}
  if(!finalNewer&&!current.verified&&!values.use_original_lottery&&!values.use_review_filter){r.missing.push(current.message);return r;}
  if(!finalNewer&&current.verified&&values.queue!==current.queue&&!values.use_original_lottery&&!values.use_review_filter){r.missing.push('本人确认队列与本次公开名单记载不一致，两项分别保留，暂不能当作当前正式排序');return r;}
  if(values.use_review_filter&&values.use_original_lottery){r.status='invalid';r.messages.push('请选择一种参考演算方式');return r;}
 }
 if(batch.stale||cat.issue||cat.rank_scope==='multiple-records'){r.missing.push('名单存在异常，暂停计算');return r;}
 if(cat.has_annotations){r.missing.push('该档名单含额外附注，需核对适用规则后再分析上车机会');return r;}
 if(!rule.verified||!op.complete){r.missing.push('本批规则或项目房源尚未完成复核，仅展示已核实名单结果');return r;}
 const allowed=Object.fromEntries((op.projects||[]).filter(p=>p.rows.some(x=>x.band===band)).map(p=>[p.id,p]));
 const selected=values.projects||[];
 if(!selected.length||new Set(selected).size!==selected.length||selected.some(p=>!allowed[p])){r.status='invalid';r.messages.push('请确认本批此档实际申报的意向项目，不重复选择，也不跨档位');return r;}
 const capacity=selected.reduce((n,p)=>n+allowed[p].rows.filter(x=>x.band===band).reduce((a,x)=>a+x.units,0),0);
 if(capacity<=0||(cat.competition?.competition_ratio==null&&cat.competition?.missing?.length)){r.missing.push('所选范围房源或名单口径未完整核实，暂不演算');return r;}
 r.facts.initial_units=capacity;r.self_report.projects=selected.map(p=>allowed[p].name);
 r.facts.qualified_intention_union=intentionUnion(bundle,match,values.apply_no||'',selected);
 const people=values.people,children=values.children,integer=Number.isInteger;
 if(!integer(people)||!integer(children)||people<1||people>30||children<0||children>=people){r.status='invalid';r.messages.push('请按本批认定时点填写计租家庭人数和未成年子女人数');return r;}
 Object.assign(r.self_report,{people,children});
 if(people<((rule.minimum_people||{})[band]||1)){r.status='invalid';r.messages.push('自填人数低于本批该档面积标准，请核对官方申报记录；不会擅自更改名单档位');return r;}
 const priorityBand=band===rule.special_priority_band;
 r.self_report.priority_candidate=priorityBand&&priorityCandidate(rule,people,children);
 r.messages.push('家庭信息及意向由本人确认，未据此认定已通过官方资格审核');
 if(priorityBand){r.messages.push(rule.evidence);r.missing.push('未公开其他家庭优先分组及项目意向，不能确定优先重排后的本人位置');}
 const final=(batch.final_rows||{})[(values.apply_no||'').toUpperCase()];
 if(final&&(!review||(final.published_at||'')>=(review.published_at||''))){r.facts.final_selection_rank=final.rank;r.facts.final_source=final.url;r.messages.push('已核实官方选房排序，不再重复叠加家庭优先资格');}
 const basis=values.basis||'';let ahead,label;
 const position=queueReference(bundle,match,values.queue);
 if(position.status==='invalid'){r.status='invalid';r.messages.push(position.message);return r;}
 r.self_report.queue=position.queue;if(position.changed)r.messages.push('当前队列由本人确认；'+position.message);
 if(basis==='official-final'&&position.changed&&!review){r.missing.push('本人确认队列与公证名单不同，需核对最新正式排序，不直接沿用旧排序');return r;}
 if(basis==='official-final'&&final&&final.scope_verified){ahead=final.ahead_same_band;r.missing=r.missing.filter(m=>m!=='未公开其他家庭优先分组及项目意向，不能确定优先重排后的本人位置');label='按官方已公布选房排序，前序均通过且每户消耗一个目标名额的参考情景';}
 else if(basis==='preliminary'){if(position.ahead===null){r.missing.push(position.message);return r;}ahead=position.ahead;label=(position.changed?'按本人确认的'+position.queue+'及原摇号顺序插入，其他家庭队列不变；':'仅按公证名单队列顺序，')+'不额外重排特殊优先组；假设前序家庭均通过审核';
  if(review&&current.verified&&position.queue===current.queue&&!values.use_original_lottery){ahead=current.ahead;label='按完整已核查队列及原摇号顺序参考计算；已排除明确不合格和撤销家庭，不重复扣减';}
  else if(review){label='原摇号历史假设：'+label;r.messages.push('此情景沿用原摇号资料，不是最新审核后的排位；未套用总体合格率');}
  if(values.use_review_filter){const f=filteredReference(bundle,match,values.apply_no||'',position.queue);if(!f.available){r.missing.push(f.message);return r;}ahead=f.ahead;label=f.message;r.facts.qualification_filter=f;}
 }
 else if(basis==='custom'){ahead=values.ahead;if(!integer(ahead)||ahead<0||ahead>=cat.n){r.status='invalid';r.messages.push('假设前序户数应为本档人数范围内的非负整数，含更高队列及特殊优先家庭');return r;}label='本人自填的适用优先规则后前序户数，不是官方排位';}
 else{r.missing.push('请选择并确认一种演算假设；不会把初步排位自动当成最终排序');return r;}
 const diverted=values.not_consuming??null;
 if(diverted!==null&&(!integer(diverted)||diverted<0||diverted>ahead)){r.status='invalid';r.messages.push('不占用目标房源的假设户数必须介于0与前序户数之间');return r;}
 r.assumptions={basis,description:label,ahead,not_consuming:diverted,queue:position.queue,queue_rank_reference:position.rank,stock:'配租启动公告初始房源，非实时剩余库存',one_unit_per_household:true};
 r.scenario={required_not_consuming:Math.max(0,ahead-capacity+1),remaining_before_self:diverted===null?null:Math.max(0,capacity-(ahead-diverted)),within_capacity:diverted===null?null:capacity-(ahead-diverted)>=1};
 r.messages.push('不占用包括选择其他项目、审核未通过或放弃等，不等于这些家庭必须放弃；本结果不是个人成功概率');r.status='scenario';return r;
}
function querySummary(bundle,match,queue){
 const batch=bundle.batches[match[0]],cat=batch.c[match[1]],position=queueReference(bundle,match,queue);let overall=null;
 if(batch.review_only)return {published_at:batch.d||'',round:null,band:cat.l,order:match[3],overall_rank:null,total:cat.n,queue,queue_rank:null,queue_rank_is_reference:true,queue_message:position.message,initial_units:cat.u||null};
 if(batch.access_context&&batch.access_context.queue===queue&&JSON.stringify(batch.access_context.match)===JSON.stringify(match))return {...batch.access_context.summary};
 if(!batch.stale&&(cat.roster_complete||!cat.issue)&&cat.rank_scope!=='multiple-records'){
  const groups=batch.Q.map((_,i)=>(batch.R||{})[match[1]+'|'+i]||[]),orders=groups.flat();
  if(orders.length===cat.n&&new Set(orders).size===orders.length&&orders.filter(n=>n===match[3]).length===1&&groups.every(g=>g.every((n,i)=>!i||n>=g[i-1])))overall=orders.filter(n=>n<match[3]).length+1;
 }
 return {published_at:batch.d||'',round:match[6]||null,band:cat.l,order:match[3],overall_rank:overall,total:cat.households??cat.n,queue,queue_rank:batch.stale?null:position.rank,queue_rank_is_reference:position.changed||!!cat.has_annotations,queue_message:position.message,initial_units:cat.u||null};
}
function analyzeSimple(bundle,match,values){
 const batch=bundle.batches[match[0]],final=(batch.final_rows||{})[(values.apply_no||'').toUpperCase()],same=values.queue===batch.Q[match[2]];
 const review=officialReview(batch,values.apply_no||''),current=reviewPosition(bundle,match,values.apply_no||''),finalNewer=!!(final?.scope_verified&&(!review||(final.published_at||'')>=(review.published_at||'')));
 const scenarioValues={...values};
 const basis=finalNewer&&(same||review)?'official-final':'preliminary';
 if(basis==='official-final'&&batch.Q.includes(final.queue))scenarioValues.queue=final.queue;
 const r=analyze(bundle,match,{...scenarioValues,basis,not_consuming:0});
 if(basis==='official-final'&&final.queue&&final.queue!==values.queue)r.messages.push('本人确认队列与最新选房名单不同；本次正式排序演算采用名单记载的'+final.queue+'，不修改本人选择，请向官方核对差异。');
 if(!values.queue){r.status='invalid';r.scenario=null;r.messages=['请先选择本人队列'];r.missing=[];}
 const text={headline:'目前资料不足，暂不能判断能否轮到你',explanation:(r.missing.length?r.missing:r.messages).join('；'),needed_change:'',next_step:'核对官方核查结果和后续选房通知。',method:'按本批初始房源计算，不是实时剩余房源，也不代表最终选房结果。'};
 if(r.status==='invalid'){text.headline='请先核对填写信息';text.next_step='按本批官方申请记录确认小区、家庭人数和孩子人数后再分析。';}
 else if(r.scenario){const count=r.facts.initial_units,ahead=r.assumptions.ahead,needed=r.scenario.required_not_consuming,position=queueReference(bundle,match,values.queue);
  text.headline=needed?'按初始房源保守测算，暂时排不到你':'按初始房源保守测算，你在名额范围内';
  if(basis==='official-final')text.explanation='按已核实的正式排序，本档有'+ahead+'户排在你前面。你选择的小区在本档共有'+count+'套初始房源。';
  else{const own=(position.rank||1)-1,higher=ahead-own;text.explanation=(higher?'按队列先后顺序，前面队列有'+higher+'户，本队列在你前面有'+own+'户，共'+ahead+'户。':'本队列在你前面有'+own+'户。')+'你选择的小区在本档共有'+count+'套初始房源。';}
  const alternatives=(batch.opportunity?.projects||[]).some(p=>!(values.projects||[]).includes(p.id)&&p.rows.some(row=>row.band===batch.c[match[1]].key)),reasons=alternatives?'选择其他小区、未通过审核或放弃':'未通过审核或放弃等';
  text.needed_change=needed?'至少要有'+needed+'户前面家庭不选这些房源，才可能给你留下一套；可能是'+reasons+'，并不全是放弃。':'这是按前面家庭每户都选一套的算法；后续核查、优先排序和房源变化仍可能改变结果。';
  text.method='先按前面家庭全部通过、每户选一套计算；本人沿用原摇号顺序，其他家庭队列暂按公开名单，不自动重排特殊优先。'+text.method;
  if(review&&current.verified&&!values.use_original_lottery){text.explanation='按完整核查队列与原摇号顺序，本档参考前序为'+ahead+'户，已排除明确不合格及撤销家庭。所选范围初始房源'+count+'套。';text.method='依据最新完整核查记录参考计算；公示排序不等于正式选房排序，不重复扣减已排除家庭。';}
  else if(review){text.headline='原摇号假设：'+text.headline;text.method='此处是你主动选择查看的原摇号历史假设，不代表最新核查后位置。'+text.method;}
  if(values.use_review_filter&&r.facts.qualification_filter){const f=r.facts.qualification_filter;text.headline='核查过滤假设：'+(needed?'按初始房源暂时排不到':'按初始房源参考有留下一套的空间');text.explanation='沿用原名单队列并按本人所选队列调整本人，原参考前序'+f.before+'户，暂剔除'+f.excluded+'户不合格及撤销家庭后，参考前序为'+f.ahead+'户，其中'+f.unreviewed_ahead+'户未审核或信息待确认仍保留。所选范围初始房源'+count+'套。';text.method=f.message+'；不重复扣减已剔除家庭，不是个人成功概率。';}
  if(basis!=='official-final'&&r.self_report.priority_candidate){text.headline='你涉及家庭优先，暂不能只凭普通排名判断结果';text.next_step='你填写的家庭情况涉及本批特殊优先条款；先核实优先资格，实际位置要等最新核查及正式排序。';}
  else if(basis!=='official-final'&&r.missing.length){text.headline='本档涉及优先重排，实际位置需等核查及正式排序';text.next_step='以下仅按当前参考顺序演算，尚未计入未公开的优先分组，不能据此认定最终机会。';}
  else if(r.self_report.queue!==r.facts.queue)text.next_step='你选择的队列与公证名单的暂定分类不同，参考位置只调整了你本人；等最新完整核查、选房名单后再确认。';
 }
 r.readable=text;r.comparison=scenarioComparison(r);return r;
}
function scenarioComparison(r){
 if(r.status!=='scenario'||!r.scenario)return [];
 const ahead=r.assumptions.ahead,capacity=r.facts.initial_units;
 return [100,80,60].map(rate=>{const consuming=Math.floor((ahead*rate+99)/100);return {per_100:rate,consuming,remaining:Math.max(0,capacity-consuming),additional_non_consuming:Math.max(0,consuming-capacity+1),within_capacity:capacity-consuming>=1};});
}
return {analyze,queueReference,querySummary,analyzeSimple,scenarioComparison,officialReview,reviewPosition,qualifiedReference,filteredReference,intentionContext,intentionUnion};
})();
if(typeof module!=='undefined')module.exports=Opportunity;

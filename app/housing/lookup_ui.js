function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function officialLink(url,label){return /^https?:\/\//.test(url||'')?'<a href="'+esc(url)+'" target="_blank" rel="noopener">'+esc(label)+'</a>':'';}
let HITS=[],QUERY='',QUERY_QUEUE='',CHOSEN=-1,QUERY_REQUEST=0;
function field(k,v){return '<div class="k">'+esc(k)+'</div><div class="v">'+esc(v)+'</div>';}
function clearScenario(i){const el=$('scenario-'+i);if(el)el.innerHTML='';}
function refreshHandlingStatuses(){
 const now=typeof LookupAccess!=='undefined'?LookupAccess.now():Date.now();
 document.querySelectorAll('.handling-status').forEach(el=>{
  const w=JSON.parse(el.dataset.window),start=Date.parse(w.start+'+08:00'),end=Date.parse(w.end+'+08:00');
  let state=now<start?'未开始':now>end?'已截止':'办理中';
  if(now>=start&&now<=end){const dt=new Date(now+8*3600000),clock=String(dt.getUTCHours()).padStart(2,'0')+':'+String(dt.getUTCMinutes()).padStart(2,'0');
   if(w.office_sessions?.length&&!w.office_sessions.some(([a,b])=>a<=clock&&clock<=b))state='期间内，当前不在受理时段';
   else if(w.working_days_only)state='期间内，工作日按公告时段受理';
  }
  el.textContent=state;
 });
}
if(typeof window!=='undefined'&&typeof window.setInterval==='function')window.setInterval(refreshHandlingStatuses,10000);
function personalReviewDetails(batch,review){
 let h='';const unit=batch.qualification?.count_unit||'户';
 if(review?.status==='ineligible')h+=field('本人官方不合格原因',review.reason_verified&&review.reason?review.reason:'原因未公开或尚未完整核实，请查看本人官方名单记录');
 if(review?.intention_types?.length){
  h+='<h3>官方名单中的本人具体意向</h3>';
  const all=batch.review_statistics?.concrete_units||[];
  for(const t of review.intention_types){
   h+='<p>'+esc(t.project)+'｜'+esc(t.area)+'㎡｜'+esc(t.layout)+'</p>';
   const r=all.find(r=>r.project===t.project&&String(r.area).replace(/约|㎡|平方米/g,'')===t.area&&r.layout.replace('两房','二房')===t.layout);
   if(r)h+='<p class="note">初始'+esc(r.initial_units??'待核实')+'套｜'+(r.complete?'本轮合格意向':'已识别意向')+esc(r.qualified_intentions??'未核实')+esc(unit)+'｜意向／套：'+esc(r.ratio??'待核实')+'</p>';
  }
  h+='<p class="note">以上来自本轮官方名单；用户的项目选择和家庭情景另列，不覆盖原申报意向。多意向不能直接相加。</p>';
 }
 if(batch.handling_guidance?.length){h+='<h3>本人下一步</h3>'+batch.handling_guidance.filter(t=>!t.includes('｜')).map(t=>'<p>'+esc(t)+'</p>').join('');
  const labels={review:'核查公示',appeal:'异议／复核',supplement:'补正材料',withdrawal:'撤销认租',selection:'选房',signing:'签约'};
  for(const w of batch.handling||[]){
   const until=w.end.slice(5,10)+(w.time_verified?' '+w.end.slice(11,16):'（具体时刻见原文）');
   h+='<p>'+esc(labels[w.kind]||w.kind)+'：<span class="handling-status" data-window="'+esc(JSON.stringify(w))+'">'+esc(w.current_state||'按公告核对')+'</span>｜截至'+esc(until)+(w.scope?'｜'+esc(w.scope):'')+'</p>';
   if(w.office_sessions?.length)h+='<p class="note">'+(w.working_days_only?'工作日 ':'')+esc(w.office_sessions.map(x=>x.join('—')).join('／'))+'</p>';
   if(w.place)h+='<p class="note">办理地点：'+esc(w.place)+'</p>';
   h+='<p class="source">'+officialLink(w.url,'办理范围及期限原文')+'</p>';
  }
 }
 return h;
}
function reviewStatistics(batch,cat){
 const stats=batch.review_statistics;if(!stats)return '';
 const cross=(stats.original_queue_rows||[]).filter(r=>r.band===cat.key),projects=(stats.project_intentions||[]).filter(r=>r.band===cat.key);
 const count=n=>n==null?'未公开':String(n);
 let html='<details><summary>核查关联统计：原队列与项目意向</summary>';
 const unit=batch.qualification?.count_unit||'户';
 const concrete=(stats.concrete_units||[]).filter(r=>r.band===cat.key);
 if(concrete.length)html+='<h3>本档具体面积与户型竞争</h3><table><tr><th>房源</th><th>初始套数</th><th>合格意向'+esc(unit)+'</th><th>意向／套</th></tr>'+concrete.map(r=>'<tr><td>'+esc(r.project)+' '+esc(r.area)+' '+esc(r.layout)+'</td><td>'+esc(r.initial_units??'未核实')+'</td><td>'+esc(r.qualified_intentions??'未核实')+(r.complete?'':'（已识别）')+'</td><td>'+esc(r.ratio??'待核实')+'</td></tr>').join('')+'</table><p class="note">家庭可选择多个项目、户型，人数不能相加。初始房源非剩余库存，意向比不是个人成功概率。</p>';
 const reasons=batch.qualification?.reason_summary;
 if(reasons?.denominator)html+='<details><summary>本轮不合格原因概览</summary><p class="note">明确不合格'+esc(reasons.denominator)+esc(unit)+'；原因已解析'+esc(reasons.parsed)+esc(unit)+'，缺项'+esc(reasons.missing)+esc(unit)+'，覆盖率'+esc(reasons.coverage)+'%。</p><table><tr><th>原因类别</th><th>涉及数量</th><th>比例</th></tr>'+reasons.categories.slice(0,5).map(r=>'<tr><td>'+esc(r.label)+'</td><td>'+esc(r.households)+'</td><td>'+esc(r.percent)+'%</td></tr>').join('')+'</table><p class="note">'+esc(reasons.note)+'</p></details>';
 if(cross.length){html+='<h3>按原摇号分类关联</h3><table><tr><th>原队列</th><th>合格</th><th>不合格</th><th>撤销</th><th>未纳入本轮</th></tr>'+cross.map(r=>'<tr><td>'+esc(r.original_queue)+'</td>'+['qualified','ineligible','withdrawn','not_reviewed'].map(k=>'<td>'+esc(count(r[k]))+'</td>').join('')+'</tr>').join('')+'</table><p class="warn">这是核查结果按原名单分类的统计，不是最新各队列人数，不能据此判定本人当前队列或审核状态。</p>';}
 if(projects.length){html+='<h3>本档合格家庭项目意向</h3><table><tr><th>项目</th><th>合格意向户</th><th>初始套数</th><th>意向户／套</th></tr>'+projects.map(r=>'<tr><td>'+esc(r.project)+'</td><td>'+esc(r.qualified_households)+'</td><td>'+esc(r.initial_units??'待核实')+'</td><td>'+esc(r.ratio??'待核实')+'</td></tr>').join('')+'</table><p class="note">'+esc(stats.intentions_note||'家庭可多选，人数不能直接相加；意向不等于最终选择。')+'</p>';}
 if(stats.issue)html+='<p class="note">'+esc(stats.issue)+'</p>';
 return html+'</details>';
}
function queueProofs(batch,queue){
 const rule=(batch.opportunity||{}).rule||{},proofs=(rule.proofs||[]).filter(p=>p.startsWith(queue+'：'));
 return proofs.map(p=>'<p class="note">'+esc(p)+'</p>').join('')+'<p class="note">'+esc(rule.proofs_common||'')+' '+officialLink(rule.proof_source,'完整材料清单')+'。材料是否符合，以官方核查为准。</p>';
}
function resetQuery(){QUERY_REQUEST++;HITS=[];QUERY='';QUERY_QUEUE='';CHOSEN=-1;$('out').innerHTML='';$('out').style.display='none';}
function chooseRecord(i){CHOSEN=i;renderAll();}
function renderAll(){
 $('out').innerHTML='<div class="query-number">本次查询：'+esc(QUERY)+'<br>本人选择：'+esc(QUERY_QUEUE)+'</div>'+
 (HITS.length>1?'<p class="warn">有多条真实批次／轮次记录，请选择本次要分析的一条。</p>':'')+HITS.map((m,i)=>render(m,i)).join('');
 if(typeof document.querySelectorAll==='function')refreshHandlingStatuses();
}
function render(match,index){
 const batch=DATA.batches[match[0]],cat=batch.c[match[1]],summary=Opportunity.querySummary(DATA,match,QUERY_QUEUE);
 if(batch.review_only){
  const r=Opportunity.officialReview(batch,QUERY);
  let text='<div class="item"><h2>官方核查记录</h2>'+field('批次',batch.t);
  if(r)text+=field('最新已核实审核状态',r.label)+field('名单公布日期',(r.published_at||'').slice(0,10))+field('官方本次公开的队列',r.queue||'本次未公开')+'<div class="source">'+officialLink(r.url,'核查公告')+'</div>';
  text+=field('本人所选队列',QUERY_QUEUE+'（本人选择，未代为核验）')+field('名单面积档位',cat.l);
  text+='<p class="warn">本批暂未收录可核实的摇号名单或正式选房排序，目前可查询审核结果，暂不提供排名和机会分析。公示结果可能更正，合格不代表已获配房源。</p>';
  const s=(batch.qualification?.bands||[]).find(b=>b.band===cat.key);
  if(s)text+=field('本次名单范围内合格主体',s.qualified==null?'未完整公开':s.qualified+(batch.qualification?.count_unit||'户'));
  text+=personalReviewDetails(batch,r);
  return text+reviewStatistics(batch,cat)+'</div>';
 }
 if(batch.stale)return '<div class="item"><h2>历史名单待复核</h2>'+field('批次',batch.t)+field('旧版名单公布日期',batch.d)+'<p class="warn">该批最新来源尚未完整核实，暂不提供排名和上车分析。原记录保留，请以官方完整名单为准。</p><div class="source">'+(batch.sources||[batch.u]).map((u,i)=>officialLink(u,'官方原文'+(i+1))).join('｜')+'</div></div>';
 const rank=summary.queue_rank===null?'暂无法确认':'第 '+summary.queue_rank+' 位'+(summary.queue_rank_is_reference?'（参考）':'');
 let text='<div class="item"><h2>查询结果</h2>';
 if(HITS.length>1)text+='<label><input type="radio" name="record" '+(CHOSEN===index?'checked':'')+' onchange="chooseRecord('+index+')">分析这条记录</label>';
 text+=field('批次',batch.t);
 const review=Opportunity.officialReview(batch,QUERY),current=Opportunity.reviewPosition(DATA,match,QUERY),qualifiedRef=Opportunity.qualifiedReference(DATA,match,QUERY);
 const finalCurrent=(batch.final_rows||{})[QUERY],finalNewer=finalCurrent&&!batch.final_notice?.pending&&(!review||(finalCurrent.published_at||'')>=(review.published_at||''));
 if(finalNewer){
  text+='<h3>当前官方正式选房记录</h3>'+field('官方正式选房排序',finalCurrent.rank)+field('正式名单公布日期',(finalCurrent.published_at||'').slice(0,10))+field('最新选房名单记载队列',finalCurrent.queue||'未列明')+officialLink(finalCurrent.url,'正式排序原文');
  if(finalCurrent.queue&&finalCurrent.queue!==QUERY_QUEUE)text+='<p class="warn">最新选房名单记载'+esc(finalCurrent.queue)+'，与本人选择的'+esc(QUERY_QUEUE)+'不同。两项分别保留，请向官方核对；正式排位不解释为本人所选队列内排位。</p>';
 }
 const queueUpdate=batch.queue_updates?.[QUERY];
 if(queueUpdate)text+='<h3>官方队列调整记录</h3>'+field('调整名单公布日期',(queueUpdate.published_at||'').slice(0,10))+field('调整后队列',queueUpdate.queue)+officialLink(queueUpdate.url,'队列调整名单原文');
 if(batch.selection_status==='not_listed_in_verified_scope')text+='<p class="warn">本批已公布正式选房名单，但未在本次已核实范围内匹配到本人。不能据此认定资格不合格；请核对队列调整及后续审核、选房安排。</p>'+officialLink(batch.final_notice?.url,'本次选房公告');
 if(review){
  text+='<h3>'+(finalNewer?'前次资格核查记录':'最新官方审核结果')+'</h3><p class="conclusion"><strong>'+esc(review.label)+'</strong></p>';
  text+=field('审核名单公布日期',(review.published_at||'').slice(0,10));
  text+=field('阶段',review.phase==='final'?'终审结果':'公示结果，可能更正');
  text+=personalReviewDetails(batch,review);
  text+=field('官方本次公开的队列',review.queue||'本次名单未公开，不沿用旧名单认定');
  if(!review.queue)text+=field('本人确认的队列',QUERY_QUEUE+'（本人选择，非本次官方核验）');
  if(review.queue&&review.queue!==QUERY_QUEUE)text+='<p class="warn">本人确认队列与本次公开名单记载不同，两项分别保留；工具未核验最新系统变更，不用旧记录覆盖本人确认信息，也不据此推断审核状态。</p>';
  if(finalNewer)text+='<p class="note">以下核查统计对应'+esc((review.published_at||'').slice(0,10))+'公示来源；当前选房事实优先使用上方已公布正式名单，两个阶段不能混为同一版本。</p>';
  else if(current.verified)text+=field('公开核查名单内的队列参考位置','第 '+current.rank+' 位（原摇号顺序参考，非最新正式选房排序）');
  else text+=field('当前选房排位','尚无可核实的正式排序；附件序号不能当作选房排位');
  if(qualifiedRef.available){text+='<h3>本轮合格名单的原顺序参考</h3>'+field('同档合格家庭',qualifiedRef.total+'户')+field('其中原摇号顺序早于本人',qualifiedRef.ahead+'户')+field('仅按原摇号顺序排列的位置','第 '+qualifiedRef.rank+' 个／'+qualifiedRef.total+'户');text+='<p class="warn">'+esc(qualifiedRef.message)+'</p>';}
  const stats=(batch.qualification?.bands||[]).find(b=>b.band===cat.key);
  if(stats){const count=n=>n==null?'未公开':n+(batch.qualification?.count_unit||'户');text+='<h3>本档最新核查统计</h3>'+field('公示合格／不合格',count(stats.qualified)+'／'+count(stats.ineligible))+field('撤销／未纳入本轮审核',count(stats.withdrawn)+'／'+count(stats.not_reviewed));
   text+=field('本轮审核合格率',stats.pass_rate==null?'待核实':stats.pass_rate+'%（撤销和未审核不计入分母）');
   text+=field('本轮合格供需比',stats.qualified_ratio==null?'待核实':'约'+stats.qualified_ratio+'户／套（初始房源，非个人成功概率）');
  }
  if(batch.qualification?.pending)text+='<p class="warn">最新附件尚未完整核实，保留上次已验证资料；请查看官方原文。</p>';
  text+=reviewStatistics(batch,cat);
  text+='<div class="source">'+officialLink(review.url,'核查公告')+'</div><p class="note">未审核不等于不合格；公示合格不等于已获房源，正式选房排序以后续公告为准。</p>';
  text+='<details><summary>原摇号记录与申请阶段统计（历史）</summary>';
 }
 text+='<div class="rank-grid"><div class="rank-card">原名单综合摇号排名<strong>'+(summary.overall_rank===null?'待核实':'第 '+summary.overall_rank+' 位')+'</strong><span class="note">本档共'+summary.total+'户</span></div><div class="rank-card">'+esc(review?'原名单假设：所选'+QUERY_QUEUE+'参考位置':QUERY_QUEUE+'梯队排名')+'<strong>'+esc(rank)+'</strong></div></div>';
 if(review)text+=field('原摇号名单分类（历史）',batch.Q[match[2]]);
 text+=field('名单公布日期',summary.published_at)+field('原名单标记轮数',summary.round===null?'名单未列明':summary.round);
 text+=field('名单认租面积档位',summary.band)+field('摇号顺序号',summary.order);
 text+=field('本档初始房源',summary.initial_units===null?'尚未完整核实':summary.initial_units+'套（非实时剩余房源）');
 const competition=cat.competition;
 text+=field('本档申请竞争比',competition?.competition_ratio!=null?'约'+competition.competition_ratio+'户申请一套（不是个人成功概率）':'暂无法计算，同档房源或完整家庭数尚未可靠对应');
 if(competition?.missing?.length)text+='<p class="note">'+esc(competition.missing.join('；'))+'</p>';
 if(competition?.supply_url)text+='<div class="source">'+officialLink(competition.supply_url,'初始房源来源')+'</div>';
 text+=field('各队列申请统计',cat.queue_complete===false?'这份名单暂不能确认各队列准确户数，请查看官方原文。':batch.Q.filter(q=>cat.q[q]!==undefined).map(q=>q+'：'+cat.q[q]+'户').join('；'));
 text+='<p class="note">综合摇号排名是本档全部申请人的摇号排序；实际选房还要看队列先后、资格核查和适用优先规则。队列户数取公证名单当时的暂定分类。</p>';
 if(QUERY_QUEUE!==batch.Q[match[2]])text+='<p class="note">所选队列与名单暂定分类不同：参考排名只把你按原摇号顺序放入所选队列，其他家庭队列不变，不代表最新正式排位。</p>';
 if(cat.has_annotations)text+='<p class="warn">该档名单包含额外附注，当前排名只反映名单记录顺序，适用条件和正式排序请核对官方原文。</p>';
 if(summary.queue_rank===null)text+='<p class="warn">'+esc(summary.queue_message)+'</p>';
 const final=(batch.final_rows||{})[QUERY];
 if(final&&!review)text+=field('官方已公布的选房排序',final.rank)+'<div class="source">'+officialLink(final.url,'正式排序原文')+'</div>';
 if(cat.issue)text+=field('数据提示',cat.issue);
 if(batch.final_notice&&!batch.final_notice.verified)text+='<p class="warn">已有选房公告，正式排序尚未完整核实。'+officialLink(batch.final_notice.url,'查看选房公告')+'</p>';
 text+='<div class="source">'+(batch.sources||[batch.u]).map((u,i)=>officialLink(u,'官方名单'+(i+1))).join('｜')+'</div>';
 if(review)text+='</details>';
 const op=batch.opportunity||{},projects=(op.projects||[]).filter(p=>p.rows.some(r=>r.band===cat.key));
 text+='<details id="analysis-'+index+'"><summary>分析我的上车机会</summary>';
 if(!op.complete||!op.rule?.verified){return text+'<p class="warn">本批房源或规则还没核实完整，暂时不能判断能否轮到你。已查到的名单信息仍可正常查看。</p></details></div>';}
 text+='<div oninput="clearScenario('+index+')" onchange="clearScenario('+index+')"><h3>你当时勾选了哪些小区？</h3><p class="note">请与已提交的认租记录核对，不要勾选当时未申报的小区。</p>';
 if(review&&review.status==='qualified'&&!current.verified&&!finalNewer)text+='<p class="warn">已核实公示合格，但当前队列或完整排序未公开，不能直接给出最新上车结论。</p><label><input id="original-'+index+'" name="reference-'+index+'" type="radio">仅看原摇号假设（不是最新排位）</label>';
 if(review&&review.status==='qualified'&&!finalNewer)text+='<label><input id="review-filter-'+index+'" name="reference-'+index+'" type="radio">按本轮核查结果，扣除不合格和撤销家庭后看看</label><p class="note">仍沿用其他家庭的原队列，未审核及未知家庭保留；公示更正可能改变结果。</p>';
 text+=projects.map(p=>'<label class="project"><input type="checkbox" name="project-'+index+'" value="'+esc(p.id)+'" '+(projects.length===1?'checked':'')+'>'+esc(p.name)+'｜本档'+p.rows.filter(r=>r.band===cat.key).reduce((n,r)=>n+r.units,0)+'套</label>').join('');
 text+='<div class="row"><label>一起申请的家庭人数<input id="people-'+index+'" type="number" min="1" max="30" inputmode="numeric" placeholder="如：3"></label><label>其中未成年孩子有几个<input id="children-'+index+'" type="number" min="0" max="29" inputmode="numeric" placeholder="没有则填0"></label></div><p class="note">按本批公告认定的家庭成员和孩子年龄填写；不会根据孩子数量自动推算家庭人数。</p></div>';
 text+='<button type="button" onclick="runAnalysis('+index+')">看看目前能不能轮到我</button><div id="scenario-'+index+'" aria-live="polite"></div>';
 text+='<details><summary>核对所选队列的条件和材料</summary>'+queueProofs(batch,QUERY_QUEUE)+'</details></details>';
 return text+'</div>';
}
function numberOrNull(id){const v=$(id).value.trim();return v===''?null:Number(v);}
function runAnalysis(index){
 if(typeof LookupAccess!=='undefined'&&!LookupAccess.check())return;
 if(CHOSEN!==index){$('scenario-'+index).innerHTML='<p class="warn">请先选择要分析的那条批次／轮次记录。</p>';return;}
 const projects=Array.from(document.querySelectorAll('input[name="project-'+index+'"]:checked'),n=>n.value);
 const result=Opportunity.analyzeSimple(DATA,HITS[index],{apply_no:QUERY,queue:QUERY_QUEUE,projects,people:numberOrNull('people-'+index),children:numberOrNull('children-'+index),use_original_lottery:!!$('original-'+index)?.checked,use_review_filter:!!$('review-filter-'+index)?.checked}),r=result.readable;
 let text='<div class="result-box"><h3>分析结果</h3><p class="conclusion"><strong>'+esc(r.headline)+'</strong></p><p>'+esc(r.explanation)+'</p>';
 if(r.needed_change)text+='<p>'+esc(r.needed_change)+'</p>';
 const intent=result.facts.qualified_intention_union;
 if(intent?.verified)text+='<p>本轮合格家庭中，有'+intent.households+'户至少申报了你所选的一个项目（家庭并集）。</p><p class="note">'+esc(intent.message)+'</p>';
 text+='<p>'+esc(r.next_step)+'</p><p class="note">'+esc(r.method)+'</p>';
 for(const message of result.messages||[])if(message.includes('最新选房名单不同'))text+='<p class="warn">'+esc(message)+'</p>';
 if(result.comparison?.length){
  text+='<details class="comparison"><summary>如果前面有些家庭没有选这些房源呢？</summary><p class="warn">以下是假设对比，不是历史实测，也不是个人成功概率。均按当前参考顺序及初始房源计算。</p>';
  if(result.missing.length)text+='<p class="warn">'+esc(r.next_step)+'</p>';
  text+=result.comparison.map(s=>'<div class="scenario-card"><strong>前面每100户，最终'+s.per_100+'户占用这些房源</strong><p>本次假设前面'+s.consuming+'户占用房源。'+(s.within_capacity?'按该假设，轮到你前参考可留'+s.remaining+'套，存在留下一套的空间。':'按该假设，暂不能留下一套；还需'+s.additional_non_consuming+'户前面家庭不占用这些房源。')+'</p></div>').join('');
  text+='<p class="note">未通过审核、放弃或选择其他可选项目，统一计入未占用，不重复扣减。取整时按更多占用计算；未公开的优先重排仍可能改变位置。</p></details>';
 }
 if(result.self_report.priority_candidate)text+='<p class="note">'+esc(DATA.batches[HITS[index][0]].opportunity.rule.evidence)+'</p>';
 text+='<div class="source">'+result.sources.filter(Boolean).map((u,i)=>officialLink(u,'计算来源'+(i+1))).join('｜')+'</div></div>';
 $('scenario-'+index).innerHTML=text;
}
async function run(){
 const key=$('q').value.trim().toUpperCase().replace(/\s/g,''),queue=$('q-queue').value;
 resetQuery();$('out').style.display='block';
 if(!key){$('out').innerHTML='<p>请先输入认租编号。</p>';return;}
 if(!queue){$('out').innerHTML='<p>请先选择本人队列，再查询。</p>';return;}
 QUERY=key;QUERY_QUEUE=queue;
 if(typeof LookupAccess!=='undefined'){
  const request=++QUERY_REQUEST;$('out').textContent='正在查询…';
  try{
   const payload=await LookupAccess.query(key,queue);
   if(request!==QUERY_REQUEST||$('q').value.trim().toUpperCase().replace(/\s/g,'')!==key||$('q-queue').value!==queue)return;
   DATA.batches=payload.batches;DATA.rows=payload.rows;
  }catch(e){if(request===QUERY_REQUEST&&LookupAccess.check())$('out').textContent=e.message||'查询失败，请稍后重试。';return;}
 }
 const found=DATA.rows[key];
 if(!found){$('out').innerHTML='<div class="query-number">本次查询：'+esc(key)+'</div><p>当前收录未匹配到这个编号。请核对输入及官方附件；查不到不代表不合格。</p>';return;}
 HITS=found.map(m=>m.slice());CHOSEN=HITS.length===1?0:-1;renderAll();
}
function setQueueChoices(queues){$('q-queue').innerHTML='<option value="">请选择本人队列</option>'+queues.map(q=>'<option value="'+esc(q)+'">'+esc(q==='未标注'?'暂无明确队列':q)+'</option>').join('');}
setQueueChoices(Array.from(new Set(['第一队列','第二队列','第三队列',...DATA.batches.flatMap(b=>b.Q)])));
$('go').addEventListener('click',run);
$('q').addEventListener('input',resetQuery);
$('q-queue').addEventListener('change',resetQuery);
$('q').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.isComposing)run();});

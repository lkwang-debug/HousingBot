"""Rule/evidence-based scenarios, not probabilities. Browser parity lives in opportunity.js."""
from __future__ import annotations

import json
from bisect import bisect_left
from pathlib import Path

ENGINE_VERSION = "opportunity-v3"


def official_review(batch, number):
    record=batch.get('review_rows',{}).get(number.upper())
    if record:return record
    if batch.get('qualification'):
        return {'status':'not_found','label':'未见于已核实名单，不能认定不合格','queue':'',
                'published_at':batch['qualification'].get('published_at',''),'url':batch['qualification'].get('url','')}
    return None


def qualified_reference(bundle,match,number):
    batch=bundle['batches'][match[0]]
    empty={'available':False,'total':None,'ahead':None,'rank':None,'message':'本轮合格名单与原摇号记录尚未完整对应'}
    if batch.get('access_context'):
        return batch['access_context'].get('qualified_reference',empty)
    own=official_review(batch,number)
    category=batch['c'][match[1]]
    if batch.get('review_only') or batch.get('stale') or category.get('issue') or category.get('rank_scope')=='multiple-records':return empty
    if not own or own['status']!='qualified' or batch.get('qualification',{}).get('pending'):return empty
    orders=[]
    for no,r in batch.get('review_rows',{}).items():
        if r['status']!='qualified' or r.get('notice_id')!=own.get('notice_id') or r['band']!=own['band']:continue
        ms=[m for m in bundle['rows'].get(no,[]) if m[0]==match[0] and m[1]==match[1]]
        if len(ms)!=1:return empty
        orders.append(ms[0][3])
    stat=next((b for b in batch.get('qualification',{}).get('bands',[]) if b['band']==own['band']),{})
    if stat.get('qualified')!=len(orders) or len(set(orders))!=len(orders) or match[3] not in orders:return empty
    ahead=sum(o<match[3] for o in orders)
    return {'available':True,'total':len(orders),'ahead':ahead,'rank':ahead+1,
            'message':'本轮同档合格家庭按原摇号顺序的参考位置，未计入最新队列先后及优先规则，不是当前梯队或正式选房排名'}


def intention_context(bundle,match,number):
    from collections import Counter
    batch=bundle['batches'][match[0]];own=official_review(batch,number)
    empty={'complete':False,'patterns':[],'qualified_households':None}
    if batch.get('access_context'):return batch['access_context'].get('intention_context',empty)
    if not own or batch.get('qualification',{}).get('pending'):return empty
    rows=[r for r in batch.get('review_rows',{}).values() if r['status']=='qualified' and r.get('notice_id')==own.get('notice_id') and r['band']==own['band']]
    known=[r for r in rows if r.get('intentions_verified')]
    stat=next((b for b in batch.get('qualification',{}).get('bands',[]) if b['band']==own['band']),{})
    complete=bool(rows) and len(known)==len(rows)==stat.get('qualified')
    counts=Counter(tuple(sorted(set(r.get('intentions',[])))) for r in known)
    return {'complete':complete,'qualified_households':stat.get('qualified'),
            'patterns':[{'projects':list(p),'households':n} for p,n in sorted(counts.items())]}


def intention_union(bundle,match,number,project_ids):
    batch=bundle['batches'][match[0]];ctx=intention_context(bundle,match,number)
    names={p['name'] for p in batch.get('opportunity',{}).get('projects',[]) if p['id'] in project_ids}
    return {'verified':ctx['complete'] and bool(names),'households':sum(p['households'] for p in ctx['patterns'] if names.intersection(p['projects'])) if ctx['complete'] and names else None,
            'message':'按家庭去重计算所选项目意向并集，不是各项目人数相加，也不代表最终选房人数'}


def filtered_reference(bundle,match,number,queue):
    batch=bundle['batches'][match[0]]
    empty={'available':False,'before':None,'excluded':None,'ahead':None,'unreviewed_ahead':None,
           'message':'本批核查资料或原队列顺序尚未完整核实，不能进行核查过滤假设'}
    if batch.get('access_context'):
        return batch['access_context'].get('filtered_reference',empty) if batch['access_context']['queue']==queue else empty
    own=official_review(batch,number)
    if not own or own['status']!='qualified' or batch.get('qualification',{}).get('pending') or batch.get('stale'):return empty
    position=queue_reference(bundle,match,queue)
    rule=batch.get('opportunity',{}).get('rule',{})
    if position['ahead'] is None or not rule.get('verified'):return empty
    ordering=rule.get('queue_order') or batch['Q'];earlier={};excluded=set();unreviewed=set()
    for no,ms in bundle['rows'].items():
        if no==number.upper():continue
        relevant=[m for m in ms if m[0]==match[0] and m[1]==match[1]]
        if len(relevant)>1:return empty
        if not relevant:continue
        m=relevant[0];q=batch['Q'][m[2]]
        if ordering.index(q)<ordering.index(queue) or (q==queue and m[3]<match[3]):
            earlier[no]=m
            r=batch.get('review_rows',{}).get(no)
            if r and r.get('band')==batch['c'][match[1]]['key'] and r['status'] in {'ineligible','withdrawn'}:excluded.add(no)
            if not r or r['status'] in {'not_reviewed','not_found','conflict'}:unreviewed.add(no)
    if len(earlier)!=position['ahead']:return empty
    return {'available':True,'before':len(earlier),'excluded':len(excluded),'ahead':len(earlier)-len(excluded),
            'unreviewed_ahead':len(unreviewed),'message':'沿用其他家庭原摇号队列，仅按本人确认队列调整本人；按本轮公示暂剔除不合格和撤销，未审核及未知家庭保留，公示更正可改变结果；非最新正式排序'}


def review_position(bundle, match, number):
    """A current reference needs complete, explicit queues; no historic fallback."""
    batch=bundle['batches'][match[0]];category=batch['c'][match[1]]
    result={'verified':False,'rank':None,'ahead':None,'queue':'','message':'最新完整队列及排序资料尚未公开或未核实，不能计算当前梯队排位'}
    if batch.get('review_only'):return result
    own=official_review(batch,number)
    if not own:return result
    result['queue']=own.get('queue','')
    if own.get('status')!='qualified' or batch.get('qualification',{}).get('pending'):return result
    current=[]
    for no,matches in bundle['rows'].items():
        relevant=[m for m in matches if m[0]==match[0] and m[1]==match[1]]
        if not relevant:continue
        if len(relevant)!=1:return result
        r=official_review(batch,no)
        if not r or r['status'] not in {'qualified','ineligible','withdrawn'}:return result
        if r['status']=='qualified':
            if r.get('queue') not in batch['Q']:return result
            current.append((r['queue'],relevant[0][3]))
    rule=batch.get('opportunity',{}).get('rule',{})
    ordering=rule.get('queue_order') or batch['Q']
    if not rule.get('verified') or set(ordering)!=set(batch['Q']) or len(ordering)!=len(set(ordering)):return result
    rank=1+sum(q==own['queue'] and order<match[3] for q,order in current)
    ahead=sum(ordering.index(q)<ordering.index(own['queue']) or (q==own['queue'] and order<match[3]) for q,order in current)
    return dict(result,verified=True,rank=rank,ahead=ahead,message='按完整核查队列及原摇号顺序的参考位置，未替代正式选房排序')


def priority_candidate(rule, people, children):
    conditions = rule.get('special_priority_conditions')
    if conditions is None and rule.get('id') == 'gm-2026-038-v2':
        conditions = [{'min_children': 3}, {'min_people': 5, 'min_children': 2}, {'people': 4, 'min_children': 2}]
    return any(people >= c.get('min_people', 1) and children >= c.get('min_children', 0)
               and ('people' not in c or people == c['people']) for c in conditions or [])


def queue_reference(bundle, match, target=None):
    """Move only this household for a named scenario; never mutate public records."""
    batch=bundle['batches'][match[0]];category=batch['c'][match[1]]
    original=match[2];target=target or batch['Q'][original]
    result={'status':'pending','queue':target,'rank':None,'ahead':None,'households':None,
            'changed':target!=batch['Q'][original],'message':''}
    if batch.get('review_only'):return dict(result,message='本批仅有核查记录，尚无可核实的原摇号或队内排位')
    if target not in batch['Q']:
        return dict(result,status='invalid',message='所选队列不属于本批公开队列')
    if (category.get('issue') and not category.get('queue_complete')) or category.get('rank_scope')=='multiple-records':
        return dict(result,message='名单存在异常或多轮歧义，暂不计算参考排位')
    index=batch['Q'].index(target)
    rule=batch.get('opportunity',{}).get('rule',{})
    ordering=rule.get('queue_order',batch['Q']) if rule.get('verified') else batch['Q']
    if len(ordering)!=len(batch['Q']) or set(ordering)!=set(batch['Q']):
        return dict(result,message='本批队列先后规则尚未完整核实')
    if result['changed']:
        orders=batch.get('R',{}).get(f'{match[1]}|{index}')
        if orders is None or len(orders)!=category['q'].get(target,0) or orders!=sorted(orders):
            return dict(result,message='目标队列顺序数据未完整核实，不能生成参考排位')
        if match[3] in orders:
            return dict(result,message='目标队列存在相同顺序号，排序口径需核实')
        rank=bisect_left(orders,match[3])+1
    else: rank=match[4]
    higher=sum(category['q'].get(q,0) for q in ordering[:ordering.index(target)])
    if result['changed'] and ordering.index(batch['Q'][original])<ordering.index(target): higher-=1
    return dict(result,status='reference' if result['changed'] else 'recorded',rank=rank,
                ahead=higher+rank-1,households=category['q'].get(target,0)+int(result['changed']),
                message='仅调整本人队列，原摇号顺序号不变，其他家庭队列不变；参考排位不是最新正式排序' if result['changed'] else '公证名单当时的队内排位，非最终选房排序')


def analyze(bundle, match, values):
    batch = bundle["batches"][match[0]]
    category = batch["c"][match[1]]
    rule = batch.get("opportunity", {}).get("rule", {})
    inventory = batch.get("opportunity", {}).get("projects", [])
    band = category.get("key", "")
    result = {"engine": ENGINE_VERSION, "status": "pending", "facts": {
        "band": category["l"], "queue": batch["Q"][match[2]], "order": match[3], "rank": match[4],
        "rank_stage": "公证摇号名单", "initial_units": None}, "self_report": {}, "assumptions": {},
        "missing": [], "messages": [], "scenario": None,
        "sources": list(dict.fromkeys(batch.get("sources", [batch.get("u", "")]) +
                                        [rule.get("source", ""), rule.get("priority_source", "")]))}
    review=official_review(batch,values.get('apply_no',''))
    current=review_position(bundle,match,values.get('apply_no',''))
    if review:
        result['facts']['review']=review
        final_record=batch.get('final_rows',{}).get(values.get('apply_no','').upper())
        final_newer=bool(final_record and final_record.get('scope_verified') and
                         final_record.get('published_at','')>=review.get('published_at',''))
        blocked={'ineligible':'最新名单列为不合格，当前不按合格家庭演算',
                 'withdrawn':'最新名单列为撤销认租，当前不继续演算',
                 'not_reviewed':'尚未纳入本轮审核，请等待官方后续启动条件及安排',
                 'conflict':'最新名单记录存在差异，需先核对官方原文',
                 'not_found':'未见于已核实的最新名单，不能据此判断合格或不合格'}
        if not final_newer and review['status'] in blocked:
            result['messages'].append(blocked[review['status']]);return result
        if not final_newer and batch.get('qualification',{}).get('pending') and not values.get('use_original_lottery'):
            result['missing'].append('最新核查资料尚未完整核实，暂不计算当前排位');return result
        if not final_newer and not current['verified'] and not values.get('use_original_lottery') and not values.get('use_review_filter'):
            result['missing'].append(current['message']);return result
        if not final_newer and current['verified'] and values.get('queue')!=current['queue'] and not values.get('use_original_lottery') and not values.get('use_review_filter'):
            result['missing'].append('本人确认队列与本次公开名单记载不一致，两项分别保留，暂不能当作当前正式排序');return result
        if values.get('use_review_filter') and values.get('use_original_lottery'):
            result['status']='invalid';result['messages'].append('请选择一种参考演算方式');return result
    if batch.get('stale') or category.get("issue") or category.get("rank_scope") == "multiple-records":
        result["missing"].append("名单存在异常，暂停计算")
        return result
    if category.get('has_annotations'):
        result['missing'].append('该档名单含额外附注，需核对适用规则后再分析上车机会')
        return result
    if not rule.get("verified") or not batch.get("opportunity", {}).get("complete"):
        result["missing"].append("本批规则或项目房源尚未完成复核，仅展示已核实名单结果")
        return result
    projects = values.get("projects", [])
    allowed = {p["id"]: p for p in inventory if any(r["band"] == band for r in p["rows"])}
    if not projects or len(projects) != len(set(projects)) or any(p not in allowed for p in projects):
        result["status"] = "invalid"
        result["messages"].append("请确认本批此档实际申报的意向项目，不重复选择，也不跨档位")
        return result
    capacity = sum(r["units"] for p in projects for r in allowed[p]["rows"] if r["band"] == band)
    if capacity <= 0 or (category.get('competition', {}).get('competition_ratio') is None and category.get('competition', {}).get('missing')):
        result['missing'].append('所选范围房源或名单口径未完整核实，暂不演算')
        return result
    result["facts"]["initial_units"] = capacity
    result["self_report"]["projects"] = [allowed[p]["name"] for p in projects]
    result['facts']['qualified_intention_union']=intention_union(bundle,match,values.get('apply_no',''),projects)
    people, children = values.get("people"), values.get("children")
    integer = lambda n: isinstance(n, int) and not isinstance(n, bool)
    if not integer(people) or not integer(children) or not 1 <= people <= 30 or not 0 <= children < people:
        result["status"] = "invalid"
        result["messages"].append("请按本批认定时点填写计租家庭人数和未成年子女人数")
        return result
    result["self_report"].update(people=people, children=children)
    if people < rule.get("minimum_people", {}).get(band, 1):
        result["status"] = "invalid"
        result["messages"].append("自填人数低于本批该档面积标准，请核对官方申报记录；不会擅自更改名单档位")
        return result
    priority_band = band == rule.get("special_priority_band")
    candidate = priority_band and priority_candidate(rule, people, children)
    result["self_report"]["priority_candidate"] = candidate
    result["messages"].append("家庭信息及意向由本人确认，未据此认定已通过官方资格审核")
    if priority_band:
        result["messages"].append(rule["evidence"])
        result["missing"].append("未公开其他家庭优先分组及项目意向，不能确定优先重排后的本人位置")
    final = batch.get("final_rows", {}).get(values.get("apply_no", "").upper())
    if final and (not review or final.get('published_at','')>=review.get('published_at','')):
        result["facts"]["final_selection_rank"] = final["rank"]
        result["facts"]["final_source"] = final["url"]
        result["messages"].append("已核实官方选房排序，不再重复叠加家庭优先资格")
    basis = values.get("basis", "")
    position=queue_reference(bundle,match,values.get('queue'))
    if position['status']=='invalid':
        result['status']='invalid';result['messages'].append(position['message']);return result
    result['self_report']['queue']=position['queue']
    if position['changed']:result['messages'].append('当前队列由本人确认；'+position['message'])
    if basis == 'official-final' and position['changed'] and not review:
        result['missing'].append('本人确认队列与公证名单不同，需核对最新正式排序，不直接沿用旧排序')
        return result
    if basis == "official-final" and final and final.get("scope_verified"):
        ahead = final["ahead_same_band"]
        result['missing'] = [m for m in result['missing'] if m != '未公开其他家庭优先分组及项目意向，不能确定优先重排后的本人位置']
        label = "按官方已公布选房排序，前序均通过且每户消耗一个目标名额的参考情景"
    elif basis == "preliminary":
        if position['ahead'] is None:
            result['missing'].append(position['message']);return result
        ahead = position['ahead']
        label = (f"按本人确认的{position['queue']}及原摇号顺序插入，其他家庭队列不变；" if position['changed'] else "仅按公证名单队列顺序，")+"不额外重排特殊优先组；假设前序家庭均通过审核"
        if review and current['verified'] and position['queue']==current['queue'] and not values.get('use_original_lottery'):
            ahead=current['ahead'];label='按完整已核查队列及原摇号顺序参考计算；已排除明确不合格和撤销家庭，不重复扣减'
        elif review:
            label='原摇号历史假设：'+label
            result['messages'].append('此情景沿用原摇号资料，不是最新审核后的排位；未套用总体合格率')
        if values.get('use_review_filter'):
            filtered=filtered_reference(bundle,match,values.get('apply_no',''),position['queue'])
            if not filtered['available']:
                result['missing'].append(filtered['message']);return result
            ahead=filtered['ahead'];label=filtered['message']
            result['facts']['qualification_filter']=filtered
    elif basis == "custom":
        ahead = values.get("ahead")
        if not integer(ahead) or not 0 <= ahead < category["n"]:
            result["status"] = "invalid"
            result["messages"].append("假设前序户数应为本档人数范围内的非负整数，含更高队列及特殊优先家庭")
            return result
        label = "本人自填的适用优先规则后前序户数，不是官方排位"
    else:
        result["missing"].append("请选择并确认一种演算假设；不会把初步排位自动当成最终排序")
        return result
    diverted = values.get("not_consuming")
    if diverted is not None and (not integer(diverted) or not 0 <= diverted <= ahead):
        result["status"] = "invalid"
        result["messages"].append("不占用目标房源的假设户数必须介于0与前序户数之间")
        return result
    required = max(0, ahead - capacity + 1)
    result["assumptions"] = {"basis": basis, "description": label, "ahead": ahead, "not_consuming": diverted,
                             "queue":position['queue'],"queue_rank_reference":position['rank'],
                             "stock": "配租启动公告初始房源，非实时剩余库存", "one_unit_per_household": True}
    result["scenario"] = {"required_not_consuming": required,
                          "remaining_before_self": None if diverted is None else max(0, capacity - (ahead - diverted)),
                          "within_capacity": None if diverted is None else capacity - (ahead - diverted) >= 1}
    result["messages"].append("不占用包括选择其他项目、审核未通过或放弃等，不等于这些家庭必须放弃；本结果不是个人成功概率")
    result["status"] = "scenario"
    return result


def load_payload(path):
    text = Path(path).read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    marker = "const DATA = "
    if marker not in text: raise ValueError("不是本工具生成的查询页")
    return decoder.raw_decode(text.split(marker, 1)[1])[0]


def query_summary(bundle, match, queue):
    batch=bundle['batches'][match[0]];category=batch['c'][match[1]]
    position=queue_reference(bundle,match,queue)
    if batch.get('review_only'):
        return {'published_at':batch.get('d',''),'round':None,'band':category['l'],'order':match[3],
                'overall_rank':None,'total':category['n'],'queue':queue,'queue_rank':None,'queue_rank_is_reference':True,
                'queue_message':position['message'],'initial_units':category.get('u') or None}
    overall=None
    if not batch.get('stale') and (category.get('roster_complete') or not category.get('issue')) and category.get('rank_scope')!='multiple-records':
        groups=[batch.get('R',{}).get(f'{match[1]}|{i}',[]) for i in range(len(batch['Q']))]
        orders=[n for group in groups for n in group]
        if len(orders)==category['n'] and len(set(orders))==len(orders) and orders.count(match[3])==1:
            overall=sum(bisect_left(group,match[3]) for group in groups if group==sorted(group))+1 if all(group==sorted(group) for group in groups) else None
    return {'published_at':batch.get('d',''),'round':match[6] if len(match)>6 and match[6] else None,
            'band':category['l'],'order':match[3],'overall_rank':overall,'total':category.get('households',category['n']),
            'queue':queue,'queue_rank':None if batch.get('stale') else position['rank'],'queue_rank_is_reference':position['changed'] or bool(category.get('has_annotations')),
            'queue_message':position['message'],'initial_units':category.get('u') or None}


def analyze_simple(bundle, match, values):
    """Human-facing defaults and conclusion; advanced inputs stay out of the UI."""
    batch=bundle['batches'][match[0]]
    final=batch.get('final_rows',{}).get(values.get('apply_no','').upper())
    same_queue=values.get('queue')==batch['Q'][match[2]]
    review=official_review(batch,values.get('apply_no',''))
    current=review_position(bundle,match,values.get('apply_no',''))
    final_newer=bool(final and final.get('scope_verified') and (not review or final.get('published_at','')>=review.get('published_at','')))
    scenario_values=dict(values)
    basis='official-final' if final_newer and (same_queue or review) else 'preliminary'
    if basis=='official-final' and final.get('queue') in batch['Q']:
        scenario_values['queue']=final['queue']
    result=analyze(bundle,match,dict(scenario_values,basis=basis,not_consuming=0))
    if basis=='official-final' and final.get('queue') and final['queue']!=values.get('queue'):
        result['messages'].append('本人确认队列与最新选房名单不同；本次正式排序演算采用名单记载的'+final['queue']+'，不修改本人选择，请向官方核对差异。')
    if not values.get('queue'):
        result.update(status='invalid',scenario=None,messages=['请先选择本人队列'],missing=[])
    readable={'headline':'目前资料不足，暂不能判断能否轮到你','explanation':'；'.join(result['missing'] or result['messages']),
              'needed_change':'','next_step':'核对官方核查结果和后续选房通知。',
              'method':'按本批初始房源计算，不是实时剩余房源，也不代表最终选房结果。'}
    if result['status']=='invalid':
        readable.update(headline='请先核对填写信息',next_step='按本批官方申请记录确认小区、家庭人数和孩子人数后再分析。')
    elif result['scenario'] is not None:
        count=result['facts']['initial_units'];ahead=result['assumptions']['ahead'];needed=result['scenario']['required_not_consuming']
        readable['headline']='按初始房源保守测算，暂时排不到你' if needed else '按初始房源保守测算，你在名额范围内'
        position=queue_reference(bundle,match,values.get('queue'))
        if basis=='official-final':
            explanation=f'按已核实的正式排序，本档有{ahead}户排在你前面。你选择的小区在本档共有{count}套初始房源。'
        else:
            own=(position['rank'] or 1)-1;higher=ahead-own
            explanation=(f'按队列先后顺序，前面队列有{higher}户，本队列在你前面有{own}户，共{ahead}户。' if higher else f'本队列在你前面有{own}户。')+f'你选择的小区在本档共有{count}套初始房源。'
        readable['explanation']=explanation
        alternatives=any(p['id'] not in values.get('projects',[]) and any(row['band']==batch['c'][match[1]].get('key') for row in p['rows']) for p in batch.get('opportunity',{}).get('projects',[]))
        reasons='选择其他小区、未通过审核或放弃' if alternatives else '未通过审核或放弃等'
        readable['needed_change']=(f'至少要有{needed}户前面家庭不选这些房源，才可能给你留下一套；可能是{reasons}，并不全是放弃。' if needed else '这是按前面家庭每户都选一套的算法；后续核查、优先排序和房源变化仍可能改变结果。')
        readable['method']='先按前面家庭全部通过、每户选一套计算；本人沿用原摇号顺序，其他家庭队列暂按公开名单，不自动重排特殊优先。'+readable['method']
        if review and current['verified'] and not values.get('use_original_lottery'):
            readable['explanation']=f'按完整核查队列与原摇号顺序，本档参考前序为{ahead}户，已排除明确不合格及撤销家庭。所选范围初始房源{count}套。'
            readable['method']='依据最新完整核查记录参考计算；公示排序不等于正式选房排序，不重复扣减已排除家庭。'
        elif review:
            readable['headline']='原摇号假设：'+readable['headline']
            readable['method']='此处是你主动选择查看的原摇号历史假设，不代表最新核查后位置。'+readable['method']
        if values.get('use_review_filter') and result['facts'].get('qualification_filter'):
            f=result['facts']['qualification_filter']
            readable['headline']='核查过滤假设：'+('按初始房源暂时排不到' if needed else '按初始房源参考有留下一套的空间')
            readable['explanation']=f'沿用原名单队列并按本人所选队列调整本人，原参考前序{f["before"]}户，暂剔除{f["excluded"]}户不合格及撤销家庭后，参考前序为{f["ahead"]}户，其中{f["unreviewed_ahead"]}户未审核或信息待确认仍保留。所选范围初始房源{count}套。'
            readable['method']=f['message']+'；不重复扣减已剔除家庭，不是个人成功概率。'
        if basis != 'official-final' and result['self_report'].get('priority_candidate'):
            readable['headline']='你涉及家庭优先，暂不能只凭普通排名判断结果'
            readable['next_step']='你填写的家庭情况涉及本批特殊优先条款；先核实优先资格，实际位置要等最新核查及正式排序。'
        elif basis != 'official-final' and result['missing']:
            readable['headline']='本档涉及优先重排，实际位置需等核查及正式排序'
            readable['next_step']='以下仅按当前参考顺序演算，尚未计入未公开的优先分组，不能据此认定最终机会。'
        elif result['self_report'].get('queue')!=result['facts']['queue']:
            readable['next_step']='你选择的队列与公证名单的暂定分类不同，参考位置只调整了你本人；等最新完整核查、选房名单后再确认。'
    result['readable']=readable
    result['comparison']=scenario_comparison(result)
    return result


def scenario_comparison(result):
    """Sensitivity examples, not empirical rates or independent dropout events."""
    if result.get('status') != 'scenario' or not result.get('scenario'):
        return []
    ahead = result['assumptions']['ahead']
    capacity = result['facts']['initial_units']
    return [{'per_100': rate, 'consuming': (ahead * rate + 99) // 100,
             'remaining': max(0, capacity - (ahead * rate + 99) // 100),
             'additional_non_consuming': max(0, (ahead * rate + 99) // 100 - capacity + 1),
             'within_capacity': capacity - (ahead * rate + 99) // 100 >= 1}
            for rate in (100, 80, 60)]

"""One frozen mobile HTML report plus an equivalent plain/Markdown rendering."""
from __future__ import annotations

from html import escape
from datetime import datetime, timedelta
import re

from app.housing.report_render import ReportPart, HOUSING_LABELS, AUDIENCE_LABELS
from app.housing.report_compact import display_title, object_summary, _status
from app.housing.reporting import REGIONS, summarize
from app.housing.daily_lifecycle import area_clauses
from app.housing.daily_delta import validate_totals

LAYOUT='daily_changes_v4'


def link(text,url):
    if not isinstance(url,str) or not re.match(r'^https?://',url) or len(url)>4000:return escape(str(text))
    return '<a href="'+escape(url,quote=True)+'">'+escape(str(text)).replace('\n','<br>')+'</a>'


class Document:
    def __init__(self):self.text=[];self.html=[]
    def p(self,text,links=()):
        self.text.append(str(text)+('｜'+'｜'.join(f'[{label}]({url})' for label,url in links) if links else ''))
        self.html.append('<p>'+escape(str(text)).replace('\n','<br>')+('｜'+'｜'.join(link(label,url) for label,url in links) if links else '')+'</p>')
    def h(self,text,level=3):
        self.text.append('#'*level+' '+text);self.html.append(f'<h{level}>'+escape(text)+f'</h{level}>')
    def table(self,headers,rows,*,keep_numbers=False):
        def plain(cell):
            value=str(cell[0] if isinstance(cell,tuple) else cell).replace('|','／').replace('\n',' / ')
            return f'[{value}]({cell[1]})' if isinstance(cell,tuple) and isinstance(cell[1],str) and re.match(r'^https?://',cell[1]) else value
        def html(cell):return link(cell[0],cell[1]) if isinstance(cell,tuple) else escape(str(cell)).replace('\n','<br>')
        self.text.append('\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join('---' for _ in headers)+' |',
                      *['| '+' | '.join(plain(cell) for cell in row)+' |' for row in rows]]))
        self.html.append('<table width="100%" border="1" cellpadding="3"'+(' style="overflow-wrap:normal"' if keep_numbers else '')+'><tr bgcolor="#edf4fc">'+''.join(
            '<th>'+escape(h)+'</th>' for h in headers)+'</tr>'+''.join(
            '<tr>'+''.join('<td>'+html(cell)+'</td>' for cell in row)+'</tr>' for row in rows)+'</table>')
    def open(self,title):
        self.text.append('**'+title+'（展开）**');self.html.append('<details style="margin:12px 0;border:1px solid #d9e2ec;padding:10px"><summary>'+escape(title)+'</summary>')
    def close(self):self.html.append('</details>')


def _source(card):
    dated=[r for r in card.references if r.stage==card.stage and r.published_at]
    return max(dated,key=lambda r:(r.published_at,r.id)) if dated else next((r for r in card.references if r.id==card.batch_id),card.references[0] if card.references else None)


def _label(card):return f'【{HOUSING_LABELS[card.profile.housing_type]}｜{AUDIENCE_LABELS[card.profile.audience]}｜{card.code}】'+display_title(card)


def _daily_status(card,snapshot):
    value=_status(card,snapshot)
    windows=[w for w in card.action_windows if w['kind']==card.stage and datetime.fromisoformat(w['end'])>=snapshot.generated_at]
    if windows and card.stage in {'selection','signing'}:
        window=max(windows,key=lambda w:w['end'])
        value=value.replace('｜办理时间见原文（未核实）','')
        value=value.replace('选房通知','选房安排已公布')
        start=datetime.fromisoformat(window['start']);end=datetime.fromisoformat(window['end'])
        value+=f'；办理期：{start:%Y-%m-%d}—{end:%Y-%m-%d}'
        if window.get('time_verified'):value+=f' {end:%H:%M}截止'
    elif card.stage in {'selection','signing'}:
        past=[w for w in card.action_windows if w['kind']==card.stage]
        if past:
            window=max(past,key=lambda w:w['end'])
            value=value.replace('｜办理时间见原文（未核实）','')
            start=datetime.fromisoformat(window['start']);end=datetime.fromisoformat(window['end'])
            value+=f'；此前已收录办理期：{start:%Y-%m-%d}—{end:%Y-%m-%d}（已过；后续安排以本期公告为准）'
    if card.review:
        r=card.review
        if r.start and r.end:value+=f'；公示期：{r.start}—{r.end}'
        if r.eligible is not None:value+=f'；合格{r.eligible}户'
        if r.ineligible is not None:value+=f'／不合格{r.ineligible}户'
    return value


def review_tables(doc,review,now=None):
    doc.h('最新资格核查与竞争概览',4)
    doc.p(('终审结果' if review.get('phase')=='final' else '核查公示结果')+'｜公布：'+review.get('published_at','')[:10],
          [('核查公告',review['url'])] if review.get('url') else ())
    if review.get('pending'):doc.p('最新核查附件未完整核实；下表如有数据，为上一份已验证结果，不代表本次全部名单。')
    bands=review.get('bands',[])
    if not bands:
        doc.p('审核人数及竞争统计待附件完整核实；未列出的家庭不认定为不合格。');return
    unit=review.get('count_unit','户')
    doc.p('统计单位：'+unit+'；以下人数／单位数仅对应本批本轮公告范围。')
    overall=review.get('overall') or {}
    if overall.get('reviewed') is not None:
        doc.p(f'已出审核结论{overall["reviewed"]}{unit}：合格{overall["qualified"]}{unit}、不合格{overall["ineligible"]}{unit}；合格率{overall["pass_rate"]}%。撤销及未纳入本轮另列。')
    doc.table(['档位','合格','不合格','撤销','未纳入本轮审核'],[
        [b['label'].replace('的房源',''),*[b[k] if b[k] is not None else '未公开' for k in ('qualified','ineligible','withdrawn','not_reviewed')]] for b in bands],keep_numbers=True)
    if review.get('display_scope_note'):
        doc.p('为什么暂未审核？')
        doc.p(review['display_scope_note'],[('审核安排原文',review['url'])] if review.get('url') else ())
    ratio_bands=[b for b in bands if b['initial_units'] is not None or b['qualified_ratio'] is not None]
    if ratio_bands:doc.table(['档位','初始套数','本轮合格'+unit+'／套'],[
        [('本批' if b['band']=='all' else b['band'].replace('+','㎡及以上')+('㎡' if '+' not in b['band'] and b['band'].isdigit() else '')),
         b['initial_units'] if b['initial_units'] is not None else '待核实',b['qualified_ratio'] or '待核实'] for b in ratio_bands],keep_numbers=True)
    else:doc.p('同范围房源尚未可靠匹配，暂不计算本轮合格供需比。')
    if ratio_bands:doc.p('本轮合格户／套不包含尚未审核家庭，不能视为整批最终竞争比。')
    doc.table(['档位','已出审核结论','合格率'],[
        [b['label'].replace('的房源',''),b['reviewed'] if b['reviewed'] is not None else '未完整公开',b['pass_rate']+'%' if b['pass_rate'] is not None else '待核实'] for b in bands],keep_numbers=True)
    if review.get('queue_note'):doc.p(review['queue_note'])
    for b in bands:
        if b['queue_complete'] and b['queues']:
            doc.p(b['label']+'合格队列：'+'／'.join(q+str(n)+'户' for q,n in b['queues'].items()))
        if b.get('count_issue'):doc.p(b['label']+'：'+b['count_issue'])
    doc.p('口径：合格率＝合格÷（合格＋不合格）；撤销、未审核不放入不合格。合格户／套仅为本轮名单与初始房源之比，不是个人成功概率。')
    doc.p('未审核不等于淘汰；各轮审核覆盖范围以本批公告为准。公示结果可能更正，公示序号不是最终选房排序。')
    if now is not None:
        from app.housing.handling import display,guidance
        doc.h('按审核结果关注下一步',4)
        for status,label in [('qualified','合格'),('ineligible','不合格'),('not_reviewed','未纳入本轮')]:
            doc.p(label+'：'+guidance(status,[],now)[0])
        for w in review.get('handling',[]):
            doc.p(display(w,now),[('办理原文',w['url'])] if w.get('url') else ())
            if w.get('place'):doc.p('办理地点：'+w['place'])
        if not review.get('handling'):doc.p('办理期限尚未核实，请查看官方原文；不据此建议补交材料。')
    stats=review.get('statistics') or {}
    concrete=stats.get('concrete_units',[])
    if concrete:
        doc.open('具体面积与户型竞争（申报意向）')
        doc.table(['具体房源','初始套数','合格意向'+unit,'意向'+unit+'／套'],[
                [r['project']+' '+str(r['area'])+('' if any(s in str(r['area']) for s in ('㎡','平米','平方米')) else '㎡')+' '+r['layout'],r['initial_units'] if r['initial_units'] is not None else '待核实',
                 ('已识别' if not r['complete'] and r['qualified_intentions'] is not None else '')+str(r['qualified_intentions']) if r['qualified_intentions'] is not None else '未核实',r['ratio'] or '待核实'] for r in concrete],keep_numbers=True)
        doc.p('同一家庭／单位可选择多个项目或户型，各行不能相加。仅为本轮合格名单意向与初始房源之比，非剩余库存、最终选择或个人成功概率。')
        if any(not r['complete'] for r in concrete):doc.p('部分具体意向或供应未完整对应，仅展示已核实内容，不将缺失视为0。')
        doc.close()
    else:doc.p('具体面积、户型及其意向人数尚未可靠对应或未公开，暂保留本轮汇总。')
    reasons=review.get('reason_summary')
    if reasons and reasons['denominator']:
        doc.open('不合格原因分析')
        doc.p(f'本轮明确不合格{reasons["denominator"]}{unit}；原因已解析{reasons["parsed"]}{unit}，缺项{reasons["missing"]}{unit}，覆盖率{reasons["coverage"]}%。')
        doc.table(['原因类别TOP5','涉及'+unit+'数','占不合格比例'],[[r['label'],r['households'],r['percent']+'%'] for r in reasons['categories'][:5]],keep_numbers=True)
        doc.p(reasons['note'])
        if reasons.get('exact'):
            doc.open('完整原文原因组合')
            doc.table(['原文原因组合','数量'],[[r['reason'],r['households']] for r in reasons['exact']],keep_numbers=True)
            doc.p('此表按完整原因组合分组，一条已核实家庭／单位记录只计一次，不与类别涉及次数混用。')
            doc.close()
        doc.close()
    stats=review.get('statistics')
    if stats:
        doc.open('核查关联统计：原队列与项目意向')
        cross=stats.get('original_queue_rows',[])
        for band in dict.fromkeys(r['band'] for r in cross):
            doc.h(('本批' if band=='all' else band.replace('+','㎡及以上')+('㎡' if band.isdigit() else ''))+'｜按原摇号分类关联',4)
            rows=[r for r in cross if r['band']==band]
            doc.table(['原队列','合格','不合格','撤销','未纳入本轮'],[
                [r['original_queue'],*[r[k] if r[k] is not None else '未公开' for k in ('qualified','ineligible','withdrawn','not_reviewed')]] for r in rows],keep_numbers=True)
        if cross:doc.p('这是核查结果按原摇号分类的交叉统计，不是最新各队列人数。家庭当前队列可能不同，个人审核结果按编号查询。')
        intentions=stats.get('project_intentions',[])
        if intentions:
            doc.h('合格家庭项目意向',4)
            for band in dict.fromkeys(r['band'] for r in intentions):
                doc.p(('本批' if band=='all' else band.replace('+','㎡及以上')+('㎡' if band.isdigit() else ''))+'档')
                doc.table(['项目','合格意向户','初始套数','意向户／套'],[
                    [r['project'],r['qualified_households'],r['initial_units'] if r['initial_units'] is not None else '待核实',r['ratio'] or '待核实'] for r in intentions if r['band']==band],keep_numbers=True)
            doc.p(stats['intentions_note'])
            if any(not r['complete'] for r in intentions):doc.p('部分意向未完整识别，已识别人数可能不完整，不计算对应竞争比。')
        else:doc.p('本轮项目意向尚未可靠解析或未公开，不填0、不猜测。')
        if stats.get('issue'):doc.p(stats['issue'])
        if stats.get('unmatched_households') and cross:doc.p('存在尚未对应原摇号的记录，不将缺失队列推定为任一队列。')
        doc.close()


def competition_tables(doc,card,graphs=True,now=None):
    review=getattr(card,'qualification',None)
    if review:
        review_tables(doc,review,now)
        if card.lottery_breakdown:
            doc.open('申请阶段统计（原摇号名单）')
            lottery_tables(doc,card,graphs)
            doc.close()
        return
    lottery_tables(doc,card,graphs)


def lottery_tables(doc,card,graphs=True):
    items=card.lottery_breakdown
    if not items:return
    doc.h('摇号房源竞争概览',4)
    rows=[]
    def short_label(item):return item['label'].replace('的房源','').replace('单身公寓类','单身公寓').replace('一房类','一房')
    for i in items:
        rows.append([short_label(i),i.get('initial_units') if i.get('initial_units') is not None else '待核实',
            i.get('households') if i.get('households_verified') else '待核实',i.get('competition_ratio') or '待核实'])
    doc.table(['认租档位','初始套数','申请户数','户／套'],rows)
    queues=list(dict.fromkeys(q for i in items if i.get('queue_complete') for q in i.get('queues',{})))
    from app.housing.lottery import queue_sort_key
    queues.sort(key=queue_sort_key)
    for start in range(0,len(queues),3):
        group=queues[start:start+3]
        doc.table(['档位',*group],[[short_label(i),*[i['queues'].get(q,'待核实') if i.get('queue_complete') else '待核实' for q in group]] for i in items])
    if any(not i.get('queue_complete') for i in items):doc.p('队列信息未公开或未完整核实的档位不填0；明确未划分队列的批次按原文说明。')
    reliable=[i for i in items if i.get('competition_ratio') is not None]
    if graphs and len(reliable)>=2:
        doc.p('各档申请竞争比（同批对比）')
        maximum=max(float(i['competition_ratio']) for i in reliable)
        for i in reliable:
            label=i['label']+'：'+i['competition_ratio']+'户／套'
            doc.text.append(label)
            width=round(float(i['competition_ratio'])/maximum*100) if maximum else 0
            doc.html.append('<p>'+escape(label)+'<br><span style="display:block;background:#edf4fc;height:8px"><span style="display:block;background:#528bc5;height:8px;width:'+str(width)+'%"></span></span></p>')
    dates='、'.join(sorted({i.get('published_at','')[:10] for i in items}-{''}))
    urls=list(dict.fromkeys(i.get('url') for i in items if i.get('url')))
    doc.p('名单公布：'+dates+'。申请户数非审核合格人数；初始套数非实时剩余，竞争比不是个人成功概率。',[(f'名单{i+1}',u) for i,u in enumerate(urls)])
    supply=list(dict.fromkeys(i.get('supply_url') for i in items if i.get('supply_url')))
    if supply:doc.p('房源来源',[(str(i+1),u) for i,u in enumerate(supply)])


def _prices(value,key,policies=()):
    if isinstance(value,dict):
        return '；'.join(('口径'+str(policies.index(o['label'])+1) if o['label'] in policies else str(o['label']))+'：'+_prices(o['value'],key)[0]+' '+_prices(o['value'],key)[1] for o in value.get('options',[])),'按所列口径'
    unit='元/月' if key=='parking' else '元/㎡·月'
    if value is None:return '待核实',unit
    text=str(value)
    return (text[:-len(unit)],unit) if text.endswith(unit) else (text,'按原文' if re.search(r'元|平方米',text) else unit)


def fee_table(doc,fees,unpublished=(),omitted=(),policies=()):
    rows=[]
    for key,label in [('rent','执行租金'),('market','市场参考租金'),('property','物业管理费'),('maintenance','专项维修费用'),('parking','停车费')]:
        if key in omitted:continue
        value,unit=_prices(fees.get(key),key,policies)
        if key in unpublished and fees.get(key) is None:value='未公布'
        rows.append([label,value,unit])
    doc.table(['费用项目','标准','单位'],rows)


def inventory_tables(doc,card,*,open_section=True):
    inv=card.inventory
    if not inv:
        doc.p('房源、费用和完整条件尚未逐项核实，请查看本批原文。');return
    if open_section: doc.open('房源明细与官方费用')
    labels={'rent':'执行租金','market':'市场参考租金','property':'物业管理费','maintenance':'专项维修费用','parking':'停车费'}
    projects=inv.get('projects',[])
    common_missing=[k for k in labels if projects and all(p.get('fees',{}).get(k) is None and not any(r.get('fees',{}).get(k) is not None for r in p.get('rows',[])) and k not in p.get('unpublished_fields',[]) for p in projects)]
    if common_missing:doc.p('本批各项目以下费用尚未逐项核实，不按0计算：'+'、'.join(labels[k] for k in common_missing))
    policies=list(dict.fromkeys(o['label'] for p in projects for fees in [p.get('fees',{}),*[r.get('fees',{}) for r in p.get('rows',[])]] for value in fees.values() if isinstance(value,dict) for o in value.get('options',[])))
    for i,label in enumerate(policies):doc.p(f'租金口径{i+1}：'+label)
    compact_projects=len(projects)>8
    if compact_projects:
        rows=[];fee_rows=[];specific=[];notes=[]
        fee_keys=[key for key in labels if key not in common_missing]
        def fee_value(value,key):
            amount,unit=_prices(value,key,policies)
            return amount if amount=='待核实' or unit=='按所列口径' else amount+' '+unit
        for p in projects:
            doc.p(p['name']+'｜项目地址：'+(p.get('address') or '待核实'))
            for r in p.get('rows',[]) or [{'layout':'待核实','area':'待核实','units':'待核实','fees':{}}]:
                rows.append([p['name'],r.get('layout','待核实'),r.get('area','待核实'),r.get('units','待核实')])
                for key,value in r.get('fees',{}).items():
                    specific.append([p['name']+' '+r.get('layout','')+' '+r.get('area',''),labels.get(key,key),fee_value(value,key)])
                if r.get('notes'):notes.append(p['name']+' '+r.get('layout','')+' '+r.get('area','')+'：'+r['notes'])
            fee_rows.append([p['name'],*[('未公布' if key in p.get('unpublished_fields',[]) and p.get('fees',{}).get(key) is None else fee_value(p.get('fees',{}).get(key),key)) for key in fee_keys]])
        doc.table(['项目','实际户型','建筑面积','套数'],rows)
        if fee_keys:doc.table(['项目',*[labels[key] for key in fee_keys]],fee_rows)
        if specific:
            doc.p('以下户型另有明确标准，按具体户型适用，不将项目共用标准直接套用。')
            doc.table(['适用户型','费用项目','标准（含单位）'],specific)
        for note in notes:doc.p('房源说明：'+note)
    for p in ([] if compact_projects else projects):
        doc.h(p['name'],4);doc.p('项目地址：'+(p.get('address') or '待核实'))
        if p.get('rows'):
            doc.table(['实际户型','建筑面积','套数'],[[r['layout'],r['area'],r['units']] for r in p['rows']])
        else:doc.p('户型、面积及套数待核实。')
        fee_table(doc,p.get('fees',{}),p.get('unpublished_fields',[]),common_missing,policies)
        for row in p.get('rows',[]):
            if row.get('fees'):
                doc.p('下列标准仅适用于：'+row['layout']+'／'+row['area'])
                fee_table(doc,row['fees'],omitted=common_missing,policies=policies)
            if row.get('notes'):doc.p('房源说明：'+row['notes'])
    if inv.get('issues'):doc.p('数据提示：'+'；'.join(inv['issues']))
    if open_section: doc.close()
    from app.housing.rule_briefs import rule_brief
    for item in inv.get('evidence',[]):
        heading=item['heading']
        if '配租面积' in heading or '配租标准' in heading:
            doc.h('配租面积标准原文',4)
            number=0
            for clause in area_clauses(item['text']):
                if re.match(r'^(?:[一二三四五六七八九十两\d]+(?:至[一二三四五六七八九十两\d]+)?人(?:及以上|以上)?家庭|单身(?:居民|及以上家庭))',clause):
                    number+=1;doc.p(str(number)+'. '+clause)
                else:doc.p(clause)
        else:
            brief=rule_brief(item)
            if brief:doc.p(re.sub(r'^[一二三四五六七八九十]+[、．.]','',heading)+'：'+brief)
    rule=inv.get('rule',{})
    if rule.get('verified') and rule.get('evidence'):doc.p('特殊优先：'+rule['evidence'])
    if rule.get('proofs'):
        doc.open('队列条件与证明材料核对')
        for p in rule['proofs']:doc.p(p)
        if rule.get('proofs_common'):doc.p(rule['proofs_common'])
        doc.close()
    sources=list(dict.fromkeys(s['url'] for s in inv.get('sources',[]) if s.get('url')))
    doc.p('完整条件、费用及材料以本批原文为准',[(f'官方资料{i+1}',u) for i,u in enumerate(sources)])


def _metrics(summary):
    return '｜'.join(f'{label}{value}{unit}' for label,value,unit in [
        ('新增',summary.new_notices,'条'),('更新',summary.updated_notices,'条'),('摇号公布',summary.lottery_batches,'批'),('选房通知',summary.selection_batches,'批')] if value)


def progress_tables(doc,card,*,compact=False):
    progress=getattr(card,'selection_progress',None)
    if not progress:return
    doc.h('实际选房进度',4)
    if not progress['scopes']:
        from app.housing.selection_progress import catalog_rows
        rows=catalog_rows(getattr(card,'inventory',None))
        doc.p('官方实际选房结果暂未取得，已选、剩余均标“未核实”，不能按0理解。')
        if rows:
            doc.table(['小区及实际户型','初始供应','已选','剩余'],[
                [r['project']+'·约'+r['area']+'㎡'+r['layout'],r['initial_supply'],'未核实','未核实'] for r in rows],keep_numbers=True)
            doc.p('供应基数来自本批官方房源表；初始供应不等于当前剩余。')
        else:doc.p('本批实际户型供应也尚未完整核实，暂不列空表。')
        check=progress.get('check')
        if check:doc.p('最近核对：'+check['checked_at'].replace('T',' ')[:16]+'；'+('官方入口暂不可用，未取得新结果。' if check.get('error') else '尚未取得完整结果。'))
        return
    value=lambda n:str(n) if n is not None else '未核实'
    for scope in progress['scopes']:
        total=scope.get('totals');prefix=scope['round']+'｜'+scope['scope']+'：'
        if total:
            doc.p(prefix+f"累计已选{value(total['selected'])}套，剩余可选{value(total['remaining'])}套。")
        else:doc.p(prefix+'仅展示已识别范围，不作整批总计。')
        if scope.get('report_net') is not None:
            net=scope['report_net'];doc.p('较上期正式报表：累计已选'+('净增' if net>=0 else '净减')+str(abs(net))+'套；不等同于当日实际选房量。')
        elif scope.get('net_change'):
            net=scope['net_change'];doc.p(f"较前份快照（{net['from'].replace('T',' ')[:16]}—{net['to'].replace('T',' ')[:16]}）：已选净变化{net['selected']:+d}套。")
        if scope.get('correction_note'):doc.p(scope['correction_note'])
        stamp=scope.get('as_of');doc.p('数据截至：'+(stamp.replace('T',' ')[:16] if stamp else '官方未提供，不能视为实时进度')+'；来源：官方选房结果'+('（人工导入）' if scope.get('method')=='import' else '')+'。')
        if scope.get('latest_incomplete'):doc.p(scope['latest_incomplete'])
        if scope.get('daily_selected'):doc.p(scope['daily_selected']['day']+'实际选定：'+str(scope['daily_selected']['count'])+'套（依据逐套选定日期）。')
        rows=[]
        for r in scope['rows']:
            label=r['project']+('·约'+r['area']+'㎡'+r['layout'] if r['level']=='type' else '（共享池，无法拆分户型）' if r['level']=='pool' else '（仅公开项目汇总，无法拆分户型）')
            if r.get('not_started'):label+='（该档尚未开始选房）'
            if r.get('pool'):label+='（共享池）'
            rows.append([label,value(r.get('supply')),value(r.get('selected')),value(r.get('remaining'))])
        doc.table(['小区及实际户型','本轮供应','已选','剩余'],rows,keep_numbers=True)
        if total:doc.p('本范围合计：供应'+value(total['supply'])+'套；已选'+value(total['selected'])+'套；剩余'+value(total['remaining'])+'套。')
        if not scope.get('complete'):doc.p('资料未覆盖完整范围；已识别数量不等于整批人数或套数。')
        families=scope.get('families',[])
        if families:
            doc.table(['日期／场次','安排家庭','选定家庭','选定率'],[
                [r['day']+' '+r['session'],r['planned'],r['selected'],r['rate']+'%' if r['rate'] is not None else '未计算'] for r in families])
            doc.p('按同场安排家庭计算；未匹配选定结果的差额不能直接认定弃选。')
            abandoned=[r for r in families if r.get('abandoned') is not None]
            for r in abandoned:doc.p(r['day']+' '+r['session']+'官方明确弃选：'+str(r['abandoned'])+'户。')
    check=progress.get('check')
    if check:
        doc.p('进度最近检查：'+check['checked_at'].replace('T',' ')[:16]+('；'+check['error']+'。保留上份已核实资料，非实时。' if check.get('error') else ''))


def batch_detail(doc,c,snapshot,*,graphs=True,launch_materials=None,compact_progress=False):
    """Shared native tables, clauses and links for daily AND instant reports."""
    doc.h(c.profile.region+'｜'+_label(c),4)
    reasons=c.delta_reasons or [f'本次新增{sum(r.is_new for r in c.references)}条／更新{sum(r.is_update for r in c.references)}条']
    doc.p('；'.join(reasons));doc.p(_daily_status(c,snapshot));doc.p('配租对象概要：'+object_summary(c))
    if c.units is not None:doc.p(f'本批已核实初始房源{c.units}套')
    progress_tables(doc,c,compact=compact_progress)
    if c.profile.project_regions:doc.p('项目所在地：'+'、'.join(c.profile.project_regions))
    if c.profile.issues:doc.p('待核实：'+'；'.join(c.profile.issues))
    if c.review and (c.review.eligible is not None or c.review.ineligible is not None):
        doc.p('本次核查公告范围：'+('合格'+str(c.review.eligible)+'户' if c.review.eligible is not None else '合格人数待核实')+'／'+('不合格'+str(c.review.ineligible)+'户' if c.review.ineligible is not None else '不合格人数待核实'))
    show_materials=c.is_open if launch_materials is None else launch_materials
    if show_materials:inventory_tables(doc,c)
    if c.lottery_breakdown or c.qualification:
        if compact_progress and getattr(c,'selection_progress',None):
            doc.p('本期优先完整展示选房进度；核查和历史竞争资料保留。')
        else:competition_tables(doc,c,graphs,now=snapshot.generated_at)
    refs=[r for r in c.references if r.is_new or r.is_update or r.id==c.batch_id]
    hints={'applying':'核对本批完整资格，按认租窗口办理并保存申请编号。',
        'announced':'先核对条件与材料，认租尚未开始；按公告窗口办理。',
        'review':'核对本人公示结果；公示、异议／复核、撤销等各有期限及范围，按本批办理事项核对。',
        'lottery':'核对认租编号及名单；摇号顺序不等于最终选房排序。',
        'selection':'核对本人名单、选房场次、材料及签约期限。',
        'signing':'按官方通知核对签约名单、时间、材料和办理地点。'}
    hint=hints.get(c.stage,'核对本次公告及已核实的办理安排，时间不明确时以官方原文为准。')
    doc.p('办理提示：'+hint,[(('配租原文' if r.id==c.batch_id else '本期公告'),r.url) for r in refs])


def tracked_materials(doc, card, *, graphs=False, include_inventory=True,now=None,compact_progress=False):
    """Keep the full source-backed material reachable for unchanged directory rows."""
    # Full tables are attached to unchanged lottery rows (the main user need
    # called out in the report). Older non-lottery rows stay as compact directory
    # entries so the single-message budget remains safe.
    if not card.lottery_breakdown and not card.qualification:
        return
    doc.open(f'{card.code}｜'+('核查结果、竞争参考与办理事项' if card.qualification else '竞争概览与配租资料'))
    if compact_progress and getattr(card,'selection_progress',None):
        doc.p('核查及历史竞争资料已保留；本期优先完整展示实际选房进度。',[
            ('核查公告',card.qualification['url'])] if card.qualification else [])
        doc.close()
        return
    if card.lottery_breakdown or card.qualification:
        competition_tables(doc, card, graphs=graphs,now=now)
    if card.inventory and include_inventory:
        inventory_tables(doc, card, open_section=False)
    elif card.inventory:
        doc.p('房源明细、费用及配租面积标准已核实，受单条消息容量限制请查看批次原文。')
    doc.close()


def render_daily(snapshot,*,max_chars=18000,max_bytes=54000,graphs=True,compact_rest=False,compact_progress=False):
    validate_totals(snapshot)
    doc=Document();morning=snapshot.kind=='digest';total=snapshot.totals
    focus=[c for c in snapshot.cards if c.changed]
    history_new=len({r.id for c in snapshot.history_events for r in c.references if r.is_new})
    tasks=[];date_only=set()
    day=snapshot.generated_at.date()+(timedelta(days=0) if morning else timedelta(days=1))
    for c in snapshot.cards:
        if c.is_open and c.end and c.end.date() in {day,snapshot.generated_at.date()}:
            tasks.append((c,'认租截止',c.end))
        elif not morning and c.is_open and c.end and c.end.date()==snapshot.generated_at.date()+timedelta(days=3):
            tasks.append((c,'3天后认租截止',c.end))
        if c.start and c.start.date()==day and c.stage=='announced':tasks.append((c,'开始认租',c.start))
        for a in c.action_windows:
            end=datetime.fromisoformat(a['end']);start=datetime.fromisoformat(a['start'])
            from app.housing.handling import LABELS
            if end>=snapshot.generated_at and end.date() in ({day,snapshot.generated_at.date()} if not morning else {day}):
                label=LABELS[a['kind']]+'截止';tasks.append((c,label,end))
                if not a.get('time_verified'):date_only.add((c.code,label,end))
            if end>=snapshot.generated_at and start.date()==day:
                label=LABELS[a['kind']]+'开始';tasks.append((c,label,start))
                date_only.add((c.code,label,start))
    title=f"{'🏠 深圳住房管家早报' if morning else '🌙 深圳住房管家晚报'} {snapshot.generated_at:%m-%d}"
    doc.h(title,2)
    doc.p(f"比较区间：{snapshot.window_start:%m-%d %H:%M}—{snapshot.generated_at:%m-%d %H:%M}（北京时间）"+
          ('；相对上一期正式报表' if snapshot.baseline_at else '；首次采用24小时窗口'))
    bad=any(int(n) for n in re.findall(r'(?:异常|未检查)\s*(\d+)',snapshot.source_status))
    if bad:doc.p('⚠ 部分来源异常或尚未检查，以下只代表已核实信息。')
    if not focus and not history_new:doc.p('较上期未发现已核实的新认租、摇号或选房安排。' if snapshot.baseline_at else '近24小时未发现已核实的新认租、摇号或选房安排。')
    doc.p(f'已采集范围：认租中{total.open_batches}批｜跟进{total.in_transit_batches}批'+('｜'+_metrics(total) if _metrics(total) else ''))
    if snapshot.transition_note:doc.p(snapshot.transition_note)
    if history_new:doc.p(f'另有历史批次新公告{history_new}条，单独列示，不计入当前跟进批数。')
    partition='｜'.join(HOUSING_LABELS[k]+f'认租{s.open_batches}批／跟进{s.in_transit_batches}批' for k,s in snapshot.housing_totals.items() if s.in_transit_batches or s.open_batches)
    doc.p(partition or '当前无已核实的跟进批次')
    doc.open('四类保租房与对象概览')
    for key,label in [('city_baozu','市级保租房'),('district_baozu','区级保租房'),('personal_baozu','保租房中的个人配租'),('enterprise_baozu','保租房中的单位定向配租')]:
        s=snapshot.categories[key];doc.p(f'{label}：认租{s.open_batches}批｜跟进{s.in_transit_batches}批'+('｜'+_metrics(s) if _metrics(s) else ''))
    if snapshot.categories.get('unlevel_baozu') and snapshot.categories['unlevel_baozu'].in_transit_batches:
        s=snapshot.categories['unlevel_baozu'];doc.p(f'组织层级待核实保租房：认租{s.open_batches}批｜跟进{s.in_transit_batches}批')
    for kind,s in snapshot.housing_totals.items():
        if s.in_transit_batches:doc.p(HOUSING_LABELS[kind]+f'对象分布：个人{s.personal_batches}批／单位{s.enterprise_batches}批／待核实{s.unknown_batches}批')
    doc.p('四类不能直接相加：保租房层级与对象存在交叉；对象分布以本栏目跟进批次为基数。');doc.close()
    doc.h('今天要办' if morning else '明日提醒与当晚事项')
    task_keys=set()
    for c,label,stamp in tasks:
        key=(c.code,label,stamp)
        if key in task_keys:continue
        when='今天' if stamp.date()==snapshot.generated_at.date() else '明天' if stamp.date()==snapshot.generated_at.date()+timedelta(days=1) else ''
        stamp_text=stamp.strftime('%m-%d')+'（具体时刻见原文）' if key in date_only else stamp.strftime('%m-%d %H:%M')
        task_keys.add(key);doc.p(f'{c.profile.region}［{c.code}］{when}{stamp_text} {label}')
    if not tasks:doc.p('今天暂无已核实的截止或办理事项。' if morning else '明日暂无已核实的截止或办理事项。')
    def region_key(c):return (-1 if c.profile.level=='city' else REGIONS.index(c.profile.region) if c.profile.region in REGIONS else 99,c.code)
    previous_section=None
    for c in sorted(focus,key=region_key):
        section='一、市级配租' if c.profile.level=='city' else '二、各区配租'
        if section!=previous_section:doc.h(section);previous_section=section
        batch_detail(doc,c,snapshot,graphs=graphs,compact_progress=compact_progress)
    opportunities=[c for c in snapshot.cards if c.is_open and c not in focus]
    if opportunities:
        doc.h('认租机会简表')
        rows=[]
        for c in sorted(opportunities,key=region_key):
            ref=_source(c)
            name=f'{c.code} {c.profile.region} {HOUSING_LABELS[c.profile.housing_type]}\n{AUDIENCE_LABELS[c.profile.audience]}·{object_summary(c)}'
            rows.append([(name,ref.url if ref else ''),c.end.strftime('%m-%d %H:%M') if c.end else '待核实',str(c.units)+'套' if c.units is not None else '待核实'])
        doc.table(['批次及对象','认租截止','房源'],rows)
        doc.p('完整项目、费用和资格请查看对应批次原文；对象概要不表示已符合申请条件。')
    rest=[c for c in snapshot.cards if c not in focus and c not in opportunities]
    if rest:
        progress_cards=[c for c in rest if getattr(c,'selection_progress',None)]
        if progress_cards:
            doc.h('选房进度')
            for c in sorted(progress_cards,key=region_key):
                doc.h(c.profile.region+'｜'+c.code+' '+display_title(c),4)
                progress_tables(doc,c)
        doc.open(f'当前跟进目录，共{len(rest)}批')
        for region in ['深圳市',*REGIONS,'']:
            selected=[c for c in rest if c.profile.region==region]
            if not selected:continue
            doc.h('市级' if region=='深圳市' else region or '地区待核实',4)
            rows=[]
            for c in selected:
                ref=_source(c);label=c.code+' '+HOUSING_LABELS[c.profile.housing_type]+' '+display_title(c)
                rows.append([(label,ref.url if ref else ''),_daily_status(c,snapshot),ref.published_at.strftime('%m-%d') if ref and ref.published_at else '待核实'])
            doc.table(['批次','当前状态','最近节点'],rows)
            for c in selected:
                tracked_materials(doc,c,graphs=False,include_inventory=not compact_rest,now=snapshot.generated_at,compact_progress=compact_progress)
        doc.close()
    for c in snapshot.history_events:
        doc.p('历史批次新公告：'+c.code+' '+c.title,[(r.title,r.url) for r in c.references if r.is_new])
    if snapshot.lookup_url:doc.p('已登记发布的自助查询入口',[('查看排位与分析',snapshot.lookup_url)])
    if snapshot.lookup_url and snapshot.lookup_pending:doc.p('本地有新资料待云端发布；查询入口暂为已登记旧版，不能视为已同步本期全部数据。')
    doc.p('跟进包含可申请、有效办理及近期等待批次；不代表仍可申请。已退出历史不计入当前统计。完整条件及否定／例外见官方原文。')
    doc.p(snapshot.source_status+'｜最近检查：'+(snapshot.source_updated_at.strftime('%m-%d %H:%M') if snapshot.source_updated_at else '暂无记录'))
    content='\n\n'.join(doc.text);html='<div style="font-size:15px;line-height:1.7;overflow-wrap:anywhere">'+''.join(doc.html)+'</div>'
    # HTML permits these end tags to be omitted immediately before the next
    # cell/row/block. This keeps all text/cells while fitting one PushPlus body.
    for tag,following in [('td','td|/tr'),('th','th|/tr'),('tr','tr|/table'),('p','p|h[1-6]|details|/details|table|/div')]:
        html=re.sub('</'+tag+r'>(?=<(?:'+following+')[ >])','',html)
    if len(content)>max_chars or len(content.encode())>max_bytes or len(html.encode('utf-16-le'))//2>19800:
        if graphs:return render_daily(snapshot,max_chars=max_chars,max_bytes=max_bytes,graphs=False,compact_rest=True,compact_progress=compact_progress)
        if not compact_progress:return render_daily(snapshot,max_chars=max_chars,max_bytes=max_bytes,graphs=False,compact_rest=True,compact_progress=True)
        raise ValueError('完整日报超过单条容量；未截断、未拆条，需核对本期内容')
    return ReportPart(title,content,tuple(sorted({i for c in [*snapshot.cards,*snapshot.history_events] for i in c.notice_ids})),
                      tuple(c.code for c in snapshot.cards),wire_html=html,
                      deadline_notice_ids=tuple(sorted({i for c,label,stamp in tasks if '认租截止' in label for i in c.notice_ids})))

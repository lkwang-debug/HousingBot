"""Instant packets use the same native components as the daily reports."""
from __future__ import annotations

from dataclasses import replace
from collections import Counter

from app.housing.daily_render import Document,batch_detail
from app.housing.report_render import ReportPart,HOUSING_LABELS
from app.housing.reporting import REGIONS

LAYOUT='instant_tables_v1'


def _render(snapshot,cards,*,graphs=True):
    doc=Document();title=f'🔔 深圳住房通告 {snapshot.generated_at:%m-%d}'
    doc.h(title,2)
    doc.p(f'生成：{snapshot.generated_at:%Y-%m-%d %H:%M}（北京时间）')
    doc.p('统计范围：仅本次触发的公告事件；不代表全市当前认租总量。')
    refs={r.id:r for c in cards for r in c.references if r.is_new or r.is_update}
    batches=[c for c in cards if c.batch_id is not None]
    new=sum(r.is_new for r in refs.values());updated=sum(r.is_update for r in refs.values())
    doc.p(f'本次涉及{len(batches)}批｜新增{new}条｜更新{updated}条｜其中认租中{sum(c.is_open for c in batches)}批')
    if any(c.batch_id is None for c in cards):doc.p('未关联公告单独展示，不计为配租批次。')
    partition=Counter(c.profile.housing_type for c in batches)
    if partition:doc.p('｜'.join(HOUSING_LABELS[k]+str(n)+'批' for k,n in partition.items()))
    doc.open('分类与配租对象概览')
    for kind,count in partition.items():
        selected=[c for c in batches if c.profile.housing_type==kind]
        audience=Counter(c.profile.audience for c in selected)
        doc.p(f'{HOUSING_LABELS[kind]}：本次{count}批｜认租中{sum(c.is_open for c in selected)}批｜个人{audience["personal"]}批／单位{audience["enterprise"]}批／对象待核实{audience["unknown"]}批')
    baozu=[c for c in batches if c.profile.housing_type=='baozu']
    if baozu:
        views=[('市级保租房',lambda c:c.profile.level=='city'),('区级保租房',lambda c:c.profile.level=='district'),
               ('保租房中的个人配租',lambda c:c.profile.audience=='personal'),('保租房中的单位定向配租',lambda c:c.profile.audience=='enterprise')]
        for label,predicate in views:
            selected=[c for c in baozu if predicate(c)]
            if selected:doc.p(f'{label}：本次{len(selected)}批｜认租中{sum(c.is_open for c in selected)}批')
        doc.p('保租房层级与对象存在交叉，不能相加。')
    else:doc.p('本次未涉及已确认保租房，不展开零值栏目。')
    doc.close()
    def order(c):return (-1 if c.profile.level=='city' else REGIONS.index(c.profile.region) if c.profile.region in REGIONS else 99,c.code)
    previous=None
    for c in sorted(cards,key=order):
        section='市级配租' if c.profile.level=='city' else '各区配租' if c.profile.level=='district' else '分类待核实'
        if section!=previous:doc.h(section);previous=section
        current_launch=any(r.id==c.batch_id and (r.is_new or r.is_update) for r in c.references)
        materials=bool(current_launch and c.stage in {'applying','announced','unverified_window'})
        batch_detail(doc,c,snapshot,graphs=graphs,launch_materials=materials)
    if snapshot.lookup_url and any(c.code in snapshot.queryable_codes for c in cards):
        doc.p('已登记发布的自助查询入口',[('查看排位与分析',snapshot.lookup_url)])
    doc.p(snapshot.source_status+'｜最近检查：'+(snapshot.source_updated_at.strftime('%m-%d %H:%M') if snapshot.source_updated_at else '暂无记录'))
    doc.p('对象为概要，完整资格及否定／例外见原文；公告公布不代表申请人已完成该阶段。')
    content='\n\n'.join(doc.text)
    html='<div style="font-size:15px;line-height:1.7;overflow-wrap:anywhere">'+''.join(doc.html)+'</div>'
    return ReportPart(title,content,tuple(sorted({i for c in cards for i in c.notice_ids})),tuple(c.code for c in cards),wire_html=html)


def render_instant(snapshot,*,max_chars=18000,max_bytes=54000):
    if not 2000<=max_chars<=18000 or not 6000<=max_bytes<=54000:raise ValueError('即时消息预算超出允许范围')
    involved=[c for c in snapshot.cards if c.changed]
    if not involved:return []
    def fits(part,reserve=0):
        return len(part.content)+reserve<=max_chars and len(part.content.encode())+reserve*3<=max_bytes and len(part.wire_html.encode('utf-16-le'))//2+reserve<=19800
    def candidate(cards):
        part=_render(snapshot,cards)
        if not fits(part,80):part=_render(snapshot,cards,graphs=False)
        return part
    packets=[];current=[]
    for card in involved:
        combined=candidate([*current,card])
        if fits(combined,80):current.append(card);continue
        if current:packets.append(candidate(current));current=[]
        single=candidate([card])
        if not fits(single,80):raise ValueError(f'{card.code}完整通告超出单条容量；未截断资料，未入队')
        current=[card]
    if current:packets.append(candidate(current))
    if len(packets)>1:
        labelled=[]
        for i,p in enumerate(packets,1):
            label=f'第{i}部分／共{len(packets)}部分；本部分统计仅含对应批次。'
            from html import escape
            labelled.append(replace(p,title=p.title+f'（{i}/{len(packets)}）',content=label+'\n\n'+p.content,
                                    wire_html='<p>'+escape(label)+'</p>'+p.wire_html))
        packets=labelled
    if not all(fits(p) for p in packets):raise ValueError('即时通告最终容量校验失败，未入队')
    return packets

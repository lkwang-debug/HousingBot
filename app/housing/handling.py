"""Explicit handling windows and display state; no inferred permission to appeal."""
from datetime import datetime,timedelta
import re

LABELS={'review':'核查公示','appeal':'异议／复核','supplement':'补正材料','withdrawal':'撤销认租',
        'selection':'选房','signing':'签约'}
DATE=re.compile(r'(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\s*(\d{1,2})\s*[:：]\s*(\d{2}))?')
KINDS={'appeal':r'异议|复核','supplement':r'补正|补交|补充材料','withdrawal':r'撤销.{0,5}认租|撤销申请|撤销本批|放弃本次认租'}


def extra_windows(notice):
    if not notice.published_at or notice.published_unverified:return []
    result=[]
    # Keep paragraph boundaries: a period in an address or section number is
    # not permission to borrow the public-notice period for an appeal window.
    clauses=[re.sub(r'\s+','',t) for t in (notice.body_text or '').split('。') if t.strip()]
    for i,clause in enumerate(clauses):
        for kind,pattern in KINDS.items():
            if not re.search(pattern,clause):continue
            text=clause
            if not DATE.search(text) and len(text)<35 and i+1<len(clauses):text+=clauses[i+1]
            if kind=='withdrawal' and not re.search(r'如需|应于|时间为|可于|须于|网上撤销|期间内|申请时间|撤销时间|受理时间',text):continue
            if kind=='supplement' and re.search(r'不设补正|不得补|不予补|不接受补|不能补',text):continue
            matches=list(DATE.finditer(text))
            if not matches:continue
            a=matches[0];b=matches[1] if len(matches)>1 and re.fullmatch(r'(?:至|到|—|–|-|－|~|～|起至)',text[a.end():matches[1].start()]) else a
            year=int(a[1] or notice.published_at.year);end_year=int(b[1] or year)
            if b is not a and not b[1] and int(b[2])<int(a[2]):end_year+=1
            try:
                start=datetime(year,int(a[2]),int(a[3]),int(a[4] or 0),int(a[5] or 0))
                end=datetime(end_year,int(b[2]),int(b[3]),int(b[4] or 23),int(b[5] or 59),0 if b[4] else 59)
            except ValueError:continue
            if end<start or (end-start).days>180:continue
            deadline_only=b is a and bool(re.search(r'前|截至|截止',text[b.end():b.end()+12]))
            if deadline_only:start=notice.published_at.replace(tzinfo=None,hour=0,minute=0,second=0,microsecond=0)
            # Office sessions may follow the date range. They are not 24h access.
            sessions=[]
            office=text[b.end():]
            for clock in re.finditer(r'(\d{1,2})[:：](\d{2})[-—–~～至](\d{1,2})[:：](\d{2})',office):
                h1,m1,h2,m2=clock.groups()
                if int(h1)<24 and int(h2)<24 and int(m1)<60 and int(m2)<60:
                    if '下午' in office[max(0,clock.start()-4):clock.start()]:h1=str(int(h1)+12 if int(h1)<12 else int(h1));h2=str(int(h2)+12 if int(h2)<12 else int(h2))
                    sessions.append([f'{int(h1):02d}:{m1}',f'{int(h2):02d}:{m2}'])
            if sessions:
                start=start.replace(hour=int(sessions[0][0][:2]),minute=int(sessions[0][0][3:]))
                end=end.replace(hour=int(sessions[-1][1][:2]),minute=int(sessions[-1][1][3:]),second=0)
            audience='希望撤销本批认租的申请人' if kind=='withdrawal' else '对审核结果有异议的申请人'
            scope='按原文办理范围'
            if kind=='appeal' and re.search(r'仅受理.{0,25}队列|仅.{0,15}队列认定',text):scope='仅受理所属队列认定异议';audience='对所属队列认定有异议的申请人'
            elif kind=='appeal' and any('仅受理对所属队列' in c for c in clauses[max(0,i-1):i+1]):scope='仅受理所属队列认定异议';audience='对所属队列认定有异议的申请人'
            if kind=='supplement':audience='公告明确允许补正的申请人';scope='仅按公告指定材料及对象补正'
            result.append({'kind':kind,'start':start.isoformat(),'end':end.isoformat(),'notice_id':notice.id,
                'url':notice.canonical_url,'time_verified':bool(b[4] or sessions),'start_time_verified':bool(a[4] or sessions),
                'deadline_only':deadline_only,
                'audience':audience,'scope':scope,'office_sessions':sessions,'working_days_only':'工作日' in text,
                'place':re.search(r'(?:前往|地点[：:]|到)([^；。]{3,100}?)(?:提交|办理|窗口)',text)[1] if re.search(r'(?:前往|地点[：:]|到)([^；。]{3,100}?)(?:提交|办理|窗口)',text) else '',
                'evidence':text,'withdrawal_without_count':kind=='withdrawal' and any('不计入弃选次数' in c for c in clauses[i:i+3])})
    return list({(r['kind'],r['start'],r['end'],r['notice_id']):r for r in result}.values())


def state(window,now):
    now=now.replace(tzinfo=None)
    start=datetime.fromisoformat(window['start']);end=datetime.fromisoformat(window['end'])
    if now<start:return '未开始'
    if now>end:return '已截止'
    sessions=window.get('office_sessions') or []
    if sessions and not any(a<=now.strftime('%H:%M')<=b for a,b in sessions):return '期间内，当前不在受理时段'
    if window.get('working_days_only'):return '期间内，工作日按公告时段受理'
    return '办理中'


def display(window,now):
    end=datetime.fromisoformat(window['end']);start=datetime.fromisoformat(window['start'])
    clock=end.strftime(' %H:%M') if window.get('time_verified') else '（具体时刻见原文）'
    times='／'.join(a+'—'+b for a,b in window.get('office_sessions',[]))
    dates=('截至' if window.get('deadline_only') else start.strftime('%m-%d')+'—')+end.strftime('%m-%d')
    return LABELS.get(window['kind'],window['kind'])+'：'+state(window,now)+'｜'+dates+clock+('｜工作日' if window.get('working_days_only') else '')+(' '+times if times else '')+('｜'+window['scope'] if window.get('scope') else '')


def guidance(status,windows,now):
    messages=[]
    if status=='qualified':messages.append('核对公示或终审阶段，关注正式选房名单及安排；合格不等于已获配房源。')
    elif status=='ineligible':messages.append('先查看本人官方原因；是否接受异议或补正、接受哪些事项，以本批公告为准。')
    elif status=='withdrawn':messages.append('当前记录为撤销认租，不继续按合格家庭预测选房。')
    elif status=='not_reviewed':messages.append('尚未纳入本轮审核，不等于不合格；后续启动范围及条件见本批公告。')
    if not windows:messages.append('尚无已核实的办理窗口，请查看本批官方公告；不据此建议补交材料。')
    for w in windows:
        if w['kind'] in {'appeal','supplement','withdrawal','selection','signing'}:messages.append(display(w,now))
    return messages

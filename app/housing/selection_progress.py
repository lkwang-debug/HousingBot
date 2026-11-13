"""Source-bound selection snapshots; public views contain anonymous totals only."""
from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select

from app.housing.models import HousingMeta, HousingNotice, utcnow

PARSER = 'selection-progress-1'
STATUSES = {'可选':'available','未选':'available','未选定':'available','待选':'available',
            '已选':'selected','已选定':'selected','已选房':'selected',
            '已签约':'signed','已签订合同':'signed','已撤回':'withdrawn','撤回':'withdrawn',
            '不可选':'unavailable','暂停配租':'unavailable'}
FIELDS = {
 'project':{'小区','小区名称','项目','项目名称','房源项目'},
 'area':{'建筑面积','实际面积','面积','建筑面积平方米','建筑面积㎡'},
 'layout':{'户型','实际户型','房屋户型'},
 'band':{'认租档位','面积档位','认租面积'},
 'supply':{'本轮供应','供应套数','配租套数','供应套数套','房源套数','套数'},
 'selected':{'已选','已选套数','已选定套数','选定套数','累计已选'},
 'remaining':{'剩余','剩余套数','剩余可选','剩余可选套数','可选套数'},
 'signed':{'已签约','已签约套数'},'withdrawn':{'撤回套数','已撤回'},
 'unavailable':{'不可选套数','不可选'},
 'room':{'房源编号','房源编码','房屋编码','房号','房间号'},
 'building':{'楼栋','楼栋号','栋号','楼栋单元'},'status':{'房源状态','房屋状态','选房状态','状态'},
 'pool':{'共享池编号','房源池编号'},'round':{'轮次','配租轮次'},
 'selected_at':{'选定日期','选房日期','选定时间','选房时间'},
}


def clean(value):
    return re.sub(r'\s+','',str(value if value is not None else '')).strip()


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def official_url(url):
    p=urlsplit(url)
    if p.scheme!='https' or not p.hostname or not p.hostname.endswith('.gov.cn') or p.username or p.password:
        raise ValueError('仅接受HTTPS官方政府来源；自媒体及截图不能登记为正式进度')
    return url


def area_key(value):
    v=clean(value).replace('平方米','').replace('平米','').replace('㎡','').replace('左右','').replace('约','')
    v=re.sub(r'[—–~～至]','-',v)
    if not re.fullmatch(r'\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?',v):
        raise ValueError('实际面积未明确，不能用认租档位代替')
    return '-'.join(format(Decimal(x).normalize(),'f') for x in v.split('-'))


def layout_key(value):
    v=clean(value).replace('两','二')
    for a,b in [('1','一'),('2','二'),('3','三'),('4','四'),('5','五'),('6','六')]:v=v.replace(a,b)
    if v in {'单间','单身公寓'}:v='单房'
    if not re.fullmatch(r'单房|[一二三四五六]房(?:[一二三四五六]厅)?',v):raise ValueError('实际户型未明确')
    return v


def number(value):
    v=clean(value).replace(',','').replace('，','')
    if v in {'','—','-','待核实','未公开','未知'}:return None
    v=re.sub(r'套$','',v)
    if not v.isdigit():raise ValueError('数量必须为非负整数，未知不能填写为0')
    return int(v)


def _header(value):return re.sub(r'[\s（）()·:：]','',str(value or ''))


def catalog_rows(inventory):
    output=[]
    for project in (inventory or {}).get('projects',[]):
        for row in project.get('rows',[]):
            try:area=area_key(row.get('area'));layout=layout_key(row.get('layout'))
            except ValueError:continue
            if not isinstance(row.get('units'),int):continue
            output.append({'project':project['name'],'area':area,'layout':layout,'initial_supply':row['units'],
                           'band':row.get('band',''),'pool':row.get('shared_pool') or ''})
    return output


def _project(name,catalog):
    value=clean(name)
    names={r['project'] for r in catalog}
    exact=[n for n in names if clean(n)==value]
    if exact:return exact[0]
    aliases=[n for n in names if value in {clean(re.sub(r'[（(].*?[）)]','',n)),
                                         *[clean(a) for a in re.findall(r'[（(]([^）)]+)[）)]',n)]}]
    if len(aliases)==1:return aliases[0]
    if len(aliases)>1:raise ValueError('项目别名有歧义')
    return str(name).strip()


def parse_tables(tables,*,catalog=(),complete=False):
    """Explicit column meanings only. No names, identity or applicant fields retained."""
    records=[];previous=None;invalid=0
    for table in tables:
        header=None;start=0
        for i,cells in enumerate(table[:8]):
            normalized=[_header(c) for c in cells]
            mapping={key:next((j for j,v in enumerate(normalized) if v in aliases),None) for key,aliases in FIELDS.items()}
            if mapping['project'] is not None and (mapping['selected'] is not None or mapping['status'] is not None):
                header=mapping;start=i+1;previous=(header,len(cells));break
        if header is None and previous and table and len(table[0])==previous[1]:header=previous[0]
        if header is None:continue
        for cells in table[start:]:
            get=lambda field:cells[header[field]] if header[field] is not None and header[field]<len(cells) else None
            project=clean(get('project'))
            if not project or project in {'合计','总计','小计'}:continue
            if _header(project) in FIELDS['project']:continue
            try:
                row={'project':_project(project,catalog),'area':area_key(get('area')) if get('area') else '',
                     'layout':layout_key(get('layout')) if get('layout') else '',
                     'band':clean(get('band')),'pool':clean(get('pool')),'source_round':clean(get('round'))}
                if bool(row['area'])!=bool(row['layout']):
                    # Retain a reliable coarser total, never allocate it between types.
                    row['area']=row['layout']=''
                if header['status'] is not None:
                    status=STATUSES.get(clean(get('status')))
                    if not status:raise ValueError('存在尚未支持的房源状态')
                    room=clean(get('room'));building=clean(get('building'))
                    if not room:raise ValueError('逐套资料缺少唯一房源编号')
                    selected_at=get('selected_at')
                    date=None
                    if selected_at and status in {'selected','signed'}:
                        date=str(selected_at).strip().replace('/','-')[:10]
                        try:datetime.strptime(date,'%Y-%m-%d')
                        except ValueError:raise ValueError('选定日期未明确，无法计算当日数量')
                    row.update(room=(building+'/' if building else '')+room,status=status,selected_day=date,mode='rooms')
                else:
                    row.update({k:number(get(k)) for k in ['supply','selected','remaining','signed','withdrawn','unavailable']})
                    if row['selected'] is None:raise ValueError('没有实际选定数量')
                    row['mode']='summary'
                    known=[r for r in catalog if r['project']==row['project'] and r['area']==row['area'] and r['layout']==row['layout']]
                    if len(known)==1:
                        row['initial_supply']=known[0]['initial_supply'];row['band']=row['band'] or known[0]['band']
                        if known[0]['pool']:row['pool']=str(known[0]['pool'])
                records.append(row)
            except ValueError:
                invalid+=1
    if invalid:raise ValueError(f'{invalid}行进度未可靠识别，整份资料未替换')
    if not records:raise ValueError('未找到明确的项目、实际结果列；可能要求登录或页面仅提供脚本')
    if len({r['source_round'] for r in records if r['source_round']})>1:raise ValueError('资料含多个轮次，请按轮次分别导入')
    if len({r['mode'] for r in records})>1:raise ValueError('逐套和汇总资料混在同组，需分范围导入')
    unique={}
    for row in records:
        identity=(row['project'],row['room']) if row['mode']=='rooms' else (row['project'],row['area'],row['layout'],row['pool'])
        if identity in unique and unique[identity]!=row:raise ValueError('同房源或同户型存在冲突，不能合并轮次')
        unique[identity]=row
    if records[0]['mode']=='rooms':
        groups={}
        for row in unique.values():
            key=(row['project'],row['area'],row['layout'],row['pool'])
            item=groups.setdefault(key,{k:row[k] for k in ['project','area','layout','band','pool','source_round']})
            for name in ['supply','selected','remaining','signed','withdrawn','unavailable']:item.setdefault(name,0)
            item['supply']+=1;item['selected']+=int(row['status'] in {'selected','signed'})
            item['signed']+=int(row['status']=='signed');item['remaining']+=int(row['status']=='available')
            for name in ['withdrawn','unavailable']:item[name]+=int(row['status']==name)
            item.setdefault('selected_dates_complete',True);item.setdefault('selected_by_day',{})
            if row['status'] in {'selected','signed'}:
                if not row['selected_day']:item['selected_dates_complete']=False
                else:item['selected_by_day'][row['selected_day']]=item['selected_by_day'].get(row['selected_day'],0)+1
        result=list(groups.values())
        # Partial room lists are observed records, not complete inventories.
        for row in result:
            if not complete:row['supply']=row['remaining']=None
    else:result=list(unique.values())
    for row in result:
        row.pop('mode',None)
        if row.get('signed') is not None and row['signed']>row['selected']:raise ValueError('签约套数大于已选套数')
        supplied=row.get('supply')
        knownsum=sum(row.get(k) or 0 for k in ['selected','remaining','withdrawn','unavailable'])
        if supplied is not None and knownsum>supplied:raise ValueError('已选、可选及其他状态超过本轮供应')
        if row.get('pool'):row['issue']='共享房源池，仅按官方池范围统计，不与其他明细重复相加'
        row['level']='type' if row['area'] and row['layout'] else 'project'
    # A project total and its detail rows must not appear in the same sum.
    if any(a['project']==b['project'] and a['level']!=b['level'] for a in result for b in result):
        raise ValueError('项目汇总与户型明细范围重叠，请只导入其中一种')
    pools={};separate=[]
    for row in result:
        if not row.get('pool'):separate.append(row);continue
        pool=pools.setdefault(row['pool'],[]);pool.append(row)
    for key,members in pools.items():
        counts=[{k:r.get(k) for k in ['supply','selected','remaining','signed','withdrawn','unavailable']} for r in members]
        if any(c!=counts[0] for c in counts):raise ValueError('共享池各行数量不一致，无法确认是否重复，暂不拆分')
        row=dict(members[0],level='pool',area='',layout='',project='／'.join(dict.fromkeys(r['project'] for r in members)))
        row['pool_members']=[{k:r[k] for k in ['project','area','layout']} for r in members]
        separate.append(row)
    return separate


def read_tables(data,suffix):
    suffix=suffix.lower().lstrip('.')
    if suffix in {'html','htm'}:
        from bs4 import BeautifulSoup
        soup=BeautifulSoup(data,'html.parser')
        if not soup.select('table') and any(t in str(soup) for t in ['client_id=','政务服务网','统一身份认证','用户登录']):
            raise ValueError('官方入口要求登录，请导入登录后取得的官方表格')
        return [[[c.get_text(' ',strip=True) for c in r.find_all(['th','td'])] for r in t.find_all('tr')]
                for t in soup.find_all('table')]
    if suffix=='csv':
        import csv
        try:text=data.decode('utf-8-sig')
        except UnicodeDecodeError:text=data.decode('gb18030')
        return [list(csv.reader(io.StringIO(text)))]
    if suffix in {'xls','xlsx'}:
        if suffix=='xlsx':
            import openpyxl
            book=openpyxl.load_workbook(io.BytesIO(data),read_only=True,data_only=True)
            try:return [[list(row) for row in sheet.iter_rows(values_only=True)] for sheet in book.worksheets]
            finally:book.close()
        from app.housing.lottery import _read_table
        return [_read_table(data,suffix)]
    if suffix=='pdf':
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages)>600:raise ValueError('进度附件页数超过上限')
            return [table for page in pdf.pages for table in page.extract_tables()]
    raise ValueError('仅支持官方Excel、CSV、HTML表格或PDF；不支持截图')


def _index(batch):return 'selection-progress-index:'+batch


def family_tables(tables):
    """Only explicit household counts, scoped to a date AND a session."""
    result={}
    for table in tables:
        for i,cells in enumerate(table[:8]):
            header=[_header(c) for c in cells]
            column=lambda names:next((j for j,c in enumerate(header) if c in names),None)
            day=column({'选房日期','安排日期'});slot=column({'场次','选房场次'})
            planned=column({'安排家庭','安排家庭数','安排选房家庭数'});chosen=column({'选定家庭','选定家庭数','已选定家庭数'})
            abandoned=column({'弃选家庭','弃选家庭数'})
            if None in (day,slot,planned,chosen):continue
            for row in table[i+1:]:
                if len(row)<=max(day,slot,planned,chosen) or not clean(row[day]):continue
                date=str(row[day]).strip()[:10]
                try:datetime.strptime(date,'%Y-%m-%d')
                except ValueError:raise ValueError('家庭统计必须明确年月日及场次')
                p=number(row[planned]);c=number(row[chosen]);a=number(row[abandoned]) if abandoned is not None and abandoned<len(row) else None
                if p is None or c is None or c>p or (a is not None and a+c>p):raise ValueError('同场家庭人数无法对账')
                item={'day':date,'session':str(row[slot]).strip(),'planned':p,'selected':c,'abandoned':a,
                      'rate':str((Decimal(c)*100/Decimal(p)).quantize(Decimal('.1'),rounding=ROUND_HALF_UP)) if p else None}
                key=(date,item['session'])
                if key in result and result[key]!=item:raise ValueError('同场次家庭统计冲突')
                result[key]=item
            break
    return list(result.values())


def snapshots(session,batch):
    index=session.get(HousingMeta,_index(batch))
    return [m.value for key in (index.value.get('keys',[]) if index else []) if (m:=session.get(HousingMeta,key))]


def prepare_import(session,notice,file,source,*,as_of=None,round_name='本批首轮',scope='整批',complete=False):
    from app.housing.batches import batch_key
    from app.housing.inventory import batch_inventory
    key=batch_key(notice)
    if not key:raise ValueError('公告尚未可靠关联批次，不能登记选房进度')
    official_url(source)
    if not round_name.strip() or not scope.strip():raise ValueError('必须明确轮次和资料范围')
    if as_of:
        at=datetime.fromisoformat(as_of)
        if at.tzinfo:at=at.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
        if at>utcnow()+timedelta(minutes=5):raise ValueError('数据截至时间不能在未来')
    else:at=None
    file=Path(file);data=file.read_bytes()
    if len(data)>30*1024*1024:raise ValueError('单份资料超过30MB')
    inventory=batch_inventory(session,key);catalog=catalog_rows(inventory)
    tables=read_tables(data,file.suffix)
    rows=parse_tables(tables,catalog=catalog,complete=complete)
    for row in rows:
        actual=row.get('source_round')
        if actual and actual!=round_name and {actual,round_name}-{'本批首轮','首轮','第一轮','第1轮'}:
            raise ValueError('资料明确轮次与--round-name不一致，请核对后分别导入')
    if catalog:
        if any(r['level']!='pool' and r['project'] not in {c['project'] for c in catalog} for r in rows):raise ValueError('房源项目与本批官方房源表不一致')
        if any(r['level']=='type' and not any(all(r[k]==c[k] for k in ['project','area','layout']) for c in catalog) for r in rows):
            raise ValueError('实际面积或户型与本批官方表不一致，需先核对本轮房源变化')
    if complete and scope=='整批' and catalog and round_name=='本批首轮':
        expected={(c['project'],c['area'],c['layout']):c['initial_supply'] for c in catalog}
        observed={(r['project'],r['area'],r['layout']):r.get('supply') for r in rows}
        if expected!=observed:raise ValueError('整批完整资料与本批全部实际户型供应不符，不能发布整批总计')
    result={'schema':1,'parser':PARSER,'batch_key':key,'notice_id':notice.id,'round':round_name,'scope':scope,
            'source':source,'as_of':at.isoformat() if at else None,'complete':bool(complete),'rows':rows,'families':family_tables(tables),
            'supply_version':digest(catalog),'file_sha256':hashlib.sha256(data).hexdigest()}
    result['version']=digest({k:v for k,v in result.items() if k not in {'source','notice_id','file_sha256'}})
    return result,data


def register(session,snapshot,data=b'',suffix='',*,private_root=None,method='import'):
    from app.config import get_settings
    root=Path(private_root or get_settings().data_dir/'selection-progress-private')
    key='selection-progress-snapshot:'+snapshot['batch_key']+':'+snapshot['version']
    existing=session.get(HousingMeta,key)
    if existing:return {'status':'existing','version':snapshot['version'],'rows':len(snapshot['rows'])}
    if data:
        root.mkdir(parents=True,exist_ok=True)
        (root/(snapshot['file_sha256']+suffix)).write_bytes(data)
    value=dict(snapshot,registered_at=utcnow().isoformat(),method=method)
    session.add(HousingMeta(key=key,value=value));index=session.get(HousingMeta,_index(snapshot['batch_key']))
    if index is None:index=HousingMeta(key=_index(snapshot['batch_key']),value={'keys':[]});session.add(index)
    index.value={'keys':[*index.value['keys'],key]}
    session.flush()
    return {'status':'registered','version':snapshot['version'],'rows':len(snapshot['rows'])}


def _totals(snap):
    rows=snap['rows']
    if not snap['complete'] or any(r.get('pool') for r in rows):return None
    return {k:sum(r[k] for r in rows) if all(r.get(k) is not None for r in rows) else None
            for k in ['supply','selected','remaining','signed','withdrawn','unavailable']}


def public_progress(session,batch,*,at=None,windows=(),notice_ids=()):
    at=at or utcnow();all_snapshots=snapshots(session,batch)
    # Versions with unknown data time remain clearly labelled, never supersede
    # a newer dated snapshot merely because they were imported later.
    eligible=[s for s in all_snapshots if datetime.fromisoformat(s['registered_at'])<=at
              and (not s['as_of'] or datetime.fromisoformat(s['as_of'])<=at)]
    grouped={}
    for s in eligible:grouped.setdefault((s['round'],s['scope']),[]).append(s)
    scopes=[]
    for identity,versions in grouped.items():
        versions.sort(key=lambda s:(bool(s['as_of']),s['as_of'] or '',s['registered_at']))
        import copy
        observed=versions[-1]
        verified=[s for s in versions if s['complete']]
        latest=verified[-1] if not observed['complete'] and verified else observed
        value=copy.deepcopy({k:v for k,v in latest.items() if k not in {'file_sha256','notice_id'}})
        if latest is not observed:value['latest_incomplete']='新资料未完整，仍保留上一份完整范围统计。'
        value['totals']=_totals(latest);value['net_change']=None;value['daily_selected']=None
        if latest['complete'] and latest['as_of'] and all(r.get('selected_dates_complete') for r in latest['rows']):
            day=latest['as_of'][:10]
            value['daily_selected']={'day':day,'count':sum(r['selected_by_day'].get(day,0) for r in latest['rows'])}
        previous=[v for v in versions if v['as_of'] and latest['as_of'] and v['as_of']<latest['as_of'] and v['complete'] and v['supply_version']==latest['supply_version']]
        if latest['complete'] and previous:
            old=previous[-1];a=_totals(latest);b=_totals(old)
            if a and b and a['selected'] is not None and b['selected'] is not None and a['supply']==b['supply']:
                value['net_change']={'from':old['as_of'],'to':latest['as_of'],'selected':a['selected']-b['selected']}
                if a['selected']<b['selected']:value['correction_note']='累计已选减少，可能涉及退选或官方更正，不能当作新增选房量。'
        for r in value['rows']:
            related=[w for w in windows if w['kind']=='selection' and (not w.get('bands') or r.get('band') in w['bands'])]
            r['not_started']=bool(related) and all(datetime.fromisoformat(w['start'])>at for w in related)
        scopes.append(value)
    checks=[m.value for m in session.scalars(select(HousingMeta).where(HousingMeta.key.like('selection-progress-check:%')))
            if (m.value.get('batch_key')==batch or m.key in {f'selection-progress-check:{n}' for n in notice_ids})
            and m.value.get('checked_at','')<=at.isoformat()]
    issue=max(checks,key=lambda v:v['checked_at']) if checks else None
    return {'scopes':scopes,'check':issue,'pending':not scopes,'version':digest([s['version'] for s in scopes]),
            'issue':issue.get('error','') if issue else ('实际选房进度尚未核实' if not scopes else '')}


def source_candidates(notice):
    """Only links actually published by an official notice; do not invent APIs."""
    values=[]
    for a in notice.attachments or []:
        if re.search(r'选房结果|房源状态|剩余房源|选房进度|选房情况',a.get('name','')):values.append(a['url'])
    body=notice.body_text or ''
    for match in re.finditer(r'https?://[^\s<>"，。；]+',body):
        url=match[0].rstrip('）)')
        context=body[max(0,match.start()-45):match.start()]
        if re.search(r'选房结果查询|认租房源查询|房源状态|剩余房源',context):values.append(url)
    result=[]
    for url in values:
        try:official_url(url)
        except ValueError:continue
        if url not in result:result.append(url)
    return result


async def collect_notice(session,client,notice,*,now=None):
    from app.housing.batches import batch_key
    from app.housing.inventory import batch_inventory
    from app.housing.daily_lifecycle import action_windows
    now=now or utcnow();urls=source_candidates(notice);key=batch_key(notice)
    if not key or not urls:return
    windows=[w for w in action_windows(notice) if w['kind']=='selection']
    if not any(datetime.fromisoformat(w['start'])<=now<=datetime.fromisoformat(w['end']) for w in windows):return
    check=session.get(HousingMeta,f'selection-progress-check:{notice.id}')
    if check and check.value.get('next_check_at','')>now.isoformat():return
    errors=[];success=0
    for url in urls[:6]:
        if not session.info.get('housing_dry_run'):session.commit()
        try:
            response=await client.get(url)
            destination=urlsplit(response.url)
            if destination.hostname!=urlsplit(url).hostname or any(t in response.url for t in ['/sso/','client_id=','/login']):
                raise ValueError('官方入口要求登录，可导入登录后取得的官方资料')
            if response.status!=200:raise ValueError('官方进度来源暂不可用')
            suffix=Path(urlsplit(url).path).suffix or '.html'
            data=response.content or response.text.encode()
            catalog=catalog_rows(batch_inventory(session,key));tables=read_tables(data,suffix)
            candidate_rows=parse_tables(tables,catalog=catalog,complete=True)
            expected={(r['project'],r['area'],r['layout']):r['initial_supply'] for r in catalog}
            observed={(r['project'],r['area'],r['layout']):r.get('supply') for r in candidate_rows}
            full=bool(expected) and observed==expected
            rows=candidate_rows if full else parse_tables(tables,catalog=catalog,complete=False)
            from bs4 import BeautifulSoup
            text=BeautifulSoup(data,'html.parser').get_text(' ',strip=True) if suffix in {'.html','.htm'} else ''
            matched=re.search(r'(?:更新时间|数据截至|统计截至)\s*[:：]?\s*(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})[日\s]*(\d{1,2}):(\d{2})',text)
            as_of=datetime(*map(int,matched.groups())).isoformat() if matched else None
            if as_of and datetime.fromisoformat(as_of)>now+timedelta(minutes=5):raise ValueError('官方进度时间在未来，暂不替换')
            s={'schema':1,'parser':PARSER,'batch_key':key,'notice_id':notice.id,'round':'官方当前轮次（尚未核实）',
               'scope':urlsplit(url).path,'source':url,'as_of':as_of,'complete':full,'rows':rows,'families':family_tables(tables),
               'supply_version':digest(catalog_rows(batch_inventory(session,key))),'file_sha256':hashlib.sha256(data).hexdigest()}
            s['version']=digest({k:v for k,v in s.items() if k not in {'notice_id','file_sha256','source'}})
            if not session.info.get('housing_dry_run'):register(session,s,data,suffix,method='public')
            success+=1
        except Exception as exc:errors.append(str(exc) if isinstance(exc,ValueError) else type(exc).__name__)
    if not session.info.get('housing_dry_run'):
        if check is None:check=HousingMeta(key=f'selection-progress-check:{notice.id}');session.add(check)
        failures=(check.value or {}).get('failures',0)+1 if errors else 0
        delay=30 if failures<3 else 120
        check.value={'batch_key':key,'checked_at':now.isoformat(),'next_check_at':(now+timedelta(minutes=delay)).isoformat(),
                     'status':'partial' if errors and success else 'failed' if errors else 'ok','error':'；'.join(dict.fromkeys(errors)),
                     'failures':failures,'urls':urls}
        session.flush()


def progress_net(current,previous):
    """Formal-report comparison, never counted as a new official notice."""
    prior={(s['round'],s['scope']):s for s in (previous or {}).get('scopes',[])}
    for scope in (current or {}).get('scopes',[]):
        old=prior.get((scope['round'],scope['scope']));scope['report_net']=None
        if not old or old['version']==scope['version'] or old.get('supply_version')!=scope.get('supply_version'):continue
        a=scope.get('totals');b=old.get('totals')
        if a and b and a['supply']==b['supply'] and a['selected'] is not None and b['selected'] is not None:
            scope['report_net']=a['selected']-b['selected']

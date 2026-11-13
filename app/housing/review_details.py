"""Evidence-bound unit intentions and reason aggregates, without eligibility inference."""
import re
from collections import Counter,defaultdict
from decimal import Decimal,ROUND_HALF_UP


def clean(text):return re.sub(r'\s+','',str(text or '')).strip('；;。')


def concrete_intentions(text,projects):
    text=clean(text);result=[]
    for name in projects or []:
        aliases=[name,re.sub(r'[（(].*?[）)]','',name),*re.findall(r'[（(]([^）)]+)[）)]',name)]
        for alias in aliases:
            if len(alias)<3:continue
            pattern=re.escape(clean(alias))+r'(?:[（(][^）)]*[）)])?(?:建筑面积|建面|面积)?(?:约)?(\d+(?:\.\d+)?(?:[-~～至]\d+(?:\.\d+)?)?)(?:㎡|平方米|平米)(?:左右)?(?:的)?(单房|单身公寓|小三房|[一二两三四五六]房(?:[一二两三四五六]厅)?)'
            for m in re.finditer(pattern,text):
                row={'project':name,'area':m[1],'layout':m[2].replace('两房','二房')}
                if row not in result:result.append(row)
    return result


def reason_text(value):
    value=clean(value)
    # Reject accidental neighbouring identity columns instead of exposing them.
    if re.search(r'\*{3,}|[A-Z]{1,6}\d[A-Z0-9]{6,}|\d{8,}',value):return ''
    return value if 0<len(value)<=1500 else ''


REASON_GROUPS=[('材料缺失',r'未.{0,5}(?:提交|提供|上传)|材料不齐|缺少.{0,8}(?:材料|证明)'),
 ('人才条件或类型不匹配',r'人才引进|迁户核准|学历|学位|职称'),
 ('社保',r'社保|社会保险|参保|缴费'),('住房及住房保障',r'住房|政策性租房|租赁补贴|自有房|购房'),
 ('家庭人口或户型条件',r'家庭人口|人口数|认租户型|未成年子女|抚养权|家庭人数')]


def reason_summary(records):
    rows=[r for r in records.values() if r['status']=='ineligible'];denominator=len(rows)
    known=[r for r in rows if r.get('reason_verified') and r.get('reason')]
    exact=Counter(r['reason'] for r in known);groups=Counter()
    for r in known:
        selected=[label for label,pattern in REASON_GROUPS if re.search(pattern,r['reason'])]
        for label in selected or ['其他／待核实']:groups[label]+=1
    ratio=lambda n:str((Decimal(n)*100/Decimal(denominator)).quantize(Decimal('.1'),rounding=ROUND_HALF_UP)) if denominator else None
    return {'denominator':denominator,'parsed':len(known),'missing':denominator-len(known),'coverage':ratio(len(known)),
            'categories':[{'label':k,'households':n,'percent':ratio(n)} for k,n in groups.most_common()],
            'exact':[{'reason':k,'households':n,'percent':ratio(n)} for k,n in exact.most_common()],
            'note':'类别按涉及家庭计数，一户可涉及多个原因，人数及比例不能相加；分母为本轮明确不合格家庭，撤销及未审核不计入。'}


def unit_summary(records,projects):
    qualified=[r for r in records.values() if r['status']=='qualified'];counts=defaultdict(set);complete={}
    for r in qualified:
        band=r['band'];complete.setdefault(band,True)
        if not r.get('types_verified'):complete[band]=False
        for x in r.get('intention_types',[]):counts[(band,x['project'],x['area'],x['layout'])].add(r['apply_no'])
    output=[]
    for p in projects:
        for row in p.get('rows',[]):
            band=row.get('band','');area=clean(row.get('area',''));layout=clean(row.get('layout','')).replace('两房','二房')
            if band not in complete:continue
            a=re.fullmatch(r'(?:约)?(\d+(?:\.\d+)?)(?:㎡|平方米|平米)?',area)
            numbers=counts.get((band,p['name'],a[1] if a else area,layout),set())
            valid=bool(a) and complete.get(band,False) and not row.get('shared_pool') and isinstance(row.get('units'),int) and row['units']>0
            ratio=str((Decimal(len(numbers))/Decimal(row['units'])).quantize(Decimal('.1'),rounding=ROUND_HALF_UP)) if valid else None
            if not valid and not numbers:continue
            output.append({'band':band,'project':p['name'],'area':row.get('area',''),'layout':row.get('layout',''),
                'initial_units':row.get('units'),'qualified_intentions':len(numbers) if complete.get(band) else (len(numbers) if numbers else None),
                'complete':valid,'ratio':ratio,'basis':'初始房源','issue':'' if valid else '具体户型意向或供应范围未完整对应，暂不计算比例'})
    return output


def pdf_reason_columns(data):
    """Read explicit reason cells; never retain raw name or identity columns."""
    import io,pdfplumber
    result={}
    previous_header=None
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                found_header=False
                for i,row in enumerate(table[:5]):
                    header=[clean(c) for c in row]
                    no=next((j for j,c in enumerate(header) if c in {'认租编号','认租申请编号','备案回执号','申请编号','受理编号'}),None)
                    reason=next((j for j,c in enumerate(header) if c in {'不合格原因','审核原因','未通过原因','原因'}),None)
                    if no is None or reason is None:continue
                    previous_header=(no,reason,len(header));found_header=True
                    for cells in table[i+1:]:
                        if len(cells)<=max(no,reason):continue
                        number=clean(cells[no]);value=reason_text(cells[reason])
                        if re.fullmatch(r'[A-Z]{1,6}\d[A-Z0-9-]{5,63}',number) and value:
                            if number in result and result[number]!=value:raise ValueError('同编号原因列冲突')
                            result[number]=value
                    break
                if not found_header and previous_header:
                    no,reason,width=previous_header
                    if table and len(table[0])==width and re.fullmatch(r'[A-Z]{1,6}\d[A-Z0-9-]{5,63}',clean(table[0][no])):
                        for cells in table:
                            if len(cells)<=max(no,reason):continue
                            number=clean(cells[no]);value=reason_text(cells[reason])
                            if re.fullmatch(r'[A-Z]{1,6}\d[A-Z0-9-]{5,63}',number) and value:
                                if number in result and result[number]!=value:raise ValueError('同编号原因列冲突')
                                result[number]=value
    return result

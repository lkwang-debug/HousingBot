"""Evidence-bound handling windows and daily visibility, independent of history."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from app.housing.extract import DATE_TOKEN_RE, extract_review_info


def action_windows(notice):
    text = re.sub(r"\s+", " ", notice.body_text or "")
    result = []
    patterns = {"selection": r"(?:正式)?选房(?:签约)?(?:时间|日期)(?:为|[：:]|\s)",
                "signing": r"(?:签约|签订合同)(?:时间|日期)(?:为|[：:]|\s)"}
    published = notice.published_at
    if not published or notice.published_unverified:
        return result
    for kind, pattern in patterns.items():
        for anchor in re.finditer(pattern, text):
            context = text[anchor.end():anchor.end()+220]
            tokens = list(DATE_TOKEN_RE.finditer(context))
            if not tokens or tokens[0].start() > 45:
                continue
            first = tokens[0]
            last = first
            if len(tokens) > 1 and re.fullmatch(r"\s*(?:至|到|—|–|-|－|~|～)\s*", context[first.end():tokens[1].start()]):
                last = tokens[1]
            year = int(first.group('year') or published.year)
            end_year = int(last.group('year') or year)
            if last is not first and not last.group('year') and int(last.group('month')) < int(first.group('month')):
                end_year += 1
            try:
                start = datetime(year, int(first.group('month')), int(first.group('day')))
                end = datetime(end_year, int(last.group('month')), int(last.group('day')), 23,59,59)
            except ValueError:
                continue
            if end < start or (end-start).days > 365:
                continue
            clock_text = context[last.end():].split('。',1)[0]
            clocks = re.findall(r"(?<!\d)([0-2]?\d)[:：]([0-5]\d)(?!\d)", clock_text)
            valid = [(int(h),int(m)) for h,m in clocks if int(h)<24]
            if valid:
                hour, minute = max(valid)
                end = end.replace(hour=hour,minute=minute,second=0)
            result.append({'kind':kind,'start':start.isoformat(),'end':end.isoformat(),
                           'notice_id':notice.id,'url':notice.canonical_url,
                           'time_verified':bool(valid),
                           'evidence':text[anchor.start():anchor.end()+last.end()]})
            prefix=text[text.rfind('。',0,anchor.start())+1:anchor.start()]
            result[-1]['bands']=[m[0]+('+' if m[1] in {'及以上','以上'} else '') for m in re.findall(r'(\d+)\s*(?:㎡|平方米|平米)\s*(左右|及以上|以上)?',prefix)]
    review = extract_review_info(notice.body_text, published)
    if review.start and review.end:
        result.append({'kind':'review','start':datetime.combine(review.start,datetime.min.time()).isoformat(),
            'end':datetime.combine(review.end,datetime.max.time()).replace(microsecond=0).isoformat(),
            'notice_id':notice.id,'url':notice.canonical_url,'evidence':review.evidence})
    from app.housing.handling import extra_windows
    result.extend(extra_windows(notice))
    return list({(r['kind'],r['start'],r['end'],r['notice_id']):r for r in result}.values())


def classify_daily(card, notices, now):
    """Retiring a daily record does NOT assert all households completed allocation."""
    actions = [w for n in notices if n.published_at and n.published_at.replace(tzinfo=None)<=now
               for w in action_windows(n)]
    card.action_windows = actions
    if card.batch_id is None:
        return 'unlinked', '原配租批次未可靠关联'
    if card.stage == 'closed':
        return 'closed', '官方已明确批次结束'
    if card.is_open:
        return 'active', '认租窗口内'
    future = [w for w in actions if datetime.fromisoformat(w['end']) >= now]
    if future:
        return 'active', '存在有效的核查、选房或签约办理期'
    if card.stage == 'announced' and card.start and card.start>now:
        return 'active', '认租时间已明确，尚未开始'
    terminal = [w for w in actions if w['kind'] in {'selection','signing'}]
    latest_node = max((r.published_at for r in card.references if r.published_at and r.published_at<=now), default=None)
    if terminal:
        last = max(terminal, key=lambda w:w['end'])
        # A later dated review/replenishment notice can start a new round.
        source = next((r for r in card.references if r.id==last['notice_id']), None)
        latest=next((n for n in notices if n.published_at==latest_node),None)
        result_only=bool(latest and re.search(r'选房结果|签约结果|办理结果',latest.title or ''))
        if result_only or not source or not latest_node or latest_node<=source.published_at:
            return 'period_elapsed', '已核实本轮选房／签约办理期已过，未确认整批全部配完'
    month_start = now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    if card.stage in {'selection','signing'} and latest_node and latest_node<month_start:
        return 'history_unverified', '选房／签约公告早于本月，办理日期仍待核实'
    reference = card.end if card.stage in {'waiting','unverified_window','applying'} and card.end else latest_node
    if reference and reference<=now-timedelta(days=60):
        return 'history_unverified', '超过60天未核实有效后续，退出日报但继续检查官方来源'
    if reference and reference<=now-timedelta(days=30):
        return 'review_needed', '超过30天，需复核后续公告'
    if not reference:
        return 'history_unverified', '缺少可靠发布时间及办理时间'
    return 'active', '仍处于近期流程跟进范围'


def area_clauses(text):
    """Split enumerated clauses, never decimal prices/dates or exception words."""
    text = re.sub(r"[ \t\r\n]+", " ", text or '').strip()
    pattern = r"(?<![\d.])(?:[（(][一二三四五六七八九十\d]+[）)]|[一二三四五六七八九十]+[、．]|[1-9][、．]|[1-9]\.(?!\d))"
    matches = list(re.finditer(pattern,text))
    if not matches:
        # Some sources flatten unnumbered population/area paragraphs. Split
        # only at a full stop/semicolon followed by an explicit household band.
        people=r'(?:[一二三四五六七八九十两\d]+(?:至[一二三四五六七八九十两\d]+)?人(?:及以上|以上)?家庭|单身(?:及以上家庭|居民))'
        text=re.sub(r'(如下[：:])\s*(?='+people+')',r'\1\n',text)
        chunks=re.split(r'(?<=[。；])\s*(?='+people+')|\n',text)
        result=[]
        for chunk in chunks:
            note=re.search(r'注[：:]|申请人自愿认租|认租家庭自愿认租',chunk)
            if note and note.start()>0:result.extend([chunk[:note.start()].strip(),chunk[note.start():].strip()])
            elif chunk.strip():result.append(chunk.strip())
        return result
    parts = []
    if text[:matches[0].start()].strip():
        parts.append(text[:matches[0].start()].strip())
    for i, match in enumerate(matches):
        part = text[match.start():matches[i+1].start() if i+1<len(matches) else len(text)].strip()
        # A 注 statement belongs to a separate paragraph, with its full exception.
        note = re.search(r"(?:注[：:]|申请人自愿认租|认租家庭自愿认租)",part)
        if note and note.start()>0:
            parts.extend([part[:note.start()].strip(),part[note.start():].strip()])
        else:
            parts.append(part)
    return parts

"""Targeted baseline repair from existing official sources; no delivery creation."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.db import SessionLocal
from app.housing.batches import rebuild_links
from app.housing.fetch import HousingHttpClient
from app.housing.models import HousingNotice,HousingSource,HousingMeta
from app.housing.parsers.detail import parse_detail
from app.housing.store import store_notice


async def repair(session,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    launch=session.get(HousingNotice,147)
    if not launch or launch.region_code!='坪山区' or '2026年第一批次' not in launch.title:
        raise ValueError('未找到预期坪山启动公告，停止定向修复')
    source=session.get(HousingSource,launch.source_id) if launch.source_id else session.scalar(select(HousingSource).where(HousingSource.region_code=='坪山区'))
    if not source:raise ValueError('坪山来源未配置')
    before={n.id:(n.batch_key_v2,n.batch_link_status) for n in session.scalars(select(HousingNotice))}
    previous_events={n.id:n.event_at for n in session.scalars(select(HousingNotice))}
    prior_revisions={r.key:r.value for r in session.scalars(select(HousingMeta).where(HousingMeta.key.like('notice-revisions:%')))}
    added=[]
    async with HousingHttpClient() as client:
        url='https://www.szpsq.gov.cn/ztfw/zfbzfw/tzgg/index.html'
        response=await client.get(url)
        soup=BeautifulSoup(response.text,'html.parser')
        links={urljoin(url,a['href']) for a in soup.select('a[href]') if
            '2026年第一批次面向个人配租保障性租赁住房' in a.get_text('',strip=True)}
        links.add('https://www.szpsq.gov.cn/ztfw/zfbzfw/tzgg/content/post_12964250.html')
        links.add('https://www.szpsq.gov.cn/ztfw/zfbzfw/tzgg/content/post_12974416.html')
        for url in sorted(links):
            response=await client.get(url)
            detail=parse_detail(response.text,url)
            if not detail.published_at or detail.published_at.year!=2026:
                raise ValueError('后续公告日期未可靠读取')
            if '第一批次面向个人配租保障性租赁住房' not in detail.title:
                raise ValueError('后续公告标题与目标批次不符')
            n,created=store_notice(session,source=source,title=detail.title,url=url,published_at=detail.published_at,
                body_text=detail.body_text,content_html=detail.content_html,attachments=detail.attachments,detail_status='ok')
            n.event_at=previous_events.get(n.id,n.published_at)
            # This operation is a repair baseline, not a fresh official revision.
            revisions=session.get(HousingMeta,f'notice-revisions:{n.id}')
            if revisions:
                if revisions.key in prior_revisions:revisions.value=prior_revisions[revisions.key]
                else:session.delete(revisions)
            (output/f'official-{n.id}.html').write_text(response.text,encoding='utf-8')
            added.append({'id':n.id,'created':created,'title':n.title,'url':url,'published_at':n.published_at.isoformat()})
    links_result=rebuild_links(session)
    changes=[{'id':n.id,'from':before.get(n.id),'to':(n.batch_key_v2,n.batch_link_status)} for n in session.scalars(select(HousingNotice))
             if before.get(n.id)!=(n.batch_key_v2,n.batch_link_status)]
    result={'sources':added,'links':links_result,'changed_links':changes,'baseline':True,'outgoing_requests':0}
    (output/'repair-history.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    with SessionLocal() as session:
        result=asyncio.run(repair(session,args.output))
        session.commit() if args.apply else session.rollback()
    print(json.dumps({'sources':len(result['sources']),'changed_links':len(result['changed_links']),'links':result['links'],'applied':args.apply},ensure_ascii=False))


if __name__=='__main__':main()

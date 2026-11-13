"""Collect official review attachments without sending messages or claiming daily keys."""
import argparse
import asyncio
import json
from datetime import timedelta

from sqlalchemy import select
from app.db import SessionLocal
from app.housing.fetch import HousingHttpClient
from app.housing.models import HousingNotice,HousingAttachmentState,utcnow
from app.housing.qualification import ingest_review,review_files


async def sync(ids=(),force=False):
    results=[]
    with SessionLocal() as session:
        stmt=select(HousingNotice).where(HousingNotice.is_target.is_(True))
        stmt=stmt.where(HousingNotice.id.in_(ids)) if ids else stmt.where(HousingNotice.published_at>=utcnow()-timedelta(days=120))
        notices=[n for n in session.scalars(stmt) if review_files(n)]
        async with HousingHttpClient() as client:
            for notice in notices:
                meta=await ingest_review(session,client,notice,force=force)
                state=session.scalar(select(HousingAttachmentState).where(
                    HousingAttachmentState.notice_id==notice.id,HousingAttachmentState.purpose=='qualification'))
                results.append({'notice_id':notice.id,'status':state.status if state else 'unlinked',
                                'verified_households':meta.get('total') if meta else None})
                session.commit()
    return results


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--notice-id',type=int,action='append',default=[])
    p.add_argument('--force',action='store_true');p.add_argument('--rebuild',action='store_true');args=p.parse_args()
    result={'reviews':asyncio.run(sync(args.notice_id,args.force)),'messages_sent':0}
    if args.rebuild:
        from app.housing.lookup import refresh_review_metadata
        result['lookup']=refresh_review_metadata()
    print(json.dumps(result,ensure_ascii=False))

"""Persist HTML alongside the existing outbox without a schema migration."""
import hashlib
from app.housing.models import HousingMeta


def freeze_wire(session,job,html):
    if len(html.encode('utf-16-le'))//2>19800:raise ValueError('完整HTML超过单条消息容量')
    record=HousingMeta(key=f'outbox-render:{job.id}',value={'template':'html','content':html,
        'text_sha256':hashlib.sha256(job.content.encode()).hexdigest(),'html_sha256':hashlib.sha256(html.encode()).hexdigest()})
    session.add(record);session.flush()


def prepared_wire(session,job):
    record=session.get(HousingMeta,f'outbox-render:{job.id}')
    if not record:return None
    value=record.value
    if value['text_sha256']!=hashlib.sha256(job.content.encode()).hexdigest() or value['html_sha256']!=hashlib.sha256(value['content'].encode()).hexdigest():
        raise ValueError('冻结正文与HTML校验不一致，拒绝投递')
    return value

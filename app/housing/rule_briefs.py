"""Short source-bound descriptions; full clauses stay in the frozen evidence."""
import re


def rule_brief(item):
    heading, text = item['heading'], item['text']
    flat = re.sub(r'\s+', '', text)
    if '配租对象' in heading:
        if all(t in flat for t in ('龙华区户籍', '2023年8月1日', '2025年4月18日', '2026年1月1日')):
            return '面向本批规定时点的龙华区户籍在册轮候人；公告指定的两类家庭另须满足收入财产限额，并持规定日期后的经济状况核对报告。适用范围、时点及例外见原文。'
        if all(t in flat for t in ('盐田区户籍', '收入财产限额', '或经民政部门认定')):
            return '面向本批规定时点的盐田区户籍在册轮候人；需满足收入财产限额并持规定日期后的核对报告，或属于公告明确的困难家庭类别。户籍时点、报告日期及认定要求见原文。'
        if '龙华区2025年度人才安居重点企业名录' in flat:
            return '面向《龙华区2025年度人才安居重点企业名录》内企业；符合多个类别时按“就高不就低”申报。企业、员工条件及各类别要求请分别核对原文。'
        return '申请对象以本批公告列明的个人、家庭或单位范围为准；同时核对完整条件、认定时点及例外，不能仅凭对象类别判断具备资格。'
    if '配租方式' in heading or '选房排位规则' in heading:
        if re.search(r'超过70(?:平方米|㎡|平米)',flat) and '五人及以上家庭' in flat:
            return '按房源面积、家庭人数和轮候排位分批选房。70㎡以上房源优先面向五人及以上家庭，特殊家庭按相应序位规定优先；各批入围、候补、递补及补选条件见原文。'
        if '公证摇号' in flat:
            return '按本批公证摇号、资格核查及适用优先规则办理；摇号顺序不直接等于最终选房排序，正式分组及办理安排见原文。'
        return '按本批公告规定的配租及选房顺序办理，重点核对适用分组、优先条件和递补安排；具体规则见原文。'
    return None

from pathlib import Path
import copy

import pytest

from app.housing.share_pack import parse_document,validate_pagination,ChromeRenderer
from app.housing.share_tool import prepare_document,make_report


def test_browser_tabs_and_markdown_produce_the_same_real_tables():
    tab='实际户型\t建筑面积\t套数\n三房二厅\t约110㎡\t537\n费用项目\t标准\t单位\n执行租金\t22.8\t元/㎡·月\n物业管理费\t3.2\t元/㎡·月'
    md='|实际户型|建筑面积|套数|\n|---|---|---|\n|三房二厅|约110㎡|537|\n\n|费用项目|标准|单位|\n|---|---|---|\n|执行租金|22.8|元/㎡·月|\n|物业管理费|3.2|元/㎡·月|'
    a=parse_document(tab,include_sources=False);b=parse_document(md,include_sources=False)
    assert len(a.blocks)==len(b.blocks)==2
    assert [v.table for v in a.blocks]==[v.table for v in b.blocks]
    assert all(v.kind=='table' for v in a.blocks)
    assert all('---' not in v.text for v in b.blocks)


def test_adjacent_markdown_tables_with_blank_lines_do_not_merge_headers():
    text='|档位|申请户数|\n\n|---|---|\n\n|40㎡|3746|\n\n|档位|第一队列|\n\n|---|---|\n\n|40㎡|330|\n\n说明：按原名单统计。'
    doc=parse_document(text,include_sources=False)
    assert [b.kind for b in doc.blocks]==['table','table','p']
    assert len(doc.blocks[0].table[0][1])==len(doc.blocks[1].table[0][1])==1
    assert doc.groups[-1]['kind']=='text'


def test_real_failed_clipboard_input_is_recognized_and_keeps_all_projects():
    text=Path('tests/housing/fixtures/share/20261026-browser-tabs.txt').read_text(encoding='utf-8')
    doc=prepare_document(text)
    tables=[b for b in doc.blocks if b.kind=='table']
    assert len(tables)>=25
    assert sum(b.table[0][0]==['费用项目','标准','单位'] for b in tables)==8
    for number in ('1963','3746','1526','1848','3810','22.8','0.25','300','537'):
        if number in text:assert number in doc.text
    for project in ('明皓府','明汇府','红坳新居','嘉禧苑','虹霓轩','凤凰英荟城','钟表基地公寓'):
        assert project in doc.text
    assert '\t' not in doc.text


def test_escaped_pipes_and_links_in_cells_are_not_truncated():
    doc=parse_document('|项目|说明|\n|---|---|\n|[小区](https://example.gov.cn/a)|一房\\|两房|',include_sources=False)
    assert doc.blocks[0].table[0][1][0]==['小区','一房|两房']
    assert 'https://' not in doc.text
    with_sources=parse_document('|项目|套数|\n|---|---|\n|[小区](https://example.gov.cn/a)|76|')
    assert with_sources.links[0].url=='https://example.gov.cn/a'


def test_inconsistent_table_rows_fail_without_silent_padding_or_data_loss():
    with pytest.raises(ValueError,match='列数不一致'):
        parse_document('费用项目\t标准\t单位\n执行租金\t22.8\t元/㎡·月\t多出数据',include_sources=False)


def test_fenced_table_example_remains_code():
    doc=parse_document('```text\n|费用|标准|\n|---|---|\n|租金|22.8|\n```',include_sources=False)
    assert len(doc.blocks)==1 and doc.blocks[0].kind=='code'


def test_chrome_paginates_table_rows_and_audits_actual_dom_cells(tmp_path):
    text='## 早报 2026-10-26\n\n项目\t实际户型\t套数\n'+'\n'.join(f'项目{i:03d}\t约50㎡一房一厅\t{i+1}' for i in range(90))
    report=make_report(text,'digest','2026-10-26','10:40');doc=prepare_document(text)
    manifest=ChromeRenderer().render(report,doc,tmp_path)
    assert len(manifest['pages'])>=2
    fragments=[f for p in manifest['pages'] for f in p['fragments'] if f.get('headers')]
    assert len(fragments)>=2 and fragments[1]['start']>0
    assert sum(len(f['rows']) for f in fragments)==90
    assert all(f['headers']==['项目','实际户型','套数'] for f in fragments)
    validate_pagination(doc,manifest)
    damaged=copy.deepcopy(manifest)
    next(f for p in damaged['pages'] for f in p['fragments'] if f.get('rows'))['rows'][0][2]='篡改'
    with pytest.raises(ValueError,match='单元格数据不完整'):validate_pagination(doc,damaged)

"""Compact HTML preserves a full long report without provider Markdown expansion."""
import re

from markdown_it import MarkdownIt


MAX_HTML_CHARS = 19_800
FOLD_PATTERN = re.compile(r'(?ms)^\*\*房源明细与官方费用\*\*[ \t\r]*\n(.*?)^\*\*申请条件与办理规则\*\*[ \t\r]*$')


def has_inventory_fold(content):
    return bool(FOLD_PATTERN.search(content))


def compact_html(content):
    # Only our two exact generated headings can create a details element.
    # Arbitrary HTML from source clauses remains escaped by MarkdownIt.
    import hashlib
    marker='HOUSINGFOLD'+hashlib.sha256(content.encode()).hexdigest()[:20]
    while marker in content:marker+='X'
    def fold(match):
        return '\n\n'+marker+'OPEN\n\n'+match[1].strip()+'\n\n'+marker+'CLOSE\n\n**申请条件与办理规则**\n'
    prepared=FOLD_PATTERN.sub(fold,content)
    html = MarkdownIt('commonmark', {'html': False, 'breaks': True}).render(prepared)
    html=html.replace('<p>'+marker+'OPEN</p>','<details><summary>房源明细与官方费用（点击展开）</summary>')
    html=html.replace('<p>'+marker+'CLOSE</p>','</details>')
    if marker in html:raise ValueError('内部折叠标记未正确渲染')
    for full, short in (('<strong>', '<b>'), ('</strong>', '</b>'),
                        ('<em>', '<i>'), ('</em>', '</i>'), ('<br />', '<br>')):
        html = html.replace(full, short)
    html = re.sub(r'(?<=>)\n(?=<)', '', html).strip()
    # HTML permits a paragraph end tag to be omitted before another block.
    # Browsers restore exactly the same paragraph boundaries; no text is cut.
    return re.sub(r'</p>(?=<(?:p|h[1-6]|ul|ol|blockquote|pre|hr|table|div|section)(?:[\s>]))', '', html)

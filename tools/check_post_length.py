"""Check a marketing post against the 小红书 limits.

只读：统计正文/标签字数，扫 U+FFFD（heredoc 写入时可能产生的编码损坏），
并确认成稿纪律里的免责关键词一个都不出现。

    python tools/check_post_length.py marketing/posts/xxx.md
"""
from __future__ import annotations

import sys
from pathlib import Path

LIMIT = 1000
BANNED = ("允许", "不一定准", "仅供参考", "十中四")
REQUIRED = ("年度计划", "认定", "配租通告")


def section(text: str, name: str) -> str:
    start = text.index(f"## {name}\n\n") + len(f"## {name}\n\n")
    rest = text[start:]
    end = rest.find("\n\n## ")
    return rest if end < 0 else rest[:end]


def main() -> None:
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")

    bad = text.count("�")
    body = section(text, "正文").strip("\n")
    tags = section(text, "标签").strip("\n")
    total = len(body) + len(tags)

    print(f"文件      {path}")
    print(f"U+FFFD    {bad}")
    print(f"正文      {len(body)}")
    print(f"标签      {len(tags)}")
    print(f"合计      {total} / {LIMIT}   余量 {LIMIT - total}")

    print("-- 免责关键词（必须全为 0）")
    for kw in BANNED:
        print(f"   {kw}  {text.count(kw)}")
    print("-- 必须出现的信源")
    for kw in REQUIRED:
        print(f"   {kw}  {text.count(kw)}")

    if bad or total > LIMIT:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

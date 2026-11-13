"""把当天的早报/晚报渲染成一张长图（宽 1080，高度按内容自适应）。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe marketing/make_daily_card.py
    .venv\\Scripts\\python.exe marketing/make_daily_card.py --evening

输出到 marketing/out/daily/。

一张 3:4（1080x1440）装不下完整日报的七千多字，所以这里出的是整张长图：
群里发一张，点开上下滑，不占字数、不刷屏。
"""
from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
OUT = ROOT / "out" / "daily"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;width:1080px;background:#0F1115;color:#fff;
  font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif}
.page{width:1080px;padding:52px 56px 60px}
h1{font-size:48px;line-height:1.22;font-weight:900;margin:0 0 6px;letter-spacing:-1px}
.date{font-size:26px;color:#FFB020;font-weight:800;letter-spacing:4px;margin-bottom:26px}
h2{font-size:34px;line-height:1.28;font-weight:900;color:#FFB020;
  margin:34px 0 14px;padding-top:18px;border-top:2px solid #232830}
p{font-size:26px;line-height:1.5;color:#C9CDD4;margin:0 0 12px}
.batch{background:#181C23;border-left:7px solid #FFB020;border-radius:0 12px 12px 0;
  padding:18px 24px;margin:0 0 14px}
.bt{font-size:28px;line-height:1.38;font-weight:800;color:#fff;margin-bottom:8px}
.bt i{font-style:normal;color:#FFB020}
.bl{font-size:24px;line-height:1.48;color:#AEB4BD;margin-top:6px}
.bl b{color:#E6E8EB;font-weight:600}
.note{font-size:23px;line-height:1.5;color:#7C838D;margin-top:8px}
.foot{margin-top:44px;padding-top:18px;border-top:2px solid #232830;
  font-size:23px;color:#6B7280;display:flex;justify-content:space-between}
"""


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def render_block(block: str) -> str:
    if block.startswith("▍"):
        label = block[1:].strip()
        if "早报" in label or "晚报" in label:
            head, _, rest = label.partition(" ")
            return (f'<h1>{esc(head)}</h1>'
                    f'<div class="date">{esc(rest or "")}</div>')
        return f"<h2>{esc(label)}</h2>"
    lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
    if lines and lines[0].startswith("【"):
        m = re.match(r"^(【[^】]+】)\s*(.*)$", lines[0])
        tag, title = (m.group(1), m.group(2)) if m else ("", lines[0])
        body = "".join(f'<div class="bl">{esc(ln)}</div>' for ln in lines[1:])
        return (f'<div class="batch"><div class="bt"><i>{esc(tag)}</i> '
                f'{esc(title)}</div>{body}</div>')
    paras = "".join(f"<p>{esc(ln)}</p>" for ln in lines)
    return paras


def build_html(text: str) -> str:
    blocks = [b.strip() for b in re.split(r"\n{2,}", text.strip()) if b.strip()]
    body = "\n".join(render_block(b) for b in blocks)
    foot = ('<div class="foot"><span>公开信息整理 · 非官方账号</span>'
            "<span>深圳保障房</span></div>")
    return ("<!doctype html><meta charset='utf-8'>"
            f"<style>{CSS}</style><div class='page'>{body}{foot}</div>"
            "<script>document.title='H='+Math.ceil(document.documentElement.scrollHeight)</script>")


def measure(session_dir: str, uri: str) -> int:
    dom = subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--user-data-dir={session_dir}", "--virtual-time-budget=3000", "--dump-dom", uri],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    hits = re.findall(r"H=(\d+)", dom)
    if not hits:
        raise RuntimeError("没能测出页面高度")
    return max(int(h) for h in hits)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evening", action="store_true", help="生成晚报（默认早报）")
    ap.add_argument("--brief", action="store_true",
                    help="精选版：只留认租中批次＋本期节点＋新增，手机上字够大")
    args = ap.parse_args()

    from app.housing.db import SessionLocal
    from app.housing.reporting import build_snapshot
    from app.housing.report_render import render_daily_plain, render_group_brief

    kind = "evening" if args.evening else "digest"
    with SessionLocal() as session:
        snap = build_snapshot(session, kind)
    text = render_group_brief(snap) if args.brief else render_daily_plain(snap)

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = f"{snap.generated_at:%Y%m%d}"
    name = f"{'晚报' if args.evening else '早报'}-{stamp}" + ("-精选" if args.brief else "")
    page = OUT / f"{name}.html"
    png = OUT / f"{name}.png"
    page.write_text(build_html(text), encoding="utf-8")

    with tempfile.TemporaryDirectory() as profile:
        height = measure(profile, page.as_uri())
        if height > 20000:
            raise RuntimeError(f"内容高度{height}px，拒绝截断；请使用 python -m app.housing.cli share-pack 导出完整分页图片")
        subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             f"--user-data-dir={profile}", "--force-device-scale-factor=1",
             f"--window-size=1080,{height}", f"--screenshot={png}", page.as_uri()],
            check=True, capture_output=True)
    print(f"字数 {len(text)}｜尺寸 1080x{height}")
    print("输出:", png)


if __name__ == "__main__":
    main()

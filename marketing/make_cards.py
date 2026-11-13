"""生成抖音图文卡片（1080x1440）。

用法：
    python marketing/make_cards.py

输出到 marketing/out/*.png。

想换成自己的真截图：把图片放到 marketing/assets/ 下，
文件名与下面 CARDS 里的 shot 字段一致（如 assets/zaobao.png），
重新跑一遍脚本，手机框里就会用你的截图替代内置的模拟推送。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
ASSETS = ROOT / "assets"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

BASE = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;width:1080px;height:1440px;overflow:hidden;
  font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif}
.card{width:1080px;height:1440px;padding:88px 72px;display:flex;flex-direction:column;
  position:relative;background:#0F1115;color:#fff}
.kicker{font-size:40px;font-weight:800;letter-spacing:8px;color:#FFB020}
.title{font-size:112px;line-height:1.16;font-weight:900;margin:26px 0 0;letter-spacing:-2px}
.sub{font-size:48px;line-height:1.55;color:#C9CDD4;margin-top:34px;font-weight:500}
.accent{color:#FFB020}
.red{color:#FF5B5B}
.footer{position:absolute;left:72px;right:72px;bottom:60px;font-size:30px;color:#6B7280;
  display:flex;justify-content:space-between;align-items:flex-end}
.phone{margin-top:52px;background:#F4F5F7;border-radius:36px;padding:26px;
  box-shadow:0 24px 60px rgba(0,0,0,.45)}
.phone img{width:100%;display:block;border-radius:14px}
.msg{background:#fff;border-radius:18px;padding:30px 32px;color:#1B1D21}
.msg .head{font-size:42px;font-weight:800;margin-bottom:8px}
.msg .meta{font-size:26px;color:#8A9099;margin-bottom:22px}
.msg .item{font-size:34px;line-height:1.5;margin:18px 0;padding-left:26px;position:relative}
.msg .item:before{content:"";position:absolute;left:0;top:16px;width:10px;height:10px;
  border-radius:50%;background:#FFB020}
.msg .item b{color:#2F6FED}
.msg .line{font-size:32px;line-height:1.6;color:#3A3F47;border-top:1px solid #EDEFF2;
  padding-top:18px;margin-top:22px}
.big{font-size:60px;font-weight:900;line-height:1.3}
.tag{display:inline-block;background:#1E2229;color:#FFB020;border-radius:999px;
  padding:12px 30px;font-size:32px;font-weight:700;margin:0 16px 16px 0}
.phone.small{text-align:center}
.phone.small img{max-height:560px;width:auto;max-width:100%}
.phone.compact{margin-top:44px}
.phone.compact .msg .line{font-size:30px;line-height:1.52;padding-top:14px;margin-top:16px}
.phone.compact .msg .head{font-size:40px}
.timeline{margin-top:auto;margin-bottom:120px}
.tl{display:flex;align-items:baseline;gap:26px;padding:24px 0;border-top:1px solid #232830}
.tl:last-child{border-bottom:1px solid #232830}
.tl .t{font-size:34px;font-weight:800;color:#FFB020;min-width:170px;letter-spacing:1px}
.tl .d{font-size:36px;color:#C9CDD4}
.steps{margin-top:64px;display:flex;flex-direction:column;gap:34px}
.step{display:flex;gap:28px;align-items:flex-start}
.step .n{font-size:44px;font-weight:900;color:#FFB020;min-width:74px}
.step .t{font-size:44px;line-height:1.45;font-weight:700}
.step .d{font-size:32px;color:#8A9099;margin-top:8px;line-height:1.5}
"""


def shot(name: str, fallback: str, cls: str = "") -> str:
    """有真截图就用真截图，没有就用内置的模拟推送。"""
    if (ASSETS / name).exists():
        return f'<div class="phone {cls}"><img src="../assets/{name}"></div>'
    return f'<div class="phone {cls}">{fallback}</div>'


CARDS: dict[str, str] = {}

CARDS["01-cover"] = """
<div class="card">
  <div class="kicker">深圳保障房</div>
  <div class="title">每天两条<br><span class="accent">不漏一批</span></div>
  <div class="sub">新通告 · 截止提醒<br>认租编号查排位</div>
  <div style="margin-top:56px">
    <span class="tag">25 个官方信息源</span>
    <span class="tag">早晚各一条</span>
    <span class="tag">只整理公开信息</span>
  </div>
  <div class="timeline">
    <div class="tl"><div class="t">08:30</div><div class="d">早报 · 今天有哪些批次在认租</div></div>
    <div class="tl"><div class="t">随时</div><div class="d">新通告 · 官网发了就推</div></div>
    <div class="tl"><div class="t">20:30</div><div class="d">晚报 · 今天新增、摇号、腾退</div></div>
    <div class="tl"><div class="t">截止前</div><div class="d">提醒 · 还剩 3 天 / 1 天各一次</div></div>
  </div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>01</span></div>
</div>
"""

CARDS["02-instant"] = """
<div class="card">
  <div class="kicker">① 新通告</div>
  <div class="title">官网发出<br><span class="accent">半小时内到</span></div>
""" + shot("instant.png", """
  <div class="msg">
    <div class="head">住房通告 2 条</div>
    <div class="meta">深圳 · 管家版　10-21 19:22</div>
    <div class="item"><b>盐田区</b> 2026 年度公租房<br>311 套 · 认租 10-22 → 11-09</div>
    <div class="item"><b>龙华区</b> 2026 年度公租房<br>894 套 · 认租 10-21 → 10-28</div>
    <div class="line">点击标题看官方原文</div>
  </div>
""") + """
  <div class="sub">市级 + 各区住建局，<br>区级网站发的也不会漏。</div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>02</span></div>
</div>
"""

CARDS["03-digest"] = """
<div class="card">
  <div class="kicker">② 每日早报 08:30</div>
  <div class="title">一屏看完<br><span class="accent">今天能报什么</span></div>
""" + shot("digest.png", """
  <div class="msg">
    <div class="head">深圳住房保障早报 10-21</div>
    <div class="meta">每天 08:30　周四</div>
    <div class="line">正在认租　<b>4 批</b><br>坪山 393 套 · 南山 616 套<br>龙华 894 套 · 光明 高层次人才</div>
    <div class="line">昨天摇号　<b>1 批</b><br>光明第一批 · 3,744 户抢 111 套</div>
    <div class="line">近 24 小时新增　<b>2 条</b><br>腾退与存量　4 条</div>
  </div>
""", "compact") + """
  <div class="sub" style="font-size:44px;margin-top:30px">在认租批次、摇号结果、<br>腾退房源，一屏看完。</div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>03</span></div>
</div>
"""

CARDS["04-evening"] = """
<div class="card">
  <div class="kicker">③ 每日晚报 20:30</div>
  <div class="title">白天漏掉的<br><span class="accent">晚上补一遍</span></div>
""" + shot("evening.png", """
  <div class="msg">
    <div class="head">深圳住房管家晚报 10-21</div>
    <div class="meta">每天 20:30</div>
    <div class="line">全市去重　认租中 <b>4 批</b> · 在途 20 批 · 本期新增 2 条</div>
    <div class="line">需要关注　坪山 B143 明天 17:30 截止<br>南山 B129 明天 18:00 截止</div>
  </div>
""", "compact") + """
  <div class="sub" style="font-size:42px;margin-top:28px">今天新增、摇号、腾退，<br>还有明天要截止的批次。</div>
  <div class="sub" style="font-size:36px;margin-top:20px;color:#8A9099">截图最下面那行「需要关注」，<br>就是把明天截止的批次单独挑出来。</div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>04</span></div>
</div>
"""

CARDS["05-remind"] = """
<div class="card">
  <div class="kicker">④ 截止提醒</div>
  <div class="title">截止前一天<br><span class="accent">再叫你一次</span></div>
""" + shot("remind.png", """
  <div class="msg">
    <div class="head">⏰ 明天截止 · 坪山区</div>
    <div class="meta">提前 1 天提醒　10-21 20:30</div>
    <div class="item">坪山区 2026 年第三批次公租房<br>393 套 · 7 个项目</div>
    <div class="item">认租截止 <b>10-22 17:30</b><br>还剩 1 天</div>
    <div class="line">错过就要等下一批</div>
  </div>
""") + """
  <div class="sub">提前 3 天、1 天各提醒一次，<br>只看你所在区。</div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>05</span></div>
</div>
"""

CARDS["06-lookup"] = """
<div class="card">
  <div class="kicker">⑤ 认租编号查排位</div>
  <div class="title">输编号<br><span class="accent">看队列和排位</span></div>
""" + shot("lookup.png", """
  <div class="msg">
    <div class="head">查询结果</div>
    <div class="meta">示例编号　SQ2026BZ3600****</div>
    <div class="line">所在队列　<b>第二队列</b>（名单标注，非最终）</div>
    <div class="line">队列内排位　<b>第 412 位</b><br>同一队列、同一户型里，顺序号在你前面的人数</div>
    <div class="line">该档房源　<b>111 套</b><br>按队列顺序，轮到的可能性较小</div>
  </div>
""") + """
  <div class="sub" style="font-size:38px;margin-top:30px;color:#E6E8EB">
    名单标注 <span class="accent">第二队列</span> · 队列内第 0 位<br>
    这档 <span class="accent">111 套</span>，前面两个队列已经占满。
  </div>
  <div class="sub" style="font-size:34px;margin-top:22px">编号本身看不出队列，队列是从官方摇号名单里算的。</div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>06</span></div>
</div>
"""

CARDS["07-districts"] = """
<div class="card">
  <div class="kicker">⑥ 覆盖范围</div>
  <div class="title" style="font-size:96px">七个区<span class="accent">都能查</span></div>
""" + shot("index.png", """
  <div class="msg">
    <div class="head">深圳保障房 · 认租编号自助查询</div>
    <div class="meta">选择你所在的区</div>
    <div class="line">宝安区 · 福田区 · 光明区 · 龙岗区<br>龙华区 · 坪山区 · 深圳市</div>
  </div>
""") + """
  <div class="sub" style="font-size:34px;margin-top:26px">没有你所在的区，说明该区暂未公布公证摇号名单；<br>公租房按轮候顺序选房，本来就没有摇号。</div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>07</span></div>
</div>
"""

CARDS["08-how"] = """
<div class="card">
  <div class="kicker">怎么用</div>
  <div class="title">三步<br><span class="accent">就够了</span></div>
  <div class="steps">
    <div class="step"><div class="n">01</div><div><div class="t">收到推送，点标题看原文</div>
      <div class="d">每一条都带官方链接，文字只是摘要</div></div></div>
    <div class="step"><div class="n">02</div><div><div class="t">查排位，输入认租编号</div>
      <div class="d">看你在哪个队列、队列内第几位</div></div></div>
    <div class="step"><div class="n">03</div><div><div class="t">等早报，每天 08:30</div>
      <div class="d">在认租批次、摇号结果、腾退房源一屏看完</div></div></div>
  </div>
  <div class="sub" style="margin-top:64px">只用公开信息，<br>姓名和证件号一律不落库。</div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>08</span></div>
</div>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile:
        for name, body in CARDS.items():
            html = f"<!doctype html><meta charset='utf-8'><style>{BASE}</style>{body}"
            page = OUT / f"{name}.html"
            page.write_text(html, encoding="utf-8")
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                 f"--user-data-dir={profile}", "--force-device-scale-factor=1",
                 "--window-size=1080,1440",
                 f"--screenshot={OUT / (name + '.png')}", page.as_uri()],
                check=True, capture_output=True,
            )
            print("ok", name)
    print("输出目录:", OUT)


if __name__ == "__main__":
    main()

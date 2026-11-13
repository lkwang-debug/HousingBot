"""生成「第一次认租」小红书卡片（1080x1440）。

用法：
    python marketing/make_rent_cards.py

输出到 marketing/out/rent/。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "rent"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

BASE = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;width:1080px;height:1440px;overflow:hidden;
  font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif}
.card{width:1080px;height:1440px;padding:88px 72px;display:flex;flex-direction:column;
  position:relative;background:#0F1115;color:#fff}
.kicker{font-size:38px;font-weight:800;letter-spacing:7px;color:#FFB020}
.title{font-size:96px;line-height:1.18;font-weight:900;margin:30px 0 0;letter-spacing:-2px}
.title.sm{font-size:78px}
.accent{color:#FFB020}
.red{color:#FF5B5B}
.sub{font-size:44px;line-height:1.55;color:#C9CDD4;margin-top:30px;font-weight:500}
.footer{position:absolute;left:72px;right:72px;bottom:58px;font-size:28px;color:#6B7280;
  display:flex;justify-content:space-between;align-items:flex-end}
.quote{margin-top:48px;background:#181C23;border-left:8px solid #FFB020;
  border-radius:0 16px 16px 0;padding:36px 40px;font-size:34px;line-height:1.66;color:#D7DBE0}
.quote .src{display:block;margin-top:22px;font-size:25px;color:#7C838D}
.plain{margin-bottom:120px}
.plain .lab{font-size:27px;color:#FFB020;font-weight:800;letter-spacing:4px}
.plain .txt{font-size:46px;line-height:1.5;font-weight:700;margin-top:16px}
.plain .txt.sm{font-size:38px;line-height:1.56;font-weight:600;color:#E6E8EB}
.tag{display:inline-block;background:#1E2229;color:#FFB020;border-radius:999px;
  padding:12px 30px;font-size:30px;font-weight:700;margin:0 14px 14px 0}
.tags{margin-top:46px}
.link{flex:1;display:flex;padding-left:38px;margin:24px 0 10px;position:relative}
.link i{display:block;width:3px;height:100%;background:repeating-linear-gradient(to bottom,#3B434F 0 12px,transparent 12px 28px)}
.link:after{content:"";position:absolute;left:31px;bottom:-4px;width:17px;height:17px;border-radius:50%;background:#FFB020;opacity:.5}
.tlist{margin-top:56px;display:flex;flex-direction:column}
.tr{display:flex;gap:26px;align-items:baseline;padding:16px 0;border-top:1px solid #232830}
.tr:last-child{border-bottom:1px solid #232830}
.tn{font-size:32px;font-weight:900;color:#FFB020;min-width:58px}
.tt{font-size:40px;color:#D7DBE0}
.check{display:flex;gap:24px;align-items:flex-start;padding:22px 0;
  border-bottom:1px solid #22262E}
.check .n{font-size:30px;font-weight:900;color:#FFB020;min-width:52px;line-height:1.5}
.check .t{font-size:34px;line-height:1.5;color:#E6E8EB}
.check .t b{color:#fff}
"""

CARDS: dict[str, str] = {}

CARDS["01-cover"] = """
<div class="card">
  <div class="kicker">深圳保租房 · 认租经验</div>
  <div class="title">第一次认租<br><span class="accent">个人经验分享</span></div>
  <div class="sub">上一篇发完，后台收到一些问题。<br>挑问得比较多的6个，一次说清。</div>
  <div class="tags">
    <span class="tag">都是私信里问的</span>
    <span class="tag">附通告出处</span>
    <span class="tag">报名前清单</span>
  </div>
  <div class="tlist">
    <div class="tr"><span class="tn">01</span><span class="tt">你可能排错队了</span></div>
    <div class="tr"><span class="tn">02</span><span class="tt">社保缴的是哪一种</span></div>
    <div class="tr"><span class="tn">03</span><span class="tt">迁户口赶不上</span></div>
    <div class="tr"><span class="tn">04</span><span class="tt">面积档只能选一个</span></div>
    <div class="tr"><span class="tn">05</span><span class="tt">没有补的机会</span></div>
    <div class="tr"><span class="tn">06</span><span class="tt">签了合同轮候号没了</span></div>
  </div>
  <div class="plain" style="margin-top:auto;margin-bottom:96px">
    <div class="txt sm accent">收藏这张，报名前对一遍</div>
  </div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>深圳保障房</span></div>
</div>
"""

CARDS["02-queue"] = """
<div class="card">
  <div class="kicker">01 · 报名前</div>
  <div class="title sm">你可能<br>排错队了</div>
  <div class="quote">
    第一队列：申请人具有光明区户籍，在本市正常缴纳社会保险……且在本市累计缴纳社会保险3年以上。<br><br>
    第二队列：申请人在光明区企事业单位正常缴纳社会保险……累计1年以上。
    <span class="src">光明住保租〔2026〕038号</span>
  </div>
  <div class="link"><i></i></div>
  <div class="plain">
    <div class="lab">翻译成人话</div>
    <div class="txt">第二队列看的是社保缴在哪，<br>不是户口在哪。</div>
    <div class="txt sm">你在光明上班、户口在南山，<br>走的是第二队列。</div>
  </div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>深圳保障房</span></div>
</div>
"""

CARDS["03-social"] = """
<div class="card">
  <div class="kicker">02 · 报名前</div>
  <div class="title sm">社保缴的<br>是哪一种</div>
  <div class="quote">
    在本市正常缴纳社会保险（养老保险或者医疗保险，不含少儿医疗保险）。
    <span class="src">光明 / 坪山 / 龙岗等多批通告通用表述</span>
  </div>
  <div class="link"><i></i></div>
  <div class="plain">
    <div class="lab">容易搞错的地方</div>
    <div class="txt">你自己缴的养老或医疗，算。<br>给孩子缴的少儿医保，不算。</div>
    <div class="txt sm" style="margin-top:22px">年限看的是累计，断过也不清零。</div>
  </div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>深圳保障房</span></div>
</div>
"""

CARDS["04-hukou"] = """
<div class="card">
  <div class="kicker">03 · 报名前</div>
  <div class="title sm">迁户口<br>赶不上</div>
  <div class="quote">
    户籍以2026年9月30日18:00前属于光明区户籍为准，2026年9月30日18:00及之后户籍从其他区迁入光明区，不视为符合"光明区户籍"条件。
    <span class="src">光明住保租〔2026〕038号</span>
  </div>
  <div class="link"><i></i></div>
  <div class="plain">
    <div class="lab">说白一点</div>
    <div class="txt">截止前一天把户口迁过来，<br>还是第三队列。</div>
    <div class="txt sm" style="margin-top:22px">每个区都有自己的时间节点，<br>看通告里那句"以某日某时为准"。</div>
  </div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>深圳保障房</span></div>
</div>
"""

CARDS["05-area"] = """
<div class="card">
  <div class="kicker">04 · 填表时</div>
  <div class="title sm">面积档<br>只能选一个</div>
  <div class="quote">
    申请人不能同时认租不同面积档位的户型。
    <span class="src">光明住保租〔2026〕038号</span>
  </div>
  <div class="link"><i></i></div>
  <div class="plain">
    <div class="lab">但人口数可以加</div>
    <div class="txt">配偶、未成年子女、父母、<br>配偶的父母都计入配租人口。</div>
    <div class="txt sm" style="margin-top:22px">40㎡ 单身 / 50㎡ 二至三人<br>90㎡、100㎡+ 四人以上<br>选错档，号再小也没用。</div>
  </div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>深圳保障房</span></div>
</div>
"""

CARDS["06-material"] = """
<div class="card">
  <div class="kicker">05 · 提交后</div>
  <div class="title sm">没有<br>补的机会</div>
  <div class="quote">
    本次认租不设补正环节。
    <span class="src">光明住保租〔2026〕038号</span>
  </div>
  <div class="link"><i></i></div>
  <div class="plain">
    <div class="lab">不是"会通知你补"</div>
    <div class="txt">是直接出局。</div>
    <div class="txt sm" style="margin-top:22px">认租截止后，住房、人口、户籍、<br>婚姻、认租意向一律不能改。<br>认租编号只对本批次有效。</div>
  </div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>深圳保障房</span></div>
</div>
"""

CARDS["07-lunhou"] = """
<div class="card">
  <div class="kicker">06 · 最贵的坑</div>
  <div class="title sm">签了合同<br><span class="red">轮候号没了</span></div>
  <div class="quote">
    认租家庭选定住房并签订租赁合同的，将立即退出轮候库，原轮候排序作废。
    <span class="src">坪山锦绣华晟家园配租通告</span>
  </div>
  <div class="link"><i></i></div>
  <div class="plain">
    <div class="lab">还有一条连着的</div>
    <div class="txt">放弃选房累计三次，<br>三年内不得再申请。</div>
    <div class="txt sm" style="margin-top:22px">排序轮到你了、有房、你没选，<br>也算一次放弃。</div>
  </div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>深圳保障房</span></div>
</div>
"""

CARDS["08-checklist"] = """
<div class="card">
  <div class="kicker">保存这张</div>
  <div class="title sm">报名前<br>对着过一遍</div>
  <div style="margin-top:40px">
    <div class="check"><div class="n">01</div><div class="t">确认自己在<b>哪个队列</b></div></div>
    <div class="check"><div class="n">02</div><div class="t">社保是<b>养老或医疗</b>，累计年限够</div></div>
    <div class="check"><div class="n">03</div><div class="t">户籍<b>迁入时间</b>在节点之前</div></div>
    <div class="check"><div class="n">04</div><div class="t">面积档<b>只选一个</b>，别选错</div></div>
    <div class="check"><div class="n">05</div><div class="t">共同申请人<b>全部加上</b></div></div>
    <div class="check"><div class="n">06</div><div class="t">材料存成<b>清晰电子版</b>，没有补正</div></div>
    <div class="check"><div class="n">07</div><div class="t">认租截止后<b>什么都不能改</b></div></div>
    <div class="check"><div class="n">08</div><div class="t">想清楚：签了<b>公租房轮候号作废</b></div></div>
  </div>
  <div class="plain" style="margin-top:auto;margin-bottom:104px">
    <div class="txt sm">具体规定各区各批不同，<br>以你要报名那批的通告为准。</div>
  </div>
  <div class="footer"><span>公开信息整理 · 非官方账号</span><span>深圳保障房</span></div>
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

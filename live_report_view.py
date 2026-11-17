#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直播自查报告渲染（版式参照 danmakus 的互动画像：指标卡 + 趋势组合图 + 双热力图 + 留痕构成 + 打米记录 + 频道偏好）。"""

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

CSS = """
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2129;--line:#232a35;--fg:#e6edf3;
--dim:#8b949e;--blue:#3b82f6;--teal:#2dd4bf;--orange:#f59e0b;--pink:#fb7299}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 22px 70px}
.hero{background:linear-gradient(135deg,#161b22,#1a2230);border:1px solid var(--line);
border-radius:14px;padding:22px 24px;display:flex;gap:18px;align-items:center}
.avatar{width:76px;height:76px;border-radius:14px;flex:0 0 auto;background:#1f2630;
display:flex;align-items:center;justify-content:center;font-size:30px}
.avatar img{width:76px;height:76px;object-fit:cover;border-radius:14px;display:block}
.hero h1{margin:2px 0 10px;font-size:26px;font-weight:650;letter-spacing:.5px}
.kicker{color:var(--dim);font-size:12px;letter-spacing:1.5px}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{background:#11161e;border:1px solid #2b3442;border-radius:7px;padding:3px 10px;font-size:12.5px}
.note{color:var(--dim);font-size:12px;margin-top:12px}
h2{font-size:15px;margin:26px 0 12px;color:var(--dim);font-weight:600;letter-spacing:.6px}
.grid{display:grid;grid-template-columns:repeat(6,1fr);gap:14px}
.grid.five{grid-template-columns:repeat(5,1fr)}
@media(max-width:1000px){.grid{grid-template-columns:repeat(3,1fr)}}
@media(max-width:1000px){.grid.five{grid-template-columns:repeat(3,1fr)}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px 16px 12px;
position:relative;overflow:hidden}
.card .k{color:var(--dim);font-size:12.5px;margin-bottom:6px}
.card .v{font-size:26px;font-weight:650;line-height:1.15}
.card .v small{font-size:13px;color:var(--dim);font-weight:400;margin-left:2px}
.card .s{color:var(--dim);font-size:11.5px;margin-top:5px}
.card::after{content:"";position:absolute;left:0;right:0;bottom:0;height:2px;
background:linear-gradient(90deg,var(--blue),var(--teal))}
.card.money::after{background:linear-gradient(90deg,var(--orange),var(--pink))}
.card.plain{background:var(--card2)}
.card.plain .v{font-size:20px}
.tabs{display:flex;gap:22px;border-bottom:1px solid var(--line);margin:30px 0 22px;padding-bottom:0}
.tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--dim);
font:inherit;font-size:13.5px;padding:8px 2px 12px;cursor:pointer}
.tabs button.on{color:var(--fg);border-bottom-color:var(--pink)}
.tabpane{display:block}
.scrollbox{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
.scrollbox table{min-width:860px}
.scrollbox thead th{position:sticky;top:0;background:var(--card2);z-index:1}
.num2{text-align:right;font-variant-numeric:tabular-nums}
.sub{color:var(--dim);font-size:11px;font-weight:400}
.amt{color:var(--orange);font-variant-numeric:tabular-nums;white-space:nowrap}
.empty{color:var(--dim);text-align:center;padding:26px}
.warnnote{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.28);
color:#f0c674;border-radius:8px;padding:9px 11px;font-size:12px;margin-top:12px;line-height:1.55}
.legend button{background:none;border:0;color:var(--dim);font:inherit;font-size:12px;cursor:pointer;
padding:0;display:flex;align-items:center}
.legend button.off{opacity:.35;text-decoration:line-through}
.trend-panel .head{margin-bottom:8px}
.career-trend .x-axis-hit{cursor:pointer}
.trend-actions{display:none;align-items:center;justify-content:space-between;gap:14px;
margin-top:4px;min-height:34px}
.trend-actions.on{display:flex}
.trend-range{color:var(--dim);font-size:12px}
.trend-reset{background:#11161e;color:var(--fg);border:1px solid #2b3442;border-radius:7px;
padding:6px 12px;font:inherit;font-size:12px;cursor:pointer}
.trend-reset:hover{border-color:var(--pink);color:#fff}
.periods{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:18px 0 4px}
.periods .label{color:var(--dim);font-size:12px;margin-right:4px}
.periods a{background:#11161e;border:1px solid var(--line);color:var(--dim);border-radius:7px;
padding:5px 13px;text-decoration:none;font-size:12.5px}
.periods a.on{color:#fff;border-color:var(--pink);background:rgba(251,114,153,.13)}
.periods select{background:#11161e;border:1px solid var(--line);color:var(--fg);
border-radius:7px;padding:5px 10px;font:inherit;font-size:12.5px}
[data-tip]{cursor:pointer}
.heat .cell:hover,.cal .cell:hover{stroke:#e6edf3;stroke-width:1}
.hoverband:hover{fill:rgba(148,163,184,.10)}
#tip{position:fixed;display:none;background:#1c2129;border:1px solid #2b3442;border-radius:8px;
padding:6px 10px;font-size:12px;color:var(--fg);pointer-events:none;z-index:50;
box-shadow:0 8px 24px rgba(0,0,0,.5);white-space:nowrap}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:18px}
.panel .head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px}
.panel .head .t{font-size:15px;font-weight:600}
.panel .head .s{color:var(--dim);font-size:12px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:900px){.two{grid-template-columns:1fr}}
.legend{display:flex;gap:16px;font-size:12px;color:var(--dim);flex-wrap:wrap}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--dim);font-weight:500}
.cal{display:block}
.compose .row{display:grid;grid-template-columns:120px 1fr 74px;gap:10px;align-items:center;margin:7px 0}
.compose .label{color:var(--dim);font-size:12.5px}
.compose .track{height:12px;background:#11161e;border-radius:6px;overflow:hidden}
.compose .fill{height:100%;border-radius:6px}
.compose .num{text-align:right;font-size:12.5px;font-variant-numeric:tabular-nums}
"""

COLORS = {"弹幕": "#3b82f6", "礼物": "#f59e0b", "醒目留言": "#a855f7",
          "上舰": "#fb7299", "互动": "#2dd4bf", "入场记录": "#64748b"}


def esc(t):
    return (str(t if t is not None else "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(t):
    return esc(t).replace('"', "&quot;").replace("'", "&#39;")


def avatar_html(account, size=96):
    face = str(account.get("face") or "").split("@", 1)[0]
    data_uri = str(account.get("_avatar_data_uri") or "")
    if face or data_uri:
        url = data_uri or ("%s@%dw_%dh_1c.png" % (face, size, size))
        return ("<div class='avatar has-image'><img src='%s' alt='账号头像' "
                "referrerpolicy='no-referrer'></div>" % esc_attr(url))
    return "<div class='avatar'>🀄</div>"


def money(v):
    return ("%.1f" % v) if v else "0"


def card(k, v, unit, sub, cls=""):
    return ('<div class="card %s"><div class="k">%s</div>'
            '<div class="v">%s<small>%s</small></div><div class="s">%s</div></div>'
            % (cls, esc(k), esc(v), esc(unit), esc(sub)))


def daily_series(events, start, end):
    """按日聚合：弹幕 / 其他互动 / 付费金额。"""
    if start > end:
        start = end
    days = (end - start).days + 1
    danmu, other, paid = defaultdict(int), defaultdict(int), defaultdict(float)
    for e in events:
        d = datetime.fromtimestamp((e.get("ts") or 0) / 1000.0, TZ).date()
        if d < start:
            continue
        if e.get("cmd") == "DANMU_MSG":
            danmu[d] += 1
        elif e.get("cmd") in ("SEND_GIFT", "GUARD_BUY", "SUPER_CHAT_MESSAGE"):
            other[d] += 1
            paid[d] += e.get("_yuan", 0.0)
        elif e.get("cmd") in ("LIKE_INFO_V3_CLICK",):
            other[d] += 1
        elif (str(e.get("cmd") or "").startswith("INTERACT_WORD")
              and e.get("interact") != "进入直播间"):
            other[d] += 1
    xs = [start + timedelta(days=i) for i in range(days)]
    return xs, [danmu[d] for d in xs], [other[d] for d in xs], [paid[d] for d in xs]


def svg_trend(xs, danmu, other, paid, interactive=False):
    W, H, PL, PR, PT, PB = 1080, 260, 44, 52, 16, 34
    iw, ih = W - PL - PR, H - PT - PB
    top = max(4, max([d + o for d, o in zip(danmu, other)] + [1]))
    mtop = max(1.0, max(paid) if paid else 1.0)
    bw = max(3.0, min(18.0, iw / max(1, len(xs)) * 0.62))
    step = iw / max(1, len(xs))
    dbars, obars, pts, labels, hov = [], [], [], [], []
    for i, x in enumerate(xs):
        cx = PL + step * i + step / 2
        h1 = danmu[i] / top * ih
        h2 = other[i] / top * ih
        if h1:
            dbars.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#3b82f6" rx="2"/>'
                         % (cx - bw / 2, PT + ih - h1, bw, h1))
        if h2:
            obars.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#2dd4bf" rx="2"/>'
                         % (cx - bw / 2, PT + ih - h1 - h2, bw, h2))
        pts.append("%.1f,%.1f" % (cx, PT + ih - paid[i] / mtop * ih))
        if i % max(1, len(xs) // 14) == 0:
            labels.append('<text x="%.1f" y="%d" fill="#8b949e" font-size="10" text-anchor="middle">%s</text>'
                          % (cx, H - 12, x.strftime("%m-%d")))
        hov.append('<rect class="hoverband" x="%.1f" y="%d" width="%.1f" height="%d" fill="transparent" '
                   'data-tip="%s｜弹幕 %d 条 · 其他互动 %d 次 · 付费 ¥%.1f"/>'
                   % (PL + step * i, PT, step, ih, x.strftime("%Y-%m-%d"), danmu[i], other[i], paid[i]))
    grid = []
    for k in range(5):
        y = PT + ih * k / 4
        grid.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#21262d"/>' % (PL, y, W - PR, y))
        grid.append('<text x="%d" y="%.1f" fill="#8b949e" font-size="10" text-anchor="end">%d</text>'
                    % (PL - 6, y + 3, round(top * (4 - k) / 4)))
        grid.append('<text x="%d" y="%.1f" fill="#f59e0b" font-size="10" text-anchor="start">%s</text>'
                    % (W - PR + 6, y + 3, ("%.0f" % (mtop * (4 - k) / 4))))
    classes = "trend career-trend" if interactive else "trend"
    data_attrs = ' data-days="%d" data-start="0"' % len(xs) if interactive else ""
    xhit = ""
    if interactive:
        xhit = ('<rect class="x-axis-hit" x="%d" y="%d" width="%d" height="%d" '
                'fill="rgba(0,0,0,0)"><title>点击选择起始日期</title></rect>'
                % (PL, PT + ih, iw, PB))
    return ('<svg class="%s" viewBox="0 0 %d %d" width="100%%" height="%d"%s>%s'
            '<g class="s-danmu">%s</g><g class="s-other">%s</g>%s'
            '<polyline class="s-paid" points="%s" fill="none" stroke="#f59e0b" stroke-width="2"/>'
            '<g class="hovers">%s</g>%s</svg>'
            % (classes, W, H, H, data_attrs, "".join(grid), "".join(dbars), "".join(obars),
               "".join(labels), " ".join(pts), "".join(hov), xhit))


def career_trend_script(xs, danmu, other, paid):
    """生涯趋势的浏览器端重绘逻辑；点击 X 轴后从所选日期重新绘制。"""
    payload = json.dumps({
        "dates": [x.isoformat() for x in xs],
        "danmu": danmu,
        "other": other,
        "paid": paid,
    }, ensure_ascii=False, separators=(",", ":"))
    return [
        "var careerTrendData=%s;" % payload,
        "var careerTrendStart=0;",
        "function trendRect(x,y,w,h,color){return '<rect x=\"'+x.toFixed(1)+'\" y=\"'+y.toFixed(1)"
        "+'\" width=\"'+w.toFixed(1)+'\" height=\"'+h.toFixed(1)+'\" fill=\"'+color+'\" rx=\"2\"/>';}",
        "function careerTrendSvg(start){",
        "var dates=careerTrendData.dates,dmAll=careerTrendData.danmu,otAll=careerTrendData.other,pdAll=careerTrendData.paid;",
        "start=Math.max(0,Math.min(start,dates.length-1));",
        "var xs=dates.slice(start),dm=dmAll.slice(start),ot=otAll.slice(start),pd=pdAll.slice(start),n=xs.length;",
        "var W=1080,H=260,PL=44,PR=52,PT=16,PB=34,iw=W-PL-PR,ih=H-PT-PB,rawTop=0,rawPay=0;",
        "for(var i=0;i<n;i++){rawTop=Math.max(rawTop,dm[i]+ot[i]);rawPay=Math.max(rawPay,pd[i]);}",
        "var top=Math.max(4,rawTop),mtop=Math.max(1,rawPay),step=iw/Math.max(1,n),"
        "bw=Math.max(3,Math.min(18,step*.62)),dbars='',obars='',pts=[],labels='',hov='';",
        "for(var i=0;i<n;i++){var cx=PL+step*i+step/2,h1=dm[i]/top*ih,h2=ot[i]/top*ih;",
        "if(h1)dbars+=trendRect(cx-bw/2,PT+ih-h1,bw,h1,'#3b82f6');",
        "if(h2)obars+=trendRect(cx-bw/2,PT+ih-h1-h2,bw,h2,'#2dd4bf');",
        "pts.push(cx.toFixed(1)+','+(PT+ih-pd[i]/mtop*ih).toFixed(1));",
        "if(i%Math.max(1,Math.floor(n/14))===0)labels+='<text x=\"'+cx.toFixed(1)+'\" y=\"'+(H-12)"
        "+'\" fill=\"#8b949e\" font-size=\"10\" text-anchor=\"middle\">'+xs[i].slice(5)+'</text>';",
        "hov+='<rect class=\"hoverband\" x=\"'+(PL+step*i).toFixed(1)+'\" y=\"'+PT+'\" width=\"'"
        "+step.toFixed(1)+'\" height=\"'+ih+'\" fill=\"transparent\" data-tip=\"'+xs[i]+'｜弹幕 '+dm[i]"
        "+' 条 · 其他互动 '+ot[i]+' 次 · 付费 ¥'+pd[i].toFixed(1)+'\"/>';}",
        "var grid='';for(var k=0;k<5;k++){var y=PT+ih*k/4;",
        "grid+='<line x1=\"'+PL+'\" y1=\"'+y.toFixed(1)+'\" x2=\"'+(W-PR)+'\" y2=\"'+y.toFixed(1)"
        "+'\" stroke=\"#21262d\"/><text x=\"'+(PL-6)+'\" y=\"'+(y+3).toFixed(1)"
        "+'\" fill=\"#8b949e\" font-size=\"10\" text-anchor=\"end\">'+Math.round(top*(4-k)/4)"
        "+'</text><text x=\"'+(W-PR+6)+'\" y=\"'+(y+3).toFixed(1)"
        "+'\" fill=\"#f59e0b\" font-size=\"10\" text-anchor=\"start\">'+Math.round(mtop*(4-k)/4)+'</text>';}",
        "var xhit='<rect class=\"x-axis-hit\" x=\"'+PL+'\" y=\"'+(PT+ih)+'\" width=\"'+iw"
        "+'\" height=\"'+PB+'\" fill=\"rgba(0,0,0,0)\"><title>点击选择起始日期</title></rect>';",
        "return '<svg class=\"trend career-trend\" viewBox=\"0 0 1080 260\" width=\"100%\" height=\"260\""
        " data-days=\"'+n+'\" data-start=\"'+start+'\"><g>'+grid+'</g><g class=\"s-danmu\">'+dbars"
        "+'</g><g class=\"s-other\">'+obars+'</g>'+labels+'<polyline class=\"s-paid\" points=\"'"
        "+pts.join(' ')+'\" fill=\"none\" stroke=\"#f59e0b\" stroke-width=\"2\"/><g class=\"hovers\">'"
        "+hov+'</g>'+xhit+'</svg>';}",
        "function renderCareerTrend(start){",
        "var stage=document.getElementById('careerTrendStage');if(!stage)return;",
        "careerTrendStart=Math.max(0,Math.min(start,careerTrendData.dates.length-1));",
        "stage.innerHTML=careerTrendSvg(careerTrendStart);",
        "document.getElementById('careerTrendActions').classList.toggle('on',careerTrendStart>0);",
        "document.getElementById('careerTrendRange').textContent=careerTrendStart>0?"
        "'当前显示 '+careerTrendData.dates[careerTrendStart]+' 起，共 '"
        "+(careerTrendData.dates.length-careerTrendStart)+' 天':' ';",
        "applyTrendSeries();}",
    ]


def svg_week_hours(events):
    grid = Counter()
    for e in events:
        if _is_entry(e):
            continue
        dt = datetime.fromtimestamp((e.get("ts") or 0) / 1000.0, TZ)
        grid[(dt.weekday(), dt.hour)] += 1
    mx = max(grid.values()) if grid else 1
    cw, ch = 36, 26
    W, H = 44 + 24 * cw, 26 + 7 * ch
    out = []
    for h in range(24):
        if h % 3 == 0:
            out.append('<text x="%d" y="14" fill="#8b949e" font-size="10">%d时</text>' % (44 + h * cw, h))
    for w in range(7):
        out.append('<text x="38" y="%d" fill="#8b949e" font-size="11" text-anchor="end">%s</text>'
                   % (26 + w * ch + 17, WEEK[w]))
        for h in range(24):
            n = grid.get((w, h), 0)
            a = 0.06 + 0.94 * (n / mx) if n else 0.05
            fill = "rgba(59,130,246,%.2f)" % a if n else "rgba(255,255,255,.045)"
            out.append('<rect class="cell" x="%d" y="%d" width="%d" height="%d" rx="3" fill="%s" '
                       'data-tip="%s %d时 · %d 次"></rect>'
                       % (44 + h * cw, 26 + w * ch, cw - 4, ch - 4, fill, WEEK[w], h, n))
    return '<svg class="heat" viewBox="0 0 %d %d" width="100%%" height="%d">%s</svg>' % (W, H, H, "".join(out))


def svg_calendar(events):
    """按周排列的活跃日历（横向为周、纵向为周几，顶部标注月份）。"""
    days = Counter()
    for e in events:
        dt = datetime.fromtimestamp((e.get("ts") or 0) / 1000.0, TZ)
        days[dt.date()] += 1
    if not days:
        return '<div class="note">暂无数据</div>'
    first = min(days)
    last = max(days)
    mx = max(days.values())
    cw, ch = 15, 15
    start = first - timedelta(days=first.weekday())
    weeks = (last - start).days // 7 + 1
    W, H = 34 + weeks * cw, 22 + 7 * ch
    out = []
    for w in range(weeks):
        for wd in range(7):
            day = start + timedelta(days=w * 7 + wd)
            if day < first or day > last:
                continue
            n = days.get(day, 0)
            fill = ("rgba(45,212,191,%.2f)" % (0.20 + 0.80 * (n / mx))) if n else "rgba(255,255,255,.075)"
            out.append('<rect class="cell" x="%d" y="%d" width="%d" height="%d" rx="3" fill="%s" '
                       'data-tip="%s · %d 次"></rect>'
                       % (34 + w * cw, 20 + wd * ch, cw - 4, ch - 4, fill,
                          day.strftime("%Y-%m-%d"), n))
    labelled = set()
    for i in range(weeks):
        d0 = start + timedelta(days=i * 7)
        for k in range(7):
            d = d0 + timedelta(days=k)
            if (d.month, d.year) not in labelled and d.day <= 7:
                labelled.add((d.month, d.year))
                out.append('<text x="%d" y="13" fill="#8b949e" font-size="10">%d月</text>'
                           % (34 + i * cw, d.month))
                break
    return '<svg class="cal" viewBox="0 0 %d %d" width="100%%" height="%d">%s</svg>' % (W, H, H, "".join(out))


def compose(rows, fmt="%d"):
    total = sum(n for _, n, _ in rows) or 1
    out = []
    for name, n, c in rows:
        out.append('<div class="row"><div class="label">%s</div>'
                   '<div class="track"><div class="fill" style="width:%.1f%%;background:%s"></div></div>'
                   '<div class="num">%s</div></div>' % (esc(name), n / total * 100, c, fmt % n))
    return "".join(out)


PAID_CMDS = ("SEND_GIFT", "GUARD_BUY", "SUPER_CHAT_MESSAGE")


def fmt_dt(ms, pattern="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp((ms or 0) / 1000.0, TZ).strftime(pattern)


def short(t, n=42):
    t = str(t or "")
    return t if len(t) <= n else t[:n] + "…"


def money_panel(events, room_names):
    """打米记录：总额、按月/按类型构成、逐笔明细。"""
    paid = [e for e in events if e.get("cmd") in PAID_CMDS and (e.get("_yuan") or 0) > 0]
    total = sum(e.get("_yuan") or 0.0 for e in paid)
    gift = sum(e.get("_yuan") or 0.0 for e in paid if e["cmd"] == "SEND_GIFT")
    guard = sum(e.get("_yuan") or 0.0 for e in paid if e["cmd"] == "GUARD_BUY")
    sc = sum(e.get("_yuan") or 0.0 for e in paid if e["cmd"] == "SUPER_CHAT_MESSAGE")
    rooms = {e.get("room") for e in paid}
    biggest = max([e.get("_yuan") or 0.0 for e in paid], default=0.0)
    out = [
        "<div class='panel'><div class='head'><div><div class='kicker'>SPENDING</div>"
        "<div class='t'>打米记录</div></div><div class='s'>免费礼物不计入消费金额</div></div>",
        "<div class='grid'>",
        card("打米总额", "%.2f" % total, "元", "共 %d 笔付费记录" % len(paid), "money"),
        card("礼物", "%.2f" % gift, "元", "%d 笔" % sum(1 for e in paid if e["cmd"] == "SEND_GIFT"), "plain"),
        card("上舰", "%.2f" % guard, "元", "%d 次" % sum(1 for e in paid if e["cmd"] == "GUARD_BUY"), "plain"),
        card("醒目留言", "%.2f" % sc, "元", "%d 条" % sum(1 for e in paid if e["cmd"] == "SUPER_CHAT_MESSAGE"), "plain"),
        card("涉及频道", len(rooms), "个", "平均单笔 ¥%.2f" % (total / max(1, len(paid))), "plain"),
        card("最大单笔", "%.2f" % biggest, "元", "单条记录金额", "plain"),
        "</div></div>",
    ]
    if not paid:
        out.append("<div class='panel'><div class='empty'>暂无打米记录</div></div>")
        return "".join(out)
    months = defaultdict(float)
    for e in paid:
        months[fmt_dt(e.get("ts"), "%Y-%m")] += e.get("_yuan") or 0.0
    out.append("<div class='two'>")
    out.append("<div class='panel'><div class='head'><div class='t'>按月打米</div>"
               "<div class='s'>%d 个月</div></div><div class='compose'>%s</div></div>"
               % (len(months), compose([(m, v, COLORS["礼物"]) for m, v in sorted(months.items())],
                                       "¥%.1f")))
    kinds = [("礼物", gift, COLORS["礼物"]), ("上舰", guard, COLORS["上舰"]),
             ("醒目留言", sc, COLORS["醒目留言"])]
    out.append("<div class='panel'><div class='head'><div class='t'>按类型</div>"
               "<div class='s'>金额构成</div></div><div class='compose'>%s</div></div>"
               % compose([r for r in kinds if r[1] > 0], "¥%.1f"))
    out.append("</div>")
    rows = []
    for e in sorted(paid, key=lambda x: x.get("ts") or 0, reverse=True):
        rid = e.get("room")
        name = room_names.get(rid, "房间 %s" % rid)
        if e["cmd"] == "SEND_GIFT":
            kind, content = "礼物", "%s ×%s" % (e.get("gift") or "礼物", e.get("num") or 1)
        elif e["cmd"] == "GUARD_BUY":
            kind, content = "上舰", "%s ×%s" % (e.get("guard") or "舰长", e.get("num") or 1)
        else:
            kind, content = "醒目留言", short(e.get("text") or "", 36)
        rows.append("<tr><td>%s</td><td><div class='ch'>%s</div><div class='sub'>%s</div></td>"
                    "<td>%s</td><td>%s</td><td class='num2 amt'>¥%.2f</td></tr>"
                    % (fmt_dt(e.get("ts")), esc(name), esc(e.get("area") or ""),
                       esc(kind), esc(content), e.get("_yuan") or 0.0))
    out.append("<div class='panel'><div class='head'><div class='t'>明细</div>"
               "<div class='s'>按时间倒序 · %d 笔</div></div><div class='scrollbox'><table>"
               "<thead><tr><th>时间</th><th>频道</th><th>类型</th><th>内容</th>"
               "<th class='num2'>金额</th></tr></thead><tbody>%s</tbody></table></div></div>"
               % (len(rows), "".join(rows)))
    return "".join(out)


def channel_panel(events, room_names, medal_names=None):
    """频道偏好：互动 / 弹幕 / 打米 / 场次的频道排行。"""
    medal_names = medal_names or {}
    stats = {}
    for e in events:
        rid, cmd = e.get("room"), e.get("cmd")
        if not rid:
            continue
        is_entry = _is_entry(e)
        if not is_entry and cmd not in ("DANMU_MSG", "SEND_GIFT", "SUPER_CHAT_MESSAGE",
                                        "GUARD_BUY", "LIKE_INFO_V3_CLICK"):
            continue
        s = stats.setdefault(rid, {"n": 0, "danmu": 0, "entry": 0, "pay": 0.0, "last": 0,
                                   "area": "", "sessions": set(), "medal_name": "",
                                   "medal_level": 0, "medal_name_ts": 0})
        level = e.get("medal_level") or e.get("medal") or 0
        try:
            level = int(level)
        except (TypeError, ValueError):
            level = 0
        if level:
            s["medal_level"] = max(s["medal_level"], level)
            medal_name = str(e.get("medal_name") or "").strip()
            stamp = e.get("ts") or 0
            if medal_name and stamp >= s["medal_name_ts"]:
                s["medal_name"] = medal_name
                s["medal_name_ts"] = stamp
        if cmd == "DANMU_MSG":
            s["danmu"] += 1
            s["n"] += 1
        elif is_entry:
            s["entry"] += 1
        else:
            s["n"] += 1
        s["pay"] += e.get("_yuan") or 0.0
        s["last"] = max(s["last"], e.get("ts") or 0)
        if e.get("area"):
            s["area"] = e["area"]
        if e.get("live_id"):
            s["sessions"].add(("live", e["live_id"]))
        elif e.get("ts"):
            s["sessions"].add(("day", rid, fmt_dt(e.get("ts"), "%Y-%m-%d")))
    total_n = sum(s["n"] for s in stats.values()) or 1
    ranked = sorted(stats.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["entry"], -kv[1]["last"]))
    pay_ranked = sorted([kv for kv in stats.items() if kv[1]["pay"] > 0],
                        key=lambda kv: -kv[1]["pay"])
    top_name = room_names.get(ranked[0][0], "—") if ranked else "—"
    top_n = ranked[0][1]["n"] if ranked else 0
    pay_name = room_names.get(pay_ranked[0][0], "—") if pay_ranked else "—"
    pay_top = pay_ranked[0][1]["pay"] if pay_ranked else 0.0
    danmu_top = max(stats.items(), key=lambda kv: kv[1]["danmu"]) if stats else None
    cards = [
        card("互动频道", len(stats), "个", "含仅入场到访的频道", "plain"),
        card("最常互动", top_name, "", "%d 次 · 占比 %.1f%%" % (top_n, top_n * 100.0 / total_n), "plain"),
        card("消费最多", pay_name, "", "¥%.2f" % pay_top, "plain"),
        card("打米频道", len(pay_ranked), "个", "有过付费记录的频道", "plain"),
        card("弹幕最多", room_names.get(danmu_top[0], "—") if danmu_top else "—", "",
             "%d 条" % (danmu_top[1]["danmu"] if danmu_top else 0), "plain"),
        card("平均每频道", "%.1f" % (total_n / max(1, len(stats))), "次", "互动次数均值", "plain"),
    ]
    out = [
        "<div class='panel'><div class='head'><div><div class='kicker'>AFFINITY</div>"
        "<div class='t'>频道偏好</div></div><div class='s'>互动不含入场与账号通知</div></div>",
        "<div class='grid'>%s</div></div>" % "".join(cards),
    ]
    if not stats:
        out.append("<div class='panel'><div class='empty'>暂无频道数据</div></div>")
        return "".join(out)
    out.append("<div class='two'>")
    out.append("<div class='panel'><div class='head'><div class='t'>互动 TOP 10</div>"
               "<div class='s'>按互动次数</div></div><div class='compose'>%s</div></div>"
               % compose([(room_names.get(rid, "房间 %s" % rid), s["n"], COLORS["弹幕"])
                          for rid, s in ranked[:10]]))
    out.append("<div class='panel'><div class='head'><div class='t'>打米 TOP 10</div>"
               "<div class='s'>按付费金额</div></div><div class='compose'>%s</div></div>"
               % compose([(room_names.get(rid, "房间 %s" % rid), s["pay"], COLORS["礼物"])
                          for rid, s in pay_ranked[:10]], "¥%.1f"))
    out.append("</div>")
    rows = []
    for rid, s in ranked:
        name = room_names.get(rid, "房间 %s" % rid)
        pay_cell = ("<td class='num2 amt'>¥%.2f</td>" % s["pay"]) if s["pay"] else "<td class='num2'>—</td>"
        medal_name = s["medal_name"] or medal_names.get(rid, "")
        medal_cell = (esc(medal_name) if medal_name
                      else ("未记录" if s["medal_level"] else "—"))
        level_cell = ("LV%d" % s["medal_level"]) if s["medal_level"] else "—"
        rows.append("<tr><td><div class='ch'>%s</div><div class='sub'>%s</div></td>"
                    "<td>%s</td><td class='num2'>%s</td>"
                    "<td class='num2'>%d</td><td class='num2'>%.1f%%</td>"
                    "<td class='num2'>%d</td><td class='num2'>%d</td>%s"
                    "<td class='num2'>%d</td><td>%s</td></tr>"
                    % (esc(name), esc(s["area"]), medal_cell, level_cell,
                       s["n"], s["n"] * 100.0 / total_n,
                       s["danmu"], s["entry"], pay_cell, len(s["sessions"]), fmt_dt(s["last"])))
    out.append("<div class='panel'><div class='head'><div class='t'>全部频道</div>"
               "<div class='s'>%d 个 · 按互动次数排序</div></div><div class='scrollbox'><table>"
               "<thead><tr><th>频道</th><th>粉丝牌</th><th class='num2'>粉丝牌等级</th>"
               "<th class='num2'>互动</th><th class='num2'>占比</th>"
               "<th class='num2'>弹幕</th><th class='num2'>入场</th><th class='num2'>打米</th>"
               "<th class='num2'>场次</th><th>最近访问</th></tr></thead><tbody>%s</tbody></table></div></div>"
               % (len(ranked), "".join(rows)))
    out.append("<div class='note'>粉丝牌信息来自弹幕、礼物、SC、上舰和互动事件；"
               "旧数据只保存了等级、没有保存名称时显示“未记录”。</div>")
    return "".join(out)


DWELL_GAP_MS = 6 * 60 * 60 * 1000
DWELL_ENTRY_GAP_MS = 3 * 60 * 60 * 1000
DWELL_LONG_MS = 6 * 60 * 60 * 1000
DWELL_CONTINUE_GAP_MS = 30 * 60 * 1000
DWELL_MAX_SEGMENT_MS = 18 * 60 * 60 * 1000
INTERACTION_CMDS = ("DANMU_MSG", "SEND_GIFT", "SUPER_CHAT_MESSAGE",
                    "GUARD_BUY", "LIKE_INFO_V3_CLICK")


def _is_entry(e):
    cmd = e.get("cmd") or ""
    if cmd.startswith("ENTRY_EFFECT"):
        return True
    return cmd.startswith("INTERACT_WORD") and e.get("interact") == "进入直播间"


def _is_interaction(e):
    cmd = e.get("cmd") or ""
    return cmd.startswith("INTERACT_WORD") or cmd in INTERACTION_CMDS


def _is_real_interaction(e):
    return _is_interaction(e) and not _is_entry(e)


def _split_dwell(seg):
    """超过 6 小时后，长间隔后的进入事件切段，真实互动则接续片段。"""
    evs = seg.get("ev") or []
    if seg["end"] - seg["start"] <= DWELL_LONG_MS:
        return [seg]
    split_at = None
    for i in range(1, len(evs)):
        if (evs[i]["ts"] - evs[i - 1]["ts"] > DWELL_CONTINUE_GAP_MS
                and _is_entry(evs[i])):
            split_at = i
            break
    if split_at is None:
        return [seg]
    out = []
    for part in (evs[:split_at], evs[split_at:]):
        real = [e for e in part if _is_real_interaction(e)]
        if not real:
            continue
        if part[-1]["ts"] <= part[0]["ts"]:
            continue
        out.extend(_split_dwell({
            "room": seg["room"], "start": part[0]["ts"],
            "end": part[-1]["ts"], "n": len(real), "ev": part,
        }))
    return out


def dwell_panel(events, room_names):
    """观看时长（估算）：首次进入/互动 → 最后一次在场记录，6 小时无记录切段。"""
    groups = defaultdict(list)
    for e in events:
        if e.get("src") == "manual" or not e.get("ts") or not e.get("room"):
            continue
        if _is_entry(e) or _is_interaction(e):
            groups[e.get("room")].append(e)
    segs, entries_n = [], 0
    for rid, items in groups.items():
        items.sort(key=lambda x: x.get("ts") or 0)
        clusters, cur = [], [items[0]]
        for e in items[1:]:
            if (e.get("ts") or 0) - (cur[-1].get("ts") or 0) > DWELL_GAP_MS:
                clusters.append(cur)
                cur = [e]
            else:
                cur.append(e)
        clusters.append(cur)
        for cl in clusters:
            splits, last_entry = [0], None
            for i, e in enumerate(cl):
                if _is_entry(e):
                    if last_entry is not None and e["ts"] - last_entry > DWELL_ENTRY_GAP_MS:
                        splits.append(i)
                    last_entry = e["ts"]
            splits.append(len(cl))
            for a, b in zip(splits, splits[1:]):
                sub = cl[a:b]
                real = [e for e in sub if _is_real_interaction(e)]
                if not real:
                    continue
                entries_n += sum(1 for e in sub if _is_entry(e))
                st, end = sub[0]["ts"], sub[-1]["ts"]
                if end > st:
                    segs.append({"room": rid, "start": st, "end": end,
                                 "n": len(real), "ev": sub})
    segs = [x for s in segs for x in _split_dwell(s)]
    segs = [s for s in segs
            if s["end"] - s["start"] <= DWELL_MAX_SEGMENT_MS]
    entries_n = sum(1 for s in segs for e in s["ev"] if _is_entry(e))
    total_ms = sum(s["end"] - s["start"] for s in segs)
    total_h = total_ms / 3600000.0
    months = defaultdict(float)
    rooms = defaultdict(float)
    for s in segs:
        h = (s["end"] - s["start"]) / 3600000.0
        months[fmt_dt(s["start"], "%Y-%m")] += h
        rooms[s["room"]] += h
    rank = sorted(rooms.items(), key=lambda kv: -kv[1])
    longest = max(segs, key=lambda s: s["end"] - s["start"], default=None)
    top_room = room_names.get(rank[0][0], "—") if rank else "—"
    avg_min = total_ms / max(1, len(segs)) / 60000.0
    lm = (room_names.get(longest["room"], "—") + " · " + fmt_dt(longest["start"], "%m-%d")) if longest else "—"
    cards = [
        card("估算总时长", "%.1f" % total_h, "小时", "首次在场 → 最后在场", "money"),
        card("活跃段数", len(segs), "段", "6h/3h 切段 · 真实互动跨空档接续 · 18h上限", "plain"),
        card("平均每段", "%.1f" % avg_min, "分钟", "段首→段末", "plain"),
        card("最长单段", "%.1f" % ((longest["end"] - longest["start"]) / 3600000.0) if longest else "0", "小时", lm, "plain"),
        card("进入记录", entries_n, "次", "无进入时以首条互动为起点", "plain"),
        card("覆盖频道", len(rooms), "个", "常停留频道 " + top_room, "plain"),
    ]
    out = [
        "<div class='panel'><div class='head'><div><div class='kicker'>WATCH TIME</div>"
        "<div class='t'>观看时长（估算）</div></div><div class='s'>首次在场 → 最后在场 · 自然空档切段</div></div>",
        "<div class='grid'>%s</div>" % "".join(cards),
        "<div class='warnnote'>⚠ 时间仅为估算：把同房间连续的「进入/弹幕/礼物/SC/上舰/点赞」"
        "视为在场记录，段首=第一条在场记录，段末=最后一条在场记录；连续 6 小时没有任何"
        "在场记录才切段；大段内两次进入间隔超过 3 小时也另起一段。"
        "超过 6 小时后，相邻真实互动间隔不超过 30 分钟则继续计时；"
        "间隔超过 30 分钟时，如果下一条是进入直播间则切段；"
        "如果下一条是弹幕、礼物、SC 或上舰等真实互动，则跨过空档接续同一片段。"
        "最终任何超过 18 小时的片段都不计入。段内至少有一条真实互动。"
        "没有进入记录时以首条互动为起点。"
        "静默观看、完全无记录的时段不计入，不等同于 B 站精确观看时长。</div></div>",
    ]
    if not segs:
        out.append("<div class='panel'><div class='empty'>暂无足够的本人进入/互动记录</div></div>")
        return "".join(out)
    out.append("<div class='two'>")
    out.append("<div class='panel'><div class='head'><div class='t'>按月估算时长</div>"
               "<div class='s'>小时</div></div><div class='compose'>%s</div></div>"
               % compose([(m, h, COLORS["弹幕"]) for m, h in sorted(months.items())], "%.1f h"))
    out.append("<div class='panel'><div class='head'><div class='t'>频道 TOP 10</div>"
               "<div class='s'>按估算时长</div></div><div class='compose'>%s</div></div>"
               % compose([(room_names.get(rid, "房间 %s" % rid), h, COLORS["醒目留言"])
                          for rid, h in rank[:10]], "%.1f h"))
    out.append("</div>")
    rows = []
    for s in sorted(segs, key=lambda x: -(x["end"] - x["start"]))[:12]:
        mins = (s["end"] - s["start"]) / 60000.0
        dur = ("%.1f 小时" % (mins / 60.0)) if mins >= 60 else ("%d 分钟" % round(mins))
        rows.append("<tr><td>%s</td><td>%s</td><td class='num2'>%s</td>"
                    "<td class='num2'>%d</td><td>%s</td></tr>"
                    % (fmt_dt(s["start"], "%Y-%m-%d %H:%M"),
                       esc(room_names.get(s["room"], "房间 %s" % s["room"])),
                       dur, s["n"], fmt_dt(s["end"], "%H:%M:%S")))
    out.append("<div class='panel'><div class='head'><div class='t'>最长的在场段</div>"
               "<div class='s'>Top 12 · 首条→最后一条在场记录</div></div><div class='scrollbox'><table>"
               "<thead><tr><th>开始时间</th><th>频道</th><th class='num2'>估算时长</th>"
               "<th class='num2'>互动数</th><th>最后在场</th></tr></thead><tbody>%s</tbody></table>"
               "</div></div>" % "".join(rows))
    return "".join(out)


def period_catalog(events, now=None):
    now = now or datetime.now(TZ)
    event_dates = [
        datetime.fromtimestamp((e.get("ts") or 0) / 1000.0, TZ)
        for e in events if e.get("ts")
    ]
    earliest = min(event_dates) if event_dates else now
    months = []
    cursor = earliest.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor <= end_month:
        months.append(cursor.strftime("%Y-%m"))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    years = [str(y) for y in range(earliest.year, now.year + 1)]
    return list(reversed(months)), list(reversed(years))


def _period_range(period, value, now):
    if period == "month":
        key = value or now.strftime("%Y-%m")
        year, month = [int(x) for x in key.split("-", 1)]
        start = datetime(year, month, 1, tzinfo=TZ)
        end = (start.replace(year=year + 1, month=1)
               if month == 12 else start.replace(month=month + 1))
        label = "%d年%02d月" % (year, month)
    elif period == "year":
        year = int(value or now.year)
        start = datetime(year, 1, 1, tzinfo=TZ)
        end = datetime(year + 1, 1, 1, tzinfo=TZ)
        label = "%d年" % year
    else:
        return None, None, "生涯"
    effective_end = min(now, end - timedelta(microseconds=1))
    return start, effective_end, label


def render_period(events, account, room_names, my_uid, period="career",
                  period_value=None, month_options=None, year_options=None,
                  medal_names=None):
    now = datetime.now(TZ)
    range_start, range_end, period_label = _period_range(period, period_value, now)
    if range_start is not None:
        cutoff = int(range_start.timestamp() * 1000)
        cutoff_end = int((range_end + timedelta(microseconds=1)).timestamp() * 1000)
        events = [e for e in events
                  if cutoff <= (e.get("ts") or 0) < cutoff_end]
    ts = [e["ts"] for e in events if e.get("ts")]
    if ts:
        first = datetime.fromtimestamp(min(ts) / 1000.0, TZ)
    else:
        first = range_start or now
    start_date = range_start or first
    end_date = range_end or now
    span = max(1, (end_date.date() - start_date.date()).days + 1)
    def cnt(*cmds):
        return sum(1 for e in events if e.get("cmd") in cmds)
    danmu = cnt("DANMU_MSG")
    gift = cnt("SEND_GIFT")
    sc = cnt("SUPER_CHAT_MESSAGE")
    guard = cnt("GUARD_BUY")
    enter = sum(1 for e in events if _is_entry(e))
    like = cnt("LIKE_INFO_V3_CLICK")
    money_total = sum(e.get("_yuan", 0.0) for e in events)
    interact = sum(1 for e in events if _is_real_interaction(e))
    room_cmds = ("DANMU_MSG", "SEND_GIFT", "SUPER_CHAT_MESSAGE", "GUARD_BUY", "LIKE_INFO_V3_CLICK")
    rooms = Counter(e.get("room") for e in events
                    if e.get("room") and ((e.get("cmd") or "").startswith(("ENTRY_EFFECT", "INTERACT_WORD"))
                                          or e.get("cmd") in room_cmds))
    days = {datetime.fromtimestamp((e.get("ts") or 0) / 1000.0, TZ).date() for e in events if e.get("ts")}
    # 有直播场次 ID 的按场次统计（与参照站口径一致），本地采集的按「房间×日期」估算
    sessions = set()
    for e in events:
        if not e.get("ts") or not e.get("room"):
            continue
        if not _is_real_interaction(e):
            continue
        d = datetime.fromtimestamp(e["ts"] / 1000.0, TZ).date()
        sessions.add(("live", e["live_id"]) if e.get("live_id") else ("day", e["room"], d))
    streak = 0
    if days:
        run = 0
        for i in range(span + 1):
            d = end_date.date() - timedelta(days=i)
            if d in days:
                run += 1
                streak = max(streak, run)
            else:
                run = 0
    wk = Counter()
    for e in events:
        if _is_entry(e):
            continue
        dt = datetime.fromtimestamp((e.get("ts") or 0) / 1000.0, TZ)
        wk[(dt.weekday(), dt.hour)] += 1
    best_time = "—"
    if wk:
        (w, h), _ = wk.most_common(1)[0]
        best_time = "%s %02d:00" % (WEEK[w], h)
    top_room = room_names.get(rooms.most_common(1)[0][0], "—") if rooms else "—"
    most_room = rooms.most_common(1)[0][1] if rooms else 0
    spend_by_room = Counter()
    for e in events:
        if e.get("_yuan"):
            spend_by_room[e.get("room")] += e["_yuan"]
    if spend_by_room:
        top_pay_room = room_names.get(spend_by_room.most_common(1)[0][0], "—")
        top_pay = spend_by_room.most_common(1)[0][1]
    else:
        top_pay_room, top_pay = "—", 0.0
    name = account.get("uname") or "未知用户"
    w = account.get("wealth_info") or {}
    xs, dm, ot, pd = daily_series(events, start_date.date(), end_date.date())
    interactive_trend = period == "career"
    trend = svg_trend(xs, dm, ot, pd, interactive=interactive_trend)
    trend_script = (career_trend_script(xs, dm, ot, pd)
                    if interactive_trend else [])
    trend_actions = ""
    trend_stage_attr = ""
    if interactive_trend:
        trend_stage_attr = " id='careerTrendStage'"
        trend_actions = (
            "<div class='trend-actions' id='careerTrendActions'>"
            "<span class='trend-range' id='careerTrendRange'></span>"
            "<button class='trend-reset' id='careerTrendReset' type='button'>"
            "恢复完整范围</button></div>"
        )
    trend_panel = (
        "<div class='panel trend-panel'><div class='head'><div>"
        "<div class='kicker'>ACTIVITY</div><div class='t'>互动趋势</div></div>"
        "<div style='display:flex;gap:16px;align-items:center'><div class='legend'>"
        "<button data-series='s-danmu'><i style='background:#3b82f6'></i>弹幕</button>"
        "<button data-series='s-other'><i style='background:#2dd4bf'></i>其他互动</button>"
        "<button data-series='s-paid'><i style='background:#f59e0b'></i>付费金额</button>"
        "</div></div></div><div class='trend-stage'%s>%s</div>%s</div>"
        % (trend_stage_attr, trend, trend_actions)
    )
    period_links = "".join(
        "<a class='%s' href='%s'>%s</a>"
        % ("on" if key == period else "", href, label)
        for key, href, label in (
            ("month", "live_report-month.html", "月"),
            ("year", "live_report-year.html", "年"),
            ("career", "live_report.html", "生涯"),
        )
    )
    catalog_months, catalog_years = period_catalog(events, now)
    month_options = month_options or catalog_months
    year_options = year_options or catalog_years
    selector = ""
    if period == "month":
        selected = period_value or now.strftime("%Y-%m")
        options = "".join(
            "<option value='live_report-month-%s.html'%s>%s年%s月</option>"
            % (key, " selected" if key == selected else "",
               key[:4], key[5:])
            for key in month_options
        )
        selector = ("<span class='label'>选择月份</span>"
                    "<select onchange='if(this.value)location.href=this.value'>%s</select>"
                    % options)
    elif period == "year":
        selected = str(period_value or now.year)
        options = "".join(
            "<option value='live_report-year-%s.html'%s>%s年</option>"
            % (key, " selected" if key == selected else "", key)
            for key in year_options
        )
        selector = ("<span class='label'>选择年份</span>"
                    "<select onchange='if(this.value)location.href=this.value'>%s</select>"
                    % options)
    html = [
        "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>互动画像 · %s · %s</title><style>%s</style></head><body><div class='wrap'>"
        % (esc(name), esc(period_label), CSS),
        "<div class='hero'>%s<div>" % avatar_html(account),
        "<div class='kicker'>互动画像 · %s</div><h1>%s</h1>" % (
            esc(period_label), esc(name)),
        "<div class='pills'><span class='pill'>UID %s</span><span class='pill'>%s - %s</span>"
        "<span class='pill'>%d 天</span></div>" % (esc(my_uid), start_date.strftime("%Y.%m.%d"),
                                                   end_date.strftime("%Y.%m.%d"), span),
        "<div class='note'>只统计本机采集到的记录；进房不等于看完整场。</div>",
        "</div></div>",
        "<div class='periods'><span class='label'>时间分类</span>%s%s</div>"
        % (period_links, selector),
        "<h2>总览</h2><div class='grid five'>",
        card("弹幕", danmu, "条", "活跃日均 %.1f" % (danmu / max(1, len(days)))),
        card("互动场次", len(sessions), "场", "场均 %.1f 次" % (interact / max(1, len(sessions)))),
        card("活跃天数", len(days), "天", "占所选范围 %.0f%%" % (len(days) * 100.0 / max(1, span))),
        card("打米", money(money_total), "元", "礼物/上舰/醒目留言", "money"),
        card("互动次数", interact, "次", "不含入场"),
        "</div><div class='grid five' style='margin-top:14px'>",
        card("最长连续活跃", streak, "天", "按有互动的日期连续计算", "plain"),
        card("常互动时段", best_time, "", "按互动次数统计", "plain"),
        card("常看频道", top_room, "", "%d 次互动" % most_room, "plain"),
        card("弹幕占比", "%.0f%%" % (danmu * 100.0 / max(1, interact)), "", "弹幕在互动中的占比", "plain"),
        card("消费最多频道", top_pay_room, "", "¥%.2f" % top_pay, "plain"),
        "</div>",
        "<div class='tabs' id='rtabs'>"
        "<button data-tab='activity' class='on'>活动节奏</button>"
        "<button data-tab='money'>打米记录</button>"
        "<button data-tab='channels'>频道偏好</button></div>",
        "<div class='tabpane' id='pane-activity'>",
        trend_panel,
        "<div class='two'>",
        "<div class='panel'><div class='head'><div class='t'>活跃日历</div>"
        "<div class='s'>%d 个活跃日</div></div>%s</div>" % (len(days), svg_calendar(events)),
        "<div class='panel'><div class='head'><div class='t'>星期与时段</div>"
        "<div class='s'>按互动次数</div></div>%s</div>" % svg_week_hours(events),
        "</div>",
        "<div class='panel'><div class='head'><div class='t'>留痕构成</div>"
        "<div class='s'>%d 次记录</div></div><div class='compose'>%s</div></div>"
        % (len(events), compose([("弹幕", danmu, COLORS["弹幕"]), ("礼物", gift, COLORS["礼物"]),
                                 ("醒目留言", sc, COLORS["醒目留言"]), ("上舰", guard, COLORS["上舰"]),
                                 ("入场记录", enter, COLORS["入场记录"])])),
        "</div>",
        "<div class='tabpane' id='pane-money' style='display:none'>%s</div>"
        % money_panel(events, room_names),
        "<div class='tabpane' id='pane-channels' style='display:none'>%s</div>"
        % channel_panel(events, room_names, medal_names),
        "<h2>资产</h2><div class='grid'>",
        card("B币", account.get("billCoin", "-"), "", "账号余额", "plain"),
        card("直播等级", account.get("user_level", "-"), "", "亲密度 %s" % account.get("user_intimacy", "-"), "plain"),
        card("财富等级", w.get("level", "-"), "", "累计 %s 分" % w.get("level_total_score", "-"), "plain"),
        card("采集记录", len(events), "条", "本机采集所得", "plain"),
        "</div>",
        "<div class='note' style='margin-top:22px'>生成于 %s ｜ 数据来自本机采集，仅含开始采集之后的行为；"
        "B 站不提供历史明细接口，历史数据需另行导出。</div>" % now.strftime("%Y-%m-%d %H:%M:%S"),
        "<div id='tip'></div>",
        "<script>",
        *trend_script,
        "var btns=document.querySelectorAll('#rtabs button');",
        "function showPane(k){btns.forEach(function(b){b.classList.toggle('on',b.dataset.tab===k)});",
        "['activity','money','channels'].forEach(function(x){",
        "document.getElementById('pane-'+x).style.display=(x===k)?'':'none'});}",
        "btns.forEach(function(b){b.addEventListener('click',function(){showPane(b.dataset.tab)})});",
        "var h=(location.hash||'').replace('#','');",
        "if(['activity','money','channels'].indexOf(h)>=0){showPane(h)}",
        "var tip=document.getElementById('tip');",
        "function tipEl(e){return e.target.closest?e.target.closest('[data-tip]'):null;}",
        "document.addEventListener('mouseover',function(e){var t=tipEl(e);",
        "if(t){tip.textContent=t.getAttribute('data-tip');tip.style.display='block';}});",
        "document.addEventListener('mousemove',function(e){if(tip.style.display!=='block')return;",
        "var x=e.clientX+14,y=e.clientY+14;",
        "if(x+280>window.innerWidth)x=e.clientX-290;if(y+70>window.innerHeight)y=e.clientY-80;",
        "tip.style.left=x+'px';tip.style.top=y+'px';});",
        "document.addEventListener('mouseout',function(e){if(tipEl(e)){tip.style.display='none';}});",
        "function applyTrendSeries(){document.querySelectorAll('[data-series]').forEach(function(b){",
        "document.querySelectorAll('.'+b.dataset.series).forEach(function(el){",
        "el.style.display=b.classList.contains('off')?'none':'';});});}",
        "document.querySelectorAll('[data-series]').forEach(function(b){",
        "b.addEventListener('click',function(){b.classList.toggle('off');applyTrendSeries();});});",
        "var careerTrendReset=document.getElementById('careerTrendReset');",
        "if(careerTrendReset){careerTrendReset.addEventListener('click',function(){renderCareerTrend(0);});}",
        "document.addEventListener('click',function(e){",
        "if(!e.target.classList||!e.target.classList.contains('x-axis-hit'))return;",
        "var svg=e.target.closest('svg.career-trend');if(!svg)return;",
        "var rect=svg.getBoundingClientRect(),x=(e.clientX-rect.left)*1080/rect.width;",
        "var n=parseInt(svg.getAttribute('data-days'),10)||0,"
        "offset=parseInt(svg.getAttribute('data-start'),10)||0;",
        "var local=Math.max(0,Math.min(n-1,Math.floor((x-44)/(984/Math.max(1,n)))));",
        "renderCareerTrend(offset+local);});",
        "</script>",
        "</div></body></html>",
    ]
    return "".join(html)


def render_pages(events, account, room_names, my_uid, medal_names=None):
    """生成可选月份、年份和生涯报告页面。"""
    now = datetime.now(TZ)
    months, years = period_catalog(events, now)
    pages = {
        "live_report.html": render_period(
            events, account, room_names, my_uid, "career",
            medal_names=medal_names),
    }
    for key in months:
        pages["live_report-month-%s.html" % key] = render_period(
            events, account, room_names, my_uid, "month", key, months, years,
            medal_names)
    for key in years:
        pages["live_report-year-%s.html" % key] = render_period(
            events, account, room_names, my_uid, "year", key, months, years,
            medal_names)
    pages["live_report-month.html"] = pages[
        "live_report-month-%s.html" % now.strftime("%Y-%m")]
    pages["live_report-year.html"] = pages[
        "live_report-year-%04d.html" % now.year]
    return pages


def render(events, account, room_names, my_uid):
    """兼容旧调用：默认返回全生涯报告。"""
    return render_pages(events, account, room_names, my_uid)["live_report.html"]

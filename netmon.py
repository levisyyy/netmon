# -*- coding: utf-8 -*-
"""
常驻网络监测脚本（零依赖，只用 Windows 自带命令 + Python 标准库）

用法:
    python netmon.py                     # 一直跑，Ctrl+C 停止
    python netmon.py --minutes 30        # 跑 30 分钟后自动停
    python netmon.py --gateway 192.168.137.1
    python netmon.py --interval 1 --http-every 15

产出:
    runs/ping_YYYYmmdd_HHMMSS.csv   每次运行的原始采样（可长期留存对比）
    netmon_live.html                实时看板，浏览器打开后每 5 秒自动刷新
"""

import argparse
import csv
import glob
import os
import re
import subprocess
import sys
import time
import datetime
import json
import urllib.request

PING = r"C:\Windows\System32\ping.exe"
IPCONFIG = r"C:\Windows\System32\ipconfig.exe"
NETSH = r"C:\Windows\System32\netsh.exe"

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BASE, "runs")
DASH = os.path.join(BASE, "netmon_live.html")


def dec(raw):
    """netsh 输出常为 UTF-8，ping 输出常为 GBK，逐个尝试避免中文关键词匹配失败。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("gbk", "ignore")


def sh(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, dec(p.stdout) + dec(p.stderr)
    except Exception as exc:
        return -1, str(exc)


def append_row(path, row, retries=12, gap=0.5):
    """安全追加一行。天擎/DLP 等安全软件会短暂锁定文件，这里重试而不是让进程崩掉。"""
    last = None
    for i in range(retries):
        try:
            with open(path, "a", newline="", encoding="utf-8-sig") as fh:
                csv.writer(fh).writerow(row)
            return True
        except PermissionError as exc:
            last = exc
            time.sleep(gap)
        except OSError as exc:
            last = exc
            time.sleep(gap)
    print("[warn] 写入被占用，样本丢弃：{}".format(last))
    return False


def detect_gateway():
    _, out = sh([IPCONFIG])
    gw = None
    for line in out.splitlines():
        if "默认网关" in line or "Default Gateway" in line:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                gw = m.group(1)
    return gw


def ping_once(host):
    rc, out = sh([PING, "-n", "1", "-w", "1000", host], timeout=8)
    low = out.lower()
    if "传输失败" in out or "general failure" in low or "transmit failed" in low:
        return False, 0, "tx_fail"
    if "请求超时" in out or "timed out" in low or "无法访问" in out:
        return False, 0, "timeout"
    m = re.search(r"=(\d+)ms", out)
    if m:
        return True, int(m.group(1)), "ok"
    m = re.search(r"<(\d+)ms", out)
    if m:
        return True, max(1, int(m.group(1)) - 1), "ok"
    if rc == 0:
        return True, 0, "ok"
    return False, 0, "fail"


def wifi_info():
    _, out = sh([NETSH, "wlan", "show", "interfaces"])
    fields = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()

    def pick(*keys):
        for k, v in fields.items():
            for key in keys:
                if key in k:
                    return v
        return ""

    return {
        "ssid": pick("SSID"),
        "bssid": pick("BSSID"),
        "band": pick("波段"),
        "channel": pick("通道", "信道"),
        "rx": pick("接收速率"),
        "rssi": pick("Rssi"),
    }


def http_probe(url):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 netmon"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read(2048)
            return True, resp.status, int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as exc:
        return True, exc.code, int((time.time() - t0) * 1000)
    except Exception:
        return False, 0, int((time.time() - t0) * 1000)


def speed_test(url, seconds=8.0):
    """限时下载测带宽。直连不走代理，返回 (字节数, 有效秒数, Mbps)。"""
    t0 = time.time()
    total = 0
    last = None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 netmon"})
        with opener.open(req, timeout=20) as resp:
            while True:
                chunk = resp.read(131072)
                if not chunk:
                    break
                total += len(chunk)
                last = time.time()
                if time.time() - t0 >= seconds:
                    break
        dt = (last - t0) if last else (time.time() - t0)
        if dt <= 0:
            dt = 0.1
        return total, dt, total * 8 / 1e6 / dt
    except Exception:
        return total, max(0.1, time.time() - t0), 0.0


def load_speed_history(limit=200):
    """读取历史所有 speed_*.csv，让重启后的走势图保留连续性。"""
    rows = []
    for path in glob.glob(os.path.join(RUNS, "speed_*.csv")):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    try:
                        rows.append((r["time"], float(r["mbps"]), float(r["mb_downloaded"])))
                    except Exception:
                        continue
        except Exception:
            continue
    rows.sort()
    return [{"time": t[5:10] + " " + t[11:16], "ts": t, "mbps": round(m, 1), "mb": round(mb, 1)}
            for t, m, mb in rows[-limit:]]


def pct(values, q):
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))
    return ordered[idx]


def bucket_series(samples, span=43200, bucket=30):
    """长窗口（12 小时）聚合：每 bucket 秒合成一个点，ms=均值，pk=峰值，全丢则 None。"""
    s = samples[-span:]
    out = []
    for i in range(0, len(s), bucket):
        chunk = s[i:i + bucket]
        ok_ms = [c["ms"] for c in chunk if c["ok"] and c["ms"] is not None]
        ts = chunk[0].get("ts", "")
        label = (ts[5:10] + " " + ts[11:16]) if ts else chunk[0].get("t", "")
        out.append({
            "t": label,
            "ms": round(sum(ok_ms) / len(ok_ms), 1) if ok_ms else None,
            "pk": max(ok_ms) if ok_ms else None,
        })
    return out


_hist_cache = {"key": None, "data": []}


def load_ping_history_sources(exclude=None, max_points=720):
    """扫描历史 ping_*.csv，各聚合为最多 max_points 个点的走势，供看板下拉选择。"""
    files = []
    for path in glob.glob(os.path.join(RUNS, "ping_*.csv")):
        if exclude and os.path.basename(path) == exclude:
            continue
        try:
            files.append((path, os.path.getmtime(path)))
        except OSError:
            continue
    files.sort(key=lambda x: x[1], reverse=True)
    key = tuple((os.path.basename(p), round(m, 1)) for p, m in files)
    if _hist_cache["key"] == key:
        return _hist_cache["data"]
    out = []
    for path, _ in files:
        rows = []
        try:
            with open(path, encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    rows.append(r)
        except Exception:
            continue
        if len(rows) < 30:
            continue
        t0 = rows[0]["time"]
        try:
            mins = round((datetime.datetime.fromisoformat(rows[-1]["time"]) -
                          datetime.datetime.fromisoformat(t0)).total_seconds() / 60)
            dur = "{}分".format(mins) if mins < 120 else "{:.1f}小时".format(mins / 60.0)
        except Exception:
            dur = ""
        label = t0[5:10] + " " + t0[11:16] + (" · " + dur if dur else "")
        step = max(1, len(rows) // max_points)
        pts = []
        for i in range(0, len(rows), step):
            chunk = rows[i:i + step]
            ok_ms = []
            for r in chunk:
                if r["ok"] == "1" and r["ms"] not in ("", "None"):
                    try:
                        ok_ms.append(float(r["ms"]))
                    except ValueError:
                        pass
            tc = chunk[0]["time"]
            pts.append({
                "t": tc[5:10] + " " + tc[11:16],
                "ms": round(sum(ok_ms) / len(ok_ms), 1) if ok_ms else None,
                "pk": max(ok_ms) if ok_ms else None,
            })
        out.append({"name": os.path.basename(path), "label": label, "pts": pts})
    _hist_cache["key"] = key
    _hist_cache["data"] = out
    return out


def build_dashboard(state, window=3600, stat=300):
    samples = state["samples"][-window:]
    recent = state["samples"][-stat:]
    oks = [s["ms"] for s in recent if s["ok"]]
    loss = 0 if not recent else round(100.0 * (len(recent) - len(oks)) / len(recent), 2)
    avg = round(sum(oks) / len(oks), 1) if oks else 0
    mx = max(oks) if oks else 0
    p95 = pct(oks, 0.95)
    spikes = sum(1 for v in oks if v > 150)

    if state["in_outage"]:
        verdict, color = "当前断链中", "#E24B4A"
    elif loss > 2 or spikes > 3:
        verdict, color = "链路不稳定", "#EF9F27"
    elif mx > 150:
        verdict, color = "有尖峰，可开会但会顿", "#EF9F27"
    else:
        verdict, color = "链路正常", "#1D9E75"

    series = [{"i": s["i"], "ms": (s["ms"] if s["ok"] else None), "ok": s["ok"],
               "t": s.get("t", "")} for s in samples]

    def _sec(ts):
        try:
            h, m, sec = ts.split(":")
            return int(h) * 3600 + int(m) * 60 + int(sec)
        except Exception:
            return None

    def _span_txt(lst):
        if len(lst) < 2:
            return "0 秒"
        a, b = _sec(lst[0].get("t", "")), _sec(lst[-1].get("t", ""))
        if a is None or b is None:
            return ""
        sp = (b - a) % 86400
        if sp < 60:
            return "{} 秒".format(sp)
        return "{} 分 {} 秒".format(sp // 60, sp % 60)

    win_txt = "最近 {} 包 · 约 {}".format(len(samples), _span_txt(samples))
    stat_txt = "最近 {} 包 · 约 {}".format(len(recent), _span_txt(recent))
    cmax = max([s["ms"] for s in samples if s["ok"]] or [0])
    wifi = state["wifi"]
    wifi_hint = ""
    if not wifi.get("ssid"):
        wifi_hint = ('<div class="foot" style="margin-top:8px;color:#EF9F27">读不到 Wi-Fi 信息：'
                     'Windows 位置权限未开启。设置 &gt; 隐私和安全性 &gt; 位置 → 开启「定位服务」和'
                     '「让桌面应用访问你的位置」，或用管理员身份运行脚本。</div>')
    started = state["started"].strftime("%H:%M:%S")
    now = datetime.datetime.now().strftime("%H:%M:%S")

    events = state["events"][-12:][::-1]
    event_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            e["time"], e["type"], e["detail"], e.get("dur", "-")
        )
        for e in events
    ) or '<tr><td colspan="4" style="color:var(--muted)">暂无事件</td></tr>'

    def _h(title, lines):
        return {"t": title, "b": "".join("<p>{}</p>".format(x) for x in lines)}

    help_json = json.dumps({
        "loss": _h("丢包率", [
            "发出去的探测包里，有多少个没有收到回复。",
            "0% 最好；偶尔 1~2 个不用在意。持续超过 2% 时，视频会议会明显卡顿、声音掉字，网页偶尔打不开。",
        ]),
        "avg": _h("平均延迟", [
            "数据包从你的电脑出发，到热点（或路由器）再回来的往返时间，越小越流畅。",
            "局域网内正常应小于 20ms；50~100ms 会有轻微顿感；超过 150ms 开会能明显感觉卡。",
        ]),
        "mx": _h("峰值延迟", [
            "统计窗口里最慢的那一次往返时间。",
            "偶发一两个尖峰影响不大；峰值频繁出现、且和平均值差很多，说明链路拥堵或被大流量占用。",
        ]),
        "spikes": _h("大于 150ms 次数", [
            "往返时间超过 150ms 的探测包个数。150ms 是人耳能感知到会议卡顿的大致门槛。",
            "次数越多，开会越容易一顿一顿；曲线图上的红点就是这些包。",
        ]),
        "speed": _h("网速（MB/s）", [
            "每 5 分钟做一次限时下载测出的速度，单位 MB/s（每秒多少兆字节）。",
            "换算：带宽标称值 ÷ 8 = MB/s。例：100 Mbps 宽带 ≈ 12.5 MB/s。测速本身会占用约 10 秒带宽，曲线会留一个小缺口，属正常。",
        ]),
        "lchart": _h("延迟走势", [
            "每秒 ping 一次网关，把往返时间画成曲线。",
            "灰色缺口＝该秒没有收到回复（丢包/断链）；红点＝超过 150ms。",
            "右上按钮切换显示范围：5 分钟 / 30 分钟 / 1 小时 / 12 小时（12 小时按每 30 秒聚合，画平均值，段内有尖峰仍标红）。图表下方一行是当前窗口的统计。",
            "「数据源」下拉框：默认「当前（实时）」；选中某个历史记录可回看那次的走势（按时间从新到旧排列，选中的选择会被记住）。点任意窗口按钮即回到实时。",
        ]),
        "schart": _h("网速走势", [
            "所有历史测速记录连成的曲线，单位 MB/s；左下和右下是首末时间，跨天处有竖虚线和日期标注。",
            "表格里「换算」列是同一速度的 Mbps 值（MB/s × 8 = Mbps），方便和宽带标称带宽对照。",
        ]),
        "events": _h("事件记录", [
            "监测脚本的自动日志，共四类：",
            "断链开始 / 断链恢复——连续 2 次 ping 失败记为断链，恢复时「持续」一列显示断了多少秒；",
            "HTTP 探测——每 15 秒访问一次 qq.com，验证不只是通、还能真正打开网页；",
            "网速测试——每 5 分钟测速记录一条；",
            "信道变化——Wi-Fi 换了信道，通常伴随周围干扰变强，网速突变时先看这里。",
        ]),
        "link": _h("链路信息", [
            "SSID：当前连接的 Wi-Fi 名称。",
            "频道：2.4G 的 1/6/11 或 5G 频道号，同信道 AP 越多越拥挤。",
            "接收速率：无线网卡当前协商速率（理论值，实际受距离和干扰影响）。",
            "信号（RSSI）：越接近 0 越强。-30 极强、-50 很好、-70 较弱、-80 以下基本不可用。",
        ]),
    }, ensure_ascii=False)

    payload = json.dumps({"series": series, "max": max(200, cmax * 1.1),
                          "long": bucket_series(state["samples"]),
                          "hist": load_ping_history_sources(
                              exclude=os.path.basename(state["csv"]))},
                         ensure_ascii=False)
    payload2 = json.dumps(state["speeds"][-200:], ensure_ascii=False)
    sp = state["speeds"][-12:][::-1]
    speed_rows = "".join(
        "<tr><td>{}</td><td>{:.1f} MB/s</td><td>{:.1f} Mbps</td><td>{} MB</td></tr>".format(
            s["time"], s["mbps"] / 8.0, s["mbps"], s["mb"]
        )
        for s in sp
    ) or '<tr><td colspan="4" style="color:var(--muted)">暂无测速记录</td></tr>'

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>网络监测看板</title>
<style>
:root{{--bg:#ffffff;--fg:#1f1f1f;--muted:#6b6b6b;--line:#e4e2da;--card:#f7f6f2;--grid:rgba(136,135,128,.18)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1b1b1b;--fg:#f0efea;--muted:#a8a6a0;--line:#33322e;--card:#242320;--grid:rgba(200,200,190,.16)}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--fg);font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:13px;line-height:1.6}}
h1{{font-size:15px;font-weight:500;margin:0 0 4px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:18px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:999px;color:#fff;font-size:12px;margin-left:8px;vertical-align:middle}}
.grid4{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}}
.card{{background:var(--card);border-radius:8px;padding:12px 14px}}
.card .k{{color:var(--muted);font-size:12px}}
.card .v{{font-size:22px;font-weight:500;margin-top:2px}}
.card .u{{font-size:12px;color:var(--muted);margin-left:2px}}
canvas{{width:100%;height:240px;display:block}}
.box{{border:0.5px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:18px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:0.5px solid var(--line)}}
th{{color:var(--muted);font-weight:400}}
.foot{{color:var(--muted);font-size:12px}}
.wbtn{{background:transparent;border:0.5px solid var(--line);color:var(--muted);font-size:12px;padding:2px 8px;border-radius:6px;cursor:pointer;font-family:inherit;margin-left:4px}}
.wbtn.on{{border-color:#378ADD;color:#378ADD}}
.hlp{{cursor:pointer;color:var(--muted);border:0.5px solid var(--line);border-radius:999px;font-size:11px;padding:0 7px;margin-left:6px;display:inline-block;line-height:16px;font-family:inherit;background:transparent}}
.hlp:hover{{color:#378ADD;border-color:#378ADD}}
#mask{{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;z-index:9}}
#mask.on{{display:flex}}
#dlg{{background:var(--bg);color:var(--fg);border:0.5px solid var(--line);border-radius:12px;max-width:480px;width:90%;padding:18px 20px}}
#dlg h3{{font-size:14px;font-weight:500;margin:0 0 8px}}
#dlg p{{margin:0 0 6px}}
</style></head>
<body>
<h1>网络监测 <span class="badge" style="background:{color}">{verdict}</span></h1>
<div class="sub">开始于 {started} · 最近刷新 {now} · 统计窗口 {stat_txt} · 网关 {gw}</div>

<div class="grid4">
  <div class="card"><div class="k">丢包率<span class="hlp" onclick="showHelp('loss')">?</span></div><div class="v">{loss}<span class="u">%</span></div></div>
  <div class="card"><div class="k">平均延迟<span class="hlp" onclick="showHelp('avg')">?</span></div><div class="v">{avg}<span class="u">ms</span></div></div>
  <div class="card"><div class="k">峰值延迟<span class="hlp" onclick="showHelp('mx')">?</span></div><div class="v">{mx}<span class="u">ms</span></div></div>
  <div class="card"><div class="k">大于 150ms 次数<span class="hlp" onclick="showHelp('spikes')">?</span></div><div class="v">{spikes}<span class="u">次</span></div></div>
  <div class="card"><div class="k">网速（{speed_time}）<span class="hlp" onclick="showHelp('speed')">?</span></div><div class="v">{speed}<span class="u">MB/s</span></div></div>
</div>

<div class="box">
  <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:0 0 8px">
    <div class="sub" style="margin:0">延迟走势（灰色缺口＝丢包，红点＝单次超过 150ms）<span class="hlp" onclick="showHelp('lchart')">?</span></div>
    <div style="margin-left:auto;display:flex;align-items:center;gap:4px">
      <span style="font-size:12px;color:var(--muted)">数据源</span>
      <select id="src" style="font-size:12px;padding:2px 4px;border:0.5px solid var(--line);border-radius:6px;background:transparent;color:var(--fg);font-family:inherit"></select>
      <button class="wbtn" data-n="300">5 分钟</button><button class="wbtn" data-n="1800">30 分钟</button><button class="wbtn" data-n="3600">1 小时</button><button class="wbtn" data-n="43200">12 小时</button>
    </div>
  </div>
  <canvas id="c"></canvas>
  <div class="foot" id="cstat" style="margin-top:6px">（{win_txt}）</div>
</div>

<div class="box">
  <div class="sub" style="margin:0 0 8px">网速走势（MB/s）<span class="hlp" onclick="showHelp('schart')">?</span><span class="foot" id="srange" style="margin-left:8px"></span></div>
  <canvas id="s" style="height:150px"></canvas>
  <table style="margin-top:10px"><thead><tr><th>时间</th><th>网速</th><th>换算</th><th>本次下载</th></tr></thead>
  <tbody>{speed_rows}</tbody></table>
</div>

<div class="box">
  <div class="sub" style="margin:0 0 8px">事件记录<span class="hlp" onclick="showHelp('events')">?</span></div>
  <table><thead><tr><th>时间</th><th>类型</th><th>详情</th><th>持续</th></tr></thead>
  <tbody>{event_rows}</tbody></table>
</div>

<div class="box">
  <div class="sub" style="margin:0 0 6px">链路信息<span class="hlp" onclick="showHelp('link')">?</span></div>
  <div>SSID {ssid} · 频道 {channel} · 接收速率 {rx} · 信号 {rssi}</div>{wifi_hint}
  <div class="foot" style="margin-top:10px">原始数据：{csv_path}</div>
</div>

<div id="mask" onclick="if(event.target===this)closeHelp()"><div id="dlg"><h3 id="dt"></h3><div id="db"></div><div style="text-align:right;margin-top:10px"><button class="wbtn" onclick="closeHelp()">关闭</button></div></div></div>

<script>
const D={payload};
const cv=document.getElementById('c');
const ctx=cv.getContext('2d');
const WINS=[300,1800,3600,43200];
let WN=parseInt(localStorage.getItem('netmon.win')||'300',10);
if(WINS.indexOf(WN)<0){{WN=300;}}
function css(n){{return getComputedStyle(document.documentElement).getPropertyValue(n).trim()}}
function sliceN(){{if(WN===43200)return D.long;const m=D.series.length;return D.series.slice(Math.max(0,m-WN));}}
let SRC=localStorage.getItem('netmon.src')||'live';
function fillSrc(){{
  var el=document.getElementById('src');
  var opts='<option value="live">当前（实时）</option>';
  (D.hist||[]).forEach(function(x){{opts+='<option value="'+x.name+'">'+x.label+'</option>';}});
  el.innerHTML=opts;
  el.value=SRC;
  if(el.selectedIndex<0){{SRC='live';el.value='live';}}
  el.addEventListener('change',function(){{
    SRC=el.value;
    try{{localStorage.setItem('netmon.src',SRC);}}catch(e){{}}
    document.querySelectorAll('.wbtn').forEach(function(x){{x.classList.remove('on');}});
    draw();
  }});
}}
function curData(){{
  if(SRC==='live')return sliceN();
  var h=(D.hist||[]).filter(function(x){{return x.name===SRC;}})[0];
  return h?h.pts:[];
}}
function draw(){{
  const S=curData();
  const dpr=window.devicePixelRatio||1;
  const w=cv.clientWidth,h=cv.clientHeight;
  cv.width=w*dpr;cv.height=h*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  const padL=42,padR=8,padT=10,padB=40;
  const iw=w-padL-padR,ih=h-padT-padB;
  const n=S.length;
  const vmax=S.reduce(function(a,p){{
    return Math.max(a,p.ms||0,p.pk||0);
  }},0);
  const maxY=Math.max(200,Math.ceil(vmax*1.1/20)*20);
  ctx.strokeStyle=css('--grid');ctx.lineWidth=1;ctx.font='11px sans-serif';ctx.fillStyle=css('--muted');ctx.textAlign='left';
  for(let v=0;v<=maxY;v+=Math.round(maxY/4)){{
    const y=padT+ih-(v/maxY)*ih;
    ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(w-padR,y);ctx.stroke();
    ctx.fillText(v+'',6,y+4);
  }}
  if(!n){{document.getElementById('cstat').textContent='暂无数据';return;}}
  const step=n>1?iw/(n-1):0;
  ctx.strokeStyle='#378ADD';ctx.lineWidth=1.5;ctx.beginPath();
  let pen=false;
  S.forEach(function(p,i){{
    if(p.ms===null){{pen=false;return;}}
    const x=padL+i*step,y=padT+ih-(p.ms/maxY)*ih;
    if(!pen){{ctx.moveTo(x,y);pen=true;}}else{{ctx.lineTo(x,y);}}
  }});
  ctx.stroke();
  ctx.fillStyle='#E24B4A';
  S.forEach(function(p,i){{
    if(p.ms!==null&&(p.ms>150||(p.pk&&p.pk>150))){{ctx.beginPath();ctx.arc(padL+i*step,padT+ih-(p.ms/maxY)*ih,3,0,7);ctx.fill();}}
  }});
  ctx.fillStyle=css('--muted');
  ctx.textAlign='center';
  const ticks=n>=1800?6:5;
  for(let k=0;k<ticks;k++){{
    const idx=Math.round(k*(n-1)/(ticks-1));
    const p=S[idx]; if(!p||!p.t) continue;
    const x=padL+idx*step;
    ctx.strokeStyle=css('--grid');ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(x,padT+ih);ctx.lineTo(x,padT+ih+4);ctx.stroke();
    const lx=Math.min(Math.max(x,padL+20),w-padR-20);
    ctx.fillText(p.t,lx,h-22);
  }}
  ctx.textAlign='left';
  ctx.fillText('最近 '+n+' 包',padL,h-6);
  const lost=S.filter(function(p){{return p.ms===null;}}).length;
  const sp150=S.filter(function(p){{return p.ms!==null&&(p.ms>150||(p.pk&&p.pk>150));}}).length;
  const isH=SRC!=='live';
  const unit=WN===43200?' 段（每段 30 秒）':' 包';
  const head=isH?('历史 '+n+' 段（聚合）'):('窗口 '+n+unit);
  document.getElementById('cstat').textContent=head+'（'+S[0].t+' 至 '+S[n-1].t+'）· 无响应 '+lost+' 段 · 峰值 '+vmax+' ms · 超过 150ms '+sp150+' 段';
}}
document.querySelectorAll('.wbtn').forEach(function(b){{
  if(parseInt(b.dataset.n,10)===WN)b.classList.add('on');
  b.addEventListener('click',function(){{
    WN=parseInt(b.dataset.n,10);
    try{{localStorage.setItem('netmon.win',WN);localStorage.setItem('netmon.src','live');}}catch(e){{}}
    SRC='live';
    var sel=document.getElementById('src'); if(sel)sel.value='live';
    document.querySelectorAll('.wbtn').forEach(function(x){{x.classList.remove('on');}});
    b.classList.add('on');
    draw();
  }});
}});
fillSrc();
draw();
window.addEventListener('resize',draw);

const S={payload2};
(function(){{
 const el=document.getElementById('s'); if(!el) return;
 const cx=el.getContext('2d');
 function sp(){{
  const dpr=window.devicePixelRatio||1,w=el.clientWidth,h=el.clientHeight;
  el.width=w*dpr;el.height=h*dpr;cx.setTransform(dpr,0,0,dpr,0,0);
  cx.clearRect(0,0,w,h);
  const padL=44,padR=8,padT=10,padB=20,iw=w-padL-padR,ih=h-padT-padB;
  cx.font='11px sans-serif';cx.fillStyle=css('--muted');
  if(!S.length){{cx.fillText('尚未测速',padL,20);return;}}
  const vals=S.map(p=>p.mbps/8);const maxV=Math.max.apply(null,vals)*1.2||1.5;
  cx.strokeStyle=css('--grid');cx.lineWidth=1;
  for(let i=0;i<=4;i++){{
    const v=maxV*i/4,y=padT+ih-(v/maxV)*ih;
    cx.beginPath();cx.moveTo(padL,y);cx.lineTo(w-padR,y);cx.stroke();
    cx.fillText((i===4?Math.round(v)+' MB/s':Math.round(v*10)/10+''),6,y+4);
  }}
  const n=S.length,step=n>1?iw/(n-1):0;
  cx.strokeStyle='#1D9E75';cx.lineWidth=1.5;cx.beginPath();
  S.forEach((p,i)=>{{const x=padL+i*step,y=padT+ih-((p.mbps/8)/maxV)*ih;i===0?cx.moveTo(x,y):cx.lineTo(x,y);}});
  cx.stroke();
  cx.fillStyle='#1D9E75';
  S.forEach((p,i)=>{{const x=padL+i*step,y=padT+ih-((p.mbps/8)/maxV)*ih;cx.beginPath();cx.arc(x,y,2.5,0,7);cx.fill();}});
  cx.fillStyle=css('--muted');cx.textAlign='left';cx.fillText(S[0].time,padL,h-6);
  cx.textAlign='right';cx.fillText(S[n-1].time,w-padR,h-6);
  cx.textAlign='center';
  let prevDay=null;
  S.forEach(function(p,i){{
    const day=(p.time||'').slice(0,5);
    if(prevDay!==null&&day!==prevDay){{
      const x=padL+i*step;
      cx.strokeStyle=css('--grid');cx.setLineDash([4,4]);
      cx.beginPath();cx.moveTo(x,padT);cx.lineTo(x,padT+ih);cx.stroke();
      cx.setLineDash([]);
      cx.fillText(day,x,padT+8);
    }}
    prevDay=day;
  }});
  const rg=document.getElementById('srange');
  if(rg)rg.textContent='范围：'+S[0].time+' ~ '+S[n-1].time+' · 共 '+n+' 次';
 }}
 sp();window.addEventListener('resize',sp);
}})();
const HELP={help_json};
function showHelp(k){{var h=HELP[k];if(!h)return;document.getElementById('dt').textContent=h.t;document.getElementById('db').innerHTML=h.b;document.getElementById('mask').classList.add('on');}}
function closeHelp(){{document.getElementById('mask').classList.remove('on');}}
document.addEventListener('keydown',function(e){{if(e.key==='Escape')closeHelp();}});
</script>
</body></html>""".format(
        color=color, verdict=verdict, started=started, now=now, win=len(recent),
        win_txt=win_txt, stat_txt=stat_txt,
        gw=state["gateway"], loss=loss, avg=avg, mx=mx, spikes=spikes,
        speed=(round(state["speed"]["mbps"] / 8.0, 1) if state.get("speed") else "—"),
        speed_time=(state["speed"]["time"] if state.get("speed") else "未测"),
        event_rows=event_rows, ssid=wifi.get("ssid") or "-", channel=wifi.get("channel") or "-",
        rx=wifi.get("rx") or "-", rssi=wifi.get("rssi") or "-",
        csv_path=os.path.basename(state["csv"]), payload=payload, payload2=payload2,
        speed_rows=speed_rows, help_json=help_json, wifi_hint=wifi_hint,
    )
    tmp = DASH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, DASH)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default=None)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--http-every", type=int, default=15)
    ap.add_argument("--http-url", default="https://www.qq.com/")
    ap.add_argument("--speed-every", type=int, default=300, help="网速测试间隔秒数，0 表示关闭")
    ap.add_argument("--speed-url", default="https://dldir1.qq.com/weixin/Windows/WeChatSetup.exe")
    ap.add_argument("--speed-seconds", type=float, default=10.0)
    ap.add_argument("--minutes", type=float, default=0, help="0 表示一直跑")
    ap.add_argument("--chart-window", type=int, default=3600,
                    help="走势图最多保留多少个采样点（1 点≈1 秒），默认 3600≈1 小时")
    args = ap.parse_args()

    os.makedirs(RUNS, exist_ok=True)
    gw = args.gateway or detect_gateway()
    if not gw:
        print("无法自动识别网关，请用 --gateway 指定")
        return 2

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(RUNS, "ping_{}.csv".format(stamp))
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        csv.writer(fh).writerow(["time", "seq", "ok", "ms", "kind", "channel", "rssi"])

    wifi0 = wifi_info()
    if not wifi0.get("ssid"):
        print("提示：读不到 Wi-Fi 信息（Windows 需要位置权限）。\n"
              "  打开 设置 > 隐私和安全性 > 位置，开启「定位服务」和「让桌面应用访问你的位置」，\n"
              "  或用管理员身份运行本脚本；否则链路信息将一直显示 -。")
    state = {
        "samples": [], "events": [], "wifi": wifi0, "gateway": gw,
        "started": datetime.datetime.now(), "csv": csv_path, "in_outage": False,
        "speed": None, "last_speed_t": 0.0,
        "speeds": load_speed_history(),
        "speed_csv": os.path.join(RUNS, "speed_{}.csv".format(stamp)),
    }
    with open(state["speed_csv"], "w", newline="", encoding="utf-8-sig") as fh:
        csv.writer(fh).writerow(["time", "mbps", "mb_downloaded"])
    fail_streak = 0
    outage_start = None
    total = 0
    fails = 0
    deadline = time.time() + args.minutes * 60 if args.minutes else None

    print("网关 {} | 数据写入 {} | 看板 {}".format(gw, csv_path, DASH))
    print("按 Ctrl+C 停止\n")

    try:
        while True:
            if deadline and time.time() >= deadline:
                break
            t0 = time.time()
            total += 1
            ok, ms, kind = ping_once(gw)
            stamp_now = datetime.datetime.now().strftime("%H:%M:%S")
            stamp_full = datetime.datetime.now().isoformat(timespec="seconds")
            state["samples"].append({"i": total, "ms": ms, "ok": ok, "t": stamp_now,
                                     "ts": stamp_full})
            _keep = max(args.chart_window, 43200) + 600
            if len(state["samples"]) > _keep:
                state["samples"] = state["samples"][-_keep:]

            if not ok:
                fails += 1
                fail_streak += 1
                if fail_streak == 2:
                    outage_start = t0 - args.interval
                    state["in_outage"] = True
                    state["events"].append({
                        "time": stamp_now, "type": "断链开始",
                        "detail": "连续失败（{}）".format(kind), "dur": "-",
                    })
                    print("[{}] !! 断链开始（{}）".format(stamp_now, kind))
            else:
                if state["in_outage"]:
                    dur = round(time.time() - outage_start, 1)
                    state["in_outage"] = False
                    state["events"].append({
                        "time": stamp_now, "type": "断链恢复", "detail": "链路回到正常",
                        "dur": "{} 秒".format(dur),
                    })
                    print("[{}] -- 断链恢复，持续 {} 秒".format(stamp_now, dur))
                fail_streak = 0

            append_row(csv_path, [
                datetime.datetime.now().isoformat(timespec="seconds"), total,
                int(ok), ms, kind, state["wifi"].get("channel", ""), state["wifi"].get("rssi", ""),
            ])

            if total % args.http_every == 0:
                h_ok, code, h_ms = http_probe(args.http_url)
                state["events"].append({
                    "time": stamp_now, "type": "HTTP 探测",
                    "detail": ("{} 返回 {} 用时 {}ms".format(args.http_url, code, h_ms) if h_ok
                               else "{} 请求失败".format(args.http_url)), "dur": "-",
                })

            now_t = time.time()
            if args.speed_every and now_t - state["last_speed_t"] >= args.speed_every:
                state["last_speed_t"] = now_t
                nbytes, secs, mbps = speed_test(args.speed_url, args.speed_seconds)
                state["speed"] = {"mbps": round(mbps, 1), "mb": round(nbytes / 1048576, 1),
                                  "time": stamp_full[5:10] + " " + stamp_full[11:16]}
                state["speeds"].append({"time": stamp_full[5:10] + " " + stamp_full[11:16],
                                        "ts": stamp_full,
                                        "mbps": round(mbps, 1),
                                        "mb": round(nbytes / 1048576, 1)})
                state["events"].append({
                    "time": stamp_now, "type": "网速测试",
                    "detail": ("下载 {:.1f} MB/s（{:.1f} MB / {:.0f}s）".format(
                        mbps / 8.0, nbytes / 1048576, secs) if mbps > 0 else "测速失败"), "dur": "-",
                })
                append_row(state["speed_csv"], [
                    datetime.datetime.now().isoformat(timespec="seconds"),
                    round(mbps, 1), round(nbytes / 1048576, 1)])
                print("[{}] ~~ 网速 {:.1f} MB/s".format(stamp_now, mbps / 8.0))

            if total % 60 == 0:
                new_wifi = wifi_info()
                if new_wifi.get("channel") and new_wifi["channel"] != state["wifi"].get("channel"):
                    state["events"].append({
                        "time": stamp_now, "type": "信道变化",
                        "detail": "{} -> {}（热点可能重启）".format(
                            state["wifi"].get("channel") or "?", new_wifi["channel"]), "dur": "-",
                    })
                    print("[{}] ** 信道 {} -> {}".format(
                        stamp_now, state["wifi"].get("channel") or "?", new_wifi["channel"]))
                state["wifi"] = new_wifi

            if total % 10 == 0:
                build_dashboard(state, args.chart_window)

            if total % 30 == 0:
                recent = state["samples"][-30:]
                oks = [s["ms"] for s in recent if s["ok"]]
                lost = len(recent) - len(oks)
                print("[{}] 近30包 丢{}({:.0f}%) 均{:.0f}ms 峰{}ms".format(
                    stamp_now, lost, 100.0 * lost / len(recent),
                    (sum(oks) / len(oks)) if oks else 0, max(oks) if oks else 0))
            time.sleep(max(0.0, args.interval - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        build_dashboard(state, args.chart_window)
        oks = [s["ms"] for s in state["samples"] if s["ok"]]
        print("\n本次共 {} 包，失败 {}（{:.2f}%），平均 {:.1f}ms，峰值 {}ms".format(
            total, fails, 100.0 * fails / total if total else 0,
            (sum(oks) / len(oks)) if oks else 0, max(oks) if oks else 0))
        print("数据：{}".format(csv_path))
        print("看板：{}".format(DASH))
    return 0


if __name__ == "__main__":
    sys.exit(main())

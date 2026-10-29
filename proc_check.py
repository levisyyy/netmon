# -*- coding: utf-8 -*-
"""进程体检：一眼看清监测脚本是否在跑、占多少资源、最新数据是什么。

双击 查看进程.bat（或 check_proc.bat）即可运行本脚本，也可以直接 python proc_check.py。
"""
import csv
import glob
import os
import re
import subprocess
import datetime

RUNS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")
ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(ROOT, "netmon_live.html")

PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
# 命令内容保持纯 ASCII，避免各终端编码差异
PS_CMD = (
    "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
    "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.Name + '|' + $_.CommandLine }"
)


def decode(b):
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return b.decode("utf-8", "ignore")


def ps_processes():
    """优先用 PowerShell 取命令行；失败则退回 tasklist（无命令行）。"""
    try:
        p = subprocess.run([PS, "-NoProfile", "-Command", PS_CMD],
                           capture_output=True, timeout=40)
        text = decode(p.stdout)
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            pid, name, cmd = line.split("|", 2)
            rows.append({"pid": pid.strip(), "name": name.strip(),
                         "cmd": cmd.strip(), "src": "powershell"})
        if rows:
            return rows, "PowerShell"
    except Exception:
        pass
    # 降级：tasklist 只能拿到进程名和 PID
    rows = []
    try:
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=30)
        for line in decode(p.stdout).splitlines():
            m = re.match(r'"([^"]+)","(\d+)"', line.strip())
            if m:
                rows.append({"pid": m.group(2), "name": m.group(1),
                             "cmd": "(无法读取命令行)", "src": "tasklist"})
    except Exception:
        pass
    return rows, "tasklist（无命令行）"


def mem_mb(pid):
    """用 tasklist 的内存列换算 MB。"""
    try:
        p = subprocess.run(["tasklist", "/FI", "PID eq " + str(pid), "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=20)
        for line in decode(p.stdout).splitlines():
            parts = [x.strip('"') for x in line.strip().split('","')]
            if len(parts) >= 5:
                kb = parts[4].replace(",", "").replace(".", "")
                kb = re.sub(r"[^\d]", "", kb)
                if kb:
                    return round(int(kb) / 1024, 1)
    except Exception:
        pass
    return None


def last_csv(pattern):
    fs = sorted(glob.glob(os.path.join(RUNS, pattern)))
    return fs[-1] if fs else None


def csv_tail(path, n=1):
    try:
        with open(path, encoding="utf-8-sig", errors="ignore") as f:
            rows = list(csv.DictReader(f))
        return rows
    except Exception:
        return []


def live_info():
    """从看板里提取最近刷新时间，判断脚本是否还活着。"""
    try:
        with open(LIVE, encoding="utf-8", errors="ignore") as f:
            html = f.read()
    except Exception:
        return None
    m = re.search(r"最近刷新[^<]*<[^>]*>\s*([0-9]{1,2}:[0-9]{2}:[0-9]{2})", html)
    if not m:
        m = re.search(r"([0-9]{2}:[0-9]{2}:[0-9]{2})\s*</", html)
    if not m:
        return None
    txt = m.group(1)
    try:
        hh, mm, ss = [int(x) for x in txt.split(":")]
        now = datetime.datetime.now()
        t = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        delta = (now - t).total_seconds()
        if delta < 0:
            delta += 86400
        return txt, round(delta)
    except Exception:
        return txt, None


def main():
    print("=" * 58)
    print(" 进程体检  " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 58)

    procs, src = ps_processes()
    print("\n[1] Python 进程列表（来源：{}）".format(src))
    if not procs:
        print("    没有发现任何 python 进程 —— 监测脚本当前没有在运行")
    for p in procs:
        mb = mem_mb(p["pid"])
        tag = ""
        if "netmon.py" in p["cmd"]:
            tag = "   <== 这就是网络监测脚本"
        print("    PID {:>6}  {}  {} MB{}".format(
            p["pid"], p["name"], mb if mb is not None else "?", tag))
        cmd = p["cmd"]
        if len(cmd) > 96:
            cmd = "..." + cmd[-96:]
        print("           命令行：{}".format(cmd))

    running = any("netmon.py" in p["cmd"] for p in procs)
    print("\n[2] 监测脚本 netmon.py：{}".format("正在运行" if running else "未运行"))

    print("\n[3] 看板心跳（netmon_live.html 每 5 秒刷新）")
    info = live_info()
    if info:
        txt, delta = info
        if delta is None:
            print("    最近刷新 {}".format(txt))
        elif delta <= 20:
            print("    最近刷新 {}（{} 秒前）—— 脚本存活".format(txt, delta))
        else:
            print("    最近刷新 {}（{} 秒前）—— 脚本可能已停止".format(txt, delta))
    else:
        print("    未找到看板文件")

    print("\n[4] 最新数据文件")
    for label, pat, keys in (
        ("延迟记录", "ping_*.csv", ("time", "ms", "ok", "channel")),
        ("网速记录", "speed_*.csv", ("time", "mbps")),
    ):
        f = last_csv(pat)
        if not f:
            print("    {}：暂无".format(label))
            continue
        rows = csv_tail(f)
        size = os.path.getsize(f)
        print("    {}：{}  ({} 行, {:.0f} KB)".format(
            label, os.path.basename(f), len(rows), size / 1024))
        if rows:
            r = rows[-1]
            if label == "延迟记录":
                print("        最后一次 {} 延迟 {}ms 状态 {} 频道 {}".format(
                    r.get("time", "?")[11:], r.get("ms", "?"),
                    "通" if r.get("ok") == "1" else "失败", r.get("channel", "?")))
            else:
                print("        最后一次 {} 网速 {} Mbps".format(
                    r.get("time", "?")[11:], r.get("mbps", "?")))

    print("\n[5] 怎么看资源占用")
    print("    任务管理器 -> 详细信息 -> 右键列标题 -> 选择列")
    print("    勾选「命令行」「CPU 时间」，即可定位到 netmon.py 那一行")
    print("=" * 58)


if __name__ == "__main__":
    main()

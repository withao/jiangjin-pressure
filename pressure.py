#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
江津区气压采集 + 曲线绘制
=========================================================
数据源：Open-Meteo（免 API Key，公开接口）
    https://api.open-meteo.com/v1/forecast
    变量：surface_pressure（本站气压 / 地面气压，hPa）
          pressure_msl   （海平面气压，hPa，便于和天气图对比）
    原生时间分辨率：15 分钟（直接对齐，无需插值）

用法
---------------------------------------------------------
  python pressure.py                  # 抓 72 小时，页面默认显示 24 小时
  python pressure.py --view 72        # 页面默认显示 72 小时
  python pressure.py --view 6         # 页面默认显示 6 小时
  python pressure.py --fetch-hours 2  # 只抓最近 2 小时（有档案累积时）
  python pressure.py --watch          # 常驻：每 15 分钟抓一次并刷新图
  python pressure.py --plot-only      # 不联网，用已有 CSV 重画
  python pressure.py --png            # 额外输出 PNG（需 matplotlib）

页面上的「6 / 24 / 72 小时」按钮可随时切换，数据已一次性嵌入，切换不联网。

产物
---------------------------------------------------------
  data/pressure.csv     时间序列（追加、自动去重）
  output/pressure.html  交互式曲线图（单文件，可离线打开）
  output/pressure.png   静态图（仅 --png 且装了 matplotlib）

说明
---------------------------------------------------------
  * surface_pressure 随海拔变化：江津城区海拔约 260 m，故本站气压约 980 hPa，
    这是正常值；要跟天气预报里的 1013 比，请看 pressure_msl。
  * 全部逻辑只用 Python 标准库，不需要 pip install。
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_DIR = os.path.join(BASE_DIR, "output")

# 江津区（几江街道一带）
LAT, LON = 29.2833, 106.2667
PLACE = "重庆市江津区"
TZ = "Asia/Shanghai"

API_URL = "https://api.open-meteo.com/v1/forecast"
SAMPLE_MIN = 15          # 采样间隔（分钟）＝数据源原生分辨率
MAX_HOURS = 72           # 接口 past_minutely_15 上限约 3 天
TIMEOUT = 30
RETRIES = 3

# 中国无夏令时，固定 +8 即可。**不要用 ZoneInfo("Asia/Shanghai")**：
# Windows 没有系统时区库、GitHub Actions 的 UTC 环境又会让 datetime.now() 偏 8 小时，
# 固定偏移在两边行为一致。
TZ_OFFSET = timezone(timedelta(hours=8))


def now_local() -> datetime:
    """当前东八区时间（naive）。云端 / 本地结果一致，不依赖系统时区。"""
    return datetime.now(TZ_OFFSET).replace(tzinfo=None)

SERIES = {
    "s": ("surface_pressure", "本站气压", "hPa"),
    "m": ("pressure_msl", "海平面气压", "hPa"),
}

CSV_HEADER = ["time", "surface_pressure_hpa", "pressure_msl_hpa"]


# ----------------------------------------------------------------------------
# 抓数
# ----------------------------------------------------------------------------
def fetch(hours: int) -> Tuple[List[dict], dict]:
    """取最近 hours 小时的 15 分钟级气压，返回 [{time, s, m}, ...] 与元信息。"""
    hours = max(1, min(hours, MAX_HOURS))
    steps = int(hours * 60 / 15) + 2

    params = {
        "latitude": f"{LAT}",
        "longitude": f"{LON}",
        "minutely_15": "surface_pressure,pressure_msl",
        "past_minutely_15": str(steps),
        "forecast_minutely_15": "1",
        "timezone": TZ,
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)

    last_err: Optional[Exception] = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "jiangjin-pressure/1.0"}
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_err = exc
            if attempt < RETRIES:
                wait = 2 * attempt
                print(f"  ! 第 {attempt} 次请求失败（{exc}），{wait}s 后重试…")
                time.sleep(wait)
    else:
        raise RuntimeError(f"请求失败（已重试 {RETRIES} 次）：{last_err}") from last_err

    block = payload.get("minutely_15") or {}
    times = block.get("time") or []
    sp = block.get("surface_pressure") or []
    msl = block.get("pressure_msl") or []

    rows: List[dict] = []
    for i, ts in enumerate(times):
        s = sp[i] if i < len(sp) else None
        m = msl[i] if i < len(msl) else None
        if s is None and m is None:
            continue
        rows.append({
            "t": datetime.fromisoformat(ts),
            "s": float(s) if s is not None else None,
            "m": float(m) if m is not None else None,
        })

    meta = {
        "lat": payload.get("latitude", LAT),
        "lon": payload.get("longitude", LON),
        "elevation": payload.get("elevation"),
        "fetched_at": now_local(),
        "steps": len(rows),
    }
    return rows, meta


def resample(rows: Sequence[dict], minutes: int = SAMPLE_MIN) -> List[dict]:
    """把 15 分钟序列重采样到 minutes 分钟网格；间隔为 15 时即原样对齐。"""
    if not rows:
        return []

    rows = sorted(rows, key=lambda r: r["t"])
    step = timedelta(minutes=minutes)

    # 网格起点：对齐到最近的整分刻度
    t0 = rows[0]["t"].replace(second=0, microsecond=0)
    t0 += timedelta(minutes=(minutes - t0.minute % minutes) % minutes)
    t_end = rows[-1]["t"]

    # 分列成 (时间, 值) 便于插值
    def col(key: str) -> List[Tuple[datetime, float]]:
        return [(r["t"], r[key]) for r in rows if r.get(key) is not None]

    cs, cm = col("s"), col("m")

    def interp(pairs: List[Tuple[datetime, float]], t: datetime) -> Optional[float]:
        if not pairs:
            return None
        keys = [p[0] for p in pairs]
        i = bisect.bisect_left(keys, t)
        if i == 0:
            return pairs[0][1] if keys[0] == t else None   # 不向外推
        if i >= len(pairs):
            return pairs[-1][1] if keys[-1] == t else None
        (ta, va), (tb, vb) = pairs[i - 1], pairs[i]
        if tb == ta:
            return vb
        # 间隔过大（>45min）说明有缺测，不插值
        if (tb - ta) > timedelta(minutes=45):
            return None
        ratio = (t - ta).total_seconds() / (tb - ta).total_seconds()
        return va + (vb - va) * ratio

    out: List[dict] = []
    t = t0
    while t <= t_end:
        s = interp(cs, t)
        m = interp(cm, t)
        if s is not None or m is not None:
            out.append({
                "t": t,
                "s": round(s, 2) if s is not None else None,
                "m": round(m, 2) if m is not None else None,
            })
        t += step
    return out


# ----------------------------------------------------------------------------
# CSV 存取
# ----------------------------------------------------------------------------
def load_csv(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out: List[dict] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                t = datetime.fromisoformat(row["time"])
            except (KeyError, ValueError):
                continue
            out.append({
                "t": t,
                "s": _f(row.get("surface_pressure_hpa")),
                "m": _f(row.get("pressure_msl_hpa")),
            })
    return out


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


def save_csv(path: str, rows: Sequence[dict]) -> List[dict]:
    """合并去重（同时间戳以新值覆盖），按时间排序后落盘。"""
    merged: Dict[datetime, dict] = {}
    for r in rows:
        merged[r["t"]] = r
    ordered = [merged[k] for k in sorted(merged)]

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for r in ordered:
            w.writerow([
                r["t"].strftime("%Y-%m-%d %H:%M"),
                "" if r["s"] is None else f"{r['s']:.2f}",
                "" if r["m"] is None else f"{r['m']:.2f}",
            ])
    return ordered


# ----------------------------------------------------------------------------
# 统计
# ----------------------------------------------------------------------------
def stats(rows: Sequence[dict], key: str) -> dict:
    vals = [r for r in rows if r.get(key) is not None]
    if not vals:
        return {}
    cur = vals[-1][key]
    times = [r["t"] for r in vals]

    def at(t: datetime) -> Optional[float]:
        i = bisect.bisect_right(times, t) - 1
        return vals[i][key] if i >= 0 else None

    chg3 = None
    v3 = at(vals[-1]["t"] - timedelta(hours=3))
    if v3 is not None:
        chg3 = round(cur - v3, 1)

    return {
        "current": cur,
        "chg3h": chg3,
        "max": max(r[key] for r in vals),
        "min": min(r[key] for r in vals),
        "span": round(max(r[key] for r in vals) - min(r[key] for r in vals), 1),
        "n": len(vals),
        "first": vals[0]["t"],
        "last": vals[-1]["t"],
    }


# ----------------------------------------------------------------------------
# 绘图：交互式 HTML（Canvas，零依赖、可离线）
# ----------------------------------------------------------------------------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
__REFRESH__
<title>__PLACE__ · 气压</title>
<style>
  :root{
    --bg:#0e1116; --panel:#151a21; --line:#232a33; --grid:#1e252e;
    --fg:#e8edf3; --dim:#8b97a6; --accent:#4ea1ff; --accent2:#f0a35e;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    background:radial-gradient(1200px 600px at 20% -10%, #17202b 0%, var(--bg) 60%);
    color:var(--fg); font-family:"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
    min-height:100vh; padding:28px 30px 20px;
  }
  header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}
  h1{font-size:22px;font-weight:600;letter-spacing:.5px}
  h1 span{color:var(--dim);font-weight:400;font-size:14px;margin-left:10px}
  .sub{color:var(--dim);font-size:12.5px;line-height:1.7;text-align:right}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
  .card{background:linear-gradient(180deg,#171d25,#141920);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .card .k{color:var(--dim);font-size:12px;letter-spacing:.3px}
  .card .v{font-size:22px;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums}
  .card .v small{font-size:12px;color:var(--dim);font-weight:400;margin-left:3px}
  .up{color:#ff6b6b} .down{color:#3ddc97} .flat{color:var(--dim)}
  .toolbar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
  .sep{width:1px;height:18px;background:var(--line);margin:0 2px}
  .btn{background:#1a212a;border:1px solid var(--line);color:var(--dim);padding:6px 14px;border-radius:7px;
       font-size:12.5px;cursor:pointer;transition:.15s}
  .btn:hover{color:var(--fg);border-color:#33404f}
  .btn.on{background:#1e3a5f;border-color:#2f5c92;color:#cfe4ff}
  .legend{margin-left:auto;color:var(--dim);font-size:12px;display:flex;gap:14px;align-items:center}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
  .wrap{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:6px}
  canvas{display:block;width:100%;height:440px}
  .tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .12s;
       background:rgba(12,16,21,.95);border:1px solid #2d3946;border-radius:8px;padding:9px 12px;
       font-size:12.5px;line-height:1.7;white-space:nowrap;box-shadow:0 8px 24px rgba(0,0,0,.45);
       font-variant-numeric:tabular-nums;z-index:5}
  .tip b{color:var(--accent);font-size:15px}
  footer{color:var(--dim);font-size:11.5px;margin-top:14px;line-height:1.8;display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap}
  .empty{padding:80px 0;text-align:center;color:var(--dim)}
</style>
</head>
<body>
<header>
  <h1>__PLACE__ · 气压<span>间隔 __STEP__ 分钟</span></h1>
  <div class="sub">
    坐标 __LAT__°N, __LON__°E　海拔 __ELEV__ m<br>
    生成于 __GEN__
  </div>
</header>

<div class="cards" id="cards"></div>

<div class="toolbar">
  <button class="btn on" data-k="s">本站气压</button>
  <button class="btn" data-k="m">海平面气压</button>
  <span class="sep"></span>
  <button class="btn" data-w="6">6 小时</button>
  <button class="btn" data-w="24">24 小时</button>
  <button class="btn" data-w="72">72 小时</button>
  <div class="legend">
    <span id="range"></span>
  </div>
</div>

<div class="wrap" id="wrap">
  <canvas id="cv"></canvas>
  <div class="tip" id="tip"></div>
</div>

<script>
const DATA = __DATA__;
const META = __META__;
const COLORS = {s:"#4ea1ff", m:"#f0a35e"};
let KEY = "s";
let WIN = __VIEW__;              // 当前显示窗口（小时）

const cv = document.getElementById("cv");
const ctx = cv.getContext("2d");
const wrap = document.getElementById("wrap");
const tip = document.getElementById("tip");

function all(k){ return DATA.filter(d => d[k] !== null).map(d => ({t:new Date(d.t.replace(" ","T")), v:d[k]})); }

// 只画最近 WIN 小时（以最后一个数据点为锚，避免本机时间与数据时区不一致）
function pts(k){
  const A = all(k);
  if (!A.length) return A;
  const end = A[A.length-1].t.getTime();
  const S = A.filter(p => p.t.getTime() >= end - WIN*3600e3);
  return S.length ? S : A;
}

function niceStep(raw){
  const p = Math.pow(10, Math.floor(Math.log10(raw||1)));
  const n = raw / p;
  const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return m * p;
}

let geom = null;

function draw(){
  const dpr = window.devicePixelRatio || 1;
  const W = wrap.clientWidth - 12, H = 440;
  cv.width = W * dpr; cv.height = H * dpr;
  cv.style.width = W + "px"; cv.style.height = H + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const P = pts(KEY);
  if (!P.length){ ctx.fillStyle="#8b97a6"; ctx.font="14px system-ui"; ctx.textAlign="center";
    ctx.fillText("暂无数据", W/2, H/2); geom=null; return; }

  const pad = {l:62, r:22, t:22, b:36};
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;

  let vmin = Math.min(...P.map(p=>p.v)), vmax = Math.max(...P.map(p=>p.v));
  let span = vmax - vmin;
  if (span < 0.6){ const mid=(vmax+vmin)/2; vmin=mid-0.3; vmax=mid+0.3; span=0.6; }
  const stepY = niceStep(span/5);
  const y0 = Math.floor(vmin/stepY)*stepY - stepY*0.35;
  const y1 = Math.ceil(vmax/stepY)*stepY + stepY*0.35;

  const t0 = P[0].t.getTime(), t1 = P[P.length-1].t.getTime();
  const X = t => pad.l + (t1===t0 ? iw/2 : (t - t0)/(t1-t0)*iw);
  const Y = v => pad.t + (y1 - v)/(y1-y0)*ih;

  // 网格 + Y 轴
  ctx.font = "12px ui-monospace,Consolas,monospace";
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let v = Math.ceil(y0/stepY)*stepY; v <= y1; v += stepY){
    const y = Y(v);
    ctx.strokeStyle = "#1e252e"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad.l, y+.5); ctx.lineTo(pad.l+iw, y+.5); ctx.stroke();
    ctx.fillStyle = "#8b97a6";
    ctx.fillText(v.toFixed(stepY < 1 ? 1 : 0), pad.l - 10, y);
  }

  // X 轴刻度：随窗口自适应（6h→1h，24h→3h，72h→6h），并按本地零点对齐
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  const spanH = (t1 - t0) / 3600e3;
  const tickH = spanH <= 8 ? 1 : spanH <= 30 ? 3 : 6;
  const tickMs = tickH * 3600e3;
  const anchor = new Date(t0); anchor.setHours(0,0,0,0);
  let tt = anchor.getTime();
  while (tt < t0) tt += tickMs;
  while (tt <= t1){
    const x = X(tt);
    ctx.strokeStyle = "#1a2029";
    ctx.beginPath(); ctx.moveTo(x+.5, pad.t); ctx.lineTo(x+.5, pad.t+ih); ctx.stroke();
    const d = new Date(tt);
    const lbl = String(d.getHours()).padStart(2,"0") + ":00";
    ctx.fillStyle = "#7b8794";
    ctx.fillText(lbl, x, pad.t + ih + 9);
    // 零点标日期
    if (d.getHours() === 0){
      ctx.fillStyle = "#a9b6c5";
      ctx.fillText((d.getMonth()+1) + "/" + d.getDate(), x, pad.t + ih + 24);
    }
    tt += tickMs;
  }

  // 面积渐变
  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ih);
  grad.addColorStop(0, KEY === "s" ? "rgba(78,161,255,.26)" : "rgba(240,163,94,.26)");
  grad.addColorStop(1, KEY === "s" ? "rgba(78,161,255,0)" : "rgba(240,163,94,0)");
  ctx.beginPath();
  ctx.moveTo(X(P[0].t.getTime()), Y(P[0].v));
  for (let i=1;i<P.length;i++) ctx.lineTo(X(P[i].t.getTime()), Y(P[i].v));
  ctx.lineTo(X(P[P.length-1].t.getTime()), pad.t+ih);
  ctx.lineTo(X(P[0].t.getTime()), pad.t+ih);
  ctx.closePath(); ctx.fillStyle = grad; ctx.fill();

  // 折线
  ctx.beginPath();
  ctx.moveTo(X(P[0].t.getTime()), Y(P[0].v));
  for (let i=1;i<P.length;i++) ctx.lineTo(X(P[i].t.getTime()), Y(P[i].v));
  ctx.strokeStyle = COLORS[KEY]; ctx.lineWidth = 2.1;
  ctx.lineJoin = "round"; ctx.lineCap = "round";
  ctx.shadowColor = KEY === "s" ? "rgba(78,161,255,.45)" : "rgba(240,163,94,.45)";
  ctx.shadowBlur = 10; ctx.stroke(); ctx.shadowBlur = 0;

  // 末点
  const last = P[P.length-1];
  const lx = X(last.t.getTime()), ly = Y(last.v);
  ctx.beginPath(); ctx.arc(lx, ly, 4.5, 0, Math.PI*2);
  ctx.fillStyle = COLORS[KEY]; ctx.fill();
  ctx.beginPath(); ctx.arc(lx, ly, 9, 0, Math.PI*2);
  ctx.fillStyle = KEY === "s" ? "rgba(78,161,255,.18)" : "rgba(240,163,94,.18)"; ctx.fill();

  geom = {P, X, Y, pad, iw, ih, W, H};
  document.getElementById("range").textContent =
    P[0].t.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"})
    + " → " + last.t.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
}

// 卡片
function cards(){
  const P = pts(KEY), A = all(KEY);
  const box = document.getElementById("cards");
  if (!P.length){ box.innerHTML = ""; return; }
  const cur = P[P.length-1].v;
  const end = P[P.length-1].t.getTime();

  // 变压始终用完整数据算，不受当前窗口限制（看 6 小时也能知道 24 小时的变化）
  function delta(h){
    const from = end - h*3600e3;
    let ref = A[0];
    for (const p of A){ if (p.t.getTime() <= from) ref = p; else break; }
    return cur - ref.v;
  }
  // 24 小时“路程”= Σ|ΔP|，即这 24 小时逐点差值绝对值之和（总起伏）
  // 净变化小、路程大 = 先降后升/先升后降的来回型
  const from24 = end - 24*3600e3;
  const seg24 = A.filter(p => p.t.getTime() >= from24);
  let path24 = 0;
  for (let i = 1; i < seg24.length; i++) path24 += Math.abs(seg24[i].v - seg24[i-1].v);

  const d3 = delta(3), d24 = delta(24);
  const cls = v => v >= 1.0 ? "up" : v <= -1.0 ? "down" : "flat";
  const word = v => v >= 1.0 ? "升压" : v <= -1.0 ? "降压" : "少变";
  const fmt = v => (v>=0?"+":"") + v.toFixed(1);
  const mx = Math.max(...P.map(p=>p.v)), mn = Math.min(...P.map(p=>p.v));
  const unit = "<small>hPa</small>";

  // 形状判读：路程 / 净位移 ≥3 说明来回折返，单向型则接近 1
  const ratio = path24 / Math.max(Math.abs(d24), 0.5);
  const shape = ratio >= 3 ? "来回型" : ratio >= 1.8 ? "有折返" : "单向型";

  box.innerHTML = `
    <div class="card"><div class="k">当前气压</div><div class="v">${cur.toFixed(1)}${unit}</div></div>
    <div class="card"><div class="k">3 小时变压</div><div class="v ${cls(d3)}">${fmt(d3)}${unit}
      <small style="margin-left:6px">${word(d3)}</small></div></div>
    <div class="card"><div class="k">24 小时变压</div><div class="v ${cls(d24)}">${fmt(d24)}${unit}
      <small style="margin-left:6px">${word(d24)}</small></div></div>
    <div class="card"><div class="k">24 小时路程 Σ|ΔP|</div><div class="v">${path24.toFixed(1)}${unit}
      <small style="margin-left:6px">${shape}</small></div></div>
    <div class="card"><div class="k">窗口最高 / 最低</div><div class="v">${mx.toFixed(1)} / ${mn.toFixed(1)}${unit}</div></div>
    <div class="card"><div class="k">窗口波动</div><div class="v">${(mx-mn).toFixed(1)}${unit}</div></div>
    <div class="card"><div class="k">采样点数</div><div class="v">${P.length}<small>个</small></div></div>`;
}

// 悬停
cv.addEventListener("mousemove", e => {
  if (!geom) return;
  const r = cv.getBoundingClientRect();
  const mx = e.clientX - r.left;
  const {P, X, Y} = geom;
  let best = 0, bd = Infinity;
  P.forEach((p,i) => { const d = Math.abs(X(p.t.getTime()) - mx); if (d < bd){ bd = d; best = i; } });
  const p = P[best], x = X(p.t.getTime()), y = Y(p.v);

  draw();
  const g = geom;
  ctx.strokeStyle = "rgba(160,180,200,.35)"; ctx.lineWidth = 1;
  ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(x+.5, g.pad.t); ctx.lineTo(x+.5, g.pad.t+g.ih); ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI*2);
  ctx.fillStyle = "#0e1116"; ctx.fill();
  ctx.strokeStyle = COLORS[KEY]; ctx.lineWidth = 2; ctx.stroke();

  const ts = p.t.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
  tip.innerHTML = `${ts}<br><b>${p.v.toFixed(1)}</b> hPa`;
  tip.style.opacity = 1;
  const tw = tip.offsetWidth;
  let left = x + 14;
  if (left + tw > g.W - 8) left = x - tw - 14;
  tip.style.left = left + "px";
  tip.style.top = Math.max(8, y - 52) + "px";
});
cv.addEventListener("mouseleave", () => { tip.style.opacity = 0; draw(); });

function bind(sel, apply){
  const btns = document.querySelectorAll(sel);
  btns.forEach(b => {
    b.addEventListener("click", () => {
      btns.forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      apply(b);
      cards(); draw();
    });
  });
}
bind("[data-k]", b => { KEY = b.dataset.k; });
bind("[data-w]", b => { WIN = parseInt(b.dataset.w, 10); });

// 按 __VIEW__ 点亮对应的窗口按钮
document.querySelectorAll("[data-w]").forEach(b => {
  if (parseInt(b.dataset.w, 10) === WIN) b.classList.add("on");
});

window.addEventListener("resize", () => { draw(); });
cards(); draw();
</script>
</body>
</html>
"""


def render_html(path: str, rows: Sequence[dict], meta: dict, step: int = SAMPLE_MIN,
                refresh: int = 0, view: int = 24) -> None:
    data = [
        {
            "t": r["t"].strftime("%Y-%m-%d %H:%M"),
            "s": r["s"],
            "m": r["m"],
        }
        for r in rows
    ]
    m = {
        "place": PLACE,
        "lat": round(float(meta.get("lat", LAT)), 4),
        "lon": round(float(meta.get("lon", LON)), 4),
        "elevation": meta.get("elevation"),
        "step": step,
        "generated": now_local().strftime("%Y-%m-%d %H:%M:%S"),
    }

    html = (HTML_TEMPLATE
            .replace("__PLACE__", PLACE)
            .replace("__REFRESH__", f'<meta http-equiv="refresh" content="{refresh}">'
                     if refresh > 0 else "")
            .replace("__LAT__", f"{m['lat']}")
            .replace("__LON__", f"{m['lon']}")
            .replace("__ELEV__", "—" if m["elevation"] is None else f"{m['elevation']:.0f}")
            .replace("__STEP__", str(step))
            .replace("__VIEW__", str(view))
            .replace("__GEN__", m["generated"])
            .replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            .replace("__META__", json.dumps(m, ensure_ascii=False)))

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)


# ----------------------------------------------------------------------------
# 可选：matplotlib 静态图
# ----------------------------------------------------------------------------
def render_png(path: str, rows: Sequence[dict], key: str = "s") -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("  · 未安装 matplotlib，跳过 PNG（HTML 已生成，可直接看）")
        return False

    from matplotlib import font_manager

    avail = {f.name for f in font_manager.fontManager.ttflist}
    for fam in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC",
                "WenQuanYi Zen Hei", "DejaVu Sans"):
        if fam in avail:
            plt.rcParams["font.sans-serif"] = [fam]
            break
    plt.rcParams["axes.unicode_minus"] = False

    P = [r for r in rows if r.get(key) is not None]
    if not P:
        return False
    xs = [r["t"] for r in P]
    ys = [r[key] for r in P]
    label = SERIES[key][1]
    color = "#4ea1ff" if key == "s" else "#f0a35e"

    fig, ax = plt.subplots(figsize=(11, 4.6), dpi=150)
    fig.patch.set_facecolor("#0e1116")
    ax.set_facecolor("#151a21")
    ax.plot(xs, ys, color=color, lw=1.8, label=label)
    ax.fill_between(xs, ys, min(ys), color=color, alpha=0.12)
    ax.scatter([xs[-1]], [ys[-1]], color=color, s=36, zorder=5)
    ax.annotate(f"{ys[-1]:.1f}", (xs[-1], ys[-1]), textcoords="offset points",
                xytext=(6, 6), color="#e8edf3", fontsize=10)

    ax.set_title(f"{PLACE} · {label}（{SAMPLE_MIN} 分钟间隔）", color="#e8edf3",
                 fontsize=13, pad=12, loc="left")
    ax.set_ylabel("hPa", color="#8b97a6")
    ax.grid(True, color="#232a33", lw=.8)
    for sp in ax.spines.values():
        sp.set_color("#2a333f")
    ax.tick_params(colors="#8b97a6", labelsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return True


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run_once(args, base: Optional[List[dict]] = None) -> List[dict]:
    rows, meta = fetch(args.fetch_hours)
    grid = resample(rows, args.interval)
    if base is None:                       # 单次模式：也要接上已有档案
        base = load_csv(args.csv)
    merged = save_csv(args.csv, list(base) + grid)

    # CSV 是长期档案（全部保留），曲线只画请求窗口
    cut = now_local() - timedelta(hours=args.hours)
    window = [r for r in merged if r["t"] >= cut] or merged
    render_html(args.html, window, meta, args.interval, args.refresh, args.view)

    st = stats(window, args.var)
    if st:
        print(f"  气压 {st['current']:.1f} hPa"
              + (f"（3h {st['chg3h']:+.1f}）" if st["chg3h"] is not None else "")
              + f" | 区间 {st['min']:.1f}–{st['max']:.1f} | {st['n']} 点")
    print(f"  → {args.csv}")
    print(f"  → {args.html}")

    if args.png:
        png = os.path.splitext(args.html)[0] + ".png"
        if render_png(png, window, args.var):
            print(f"  → {png}")
    return merged


def next_mark(now: datetime, step: int) -> datetime:
    """返回下一个 step 分钟刻度（对齐到整点后的 :00/:15/:30/:45 …）。"""
    base = now.replace(minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=(now.minute // step + 1) * step)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="抓取重庆市江津区气压并按 20 分钟间隔绘制曲线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--hours", type=int, default=MAX_HOURS,
                    help=f"抓取并嵌入的数据深度（小时），默认 {MAX_HOURS}（接口上限）；页面可切 6/24/72 查看")
    ap.add_argument("--fetch-hours", type=int, default=None,
                    help="每次联网抓取回溯小时数，默认同 --hours；有本地档案累积时可设小值（如 2）")
    ap.add_argument("--interval", type=int, default=SAMPLE_MIN, help="采样间隔（分钟），默认 15")
    ap.add_argument("--var", choices=["s", "m"], default="s",
                    help="s=本站气压（默认）  m=海平面气压")
    ap.add_argument("--view", type=int, default=24,
                    help="页面默认显示窗口（小时），可在页面上切 6/24/72，默认 24")
    ap.add_argument("--watch", action="store_true", help="常驻模式：每 20 分钟抓一次并刷新")
    ap.add_argument("--plot-only", action="store_true", help="不联网，用已有 CSV 重画")
    ap.add_argument("--png", action="store_true", help="额外输出 PNG（需 matplotlib）")
    ap.add_argument("--refresh", type=int, default=None,
                    help="HTML 自动刷新秒数（常驻模式默认 300，普通模式默认关闭）")
    ap.add_argument("--csv", default=os.path.join(DATA_DIR, "pressure.csv"))
    ap.add_argument("--html", default=os.path.join(OUT_DIR, "pressure.html"))
    args = ap.parse_args(argv)

    args.hours = max(1, min(args.hours, MAX_HOURS))
    args.view = max(1, min(args.view, args.hours))
    if args.fetch_hours is None:
        args.fetch_hours = args.hours
    args.fetch_hours = max(1, min(args.fetch_hours, MAX_HOURS))
    if args.refresh is None:
        args.refresh = 300 if args.watch else 0
    print(f"[{PLACE}] {LAT}°N {LON}°E · {args.interval} 分钟间隔 · 抓取 {args.fetch_hours}h / 嵌入 {args.hours}h / 默认看 {args.view}h")

    if args.plot_only:
        rows = load_csv(args.csv)
        if not rows:
            print("  ! CSV 为空，先跑一次联网抓取")
            return 1
        # 与联网模式一致：CSV 是全量档案，曲线只画 --hours 窗口
        cut = now_local() - timedelta(hours=args.hours)
        window = [r for r in rows if r["t"] >= cut]
        if not window:                      # 档案比窗口还旧，退回全部
            window = rows
        render_html(args.html, window, {"lat": LAT, "lon": LON, "elevation": None},
                    args.interval, args.refresh, args.view)
        st = stats(window, args.var)
        print(f"  {st['n']} 点 · {st['first']:%m-%d %H:%M} → {st['last']:%m-%d %H:%M}")
        print(f"  → {args.html}")
        return 0

    if not args.watch:
        try:
            run_once(args)
        except Exception as exc:  # noqa: BLE001
            print(f"  × {exc}")
            return 2
        return 0

    # 常驻
    print("  常驻模式，Ctrl+C 结束")
    base = load_csv(args.csv)
    try:
        while True:
            try:
                base = run_once(args, base)
            except Exception as exc:  # noqa: BLE001
                print(f"  × 本轮失败：{exc}")
            nxt = next_mark(now_local(), args.interval)
            wait = (nxt - now_local()).total_seconds()
            print(f"  下次采集 {nxt:%H:%M}（{wait/60:.1f} 分钟后）\n")
            time.sleep(max(5, wait))
    except KeyboardInterrupt:
        print("\n  已停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())

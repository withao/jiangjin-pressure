#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
钓二嘴（长江）水位采集
=========================================================
数据源：重庆市水文监测总站  https://cqsw.slj.cq.gov.cn/hydrology/v2/
        POST /hydrologyapi/stRiverR/getRiver   （返回全部河道站最新值）
        站点：钓二嘴 stcd=60105000（大渡口区，长江），每小时整点更新

**这个接口只给「最新值」，没有历史序列**，所以必须自己按天存档：
    data/level/YYYY-MM-DD.csv     time,z
每次运行取一次，按时间戳去重；同一小时内重复运行不会产生新行。

用法
---------------------------------------------------------
  python level.py                 # 抓一次并落盘，打印当前水位
  python level.py --show          # 只打印已有档案的统计，不联网
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEVEL_DIR = os.path.join(BASE_DIR, "data", "level")

API_URL = "https://cqsw.slj.cq.gov.cn/hydrologyapi/stRiverR/getRiver"
STCD = "60105000"
ST_NAME = "钓二嘴"
RIVER = "长江"
DISTRICT = "大渡口区"
TIMEOUT = 30
RETRIES = 3


# ----------------------------------------------------------------------------
# 抓数
# ----------------------------------------------------------------------------
def fetch_latest() -> Dict:
    """返回 {'t': datetime, 'z': float, 'wptn': str, 'usfl': str}。"""
    body = urllib.parse.urlencode({"num": 1, "size": 2000, "v": 2}).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://cqsw.slj.cq.gov.cn/hydrology/v2/",
        "User-Agent": "Mozilla/5.0 (compatible; level-collector/1.0)",
        "Accept": "application/json, text/plain, */*",
    }

    last: Optional[Exception] = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            rows = ((payload.get("data") or {}).get("pageinfo")) or []
            for r in rows:
                if str(r.get("stcd")) == STCD:
                    z = r.get("z")
                    if z is None:
                        raise ValueError("接口返回的 z 为空")
                    return {
                        "t": datetime.strptime(r["tm"], "%Y-%m-%d %H:%M:%S"),
                        "z": float(z),
                        "wptn": r.get("wptn"),
                        "usfl": r.get("usfl"),
                    }
            raise ValueError(f"返回的 {len(rows)} 个站里没有 {STCD}")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < RETRIES:
                print(f"  ! 水位第 {attempt} 次失败（{exc}），{2*attempt}s 后重试…")
                time.sleep(2 * attempt)
    raise RuntimeError(f"水位抓取失败（已重试 {RETRIES} 次）：{last}") from last


# ----------------------------------------------------------------------------
# 存档
# ----------------------------------------------------------------------------
def day_path(root: str, t: datetime) -> str:
    return os.path.join(root, t.strftime("%Y-%m-%d") + ".csv")


def load_all(root: str = LEVEL_DIR, days: int = 0) -> List[Dict]:
    """读取全部按天存档；days>0 时只取最近 days 天。"""
    if not os.path.isdir(root):
        return []
    files = sorted(f for f in os.listdir(root) if f.endswith(".csv"))
    if days > 0:
        files = files[-days:]
    out: List[Dict] = []
    for fn in files:
        with open(os.path.join(root, fn), "r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    t = datetime.fromisoformat(row["time"])
                    z = float(row["z"])
                except (KeyError, ValueError):
                    continue
                out.append({"t": t, "z": z})
    out.sort(key=lambda r: r["t"])
    return out


def save(root: str, row: Dict) -> bool:
    """按天写入，按时间戳去重。返回是否真的新增了一行。"""
    os.makedirs(root, exist_ok=True)
    path = day_path(root, row["t"])
    rows: List[Dict] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append({"t": datetime.fromisoformat(r["time"]),
                                 "z": float(r["z"])})
                except (KeyError, ValueError):
                    continue
    if any(r["t"] == row["t"] for r in rows):
        return False
    rows.append({"t": row["t"], "z": row["z"]})
    rows.sort(key=lambda r: r["t"])
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "z"])
        for r in rows:
            w.writerow([r["t"].strftime("%Y-%m-%d %H:%M"), f"{r['z']:.3f}"])
    return True


def sync(root: str = LEVEL_DIR, quiet: bool = False) -> Optional[Dict]:
    """抓一次 → 落盘 → 返回最新记录；失败返回 None（不抛，页面照常出）。"""
    try:
        row = fetch_latest()
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            print(f"  × 水位：{exc}")
        return None
    added = save(root, row)
    if not quiet:
        print(f"  水位 {ST_NAME} {row['z']:.2f} m @ {row['t']:%m-%d %H:%M}"
              + ("（新增）" if added else "（已存在）"))
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"抓取{ST_NAME}（{RIVER}）水位并归档")
    ap.add_argument("--dir", default=LEVEL_DIR, help="存档目录")
    ap.add_argument("--show", action="store_true", help="不联网，只看已有档案")
    args = ap.parse_args(argv)

    if args.show:
        rows = load_all(args.dir)
        if not rows:
            print("  档案为空")
            return 1
        zs = [r["z"] for r in rows]
        print(f"  {len(rows)} 点 · {rows[0]['t']:%m-%d %H:%M} → {rows[-1]['t']:%m-%d %H:%M}")
        print(f"  水位 {rows[-1]['z']:.2f} m | 区间 {min(zs):.2f}–{max(zs):.2f} | 变幅 {max(zs)-min(zs):.2f} m")
        return 0

    row = sync(args.dir)
    return 0 if row else 2


if __name__ == "__main__":
    raise SystemExit(main())

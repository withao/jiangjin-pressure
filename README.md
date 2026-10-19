# 江津区气压曲线

抓取重庆市江津区（29.2833°N, 106.2667°E）气压，按 **15 分钟**间隔采样并绘制曲线。

- 数据源：Open-Meteo 公开接口，**免 API Key**
- 原生分辨率就是 15 分钟（`surface_pressure` / `pressure_msl`），**直接对齐、不做插值**
- 纯标准库实现，`python pressure.py` 直接可跑，**不需要 pip install**
- 云端自动跑：**GitHub Actions 每 15 分钟一次 + GitHub Pages 托管**，见 [`GITHUB_SETUP.md`](GITHUB_SETUP.md)

## 快速开始

```bash
python pressure.py                 # 抓 72h，页面默认看 24h
python pressure.py --view 72       # 页面默认看 72h
python pressure.py --view 6        # 页面默认看 6h
python pressure.py --fetch-hours 2 # 只抓最近 2 小时（有档案累积时用，省流量）
python pressure.py --watch         # 常驻：每 15 分钟抓一次、刷新图
python pressure.py --plot-only     # 不联网，用已有 CSV 重画
python pressure.py --var m         # 改看海平面气压
python pressure.py --png           # 额外输出 PNG（需 matplotlib）
```

页面上有 **6 / 24 / 72 小时** 三个切换按钮：数据一次性嵌进 HTML，切换不联网。

## 长江水位（钓二嘴）

页面下半部分是 **钓二嘴（长江，大渡口区，站号 60105000）** 的水位曲线，数据来自
[重庆市水文监测总站](https://cqsw.slj.cq.gov.cn/hydrology/v2/)。

```bash
python level.py            # 抓一次并落盘
python level.py --show     # 只看已有档案
```

- 接口 `POST /hydrologyapi/stRiverR/getRiver` 返回**全部 398 个河道站的最新值**，**没有历史序列**；
- 所以按天存档：`data/level/YYYY-MM-DD.csv`，每小时一行，按时间戳去重；
- 站点整点更新，同一小时内重复运行不会产生新行；
- 云端由 workflow 把新增行提交回仓库（`[skip ci]` + `paths-ignore: data/**`，不会自触发）。

Windows 双击：`run_once.bat`（抓一次）/ `run_watch.bat`（常驻）。

> ⚠️ **改 `.bat` 时必须保持「纯 ASCII + CRLF」**。cmd 在 `chcp` 生效前就按系统 OEM 代码页（简中为 936/GBK）逐字节解析批处理文件，UTF-8 中文会被拆成乱码，进而把 `if (...)` 块、`echo` 行解析崩掉——症状是 `'cho' 不是内部或外部命令`、`can't open file '...\('`。日志文案请写在 Python 里，`.bat` 只留英文。

## 云端自动跑（推荐）

不用开电脑、不用买服务器：**GitHub Actions 每 15 分钟跑一次，GitHub Pages 托管页面**。

- 工作流：`.github/workflows/pressure.yml`
- 完整步骤（建仓 → Pages → 验证 → 排错）：**[`GITHUB_SETUP.md`](GITHUB_SETUP.md)**
- 关键点：**不往仓库提交任何文件**（用 Pages artifact 部署），所以不会产生 96 次/天的提交历史


## 产物

| 文件 | 说明 |
|---|---|
| `data/pressure.csv` | 时间序列，追加写入并按时间戳去重 |
| `output/pressure.html` | 交互式曲线（悬停看逐点值、可切本站/海平面气压） |
| `output/pressure.png` | 静态图，仅 `--png` 且装了 matplotlib 时生成 |

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--hours` | 72 | **抓取并嵌入**的数据深度（小时），接口上限 72 |
| `--view` | 24 | **页面默认显示**窗口；可切 6/24/72 |
| `--fetch-hours` | 同 `--hours` | **每次联网抓取**回溯小时数；有本地档案累积时可设小值 |
| `--interval` | 15 | 采样间隔（分钟）；数据源原生 15，改别的值等于插值 |
| `--var` | s | `s` 本站气压 / `m` 海平面气压 |
| `--watch` | 关 | 常驻模式，对齐到 :00/:15/:30/:45 采集 |
| `--refresh` | 常驻 300，否则 0 | HTML 自动刷新秒数 |
| `--csv` / `--html` | data/ · output/ | 自定义输出路径 |

卡片里的 **3 小时变压**看“正在怎么变”，**24 小时变压**看“变了多少”——后者更接近一次天气过程对鱼的实际影响。

## 两种气压别混用

- **本站气压（surface_pressure）**：随海拔变化。江津城区海拔约 250–270 m，所以只有 **约 980–990 hPa**，这是对的。
- **海平面气压（pressure_msl）**：折算到海平面，**约 1010 hPa**，才能和天气预报、天气图上的数值对比。

## 3 小时变压

曲线卡片里的「3 小时变压」是天气系统移动的常用判据：

- ≥ **+1.0 hPa** → 升压（高压系统移入，天气多转好）
- ≤ **−1.0 hPa** → 降压（低压/锋面接近，易转阴雨大风）
- 中间 → 少变

## 说明

- 数据为气象模型输出（非本站实测站数据），趋势可信，绝对值有 1 hPa 量级误差。
- 接口限制：`past_minutely_15` 最多约 3 天，长期记录靠 `--watch` 或云端定时任务持续累积。
- 想本地定时跑，可用「任务计划程序」每 15 分钟执行一次
  `python pressure.py --hours 1`（无需常驻进程，同样会累积进 CSV）。但**云端方案更省事**，见 `GITHUB_SETUP.md`。
- `.venv/`（约 140 MB）**只为生成 PNG** 而装，删掉不影响 HTML 与采集；此时 `--png` 会自动跳过并提示。

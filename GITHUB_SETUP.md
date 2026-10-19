# 用 GitHub 自动跑（每 15 分钟）· 部署说明

目标：不买服务器、不备案、不要个人网站，靠 **GitHub Actions 定时执行 + GitHub Pages 托管**，得到一个永远在线、每 15 分钟自动更新的气压页面。

最终地址形如：`https://withao.github.io/jiangjin-pressure/`

> **当前状态（2026-10-19 22:50）**：已部署成功，页面可访问，workflow 全绿。
> 仓库 `withao/jiangjin-pressure`（public），Pages 源 = GitHub Actions，已加 `push` 触发。
> 只画气压。水位模块已整块删除（抓不到，见 §6）。

---

## 0. 三个先说清楚的结论

| 问题 | 结论 |
|---|---|
| 需要代理吗？ | **云端不需要**。Actions 跑在 GitHub 的境外机器上，直连 `api.open-meteo.com`。你本地的 7897 只在 push 时可能用得上——实测这台机器 `github.com:22` 直连可通，SSH 已验证 `Hi withao!`，**连本地代理都不用配**。 |
| 会把仓库写脏吗？ | **云端一个字都不写**。HTML 是运行时生成、走 Pages artifact 部署，仓库里只有代码。 |
| 水位还画吗？ | **不画了**。重庆水文只对境内开放，境外机器连过去是 `[Errno 101] Network is unreachable`，`level.py` 与页面下半部分已删。 |
| 15 分钟准吗？ | **不准，但够用**。GitHub 免费 runner 的 cron 高峰期会延迟 3～15 分钟，偶尔跳过一轮。数据源本身就是 15 分钟模型值，延迟一轮不影响看趋势。 |

---

## 1. 建仓库

GitHub 右上角 **+ → New repository**：

- Repository name：`jiangjin-pressure`（可换，后面地址跟着变）
- **Public** ← 必须
- 不要勾 Add a README / .gitignore / license（本地已有）

> **为什么必须 Public**：私有仓的 Actions 免费额度是 **2000 分钟/月**。96 次/天 × 30 天 ≈ 2880 分钟（每次按 1 分钟起算），必然超。公开仓用标准 runner **不计费**。天气数据也不敏感。

---

## 2. 本地初始化并推送

```bash
cd /d/ai_gen/jiangjin_pressure
git init -b main
git add .
git commit -m "init: 江津气压 15 分钟采集 + Pages 自动发布"
git remote add origin git@github.com:withao/jiangjin-pressure.git
git push -u origin main
```

SSH 已确认可用（`ssh -T git@github.com` 返回 `Hi withao!`），不需要再配 key 或代理。

如果换过用户名/仓库名，把上面 URL 里的 `withao/jiangjin-pressure` 改掉即可。

---

## 3. 打开 Pages（关键一步）

仓库 → **Settings → Pages** → **Build and deployment** → **Source** 选 **`GitHub Actions`**。

⚠️ 不要选「Deploy from a branch」——本项目的 HTML 是运行时生成、不落仓库的，选分支会一直是 404。

---

## 4. 手动跑一次验证

仓库 → **Actions** → 左侧 **pressure** → 右侧 **Run workflow** → **Run workflow**。

约 20～40 秒后：

- 出现绿勾 → 打开 `https://withao.github.io/jiangjin-pressure/`
- 出现红叉 → 点进去看是哪一步（`Fetch and render` 多半是网络/接口，`deploy` 多半是第 3 步没设对）

首次发布后 Pages 生效可能需要 1～2 分钟。

---

## 5. 之后就是全自动

工作流 `.github/workflows/pressure.yml`：

```yaml
on:
  schedule:
    - cron: "*/15 * * * *"
  push:
    branches: [main]
  workflow_dispatch:
```

> `push` 这一条是为了「改完代码立刻跑一轮」，不用等 cron 的整刻。**注意**：它只在 `main` 上生效。

每轮做的事：抓最近 **72h** 气压（15 分钟原生分辨率，288 个点）→ 渲染单文件 HTML（可切 6/24/72h，默认 24h）→ 上传 Pages artifact → 部署。

页面上的「生成于」用的是**东八区时间**，不会因为 runner 是 UTC 而显示成早 8 小时。

---

## 6. 为什么不用「提交回仓库」的方案

常见写法是 `git commit && git push` 把 CSV / HTML 提交回仓库。我算过账，这个项目**不能这么干**：

- 每 15 分钟一次 = **96 次/天 = 3.5 万次/年**提交；
- 每次提交都要存一份**完整的 CSV**（git 存快照，不是增量）；
- CSV 一年长到约 1.4 MB，按平均值 0.7 MB 估：3.5 万 × 0.7 MB ≈ **24 GB** 的 git 历史。

所以气压改成 **artifact 部署**：仓库里不存气压数据，每次现抓现渲染。

**代价**：Pages 上只有「最近 72 小时」（接口上限）。想留更长的气压档案，只能另外做归档任务。

### 水位为什么整块删了

原本想做第二张图，卡在两件事上：

1. 重庆市水文总站的接口**只返回各站「当前值」，没有历史序列**（`getRiverByCz` 那条历史接口实测一律 500），必须自己存档；
2. 更要命的是**只对境内开放**。实测云端报错：

```
RuntimeError: 水位抓取失败（已重试 3 次）：
<urlopen error [Errno 101] Network is unreachable>
```

`[Errno 101]` 是**连接阶段就被挡回**（不是超时、不是证书问题），目标 `203.93.111.203` 从 GitHub 的境外机器完全不可达。本机同一份代码一次就成功，所以不是代码问题。

能绕的办法（本机采集 + push、云端只读）需要一台天天开机的电脑，且曲线会断续。既然重点是**气压变化本身**，就直接删了：`level.py`、`data/level/`、页面下半部分、`--level-*` 参数全部移除。仓库里不再有任何需要写的目录，workflow 的 `contents: write` 也一并去掉。

---

## 7. 想改的地方

| 想改 | 改哪里 |
|---|---|
| 地点 | `pressure.py` 顶部 `LAT, LON, PLACE` |
| 默认显示窗口 | workflow 里 `--view 24` → 改 6 或 72（数据深度由 `--hours` 控制，已是上限 72） |
| 采样间隔 | workflow 里 `--interval 15`（数据源原生就是 15，改成别的值等于插值造假数据） |
| 默认看海平面气压 | workflow 里加 `--var m` |
| 停止自动跑 | Actions → pressure → 右上 `...` → **Disable workflow** |

---

## 8. 排错

| 症状 | 原因 / 处理 |
|---|---|
| Pages 404 | 第 3 步 Source 没选 `GitHub Actions`；或首次部署还没生效（等 1～2 分钟） |
| `Get Pages site failed` | 同上，Pages 未启用 |
| 曲线是空的 | `Sanity check` 会直接报错并中止，不会发布空图；看 `Fetch and render` 日志里的报错 |
| 连续多轮没跑 | GitHub 对高频 cron 会限流；手动 Run 一次即可，或把频率降到 `*/30` |
| 60 天后自动停 | 只在仓库**长期无任何活动**时发生；本项目每天都有 workflow 运行，不会触发 |

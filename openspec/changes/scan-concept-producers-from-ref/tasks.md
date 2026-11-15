## 1. 扫描改成读 ref

- [ ] 1.1 `--scripts` / `--refs` 接受 `<repo>:<ref>:<subdir>`；枚举用
  `git ls-tree -r --name-only <ref> -- <subdir>`，取内容用 `git show <ref>:<path>`。
  `<repo>` 解析成工作区里的仓目录（`git -C <repo>`），**只用来找 .git，不读文件**。
  验证：把本地工作树切到一个落后 98 笔的提交，跑出的 `写侧命中` 与 `.py` 计数
  与在 `gitee/main` 上跑**完全相同**。
- [ ] 1.2 `scope` 段改记 `{repo, ref, commit, subdir, py}` 四元组 + 总数；
  `format_split()` 印出的口径行带上「读的是哪个 ref@commit」。
  验证：声明文件 `scope` 里能读到 sha（40 位或短 sha）且与
  `git rev-parse <ref>` 相等。
- [ ] 1.3 ref 取不到 ⇒ 退 2，stderr 点名是哪棵树、缺什么。
  验证：`--scripts no-such-repo:gitee/main:scripts` ⇒ EXIT=2，且**不写**输出文件。

## 2. 补登 40 条（`semantic-extract-daily` 那批）

- [ ] 2.1 40 条写入 `concept-producers.json`：`kind: bulk_etl`、
  `producer: fd-industry-data/scripts/extract_<x>.py`、
  `schedule: cronjob fd-prod/semantic-extract-daily 20 5 * * * Asia/Shanghai`、
  `last_verified: 2026-11-15`。按脚本归组：
  `extract_ncd.py` 17（`chinamoney-ncd-curve`）、`extract_rates.py` 8
  （`chinamoney-shibor`）、`extract_weather_daily.py` 6
  （`elexon-bmrs`/`noaa-ncei`/`open-meteo` 三个源共用一份脚本）、
  `extract_eastmoney.py` 2、`extract_lpr.py` 2、`extract_nasa_power.py` 2、
  `extract_uk_hpi.py` 2、`extract_usgs_nwis.py` 1。
  验证：`len(producers) = 501`、`bulk_etl` 条数 **137**、`undecided` **364**。
- [ ] 2.2 每条 `evidence` 写三条互相独立的证据（照上一个 change 的纪律）：
  ① 代码级出现在**部署版脚本**里的位置（ConfigMap 与 `gitee/main` 逐字节相等，
  所以仓内行号即部署行号）；② 写入方自己的解析能过——`_semextract.ensure_concepts()`
  按 `(code, zh, en)` 建概念，解析不到直接 `SystemExit`；
  ③ 库内 `source_used` 等于该脚本的 `SOURCE` 常量。
  验证：40 条全部三关都过，且抽 3 条人工复核。
- [ ] 2.3 **把「固定历史数据集」这一类写清楚**，别让下一个人把它当停摆：
  `extract_nasa_power.py`（乌鲁木齐 1984 整年）与 `extract_usgs_nwis.py`
  （站点 01646500 的 2020 整年）的产物**永远停在 1984/2020 是设计**，
  两份脚本的 docstring 各自写明「MERRA-2/NWIS finalized、不存在更新的行」。
  验证：这两条 4 个概念（`meteo.precip.daily`/`meteo.t2m.daily`/
  `hydro.usgs.discharge.01646500` 及同批）的 `evidence` 里带这句。

## 3. 基线重种与线上读数

- [ ] 3.1 `--write-baseline` 重种：`dormant` **404 → 364**。
  验证：重种后带 `--baseline` 实跑 `新增 0 / 已恢复 0（快照内共 364 条）`，
  且差集是「少 40、多 0」。
- [ ] 3.2 真库实跑一次，读数从 `非爬取产出 97 / 停摆 404` 变成
  **`非爬取产出 137 / 停摆 364`**。
  验证：两个数字同屏，且 `etl_fed` 行逐条打印 `producer` 与 `schedule`
  （含新加的 `semantic-extract-daily`）。

## 4. 闸自证与回归

- [ ] 4.1 `--self-test` 补两臂（成对）：
  「工作树落后但 ref 正确 ⇒ 结果不变」＋「ref 不存在 ⇒ 退 2 且不写文件」。
  验证：把 ref 参数改成不存在的名字，第二条当场转红；把工作树 checkout 成旧提交，
  第一条保持绿。
- [ ] 4.2 `--check` 在**落后工作树**上仍然 RC=0（这是本次要买到的东西）。
  验证：在同一棵落后 98 笔的树上跑 `--check`，输出 `✓ 与仓内一致`。

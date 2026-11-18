## 1. 扫描改成读 ref

- [x] 1.1 `--scripts` / `--refs` 接受 `<repo>:<ref>:<subdir>`；枚举用
  `git ls-tree -r --name-only <ref> -- <subdir>`，取内容用一次
  `git cat-file --batch`（按文件起 624 次 `git show` 太慢）。
  **不提供「退回工作树」的兼容档**——能退回去，结果就又取决于谁检出了哪一版。
  验证：自证臂「工作树被改脏也不影响结果」——把夹具里两个脚本正文改成
  `SERIES = []` 后再扫，结果与改动前**完全相同**；红证：把 `_iter_py` 换回
  `rglob` 工作树，该臂当场转 ✗ 并打印 `({}, …)`（owners 空）。
- [x] 1.2 `scope` 改记 `{script_trees, ref_trees, commits, py_files}`，树声明是
  可移植的 `<仓>:<ref>:<子目录>`（不含绝对路径），`commits` 是 `仓@ref → sha`。
  打印新增一行 `读的是提交：fd-industry-data:gitee/main@5cd14533、…`。
  验证：声明文件 `scope.commits` 的 sha 与 `git rev-parse gitee/main` 相等；
  实跑输出确实印出那一行（三棵树三个 sha）。
  **判据与来源分开**：sha 参与记录、**不参与 `--check` 比较**——它每来一笔上游
  提交就变，算进判据就成了常年红的闸。`--check` 只比语义内容，sha 有漂移时打
  `ℹ 内容未变，但上游提交已前进：记录 … → 当前 …`。
  验证：本次实跑就命中该情形（`fd-infra-deploy` 从 `f2057be4` 前进到
  `db23ee80`），`--check` 仍 **RC=0**。
- [x] 1.3 ref 取不到 ⇒ 退 2，stderr 点名是哪棵树、缺什么。
  验证：自证臂三路——`ref 不存在`／`给工作树路径`（报「不是三段式」）／
  `路径下没有 .py`（报「空扫描＝假绿」），三路都抛 `RefError`；
  主流程里任何 `RefError` 一律 `判不出（EXIT=2）`。

## 2. 补登 40 条（`semantic-extract-daily` 那批）

- [x] 2.1 40 条写入：`kind: bulk_etl`、`producer: fd-industry-data/scripts/extract_<x>.py`、
  `schedule: cronjob fd-prod/semantic-extract-daily 20 5 * * * Asia/Shanghai`、
  `last_verified: 2026-11-15`。按脚本归组（实测）：`extract_ncd.py` 17、
  `extract_rates.py` 8、`extract_weather_daily.py` 6（三个子源共用一份脚本）、
  `extract_eastmoney.py` 2、`extract_lpr.py` 2、`extract_nasa_power.py` 2、
  `extract_uk_hpi.py` 2、`extract_usgs_nwis.py` 1。
  验证：`len(producers) = 501`、`bulk_etl` **137**、`undecided` **364**；
  且 `undecided` 集合与重种后的 `dormant` 快照**逐个相等**（差集 0）。
- [x] 2.2 每条 `evidence` 三条互相独立的证据：
  ① 代码级出现——`gitee/main` 那份脚本里的行号（与线上 ConfigMap **逐字节相等**，
  所以仓内行号即部署行号）；② 写入方自己的解析能过——`_semextract.ensure_concepts()`
  按 `(code, zh, en)` 建并解析，解析不到直接 `SystemExit`；
  ③ 库内 `source_used` == 该脚本的 `SOURCE` 常量。
  验证：③ **对全部 40 条机器核对**，不是抽查——按 `SOURCE` 归组计数与库里
  `source_used` 归组计数逐源相等：`chinamoney-ncd-curve` 17／`chinamoney-shibor` 8／
  `noaa-ncei` 3／`open-meteo` 2／`nasa-power` 2／`eastmoney-data` 2／`uk-hpi` 2／
  `chinamoney-lpr` 2／`elexon-bmrs` 1／`usgs-nwis` 1 = 40。
- [x] 2.3 **「固定历史数据集」写进 evidence**，免得下一个人当停摆：
  `extract_nasa_power.py`（乌鲁木齐 1984 整年 MERRA-2 finalized）与
  `extract_usgs_nwis.py`（站点 01646500 的 2020 整年）——两份脚本 docstring
  各自明写「不存在更新的行，全停在 1984/2020 是设计」。
  验证：这 4 条（`meteo.precip.daily`／`meteo.t2m.daily`／
  `hydro.usgs.discharge.01646500` ＋）的 evidence 里带这句。

## 3. 基线重种与线上读数

- [x] 3.1 `--write-baseline` 重种：`dormant` **404 → 364**。
  验证：重种后带 `--baseline` 实跑 `新增停摆 0 条 / 已恢复 0 条（快照内共 364 条）`；
  差集**少 40、多 0**（少掉的第一批就是那 40 条，如
  `hydro.usgs.discharge.01646500`／`meteo.city.precip_sum.daily`）。
- [x] 3.2 真库实跑：`非爬取产出 97 → 137`、`停摆 404 → 364`。
  验证：`在用概念 1024：可路由 274 / 非爬取产出 137 / 停摆 364 / 从未供数 249`
  （**274+137+364+249 = 1024 ✓**），且 `etl_fed` 行逐条打印 producer + schedule，
  例如 `price.cpi.index.monthly: 产出链 …/extract_eastmoney.py（排期 cronjob
  fd-prod/semantic-extract-daily 20 5 * * * Asia/Shanghai）`。

## 4. 闸自证与回归

- [x] 4.1 `--self-test` 13 条（新增 4 条，其中 3 条是「取不到」三路 + 1 条
  「工作树改脏不影响」）。红证两条：把 `_iter_py` 换回工作树 ⇒
  「工作树被改脏也不影响结果」转 ✗；把 `.py` 空扫描改回「返回空」⇒
  「路径下没有 .py ⇒ RefError」转 ✗。
- [x] 4.2 `--check` 在落后工作树上仍 RC=0（本次要买到的东西）。
  验证：本机 `fd-industry-data` 工作树落后 `gitee/main` **98 笔**，用 ref 参数跑
  `--check` 得 `✓ 与仓内一致`、RC=0；同一棵树上按老办法（工作树路径）会得
  `✗ 需要重生成`。闸自身自证 `check_concept_routability.py --self-test` 20 条
  仍全绿（本次没改它）。

## 5. 订正记录（本次排查中我自己犯的两个误判，写下来防复发）

- [x] 5.1 排查时先报过「9 个声明源文件不存在」「部署版比仓内多两笔修复」——**都错**。
  真因是拿**检出工作树**当真相：那棵树落后 98 笔，`_semextract.py` 与 8 个抽取器
  都在 `gitee/main` 里、且与线上 ConfigMap 逐字节相等。
  验证：按 ref 逐个 `git cat-file -e gitee/main:scripts/<f>` 全部存在，大小与
  CM 相等（`extract_batch1.py` 22146／`extract_batch2.py` 49009）。
  这正是本 change 要根治的同一个病，故并列在此。

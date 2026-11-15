## 1. 声明面（数据文件）

- [ ] 1.1 生成 `fd-infra-deploy/tools/data/concept-producers.json` 的**骨架**：
  从基线里 501 条 `dormant` 出发，把 98 条「写侧脚本命中」的写成候选条目
  （`kind: bulk_etl`、`producer` 填命中的脚本路径、`schedule: unknown`、
  `last_verified` 留空 ⇒ 视为未复核），其余 403 条写 `kind: undecided`。
  验证：`python3 -c "import json;d=json.load(open(...));print(len(d['producers']))"` = 501。
- [ ] 1.2 逐族复核并填 `schedule` / `last_verified` / `evidence`：先做 5 个最大的族
  （行业经营 113、农业 79、电力与碳 59、利率 29、城乡建设 29）。
  **复核判据**：脚本里真有写路径（INSERT/upsert 到 `semantic_observations`），
  不是只在注释或告警清单里出现。验证：每条 `evidence` 写出文件名 + 行号。
- [ ] 1.3 `schedule` 必须写**实际排期**：CronJob 看 `spec.schedule`、宿主 cron 看
  crontab、都没有写 `manual`。验证：任取 5 条，`kubectl get cronjob` / `crontab -l`
  能对上；对不上就是抄错了。

## 2. 闸读声明（四档）

- [ ] 2.1 `check_concept_routability.py` 新增 `--producers <file>`（缺省取仓内
  `tools/data/concept-producers.json`，读不到 ⇒ **退 2**，不许静默当「没有声明」）。
  验证：`--producers /nonexistent` ⇒ EXIT=2 且 stderr 说明读不到。
- [ ] 2.2 `classify()` 从三档扩到四档：`etl_fed` 仅在声明存在且未过期时成立；
  `etl_fed` **不计命中**但必须打印 `producer` 与 `schedule`。
  验证：夹具里一条 `kind: bulk_etl` + `last_verified: today` ⇒ 判 `etl_fed`、退出码不受影响。
- [ ] 2.3 过期降级：`last_verified` 超过 `STALE_DAYS`（默认 120，可用
  `--stale-days` 覆盖）⇒ 回 `dormant` 并单列一行「声明过期，需复核」。
  验证：夹具 `last_verified: 2026-01-01` ⇒ 该条判 `dormant` 且打印过期原因。
- [ ] 2.4 `--self-test` 补 4 条：`etl_fed` 生效／过期降级／`undecided` 仍算
  `dormant`／声明文件读不到 ⇒ 退 2。验证：`--self-test` 全绿且条数从 8 变 12。

## 3. 输出口径（上限/下限必须同屏）

- [ ] 3.1 打印三层拆分（写侧命中 / 仅被闸引用 / 无字面量）**并与分档数同屏**，
  且同屏写明「第一档是上限、第三档是下限」——口径与数字分两处打，读者只会记住数字。
  验证：实跑输出里三档数字 + 上下限说明同时出现。
- [ ] 3.2 基线重算：声明生效后 `dormant` 会少于 501，用 `--write-baseline` 重种并在
  提交信息里逐条说明 delta（哪一族因哪份声明而退出）。验证：重种后 `--baseline`
  实跑 `新增 0 / 已恢复 N`，`N` 与提交信息里的条目数一致。

## 4. 两个具体族的处置（不在本 change 内改库，只出判据与待办）

- [ ] 4.1 **8 条宏观**（6/88/89/90/91/92/227/232）：在提案里记明它需要三件——
  取回 `feat/datacommons-datasource` 的 `bulk_ingest_country_macro.py`、在
  `fd-ashare` 注册 3 个函数 + 补 `非制造业-指数` 列、补 `concept_bindings`。
  验证：这三件在 tasks 里各留一条「未做」并以 `blocked:` 标注外部依赖。
- [ ] 4.2 **农业族 79 条**（`extract_batch2.py` 那类 additive 回填）：写清「复跑插 0 行」
  意味着**它不是增量刷新**，要判它是否够格当 `bulk_etl` 产出者。
  验证：结论落进 `evidence`，并在提案的 Impact 里点名。

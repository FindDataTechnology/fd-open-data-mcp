# 产出者扫描要读提交 ref，不读工作树

## Why

上一个 change（`2026-11-15-declare-non-crawl-concept-producers`）把
`fd-infra-deploy/tools/gen_concept_producers.py` 落地时，`--scripts` 收的是
**工作树路径**。而本机 `fd-industry-data` 的工作树停在 `main` 上、**比
`gitee/main` 落后 98 笔**——于是那次扫描只看了 534 个 `.py`，得出的
`写侧命中 97 / 404 undecided` 是在陈旧输入上算的。

按 `gitee/main` 的树重算（624 个 `.py`）：

```
写侧命中 40（404 里还剩的） / 只被闸·测试·夹具命名 97 / 无字面量 267
```

`40 + 97 + 267 = 404` ✓。加上已复核的 97，全量口径是
**137 写侧 / 97 只被闸命名 / 267 无字面量 = 501** ✓（与旧口径的差就是
**40 条从「无字面量」挪进了「写侧命中」**）。

那 40 条不是理论命中：它们由 **`fd-prod/semantic-extract-daily`
（`20 5 * * *` Asia/Shanghai）每天在跑**的 8 个抽取器产出，2026-11-14 那一轮
每条都插了新行。而它们在声明文件里仍是 `undecided` ⇒ 闸把它们判成 `dormant`
⇒ **爬取规划永远排不上，而它们其实天天有数据**。这正是上一个 change 要消除的
那种「静默停摆」，只是被一道读错树的扫描留在了外面。

根因一句话：**「谁在被写」是关于提交的事实，不是关于某人手上检出的事实。**
本工作区已有先例——`check_root_copies.py` 的注释写着「正本读 `gitee/main` ref、
绝不读工作树」，理由与这里一模一样。

## What Changes

- **`gen_concept_producers.py` 改为按 ref 扫描**：`--scripts` / `--refs` 接受
  `<仓名>:<ref>:<子目录>` 形式（例：`fd-industry-data:gitee/main:scripts`），
  用 `git ls-tree -r --name-only` 枚举、`git show <ref>:<path>` 取内容，
  不再碰工作树。
- **`scope` 段记下「读的是哪个 ref、解析成哪个提交」**（`ref` + `commit` +
  每个树的 `py` 计数），让读数字的人能自己复算；只留绝对路径的话，
  下一个人无法判断那次扫描看的是哪一版。
- **ref 取不到 → 退 2 判不出**，不许退化成「0 条写侧命中」。这条同族于
  `82f0b32`「『0 个声明过的副本』不再报绿」——**0 命中是最危险的成功**。
- **补登那 40 条**（`kind: bulk_etl`，`producer` 指
  `fd-industry-data/scripts/extract_*.py`，`schedule` 写线上读到的
  `cronjob fd-prod/semantic-extract-daily 20 5 * * * Asia/Shanghai`）。
  三条互相独立的证据逐条成立（见 tasks 1.2）。
- **基线重种 404 → 364**，并把 `split.write_side` 从 97 改成 137。

## Impact

- Affected specs: `catalog-enumeration`
- Affected code:
  - `fd-infra-deploy/tools/gen_concept_producers.py`（按 ref 扫描 + `scope` 记 ref/commit + 取不到退 2）
  - `fd-infra-deploy/tools/data/concept-producers.json`（+40 条复核条目，`split` 三档数改为 137/97/267）
  - `fd-infra-deploy/tools/data/concept-routability-baseline.json`（404 → 364）
- 不改数据库、不写生产库；`concept_bindings` 不动。
- **行为变化只在闸的读数上**：`非爬取产出 97 → 137`、`停摆 404 → 364`。
  这不是「放宽判据」——是把 40 条**确实有每日产出者**的概念从「没产出者」
  这一类里挪出去，挪的依据是三条实测证据，不是推断。

## 与「多生产者共写」的关系（刻意不做的事）

同一天同一批 ConfigMap 里还有 `semantic-extract-batch-src`
（`extract_batch1.py` / `extract_batch2.py`），它已被上一个 change 的 97 条覆盖。
本次只补 `semantic-extract-daily` 的 8 个抽取器——**按作业分，不按目录分**：
一个源由哪条 CronJob 跑，`schedule` 就写哪条，否则读者会去查一个不存在的排期。

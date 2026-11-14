# scripts/data — 入湖脚本的输入数据

这几个文件是 `executor=direct` 策略在运行期从 `fd-scripts` ConfigMap 里读的输入：

| 文件 | 谁在用 | 是什么 |
|---|---|---|
| `cn_trade_cal_2026.txt` | `bulk_ingest_astock_daily.py`（`--calendar-file` 的默认目标） | 2026 年 A 股交易日历，一行一个 `YYYY-MM-DD` |
| `universe-all-symbols.txt` | `run_astock_backfill_batches.py`、日增量取池 | 全部待采股票代码，一行一个 |
| `new-series-symbols.txt` | 同上（新开序列的代码子集） | 新序列首批代码 |

## 为什么它们在这里

它们的**唯一**来源曾是 xinru `fd-master` 的 `fd-scripts` ConfigMap（键名与文件名同名）。
2026-11-14 逐仓扫过工作区 30 个仓的 `origin|gitee|github/main` 树，这三个文件
**和 `run_astock_backfill_batches.py` 一样都不在任何仓里**——只有集群上那一份，改了、
错了、丢了都没有痕迹可比。同一族事故见过好几次（广场四个机器面、cheap1 的 strapi
备份脚本），所以按「把无出处的裸副本收进仓」的既有做法抄了一份进来，**内容逐字节未改**
（落仓后 sha256 与线上一致：`8650c2a41ce7` / `60f9cc2a7fa0` / `2a3e05675593`）。

## 怎么用它们的出处

`fd-infra-deploy/tools/check_fd_scripts_configmap.py` 按 `SOURCES` 表把线上
`fd-scripts` 与这里逐字节比。所以：

- **改这里的文件 ＝ 改线上行为的前提**：改完必须同步 ConfigMap（否则闸会红，而且线上还在用旧的那份）；
- 反过来，线上被手改过也会被这道闸点名。以前两个方向都没有任何通道。

数据文件是**输入**不是产出，别在跑批时让脚本回写它们。

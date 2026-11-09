-- 概念新鲜度巡检（只读）。按 concepts.frequency 推算「最近应已结束的报告期」，
-- 与库里 max(date) 对比。2026-11-09 首版 + 同日修正两点：
--   ① 日期格式按 frequency 分派：年/半年/季用 YYYY-MM-DD，yearly/annual 也接受裸 YYYY
--      （库里同时存在 '2025' 与 '2025-12-31' 两种写法，早期实现只认后者会误报"无数据"）；
--   ② 周/时/非日期值（2026-W41 / LATEST / 2023-11 / 2026-11-06T05）一律排除，不让它们污染 max()。
--
-- 用法： psql "$FD_OPEN_DATA_MCP_DATABASE_URL" -f scripts/freshness_check.sql
-- 判读：滞后/无数据 里，先看该概念有没有被 enabled 策略覆盖（第二段查询）——
--       无策略覆盖的"缺口"是尚未接入，不是故障。
-- window_start 用**字面量**（如 2025-10-05）。写 current_date 表达式会让
-- postgres_fdw 拒绝下推，退化成搬运分片全表 4825 万行（EXPLAIN 实证）。
\set window_start 2025-10-05

with local_obs as materialized (
  select concept_id, max(date) as latest
  from semantic_observations
  where date ~ '^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?$'
    and date not like '%-W%'
  group by 1
),
shard_obs as materialized (
  -- MATERIALIZED 必须：不写它，planner 把 GROUP BY 拆回本地，只下推 WHERE，
  -- 仍要搬 487 万行过 tailnet。
  select concept_id, max(date) as latest
  from ft_sem_obs_xinru3
  where date > :'window_start'
  group by 1
),
obs as (
  select concept_id, max(latest) as latest
  from (select * from local_obs union all select * from shard_obs) u
  group by 1
),
exp as (
  select c.id, c.code, c.frequency,
    case c.frequency
      when 'daily'      then (current_date - 1)::text
      when 'hourly'     then current_date::text
      when 'monthly'    then to_char(date_trunc('month', current_date - interval '1 month'), 'YYYY-MM-DD')
      when 'quarter'    then to_char(date_trunc('quarter', current_date) - interval '1 day', 'YYYY-MM-DD')
      when 'quarterly'  then to_char(date_trunc('quarter', current_date) - interval '1 day', 'YYYY-MM-DD')
      when 'semiannual' then to_char(date_trunc('year', current_date)
                                + case when extract(month from current_date) >= 7
                                       then interval '6 month' else interval '0 month' end
                                + interval '1 month' - interval '1 day', 'YYYY-MM-DD')
      when 'yearly'     then (extract(year from current_date)::int - 1)::text
      when 'annual'     then (extract(year from current_date)::int - 1)::text
    end as expect
  from concepts c
),
judged as (
  select e.frequency, e.id, e.code, e.expect, o.latest,
         case
           when o.latest is null then 'no_data'
           -- yearly/annual：库里可能是 '2025' 或 '2025-12-31'，取前 4 位比较
           when e.frequency in ('yearly','annual') then
                case when left(o.latest, 4) >= e.expect then 'ok' else 'stale' end
           else case when o.latest >= e.expect then 'ok' else 'stale' end
         end as verdict
  from exp e left join obs o on o.concept_id = e.id
  where e.expect is not null
)
select frequency,
       count(*) filter (where verdict = 'no_data') as 无数据,
       count(*) filter (where verdict = 'stale')   as 滞后,
       count(*) filter (where verdict = 'ok')      as 达标,
       max(latest) as 库里最新
from judged group by 1
order by 滞后 desc, 无数据 desc, 1;

-- 第二段：只看「被 enabled 策略覆盖、但滞后或无数据」的概念（真正的待办）
with local_obs as materialized (
  select concept_id, max(date) as latest
  from semantic_observations
  where date ~ '^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?$' and date not like '%-W%'
  group by 1
),
shard_obs as materialized (
  select concept_id, max(date) as latest
  from ft_sem_obs_xinru3
  where date > :'window_start'
  group by 1
),
obs as (
  select concept_id, max(latest) as latest
  from (select * from local_obs union all select * from shard_obs) u
  group by 1
),
cov as (
  select p.id as pid, p.name as pname,
         jsonb_array_elements_text(p.concept_ids)::int as cid
  from crawl_policies p
  where p.enabled and p.concept_ids is not null
)
select cov.pid, left(cov.pname, 26) as policy, c.id as concept,
       left(c.code, 34) as code, c.frequency, coalesce(o.latest, '(none)') as latest
from cov
join concepts c on c.id = cov.cid
left join obs o on o.concept_id = c.id
where (c.frequency in ('quarter','quarterly')
         and (o.latest is null
              or o.latest < to_char(date_trunc('quarter', current_date) - interval '1 day', 'YYYY-MM-DD')))
   or (c.frequency in ('yearly','annual')
         and (o.latest is null
              or left(o.latest, 4) < (extract(year from current_date)::int - 1)::text))
   or (c.frequency = 'monthly'
         and (o.latest is null
              or o.latest < to_char(date_trunc('month', current_date - interval '1 month'), 'YYYY-MM-DD')))
   or (c.frequency = 'daily'
         and (o.latest is null or o.latest < (current_date - 1)::text))
order by cov.pid, c.id;

-- ── 第三段：策略出数率（第二道闸门）─────────────────────────────────────────
-- 第一段问"数据停了吗"，这一段问"策略在跑但一行没出吗"。
-- 2026-11-09 的教训：四个 enabled 策略连续 7–8 轮全部 skipped、零产出、零失败，
-- 状态看着都"成功"，只有出数率能揭示它们空转。
-- 判读：轮次≥3 且 出数=0 即空转；跳过占满且 detail 为空的是"跳过原因不可见"
--（0.5.57 起 detail 会写明原因，此列可用来确认镜像是否已更新）。
with w as (
  select policy_id,
         count(*)                                                as runs,
         count(*) filter (where coalesce(rows_new, 0) > 0)        as yields,
         count(*) filter (where status in ('redundant','no_op'))  as skipped,
         count(*) filter (where status = 'failed')                as failed,
         count(*) filter (where status = 'zero_yield')            as zero_yield,
         max(started_at) filter (where coalesce(rows_new,0) > 0)  as last_yield,
         count(*) filter (where status in ('redundant','no_op') and detail is null) as skip_without_reason
  from policy_runs
  where started_at > now() - interval '7 day'
  group by 1
)
select p.id, left(p.name, 28) as policy, p.executor,
       w.runs as 轮次, w.yields as 出数, w.skipped as 跳过,
       w.failed as 失败, w.zero_yield as 零产出,
       w.skip_without_reason as 跳过无原因,
       to_char(w.last_yield, 'MM-DD HH24:MI') as 最后出数
from w join crawl_policies p on p.id = w.policy_id
where p.enabled
  and w.runs >= 3
  and w.yields = 0
order by w.runs desc, p.id;

-- ── 第四段：同策略内的落后者（局部失效）───────────────────────────────────
-- 前三段是整体尺度；这一段专抓「同伴都好了，只有它没好」。
-- 2026-11-09 实例：策略 4 的 8 个概念里 6 个到 2025-12-31，只有
-- co2_emissions / co2.per_capita 为空——整体统计只会把它算进"yearly 128 个滞后"，
-- 看不出这是一处局部失效（源侧标识符过期）。
with local_obs as materialized (
  select concept_id, max(date) as latest
  from semantic_observations
  where date ~ '^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?$' and date not like '%-W%'
  group by 1
),
shard_obs as materialized (
  select concept_id, max(date) as latest
  from ft_sem_obs_xinru3
  where date > :'window_start'
  group by 1
),
obs as (
  select concept_id, max(latest) as latest
  from (select * from local_obs union all select * from shard_obs) u
  group by 1
),
cov as (
  select p.id as pid, p.name as pname,
         jsonb_array_elements_text(p.concept_ids)::int as cid
  from crawl_policies p
  where p.enabled and p.concept_ids is not null
),
joined as (
  select cov.pid, cov.pname, c.id, c.code, c.frequency, o.latest
  from cov join concepts c on c.id = cov.cid
  left join obs o on o.concept_id = c.id
),
bench as (
  select pid, frequency, max(latest) as best, count(*) as members
  from joined group by 1, 2 having count(*) >= 3
)
select j.pid, left(j.pname, 26) as policy, j.id as concept,
       left(j.code, 30) as code, j.frequency,
       coalesce(j.latest, '(none)') as latest,
       b.best as 同伴最好, b.members as 同组数
from joined j
join bench b on b.pid = j.pid and b.frequency = j.frequency
where b.best is not null
  and (j.latest is null or j.latest < b.best)
order by j.pid, j.frequency, j.id;

-- ── 第五段：策略钉住了已废弃的概念（静默死亡）─────────────────────────────
-- 2026-11-10 定位：2026-09-17/19 有人把这批概念标成 deprecated
-- （248-255 / 381 / 383-388 / 1446-1455 / 2111），而 planner 的「废弃即拒绝」
-- 闸门会让这些概念全部进 unroutable → plan_cells=0 → 以 no_op 收盘。
-- 结果是 4 个 enabled 策略每天照跑、零报错、零告警，静默 54 天。
-- 判据只有一条：enabled 策略的 concept_ids 里出现 concepts.deprecated = true。
-- 废弃数 = 概念总数 → 该策略已完全死掉；小于则是部分少抓。
select p.id, left(p.name, 30) as policy, p.entity_type,
       jsonb_array_length(p.concept_ids) as 概念总数,
       count(*) as 废弃数,
       string_agg(c.id::text || ' ' || c.code, ', ' order by c.id) as 废弃概念
from crawl_policies p
cross join lateral jsonb_array_elements_text(p.concept_ids) t(cid)
join concepts c on c.id = t.cid::int
where p.enabled and c.deprecated
group by p.id, p.name, p.entity_type, p.concept_ids
order by count(*) desc, p.id;

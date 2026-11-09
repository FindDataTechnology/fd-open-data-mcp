-- 概念新鲜度巡检（只读）。按 concepts.frequency 推算「最近应已结束的报告期」，
-- 与库里 max(date) 对比。2026-11-09 首版 + 同日修正两点：
--   ① 日期格式按 frequency 分派：年/半年/季用 YYYY-MM-DD，yearly/annual 也接受裸 YYYY
--      （库里同时存在 '2025' 与 '2025-12-31' 两种写法，早期实现只认后者会误报"无数据"）；
--   ② 周/时/非日期值（2026-W41 / LATEST / 2023-11 / 2026-11-06T05）一律排除，不让它们污染 max()。
--
-- 用法： psql "$FD_OPEN_DATA_MCP_DATABASE_URL" -f scripts/freshness_check.sql
-- 判读：滞后/无数据 里，先看该概念有没有被 enabled 策略覆盖（第二段查询）——
--       无策略覆盖的"缺口"是尚未接入，不是故障。
with obs as (
  select concept_id, max(date) as latest
  from semantic_observations
  where date ~ '^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?$'   -- YYYY | YYYY-MM | YYYY-MM-DD
    and date not like '%-W%'
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
with obs as (
  select concept_id, max(date) as latest
  from semantic_observations
  where date ~ '^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?$' and date not like '%-W%'
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

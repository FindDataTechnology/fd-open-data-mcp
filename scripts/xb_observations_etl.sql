-- add-crossborder-vertical-line 2.5 ETL: concepts + semantic_observations
-- 词表真源：fd-open-data-protocol v2026.11（gitee 9195f52 已推送）
-- 幂等：concepts ON CONFLICT DO NOTHING；observations NOT EXISTS 守卫

-- 1) concepts 五元组注册（TradeFlow 5 变体 + FxRate 1 变体）
INSERT INTO concepts (code, name_en, name_zh, category, unit, measure, frequency, entity_type, source, verified, deprecated, concept_code)
SELECT v.code, v.name_en, v.name_zh, 'crossborder', v.unit, v.measure, v.frequency, v.entity_type, v.source, false, false, v.family
FROM (VALUES
  ('trade.flow_value','Trade flow value','贸易流值','usd','export_world','yearly','hs_code','un-comtrade','TradeFlow'),
  ('trade.flow_value','Trade flow value','贸易流值','usd','export_us','yearly','hs_code','un-comtrade','TradeFlow'),
  ('trade.flow_value','Trade flow value','贸易流值','usd','export_de','yearly','hs_code','un-comtrade','TradeFlow'),
  ('trade.flow_value','Trade flow value','贸易流值','usd','export_jp','yearly','hs_code','un-comtrade','TradeFlow'),
  ('trade.flow_value','Trade flow value','贸易流值','usd','export_uk','yearly','hs_code','un-comtrade','TradeFlow'),
  ('fx.rate','FX reference rate','汇率参考价','cny','reference','monthly','currency_pair','ecb-exr','FxRate')
) AS v(code, name_en, name_zh, unit, measure, frequency, entity_type, source, family)
ON CONFLICT DO NOTHING;

-- 2) observations：un-comtrade → TradeFlow（HS6 × 伙伴国 × 年度 FOB/USD）
INSERT INTO semantic_observations (concept_id, entity_type, entity_id, date, granularity, value, unit, source_used, fetched_at)
SELECT DISTINCT ON (c.id, e.id, i.payload->>'ref_year')
       c.id, 'hs_code', e.id,
       i.payload->>'ref_year', 'year',
       i.payload->>'fob_value', 'usd', 'un-comtrade', now()
FROM crawl_items i
JOIN crawl_runs r ON r.id = i.run_id AND r.source = 'un-comtrade'
JOIN entities e ON e.entity_type = 'hs_code' AND e.code = i.payload->>'cmd_code'
JOIN concepts c ON c.code = 'trade.flow_value' AND c.entity_type = 'hs_code'
     AND c.measure = 'export_' || (
       CASE i.payload->>'partner_code'
         WHEN '0' THEN 'world' WHEN '842' THEN 'us' WHEN '276' THEN 'de'
         WHEN '392' THEN 'jp' WHEN '826' THEN 'uk' END)
WHERE i.payload->>'flow_code' = 'X'
  AND i.payload->>'ref_year' IS NOT NULL
  AND i.payload->>'fob_value' IS NOT NULL
  AND c.measure IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM semantic_observations o
    WHERE o.concept_id = c.id AND o.entity_type = 'hs_code' AND o.entity_id = e.id
      AND o.date = i.payload->>'ref_year' AND o.granularity = 'year'
      AND o.source_used = 'un-comtrade');

-- 3) observations：ecb-exr → FxRate（EURCNY 月度参考价，CNY 计价）
INSERT INTO semantic_observations (concept_id, entity_type, entity_id, date, granularity, value, unit, source_used, fetched_at)
SELECT DISTINCT ON (c.id, e.id, i.payload->>'period')
       c.id, 'currency_pair', e.id,
       i.payload->>'period', 'month',
       i.payload->>'value', 'cny', 'ecb-exr', now()
FROM crawl_items i
JOIN crawl_runs r ON r.id = i.run_id AND r.source = 'ecb-exr'
JOIN entities e ON e.entity_type = 'currency_pair' AND e.code = 'EURCNY'
JOIN concepts c ON c.code = 'fx.rate' AND c.entity_type = 'currency_pair' AND c.measure = 'reference'
WHERE i.payload->>'currency' = 'CNY'
  AND i.payload->>'period' IS NOT NULL
  AND i.payload->>'value' IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM semantic_observations o
    WHERE o.concept_id = c.id AND o.entity_type = 'currency_pair' AND o.entity_id = e.id
      AND o.date = i.payload->>'period' AND o.granularity = 'month'
      AND o.source_used = 'ecb-exr')
ORDER BY c.id, e.id, i.payload->>'period', r.created_at DESC;

-- 4) 验收：计数 + 抽样溯源
SELECT 'xb_concepts' AS k, count(*) FROM concepts WHERE category = 'crossborder';
SELECT 'xb_obs_uncomtrade' AS k, count(*), min(date), max(date) FROM semantic_observations WHERE source_used = 'un-comtrade';
SELECT 'xb_obs_ecbexr' AS k, count(*), min(date), max(date) FROM semantic_observations WHERE source_used = 'ecb-exr';
SELECT e.code AS hs, c.measure AS partner, o.date AS yr, o.value AS fob_usd
FROM semantic_observations o
JOIN entities e ON e.id = o.entity_id
JOIN concepts c ON c.id = o.concept_id
WHERE o.source_used = 'un-comtrade' AND e.code = '610910' AND c.measure = 'export_uk'
ORDER BY o.date DESC LIMIT 3;

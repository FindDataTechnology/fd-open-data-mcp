# Spec Delta

## Purpose

把「补数失败要标注」补成「补数失败要能回答为什么」：今天的降级标注只说失败，不说原因，
于是上游全拒与单点偶发在读数上同形。

## MODIFIED Requirements

### Requirement: 补数失败的降级标注

补数尝试失败时，系统 SHALL 返回已覆盖的部分序列并**明确标注缺口**，SHALL NOT 抛异常，SHALL NOT 把不完整序列当作完整序列返回。

响应 SHALL 携带：`gaps`（缺失区间列表）、`fill_attempted`（是否尝试过补数）、`fill_failed`（补数是否失败）。已完整覆盖时 `gaps` 为空数组。

当补数失败时，响应 SHALL additionally 携带一个可判的失败原因，至少区分「上游对该区间全部拒绝」
（例如全 provider 被限流）与「其它错误」，并 SHALL 保留一段可用于诊断的原始错误摘要。MUST NOT
只给一个布尔而不给原因——只有布尔时，运维无法区分「今天上游封了」与「我们代码坏了」。

#### Scenario: 补数失败仍返回已覆盖部分
- **WHEN** 某标的缓存覆盖 2026-01-01→2026-03-31，请求 2025-01-01→2026-10-17 且上游不可达
- **THEN** 返回 2026-01→03 的点，`gaps` 含两段缺失区间，`fill_attempted=true`、`fill_failed=true`，不抛异常

#### Scenario: 无缓存且上游失败时返回空序列与全窗缺口
- **WHEN** 某标的零缓存且上游不可达
- **THEN** 返回空序列、`gaps` 为整个窗口、`fill_failed=true`

#### Scenario: 覆盖完整时 gaps 为空
- **WHEN** 请求窗口已被完整覆盖
- **THEN** `gaps` 为空数组、`fill_attempted=false`

#### Scenario: 失败原因可判
- **WHEN** 补数因为所有上游都被限流而失败
- **THEN** 响应里的失败原因指出「上游全部拒绝」而不是「其它错误」
- **AND THEN** 同时带一段原始错误摘要，供人判读

#### Scenario: 代码缺陷与上游拒绝不同形
- **WHEN** 补数因为本地异常（例如序列化或连接串错误）而失败
- **THEN** 失败原因与「上游全部拒绝」区分开
- **AND THEN** 不把它记成数据缺口以外的含义

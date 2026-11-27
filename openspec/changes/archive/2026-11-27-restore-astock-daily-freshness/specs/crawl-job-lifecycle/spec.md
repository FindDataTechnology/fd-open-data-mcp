# Spec Delta

## Purpose

把「deadline 杀死的 run」从「只知道超时」升级为「知道死在哪」：被杀之前的进度必须已经落盘，
关闭时把进度合成进原因。今天的形态是 `deadline exceeded after 10800s; … pod 已回收`，
运维只能重跑，而重跑正是要避免的。

## MODIFIED Requirements

### Requirement: Deadline-Terminated Runs SHALL Close With a Distinguishable Reason

When a Job is terminated because it exceeded its runtime ceiling, the reconciler SHALL close the corresponding `policy_runs` row as failed and record a detail string identifying a deadline termination, so that operators can distinguish a wedged crawler from a slow upstream source.

The detail SHALL additionally carry the last progress the execution body reported before it was
killed — at minimum the item it was working on and how many it had completed — whenever such
progress exists. Only when no progress was ever recorded may the detail fall back to a pure
"deadline exceeded" string, and that fallback SHALL be marked as reason-opaque so a later gate can
count it separately from a deadline kill that has a location.

The execution body SHALL record that progress on its own, frequently enough that a kill at any
point leaves the most recent item behind, and SHALL record it on receiving a termination signal as
well as after each completed item.

#### Scenario: Polling a deadline-terminated Job

- **WHEN** `poll()` observes a Job whose status conditions include `type=Failed` with `reason=DeadlineExceeded`
- **THEN** it returns a terminal outcome and the associated `policy_runs` row is closed with `detail` containing `deadline exceeded`

#### Scenario: 被杀时带着位置

- **WHEN** 一个 run 在完成第 N 个标的、开始第 N+1 个时被 deadline 杀掉
- **THEN** 关闭时的 `detail` 含有 `deadline exceeded` 与该标的标识
- **AND THEN** 也含有已完成计数（N），使「跑得多慢」可读

#### Scenario: 一点进度都没有才允许不透明

- **WHEN** 一个 run 从未记录过任何进度就被杀掉
- **THEN** `detail` 是纯 deadline 字符串并被标记为 reason-opaque
- **AND THEN** 该标记可被新鲜度闸单独计数，不与「带位置的 deadline」混为一谈

#### Scenario: TTL still reclaims the object afterwards

- **WHEN** a Job has been terminated by its deadline and its run row is closed
- **THEN** `ttlSecondsAfterFinished` (86400 s) continues to be honored, so the Job and its pod are garbage-collected without operator action

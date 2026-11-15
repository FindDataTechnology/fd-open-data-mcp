## ADDED Requirements

### Requirement: The producer scan reads a committed ref, not a working tree

`gen_concept_producers.py` SHALL enumerate and read the write-side and reference
trees from a **git ref** (`<repo>:<ref>:<subdir>`, resolved via `git ls-tree` /
`git show`), never from whatever happens to be checked out in a working tree.
The declaration file's `scope` section SHALL record, for each scanned tree, the
repo, the ref and the **resolved commit sha**, plus the `.py` file count, so that
a reader can reproduce the split without guessing which version was measured.

A ref that cannot be resolved SHALL be a hard failure (`EXIT=2`, "判不出") and
SHALL NOT degrade to "zero write-side hits". Zero hits is the most dangerous
success: it reads as "nothing produces these concepts" while the real cause is
that the scan never saw the code.

#### Scenario: A stale checkout does not change the split

- **WHEN** the local working tree of `fd-industry-data` is behind `gitee/main`
  by any number of commits
- **THEN** the reported three-way split and `.py` count equal the values measured
  from the ref, so the numbers do not depend on which branch someone has checked out

#### Scenario: The split names the ref and the commit it resolved to

- **WHEN** the declaration file is regenerated
- **THEN** its `scope` names each tree as `<repo>:<ref>:<subdir>`, records the
  resolved commit sha and the per-tree `.py` count, and the printed split line
  reports the total `.py` count actually read

#### Scenario: An unresolvable ref fails instead of reporting zero

- **WHEN** a declared tree's ref does not exist (typo, un-fetched remote, wrong repo)
- **THEN** the generator exits `2` with the offending tree named, and does not
  write a declaration file whose split says `写侧命中 0`

#### Scenario: A provenance-only change does not turn the fixed-point check red

- **WHEN** upstream commits advance but the scanned content is unchanged, so the
  recorded `scope.commits` shas no longer match the current refs
- **THEN** the fixed-point check (`--check`) still passes and prints the recorded
  and current commits as a note, because the commit shas are provenance (they
  participate in the record) and not part of the criterion (they must not make
  the check red on every upstream push — a gate that is red every day is ignored)

#### Scenario: Content drift still fails the check

- **WHEN** a script in the scanned ref changes such that the set of concept-code
  literals changes
- **THEN** `--check` reports the declaration file as needing regeneration and
  names the commit drift, so content staleness stays a red condition even though
  provenance staleness is not

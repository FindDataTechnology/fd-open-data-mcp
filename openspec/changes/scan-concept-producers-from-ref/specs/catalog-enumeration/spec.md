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

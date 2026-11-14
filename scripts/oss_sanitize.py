#!/usr/bin/env python3
"""OSS sanitization gate (add-data-stream-engine task 6.4).

A read-only scanner that fails the build when internal assets would ship in the
public repo. Three classes of violation, each reported as file:line:

  1. CREDENTIALS — private keys, secret/token assignments with literal values,
     password-in-URL, cloud access-key ids. A committed secret is already leaked.
  2. INTERNAL TOPOLOGY — deploy manifests / secrets dirs / binary databases
     that belong to the private fleet, never the OSS repo.
  3. INTERNAL HOSTS — private registry hosts, tailnet (100.64.0.0/10) addresses,
     and private service names baked into shipped files. (The public brand site
     finddatatech.cloud is NOT internal and is allowed.)

Exit 0 = clean; exit 1 = violations listed. Run it in CI before publishing and
locally before any commit that touches config. It never writes anything.

False-positive guards: f-string DSN templates (`postgresql://{user}:{pw}@`) and
os.environ/getenv lookups are SAFE (the secret is injected at runtime), and
obvious placeholders (YOUR_..., CHANGE_ME, EXAMPLE) are ignored. The gate's own
test fixtures are excluded by name.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Directories that never ship in the OSS repo.
EXCLUDED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
                 ".pytest_cache", "build", "dist", ".mypy_cache"}
# The gate's own fixtures contain planted fake secrets — exclude them.
# The scanner source is excluded for a structural reason, not a policy one: a
# scrubber's rulebook must *contain* the strings it looks for, so scanning it
# always yields matches for the pattern definitions themselves and for the
# docstring that names the tailnet range (measured 2026-11-14: 3 self-matches,
# which kept `Build Check` red on every commit and buried the 72 real findings
# underneath). Excluding the file changes nothing about what ships: the rulebook
# is public by design, and the strings it contains are the detector, not the
# secret.
EXCLUDED_FILES = {"tests/test_oss_sanitize.py", "scripts/oss_sanitize.py"}
# Deploy / private assets: presence anywhere is a violation.
FORBIDDEN_PATH_PATTERNS = [
    r"(^|/)deploy/", r"(^|/)secrets/", r"(^|/)k8s/",
    r"(^|/)\.workflow/",  # Gitee private CI config — never ships
    r"\.db$", r"\.pem$", r"\.key$", r"id_rsa", r"\.env$",
]

# Credential shapes in file CONTENT (value-bearing literals). Built from parts
# to keep the quoting legible: a literal assignment must not be a {template},
# an os.environ/getenv lookup, or an obvious placeholder.
_ASSIGN = (
    r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*[\"']"
    r"(?![^\"']*(?:\{|environ|getenv|YOUR_|CHANGE|EXAMPLE|placeholder|dummy))"
    r"[^\"']{12,}[\"']"
)
# A DSN literal is a credential unless the password segment is a placeholder:
# ALL-CAPS+underscore (FD_PG_PASSWORD) or <angle-bracketed> — both are template
# conventions. A real secret is mixed-case (GiteeCI#2026push) and is flagged.
_DSN = r"postgresql://[^\"'{]*:(?![A-Z0-9_]+@)[^\"'@/{}<>]{6,}@"
CREDENTIAL_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(_ASSIGN),
    re.compile(_DSN),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[0-9A-Za-z]{36}"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
]
# Internal host / topology markers that must not ship.
INTERNAL_HOST_PATTERNS = [
    re.compile(r"harbor\.local"),
    re.compile(r"lawcraw_business"),
    re.compile(r"\b100\.64\.\d+\.\d+\b"),          # tailnet CGNAT range
    re.compile(r"\.svc\.cluster\.local"),
    re.compile(r"guangzhou-xinru"),                # private coordinator hostname
]

TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".txt",
                 ".sh", ".ini", ".cfg", ".html", ".js", ".css", ".env.example"}


def _iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if rel.as_posix() in EXCLUDED_FILES:
            continue
        yield rel.as_posix(), path


def scan(root: Path) -> list[dict]:
    violations: list[dict] = []
    for rel, path in _iter_files(root):
        for pat in FORBIDDEN_PATH_PATTERNS:
            if re.search(pat, rel):
                violations.append({"kind": "topology", "file": rel, "line": 0,
                                   "match": pat})
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for cp in CREDENTIAL_PATTERNS:
                if cp.search(line):
                    violations.append({"kind": "credential", "file": rel,
                                       "line": i, "match": cp.pattern})
            for hp in INTERNAL_HOST_PATTERNS:
                if hp.search(line):
                    violations.append({"kind": "internal_host", "file": rel,
                                       "line": i, "match": hp.pattern})
    return violations


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OSS sanitization gate")
    ap.add_argument("root", nargs="?", default=".", type=Path)
    args = ap.parse_args(argv)
    violations = scan(args.root)
    if not violations:
        print(f"clean: no internal assets found under {args.root}")
        return 0
    by_kind: dict[str, list[dict]] = {}
    for v in violations:
        by_kind.setdefault(v["kind"], []).append(v)
    for kind, items in sorted(by_kind.items()):
        print(f"\n== {kind.upper()} ({len(items)}) ==")
        for v in items[:200]:
            loc = f"{v['file']}:{v['line']}" if v["line"] else v["file"]
            print(f"  {loc}  [{v['match']}]")
    print(f"\nFAIL: {len(violations)} violation(s). Remove/redact before publishing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

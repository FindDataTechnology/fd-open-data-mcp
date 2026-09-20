"""Build shim: ship the alembic revision chain inside the wheel.

The startup schema gate (``fd_open_data_mcp/db/__init__.py``) and the
migrate stage resolve the shipped revision chain from the package-internal
``alembic/`` when running from an installed wheel — e.g. the business MCP
image installs this package from PyPI and has no source checkout. The chain
stays single-sourced at the repo-root ``alembic/``; this hook copies it into
the build output so the wheel carries it, instead of keeping a second
physical copy under the package that could drift.

Only ``versions/`` is copied: that is all ``ScriptDirectory`` (and therefore
the gate) needs. The full script directory lives untouched in the source
tree and in the fd-open-data-mcp image's ``/app/alembic`` layout.
"""
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class build_py(_build_py):
    def run(self):
        super().run()
        src = Path("alembic", "versions")
        if not src.is_dir() or not list(src.glob("*.py")):
            raise SystemExit(
                f"alembic chain missing at {src} — refusing to build a wheel "
                "that the schema gate cannot resolve")
        dst = Path(self.build_lib, "fd_open_data_mcp", "alembic", "versions")
        dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.glob("*.py")):
            (dst / f.name).write_bytes(f.read_bytes())


setup(cmdclass={"build_py": build_py})

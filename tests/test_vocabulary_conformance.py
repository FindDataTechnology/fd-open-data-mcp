"""Vocabulary conformance check (db-schema-lifecycle task 6.5, design D8).

Self-contained on purpose: builds its own SQLite engine and schema via
``Base.metadata.create_all`` instead of the ``session`` fixture, so it does
not depend on how ``conftest`` wires ``fd_open_data_mcp.db`` (that module's
schema bootstrap is being changed by the same OpenSpec change).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fd_open_data_mcp.models import Base, Concept
from fd_open_data_protocol.vocabulary import load_vocabulary

_SCRIPT = Path(__file__).parent.parent / "scripts" / "check_vocabulary_conformance.py"
_spec = importlib.util.spec_from_file_location("check_vocabulary_conformance", _SCRIPT)
check_vocabulary_conformance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_vocabulary_conformance)


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'vocab.db'}")
    Base.metadata.create_all(engine)
    return engine


def _add_concept(engine, *, code: str, concept_code: str | None) -> None:
    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.add(Concept(code=code, entity_type="country", concept_code=concept_code))
        session.commit()


def test_bogus_concept_code_is_flagged(tmp_path):
    engine = _engine(tmp_path)
    _add_concept(engine, code="test.var", concept_code="BOGUS_FAMILY_X")

    problems = check_vocabulary_conformance.conformance_problems(engine)

    assert len(problems) == 1
    assert "BOGUS_FAMILY_X" in problems[0]
    # The problem names the vocabulary version so failures are traceable (D8).
    assert load_vocabulary().version in problems[0]
    engine.dispose()


def test_valid_family_id_is_clean(tmp_path):
    engine = _engine(tmp_path)
    _add_concept(
        engine,
        code="test.gdp",
        concept_code=load_vocabulary().ids("concept")[0],
    )
    _add_concept(engine, code="test.nocode", concept_code=None)  # NULL never offends

    assert check_vocabulary_conformance.conformance_problems(engine) == []
    engine.dispose()


def test_missing_concepts_table_is_pre_adoption_not_a_crash(tmp_path):
    """A database with no concepts table (migrations never run) is skipped."""
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")

    assert not check_vocabulary_conformance.concepts_table_exists(engine)
    assert check_vocabulary_conformance.conformance_problems(engine) == []
    engine.dispose()


def test_report_is_capped_at_sample_limit(tmp_path):
    engine = _engine(tmp_path)
    for i in range(check_vocabulary_conformance.SAMPLE_LIMIT + 5):
        _add_concept(engine, code=f"test.var{i}", concept_code=f"BOGUS_{i:03d}")

    problems = check_vocabulary_conformance.conformance_problems(engine)

    # At most SAMPLE_LIMIT offending values, plus one trailing "...and N more" line.
    assert len(problems) == check_vocabulary_conformance.SAMPLE_LIMIT + 1
    assert problems[-1].startswith("...and 5 more offending concept_code value(s)")
    engine.dispose()


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    engine = _engine(tmp_path)
    url = f"sqlite:///{tmp_path / 'vocab.db'}"
    engine.dispose()

    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", url)
    assert check_vocabulary_conformance.main() == 0

    _add_concept(create_engine(url), code="test.var", concept_code="BOGUS_FAMILY_X")
    assert check_vocabulary_conformance.main() == 1
    assert "BOGUS_FAMILY_X" in capsys.readouterr().out

    monkeypatch.delenv("FD_OPEN_DATA_MCP_DATABASE_URL")
    assert check_vocabulary_conformance.main() == 2

"""Smoke tests for entity taxonomy against live Postgres DB."""
from __future__ import annotations

import pytest

from fd_open_data_mcp.entities.taxonomy import (
    count_by_type,
    find_entity,
    list_entities,
)


def test_taxonomy_can_connect():
    """Verify taxonomy can connect to the remote Postgres DB."""
    counts = count_by_type()
    # At least some entities should exist
    total = sum(counts.values())
    assert total > 0, "No entities found in taxonomy"


def test_country_lookup():
    """Test country entity lookup."""
    cn = find_entity("country", "CN")
    assert cn is not None, "China (CN) should exist in taxonomy"
    assert cn["iso_code"] == "CN"
    assert cn["name_en"] == "China" or cn["name_zh"] == "中国"


def test_stock_lookup():
    """Test stock entity lookup."""
    apple = find_entity("stock", "AAPL")
    assert apple is not None, "AAPL should exist in taxonomy"
    assert apple["ticker"] == "AAPL"
    assert apple["symbol_type"] == "stock"


def test_company_lookup():
    """Test company entity lookup."""
    apple_company = find_entity("company", "AAPL")
    assert apple_company is not None, "AAPL company should exist in taxonomy"
    assert apple_company["code"] == "AAPL"


def test_list_entities_returns_data():
    """Test that list_entities returns data for various entity types."""
    # Test country listing
    countries = list_entities("country", limit=10)
    assert len(countries) > 0, "Should have at least one country"
    assert all(["iso_code" in c for c in countries]), "Each country should have iso_code"

    # Test stock listing
    stocks = list_entities("stock", limit=10)
    assert len(stocks) >= 0, "Stock list may be empty if no data"

    # Test company listing
    companies = list_entities("company", limit=10)
    assert len(companies) > 0, "Should have at least one company"
    assert all(["code" in c for c in companies]), "Each company should have code"


def test_entity_counts_reasonable():
    """Verify entity counts are within reasonable ranges."""
    counts = count_by_type()

    # Should have some countries
    assert counts.get("country", 0) >= 1, "Should have at least one country"

    # Should have some companies
    assert counts.get("company", 0) >= 1, "Should have at least one company"

    # Country count should be < 300 (all countries in world)
    assert counts.get("country", 0) < 300, "Country count seems too high"

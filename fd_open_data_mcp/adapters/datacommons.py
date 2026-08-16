"""Google Data Commons adapter: param mapping + facet-disambiguating extraction.

The DC /v2/observation API returns multiple observations per (entity, variable,
date) — one per *facet* (source/provenance). This adapter picks one facet per
(variable, entity) and returns its observation, so ``_extract_value`` (which
assumes one value per cell) gets a deterministic answer.

Facet selection heuristic (``_best_facet``):
  1. prefer ``isDcAggregate`` facets (DC's curated cross-source aggregates),
  2. then the most recent ``exportDate``,
  3. then the highest ``obsCount``.
# ponytail: heuristic, not a configurable ranking; extend ``_best_facet`` if a
# variable needs a different facet preference.
"""
from __future__ import annotations

from typing import Any, Optional

from fd_open_data_mcp.adapters import register
from fd_open_data_mcp.errors import FetchError


def _best_facet(ordered_facets: list[dict], facets_meta: dict) -> Optional[dict]:
    """Pick the single best facet dict from DC's ``orderedFacets`` array."""
    if not ordered_facets:
        return None
    def sort_key(f: dict) -> tuple:
        meta = facets_meta.get(f.get("facetId", ""), {}) or {}
        return (
            1 if meta.get("isDcAggregate") else 0,            # prefer DC aggregate
            meta.get("exportDate") or "",                     # then latest export
            f.get("obsCount", 0),                             # then most observations
        )
    return max(ordered_facets, key=sort_key)


def _observations(result: dict, variable_dcid: str, entity_dcid: str) -> list[dict]:
    """Navigate ``byVariable[v][byEntity][e][orderedFacets][best][observations]``.

    Returns the best facet's observation list (``[{date, value}, ...]``), or ``[]``
    when the variable/entity/facet is absent (caller treats as no value → failover).
    """
    by_var = (result or {}).get("byVariable") or {}
    var_node = by_var.get(variable_dcid)
    if not var_node:
        return []
    by_ent = var_node.get("byEntity") or {}
    ent_node = by_ent.get(entity_dcid)
    if not ent_node:
        return []
    facets_meta = (result or {}).get("facets") or {}
    best = _best_facet(ent_node.get("orderedFacets") or [], facets_meta)
    if best is None:
        return []
    return best.get("observations") or []


def _match_obs(observations: list[dict], date: str) -> Optional[Any]:
    """Find the observation whose date matches ``date``.

    DC dates are years (``"2023"``). A caller date of ``"2023"`` or ``"2023-01-01"``
    both match ``"2023"``. ``"LATEST"`` returns the max-date observation.
    """
    if not observations:
        return None
    if date == "LATEST":
        return max(observations, key=lambda o: o.get("date", "")).get("value")
    year = str(date)[:4]
    for o in observations:
        od = str(o.get("date", ""))
        if od == date or od == year:
            return o.get("value")
    return None


class DataCommonsAdapter:
    """Adapter for the DC ``get_observation`` command."""

    def build_params(
        self, fn: Any, identifier: str, date: str, binding: Optional[Any] = None,
    ) -> dict:
        """identifier is the entity DCID (``country/USA``); the binding's column
        name is the variable DCID (e.g. ``Count_Person``)."""
        variable = getattr(getattr(binding, "column", None), "name", None)
        if not variable:
            raise FetchError("datacommons binding has no column name (variable DCID)",
                             source="datacommons", command=fn.command)
        # DC's `date` query param wants a bare year ("2023") or "LATEST"; a full
        # ISO date ("2023-12-31" — what the yearly crawl expands to) returns NO
        # observations (the API silently filters it out). Normalize to the year;
        # extract_value's _match_obs still matches the original date string
        # against the returned observation's year date.
        if not date or date == "LATEST":
            dc_date = "LATEST"
        else:
            dc_date = str(date)[:4]
        return {
            "variable_dcids": [variable],
            "entity_dcids": [identifier],
            "date": dc_date,
        }

    def build_range_params(
        self, fn: Any, identifier: str, start: str, end: str, binding: Optional[Any] = None,
    ) -> dict:
        """Range fetch: ask DC for the full series (``date=""`` returns every
        observation across all facets); ``extract_series`` filters to [start, end].

        ``date=""`` is DC's native "all history" form — NOT ``LATEST`` (which
        returns only the single most-recent observation).
        """
        variable = getattr(getattr(binding, "column", None), "name", None)
        if not variable:
            raise FetchError("datacommons binding has no column name (variable DCID)",
                             source="datacommons", command=fn.command)
        return {
            "variable_dcids": [variable],
            "entity_dcids": [identifier],
            "date": "",
        }

    def extract_value(
        self, result: Any, column_name: str, date: str, identifier: Optional[str] = None,
    ) -> Optional[str]:
        """Pull the value for (variable=column_name, entity=identifier, date).

        Returns ``str(value)`` (cache stores strings) or ``None`` → failover.
        """
        if not isinstance(result, dict) or identifier is None:
            return None
        obs = _observations(result, column_name, identifier)
        value = _match_obs(obs, date)
        return None if value is None else str(value)

    def extract_series(
        self, result: Any, column_name: str, start: str, end: str,
    ) -> dict:
        """Pull every (date, value) in [start, end] from the best facet.

        DC year dates (``"2020"``) are normalized to ``"2020-01-01"`` so they
        compare against YYYY-MM-DD range bounds.
        """
        if not isinstance(result, dict):
            return {}
        # entity DCID isn't passed to extract_series by dispatch; recover it from
        # the byEntity node (there is one entity per call in v1).
        by_var = result.get("byVariable") or {}
        var_node = by_var.get(column_name)
        if not var_node:
            return {}
        by_ent = var_node.get("byEntity") or {}
        if not by_ent:
            return {}
        entity_dcid = next(iter(by_ent))
        obs = _observations(result, column_name, entity_dcid)
        out: dict[str, Any] = {}
        for o in obs:
            od = str(o.get("date", ""))
            if len(od) == 4 and od.isdigit():
                norm = f"{od}-01-01"   # year → YYYY-01-01 for range compare
            else:
                norm = od
            if start <= norm <= end:
                out[norm] = o.get("value")
        return out


adapter_instance = DataCommonsAdapter()
register("datacommons", "get_observation", adapter_instance)


if __name__ == "__main__":
    # ponytail: one runnable self-check for the facet/date-match logic.
    sample = {
        "byVariable": {
            "Count_Person": {
                "byEntity": {
                    "country/USA": {
                        "orderedFacets": [
                            {"facetId": "1", "obsCount": 2,
                             "observations": [{"date": "2022", "value": 333287557}]},
                            {"facetId": "2", "obsCount": 3,
                             "observations": [
                                 {"date": "2021", "value": 332048708},
                                 {"date": "2023", "value": 334914815},
                             ]},
                        ]
                    }
                }
            }
        },
        "facets": {
            "1": {"exportDate": "2023-01-01"},
            "2": {"exportDate": "2024-06-01"},  # newer → wins
        },
    }
    a = DataCommonsAdapter()
    v = a.extract_value(sample, "Count_Person", "2023", "country/USA")
    assert v == "334914815", f"expected 334914815, got {v}"
    latest = a.extract_value(sample, "Count_Person", "LATEST", "country/USA")
    assert latest == "334914815", f"LATEST should be 334914815, got {latest}"
    series = a.extract_series(sample, "Count_Person", "2020-01-01", "2023-12-31")
    assert series == {"2021-01-01": 332048708, "2023-01-01": 334914815}, series
    missing = a.extract_value(sample, "Count_Person", "2019", "country/USA")
    assert missing is None
    print("datacommons adapter self-check OK")

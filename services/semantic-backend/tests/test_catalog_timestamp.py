from datetime import UTC, datetime

import pytest
from semantic_api.catalog_v1.models import CompilerFailure

from semantic_backend.catalog_configuration import source_timestamp


@pytest.mark.parametrize(
    "value",
    [
        "2026-06-01T00:00:00.000000001Z",
        "2026-06-01T00:00:00.000000002Z",
        "2026-06-01T08:00:00.123456001+08:00",
    ],
)
def test_nonzero_submicrosecond_input_is_rejected_before_datetime_parsing(value):
    with pytest.raises(CompilerFailure, match="SOURCE_TIME_PRECISION"):
        source_timestamp(value, naive_utc=False)


@pytest.mark.parametrize(
    "value",
    [
        "2026-06-01T00:00:00.123456Z",
        "2026-06-01T00:00:00.123456000Z",
        "2026-06-01T08:00:00.123456000+08:00",
    ],
)
def test_exact_microseconds_trailing_zeroes_and_offsets_are_preserved(value):
    result = source_timestamp(value, naive_utc=False)
    assert result.astimezone(UTC) == datetime(2026, 6, 1, microsecond=123456, tzinfo=UTC)


@pytest.mark.parametrize("fraction", ["000000001", "000000002"])
def test_registry_rejects_submicrosecond_rows_before_arrow_conversion(fraction):
    import copy

    from semantic_api.catalog_v1.models import CatalogDocument
    from test_catalog_compilation import CASES, setup

    from semantic_backend.catalog_configuration import (
        CatalogConfiguration,
        CatalogEntry,
        CatalogRegistry,
    )

    case = copy.deepcopy(CASES[0])
    _, _, _, bindings, _, _ = setup(case, None)
    case["rows"]["measurements"][0]["at"] = f"2026-06-01T00:00:00.{fraction}Z"
    entry = CatalogEntry(
        document=CatalogDocument.model_validate(case["catalog"]),
        bindings=bindings,
        rows=case["rows"],
    )
    with pytest.raises(CompilerFailure, match="SOURCE_TIME_PRECISION"):
        CatalogRegistry(
            CatalogConfiguration(contract_version="catalog-server/v1", entries=(entry,))
        )

from semantic_data_nexus_databricks.models import CapabilityDeclaration


def test_capability_serialization_is_deterministic() -> None:
    serialized = CapabilityDeclaration.databricks_sql().to_dict()
    assert serialized["operations"] == [
        "SELECT",
        "FILTER",
        "AGGREGATE",
        "JOIN",
        "SORT",
        "LIMIT",
    ]
    assert serialized["disposition_formats"] == {
        "EXTERNAL_LINKS": ["JSON_ARRAY", "ARROW_STREAM", "CSV"],
        "INLINE": ["JSON_ARRAY"],
    }
    assert serialized["named_parameterization"] is True
    assert serialized["cancellation"] is True

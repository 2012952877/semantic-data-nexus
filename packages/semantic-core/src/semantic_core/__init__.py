"""Versioned semantic contracts and deterministic validation."""

from semantic_core.models import Ontology, SemanticQueryGraph
from semantic_core.validation import SemanticValidationError, validate_sqg

__all__ = [
    "Ontology",
    "SemanticQueryGraph",
    "SemanticValidationError",
    "validate_sqg",
]

__version__ = "0.1.0"


from __future__ import annotations

import re

from semantic_api.catalog_v1.catalog import fingerprint
from semantic_api.catalog_v1.models import (
    Ambiguity,
    CatalogDocument,
    Choice,
    CompilerContext,
    CompilerFailure,
    NamedTerm,
    Resolution,
    ResourceVersion,
)


def initialize(
    question: str,
    pin: ResourceVersion,
    catalog: CatalogDocument,
    capabilities: tuple[str, ...],
    answers: tuple[Resolution, ...] = (),
) -> CompilerContext:
    lexicon: dict[str, dict[str, Choice]] = {}

    def add(term: NamedTerm, kind: str, field_id: str | None = None) -> None:
        choice = Choice.model_validate(
            {
                "id": term.id,
                "label": term.label,
                "kind": kind,
                "target_id": term.id,
                "field_id": field_id,
            }
        )
        for text in (term.id, term.label, *term.synonyms):
            lexicon.setdefault(text.casefold(), {})[choice.id] = choice

    for entity in catalog.entities:
        add(entity, "entity")
    for field in catalog.fields:
        add(field, "field")
        for member in field.members:
            add(member, "member", field.id)
    for metric in catalog.metrics:
        add(metric, "metric")
    for window in catalog.time_windows:
        add(window, "time")
    matches = []
    for text, choices in lexicon.items():
        for match in re.finditer(r"(?<!\w)" + re.escape(text) + r"(?!\w)", question.casefold()):
            matches.append((match.start(), match.end(), text, choices))
    # Longest nonoverlapping mentions prevent an entity label inside a field label
    # from creating a spurious second constraint.
    matches.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))
    spans: list[tuple[int, int]] = []
    resolutions: list[Resolution] = []
    ambiguities: list[Ambiguity] = []
    supplied = {answer.term: answer.choice for answer in answers}
    for start, end, text, choices in matches:
        if any(start < b and end > a for a, b in spans):
            continue
        spans.append((start, end))
        if text in supplied:
            if choices.get(supplied[text].id) != supplied[text]:
                raise CompilerFailure("CLARIFICATION_CONTEXT_CHANGED")
            resolutions.append(Resolution(term=text, choice=supplied[text]))
        elif len(choices) == 1:
            resolutions.append(Resolution(term=text, choice=next(iter(choices.values()))))
        else:
            ambiguity = Ambiguity(
                id="ambiguity-" + fingerprint({"term": text, "choices": sorted(choices)})[:32],
                term=text,
                choices=tuple(choices[key] for key in sorted(choices)),
            )
            if ambiguity not in ambiguities:
                ambiguities.append(ambiguity)
    if any(r.choice.kind == "time" for r in resolutions):
        time_fields = {f.id: f for f in catalog.fields if f.data_type == "datetime"}
        explicit = {r.choice.target_id for r in resolutions if r.choice.kind == "field"}
        if len(time_fields) > 1 and not explicit & time_fields.keys():
            term = "time-field"
            choices = {
                f.id: Choice(id=f.id, label=f.label, kind="field", target_id=f.id)
                for f in time_fields.values()
            }
            if term in supplied:
                if choices.get(supplied[term].id) != supplied[term]:
                    raise CompilerFailure("CLARIFICATION_CONTEXT_CHANGED")
                resolutions.append(Resolution(term=term, choice=supplied[term]))
            else:
                ambiguities.append(
                    Ambiguity(
                        id="ambiguity-time-field",
                        term=term,
                        choices=tuple(choices[key] for key in sorted(choices)),
                    )
                )
    if set(supplied) - {r.term for r in resolutions}:
        raise CompilerFailure("CLARIFICATION_CONTEXT_CHANGED")
    return CompilerContext(
        question=question,
        catalog=pin,
        semantic_catalog=catalog,
        resolutions=tuple(resolutions),
        ambiguities=tuple(ambiguities),
        capabilities=capabilities,
    )

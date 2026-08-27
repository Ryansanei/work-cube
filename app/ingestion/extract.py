"""Extracts a template's fields from an authority document PDF via Docling.
Which labels to search for, and how to normalize what's found, comes from
the active template's field definitions (app/templates.py) — not a
hardcoded list — so a new template needs new data, not a new extractor.

No LLM in this path — Phase 1's synthetic documents are consistently
labeled, so a deterministic label-match over Docling's structured text
output is sufficient and keeps the base pipeline runnable with zero model
dependency, not even Ollama. LLM-assisted extraction for messier,
inconsistently formatted real-world documents is a natural future
enhancement, not built here.

Docling's layout model merges visually-close lines into a single text block
rather than one item per line (verified directly against a rendered test
document — a naive one-regex-per-whole-line parser silently missed most
fields because of this). Parsing here instead scans each text item for every
known label's *position* and slices the value between one label and the
next, so it works whether a block holds one field or several. The
provenance bounding box for a field is therefore the containing text
block's box, not a pixel-tight box around just that field — an accepted
Phase 1 approximation.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from docling.document_converter import DocumentConverter

from app.templates import Template

_converter: DocumentConverter | None = None


@dataclass
class ExtractedField:
    value: str
    page_no: int
    bbox: tuple[float, float, float, float]  # l, t, r, b


@dataclass
class ExtractionResult:
    fields: dict[str, ExtractedField]
    missing_required: list[str]

    @property
    def needs_review(self) -> bool:
        return len(self.missing_required) > 0


def _get_converter() -> DocumentConverter:
    global _converter
    if _converter is None:
        _converter = DocumentConverter()
    return _converter


def _build_label_patterns(template: Template) -> list[tuple[str, re.Pattern]]:
    """One (field_name, pattern) pair per label alias — a field with several
    aliases (e.g. a document that might say "Billed Amount:" or "Amount
    Due:") gets one pattern per alias, all mapping to the same field name."""
    patterns = []
    for f in template.fields:
        for alias in f.label_aliases:
            patterns.append((f.name, re.compile(re.escape(alias) + r":\s*")))
    return patterns


def _parse_labeled_fields(text: str, patterns: list[tuple[str, re.Pattern]]) -> dict[str, str]:
    """Finds every known label's position in `text` and slices the value
    between the end of one label and the start of the next (any label, not
    just the same field), so merged multi-field blocks parse correctly."""
    matches = []
    for field_name, pattern in patterns:
        m = pattern.search(text)
        if m:
            matches.append((m.start(), m.end(), field_name))
    matches.sort(key=lambda m: m[0])

    fields = {}
    for i, (_, end, field_name) in enumerate(matches):
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        raw_value = text[end:next_start].strip()
        if field_name not in fields:
            fields[field_name] = raw_value
    return fields


def extract_document(pdf_path: Path, template: Template) -> ExtractionResult:
    patterns = _build_label_patterns(template)
    result = _get_converter().convert(str(pdf_path))
    fields: dict[str, ExtractedField] = {}

    for item in result.document.texts:
        prov = item.prov[0] if item.prov else None
        raw_fields = _parse_labeled_fields(item.text, patterns)
        for field_name, raw_value in raw_fields.items():
            if field_name in fields:
                continue
            bbox = (prov.bbox.l, prov.bbox.t, prov.bbox.r, prov.bbox.b) if prov else (0, 0, 0, 0)
            fields[field_name] = ExtractedField(
                value=_normalize(template.field(field_name), raw_value),
                page_no=prov.page_no if prov else 1,
                bbox=bbox,
            )

    missing_required = [f for f in template.required_field_names if f not in fields]
    return ExtractionResult(fields=fields, missing_required=missing_required)


def _normalize(field, raw_value: str) -> str:
    if raw_value == "(none)":
        return ""
    if field.field_type == "enum" and field.enum_options:
        for opt in field.enum_options:
            if raw_value.lower() == opt["label"].lower():
                return opt["value"]
        return raw_value
    if field.field_type == "number":
        match = re.search(r"[\d,]+\.?\d*", raw_value)
        return match.group(0).replace(",", "") if match else ""
    return raw_value

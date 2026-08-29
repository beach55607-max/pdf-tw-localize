#!/usr/bin/env python3
"""Catalog color-management preservation and verification helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def _pypdf() -> tuple[Any, Any, Any]:
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject
    except ImportError as exc:  # fail closed only when the source needs cloning
        raise RuntimeError(
            "pypdf is required to preserve a source Catalog /OutputIntents entry"
        ) from exc
    return PdfReader, PdfWriter, NameObject


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def catalog_color_evidence(path: Path) -> dict[str, Any]:
    """Return normalized OutputIntent and decoded ICC stream evidence."""

    PdfReader, _, _ = _pypdf()
    reader = PdfReader(str(path), strict=False)
    root = reader.trailer["/Root"]
    output_intents = root.get("/OutputIntents")
    entries: list[dict[str, Any]] = []
    for index, reference in enumerate(output_intents or []):
        intent = reference.get_object()
        profile_ref = intent.get("/DestOutputProfile")
        profile_evidence: dict[str, Any] | None = None
        if profile_ref is not None:
            profile = profile_ref.get_object()
            decoded = profile.get_data()
            profile_evidence = {
                "decoded_bytes": len(decoded),
                "decoded_sha256": hashlib.sha256(decoded).hexdigest().upper(),
                "N": _plain(profile.get("/N")),
                "Alternate": _plain(profile.get("/Alternate")),
                "Range": [_plain(item) for item in (profile.get("/Range") or [])],
            }
        entries.append(
            {
                "index": index,
                "Type": _plain(intent.get("/Type")),
                "S": _plain(intent.get("/S")),
                "OutputConditionIdentifier": _plain(
                    intent.get("/OutputConditionIdentifier")
                ),
                "OutputCondition": _plain(intent.get("/OutputCondition")),
                "RegistryName": _plain(intent.get("/RegistryName")),
                "Info": _plain(intent.get("/Info")),
                "DestOutputProfile": profile_evidence,
            }
        )
    return {
        "catalog_output_intents_present": output_intents is not None,
        "output_intent_count": len(entries),
        "output_intents": entries,
    }


def clone_output_intents(source: Path, candidate: Path, output: Path) -> dict[str, Any]:
    """Clone source OutputIntents into a candidate and verify decoded identity.

    ``candidate`` and ``output`` must be different paths, and ``output`` must
    not already exist.  If the source has no OutputIntents, the caller should
    keep the candidate unchanged instead of invoking this helper.
    """

    if output.exists():
        raise FileExistsError(f"Refusing to overwrite catalog-clone output: {output}")
    PdfReader, PdfWriter, NameObject = _pypdf()
    source_reader = PdfReader(str(source), strict=False)
    candidate_reader = PdfReader(str(candidate), strict=False)
    source_intents = source_reader.trailer["/Root"].get("/OutputIntents")
    if source_intents is None:
        raise ValueError("Source has no Catalog /OutputIntents entry to clone")

    writer = PdfWriter()
    writer.clone_document_from_reader(candidate_reader)
    writer.root_object[NameObject("/OutputIntents")] = source_intents.clone(writer)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        writer.write(stream)

    source_evidence = catalog_color_evidence(source)
    output_evidence = catalog_color_evidence(output)
    if source_evidence != output_evidence:
        raise RuntimeError(
            "Catalog OutputIntents or decoded ICC profile changed during cloning"
        )
    return {
        "method": "pypdf_object_clone",
        "source": source_evidence,
        "candidate": output_evidence,
        "output_intents_exact": True,
    }

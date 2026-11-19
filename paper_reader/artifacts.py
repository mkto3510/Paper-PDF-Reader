"""Recover the positions of text marked as PDF Artifact."""

from __future__ import annotations

import re

import pymupdf
from pypdf.generic import ContentStream, DecodedStreamObject, NameObject


LAYER_NAME = "PaperPdfReader artifact analysis"
LAYER_KEY = NameObject("/PaperPdfReaderArtifact")
OPTIONAL_CONTENT = NameObject("/OC")
PAINT = {b"Tj", b"TJ", b"'", b'"', b"Do"}
ARTIFACT_NAME = re.compile(rb"/Artifact(?=[\s/<>\[\]()%]|$)")


def _decoded_names(data: bytes) -> bytes:
    return re.sub(rb"#([a-fA-F0-9]{2})", lambda m: bytes([int(m[1], 16)]), data)


def _mark(data):
    stream = DecodedStreamObject()
    stream.set_data(data)
    content = ContentStream(stream, None)
    marked = []
    result = []
    changed = False
    for args, op in content.operations:
        if op in (b"BMC", b"BDC"):
            marked.append(bool(args and args[0] == "/Artifact") or bool(marked and marked[-1]))
        elif op == b"EMC" and marked:
            marked.pop()
        if marked and marked[-1] and op in PAINT:
            result.extend((([OPTIONAL_CONTENT, LAYER_KEY], b"BDC"), (args, op), ([], b"EMC")))
            changed = True
        else:
            result.append((args, op))
    if not changed:
        return None
    content.operations = result
    return content.get_data()


def _set_layer_property(doc, owner, marker):
    kind, value = doc.xref_get_key(owner, "Resources")
    resource = int(value.split()[0]) if kind == "xref" else owner
    prefix = "" if kind == "xref" else "Resources/"
    kind, value = doc.xref_get_key(resource, prefix + "Properties")
    if kind == "xref":
        resource, prefix = int(value.split()[0]), ""
    else:
        prefix += "Properties/"
    doc.xref_set_key(resource, prefix + str(LAYER_KEY)[1:], f"{marker} 0 R")


def artifact_glyphs(page):
    """Return exact (origin, character) keys without modifying the open document."""
    document = page.parent
    sources = (*page.get_contents(), *(item[0] for item in page.get_xobjects()))
    if not any(ARTIFACT_NAME.search(_decoded_names(document.xref_stream(xref) or b"")) for xref in sources):
        return set()
    with pymupdf.open() as scratch:
        scratch.insert_pdf(document, from_page=page.number, to_page=page.number, links=False, annots=False, widgets=False)
        copy = scratch[0]
        marker = scratch.add_ocg(LAYER_NAME, on=True)
        xrefs = copy.get_contents()
        if xrefs:
            joined = b"\n".join(scratch.xref_stream(xref) or b"" for xref in xrefs)
            if marked := _mark(joined):
                _set_layer_property(scratch, copy.xref, marker)
                scratch.update_stream(xrefs[0], marked)
                copy.set_contents(xrefs[0])
        for form, _, _, _ in copy.get_xobjects():
            data = scratch.xref_stream(form) or b""
            if ARTIFACT_NAME.search(_decoded_names(data)) and (marked := _mark(data)):
                _set_layer_property(scratch, form, marker)
                scratch.update_stream(form, marked)
        copy = scratch.reload_page(copy)
        return {(round(origin[0], 1), round(origin[1], 1), chr(code))
                for span in copy.get_texttrace() if span["layer"] == LAYER_NAME
                for code, _, origin, _ in span["chars"] if 0 <= code <= 0x10FFFF}

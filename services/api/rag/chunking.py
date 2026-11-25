"""Text chunking strategies (benchmarked by rag/eval.py).

fixed(size, overlap):        recursive split on paragraphs -> sentences -> words, packed to ~size chars
parent_child(parent, child): children (small, precise) are embedded and searched; the parent
                             (larger, coherent) is what the model reads. Better recall of exact
                             facts without losing context.
"""

import re
from dataclasses import dataclass, field

_SPLITTERS = [re.compile(r"\n\s*\n"), re.compile(r"(?<=[.!?])\s+"), re.compile(r"\s+")]


def _split(text: str, size: int, level: int = 0) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size or level >= len(_SPLITTERS):
        return [text[i : i + size] for i in range(0, len(text), size)] if len(text) > size else [text]
    parts = [p for p in _SPLITTERS[level].split(text) if p.strip()]
    if len(parts) == 1:
        return _split(text, size, level + 1)
    out: list[str] = []
    for p in parts:
        out.extend(_split(p, size, level + 1) if len(p) > size else [p.strip()])
    return out


def fixed(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    """Pack pieces into chunks of at most ~size chars; consecutive chunks share ~overlap chars."""
    pieces = _split(text, size)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n{piece}" if current else piece
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            # start the next chunk at a word boundary inside the overlap
            tail = tail[tail.find(" ") + 1 :] if " " in tail else tail
            current = f"{tail} {piece}".strip() if tail else piece
        else:
            current = piece
    if current:
        chunks.append(current)
    return chunks


@dataclass
class Parent:
    text: str
    children: list[str] = field(default_factory=list)


def parent_child(text: str, parent_size: int = 2000, child_size: int = 400, overlap: int = 50) -> list[Parent]:
    return [Parent(p, fixed(p, child_size, overlap)) for p in fixed(text, parent_size, 0)]

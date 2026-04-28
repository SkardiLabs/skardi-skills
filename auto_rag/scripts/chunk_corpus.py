#!/usr/bin/env python3
"""Walk a corpus directory and emit an NDJSON file of chunks ready for bulk_ingest.py.

Output file is newline-delimited JSON with a `.json` extension (the
extension DataFusion recognises). Each line is an object with fields:
  - id: stable 64-bit integer derived from (source, chunk_idx) so re-running
    on the same corpus produces the same ids.
  - source: relative path from --corpus root (for citation).
  - chunk_idx: 0-indexed position of the chunk within that source.
  - content: the chunk text, prefixed with the heading trail so dense-retrieval
    scores retain the section titles.

We use NDJSON instead of CSV because DataFusion's CSV reader tokenises by
line, which mis-splits cells containing embedded newlines. JSON escapes
newlines inside strings, so multi-paragraph chunks round-trip cleanly.

Chunker behaviour:
  - Markdown (.md): splits on H2/H3 headings first, then packs paragraphs
    within each section into <= --max-chars chunks with --overlap char overlap.
  - Plain text (.txt, .rst, .log, etc.): paragraph-pack with the same budget.
  - YAML front-matter is stripped.
  - Non-text / binary files are skipped with a warning.

This chunker is intentionally simple. Swap it out if your corpus needs
something fancier (semantic chunking, code-aware splitting, etc.) — any CSV
with the same header works downstream.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

DEFAULT_INCLUDE = "*.md,*.markdown,*.txt,*.rst"
DEFAULT_MAX_CHARS = 1200
DEFAULT_OVERLAP = 200

FRONT_MATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def stable_id(source, chunk_idx):
    """64-bit positive int derived from source path + chunk index."""
    h = hashlib.blake2b(f"{source}\0{chunk_idx}".encode(), digest_size=8).digest()
    # Mask to 63 bits so it fits signed BIGINT.
    return int.from_bytes(h, "big") & ((1 << 63) - 1)


def strip_front_matter(text):
    return FRONT_MATTER_RE.sub("", text, count=1)


def split_markdown_sections(text):
    """Yields (heading_trail, body) tuples. Heading trail is a breadcrumb
    like 'Chapter 1 > Introduction' for the nearest enclosing H1+H2 etc."""
    # Find all H2/H3 boundaries. Keep H1 as the doc title (prefixed to all).
    doc_title = None
    m = re.search(r"^# (.+?)\s*$", text, re.MULTILINE)
    if m:
        doc_title = m.group(1).strip()
    sections = []
    current = {"trail": [doc_title] if doc_title else [], "start": 0}
    for match in re.finditer(r"^(#{2,3})\s+(.+?)\s*$", text, re.MULTILINE):
        sections.append((current, match.start()))
        level = len(match.group(1))
        heading = match.group(2).strip()
        trail = [doc_title] if doc_title else []
        if level == 2:
            trail.append(heading)
        elif level == 3:
            # Try to keep the most recent H2 in the trail; fall back to just H3.
            if current["trail"] and len(current["trail"]) >= 2:
                trail = current["trail"][:2] + [heading]
            else:
                trail.append(heading)
        current = {"trail": trail, "start": match.end()}
    sections.append((current, len(text)))

    result = []
    for (meta, _), end in zip(sections, [e for _, e in sections][1:] + [len(text)]):
        body = text[meta["start"] : end].strip()
        if body:
            trail = " > ".join(t for t in meta["trail"] if t)
            result.append((trail, body))
    return result


def pack_paragraphs(text, max_chars, overlap):
    """Splits text into ~max_chars chunks, breaking on paragraph boundaries
    when possible. Applies a char-level overlap on joined boundaries."""
    paragraphs = [p.strip() for p in PARAGRAPH_SPLIT_RE.split(text) if p.strip()]
    chunks = []
    buf = ""
    for p in paragraphs:
        if not buf:
            buf = p
            continue
        if len(buf) + 2 + len(p) <= max_chars:
            buf = f"{buf}\n\n{p}"
        else:
            chunks.append(buf)
            # carry tail of the previous chunk as overlap
            tail = buf[-overlap:] if overlap > 0 else ""
            buf = (tail + "\n\n" + p).strip() if tail else p
    if buf:
        chunks.append(buf)

    # If any single paragraph exceeds max_chars, hard-split it.
    expanded = []
    for c in chunks:
        if len(c) <= max_chars:
            expanded.append(c)
            continue
        step = max(1, max_chars - overlap)
        for i in range(0, len(c), step):
            expanded.append(c[i : i + max_chars])
    return expanded


def chunk_markdown(text, max_chars, overlap):
    sections = split_markdown_sections(text)
    chunks = []
    if not sections:
        # No H2/H3 — fall back to paragraph-pack.
        for chunk in pack_paragraphs(text, max_chars, overlap):
            chunks.append(chunk)
        return chunks
    for trail, body in sections:
        for chunk in pack_paragraphs(body, max_chars, overlap):
            if trail:
                chunks.append(f"[{trail}] {chunk}")
            else:
                chunks.append(chunk)
    return chunks


def chunk_text(text, max_chars, overlap):
    return pack_paragraphs(text, max_chars, overlap)


def is_markdown(path):
    return path.suffix.lower() in {".md", ".markdown"}


def iter_files(corpus_root, patterns):
    pats = [p.strip() for p in patterns.split(",") if p.strip()]
    seen = set()
    for pat in pats:
        for p in sorted(corpus_root.rglob(pat)):
            if p.is_file() and p not in seen:
                seen.add(p)
                yield p


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", required=True, help="Root directory of documents")
    ap.add_argument(
        "--out",
        required=True,
        help="Output NDJSON path. Must end in .json so DataFusion recognises it.",
    )
    ap.add_argument(
        "--include",
        default=DEFAULT_INCLUDE,
        help=f"Comma-separated glob patterns (default: {DEFAULT_INCLUDE})",
    )
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    args = ap.parse_args()

    corpus = Path(args.corpus).expanduser().resolve()
    if not corpus.is_dir():
        raise SystemExit(f"--corpus {corpus} is not a directory")
    out = Path(args.out).expanduser().resolve()
    if out.suffix.lower() != ".json":
        raise SystemExit(
            f"--out {out} must end in .json (DataFusion's JSON reader only "
            f"recognises that extension)."
        )
    out.parent.mkdir(parents=True, exist_ok=True)

    n_files = 0
    n_chunks = 0
    skipped = []
    with out.open("w", encoding="utf-8") as f:
        for path in iter_files(corpus, args.include):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                skipped.append(str(path.relative_to(corpus)))
                continue
            text = strip_front_matter(text)
            rel = str(path.relative_to(corpus))
            chunks = (
                chunk_markdown(text, args.max_chars, args.overlap)
                if is_markdown(path)
                else chunk_text(text, args.max_chars, args.overlap)
            )
            for idx, chunk in enumerate(chunks):
                if not chunk.strip():
                    continue
                obj = {
                    "id": stable_id(rel, idx),
                    "source": rel,
                    "chunk_idx": idx,
                    "content": chunk,
                }
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                n_chunks += 1
            n_files += 1

    print(f"Wrote {n_chunks} chunks from {n_files} files -> {out}")
    if skipped:
        print(f"  skipped {len(skipped)} non-UTF8 files: {skipped[:5]}{'...' if len(skipped) > 5 else ''}")


if __name__ == "__main__":
    main()

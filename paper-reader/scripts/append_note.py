#!/usr/bin/env python3
"""Append a paper note to the unified reading log.

Maintains the index table, de-duplicates entries by DOI / arXiv ID / title,
and appends to an existing entry instead of creating a duplicate.

The notes file is never rewritten destructively: existing text stays as is.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys

HEADER = """# 论文阅读记录

> 由 paper-reader 维护：索引表自动更新，正文按时间追加，不覆盖旧内容。
> 速读卡片和精读笔记按论文归在同一节；再次阅读追加「补充」小节。

<!-- index:start -->
| 日期 | 论文 | 年份 | 深度 | 判断 | 标签 |
|---|---|---|---|---|---|
<!-- index:end -->
"""

DEPTH_RANK = {"scan": 1, "deep": 2, "supplement": 0}
DEPTH_LABEL = {"scan": "速读", "deep": "精读", "supplement": "补充"}


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "-", value)
    return value.strip("-")


def normalize_title(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def normalize_arxiv(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.I)
    value = re.sub(r"v\d+$", "", value)
    return value


def entry_ids(title: str, arxiv: str, doi: str):
    """Return (anchor, list of ids used for de-duplication)."""
    ids = []
    anchor = ""
    if arxiv:
        anchor = "p-" + slugify(normalize_arxiv(arxiv))
        ids.append(anchor)
    if doi:
        doi_id = "p-" + slugify(doi)
        ids.append(doi_id)
        if not anchor:
            anchor = doi_id
    if not anchor:
        digest = hashlib.md5(normalize_title(title).encode("utf-8")).hexdigest()[:8]
        anchor = "p-" + digest
        ids.append(anchor)
    return anchor, ids


def cell(value: str) -> str:
    # A literal pipe would break the markdown table and the row parser below,
    # so render it as the full-width character instead of escaping.
    return (value or "").replace("|", "｜").replace("\n", " ").strip()


def format_tags(tags: str) -> str:
    parts = [t.strip().lstrip("#") for t in re.split(r"[,\s]+", tags or "") if t.strip()]
    return " ".join("#" + p for p in parts)


def ensure_file(path: str) -> str:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(HEADER)
    return HEADER


def parse_meta(text: str):
    """Yield (match_start, match_end, meta_dict) for every entry in the file."""
    for match in re.finditer(r"<!-- meta: (\{.*?\}) -->", text, re.S):
        try:
            meta = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        yield match.start(), match.end(), meta


def find_existing(text: str, ids, title, arxiv, doi):
    wanted_title = normalize_title(title)
    wanted_arxiv = normalize_arxiv(arxiv)
    for start, _end, meta in parse_meta(text):
        if wanted_arxiv and normalize_arxiv(meta.get("arxiv", "")) == wanted_arxiv:
            return start, meta
        if doi and meta.get("doi", "").lower() == doi.lower():
            return start, meta
        if meta.get("title") and normalize_title(meta["title"]) == wanted_title:
            return start, meta
    for anchor in ids:
        marker = '<a id="%s"></a>' % anchor
        pos = text.find(marker)
        if pos != -1:
            return pos, {"_anchor": anchor}
    return None, None


def anchor_for_meta(meta: dict, text: str, fallback: str) -> str:
    if meta.get("_anchor"):
        return meta["_anchor"]
    anchor, _ = entry_ids(meta.get("title", ""), meta.get("arxiv", ""), meta.get("doi", ""))
    if ('<a id="%s"></a>' % anchor) in text:
        return anchor
    return fallback or anchor


def entry_bounds(text: str, entry_start: int):
    """Return (start, end) of an entry: start at its <a id> line, end at <!-- /entry -->."""
    if entry_start > 0:
        line_start = text.rfind("\n", 0, entry_start) + 1
        if text[line_start:entry_start].strip() == "" and text[line_start:entry_start].count("\n") == 0:
            # Keep the anchor line start; preceding blank line is not part of the entry.
            entry_start = line_start
    end_marker = text.find("<!-- /entry -->", entry_start)
    if end_marker != -1:
        return entry_start, end_marker
    nxt = text.find("\n<a id=", entry_start + 1)
    if nxt == -1:
        return entry_start, len(text)
    return entry_start, nxt


def split_leading_heading(body: str):
    """If the body already starts with a '### ' heading, return (heading, rest)."""
    first, sep, rest = body.partition("\n")
    if first.startswith("### "):
        return first[4:].strip(), rest.lstrip("\n")
    return None, body


def subsection_heading(existing_text: str, depth: str, date: str, heading: str) -> str:
    if depth == "scan":
        return "### 速读卡片" if "### 速读卡片" not in existing_text else "### 补充 · %s · 速读" % date
    if depth == "deep":
        return "### 精读笔记" if "### 精读笔记" not in existing_text else "### 补充 · %s · 精读" % date
    return "### 补充 · %s · %s" % (date, heading or "补充")


def add_index_row(text: str, date: str, title: str, anchor: str, year: str, depth: str,
                  verdict: str, tags: str) -> str:
    row = "| %s | [%s](#%s) | %s | %s | %s | %s |" % (
        cell(date), cell(title), anchor, cell(year), DEPTH_LABEL.get(depth, depth),
        cell(verdict), cell(tags),
    )
    start = text.find("<!-- index:start -->")
    end = text.find("<!-- index:end -->")
    if start == -1 or end == -1:
        return text
    block = text[start:end]
    sep = re.search(r"^\|[\s:\-|]+\|$", block, re.M)
    if not sep:
        return text
    insert_at = start + sep.end()
    return text[:insert_at] + "\n" + row + text[insert_at:]


def update_index_depth(text: str, anchor: str, depth: str, verdict: str) -> str:
    if depth == "supplement":
        return text
    pattern = re.compile(r"^\|([^\n]*?\(#%s\)[^\n]*?)\|$" % re.escape(anchor), re.M)
    match = pattern.search(text)
    if not match:
        return text
    cells = match.group(1).split("|")
    # Layout inside the capture: date, title, year, depth, verdict, tags.
    if len(cells) < 6:
        return text
    current = cells[3].strip()
    current_rank = 2 if current == "精读" else 1 if current == "速读" else 0
    if DEPTH_RANK.get(depth, 0) > current_rank:
        cells[3] = " %s " % DEPTH_LABEL[depth]
    if verdict:
        cells[4] = " %s " % cell(verdict)
    new_row = "|" + "|".join(cells) + "|"
    return text[:match.start()] + new_row + text[match.end():]


def main() -> int:
    parser = argparse.ArgumentParser(description="Append a paper note to the unified reading log.")
    parser.add_argument("--notes", default="ForMe/阅读记录.md", help="Path of the unified reading log.")
    parser.add_argument("--title", required=True, help="Paper title.")
    parser.add_argument("--year", default="", help="Publication year.")
    parser.add_argument("--venue", default="", help="Venue or journal.")
    parser.add_argument("--authors", default="", help="Short author list, e.g. 'Vaswani et al.'")
    parser.add_argument("--arxiv", default="", help="arXiv ID, e.g. 1706.03762.")
    parser.add_argument("--doi", default="", help="DOI.")
    parser.add_argument("--depth", choices=["scan", "deep", "supplement"], default="scan")
    parser.add_argument("--verdict", default="", help="e.g. 强烈推荐精读 / 值得一读 / 按需 / 跳过.")
    parser.add_argument("--tags", default="", help="Comma or space separated tags.")
    parser.add_argument("--heading", default="", help="Topic label for a supplement subsection.")
    parser.add_argument("--body", default="-", help="Markdown body file, or '-' for stdin.")
    parser.add_argument("--date", default=_dt.date.today().isoformat(), help="YYYY-MM-DD.")
    args = parser.parse_args()

    if args.body == "-":
        body = sys.stdin.read()
    else:
        with open(args.body, "r", encoding="utf-8") as fh:
            body = fh.read()
    body = body.strip()
    if not body:
        print("error: empty body", file=sys.stderr)
        return 1
    if "<!-- /entry -->" in body or "<!-- meta:" in body:
        print("error: body must not contain entry markers", file=sys.stderr)
        return 1

    text = ensure_file(args.notes)
    anchor, ids = entry_ids(args.title, args.arxiv, args.doi)
    existing_start, existing_meta = find_existing(text, ids, args.title, args.arxiv, args.doi)

    if existing_start is not None:
        meta = existing_meta or {}
        anchor = anchor_for_meta(meta, text, anchor)
        start, end = entry_bounds(text, existing_start)
        entry_text = text[start:end]
        body_heading, body = split_leading_heading(body)
        heading = "### " + body_heading if body_heading else subsection_heading(
            entry_text, args.depth, args.date, args.heading)
        before = text[:end]
        prefix = "" if before.endswith("\n\n") else "\n\n"
        block = "%s%s\n\n%s\n\n" % (prefix, heading, body)
        text = before + block + text[end:]
        text = update_index_depth(text, anchor, args.depth, args.verdict)
        action = "appended to existing entry"
    else:
        while ('<a id="%s"></a>' % anchor) in text:
            anchor = anchor + "-2"
        meta = {
            "title": args.title,
            "year": args.year,
            "venue": args.venue,
            "authors": args.authors,
            "arxiv": normalize_arxiv(args.arxiv),
            "doi": args.doi,
            "added": args.date,
        }
        entry = '<a id="%s"></a>\n## %s\n\n<!-- meta: %s -->\n\n%s\n\n<!-- /entry -->\n\n---\n' % (
            anchor, args.title, json.dumps(meta, ensure_ascii=False, sort_keys=True), body,
        )
        if not text.endswith("\n"):
            text += "\n"
        if text and not text.endswith("\n\n"):
            text += "\n"
        text = text + entry
        had_index = "<!-- index:start -->" in text and "<!-- index:end -->" in text
        text = add_index_row(text, args.date, args.title, anchor, args.year, args.depth,
                             args.verdict, format_tags(args.tags))
        if not had_index:
            print("warning: index markers not found; entry appended without an index row",
                  file=sys.stderr)
        action = "created new entry"

    with open(args.notes, "w", encoding="utf-8") as fh:
        fh.write(text)

    print("%s: %s" % (action, args.notes))
    print("anchor: #%s" % anchor)
    return 0


if __name__ == "__main__":
    sys.exit(main())

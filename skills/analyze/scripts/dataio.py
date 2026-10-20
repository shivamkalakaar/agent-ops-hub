#!/usr/bin/env python3
"""Read a messy CSV, TSV, JSON, NDJSON or XLSX file into a typed table (standard library only).

  python3 scripts/dataio.py sales.csv
  python3 scripts/dataio.py book.xlsx --sheet Sales --head 5
  cat sales.csv | python3 scripts/dataio.py - --json

As a module:  from dataio import read_table;  t = read_table("sales.xlsx");  t.values("Revenue")

What it does for you: sniffs the delimiter and encoding, skips title rows above the header, drops empty
columns and blank rows, infers a type per column (int, float, money, percent, date, datetime, bool,
category, id, text), understands "1.234,56", "$1,200", "12%", "(500)", "N/A", "#DIV/0!", reports
DD/MM versus MM/DD ambiguity instead of guessing, and flags Total rows (they are left out of the rows).
Exit codes: 0 ok, 2 file or usage problem (one line message).
"""
from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import os
import re
import sys
from array import array

import _parse as P

csv.field_size_limit(2 ** 31 - 1)
MAX_BYTES = 250 * 1024 * 1024
TOTAL_RE = re.compile(r"^(grand[\s-]*|sub[\s-]*|overall[\s-]*|column[\s-]*|report[\s-]*)?(total|totals|sum)(\s+row|\s+all)?\s*:?$", re.I)
DELIMS = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}


class DataError(Exception):
    """A problem with the input that the user can fix. The message is one plain line."""


class Table:
    """header: column names; rows: lists of raw text; infos: per-column inference; meta: how it was read."""

    def __init__(self, header, rows, infos, meta, row_numbers=None):
        self.header, self.rows, self.infos, self.meta = header, rows, infos, meta
        self.row_numbers = row_numbers
        self._vals = {}

    @property
    def types(self):
        return [i["type"] for i in self.infos]

    @property
    def n_rows(self):
        return len(self.rows)

    @property
    def n_cols(self):
        return len(self.header)

    def index(self, col):
        if isinstance(col, int):
            if 0 <= col < len(self.header):
                return col
            raise DataError("column number %d is out of range (0-%d)" % (col, len(self.header) - 1))
        norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())
        want = norm(str(col))
        for j, h in enumerate(self.header):
            if h == col:
                return j
        for j, h in enumerate(self.header):
            if norm(h) == want:
                return j
        near = difflib.get_close_matches(str(col), self.header, n=3, cutoff=0.5)
        raise DataError("no column named '%s'. Columns: %s%s" % (
            col, ", ".join(self.header[:25]) + (" ..." if len(self.header) > 25 else ""),
            ("  Did you mean: " + ", ".join(near) + "?") if near else ""))

    def raw(self, col):
        j = self.index(col)
        return [r[j] for r in self.rows]

    def values(self, col):
        """Typed values (None for nulls and unparseable cells). Dates come back as datetime.date/datetime."""
        j = self.index(col)
        if j not in self._vals:
            conv = P.make_converter(self.infos[j])
            self._vals[j] = [conv(r[j]) for r in self.rows]
        return self._vals[j]

    def src_row(self, k):
        """Row number in the source file (1-based) for table row k."""
        if self.row_numbers is not None and 0 <= k < len(self.row_numbers):
            return self.row_numbers[k]
        return k + 1 + self.meta.get("header_row", 1)


# ------------------------------------------------------------------ readers
def _sniff(sample):
    lines = [ln for ln in sample.splitlines()[:40] if ln.strip()]
    if len(lines) > 1 and not sample.endswith("\n"):
        lines = lines[:-1]
    best, best_score = ",", (0, 0)
    for d in (",", ";", "\t", "|"):
        counts = []
        for ln in lines:
            try:
                counts.append(len(next(csv.reader([ln], delimiter=d))))
            except (csv.Error, StopIteration):
                counts.append(1)
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        score = (counts.count(mode) if mode > 1 else 0, mode)
        if score > best_score:
            best, best_score = d, score
    return best


def _stream(reader, cap):
    """Collect rows. Low-cardinality columns are interned (shared strings) to keep big files small in memory."""
    rows, cols, dicts = [], [], {}
    try:
        for row in reader:
            rows.append(row)
            n = len(rows)
            if n == 20000:
                cols = [j for j in range(len(row)) if len({r[j] for r in rows if len(r) > j}) < 0.3 * n]
                dicts = dict((j, {}) for j in cols)
                for r in rows:
                    for j in cols:
                        if len(r) > j:
                            r[j] = dicts[j].setdefault(r[j], r[j])
            elif n > 20000 and cols:
                for j in cols:
                    if len(row) > j:
                        d = dicts[j]
                        row[j] = d.setdefault(row[j], row[j])
                        if len(d) > 100000:
                            cols = [c for c in cols if c != j]
            if cap and n >= cap:
                return rows, True
    except csv.Error as ex:
        raise DataError("the file is not valid CSV near row %d (%s)" % (len(rows) + 1, ex))
    return rows, False


def _read_delimited(opener, encs, cap, meta):
    for enc in encs:
        try:
            with opener(enc) as f:
                sample = f.read(65536)
                delim = _sniff(sample)
                f.seek(0)
                rows, cut = _stream(csv.reader(f, delimiter=delim), cap)
        except UnicodeDecodeError:
            continue
        meta.update(encoding=enc, delimiter=delim, format="csv")
        if cut:
            meta["truncated"] = True
        return rows
    raise DataError("could not decode the file as text (tried %s)" % ", ".join(encs))


def _encodings(head):
    if head.startswith(b"\xef\xbb\xbf"):
        return ["utf-8-sig"]
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return ["utf-16"]
    if head[:200].count(b"\x00") > 20:
        return ["utf-16-le" if head[1:2] == b"\x00" else "utf-16-be"]
    return ["utf-8", "cp1252", "latin-1"]


def _jstr(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v == int(v) and abs(v) < 1e15 else repr(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)


def _json_rows(text):
    text = text.lstrip("﻿")
    try:
        obj = json.loads(text)
    except ValueError:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        try:
            obj = [json.loads(ln) for ln in lines]
        except ValueError:
            raise DataError("the file looks like JSON but is not valid JSON or NDJSON")
    if isinstance(obj, dict):
        lists = [(k, v) for k, v in obj.items() if isinstance(v, list) and v and isinstance(v[0], (dict, list))]
        if lists:
            obj = lists[0][1]
        elif obj and all(isinstance(v, list) for v in obj.values()):
            keys = list(obj)
            n = max(len(v) for v in obj.values())
            return [keys] + [[_jstr(obj[k][i]) if i < len(obj[k]) else "" for k in keys] for i in range(n)]
        else:
            obj = [obj]
    if not isinstance(obj, list):
        raise DataError("JSON must be a list of records (or an object holding one)")
    if obj and all(isinstance(r, list) for r in obj):
        return [[_jstr(c) for c in r] for r in obj]
    recs = []
    for r in obj:
        if not isinstance(r, dict):
            r = {"value": r}
        flat = {}
        for k, v in r.items():
            if isinstance(v, dict) and v:
                for k2, v2 in v.items():
                    flat["%s.%s" % (k, k2)] = v2
            else:
                flat[k] = v
        recs.append(flat)
    keys = []
    seen = set()
    for r in recs:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return [keys] + [[_jstr(r.get(k)) for k in keys] for r in recs]


def _load(path, sheet, cap, meta):
    if path == "-":
        data = sys.stdin.buffer.read()
        head = data[:4096]
        opener = lambda enc: io.StringIO(data.decode(enc), newline="")
        size = len(data)
        is_json_ext = False
    else:
        if not os.path.exists(path):
            raise DataError("file not found: %s" % path)
        if os.path.isdir(path):
            raise DataError("%s is a folder, give a file" % path)
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(4096)
        opener = lambda enc: open(path, "r", encoding=enc, newline="")
        is_json_ext = path.lower().endswith((".json", ".ndjson", ".jsonl"))
    if size == 0:
        raise DataError("the file is empty")
    if size > MAX_BYTES:
        raise DataError("the file is %d MB; this tool reads up to %d MB. Split it or export a sample first"
                        % (size // 2 ** 20, MAX_BYTES // 2 ** 20))
    if head.startswith(b"PK\x03\x04"):
        if path == "-":
            raise DataError("XLSX cannot be read from stdin, give a file path")
        return _load_xlsx(path, sheet, cap, meta)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise DataError("this is an old .xls file; open it in Excel and save as .xlsx or CSV, then retry")
    if head.startswith(b"%PDF"):
        raise DataError("this is a PDF, not a table; export the table to CSV or XLSX first")
    encs = _encodings(head)
    first = head.decode(encs[0], errors="ignore").lstrip("﻿ \t\r\n")[:1]
    if is_json_ext or first in ("[", "{"):
        for enc in encs:
            try:
                with opener(enc) as f:
                    text = f.read()
                break
            except UnicodeDecodeError:
                continue
        else:
            raise DataError("could not decode the file as text")
        try:
            rows = _json_rows(text)
            meta.update(format="json", encoding=enc)
            return rows
        except DataError:
            if is_json_ext:
                raise
    return _read_delimited(opener, encs, cap, meta)


def _load_xlsx(path, sheet, cap, meta):
    import _xlsx
    try:
        sheets, d1904 = _xlsx.list_sheets(path)
    except (ValueError, KeyError, OSError) as ex:
        raise DataError("cannot open this as an Excel workbook (%s)" % ex)
    except Exception as ex:                         # zip or XML damage
        raise DataError("cannot open this as an Excel workbook (%s)" % ex.__class__.__name__)
    if not sheets:
        raise DataError("the workbook has no sheets")
    meta.update(format="xlsx", sheets=[s[0] for s in sheets])
    chosen = None
    if sheet is not None:
        for i, s in enumerate(sheets):
            if s[0] == str(sheet) or s[0].lower() == str(sheet).lower() or str(i + 1) == str(sheet):
                chosen = s
                break
        if chosen is None:
            raise DataError("no sheet '%s'. Sheets: %s" % (sheet, ", ".join(s[0] for s in sheets)))
    else:
        visible = [s for s in sheets if s[2] == "visible"] or sheets
        chosen = visible[0]
        if len(sheets) > 1:
            meta["warnings"].append("workbook has %d sheets (%s); reading '%s'. Use --sheet to pick another."
                                    % (len(sheets), ", ".join(s[0] for s in sheets), chosen[0]))
    meta["sheet"] = chosen[0]
    try:
        rows, merged = _xlsx.read_sheet(path, chosen[1], d1904, cap)
    except (ValueError, KeyError) as ex:
        raise DataError("cannot read sheet '%s' (%s)" % (chosen[0], ex))
    except Exception as ex:
        raise DataError("cannot read sheet '%s' (%s)" % (chosen[0], ex.__class__.__name__))
    if merged:
        meta["merged_cells"] = merged
        meta["warnings"].append("%d merged cell range(s) ignored: the value sits only in the top-left cell." % merged)
    if cap and len(rows) >= cap:
        meta["truncated"] = True
    return rows


# ------------------------------------------------------------------ assembling
def _find_header(rows):
    counts = [sum(1 for c in r if c.strip()) for r in rows[:40]]
    if not counts or max(counts) == 0:
        return None
    w = max(counts)
    thr = 1 if w == 1 else max(2, (w * 6 + 9) // 10)
    for i, c in enumerate(counts):
        if c >= thr:
            return i
    return None


def _looks_headerless(row):
    cells = [c.strip() for c in row if c.strip()]
    if not cells:
        return False
    numeric = sum(1 for c in cells if P.number_shape(c) is not None or P.date_parts(c) is not None)
    return numeric / len(cells) > 0.6


def _find_totals(body, infos):
    label_cols = [j for j, i in enumerate(infos) if i["type"] not in P.NUMERIC_TYPES]
    flagged = {}
    for k, r in enumerate(body):
        for j in label_cols:
            v = r[j].strip()
            if v and TOTAL_RE.match(v):
                flagged[k] = ("label", v)
                break
    num = [j for j, i in enumerate(infos) if i["type"] in P.NUMERIC_TYPES]
    if len(num) >= 2 and len(body) >= 3:
        k = len(body) - 1
        while k in flagged and k > 0:
            k -= 1
        if k not in flagged and k >= 2:
            row = body[k]
            m = ok = 0
            for j in num:
                conv = P.make_converter(infos[j])
                v = conv(row[j])
                if v is None:
                    continue
                m += 1
                s = sum(x for x in (conv(rr[j]) for idx, rr in enumerate(body[:k]) if idx not in flagged) if x is not None)
                if s != 0 and abs(v - s) <= max(0.005 * abs(s), 0.01):
                    ok += 1
            if m >= 2 and ok >= max(2, 0.5 * m):
                flagged[k] = ("sum", "equals the sum of the rows above in %d columns" % ok)
    return flagged


def _assemble(rows, meta, date_order, keep_totals, max_rows):
    w = meta["warnings"]
    hi = _find_header(rows)
    if hi is None:
        raise DataError("no data found: the file has no non-empty rows")
    headerless = _looks_headerless(rows[hi])
    if hi > 0:
        skipped = [" | ".join(c.strip() for c in r if c.strip()) for r in rows[:hi] if any(c.strip() for c in r)]
        if skipped:
            meta["skipped_rows"] = skipped[:5]
            w.append("skipped %d title/notes row(s) above the header: %s" % (len(skipped), "; ".join(s[:60] for s in skipped[:3])))
    if headerless:
        first_data = hi
        width = max(len(r) for r in rows[hi:hi + 200])
        header = ["col_%d" % (j + 1) for j in range(width)]
        w.append("no header row found (the first row is all numbers or dates); columns are named col_1, col_2, ...")
    else:
        first_data = hi + 1
        header = [c.strip() for c in rows[hi]]
    body_src = rows[first_data:]
    nums = array("i")
    body = []
    wmax = len(header)
    blank = 0
    for off, r in enumerate(body_src):
        last = len(r)
        while last and not r[last - 1].strip():
            last -= 1
        if last == 0:
            blank += 1
            continue
        if last > wmax:
            wmax = last
        body.append(r)
        nums.append(first_data + off + 1)
    if max_rows and len(body) > max_rows:
        body, nums = body[:max_rows], nums[:max_rows]
        meta["truncated"] = True
    if wmax > len(header):
        header += ["col_%d" % (j + 1) for j in range(len(header), wmax)]
    width = len(header)
    ragged = 0
    for k, r in enumerate(body):
        if len(r) != width:
            ragged += 1
            body[k] = (r + [""] * (width - len(r))) if len(r) < width else r[:width]
    if ragged >= 3 and ragged > 0.05 * len(body):
        w.append("%d rows had a different number of cells than the header (padded with blanks); check the delimiter." % ragged)
    # names: blank -> col_N, duplicates -> _2
    seen = {}
    auto = set()
    for j, h in enumerate(header):
        if not h:
            h = "col_%d" % (j + 1)
            auto.add(j)
        if h in seen:
            seen[h] += 1
            h = "%s_%d" % (h, seen[h])
        else:
            seen[h] = 1
        header[j] = h
    # drop columns with no header and no data
    keep = [j for j in range(width) if not (j in auto and all(not r[j].strip() for r in body))]
    if len(keep) < width:
        meta["dropped_columns"] = [j + 1 for j in range(width) if j not in keep]
        w.append("dropped %d empty column(s) without a header" % (width - len(keep)))
        header = [header[j] for j in keep]
        body = [[r[j] for j in keep] for r in body]
    meta.update(header_row=hi + 1 if not headerless else 0, blank_rows=blank, source_rows=len(body_src))
    if blank:
        meta["notes"] = meta.get("notes", []) + ["%d blank row(s) removed" % blank]

    infos = [P.infer_column(h, [r[j] for r in body], date_order) for j, h in enumerate(header)]
    totals = _find_totals(body, infos)
    if totals:
        meta["totals"] = [{"source_row": nums[k], "position": k, "how": v[0], "detail": v[1],
                           "cells": [c.strip() for c in body[k]]} for k, v in sorted(totals.items())]
        if keep_totals:
            meta["totals_kept"] = True
            w.append("%d total row(s) found and KEPT (they will double count in sums)" % len(totals))
        else:
            gone = set(totals)
            body = [r for k, r in enumerate(body) if k not in gone]
            nums = array("i", (n for k, n in enumerate(nums) if k not in gone))
            infos = [P.infer_column(h, [r[j] for r in body], date_order) for j, h in enumerate(header)]
            w.append("excluded %d total/subtotal row(s) so they do not double count (source rows %s)" % (
                len(totals), ", ".join(str(t["source_row"]) for t in meta["totals"][:6])))
    if meta.get("truncated"):
        w.append("only the first %d data rows were read (file is larger); results describe that part only" % len(body))
    for i in infos:
        if i["type"] in ("date", "datetime") and i["date_order"] in ("ambiguous", "mixed"):
            meta.setdefault("ambiguous_dates", {})[i["name"]] = i["date_order"]
            w.append("dates in '%s' are ambiguous (%s). Ask which is right, then re-run with --date-order dmy or mdy; "
                     "until then those dates are not parsed." % (i["name"], i["date_reason"]))
        if i["sep_ambiguous"]:
            w.append("numbers in '%s' use one separator (like 1,234) that could be a thousands or a decimal mark; read as thousands. "
                     "Check it." % i["name"])
        if i["bad_count"]:
            w.append("'%s' is %s but %d cell(s) are not (e.g. %s); those are ignored." % (
                i["name"], i["type"], i["bad_count"], ", ".join(repr(b[:20]) for b in i["bad"][:3])))
    return Table(header, body, infos, meta, nums)


def read_table(path, sheet=None, date_order=None, keep_totals=False, max_rows=None):
    """Read CSV/TSV/JSON/NDJSON/XLSX (path or '-' for stdin) into a Table. Raises DataError with a plain message."""
    meta = {"source": path, "warnings": [], "sheets": [], "sheet": None, "encoding": None, "delimiter": None,
            "format": None, "truncated": False, "skipped_rows": [], "totals": [], "merged_cells": 0, "dropped_columns": []}
    cap = (max_rows + 60) if max_rows else None
    rows = _load(path, sheet, cap, meta)
    rows = [[("" if c is None else str(c)) for c in r] for r in rows] if meta["format"] == "json" else rows
    if not rows:
        raise DataError("no data found: the file has no rows")
    return _assemble(rows, meta, date_order, keep_totals, max_rows)


# ------------------------------------------------------------------ CLI
def _example(col_vals):
    for v in col_vals:
        if v.strip():
            return v.strip()[:24]
    return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="CSV, TSV, JSON, NDJSON or XLSX path, or - for stdin")
    ap.add_argument("--sheet", help="sheet name or number (XLSX)")
    ap.add_argument("--date-order", choices=["dmy", "mdy"], help="resolve ambiguous 03/04/2026 style dates")
    ap.add_argument("--keep-totals", action="store_true", help="do not drop rows recognised as totals")
    ap.add_argument("--max-rows", type=int, help="read only the first N data rows")
    ap.add_argument("--head", type=int, default=5, help="rows to preview (default 5)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    try:
        t = read_table(a.file, a.sheet, a.date_order, a.keep_totals, a.max_rows)
    except DataError as ex:
        print("error: %s" % ex, file=sys.stderr)
        return 2
    if a.json:
        out = {"rows": t.n_rows, "columns": [{"name": h, "type": i["type"], "nulls": i["nulls"], "unique": i["unique"]}
                                             for h, i in zip(t.header, t.infos)],
               "meta": {k: v for k, v in t.meta.items() if k != "notes"}, "head": t.rows[:a.head]}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    m = t.meta
    print("%s: %d rows x %d columns (%s%s%s)" % (
        m["source"], t.n_rows, t.n_cols, m["format"],
        ", " + m["encoding"] if m.get("encoding") else "",
        ", delimiter " + DELIMS.get(m["delimiter"], repr(m["delimiter"])) if m.get("delimiter") else ""))
    if m.get("sheet"):
        print("sheet: %s" % m["sheet"])
    print("%-4s %-28s %-9s %6s %7s  %s" % ("#", "column", "type", "nulls", "unique", "example"))
    for j, (h, i) in enumerate(zip(t.header, t.infos)):
        print("%-4d %-28s %-9s %6d %7d  %s" % (j, h[:28], i["type"], i["nulls"], i["unique"], _example(t.raw(j))))
    for msg in m["warnings"]:
        print("warning: %s" % msg)
    if a.head > 0 and t.rows:
        print("first %d rows:" % min(a.head, t.n_rows))
        for r in t.rows[:a.head]:
            print("  " + " | ".join(c.strip()[:18] for c in r))
    return 0


if __name__ == "__main__":
    sys.exit(main())

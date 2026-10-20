"""Internal: read an .xlsx workbook with zipfile + XML only. Used by dataio.py, not a command.

Returns cell text rows. Dates (by number format) come out as ISO text, percent formats as "12%",
currency formats with their symbol, so the normal type inference sees what a person sees in Excel.
Merged cells are ignored (only the top-left cell holds a value); formulas give their cached value.
"""
from __future__ import annotations

import datetime as dt
import re
import zipfile
import xml.etree.ElementTree as ET

REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
DATE_IDS = set(range(14, 18)) | {22} | set(range(27, 37)) | set(range(50, 59))
TIME_IDS = {18, 19, 20, 21, 45, 46, 47}
PERCENT_IDS = {9, 10}


def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _classify_format(code: str) -> str:
    raw = code
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code).lower()
    if "y" in code or "d" in code:
        return "datetime" if ("h" in code or "s" in code) else "date"
    if "h" in code or "s" in code:
        return "time"
    if "m" in code and "0" not in code and "#" not in code:
        return "date"
    if "%" in code:
        return "percent"
    for sym in "$€£¥":
        if sym in raw:
            return "cur:" + sym
    return ""


def _styles(z, names):
    """style index -> kind ('date', 'datetime', 'time', 'percent', 'cur:$', '')."""
    if "xl/styles.xml" not in names:
        return []
    root = ET.fromstring(z.read("xl/styles.xml"))
    codes = {}
    xfs = []
    for el in root.iter():
        ln = _ln(el.tag)
        if ln == "numFmt":
            codes[int(el.get("numFmtId"))] = el.get("formatCode") or ""
        elif ln == "cellXfs":
            xfs = [int(x.get("numFmtId") or 0) for x in el if _ln(x.tag) == "xf"]
    out = []
    for fid in xfs:
        if fid in codes:
            out.append(_classify_format(codes[fid]))
        elif fid in DATE_IDS:
            out.append("date" if fid != 22 else "datetime")
        elif fid in TIME_IDS:
            out.append("time")
        elif fid in PERCENT_IDS:
            out.append("percent")
        else:
            out.append("")
    return out


def _shared_strings(z, names):
    if "xl/sharedStrings.xml" not in names:
        return []
    out = []
    with z.open("xl/sharedStrings.xml") as fh:
        for _, el in ET.iterparse(fh, events=("end",)):
            if _ln(el.tag) == "si":
                parts = []
                for ch in el:
                    ln = _ln(ch.tag)
                    if ln == "t":
                        parts.append(ch.text or "")
                    elif ln == "r":
                        parts.extend((t.text or "") for t in ch if _ln(t.tag) == "t")
                out.append("".join(parts))
                el.clear()
    return out


def serial_to_text(x: float, kind: str, epoch1904: bool) -> str:
    days = int(x // 1)
    secs = int(round((x - days) * 86400))
    if secs >= 86400:
        days, secs = days + 1, 0
    if kind == "time":
        return "%02d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)
    if epoch1904:
        base = dt.datetime(1904, 1, 1)
    else:
        base = dt.datetime(1899, 12, 31) if x < 60 else dt.datetime(1899, 12, 30)
    d = base + dt.timedelta(days=days, seconds=secs)
    if secs == 0:
        return d.strftime("%Y-%m-%d")
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _num_text(text: str) -> str:
    try:
        x = float(text)
    except ValueError:
        return text
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return repr(float("%.15g" % x))


def _cell_text(kind_t, v, style_kind, shared, epoch1904, inline):
    if kind_t == "s":
        try:
            return shared[int(v)]
        except (ValueError, IndexError, TypeError):
            return ""
    if kind_t == "inlineStr":
        return inline or ""
    if kind_t == "d":
        return (v or "")[:19].replace("T", " ")
    if kind_t == "b":
        return "TRUE" if v == "1" else "FALSE"
    if kind_t in ("str", "e"):
        return v or ""
    if v is None or v == "":
        return ""
    try:
        x = float(v)
    except ValueError:
        return v
    if style_kind in ("date", "datetime", "time"):
        return serial_to_text(x, style_kind, epoch1904)
    if style_kind == "percent":
        return ("%.10g" % (x * 100)) + "%"
    if style_kind.startswith("cur:"):
        return ("-" if x < 0 else "") + style_kind[4:] + _num_text(str(abs(x)))
    return _num_text(v)


def list_sheets(path):
    """Return (sheets, date1904) where sheets = [(name, zip_path, state)]."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("this zip file is not an Excel workbook (no xl/workbook.xml)")
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for el in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                t = el.get("Target") or ""
                rels[el.get("Id")] = t.lstrip("/") if t.startswith("/") else "xl/" + t
        sheets = []
        d1904 = False
        for el in wb.iter():
            ln = _ln(el.tag)
            if ln == "workbookPr":
                d1904 = (el.get("date1904") or "").lower() in ("1", "true")
            elif ln == "sheet":
                sheets.append((el.get("name"), rels.get(el.get(REL_ID), ""), el.get("state") or "visible"))
    return sheets, d1904


def read_sheet(path, zip_path, date1904=False, max_rows=None):
    """Return (rows, merged_count). rows are lists of text."""
    rows = []
    merged = 0
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if zip_path not in names:
            raise ValueError("sheet data not found in workbook")
        styles = _styles(z, names)
        shared = _shared_strings(z, names)
        cells = {}
        cur = None
        last_row = 0
        with z.open(zip_path) as fh:
            for _, el in ET.iterparse(fh, events=("end",)):
                ln = _ln(el.tag)
                if ln == "c":
                    ref = el.get("r") or ""
                    idx = _col_index(ref) if ref else (max(cells) + 1 if cells else 0)
                    v = inline = None
                    for ch in el:
                        cl = _ln(ch.tag)
                        if cl == "v":
                            v = ch.text
                        elif cl == "is":
                            inline = "".join((t.text or "") for t in ch.iter() if _ln(t.tag) == "t")
                    s = el.get("s")
                    sk = styles[int(s)] if s is not None and s.isdigit() and int(s) < len(styles) else ""
                    cells[idx] = _cell_text(el.get("t"), v, sk, shared, date1904, inline)
                elif ln == "row":
                    rn = int(el.get("r") or last_row + 1)
                    gap = 0
                    while last_row + 1 < rn and gap < 1000:          # keep blank rows so row numbers match Excel
                        rows.append([])
                        last_row += 1
                        gap += 1
                    last_row = rn
                    if cells:
                        width = max(cells) + 1
                        rows.append([cells.get(i, "") for i in range(width)])
                    else:
                        rows.append([])
                    cells = {}
                    el.clear()
                    if max_rows and len(rows) >= max_rows + 60:
                        break
                elif ln == "mergeCell":
                    merged += 1
    return rows, merged

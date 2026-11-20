#!/usr/bin/env python3
"""既存の関数書き出しから検索用索引を作る（IDA 不要）。

kinds は疑似コードの形の上の推定。確かめは関数を読む。
"""
from __future__ import annotations

import argparse
import csv
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from itertools import islice

DATA = re.compile(r"^(0x[0-9a-fA-F]+)\s+(.*?)\s*size=")
SECTION = re.compile(r"^\[([^\]]+)\]$", re.MULTILINE)
TOKEN = re.compile(r"\b\w+\b")
MEMBER = re.compile(r"\s*(?:\.|->)\s*[A-Za-z_]\w*")
BRACKET = re.compile(r"\s*\[")
ASSIGN = re.compile(r"\s*(?:<<=|>>=|[+\-*/%&|^]=|=(?!=)|\+\+|--)")
# C の文字列・コメントに含まれる名前は使用と数えない。
NON_CODE = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.DOTALL)


def _write_at(code: str, start: int, end: int) -> bool:
    before = start
    while before and code[before - 1].isspace():
        before -= 1
    if code[max(0, before - 2):before] in ("++", "--"):
        return True
    # メンバと添字を辿る。解釈できない式は read に倒す。
    pos = end
    while True:
        member = MEMBER.match(code, pos)
        if member:
            pos = member.end()
            continue
        bracket = BRACKET.match(code, pos)
        if bracket:
            pos = bracket.end()
            depth = 1
            while pos < len(code) and depth:
                depth += (code[pos] == "[") - (code[pos] == "]")
                pos += 1
            if depth:
                return False
            continue
        break
    return bool(ASSIGN.match(code, pos))


def classify(code: str, names: set[str]) -> dict[str, str]:
    if not names:
        return {}
    code = NON_CODE.sub(lambda m: " " * len(m.group()), code)
    found: dict[str, set[str]] = {}
    for match in TOKEN.finditer(code):
        name = match.group()
        if name in names:
            kind = "write" if _write_at(code, *match.span()) else "read"
            found.setdefault(name, set()).add(kind)
    return {name: ",".join(k for k in ("write", "read") if k in kinds)
            for name, kinds in found.items()}


def _function_texts(source: Path, functions):
    # 小ファイルを並行して先読みする。順番を保ち、一度に保持する量を制限する。
    def read(func):
        return (source / func["file"]).read_text(encoding="utf-8-sig")

    with ThreadPoolExecutor(max_workers=32) as pool:
        while batch := list(islice(functions, 256)):
            yield from zip(batch, pool.map(read, batch))


def build_indexes(export_dir: Path | str, output_dir: Path | str | None = None) -> dict:
    """index.txt の全関数を読む。output_dir 指定時は入力側に書かない。"""
    source = Path(export_dir)
    output = Path(output_dir) if output_dir is not None else source
    started = time.monotonic()
    lines = (source / "index.txt").read_text(encoding="utf-8-sig").splitlines()
    header_at = next(i for i, line in enumerate(lines) if line.startswith("name\taddress\tfile\t"))
    metadata = {line.split(":", 1)[0]: line for line in lines[1:header_at] if ":" in line}
    preamble = "\n".join([lines[0], metadata["symbols_commit"], metadata["idb_input_sha256"]]) + "\n"
    functions = csv.DictReader(lines[header_at:], delimiter="\t")
    output.mkdir(parents=True, exist_ok=True)
    # 不完全な生成物を既存の索引として見せない。
    all_tmp = output / "all_functions.txt.tmp"
    xref_tmp = output / "xrefs.tsv.tmp"
    rows = []
    count = 0
    try:
        with all_tmp.open("w", encoding="utf-8", newline="\n") as merged:
            merged.write(preamble)
            for func, text in _function_texts(source, functions):
                body = text.partition("\n")[2]
                merged.write("===== %s %s =====\n" % (func["address"], func["name"]))
                merged.write(body)
                if not body.endswith("\n"):
                    merged.write("\n")
                sections = {}
                matches = list(SECTION.finditer(body))
                for i, match in enumerate(matches):
                    end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
                    sections[match.group(1)] = body[match.end():end]
                targets = {}
                for line in sections.get("referenced data and strings", "").splitlines():
                    match = DATA.match(line)
                    if match:
                        addr, name = match.groups()
                        targets[int(addr, 16)] = (addr, name.strip())
                kinds = classify(sections.get("pseudocode", ""), {n for _, n in targets.values() if n})
                for target, (addr, name) in targets.items():
                    rows.append((target, int(func["address"], 16), addr, name or addr,
                                 func["address"], func["name"], kinds.get(name, "ref")))
                count += 1
        with xref_tmp.open("w", encoding="utf-8", newline="") as xrefs:
            xrefs.write(preamble)
            writer = csv.writer(xrefs, delimiter="\t", lineterminator="\n")
            writer.writerow(["target_addr", "target_name", "func_addr", "func_name", "kinds"])
            for row in sorted(rows, key=lambda r: (r[0], r[1])):
                writer.writerow(row[2:])
        all_tmp.replace(output / "all_functions.txt")
        xref_tmp.replace(output / "xrefs.tsv")
    finally:
        all_tmp.unlink(missing_ok=True)
        xref_tmp.unlink(missing_ok=True)
    return {"functions": count, "xrefs": len(rows), "seconds": round(time.monotonic() - started, 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_dir", type=Path)
    parser.add_argument("--output-dir", type=Path, help="生成先（省略時は書き出し元）")
    args = parser.parse_args()
    result = build_indexes(args.export_dir, args.output_dir)
    print("{functions} functions, {xrefs} xrefs, {seconds} seconds".format(**result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

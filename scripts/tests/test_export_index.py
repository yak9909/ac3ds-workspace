import csv
import io

import pytest

from export_index import build_indexes, classify

NOTE = "生成物。編集しない（orch.py export が作り直す）"


@pytest.mark.parametrize("code,expected", [
    ("x = 1;", "write"), ("x |= 4;", "write"), ("x[2] = 0;", "write"),
    ("++x;", "write"), ("x++; --x; x--;", "write"),
    ("if (x == 3)", "read"), ("y = x & 8;", "read"),
    ("x = x + 1;", "write,read"), ("y = 1;", "ref"), ("x2 = 1;", "ref"),
    ("x.field = 0;", "write"), ("x[i].field += 1;", "write"),
    ("x <<= 2; x >>= 1; x &= 1; x ^= 2; x -= 1; x *= 2; x /= 2; x %= 2;", "write"),
    ('puts("x = 1"); /* x */ // x\n', "ref"),
    ("if (x != 3 && x <= 4 && x >= 0) {}", "read"),
    ("x[x] = 1;", "write,read"),
])
def test_kinds(code, expected):
    assert classify(code, {"x"}).get("x", "ref") == expected


def fixture_export(tmp_path):
    (tmp_path / "index.txt").write_text(
        NOTE + "\nidb_input_sha256: hash\nsymbols_commit: commit\nmode: partial\n"
        "name\taddress\tfile\tdecompile\tsymbols_commit\n"
        "later\t0x00200000\tlater.txt\tok\tcommit\n"
        "first\t0x00100000\tfirst.txt\tok\tcommit\n", encoding="utf-8")
    for filename, address, name, code in [
        ("later.txt", "0x00200000", "later", "x = 1; y = x;"),
        ("first.txt", "0x00100000", "first", "x2 = 2;"),
    ]:
        (tmp_path / filename).write_text(
            NOTE + f"\n{address} {name}\n[disassembly]\nx = 0;\n"
            "[referenced data and strings]\n"
            "0x00949D68 x size=4 type=int\n"
            "0x00949D68 x size=4 type=int\n"
            "0x0010007A  size=2 type=short\n"
            f"[pseudocode]\n{code}\n", encoding="utf-8")
    return tmp_path


def test_outputs(tmp_path):
    source = fixture_export(tmp_path)
    out = tmp_path / "output"
    result = build_indexes(source, out)
    assert result["functions"] == 2 and result["xrefs"] == 4
    text = (out / "xrefs.tsv").read_text(encoding="utf-8")
    preamble = NOTE + "\nsymbols_commit: commit\nidb_input_sha256: hash\n"
    assert text.startswith(preamble)
    rows = list(csv.DictReader(io.StringIO(text[len(preamble):]), delimiter="\t"))
    assert [(r["target_addr"], r["func_addr"]) for r in rows] == [
        ("0x0010007A", "0x00100000"), ("0x0010007A", "0x00200000"),
        ("0x00949D68", "0x00100000"), ("0x00949D68", "0x00200000")]
    assert rows[0]["target_name"] == "0x0010007A" and rows[0]["kinds"] == "ref"
    assert rows[2]["kinds"] == "ref" and rows[3]["kinds"] == "write,read"
    merged = (out / "all_functions.txt").read_text(encoding="utf-8")
    assert merged.startswith(preamble) and merged.count(NOTE) == 1
    assert merged.index("===== 0x00200000 later =====") < merged.index("===== 0x00100000 first =====")
    assert not (source / "all_functions.txt").exists()
    (source / "first.txt").write_text(NOTE + "\nchanged\n", encoding="utf-8")
    build_indexes(source, out)
    assert "changed" in (out / "all_functions.txt").read_text(encoding="utf-8")

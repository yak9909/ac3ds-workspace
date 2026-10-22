"""選択したゲームのシンボルを C++17 のヘッダーに書き出す。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import tomllib
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
IDENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")
WORDS = re.compile(r"\b[A-Za-z_][A-Za-z_0-9]*\b")
IDA_INTEGER = re.compile(r"\b(?:(signed|unsigned)\s+)?__int(8|16|32|64)\b")
TYPE_MARKER = re.compile(r"^// type: ([A-Za-z_][A-Za-z_0-9]*)\s*$", re.M)
FORBIDDEN = re.compile(r"\b(?:ACNL|HHD)-[A-Z]\d+\b|ac3ds-re|symbols[/\\]", re.I)
CPP_KEYWORDS = set("""alignas alignof and and_eq asm auto bitand bitor bool break case catch
char char16_t char32_t class compl const constexpr const_cast continue decltype default
delete do double dynamic_cast else enum explicit export extern false float for friend goto
if inline int long mutable namespace new noexcept not not_eq nullptr operator or or_eq
private protected public register reinterpret_cast return short signed sizeof static
static_assert static_cast struct switch template this thread_local throw true try typedef
typeid typename union unsigned using virtual void volatile wchar_t while xor xor_eq""".split())


def require_names(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not IDENT.fullmatch(x) for x in value):
        raise ValueError(f"{label} は名前の配列にしてください")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} に重複があります")
    return value


def read_config(repo: Path, config_path: Path | None = None) -> tuple[str, Path, dict]:
    # --config の相対パスは CLI の通常の規則どおり、現在のディレクトリを基準にする。
    config = tomllib.loads((config_path if config_path is not None else repo / "game_api.toml").read_text(encoding="utf-8"))
    required = {"build", "output", "functions", "globals", "types"}
    if not required <= set(config) or set(config) - required - {"namespace", "hooks", "checks"}:
        raise ValueError("一覧の項目は build, output, functions, globals, types（任意: namespace, hooks, checks）にしてください")
    build, output = config["build"], config["output"]
    if not isinstance(build, str) or not IDENT.fullmatch(build):
        raise ValueError("build が不正です")
    if not isinstance(output, str) or not output or Path(output).is_absolute():
        raise ValueError("output はリポジトリ内の相対パスにしてください")
    destination = (repo / output).resolve()
    if not destination.is_relative_to(repo.resolve()):
        raise ValueError("output はリポジトリ内にしてください")
    names = {key: require_names(config[key], key) for key in ("functions", "globals", "types")}
    namespace = config.get("namespace", "")
    if not isinstance(namespace, str) or (namespace and any(
            not IDENT.fullmatch(part) or part in CPP_KEYWORDS for part in namespace.split("::"))):
        raise ValueError("namespace は C++ の名前空間名にしてください（入れ子は ::）")
    names["namespace"] = namespace
    used = set(names["functions"] + names["globals"])
    for label in ("hooks", "checks"):
        entries = config.get(label, [])
        if not isinstance(entries, list):
            raise ValueError(f"{label} はテーブルの配列にしてください")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"name", "function", "offset", "length"}:
                raise ValueError(f"{label} の項目は name, function, offset, length にしてください")
            for field in ("name", "function"):
                if not isinstance(entry[field], str) or not IDENT.fullmatch(entry[field]):
                    raise ValueError(f"{label}.{field} が不正です")
            if entry["name"] in used:
                raise ValueError(f"重複した出力名: {entry['name']}")
            used.add(entry["name"])
            if type(entry["offset"]) is not int or entry["offset"] < 0:
                raise ValueError(f"{label}.offset は非負の整数にしてください")
            if type(entry["length"]) is not int or not 4 <= entry["length"] <= 64:
                raise ValueError(f"{label}.length は 4〜64 の整数にしてください")
        names[label] = entries
    return build, destination, names


def code_image(build: str, info: dict) -> bytes:
    """展開場所の違いと base/update の同居を、ビルドのハッシュで解決する。"""
    folder = ROOT / "_local/builds" / build
    paths = sorted(folder.rglob("code.bin"))
    if not paths:
        raise ValueError(f"code.bin がありません: {build}")
    for path in paths:
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest().lower() == info["code_sha256"].lower():
            return data
    raise ValueError(f"code.bin の SHA256 がビルドと一致しません: {build}")


def code_layout(symbols: Path, info: dict) -> tuple[int, list[tuple[int, int]]]:
    meta_path = symbols / "meta.toml"
    meta = tomllib.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    segments = meta.get("segments", info.get("segments", {}))

    def number(value: int | str) -> int:
        return int(value, 0) if isinstance(value, str) else int(value)

    if "text" in segments:
        ranges = sorted((number(bounds[0]), number(bounds[1]))
                        for name, bounds in segments.items() if name != "bss")
        return number(segments["text"][0]), ranges
    # セグメントが無い古い書き出しでは、配置済みイメージの開始を仮定する。
    print("警告: 配置情報が無いため code.bin は仮想番地 0x00100000 から始まると仮定します", file=sys.stderr)
    return 0x00100000, []


def check_lines(entry: dict, functions: dict, data: bytes, base: int,
                ranges: list[tuple[int, int]]) -> list[str]:
    name, function = entry["name"], entry["function"]
    if function not in functions:
        raise ValueError(f"見つからない関数: {function}")
    row = functions[function]
    start = int(row["address"], 0)
    offset, length = entry["offset"], entry["length"]
    if start < 0 or offset + length > int(row["size"], 0):
        raise ValueError(f"関数の範囲外: {name}")
    addr = (start & ~1) + offset
    end, position = addr + length, addr - base
    if position < 0 or position + length > len(data):
        raise ValueError(f"code.bin の範囲外: {name}")
    if ranges:
        cursor = addr
        for begin, stop in ranges:
            if begin <= cursor < stop:
                cursor = min(end, stop)
                if cursor == end:
                    break
        if cursor != end:
            raise ValueError(f"code.bin のセグメント範囲外: {name}")
    crc = zlib.crc32(data[position:position + length])
    return [f"inline constexpr std::uintptr_t {name}_addr = 0x{addr:08X};",
            f"inline constexpr std::uint32_t {name}_len = {length};",
            f"inline constexpr std::uint32_t {name}_crc32 = 0x{crc:08X};"]


def read_symbols(folder: Path) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for csv_path in sorted(folder.glob("*.csv")):
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                name = row["name"]
                if not name:
                    continue
                if name in found:
                    raise ValueError(f"重複した名前: {name}")
                found[name] = row
    return found


def strip_comments(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


def ida_integer(match: re.Match[str]) -> str:
    # signed と前置きなしの __intN は符号付きの型、unsigned __intN は符号なしの型にする。
    sign = "u" if match.group(1) == "unsigned" else ""
    return f"std::{sign}int{match.group(2)}_t"


def cpp_syntax(code: str) -> str:
    code = strip_comments(code)
    code = IDA_INTEGER.sub(ida_integer, code)
    code = re.sub(r"\b__(?:fastcall|cdecl|stdcall|thiscall|bitmask)\b", "", code)
    code = re.sub(r"\b(?:uint8_t|uint16_t|uint32_t|uint64_t|int8_t|int16_t|int32_t|int64_t)\b",
                  lambda m: "std::" + m.group() if code[max(0, m.start()-5):m.start()] != "std::" else m.group(), code)
    return code


ENUM_DEFINITION = re.compile(r"\benum\s+([A-Za-z_][A-Za-z_0-9]*)(?:\s*:\s*([^{};]+?))?(\s*)\{")
ENUM_ANONYMOUS = re.compile(r"\benum\s*(?:\{|:)")
ENUM_BASE = re.compile(r"std::(u?)int(8|16|32|64)_t")
ENUM_VALUE = re.compile(r"=\s*(-?)\s*(0[xX][0-9A-Fa-f]+|\d+)")


def fixed_enums(code: str, owner: str) -> str:
    """enum に IDA と同じ大きさの固定の基底型を付け、直後に sizeof の static_assert を置く。

    IDA の enum は宣言に大きさの指定が無ければ 4 バイト。大きさを指定しない C++ の enum は
    値の範囲に合わせて 1〜2 バイトになり得るので（devkitARM は short enums）、
    構造体の欄の位置が静かにずれる。そのため全ての enum に基底型を明示する。
    """
    if ENUM_ANONYMOUS.search(code):
        raise ValueError(f"名前の無い enum は大きさを決められません: {owner}")
    pieces: list[str] = []
    cursor = 0
    for match in ENUM_DEFINITION.finditer(code):
        if match.start() < cursor:
            continue
        name, declared, space = match.groups()
        close = re.compile(r"\}\s*;").search(code, match.end())
        if not close:
            raise ValueError(f"enum の終わりがありません: {name}")
        if declared is None:
            values = [(sign, int(digits, 0)) for sign, digits in ENUM_VALUE.findall(code[match.end():close.start()])]
            if any(sign for sign, _ in values):
                if any(not sign and number > 0x7FFFFFFF for sign, number in values):
                    raise ValueError(f"負の値と 0x7FFFFFFF を超える値を持つ enum: {name}")
                base, size = "std::int32_t", 4
            else:
                base, size = "std::uint32_t", 4
        else:
            declared = declared.strip()
            if not ENUM_BASE.fullmatch(declared):
                raise ValueError(f"解釈できない enum の基底型: {name} : {declared}")
            base, size = declared, int(ENUM_BASE.fullmatch(declared).group(2)) // 8
        pieces.append(code[cursor:match.start()])
        pieces.append(f"enum {name} : {base}{space}{{" + code[match.end():close.end()]
                      + f"\nstatic_assert(sizeof({name}) == {size});")
        cursor = close.end()
    pieces.append(code[cursor:])
    return "".join(pieces)


def read_types(folder: Path) -> tuple[dict[str, str], dict[str, str]]:
    definitions: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for header in sorted(folder.glob("*.h")):
        source = header.read_text(encoding="utf-8-sig")
        matches = list(TYPE_MARKER.finditer(source))
        for index, match in enumerate(matches):
            name = match.group(1)
            if name in definitions:
                raise ValueError(f"重複した型: {name}")
            end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            body = fixed_enums(cpp_syntax(source[match.end():end]).strip(), name)
            if not body:
                raise ValueError(f"空の型: {name}")
            definitions[name] = body
            kind = re.search(r"\b(struct|class|union|enum)\s+" + re.escape(name) + r"\b", body)
            if kind:
                kinds[name] = kind.group(1)
    return definitions, kinds


def dependencies(code: str, names: set[str], self_name: str) -> set[str]:
    return (set(WORDS.findall(code)) & names) - {self_name}


def selected_types(definitions: dict[str, str], kinds: dict[str, str], requested: list[str],
                   references: list[str]) -> list[str]:
    all_names = set(definitions)
    missing = set(requested) - all_names
    if missing:
        raise ValueError("見つからない型: " + ", ".join(sorted(missing)))
    needed = set(requested)
    for reference in references:
        needed.update(set(WORDS.findall(reference)) & all_names)
    pending = list(needed)
    while pending:
        current = pending.pop()
        for dependency in dependencies(definitions[current], all_names, current) - needed:
            needed.add(dependency)
            pending.append(dependency)
    # ポインタによる循環参照には前方宣言を用い、定義は元ファイルの依存順を保つ。
    order = [name for name in definitions if name in needed]
    return order


def address(row: dict[str, str]) -> str:
    value = int(row["address"], 0)
    if value < 0:
        raise ValueError("負の番地です")
    return f"0x{value:08X}"


def function_lines(name: str, row: dict[str, str]) -> list[str]:
    prototype = row["prototype"].strip()
    addr = address(row)
    if not prototype:
        print(f"警告: {name} にプロトタイプがありません", file=sys.stderr)
        return ["// プロトタイプ未設定",
                f"inline constexpr std::uintptr_t {name}_addr = {addr};"]
    prototype = cpp_syntax(prototype).strip()
    match = re.fullmatch(r"(.+?)\s*\((.*)\)", prototype, re.S)
    if not match:
        raise ValueError(f"解釈できないプロトタイプ: {name}")
    result, arguments = match.groups()
    return [f"using {name}_t = {result.strip()} (*)({arguments.strip()});",
            f"inline constexpr std::uintptr_t {name}_addr = {addr};"]


def generate(repo: Path, config_path: Path | None = None) -> Path:
    build, destination, wanted = read_config(repo, config_path)
    builds = tomllib.loads((ROOT / "ac3ds-re/meta/builds.toml").read_text(encoding="utf-8"))["builds"]
    if build not in builds or "code_sha256" not in builds[build]:
        raise ValueError(f"不明なビルド: {build}")
    symbols = ROOT / "ac3ds-re/symbols" / build
    if not symbols.is_dir():
        raise ValueError(f"symbols がないビルド: {build}")
    functions = read_symbols(symbols / "functions")
    globals_ = read_symbols(symbols / "globals")
    definitions, kinds = read_types(symbols / "types")
    for label, names, available in (("関数", wanted["functions"], functions),
                                    ("大域変数", wanted["globals"], globals_)):
        missing = set(names) - set(available)
        if missing:
            raise ValueError(f"見つからない{label}: " + ", ".join(sorted(missing)))
    references = [functions[name]["prototype"] for name in wanted["functions"]]
    references += [globals_[name]["type"] for name in wanted["globals"]]
    order = selected_types(definitions, kinds, wanted["types"], references)
    checks = wanted["hooks"] + wanted["checks"]
    verification = []
    if checks:
        data = code_image(build, builds[build])
        base, ranges = code_layout(symbols, builds[build])
        for entry in checks:
            verification.extend(check_lines(entry, functions, data, base, ranges) + [""])
    lines = ["// 生成物。編集しない（export_game_api.py が作り直す）。",
             f"// build: {build}", f"// code SHA256: {builds[build]['code_sha256']}",
             "#pragma once", "#include <cstdint>", ""]
    if wanted["namespace"]:
        lines.extend([f"namespace {wanted['namespace']} {{", ""])
    declarations = [f"{kinds[name]} {name};" for name in order if kinds.get(name) in ("struct", "class", "union")]
    if declarations:
        lines.extend(declarations + [""])
    for name in order:
        if name in ("uint8_t", "uint16_t", "uint32_t", "uint64_t", "int8_t", "int16_t", "int32_t", "int64_t"):
            continue  # <cstdint> の型を重ねて定義しない
        lines.extend([definitions[name], ""])
    for name in wanted["functions"]:
        lines.extend(function_lines(name, functions[name]) + [""])
    for name in wanted["globals"]:
        raw_type = globals_[name]["type"].strip()
        if not raw_type:
            raise ValueError(f"大域変数 {name} に型がありません")
        typ = cpp_syntax(raw_type).strip()
        lines.append(f"using {name}_t = {typ};")
        lines.append(f"inline constexpr std::uintptr_t {name}_addr = {address(globals_[name])};")
        lines.append("")
    lines.extend(verification)
    if wanted["namespace"]:
        lines.append(f"}} // namespace {wanted['namespace']}")
    result = "\n".join(lines).rstrip() + "\n"
    if FORBIDDEN.search(result):
        raise ValueError("生成したヘッダーに解析固有の情報が含まれています")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 改行は OS によらず LF に固定する（Windows の既定の CRLF だと、収録済みの物と全行が食い違う）。
    destination.write_bytes(result.encode("utf-8"))
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="game_api.toml を置いた派生リポジトリ")
    parser.add_argument("--config", type=Path, help="一覧のパス（既定: <repo>/game_api.toml）")
    args = parser.parse_args()
    try:
        print(generate(args.repo.resolve(), args.config))
    except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

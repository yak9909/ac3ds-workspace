import csv
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import export_game_api as api


class ExportGameApiTests(unittest.TestCase):
    def setUp(self):
        evidence = Path(__file__).resolve().parents[2] / "_local/evidence/ws-game-api-2"
        evidence.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=evidence)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "product"
        self.repo.mkdir()
        build = self.root / "ac3ds-re" / "symbols" / "sample"
        for part in ("functions", "globals", "types"):
            (build / part).mkdir(parents=True)
        meta = self.root / "ac3ds-re" / "meta"
        meta.mkdir()
        (meta / "builds.toml").write_text('[builds.sample]\ncode_sha256 = "' + "a" * 64 + '"\n', encoding="utf-8")
        self.write_csv(build / "functions" / "00100000.csv",
                       ["address", "size", "name", "prototype", "confidence", "source", "refs", "clone_of"],
                       [["0x00123457", "4", "ThumbFunc", "int __fastcall(Node *node)", "", "", "", ""],
                        ["0x00123460", "4", "UnknownFunc", "", "", "", "", ""]])
        self.write_csv(build / "globals" / "00100000.csv",
                       ["address", "size", "name", "type", "confidence", "source", "refs"],
                       [["0x00123480", "4", "GlobalNode", "Node *", "", "", ""]])
        (build / "types" / "local_types.h").write_text(
            "struct Leaf;\nstruct Node;\n// type: Leaf\nstruct Leaf { unsigned __int16 value; };\n"
            "// type: Node\n#pragma pack(push, 2)\nstruct Node { Leaf leaf; }; "
            "// ACNL-F0412 symbols\\sample ac3ds-re\n#pragma pack(pop)\n", encoding="utf-8")
        self.config()
        self.root_patch = patch.object(api, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.symbols = build
        self.code = bytes(range(128))
        self.code_folder = self.root / "_local/builds/sample"
        self.code_folder.mkdir(parents=True)
        (self.code_folder / "code.bin").write_bytes(self.code)
        (meta / "builds.toml").write_text(
            '[builds.sample]\ncode_sha256 = "' + hashlib.sha256(self.code).hexdigest() + '"\n'
            '[builds.sample.segments]\ntext = ["0x00123450", "0x001234D0"]\n', encoding="utf-8")

    @staticmethod
    def write_csv(path, fields, rows):
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)

    def config(self, functions='["ThumbFunc", "UnknownFunc"]', types='["Node"]'):
        (self.repo / "game_api.toml").write_text(
            'build = "sample"\noutput = "include/game_api.h"\n'
            f'functions = {functions}\nglobals = ["GlobalNode"]\ntypes = {types}\n', encoding="utf-8")

    def test_export_and_dependencies(self):
        with patch("sys.stderr") as stderr:
            output = api.generate(self.repo)
        content = output.read_text(encoding="utf-8")
        self.assertIn("int (*)(Node *node)", content)
        self.assertIn("ThumbFunc_addr = 0x00123457", content)
        self.assertIn("// プロトタイプ未設定\ninline constexpr std::uintptr_t UnknownFunc_addr = 0x00123460;", content)
        self.assertNotIn("using UnknownFunc_t", content)
        self.assertIn("GlobalNode_t = Node *", content)
        self.assertLess(content.index("struct Leaf {"), content.index("struct Node {"))
        self.assertIn("std::uint16_t value", content)
        self.assertIn("code SHA256: " + hashlib.sha256(self.code).hexdigest(), content)
        self.assertNotIn("ACNL-F0412", content)
        self.assertNotIn("ac3ds-re", content)
        self.assertNotIn("symbols\\sample", content)
        self.assertTrue(stderr.write.called)

    def test_unknown_function_is_error_and_does_not_write(self):
        self.config(functions='["Missing"]')
        with self.assertRaisesRegex(ValueError, "見つからない関数: Missing"):
            api.generate(self.repo)
        self.assertFalse((self.repo / "include/game_api.h").exists())

    def test_unknown_type_is_error(self):
        self.config(types='["Missing"]')
        with self.assertRaisesRegex(ValueError, "見つからない型: Missing"):
            api.generate(self.repo)

    def test_unknown_global_is_error(self):
        text = (self.repo / "game_api.toml").read_text(encoding="utf-8")
        (self.repo / "game_api.toml").write_text(text.replace('globals = ["GlobalNode"]', 'globals = ["Missing"]'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "見つからない大域変数: Missing"):
            api.generate(self.repo)

    def test_output_cannot_escape_repo(self):
        text = (self.repo / "game_api.toml").read_text(encoding="utf-8")
        (self.repo / "game_api.toml").write_text(text.replace("include/game_api.h", "../outside.h"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "リポジトリ内"):
            api.generate(self.repo)

    def add_checks(self, extra='', function="ThumbFunc", offset=0, length=4, name="Entry", kind="hooks"):
        path = self.repo / "game_api.toml"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(extra + f'\n[[{kind}]]\nname = "{name}"\nfunction = "{function}"\n'
                         f'offset = {str(offset).lower()}\nlength = {str(length).lower()}\n')

    def test_config_namespace_hooks_and_checks(self):
        self.add_checks(extra='namespace = "game::api"\n')
        self.add_checks(function="UnknownFunc", name="Guard", kind="checks")
        spec = self.repo / "spec/game_api.toml"
        spec.parent.mkdir()
        (self.repo / "game_api.toml").rename(spec)
        output = api.generate(self.repo, spec)
        self.assertEqual(output, self.repo / "include/game_api.h")
        content = output.read_text(encoding="utf-8")
        self.assertIn("namespace game::api {", content)
        self.assertTrue(content.endswith("} // namespace game::api\n"))
        self.assertLess(content.index("namespace game::api {"), content.index("struct Leaf"))
        self.assertIn("ThumbFunc_addr = 0x00123457", content)
        self.assertIn("Entry_addr = 0x00123456", content)
        self.assertIn("Guard_addr = 0x00123460", content)
        for name, position in (("Entry", 0x00123456 - 0x00123450), ("Guard", 0x00123460 - 0x00123450)):
            self.assertIn(f"std::uint32_t {name}_len = 4;", content)
            self.assertIn(f"{name}_crc32 = 0x{zlib.crc32(self.code[position:position + 4]):08X};", content)
        self.assertNotIn("ac3ds-re", content)

    def test_offset_and_length_boundaries(self):
        path = self.symbols / "functions/00100000.csv"
        path.write_text(path.read_text(encoding="utf-8").replace("0x00123457,4,", "0x00123457,68,"), encoding="utf-8")
        self.add_checks(offset=4, length=64)
        content = api.generate(self.repo).read_text(encoding="utf-8")
        position = (0x00123457 & ~1) + 4 - 0x00123450
        self.assertIn(f"Entry_crc32 = 0x{zlib.crc32(self.code[position:position + 64]):08X};", content)

    def test_invalid_check_entries(self):
        for options, message in (({"function": "Missing"}, "見つからない関数"),
                                 ({"offset": -1}, "offset"), ({"offset": True}, "offset"),
                                 ({"offset": 1}, "関数の範囲外"),
                                 ({"length": 3}, "length"), ({"length": 65}, "length"),
                                 ({"length": True}, "length"), ({"name": "ThumbFunc"}, "重複"),
                                 ({"name": "bad-name"}, "name")):
            with self.subTest(options=options):
                self.config()
                self.add_checks(**options)
                with self.assertRaisesRegex(ValueError, message):
                    api.generate(self.repo)
                self.assertFalse((self.repo / "include/game_api.h").exists())

    def test_duplicate_hook_and_check_is_error(self):
        self.add_checks()
        self.add_checks(kind="checks")
        with self.assertRaisesRegex(ValueError, "重複"):
            api.generate(self.repo)

    def test_invalid_namespace(self):
        for namespace in ("game;oops", "class", "game::", "::game"):
            with self.subTest(namespace=namespace):
                self.config()
                self.add_checks(extra=f'namespace = "{namespace}"\n')
                with self.assertRaisesRegex(ValueError, "namespace"):
                    api.generate(self.repo)

    def test_missing_code_is_error_only_with_checks(self):
        (self.code_folder / "code.bin").unlink()
        content = api.generate(self.repo).read_text(encoding="utf-8")
        self.assertNotIn("namespace ", content)
        self.add_checks(kind="checks")
        with self.assertRaisesRegex(ValueError, "code.bin がありません"):
            api.generate(self.repo)
        self.assertEqual((self.repo / "include/game_api.h").read_text(encoding="utf-8"), content)

    def test_nested_code_selected_by_hash(self):
        update = self.code_folder / "extracted/update/exefs/code.bin"
        update.parent.mkdir(parents=True)
        (self.code_folder / "code.bin").rename(update)
        (self.code_folder / "code.bin").write_bytes(b"wrong build")
        self.add_checks()
        api.generate(self.repo)
        update.write_bytes(b"wrong update")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            api.generate(self.repo)

    def test_code_range_and_segment_gap_are_errors(self):
        self.add_checks()
        for text, message in ((["0x00123458", "0x001234D0"], "code.bin の範囲外"),
                              (["0x00123300", "0x001234D0"], "code.bin の範囲外"),
                              (["0x00123450", "0x00123458"], "セグメント範囲外")):
            with self.subTest(text=text):
                (self.symbols / "meta.toml").write_text(
                    f'[segments]\ntext = ["{text[0]}", "{text[1]}"]\n', encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    api.generate(self.repo)

    def test_missing_layout_warns_about_assumed_base(self):
        (self.root / "ac3ds-re/meta/builds.toml").write_text(
            '[builds.sample]\ncode_sha256 = "' + hashlib.sha256(self.code).hexdigest() + '"\n', encoding="utf-8")
        path = self.symbols / "functions/00100000.csv"
        path.write_text(path.read_text(encoding="utf-8").replace("0x00123457", "0x00100001"), encoding="utf-8")
        self.add_checks()
        with patch("sys.stderr") as stderr:
            content = api.generate(self.repo).read_text(encoding="utf-8")
        self.assertIn("Entry_addr = 0x00100000", content)
        self.assertIn("仮想番地 0x00100000", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_ida_integer_spellings_become_cstdint_names(self):
        for bits in ("8", "16", "32", "64"):
            with self.subTest(bits=bits):
                self.assertEqual(api.cpp_syntax(f"signed __int{bits} a"), f"std::int{bits}_t a")
                self.assertEqual(api.cpp_syntax(f"unsigned __int{bits} a"), f"std::uint{bits}_t a")
                self.assertEqual(api.cpp_syntax(f"__int{bits} a"), f"std::int{bits}_t a")
        self.assertEqual(api.cpp_syntax("int __fastcall(signed __int8 a, unsigned __int64 *b)"),
                         "int (std::int8_t a, std::uint64_t *b)")
        self.assertEqual(api.cpp_syntax("signed char c; my__int8 d;"), "signed char c; my__int8 d;")

    def test_ida_integers_in_types_and_prototypes_are_cpp(self):
        (self.symbols / "types/ints.h").write_text(
            "// type: Ints\nstruct Ints { signed __int8 a; unsigned __int8 b; __int16 c; "
            "signed __int32 d; unsigned __int64 e; };\n", encoding="utf-8")
        path = self.symbols / "functions/00100000.csv"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "int __fastcall(Node *node)", '"int __fastcall(signed __int8 a, unsigned __int64 *b)"'), encoding="utf-8")
        self.config(types='["Node", "Ints"]')
        content = api.generate(self.repo).read_text(encoding="utf-8")
        self.assertIn("struct Ints { std::int8_t a; std::uint8_t b; std::int16_t c; "
                      "std::int32_t d; std::uint64_t e; };", content)
        self.assertIn("using ThumbFunc_t = int (*)(std::int8_t a, std::uint64_t *b);", content)
        for leftover in ("signed std::", "unsigned std::", "__int"):
            self.assertNotIn(leftover, content)

    ENUM_TYPES = (
        "// type: Mode\nenum Mode\n{\n  Mode_A = 0x0,\n  Mode_B = 0x1,\n};\n"
        "// type: Flags\nenum __bitmask Flags\n{\n  Flags_X = 0x1,\n  Flags_Y = 0x2,\n};\n"
        "// type: Small\nenum Small : unsigned __int8\n{\n  Small_A = 0x0,\n  Small_B = 0x3,\n};\n"
        "// type: Half\nenum __bitmask Half : __int16\n{\n  Half_A = -1,\n};\n"
        "// type: Wide\nenum Wide : unsigned __int64\n{\n  Wide_A = 0x1,\n};\n"
        "// type: Signed\nenum Signed\n{\n  Signed_A = -1,\n  Signed_B = 0x7FFFFFFF,\n};\n"
        "// type: Big\nenum Big\n{\n  Big_A = 0xFFFFFFFF,\n};\n"
        "// type: Mix\n#pragma pack(push, 2)\nstruct Mix\n{\n  unsigned __int8 a;\n  Mode m;\n  Small s;\n"
        "  unsigned __int16 b;\n  Flags f;\n  Half h;\n  signed __int8 c;\n  Wide w;\n  Signed g;\n  Big z;\n};\n"
        "#pragma pack(pop)\n")
    ENUM_SIZES = {"Mode": 4, "Flags": 4, "Small": 1, "Half": 2, "Wide": 8, "Signed": 4, "Big": 4}
    MIX_FIELDS = [("a", 1), ("m", 4), ("s", 1), ("b", 2), ("f", 4), ("h", 2), ("c", 1), ("w", 8), ("g", 4), ("z", 4)]

    def enum_header(self):
        (self.symbols / "types/enums.h").write_text(self.ENUM_TYPES, encoding="utf-8")
        self.config(types='["Node", "Mix"]')
        return api.generate(self.repo)

    def test_enums_get_fixed_base_and_size_assert(self):
        content = self.enum_header().read_text(encoding="utf-8")
        for declaration in ("enum Mode : std::uint32_t\n{", "enum Flags : std::uint32_t\n{",
                            "enum Small : std::uint8_t\n{", "enum Half : std::int16_t\n{",
                            "enum Wide : std::uint64_t\n{", "enum Signed : std::int32_t\n{",
                            "enum Big : std::uint32_t\n{"):
            self.assertIn(declaration, content)
        for name, size in self.ENUM_SIZES.items():
            self.assertIn(f"}};\nstatic_assert(sizeof({name}) == {size});\n", content)
            self.assertLess(content.index(f"enum {name} :"), content.index(f"static_assert(sizeof({name})"))
        self.assertNotIn("__bitmask", content)
        self.assertEqual(content.count("static_assert"), len(self.ENUM_SIZES))

    def test_unusual_enums_are_errors(self):
        for body, message in (("enum Anon\n{\n  A = 1,\n};\nenum { B };\n", "名前の無い enum"),
                              ("enum Bad : unsigned long\n{\n  A = 1,\n};\n", "基底型"),
                              ("enum Mixed\n{\n  A = -1,\n  B = 0x80000000,\n};\n", "負の値")):
            with self.subTest(body=body):
                (self.symbols / "types/enums.h").write_text("// type: Bad\n" + body, encoding="utf-8")
                self.config(types='["Node", "Bad"]')
                with self.assertRaisesRegex(ValueError, message):
                    api.generate(self.repo)

    def test_output_uses_lf_line_endings(self):
        self.assertNotIn(b"\r", self.enum_header().read_bytes())

    @staticmethod
    def compilers():
        found = []
        for name, hint in (("arm-none-eabi-g++", "C:/devkitPro/devkitARM/bin"), ("g++", None), ("c++", None)):
            path = shutil.which(name) or (shutil.which(name, path=hint) if hint else None)
            if not path or os.path.normcase(os.path.realpath(path)) in [os.path.normcase(os.path.realpath(p)) for p in found]:
                continue
            # 壊れた環境のコンパイラ（PATH 不足で何も出さずに失敗する物）は使わない。
            probe = subprocess.run([path, "-x", "c++", "-fsyntax-only", "-"], input="int main() {}\n",
                                   capture_output=True, text=True, timeout=120)
            if probe.returncode == 0:
                found.append(path)
        return found

    def expected_offsets(self):
        # IDA の配置: 欄は自分の大きさに揃え、#pragma pack(2) で 2 を上限にする。enum は宣言の大きさ。
        offsets, position = {}, 0
        for field, size in self.MIX_FIELDS:
            align = min(size, 2)
            position = -(-position // align) * align
            offsets[field] = position
            position += size
        return offsets, -(-position // 2) * 2

    def layout_source(self):
        offsets, total = self.expected_offsets()
        asserts = [f"static_assert(offsetof(Mix, {field}) == {offset}, \"{field}\");" for field, offset in offsets.items()]
        return ('#include <cstddef>\n#include "enums_api.h"\nusing namespace gohan_test;\n'
                + "\n".join(asserts) + f"\nstatic_assert(sizeof(Mix) == {total}, \"Mix\");\n")

    def compile(self, compiler, header_text):
        folder = self.root / "compile"
        folder.mkdir(exist_ok=True)
        (folder / "enums_api.h").write_text(header_text, encoding="utf-8")
        (folder / "layout.cpp").write_text(self.layout_source(), encoding="utf-8")
        return subprocess.run([compiler, "-std=gnu++17", "-fsyntax-only", "-I", str(folder), str(folder / "layout.cpp")],
                              capture_output=True, text=True, timeout=120)

    def test_generated_enum_layout_matches_ida_with_real_compilers(self):
        (self.repo / "game_api.toml").write_text(
            'build = "sample"\noutput = "include/game_api.h"\nnamespace = "gohan_test"\n'
            'functions = []\nglobals = []\ntypes = ["Mix"]\n', encoding="utf-8")
        (self.symbols / "types/enums.h").write_text(self.ENUM_TYPES, encoding="utf-8")
        header = api.generate(self.repo).read_text(encoding="utf-8")
        compilers = self.compilers()
        if not compilers:
            self.skipTest("C++ コンパイラがありません")
        for compiler in compilers:
            with self.subTest(compiler=compiler):
                result = self.compile(compiler, header)
                self.assertEqual(result.returncode, 0, result.stderr)
        # 基底型を外すと、short enums の devkitARM では位置も大きさも合わなくなる（検査が効いている確認）。
        arm = [c for c in compilers if "arm-none-eabi" in c]
        if arm:
            stripped = re.sub(r"(enum \w+) : std::u?int\d+_t", r"\1", header)
            self.assertNotEqual(self.compile(arm[0], stripped).returncode, 0)


if __name__ == "__main__":
    unittest.main()

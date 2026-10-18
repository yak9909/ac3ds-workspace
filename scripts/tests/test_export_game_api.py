import csv
import hashlib
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


if __name__ == "__main__":
    unittest.main()

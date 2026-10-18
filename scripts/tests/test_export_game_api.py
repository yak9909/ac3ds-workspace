import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import export_game_api as api


class ExportGameApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
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
        self.assertIn("code SHA256: " + "a" * 64, content)
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


if __name__ == "__main__":
    unittest.main()

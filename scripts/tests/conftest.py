import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
REAL_ROOT = SCRIPTS.parent

# 試験で作るリポジトリごとに git fsmonitor の常駐の処理が残らないように
_n = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
os.environ.update({"GIT_CONFIG_COUNT": str(_n + 1), "GIT_CONFIG_KEY_%d" % _n: "core.fsmonitor",
                   "GIT_CONFIG_VALUE_%d" % _n: "false"})

# 試験用の値は、自分のソースが検査に引っかからないよう分けて書く
FAKE_EMAIL = "someone" + "@" + "private-host.net"
FAKE_TOKEN = "gh" + "p_" + "A" * 30
FAKE_USERPATH = "C:" + "\\Users\\" + "alice\\doc"

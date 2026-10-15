import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
REAL_ROOT = SCRIPTS.parent

# 試験用の値は、自分のソースが検査に引っかからないよう分けて書く
FAKE_EMAIL = "someone" + "@" + "private-host.net"
FAKE_TOKEN = "gh" + "p_" + "A" * 30
FAKE_USERPATH = "C:" + "\\Users\\" + "alice\\doc"

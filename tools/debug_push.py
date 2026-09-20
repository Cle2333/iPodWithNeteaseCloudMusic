"""直接跑一遍 push 的写入流程，拿完整堆栈（cmd_push 只打印了错误信息）。"""

import os
from __future__ import annotations

import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ipod_cli.discovery import require_ipod  # noqa: E402
from ipod_cli.library import read_library  # noqa: E402
from ipod_cli.ncm.client import DEFAULT_BASE_URL, NcmClient  # noqa: E402
from ipod_cli.ncm.state import StateStore  # noqa: E402
from ipod_cli.ncm.sync import SyncSource, plan_sync, sync_to_ipod  # noqa: E402


def main() -> int:
    reh = Path(sys.argv[1] if len(sys.argv) > 1 else str(Path(os.environ.get("TEMP", ".")) / "reh-p2"))
    store = StateStore(".ncm/ncm.db")
    account = store.active_account()
    assert account, "没有登录账号"

    client = NcmClient(DEFAULT_BASE_URL, cookie=account.cookie)
    device = require_ipod(str(reh))
    library = read_library(device.root)

    plan = plan_sync(
        client, store, SyncSource.liked(), level="exhigh",
        uid=account.uid, cookie=account.cookie, limit=3,
    )
    device.activate()

    try:
        outcome = sync_to_ipod(
            client, store, plan, device, library,
            cookie=account.cookie, make_playlist=True,
            progress=lambda m: print(f"  {m}"),
        )
    except Exception:
        print()
        print("=" * 70)
        traceback.print_exc()
        print("=" * 70)
        return 1

    print(f"added={outcome.added} verified={outcome.verified}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

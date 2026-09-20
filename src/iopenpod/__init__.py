"""Vendored subset of iOpenPod (MIT) — iTunesDB / ArtworkDB read-write engine.

Trimmed from https://github.com/TheRealSavi/iOpenPod at commit a202424
(iOpenPod v1.68.1) for a Classic-only import/export tool.

This is the SECOND and last vendored file that differs from upstream: the
original imports ``.infrastructure.version`` purely to expose
``__version__``. Version reporting is not needed here, so the dependency (and
the whole ``infrastructure`` package) is dropped.

Upstream code that was kept is byte-identical to make future cherry-picks
from upstream straightforward.
"""

__all__: list[str] = []

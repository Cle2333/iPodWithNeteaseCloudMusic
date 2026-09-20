"""Find missing first-party imports in the vendored tree.

Walks every vendored file, resolves each `iopenpod.*` import against the new
tree, and reports the ones that do not exist there.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SRC = Path("src")


def exists(mod: str) -> bool:
    base = SRC / Path(*mod.split("."))
    return base.with_suffix(".py").exists() or (base / "__init__.py").exists()


def resolve_relative(path: Path, node: ast.ImportFrom) -> list[str]:
    # For a relative import, the anchor is always the *containing package*:
    # for `a/b/c.py` that is `a.b`; for `a/b/__init__.py` also `a.b`.
    # Both cases drop the last path element.
    parts = list(path.relative_to(SRC).with_suffix("").parts)[:-1]
    depth = node.level
    base = parts[: len(parts) - (depth - 1)] if depth > 1 else parts
    mods = []
    if node.module:
        mods.append(".".join(base + node.module.split(".")))
    else:
        for a in node.names:
            mods.append(".".join(base + [a.name]))
    return mods


def main() -> None:
    missing: dict[str, list[tuple[str, int]]] = defaultdict(list)
    local_optional: dict[str, list[tuple[str, int]]] = defaultdict(list)

    for path in sorted(SRC.rglob("*.py")):
        if "ipod_cli" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue

        # Collect names defined in this file (lazy imports inside try/except are
        # often guarded, so track those separately).
        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        guarded.add(id(sub))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    mods = resolve_relative(path, node)
                elif node.module and node.module.startswith("iopenpod"):
                    # `from pkg.mod import name` — only pkg.mod must exist;
                    # `name` may be a function/class, not a module.
                    mods = [node.module]
                else:
                    continue
                for m in mods:
                    if not exists(m):
                        entry = (path.as_posix(), node.lineno)
                        (local_optional if id(node) in guarded else missing)[m].append(entry)

    print("=" * 70)
    print("缺失的模块（非 try/except 保护 —— 会导致 import 失败）")
    print("=" * 70)
    if not missing:
        print("  （无）")
    for mod, hits in sorted(missing.items(), key=lambda kv: -len(kv[1])):
        print(f"\n{mod}   <- {len(hits)} 处")
        for f, ln in hits[:6]:
            print(f"      {f}:{ln}")

    print()
    print("=" * 70)
    print("缺失但被 try/except 保护（可能安全）")
    print("=" * 70)
    if not local_optional:
        print("  （无）")
    for mod, hits in sorted(local_optional.items(), key=lambda kv: -len(kv[1])):
        print(f"  {mod}   <- {len(hits)} 处  ({hits[0][0]}:{hits[0][1]})")


if __name__ == "__main__":
    main()

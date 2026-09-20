"""ipod 命令行入口。

九个子命令，覆盖"把歌搬来搬去"这件事：

    ipod doctor                   环境自检
    ipod info                     看设备
    ipod list                     列曲目
    ipod import <路径>            导入（PC → iPod）
    ipod export <目录>            导出（iPod → PC）
    ipod remove <条件>            删除曲目
    ipod verify                   健康检查（写入后核对用）
    ipod backup / restore         整机备份还原
    ipod rename <新名字>          改设备名
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .backup import (
    BackupError,
    create_backup,
    default_backup_dir,
    load_manifest,
    restore_backup,
    verify_backup,
)
from .dbwrite import DatabaseWriteError, write_library
from .discovery import DeviceNotFoundError, describe, require_ipod
from .exporter import (
    build_export_items,
    execute_export,
    select_tracks,
    summarize,
    write_csv_manifest,
    write_json_manifest,
)
from .importer import ImportError_, build_import_plan, execute_import
from .library import LibraryError, read_library
from .mediafile import UnreadableMediaError, collect_audio_files
from .remover import RemoveError, build_remove_plan, execute_remove
from .transcode import ffmpeg_available, transcode_for_import
from .verify import FAIL, OK, WARN, check_device, report_as_dict

# ── 退出码 ───────────────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USER_ABORT = 2
EXIT_VERIFY_FAILED = 3


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _fake_tty_progress(message: str) -> None:
    """进度输出。

    只有在真终端里才用 ``\\r`` 原地刷新；输出被重定向到文件或管道时
    逐行打印，否则日志里会糊成一团带控制字符的乱码。
    """
    width = 78
    if sys.stdout.isatty():
        sys.stdout.write("\r" + message[:width].ljust(width))
        sys.stdout.flush()
    else:
        print(message[:width])


def _end_progress() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 78 + "\r")
        sys.stdout.flush()


# ──────────────────────────────────────────────────────────────────────
# info
# ──────────────────────────────────────────────────────────────────────

def cmd_info(args: argparse.Namespace) -> int:
    device = require_ipod(args.ipod)
    _out("=" * 60)
    _out("设备信息")
    _out("=" * 60)
    _out(describe(device))

    try:
        library = read_library(device.root)
    except LibraryError as exc:
        _err(f"\n读取曲目库失败：{exc}")
        return EXIT_ERROR

    summary = library.summary()
    _out("")
    _out(f"曲目数    : {summary['track_count']}")
    _out(f"专辑数    : {summary['album_count']}")
    _out(f"艺人数    : {summary['artist_count']}")
    _out(f"曲目占用  : {_human(summary['total_bytes'])}")
    if summary["total_ms"]:
        total_seconds = summary["total_ms"] // 1000
        _out(f"总时长    : {total_seconds // 3600} 小时 {total_seconds % 3600 // 60} 分")

    if not device.is_supported:
        _out("")
        _out("⚠  这台设备的签名方案不是 HASH58，本工具只在 Classic 上验证过。")
        _out("   继续操作可能写坏数据库，请先备份。")

    _out("")
    _out("环境")
    _out(f"  ffmpeg    : {'已找到（可转码 FLAC 等格式）' if ffmpeg_available() else '未找到（无法转码）'}")
    return EXIT_OK


def _human(num_bytes: int) -> str:
    from .discovery import human_size

    return human_size(num_bytes)


# ──────────────────────────────────────────────────────────────────────
# list
# ──────────────────────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> int:
    device = require_ipod(args.ipod)
    library = read_library(device.root)

    tracks = select_tracks(
        library,
        artist=args.artist,
        album=args.album,
        genre=args.genre,
        search=args.search,
        playlist=args.playlist,
    )

    if not tracks:
        _out("没有符合条件的曲目。")
        return EXIT_OK

    if args.json:
        import json

        _out(json.dumps(
            [
                {
                    "title": t.title, "artist": t.artist, "album": t.album,
                    "duration": t.duration_text, "bitrate": t.bitrate,
                    "filetype": t.filetype, "path": t.relative_path,
                }
                for t in tracks
            ],
            ensure_ascii=False,
            indent=2,
        ))
        return EXIT_OK

    _out(f"{device.display_name}  —  共 {len(tracks)} 首")
    _out("")

    if args.by_album:
        for line in _format_by_album(tracks):
            _out(line)
    else:
        for index, track in enumerate(tracks, start=1):
            _out(_format_track_line(index, track, track.album))
    return EXIT_OK


def _format_track_line(index: int, track, album: str) -> str:
    title = _truncate(track.title or "（无标题）", 34)
    artist = _truncate(track.artist or "未知艺人", 18)
    album_text = _truncate(album or "", 20)
    return (
        f"{index:>4}. {title:<34} {artist:<18} {album_text:<20} "
        f"{track.duration_text:>6}  {track.bitrate:>3}kbps"
    )


def _format_by_album(tracks) -> list[str]:
    groups: dict[tuple[str, str], list] = {}
    for track in tracks:
        key = (track.album_artist or track.artist or "未知艺人", track.album or "未知专辑")
        groups.setdefault(key, []).append(track)

    lines: list[str] = []
    for (artist, album), items in sorted(groups.items()):
        total_ms = sum(t.length for t in items)
        minutes = total_ms // 60000
        lines.append(f"▸ {artist} — {album}  （{len(items)} 首，{minutes} 分）")
        for track in sorted(items, key=lambda t: (t.disc_number, t.track_number, t.title)):
            lines.append(
                f"     {track.track_number:>2}. {_truncate(track.title, 40):<40} "
                f"{track.duration_text:>6}"
            )
        lines.append("")
    return lines


def _truncate(text: str, args_width: int) -> str:
    """按显示宽度截断（中日韩字符算 2 格）。"""
    text = str(text or "")
    width = 0
    result = []
    for ch in text:
        char_width = 2 if _is_wide(ch) else 1
        if width + char_width > args_width:
            result.append("…")
            break
        result.append(ch)
        width += char_width
    return "".join(result)


def _is_wide(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x20000 <= code <= 0x3FFFD
    )


# ──────────────────────────────────────────────────────────────────────
# import
# ──────────────────────────────────────────────────────────────────────

def cmd_import(args: argparse.Namespace) -> int:
    device = require_ipod(args.ipod)

    # 收集文件
    sources: list[Path] = []
    for raw in args.paths:
        path = Path(raw).expanduser()
        if not path.exists():
            _err(f"路径不存在，已跳过：{path}")
            continue
        try:
            sources.extend(collect_audio_files(path, recursive=not args.no_recursive))
        except UnreadableMediaError as exc:
            _err(f"无法扫描 {path}：{exc}")

    if not sources:
        _err("没有找到可导入的音频文件。")
        return EXIT_ERROR

    # 去重（同一文件被多个参数路径覆盖时）
    unique: list[Path] = []
    seen: set[str] = set()
    for path in sources:
        key = str(path.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    sources = unique

    _out(f"找到 {len(sources)} 个音频文件。")
    library = read_library(device.root)
    _out(f"iPod 上现有 {len(library.tracks)} 首曲目。")
    _out("")

    device.activate()

    plan = build_import_plan(
        device,
        library,
        sources,
        force=args.force,
        allow_transcode=not args.no_transcode,
        progress=_fake_tty_progress,
    )
    _end_progress()

    # 计划汇总
    _out("导入计划")
    _out("=" * 60)
    for item in plan.errored:
        _out(f"  ✗ {item.pc.display_name}")
        _out(f"      {item.reason}")
    for item in plan.skipped:
        _out(f"  – {item.pc.display_name}  （{item.reason}）")
    for item in plan.to_add:
        marker = "转码" if item.action == "transcode" else "新增"
        _out(f"  + [{marker}] {item.pc.display_name}  →  {item.ipod_location}")
    _out("")
    _out(
        f"合计：新增 {len(plan.to_add)} 首，跳过 {len(plan.skipped)} 首，"
        f"失败 {len(plan.errored)} 首"
    )
    _out(f"需要空间：{_human(plan.bytes_to_copy)}（iPod 剩余 {device.free_text}）")

    if not plan.to_add:
        _out("")
        _out("没有需要导入的曲目。")
        return EXIT_OK

    if not plan.fits:
        _err("")
        _err("iPod 剩余空间不足，已放弃导入。")
        return EXIT_ERROR

    if args.dry_run:
        _out("")
        _out("（--dry-run：以上为预览，未做任何修改）")
        return EXIT_OK

    if not args.yes:
        _out("")
        answer = input("确认写入 iPod？输入 y 继续：").strip().lower()
        if answer not in {"y", "yes", "是"}:
            _out("已取消。")
            return EXIT_USER_ABORT

    _out("")
    transcode_fn = None if args.no_transcode else transcode_for_import
    try:
        result = execute_import(
            plan,
            progress=_fake_tty_progress,
            transcode=transcode_fn,
        )
    except ImportError_ as exc:
        _end_progress()
        _err("")
        _err(str(exc))
        return EXIT_ERROR

    _end_progress()
    _out("")
    _out(f"已写入 {result.added} 首，复制 {_human(result.bytes_copied)}。")
    for source, reason in result.failed:
        _err(f"  失败：{source}\n        {reason}")

    if not result.database_written:
        _err("数据库写入失败。iPod 未被修改（写入前已生成 iTunesDB.backup）。")
        return EXIT_ERROR

    if not result.verified:
        _err("")
        _err(f"⚠  读回校验未通过：{result.verification_note}")
        _err("   iPod 数据库可能不一致。可用 iTunesDB.backup 还原。")
        return EXIT_VERIFY_FAILED

    _out(result.verification_note)
    _out("")
    _out("完成。安全移除 iPod 后再拔线。")
    return EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# export
# ──────────────────────────────────────────────────────────────────────

def cmd_export(args: argparse.Namespace) -> int:
    device = require_ipod(args.ipod)
    library = read_library(device.root)

    # --all 是"全部导出"的显式写法（不带筛选条件时本来就是全部）。
    # 跟筛选条件一起用说明用户理解有误，直接拦下来而不是悄悄忽略筛选。
    if args.all and any(
        (args.artist, args.album, args.genre, args.search, args.playlist)
    ):
        _err(
            "--all 表示导出全部，不能与 --artist/--album/--genre/--search/--playlist 同时使用。\n"
            "  想导出全部就去掉 --all 和筛选条件；想筛选就去掉 --all。"
        )
        return EXIT_ERROR

    try:
        tracks = select_tracks(
            library,
            artist=args.artist,
            album=args.album,
            genre=args.genre,
            search=args.search,
            playlist=args.playlist,
        )
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR

    if not tracks:
        _out("没有符合条件的曲目。")
        return EXIT_OK

    dest_dir = Path(args.dest).expanduser()
    items = build_export_items(
        device,
        tracks,
        dest_dir,
        flat=args.flat,
        organize_by=args.organize_by,
        keep_original=args.keep_original,
    )

    _out(f"准备导出 {len(items)} 首到 {dest_dir}")
    _out("")

    if args.dry_run:
        for item in items[:50]:
            flag = "" if item.exists else "  ⚠ 源文件缺失"
            _out(f"  {item.track.display_name}  →  {item.dest.relative_to(dest_dir)}{flag}")
        if len(items) > 50:
            _out(f"  … 还有 {len(items) - 50} 首")
        _out("")
        _out("（--dry-run：以上为预览，未做任何修改）")
        return EXIT_OK

    result = execute_export(items, progress=_fake_tty_progress, overwrite=args.overwrite)
    _end_progress()
    _out("")
    _out(summarize(result.items))

    if result.missing:
        _out("")
        _out("以下曲目在 iPod 上找不到对应文件：")
        for item in result.missing[:20]:
            _out(f"  {item.track.display_name}  ({item.track.relative_path})")
        if len(result.missing) > 20:
            _out(f"  … 还有 {len(result.missing) - 20} 个")

    if args.csv:
        path = write_csv_manifest(result.items, Path(args.csv).expanduser())
        _out(f"清单已写入：{path}")
    if args.json_manifest:
        path = write_json_manifest(result.items, Path(args.json_manifest).expanduser())
        _out(f"清单已写入：{path}")

    return EXIT_OK if not result.failed else EXIT_ERROR


# ──────────────────────────────────────────────────────────────────────
# doctor
# ──────────────────────────────────────────────────────────────────────

def cmd_doctor(args: argparse.Namespace) -> int:
    _out("=" * 60)
    _out("环境自检")
    _out("=" * 60)

    _out(f"Python    : {sys.version.split()[0]}")
    _out(f"ipod-cli  : {__version__}")

    if ffmpeg_available():
        from .transcode import find_ffmpeg

        _out(f"ffmpeg    : 已找到 —— {find_ffmpeg()}")
    else:
        _out("ffmpeg    : ✗ 未找到 —— FLAC/OGG 等格式无法导入")
        _out("            winget install Gyan.FFmpeg")

    from iopenpod.device import create_virtual_ipod  # noqa: F401  确认内核可导入

    _out("内核      : ✓ vendored iOpenPod 子集可导入")

    _out("")
    _out("设备扫描")
    try:
        device = require_ipod(args.ipod)
        _out(describe(device))
        try:
            library = read_library(device.root)
            _out(f"曲目库    : 可读，{len(library.tracks)} 首")
        except LibraryError as exc:
            _out(f"曲目库    : ✗ {exc}")
    except DeviceNotFoundError as exc:
        # 错误一律走 stderr，方便脚本区分正常输出和失败
        _err("")
        _err(str(exc))
        return EXIT_ERROR

    return EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# verify
# ──────────────────────────────────────────────────────────────────────

_STATUS_MARK = {OK: "✅", WARN: "⚠ ", FAIL: "❌"}


def cmd_verify(args: argparse.Namespace) -> int:
    device = require_ipod(args.ipod)
    report = check_device(
        device, progress=_fake_tty_progress, skip_files=args.no_files
    )
    _end_progress()

    if args.json:
        _out(json.dumps(report_as_dict(report), ensure_ascii=False, indent=2))
        return EXIT_OK if report.ok else EXIT_VERIFY_FAILED

    _out("=" * 62)
    _out(f"健康检查：{device.display_name}")
    _out("=" * 62)
    for check in report.checks:
        mark = _STATUS_MARK.get(check.status, "?")
        _out(f"  {mark} {check.name}")
        _out(f"      {check.summary}")
        # 详情只在有问题或明确要求时打，避免正常情况刷屏
        if check.details and (args.verbose or check.status != OK):
            for line in check.details:
                _out(f"        {line}")
    _out("")

    if report.ok and not report.warnings:
        _out("全部通过。")
        return EXIT_OK
    if report.ok:
        _out(f"通过，但有 {len(report.warnings)} 项提醒。（加 --verbose 看详情）")
        return EXIT_OK

    _err(f"发现 {len(report.failures)} 项问题：")
    for check in report.failures:
        _err(f"  ❌ {check.name}：{check.summary}")
    _err("")
    _err("数据可能不一致。建议用 `ipod backup` 先备份当前状态，再排查。")
    return EXIT_VERIFY_FAILED


# ──────────────────────────────────────────────────────────────────────
# backup / restore
# ──────────────────────────────────────────────────────────────────────

def cmd_backup(args: argparse.Namespace) -> int:
    device = require_ipod(args.ipod)
    dest = Path(args.dest).expanduser() if args.dest else default_backup_dir(device)

    _out(f"备份 {device.display_name} 到：{dest}")
    if args.with_music:
        _out("（含 Music 目录，会复制几十 GB，需要较长时间）")
    _out("")

    manifest = create_backup(
        device, dest, include_music=args.with_music, progress=_fake_tty_progress
    )
    _end_progress()
    _out("")
    _out(f"已备份 {len(manifest.metadata_files)} 个元数据文件"
         f"（{_human(manifest.total_bytes)}）"
         + (f"，另有 {manifest.music_file_count} 个音乐文件"
            if manifest.includes_music else ""))
    _out(f"记录的曲目数：{manifest.track_count}")
    _out(f"清单：{dest / 'manifest.json'}")
    _out("")
    _out("还原用：ipod restore " + str(dest))

    if args.verify:
        _out("")
        _out("正在校验备份完整性…")
        consistent, problems = verify_backup(dest, progress=_fake_tty_progress)
        _end_progress()
        if consistent:
            _out("校验通过，备份完整。")
        else:
            _err(f"校验发现 {len(problems)} 个问题：")
            for problem in problems[:10]:
                _err(f"  {problem}")
            return EXIT_ERROR
    return EXIT_OK


def cmd_restore(args: argparse.Namespace) -> int:
    device = require_ipod(args.ipod)
    source = Path(args.source).expanduser()
    manifest = load_manifest(source)

    _out("=" * 62)
    _out("还原备份")
    _out("=" * 62)
    _out(f"设备      {device.display_name}")
    _out(f"备份来自  {manifest.ipod_root}")
    _out(f"备份时间  {manifest.created}")
    _out(f"元数据    {len(manifest.metadata_files)} 个文件"
         f"（{_human(manifest.total_bytes)}）")
    _out(f"曲目数    {manifest.track_count}"
         + (f"（含 {manifest.music_file_count} 个音乐文件）"
            if manifest.includes_music else ""))
    _out("")
    _out("这会覆盖设备上的 Device / iTunes / Artwork（会先把当前状态另存一份）。")

    if not manifest.includes_music:
        _out("")
        _out("⚠  这个备份不含 Music 目录，所以只还原数据库和封面，**不动音频文件**。")
        _out("   如果备份之后从设备上删过歌，还原后那些歌会回到曲库里但文件不在——")
        _out("   iPod 上会显示却播不出来。用 `ipod verify` 能看到这种缺失。")
        _out("   要连音频一起还原，当初备份时得加 --with-music。")

    if args.dry_run:
        _out("")
        _out("（--dry-run：以上为预览，未做任何修改）")
        return EXIT_OK

    if not args.yes:
        _out("")
        answer = input("确认还原？输入 y 继续：").strip().lower()
        if answer not in {"y", "yes", "是"}:
            _out("已取消。")
            return EXIT_USER_ABORT

    _out("")
    saved = restore_backup(
        source, device, backup_current=not args.no_snapshot, progress=_fake_tty_progress
    )
    _end_progress()
    _out("")
    if saved is not None:
        _out(f"还原前的状态已另存：{saved}")
    elif not args.no_snapshot:
        _err("⚠  当前状态另存失败——本次还原不可逆。")
    _out("还原完成。安全移除 iPod 后再拔线。")
    return EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# remove
# ──────────────────────────────────────────────────────────────────────

def cmd_remove(args: argparse.Namespace) -> int:
    device = require_ipod(args.ipod)

    # 没给任何筛选条件时，select_tracks 会返回全部曲目——那是"删光整个库"，
    # 后果太重，必须显式指定条件。
    # --keep-playlist 单独用是合法的：意思是"除了这个歌单，其余全删"。
    if not any((args.title, args.artist, args.album, args.genre,
                args.playlist, args.search, args.keep_playlist)):
        _err(
            "必须指定筛选条件，否则会匹配到全部曲目。\n"
            "  可用：--title / --artist / --album / --genre / --playlist / --search\n"
            "  或：  --keep-playlist 歌单名（全删，只留这个歌单里的）\n"
            "  提示：先用 `ipod list` 或 `ipod remove ... --dry-run` 确认匹配范围。"
        )
        return EXIT_ERROR

    library = read_library(device.root)
    _out(f"iPod 上现有 {len(library.tracks)} 首曲目。")

    try:
        tracks = select_tracks(
            library,
            artist=args.artist,
            album=args.album,
            genre=args.genre,
            search=args.search,
            playlist=args.playlist,
            keep_playlists=tuple(args.keep_playlist or ()),
        )
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR

    if args.title:
        needle = args.title.casefold()
        tracks = [t for t in tracks if needle in t.title.casefold()]

    plan = build_remove_plan(device, library, tracks)

    _out("")
    _out("删除计划")
    _out("=" * 60)
    limit = 40
    for track in plan.to_remove[:limit]:
        _out(f"  − {track.display_name}")
        _out(f"      {track.relative_path}  ({_human(track.size)})")
    if plan.count > limit:
        _out(f"  … 还有 {plan.count - limit} 首")
    _out("")
    _out(f"合计：删除 {plan.count} 首，释放 {_human(plan.bytes_freed)}，"
         f"剩余 {plan.remaining} 首")
    _out("")

    if args.dry_run:
        _out("（--dry-run：以上为预览，未做任何修改）")
        return EXIT_OK

    if not args.yes:
        answer = input(f"确认从 iPod 删除这 {plan.count} 首？输入 y 继续：").strip().lower()
        if answer not in {"y", "yes", "是"}:
            _out("已取消。")
            return EXIT_USER_ABORT

    _out("")
    device.activate()
    try:
        result = execute_remove(
            plan, progress=_fake_tty_progress, compact_artwork=args.compact_artwork
        )
    except (RemoveError, DatabaseWriteError) as exc:
        _end_progress()
        _err("")
        _err(str(exc))
        return EXIT_ERROR

    _end_progress()
    _out("")
    _out(f"已删除 {result.files_deleted} 个文件。")

    # 播放列表重建数量已由写入过程实时报告过，这里不再重复

    if not result.verified:
        _err("")
        _err(f"⚠  读回校验未通过：{result.verification_note}")
        _err("   可用 `ipod restore` 从备份还原。")
        return EXIT_VERIFY_FAILED

    _out(result.verification_note)
    if result.file_errors:
        _err("")
        _err(f"{len(result.file_errors)} 个文件删除失败（库已清掉这些曲目，"
             f"留下的是孤儿文件，可用 `ipod verify` 查看）：")
        for path, reason in result.file_errors[:5]:
            _err(f"  {path}\n      {reason}")
    _out("")
    _out("完成。安全移除 iPod 后再拔线。")
    return EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# rename
# ──────────────────────────────────────────────────────────────────────

def cmd_rename(args: argparse.Namespace) -> int:
    device = require_ipod(args.ipod)
    library = read_library(device.root)
    new_name = args.name.strip()

    if not new_name:
        _err("新名字不能为空。")
        return EXIT_ERROR

    current = library.ipod_name or "（未知）"
    _out(f"当前设备名：{current}")
    _out(f"新设备名  ：{new_name}")
    _out("")
    _out(f"共 {len(library.tracks)} 首曲目会被一并重写（内容不变）。")

    if args.dry_run:
        _out("")
        _out("（--dry-run：以上为预览，未做任何修改）")
        return EXIT_OK

    if not args.yes:
        _out("")
        answer = input("确认改名？输入 y 继续：").strip().lower()
        if answer not in {"y", "yes", "是"}:
            _out("已取消。")
            return EXIT_USER_ABORT

    from .dbwrite import build_track_infos

    device.activate()
    infos, _ = build_track_infos(library.track_dicts, progress=_fake_tty_progress)
    _out("")
    try:
        result = write_library(
            device,
            library,
            infos,
            progress=_fake_tty_progress,
            pc_file_paths=None,          # 不碰封面库
            master_playlist_name_override=new_name,
        )
    except DatabaseWriteError as exc:
        _end_progress()
        _err("")
        _err(str(exc))
        return EXIT_ERROR

    _end_progress()
    _out("")
    if not result.verified:
        _err(f"⚠  读回校验未通过：{result.verification_note}")
        return EXIT_VERIFY_FAILED
    _out(result.verification_note)
    _out(f"设备已改名：{new_name}（重新连接 iPod 后生效）")
    return EXIT_OK


# ──────────────────────────────────────────────────────────────────────
# 参数解析
# ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipod",
        description="iPod Classic 歌曲导入导出工具（中文）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  ipod doctor                        环境自检\n"
            "  ipod info                          查看设备信息\n"
            "  ipod list --by-album               按专辑列出曲目\n"
            "  ipod import ~/Music/新专辑         导入整个文件夹\n"
            "  ipod import 歌.mp3 --dry-run       只预览不改动\n"
            "  ipod export ~/备份 --all           全部导出到 PC\n"
            "  ipod remove --artist 测试 --dry-run  预览删除范围\n"
            "  ipod verify                        写入后核对数据一致性\n"
            "  ipod backup                        备份整个 iPod_Control\n"
            "  ipod restore ~/iPod备份-20260919   从备份还原\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"ipod-cli {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="命令")

    # info
    p_info = sub.add_parser("info", help="查看连接的 iPod 信息")
    p_info.add_argument("--ipod", metavar="路径", help="iPod 根目录（默认自动检测）")
    p_info.set_defaults(func=cmd_info)

    # list
    p_list = sub.add_parser("list", help="列出 iPod 上的曲目")
    p_list.add_argument("--ipod", metavar="路径", help="iPod 根目录（默认自动检测）")
    p_list.add_argument("--by-album", action="store_true", help="按专辑分组显示")
    p_list.add_argument("--artist", metavar="艺人", help="只看某艺人的歌")
    p_list.add_argument("--album", metavar="专辑", help="只看某专辑")
    p_list.add_argument("--genre", metavar="流派", help="只看某流派")
    p_list.add_argument("--search", metavar="关键词", help="按标题/艺人/专辑搜索")
    p_list.add_argument("--playlist", metavar="播放列表", help="只看某个播放列表")
    p_list.add_argument("--json", action="store_true", help="输出 JSON")
    p_list.set_defaults(func=cmd_list)

    # import
    p_imp = sub.add_parser("import", help="把歌曲导入 iPod")
    p_imp.add_argument("paths", nargs="+", metavar="路径", help="文件或文件夹")
    p_imp.add_argument("--ipod", metavar="路径", help="iPod 根目录（默认自动检测）")
    p_imp.add_argument("--dry-run", action="store_true", help="只显示计划，不做任何修改")
    p_imp.add_argument("--force", action="store_true", help="即使疑似重复也导入")
    p_imp.add_argument("--no-artwork", action="store_true", help="不写专辑封面")
    p_imp.add_argument("--no-transcode", action="store_true", help="不转码，跳过 iPod 不支持的格式")
    p_imp.add_argument("--no-recursive", action="store_true", help="不递归子文件夹")
    p_imp.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
    p_imp.set_defaults(func=cmd_import)

    # export
    p_exp = sub.add_parser("export", help="把歌曲从 iPod 导出到 PC")
    p_exp.add_argument("dest", metavar="目标目录", help="导出到哪个目录")
    p_exp.add_argument("--ipod", metavar="路径", help="iPod 根目录（默认自动检测）")
    p_exp.add_argument("--all", action="store_true", help="导出全部（默认行为）")
    p_exp.add_argument("--artist", metavar="艺人", help="只导出某艺人")
    p_exp.add_argument("--album", metavar="专辑", help="只导出某专辑")
    p_exp.add_argument("--genre", metavar="流派", help="只导出某流派")
    p_exp.add_argument("--search", metavar="关键词", help="按标题/艺人/专辑筛选")
    p_exp.add_argument("--playlist", metavar="播放列表", help="只导出某个播放列表")
    p_exp.add_argument(
        "--organize-by", choices=["artist", "album", "none"], default="artist",
        help="分目录方式（默认按艺人）",
    )
    p_exp.add_argument("--flat", action="store_true", help="全部平铺到一个目录")
    p_exp.add_argument("--keep-original", action="store_true", help="保留 iPod 上的原始文件名")
    p_exp.add_argument("--overwrite", action="store_true", help="覆盖已存在的目标文件")
    p_exp.add_argument("--dry-run", action="store_true", help="只显示计划，不做任何修改")
    p_exp.add_argument("--csv", metavar="文件", help="同时导出一份 CSV 清单")
    p_exp.add_argument("--json-manifest", metavar="文件", help="同时导出一份 JSON 清单")
    p_exp.set_defaults(func=cmd_export)

    # doctor
    p_doc = sub.add_parser("doctor", help="检查环境与设备是否可用")
    p_doc.add_argument("--ipod", metavar="路径", help="iPod 根目录（默认自动检测）")
    p_doc.set_defaults(func=cmd_doctor)

    # verify
    p_ver = sub.add_parser("verify", help="健康检查：签名/封面/文件/播放列表一致性")
    p_ver.add_argument("--ipod", metavar="路径", help="iPod 根目录（默认自动检测）")
    p_ver.add_argument("--json", action="store_true", help="输出 JSON")
    p_ver.add_argument("-v", "--verbose", action="store_true", help="显示全部详情")
    p_ver.add_argument(
        "--no-files", action="store_true",
        help="跳过逐文件核对（大曲库时快很多，但查不出孤儿/缺失文件）",
    )
    p_ver.set_defaults(func=cmd_verify)

    # backup
    p_bak = sub.add_parser("backup", help="备份整个 iPod_Control（可完整还原）")
    p_bak.add_argument("dest", nargs="?", metavar="目标目录",
                       help="备份到哪（默认 ~/Documents/iPod备份-时间戳）")
    p_bak.add_argument("--ipod", metavar="路径", help="iPod 根目录（默认自动检测）")
    p_bak.add_argument("--with-music", action="store_true",
                       help="连 Music 一起备份（几十 GB，很慢）")
    p_bak.add_argument("--verify", action="store_true", help="备份后立即校验哈希")
    p_bak.set_defaults(func=cmd_backup)

    # restore
    p_res = sub.add_parser("restore", help="从备份还原（会先另存当前状态）")
    p_res.add_argument("source", metavar="备份目录", help="哪个备份")
    p_res.add_argument("--ipod", metavar="路径", help="iPod 根目录（默认自动检测）")
    p_res.add_argument("--no-snapshot", action="store_true",
                       help="不另存当前状态（省时间，但还原就不可逆了）")
    p_res.add_argument("--dry-run", action="store_true", help="只看会做什么")
    p_res.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
    p_res.set_defaults(func=cmd_restore)

    # remove
    p_rem = sub.add_parser("remove", help="从 iPod 删除曲目（必须先指定筛选条件）")
    p_rem.add_argument("--title", metavar="标题", help="按标题匹配")
    p_rem.add_argument("--artist", metavar="艺人", help="按艺人匹配")
    p_rem.add_argument("--album", metavar="专辑", help="按专辑匹配")
    p_rem.add_argument("--genre", metavar="流派", help="按流派匹配")
    p_rem.add_argument("--search", metavar="关键词", help="按标题/艺人/专辑搜索")
    p_rem.add_argument("--playlist", metavar="播放列表", help="只删某个播放列表里的歌")
    p_rem.add_argument(
        "--keep-playlist", default=[], action="append", metavar="播放列表",
        help="保留该播放列表里的歌，其余全删（可重复指定）",
    )
    p_rem.add_argument("--ipod", metavar="路径", help="iPod 根目录（默认自动检测）")
    p_rem.add_argument("--dry-run", action="store_true", help="只显示计划，不做任何修改")
    p_rem.add_argument("--compact-artwork", action="store_true",
                       help="同时整理封面库（去掉孤儿条目，会多花点时间）")
    p_rem.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
    p_rem.set_defaults(func=cmd_remove)

    # rename
    p_ren = sub.add_parser("rename", help="改 iPod 的名字")
    p_ren.add_argument("name", metavar="新名字", help="新的设备名")
    p_ren.add_argument("--ipod", metavar="路径", help="iPod 根目录（默认自动检测）")
    p_ren.add_argument("--dry-run", action="store_true", help="只显示计划，不做任何修改")
    p_ren.add_argument("-y", "--yes", action="store_true", help="跳过确认提示")
    p_ren.set_defaults(func=cmd_rename)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK

    try:
        return int(args.func(args) or EXIT_OK)
    except DeviceNotFoundError as exc:
        _err("")
        _err(str(exc))
        return EXIT_ERROR
    except LibraryError as exc:
        _err("")
        _err(str(exc))
        return EXIT_ERROR
    except (
        ImportError_,
        RemoveError,
        BackupError,
        DatabaseWriteError,
    ) as exc:
        _err("")
        _err(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        _end_progress()
        _err("\n已中断。")
        return EXIT_USER_ABORT
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

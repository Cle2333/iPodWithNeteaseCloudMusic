"""Vendored slice of iOpenPod's sync package.

Only the pure metadata/playlist conversion helpers are needed by ipod-cli;
the sync engine, planner, executor and transcoder are intentionally NOT
vendored.

Files are copied unmodified from iOpenPod so upstream fixes can be
cherry-picked without conflicts.
"""

from ._playlist_builder import build_and_evaluate_playlists
from ._track_conversion import (
    ipod_filetype_for_extension,
    track_dict_to_info,
    trackinfo_to_eval_dict,
)
from .path_identity import coerce_int, stable_path_key

__all__ = [
    "build_and_evaluate_playlists",
    "coerce_int",
    "ipod_filetype_for_extension",
    "stable_path_key",
    "track_dict_to_info",
    "trackinfo_to_eval_dict",
]

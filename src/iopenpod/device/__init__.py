"""ipod_device — trimmed iPod device identification package.

Vendored from iOpenPod (MIT). This file is the ONE vendored file that was
rewritten rather than copied: the upstream version imports the whole device
layer (USB VPD probing, macOS IOKit, Linux udev, the GUI-oriented image
helper), none of which a Classic-only file-based tool needs.

Removed relative to upstream:
  .authority      — SysInfo authority/caching store
  .images         — iPod model artwork images (GUI pickers)
  .scanner        — cross-platform USB drive probing
  .vpd_libusb     — USB VPD identification over libusb
  .vpd_usb_control, .vpd_linux, .vpd_windows

Everything else is re-exported exactly as upstream so vendored modules keep
working through the package root (they do `from iopenpod.device import X`).
"""

# flake8: noqa: F401
# ruff: noqa: F401

# ── artwork ──────────────────────────────────────────────────────────
from .artwork import (
    ARTWORK_FORMATS_BY_ID,
    ITHMB_FORMAT_MAP,
    ITHMB_SIZE_MAP,
    cover_art_format_definitions_for_device,
    ithmb_formats_for_device,
    photo_formats_for_device,
    resolve_cover_art_format_definitions,
    resolve_cover_art_format_definitions_for_device,
)
from .bootstrap import ensure_device_itunes_database

# ── capabilities ─────────────────────────────────────────────────────
from .capabilities import (
    ArtworkFormat,
    DeviceCapabilities,
    capabilities_for_family_gen,
    checksum_type_for_family_gen,
    cover_art_formats_for_family_gen,
)
from .checksum import (
    CHECKSUM_MHBD_SCHEME,
    MHBD_SCHEME_TO_CHECKSUM,
    ChecksumType,
)

# ── durability (FAT32-safe writes) ───────────────────────────────────
from .durability import (
    durable_publish_new,
    durable_replace,
    durable_unlink,
    flush_filesystem,
    flush_parent_directory,
    flush_written_file,
    open_unique_sibling_temp,
)

# ── filesystem ───────────────────────────────────────────────────────
from .filesystem import (
    detect_filesystem_type,
    filesystem_itunesdb_platform,
)

# ── info (device_info) ───────────────────────────────────────────────
from .info import (
    DeviceInfo,
    UnidentifiedDeviceError,
    clear_current_device,
    detect_checksum_type,
    enrich,
    get_current_device,
    get_current_device_for_path,
    get_firewire_id,
    has_exact_model_number,
    has_safe_device_profile,
    itdb_write_filename,
    read_sysinfo,
    require_exact_model_number,
    require_safe_device_profile,
    resolve_itdb_path,
    set_current_device,
)

# ── lookup ───────────────────────────────────────────────────────────
from .lookup import (
    extract_model_number,
    get_friendly_model_name,
    get_model_info,
    infer_generation,
    lookup_by_serial,
    match_serial_suffix,
)

# ── models ───────────────────────────────────────────────────────────
from .models import (
    IPOD_MODELS,
    IPOD_RECOVERY_USB_PIDS,
    IPOD_USB_PIDS,
    SERIAL_LAST3_TO_MODEL,
    SERIAL_SUFFIX_TO_MODEL,
    USB_PID_TO_MODEL,
    canonicalize_model_identity,
)

# ── metadata writes ──────────────────────────────────────────────────
from .metadata_write import (
    guarded_device_metadata_session,
)

# ── path safety ──────────────────────────────────────────────────────
from .path_safety import (
    UnsafeDevicePathError,
    resolve_device_path,
)

# ── sysinfo parsing/evidence ─────────────────────────────────────────
from .sysinfo import (
    DeviceEvidence,
    EvidenceValue,
    ParsedSysInfoExtended,
    identity_from_sysinfo,
    identity_from_sysinfo_extended,
    parse_sysinfo_extended,
    parse_sysinfo_text,
)

# ── virtual iPods ────────────────────────────────────────────────────
from .virtual import (
    VIRTUAL_IPOD_INFO_FILENAME,
    available_virtual_ipod_models,
    create_virtual_ipod,
    ensure_virtual_itunes_database,
    has_virtual_ipod_info,
    load_virtual_ipod_info,
    virtual_ipod_info_path,
)

# ── write safety ─────────────────────────────────────────────────────
from .write_guard import (
    DatabaseGeneration,
    DeviceBusyError,
    DeviceWriteGuard,
    DeviceWriteSafetyError,
    ExternalDatabaseChangeError,
    capture_database_generation,
)
from .write_readiness import (
    inspect_device_write_readiness,
    revalidate_device_write_readiness,
    volume_lock_key,
)

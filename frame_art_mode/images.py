from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import logging
import os
from pathlib import Path
import re
import struct
from typing import Any

from PIL import Image, ImageOps
from unidecode import unidecode

try:
    from PIL import ImageCms
except ImportError:
    ImageCms = None

from .models import (
    NORMALIZATION_VERSION,
    OCCASION_PREFIX,
    ROOT_SET,
    CachedImage,
    ImageError,
    PreparedImage,
    SourceImage,
    atomic_write_bytes,
)

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
)
IGNORED_NAMES = frozenset({"@eaDir", ".DS_Store"})
BACKGROUND = (24, 24, 24)
MIN_SIDE_PX = 640
MAX_LONGEST_SIDE_PX = 3840
MAX_ASPECT_RATIO = 3.5
DEFAULT_MAX_IMAGE_BYTES = 5242880
QUALITY_STEPS = (88, 82, 76, 70, 64)
THUMBNAIL_MAX_SIZE = (360, 240)
THUMBNAIL_QUALITY = 78

__all__ = [
    "BACKGROUND",
    "DEFAULT_MAX_IMAGE_BYTES",
    "IGNORED_NAMES",
    "IMAGE_EXTENSIONS",
    "MAX_ASPECT_RATIO",
    "MAX_LONGEST_SIDE_PX",
    "MIN_SIDE_PX",
    "NORMALIZATION_VERSION",
    "OCCASION_PREFIX",
    "QUALITY_STEPS",
    "ROOT_SET",
    "SRGB_PROFILE",
    "THUMBNAIL_MAX_SIZE",
    "THUMBNAIL_QUALITY",
    "CachedImage",
    "ImageError",
    "LegacyPreparedImage",
    "NormalizedAssets",
    "PreparedImage",
    "SourceImage",
    "discover_sets",
    "file_signature",
    "natural_key",
    "normalize_image",
    "normalize_image_assets",
    "prepare_set",
    "read_verified_source",
    "safe_filename",
]


@dataclass(frozen=True)
class NormalizedAssets:
    tv_bytes: bytes
    thumb_bytes: bytes
    fingerprint: str
    byte_size: int
    width: int
    height: int


@dataclass(frozen=True)
class LegacyPreparedImage:
    source_rel: str
    filename: str
    fingerprint: str
    staged_path: Path


def _fixed(value: float) -> int:
    return round(value * 65536)


def _fallback_srgb_profile() -> bytes:
    def xyz(values: tuple[float, float, float]) -> bytes:
        return b"XYZ \0\0\0\0" + struct.pack(">iii", *(_fixed(v) for v in values))

    def curve() -> bytes:
        parameters = (2.4, 1 / 1.055, 0.055 / 1.055, 1 / 12.92, 0.04045, 0, 0)
        return (
            b"para\0\0\0\0"
            + struct.pack(">H", 4)
            + b"\0\0"
            + struct.pack(">7i", *(_fixed(value) for value in parameters))
        )

    description_text = b"sRGB IEC61966-2.1\0"
    description = (
        b"desc\0\0\0\0"
        + struct.pack(">I", len(description_text))
        + description_text
        + struct.pack(">IIH", 0, 0, 0)
        + b"\0" * 68
    )
    tags = [
        (b"desc", description),
        (b"wtpt", xyz((0.9642, 1.0, 0.8249))),
        (b"rXYZ", xyz((0.4360747, 0.2225045, 0.0139322))),
        (b"gXYZ", xyz((0.3850649, 0.7168786, 0.0971045))),
        (b"bXYZ", xyz((0.1430804, 0.0606169, 0.7141733))),
        (b"rTRC", curve()),
        (b"gTRC", curve()),
        (b"bTRC", curve()),
    ]
    header = bytearray(128)
    header[4:8] = b"pyth"
    header[8:12] = struct.pack(">I", 0x02100000)
    header[12:16] = b"mntr"
    header[16:20] = b"RGB "
    header[20:24] = b"XYZ "
    header[24:36] = struct.pack(">6H", 2020, 1, 1, 0, 0, 0)
    header[36:40] = b"acsp"
    header[40:44] = b"APPL"
    header[64:68] = struct.pack(">I", 0)
    header[68:80] = struct.pack(">iii", _fixed(0.9642), _fixed(1), _fixed(0.8249))
    header[80:84] = b"pyth"
    table = bytearray(struct.pack(">I", len(tags)))
    payload = bytearray()
    offset = 128 + 4 + len(tags) * 12
    for signature, content in tags:
        offset += (-offset) % 4
        while len(payload) + 128 + 4 + len(tags) * 12 < offset:
            payload.append(0)
        table.extend(signature + struct.pack(">II", offset, len(content)))
        payload.extend(content)
        offset += len(content)
    profile = header + table + payload
    profile[0:4] = struct.pack(">I", len(profile))
    return bytes(profile)


SRGB_PROFILE = _fallback_srgb_profile()
if ImageCms is not None:
    SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def natural_key(value: str) -> tuple[Any, ...]:
    parts = tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    )
    return parts + ((2, value.casefold()), (3, value))


_natural_key = natural_key


def file_signature(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


_file_signature = file_signature


def is_ignored_name(name: str) -> bool:
    return (
        name in IGNORED_NAMES
        or name.startswith(".")
        or name.startswith("._")
        or name.casefold().endswith(".tmp")
    )


def read_verified_source(
    path: Path,
    expected_signature: tuple[int, int, int, int] | None = None,
    *,
    race_message: str = "source changed while reading",
) -> tuple[bytes, tuple[int, int, int, int]]:
    before = file_signature(path.stat())
    if expected_signature is not None and before != expected_signature:
        raise ImageError(race_message)
    data = path.read_bytes()
    after = file_signature(path.stat())
    if after != before:
        raise ImageError(race_message)
    return data, after


def discover_sets(root: Path) -> tuple[dict[str, list[SourceImage]], list[str]]:
    sets: dict[str, list[SourceImage]] = {ROOT_SET: []}
    warnings: list[str] = []
    try:
        resolved_root = root.resolve()
        entries = sorted(os.scandir(resolved_root), key=lambda entry: natural_key(entry.name))
    except OSError as exc:
        return sets, [f"{root}: {exc}"]

    for entry in entries:
        if is_ignored_name(entry.name):
            continue
        try:
            if entry.is_dir(follow_symlinks=True):
                set_id = f"{OCCASION_PREFIX}{entry.name}"
                sets[set_id] = []
                _walk_set(Path(entry.path), resolved_root, set_id, sets[set_id], warnings)
            elif (
                entry.is_file(follow_symlinks=True)
                and Path(entry.name).suffix.casefold() in IMAGE_EXTENSIONS
            ):
                signature = file_signature(entry.stat(follow_symlinks=True))
                rel = Path(entry.name).as_posix()
                sets[ROOT_SET].append(
                    SourceImage(Path(entry.path), ROOT_SET, rel, rel, signature)
                )
        except OSError as exc:
            warnings.append(f"{entry.path}: {exc}")

    for images in sets.values():
        images.sort(key=lambda image: natural_key(image.name_rel))
    return sets, warnings


def _walk_set(
    directory: Path,
    root: Path,
    set_id: str,
    images: list[SourceImage],
    warnings: list[str],
) -> None:
    root_stat = root.stat()
    visited: set[tuple[int, int]] = {(root_stat.st_dev, root_stat.st_ino)}

    def visit(path: Path) -> None:
        try:
            stat_result = path.stat()
            identity = (stat_result.st_dev, stat_result.st_ino)
            if identity in visited:
                return
            visited.add(identity)
            entries = sorted(os.scandir(path), key=lambda entry: natural_key(entry.name))
        except OSError as exc:
            warnings.append(f"{path}: {exc}")
            return

        for entry in entries:
            if is_ignored_name(entry.name):
                continue
            try:
                entry_path = Path(entry.path)
                if entry.is_dir(follow_symlinks=True):
                    visit(entry_path)
                elif (
                    entry.is_file(follow_symlinks=True)
                    and entry_path.suffix.casefold() in IMAGE_EXTENSIONS
                ):
                    signature = file_signature(entry.stat(follow_symlinks=True))
                    relative = Path(os.path.relpath(entry_path, root))
                    name_rel = entry_path.relative_to(directory).as_posix()
                    images.append(
                        SourceImage(
                            entry_path,
                            set_id,
                            relative.as_posix(),
                            name_rel,
                            signature,
                        )
                    )
            except (OSError, ValueError) as exc:
                warnings.append(f"{entry.path}: {exc}")

    visit(directory)


def safe_filename(name_rel: str, used: set[str]) -> str:
    stem = Path(name_rel).with_suffix("").as_posix().replace("/", "_")
    text = unidecode(stem)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", text)
    safe = re.sub(r"[_-]+", "-", safe).strip("-_") or "image"
    safe = safe[:160].rstrip("-_") or "image"
    candidate = f"{safe}.jpg"
    index = 2
    while candidate.casefold() in used:
        candidate = f"{safe}-{index}.jpg"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _prepare_rgb_image(data: bytes) -> Image.Image:
    if not data:
        raise ImageError("image file is empty")
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.format == "MPO":
                # Phone motion/gainmap photos wrap secondary frames in MPO; keep frame 0 only.
                original.seek(0)
            else:
                if original.format == "GIF" and getattr(original, "n_frames", 1) != 1:
                    raise ImageError("animated GIFs are not supported")
                if getattr(original, "is_animated", False) or getattr(original, "n_frames", 1) > 1:
                    raise ImageError(f"animated {original.format or 'image'}s are not supported")
            embedded_profile = original.info.get("icc_profile")
            original.load()
            image = ImageOps.exif_transpose(original)
            width, height = image.size
            if width < MIN_SIDE_PX or height < MIN_SIDE_PX:
                raise ImageError(
                    f"image is too small ({width}x{height}; both sides must be at least {MIN_SIDE_PX}px)"
                )
            if max(width, height) / min(width, height) > MAX_ASPECT_RATIO:
                raise ImageError(f"aspect ratio exceeds 3.5:1 ({width}x{height})")

            if image.mode.startswith("I;16"):
                image = image.point(lambda value: value / 256).convert("L")

            alpha = None
            if "A" in image.getbands() or "transparency" in image.info:
                rgba = image.convert("RGBA")
                alpha = rgba.getchannel("A")
                if image.mode in {"L", "LA"}:
                    image = image.getchannel("L")
                elif image.mode == "1":
                    image = image.convert("L")
                else:
                    image = rgba.convert("RGB")

            if embedded_profile:
                if ImageCms is None:
                    if embedded_profile != SRGB_PROFILE:
                        raise ImageError("embedded ICC profile cannot be verified without ImageCms")
                    if image.mode == "CMYK":
                        raise ImageError("CMYK input cannot be converted without ImageCms")
                    image = image.convert("RGB")
                else:
                    try:
                        source_profile = ImageCms.ImageCmsProfile(io.BytesIO(embedded_profile))
                        target_profile = ImageCms.ImageCmsProfile(io.BytesIO(SRGB_PROFILE))
                        profile_space = source_profile.profile.xcolor_space.strip()
                        if profile_space == "RGB":
                            if image.mode == "CMYK":
                                raise ImageError("CMYK image has an incompatible RGB profile")
                            image = image.convert("RGB")
                        elif profile_space == "CMYK":
                            if image.mode != "CMYK":
                                raise ImageError("CMYK profile does not match the image data")
                        elif profile_space == "GRAY":
                            if image.mode not in {"L", "1"}:
                                raise ImageError("grayscale profile does not match the image data")
                            image = image.convert("L")
                        elif profile_space in {"LAB", "XYZ"}:
                            if image.mode != profile_space:
                                raise ImageError(
                                    f"{profile_space} profile does not match the image data"
                                )
                        else:
                            raise ImageError(
                                f"unsupported embedded profile color space: {profile_space}"
                            )
                        image = ImageCms.profileToProfile(
                            image, source_profile, target_profile, outputMode="RGB"
                        )
                    except Exception as exc:
                        if isinstance(exc, ImageError):
                            raise
                        raise ImageError(
                            f"embedded ICC profile cannot be converted: {exc}"
                        ) from exc
            else:
                image = image.convert("RGB")

            if alpha is not None:
                rgba = image.convert("RGBA")
                rgba.putalpha(alpha)
                background = Image.new("RGBA", rgba.size, BACKGROUND + (255,))
                image = Image.alpha_composite(background, rgba).convert("RGB")
            elif image.mode != "RGB":
                image = image.convert("RGB")

            if max(image.size) > MAX_LONGEST_SIDE_PX:
                scale = MAX_LONGEST_SIDE_PX / max(image.size)
                size = (round(image.width * scale), round(image.height * scale))
                image = image.resize(size, Image.Resampling.LANCZOS)

            return image
    except ImageError:
        raise
    except Exception as exc:
        raise ImageError(f"invalid or unsupported image: {exc}") from exc


def _encode_tv_jpeg(image: Image.Image, max_image_bytes: int) -> bytes:
    if max_image_bytes < 1:
        raise ImageError("max_image_bytes must be >= 1")
    for quality in QUALITY_STEPS:
        output = io.BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=quality,
            progressive=False,
            subsampling=2,
            optimize=True,
            icc_profile=SRGB_PROFILE,
            exif=b"",
        )
        candidate = output.getvalue()
        if len(candidate) <= max_image_bytes:
            return candidate
    raise ImageError(
        f"normalized JPEG exceeds max_image_bytes ({max_image_bytes} bytes) "
        f"even at quality {QUALITY_STEPS[-1]}"
    )


def _encode_thumbnail_jpeg(image: Image.Image) -> bytes:
    thumb = image.copy()
    thumb.thumbnail(THUMBNAIL_MAX_SIZE, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    thumb.save(
        output,
        format="JPEG",
        quality=THUMBNAIL_QUALITY,
        progressive=False,
        subsampling=2,
        optimize=True,
        icc_profile=SRGB_PROFILE,
        exif=b"",
    )
    return output.getvalue()


def normalize_image_assets(
    data: bytes,
    *,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> NormalizedAssets:
    image = _prepare_rgb_image(data)
    tv_bytes = _encode_tv_jpeg(image, max_image_bytes)
    thumb_bytes = _encode_thumbnail_jpeg(image)
    fingerprint = hashlib.sha256(tv_bytes).hexdigest()
    return NormalizedAssets(
        tv_bytes=tv_bytes,
        thumb_bytes=thumb_bytes,
        fingerprint=fingerprint,
        byte_size=len(tv_bytes),
        width=image.width,
        height=image.height,
    )


def normalize_image(
    data: bytes,
    *,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> bytes:
    image = _prepare_rgb_image(data)
    return _encode_tv_jpeg(image, max_image_bytes)


def prepare_set(
    images: list[SourceImage],
    snapshot_dir: Path,
    *,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> tuple[list[LegacyPreparedImage], list[str]]:
    prepared: list[LegacyPreparedImage] = []
    warnings: list[str] = []
    used: set[str] = set()
    for source in images:
        try:
            data, _signature = read_verified_source(
                source.path,
                expected_signature=source.signature,
                race_message="source changed while its sync snapshot was being captured",
            )
            normalized = normalize_image(data, max_image_bytes=max_image_bytes)
            filename = safe_filename(source.name_rel, used)
            staged_path = snapshot_dir / filename
            atomic_write_bytes(staged_path, normalized, mode=0o600, fsync=False)
            prepared.append(
                LegacyPreparedImage(
                    source_rel=source.source_rel,
                    filename=filename,
                    fingerprint=hashlib.sha256(normalized).hexdigest(),
                    staged_path=staged_path,
                )
            )
        except (OSError, ImageError) as exc:
            warnings.append(f"{source.source_rel}: {exc}")
    return prepared, warnings

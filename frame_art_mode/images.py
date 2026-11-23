from __future__ import annotations

import hashlib
import io
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
from unidecode import unidecode

try:
    from PIL import ImageCms
except ImportError:
    ImageCms = None

ROOT_SET = "root"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
IGNORED_NAMES = {"@eaDir", ".DS_Store"}
BACKGROUND = (24, 24, 24)


class ImageError(ValueError):
    pass


@dataclass(frozen=True)
class SourceImage:
    path: Path
    set_id: str
    source_rel: str
    name_rel: str
    signature: tuple[int, int, int, int]


@dataclass(frozen=True)
class PreparedImage:
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
        return b"para\0\0\0\0" + struct.pack(">H", 4) + b"\0\0" + struct.pack(
            ">7i", *(_fixed(value) for value in parameters)
        )

    description_text = b"sRGB IEC61966-2.1\0"
    description = b"desc\0\0\0\0" + struct.pack(">I", len(description_text)) + description_text
    description += struct.pack(">IIH", 0, 0, 0) + b"\0" * 68
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


def _natural_key(value: str) -> tuple[Any, ...]:
    parts = tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    )
    return parts + ((2, value.casefold()), (3, value))


def _file_signature(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _ignored(name: str) -> bool:
    return (
        name in IGNORED_NAMES
        or name.startswith(".")
        or name.startswith("._")
        or name.casefold().endswith(".tmp")
    )


def discover_sets(root: Path) -> tuple[dict[str, list[SourceImage]], list[str]]:
    root = root.resolve()
    sets: dict[str, list[SourceImage]] = {ROOT_SET: []}
    warnings: list[str] = []
    try:
        entries = sorted(os.scandir(root), key=lambda entry: _natural_key(entry.name))
    except OSError as exc:
        return sets, [f"{root}: {exc}"]

    for entry in entries:
        if _ignored(entry.name):
            continue
        try:
            if entry.is_dir(follow_symlinks=True):
                set_id = f"occasion:{entry.name}"
                sets[set_id] = []
                _walk_set(Path(entry.path), root, set_id, sets[set_id], warnings)
            elif entry.is_file(follow_symlinks=True) and Path(entry.name).suffix.casefold() in IMAGE_EXTENSIONS:
                signature = _file_signature(entry.stat(follow_symlinks=True))
                sets[ROOT_SET].append(
                    SourceImage(
                        Path(entry.path), ROOT_SET, Path(entry.name).as_posix(), Path(entry.name).as_posix(), signature
                    )
                )
        except OSError as exc:
            warnings.append(f"{entry.path}: {exc}")

    for images in sets.values():
        images.sort(key=lambda image: _natural_key(image.name_rel))
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
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in visited:
                return
            visited.add(identity)
            entries = sorted(os.scandir(path), key=lambda entry: _natural_key(entry.name))
        except OSError as exc:
            warnings.append(f"{path}: {exc}")
            return
        for entry in entries:
            if _ignored(entry.name):
                continue
            try:
                entry_path = Path(entry.path)
                if entry.is_dir(follow_symlinks=True):
                    visit(entry_path)
                elif entry.is_file(follow_symlinks=True) and entry_path.suffix.casefold() in IMAGE_EXTENSIONS:
                    signature = _file_signature(entry.stat(follow_symlinks=True))
                    relative = Path(os.path.relpath(entry_path, root))
                    name_rel = entry_path.relative_to(directory).as_posix()
                    images.append(SourceImage(entry_path, set_id, relative.as_posix(), name_rel, signature))
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


def normalize_image(data: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.format == "GIF" and getattr(original, "n_frames", 1) != 1:
                raise ImageError("animated GIFs are not supported")
            original.load()
            image = ImageOps.exif_transpose(original)
            width, height = image.size
            if width < 640 or height < 640:
                raise ImageError(f"image is too small ({width}x{height}; both sides must be at least 640px)")
            if max(width, height) / min(width, height) > 3.5:
                raise ImageError(f"aspect ratio exceeds 3.5:1 ({width}x{height})")

            alpha = None
            if "A" in image.getbands() or "transparency" in image.info:
                rgba = image.convert("RGBA")
                alpha = rgba.getchannel("A")
                image = image.getchannel("L") if image.mode == "LA" else rgba.convert("RGB")

            embedded_profile = original.info.get("icc_profile")
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
                                raise ImageError(f"{profile_space} profile does not match the image data")
                        else:
                            raise ImageError(f"unsupported embedded profile color space: {profile_space}")
                        image = ImageCms.profileToProfile(
                            image, source_profile, target_profile, outputMode="RGB"
                        )
                    except Exception as exc:
                        if isinstance(exc, ImageError):
                            raise
                        raise ImageError(f"embedded ICC profile cannot be converted: {exc}") from exc
            else:
                image = image.convert("RGB")

            if alpha is not None:
                rgba = image.convert("RGBA")
                rgba.putalpha(alpha)
                background = Image.new("RGBA", rgba.size, BACKGROUND + (255,))
                image = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                image = image.convert("RGB")

            if max(image.size) > 3840:
                scale = 3840 / max(image.size)
                size = (round(image.width * scale), round(image.height * scale))
                image = image.resize(size, Image.Resampling.LANCZOS)

            output = io.BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=88,
                progressive=False,
                subsampling=2,
                icc_profile=SRGB_PROFILE,
                exif=b"",
            )
            return output.getvalue()
    except ImageError:
        raise
    except Exception as exc:
        raise ImageError(f"invalid or unsupported image: {exc}") from exc


def prepare_set(
    images: list[SourceImage], snapshot_dir: Path
) -> tuple[list[PreparedImage], list[str]]:
    prepared: list[PreparedImage] = []
    warnings: list[str] = []
    used: set[str] = set()
    for source in images:
        try:
            before = _file_signature(source.path.stat())
            if before != source.signature:
                raise ImageError("source changed while its sync snapshot was being captured")
            data = source.path.read_bytes()
            if _file_signature(source.path.stat()) != before:
                raise ImageError("source changed while its sync snapshot was being captured")
            normalized = normalize_image(data)
            filename = safe_filename(source.name_rel, used)
            staged_path = snapshot_dir / filename
            staged_path.write_bytes(normalized)
            prepared.append(
                PreparedImage(
                    source_rel=source.source_rel,
                    filename=filename,
                    fingerprint=hashlib.sha256(normalized).hexdigest(),
                    staged_path=staged_path,
                )
            )
        except (OSError, ImageError) as exc:
            warnings.append(f"{source.source_rel}: {exc}")
    return prepared, warnings

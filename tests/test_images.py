import ast
import hashlib
import io
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from PIL import Image, JpegImagePlugin

try:
    from PIL import ImageCms
except ImportError:
    ImageCms = None

from frame_art_mode.images import (
    BACKGROUND,
    OCCASION_PREFIX,
    QUALITY_STEPS,
    ROOT_SET,
    SRGB_PROFILE,
    ImageError,
    NormalizedAssets,
    _fallback_srgb_profile,
    discover_sets,
    normalize_image,
    normalize_image_assets,
    prepare_set,
    read_verified_source,
    safe_filename,
)


def image_bytes(mode="RGB", size=(800, 640), color=(200, 30, 40), fmt="PNG", **save_options):
    image = Image.new(mode, size, color)
    output = io.BytesIO()
    image.save(output, format=fmt, **save_options)
    return output.getvalue()


def noisy_image_bytes(size=(1400, 1050), fmt="PNG") -> bytes:
    raw = os.urandom(size[0] * size[1] * 3)
    image = Image.frombytes("RGB", size, raw)
    output = io.BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


def gray_icc_profile() -> bytes:
    def fixed(value: float) -> int:
        return round(value * 65536)

    def xyz(values: tuple[float, float, float]) -> bytes:
        return b"XYZ \0\0\0\0" + struct.pack(">iii", *(fixed(v) for v in values))

    def curve() -> bytes:
        params = (2.2, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return (
            b"para\0\0\0\0"
            + struct.pack(">H", 4)
            + b"\0\0"
            + struct.pack(">7i", *(fixed(v) for v in params))
        )

    desc_text = b"Gray Gamma 2.2\0"
    desc = (
        b"desc\0\0\0\0"
        + struct.pack(">I", len(desc_text))
        + desc_text
        + struct.pack(">IIH", 0, 0, 0)
        + b"\0" * 68
    )
    tags = [(b"desc", desc), (b"wtpt", xyz((0.9642, 1.0, 0.8249))), (b"kTRC", curve())]
    header = bytearray(128)
    header[4:8] = b"pyth"
    header[8:12] = struct.pack(">I", 0x02100000)
    header[12:16] = b"mntr"
    header[16:20] = b"GRAY"
    header[20:24] = b"XYZ "
    header[24:36] = struct.pack(">6H", 2020, 1, 1, 0, 0, 0)
    header[36:40] = b"acsp"
    header[40:44] = b"APPL"
    header[68:80] = struct.pack(">iii", fixed(0.9642), fixed(1), fixed(0.8249))
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


class ImagePipelineTests(unittest.TestCase):
    def test_jpeg_is_sized_profiled_and_transparency_is_composited(self):
        source = image_bytes("RGBA", color=(255, 0, 0, 0))
        normalized = normalize_image(source)
        with Image.open(io.BytesIO(normalized)) as output:
            self.assertEqual(output.format, "JPEG")
            self.assertEqual(output.size, (800, 640))
            self.assertFalse(output.info.get("progressive", 0))
            self.assertEqual(JpegImagePlugin.get_sampling(output), 2)
            self.assertIn("icc_profile", output.info)
            self.assertFalse(output.getexif())
            if ImageCms is not None:
                ImageCms.ImageCmsProfile(io.BytesIO(output.info["icc_profile"]))
            self.assertTrue(all(channel < 60 for channel in output.convert("RGB").getpixel((10, 10))))

    def test_orients_and_caps_large_images(self):
        image = Image.new("RGB", (640, 800), "blue")
        exif = image.getexif()
        exif[274] = 6
        output = io.BytesIO()
        image.save(output, "JPEG", exif=exif)
        with Image.open(io.BytesIO(normalize_image(output.getvalue()))) as normalized:
            self.assertEqual(normalized.size, (800, 640))

        with Image.open(io.BytesIO(normalize_image(image_bytes(size=(5000, 2500))))) as normalized:
            self.assertEqual(normalized.size, (3840, 1920))

    def test_rejects_small_extreme_aspect_and_animated_gif_and_webp_and_corrupt(self):
        for data in (
            b"",
            b"not an image",
            image_bytes(size=(639, 800)),
            image_bytes(size=(800, 200)),
        ):
            with self.assertRaises(ImageError):
                normalize_image(data)

        frames = [Image.new("RGB", (640, 640), color) for color in ("red", "blue")]
        gif_output = io.BytesIO()
        frames[0].save(gif_output, "GIF", save_all=True, append_images=frames[1:])
        with self.assertRaisesRegex(ImageError, "animated"):
            normalize_image(gif_output.getvalue())

        webp_output = io.BytesIO()
        frames[0].save(webp_output, "WEBP", save_all=True, append_images=frames[1:])
        with self.assertRaisesRegex(ImageError, "animated"):
            normalize_image(webp_output.getvalue())

    def test_rejects_invalid_and_mismatched_embedded_color_profiles(self):
        source = image_bytes(fmt="JPEG", icc_profile=b"not an ICC profile")
        with self.assertRaisesRegex(ImageError, "ICC"):
            normalize_image(source)

        if ImageCms is not None:
            srgb_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            cmyk_source = image_bytes("CMYK", color=(0, 100, 100, 0), fmt="JPEG", icc_profile=srgb_profile)
            with self.assertRaisesRegex(ImageError, "incompatible RGB profile"):
                normalize_image(cmyk_source)

    @unittest.skipIf(ImageCms is None, "ImageCms is optional")
    def test_converts_valid_embedded_srgb_profile(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        source = image_bytes(fmt="JPEG", icc_profile=profile)
        with Image.open(io.BytesIO(normalize_image(source))) as output:
            self.assertIn("icc_profile", output.info)

    @unittest.skipIf(ImageCms is None, "ImageCms is optional")
    def test_grayscale_l_mode_with_transparency_and_gray_icc_profile(self):
        gray_profile = gray_icc_profile()
        im = Image.new("L", (640, 640), 210)
        # Mark left half as the transparent index (0) while keeping right half opaque (210)
        for x in range(320):
            for y in range(640):
                im.putpixel((x, y), 0)
        buf = io.BytesIO()
        im.save(buf, format="PNG", transparency=0, icc_profile=gray_profile)

        assets = normalize_image_assets(buf.getvalue())
        with Image.open(io.BytesIO(assets.tv_bytes)) as output:
            self.assertEqual(output.mode, "RGB")
            self.assertEqual(output.size, (640, 640))
            self.assertEqual(output.info.get("icc_profile"), SRGB_PROFILE)
            # Transparent region composited onto #181818 (24, 24, 24)
            transparent_pixel = output.getpixel((50, 320))
            self.assertTrue(
                all(abs(channel - BACKGROUND[0]) <= 3 for channel in transparent_pixel),
                f"expected #181818 background, got {transparent_pixel}",
            )
            # Opaque region preserves converted bright gray (~210)
            opaque_pixel = output.getpixel((500, 320))
            self.assertTrue(
                all(channel >= 180 for channel in opaque_pixel),
                f"expected bright gray pixel, got {opaque_pixel}",
            )

    def test_image_cms_none_fallback_accepts_srgb_and_rejects_unverified_profiles(self):
        fallback_bytes = _fallback_srgb_profile()
        if ImageCms is not None:
            parsed = ImageCms.ImageCmsProfile(io.BytesIO(fallback_bytes))
            self.assertEqual(parsed.profile.xcolor_space.strip(), "RGB")

        with mock.patch("frame_art_mode.images.ImageCms", None):
            srgb_source = image_bytes(fmt="JPEG", icc_profile=SRGB_PROFILE)
            with Image.open(io.BytesIO(normalize_image(srgb_source))) as output:
                self.assertEqual(output.mode, "RGB")

            other_source = image_bytes(fmt="JPEG", icc_profile=gray_icc_profile())
            with self.assertRaisesRegex(ImageError, "ImageCms"):
                normalize_image(other_source)

            cmyk_srgb = image_bytes("CMYK", color=(0, 100, 100, 0), fmt="JPEG", icc_profile=SRGB_PROFILE)
            with self.assertRaisesRegex(ImageError, "CMYK"):
                normalize_image(cmyk_srgb)

    def test_decodes_cmyk_and_special_pixel_modes(self):
        cmyk_source = image_bytes("CMYK", color=(0, 100, 100, 0), fmt="JPEG")
        with Image.open(io.BytesIO(normalize_image(cmyk_source))) as output:
            self.assertEqual(output.mode, "RGB")

        for mode, color, fmt in (
            ("1", 1, "PNG"),
            ("L", 180, "PNG"),
            ("LA", (120, 0), "PNG"),
            ("PA", (200, 0), "TIFF"),
        ):
            with self.subTest(mode=mode):
                raw = image_bytes(mode=mode, color=color, fmt=fmt)
                with Image.open(io.BytesIO(normalize_image(raw))) as output:
                    self.assertEqual(output.mode, "RGB")
                    self.assertEqual(output.size, (800, 640))

        # Verify I;16 scales 16-bit luminance instead of clamping values >= 256 to 255
        im16 = Image.new("I;16", (640, 640), 32768)
        buf16 = io.BytesIO()
        im16.save(buf16, format="PNG")
        with Image.open(io.BytesIO(normalize_image(buf16.getvalue()))) as output16:
            pixel = output16.getpixel((320, 320))
            self.assertTrue(all(110 <= channel <= 145 for channel in pixel), f"unexpected pixel {pixel}")

        # Verify P mode with palette transparency composites onto #181818
        paletted = Image.new("P", (640, 640), 0)
        paletted.putpalette([255, 0, 0, 0, 255, 0])
        buf_p = io.BytesIO()
        paletted.save(buf_p, format="PNG", transparency=0)
        with Image.open(io.BytesIO(normalize_image(buf_p.getvalue()))) as output_p:
            pixel = output_p.getpixel((320, 320))
            self.assertTrue(all(channel < 45 for channel in pixel), f"unexpected pixel {pixel}")

    def test_quality_step_down_and_max_image_bytes_rejection(self):
        self.assertEqual(QUALITY_STEPS, (88, 82, 76, 70, 64))
        noisy = noisy_image_bytes(size=(900, 700))

        full_quality = normalize_image(noisy, max_image_bytes=5_242_880)
        # Choose a budget between quality-88 size and quality-64 size
        im = Image.open(io.BytesIO(noisy))
        q88_buf = io.BytesIO()
        im.save(q88_buf, format="JPEG", quality=88, progressive=False, subsampling=2, optimize=True)
        q64_buf = io.BytesIO()
        im.save(q64_buf, format="JPEG", quality=64, progressive=False, subsampling=2, optimize=True)
        q88_len = len(q88_buf.getvalue())
        q64_len = len(q64_buf.getvalue())
        self.assertLess(q64_len, q88_len)

        budget = (q88_len + q64_len) // 2
        stepped = normalize_image(noisy, max_image_bytes=budget)
        self.assertLessEqual(len(stepped), budget)
        self.assertLess(len(stepped), len(full_quality))

        too_small_budget = max(1, q64_len // 2)
        with self.assertRaisesRegex(ImageError, "max_image_bytes"):
            normalize_image(noisy, max_image_bytes=too_small_budget)

    def test_normalize_image_assets_generates_tv_and_thumbnail(self):
        # Portrait JPEG with EXIF Orientation=6 (rotates 640x1280 -> 1280x640) and extra EXIF tags
        src_img = Image.new("RGB", (640, 1280), (40, 120, 200))
        exif = src_img.getexif()
        exif[274] = 6  # Orientation
        exif[271] = "TestCamera"  # Make
        exif[305] = "TestSoftware"  # Software
        src_buf = io.BytesIO()
        src_img.save(src_buf, format="JPEG", exif=exif)

        assets = normalize_image_assets(src_buf.getvalue())
        self.assertIsInstance(assets, NormalizedAssets)
        self.assertEqual(assets.fingerprint, hashlib.sha256(assets.tv_bytes).hexdigest())
        self.assertEqual(assets.byte_size, len(assets.tv_bytes))
        self.assertEqual((assets.width, assets.height), (1280, 640))

        with Image.open(io.BytesIO(assets.tv_bytes)) as tv_img:
            self.assertEqual(tv_img.format, "JPEG")
            self.assertEqual(tv_img.mode, "RGB")
            self.assertEqual(tv_img.size, (1280, 640))
            self.assertFalse(tv_img.info.get("progressive", 0))
            self.assertEqual(JpegImagePlugin.get_sampling(tv_img), 2)
            self.assertEqual(tv_img.info.get("icc_profile"), SRGB_PROFILE)
            self.assertFalse(tv_img.getexif())
            self.assertNotIn("exif", tv_img.info)

        with Image.open(io.BytesIO(assets.thumb_bytes)) as thumb:
            self.assertEqual(thumb.format, "JPEG")
            self.assertEqual(thumb.mode, "RGB")
            self.assertEqual(thumb.size, (360, 180))
            self.assertFalse(thumb.info.get("progressive", 0))
            self.assertEqual(JpegImagePlugin.get_sampling(thumb), 2)
            self.assertEqual(thumb.info.get("icc_profile"), SRGB_PROFILE)
            self.assertFalse(thumb.getexif())
            self.assertNotIn("exif", thumb.info)

        # Tall portrait aspect fits inside (360, 240) preserving aspect ratio
        portrait = image_bytes(size=(800, 1600), color=(90, 140, 50), fmt="PNG")
        portrait_assets = normalize_image_assets(portrait)
        with Image.open(io.BytesIO(portrait_assets.thumb_bytes)) as p_thumb:
            self.assertEqual(p_thumb.mode, "RGB")
            self.assertEqual(p_thumb.size, (120, 240))
            self.assertEqual(p_thumb.info.get("icc_profile"), SRGB_PROFILE)
            self.assertFalse(p_thumb.getexif())

    def test_safe_names_transliterate_and_deduplicate(self):
        used = set()
        self.assertEqual(safe_filename("café/party__photo 1.PNG", used), "cafe-party-photo-1.jpg")
        self.assertEqual(safe_filename("cafe/party-photo 1.png", used), "cafe-party-photo-1-2.jpg")

    def test_discovery_ignores_metadata_and_keeps_occasion_recursion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "top.jpg").write_bytes(image_bytes(fmt="JPEG"))
            (root / ".hidden.jpg").write_bytes(image_bytes(fmt="JPEG"))
            (root / "._appledouble.jpg").write_bytes(image_bytes(fmt="JPEG"))
            (root / "partial.jpg.tmp").write_bytes(image_bytes(fmt="JPEG"))
            (root / "@eaDir").mkdir()
            (root / "@eaDir" / "preview.jpg").write_bytes(image_bytes(fmt="JPEG"))
            (root / ".DS_Store").write_bytes(b"ds")
            (root / "Party" / "nested").mkdir(parents=True)
            (root / "Party" / "nested" / "photo2.PNG").write_bytes(image_bytes())
            (root / "Party" / "nested" / ".ignored.png").write_bytes(image_bytes())
            (root / "Party" / "nested" / "._dot.png").write_bytes(image_bytes())
            (root / "Party" / "nested" / "upload.tmp").write_bytes(image_bytes())
            (root / "Party" / "notes.txt").write_text("not an image")
            external = root.parent / f"{root.name}-external.jpg"
            external.write_bytes(image_bytes(fmt="JPEG"))
            self.addCleanup(lambda: external.unlink(missing_ok=True))
            (root / "Party" / "nested" / "linked.jpg").symlink_to(external)
            (root / "Party" / "nested" / "root-loop").symlink_to(root, target_is_directory=True)
            (root / "LoopTop").symlink_to(root, target_is_directory=True)

            sets, warnings = discover_sets(root)
            self.assertFalse(warnings)
            self.assertEqual([item.source_rel for item in sets[ROOT_SET]], ["top.jpg"])
            self.assertEqual(
                [item.source_rel for item in sets[f"{OCCASION_PREFIX}Party"]],
                ["Party/nested/linked.jpg", "Party/nested/photo2.PNG"],
            )
            self.assertEqual(sets[f"{OCCASION_PREFIX}LoopTop"], [])

    def test_discovery_missing_root_returns_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "does_not_exist"
            sets, warnings = discover_sets(missing)
            self.assertEqual(sets, {ROOT_SET: []})
            self.assertEqual(len(warnings), 1)

    def test_natural_order_handles_digit_and_text_prefixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("10.jpg", "2.jpg", "trip.jpg"):
                (root / name).write_bytes(image_bytes(fmt="JPEG"))
            sets, _ = discover_sets(root)
            self.assertEqual([item.name_rel for item in sets[ROOT_SET]], ["2.jpg", "10.jpg", "trip.jpg"])

    def test_read_verified_source_detects_mid_read_change_and_prepare_set_compatibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "images"
            snap = base / "snapshot"
            root.mkdir()
            snap.mkdir()
            img_path = root / "photo.jpg"
            img_path.write_bytes(image_bytes(fmt="JPEG"))

            sets, _ = discover_sets(root)
            source = sets[ROOT_SET][0]
            data, sig = read_verified_source(source.path, expected_signature=source.signature)
            self.assertEqual(sig, source.signature)
            self.assertTrue(data)

            # Mutate file after discovery
            img_path.write_bytes(image_bytes(color=(10, 20, 30), fmt="JPEG") + b"\x00")
            with self.assertRaisesRegex(ImageError, "changed"):
                read_verified_source(source.path, expected_signature=source.signature)

            # Mutate between stat calls inside read_verified_source
            original_read = Path.read_bytes

            def mutate_during_read(self_path: Path) -> bytes:
                result = original_read(self_path)
                self_path.write_bytes(result + b"\x00")
                return result

            fresh_sets, _ = discover_sets(root)
            fresh_source = fresh_sets[ROOT_SET][0]
            with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=mutate_during_read):
                prepared, warnings = prepare_set([fresh_source], snap)
            self.assertEqual(prepared, [])
            self.assertEqual(len(warnings), 1)
            self.assertIn("source changed while its sync snapshot was being captured", warnings[0])

            # Normal prepare_set produces staged_path for legacy service.py compatibility
            valid_sets, _ = discover_sets(root)
            prepared, warnings = prepare_set(valid_sets[ROOT_SET], snap)
            self.assertEqual(warnings, [])
            self.assertEqual(len(prepared), 1)
            self.assertTrue(prepared[0].staged_path.is_file())

    def test_images_module_respects_import_boundaries(self):
        path = Path(__file__).resolve().parents[1] / "frame_art_mode" / "images.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = {"samsungframetv", "flask", "tv_daemon", "web_server"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    self.assertNotIn(top, forbidden)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    self.assertNotIn(top, forbidden)
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden)


if __name__ == "__main__":
    unittest.main()

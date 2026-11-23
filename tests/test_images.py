import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, JpegImagePlugin
try:
    from PIL import ImageCms
except ImportError:
    ImageCms = None

from frame_art_mode.images import ImageError, discover_sets, normalize_image, safe_filename


def image_bytes(mode="RGB", size=(800, 640), color=(200, 30, 40), fmt="PNG", **save_options):
    image = Image.new(mode, size, color)
    output = io.BytesIO()
    image.save(output, format=fmt, **save_options)
    return output.getvalue()


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

    def test_rejects_small_extreme_aspect_and_animated_gif(self):
        for data in (image_bytes(size=(639, 800)), image_bytes(size=(800, 200))):
            with self.assertRaises(ImageError):
                normalize_image(data)
        frames = [Image.new("RGB", (640, 640), color) for color in ("red", "blue")]
        output = io.BytesIO()
        frames[0].save(output, "GIF", save_all=True, append_images=frames[1:])
        with self.assertRaisesRegex(ImageError, "animated"):
            normalize_image(output.getvalue())

    def test_rejects_invalid_embedded_color_profile(self):
        source = image_bytes(fmt="JPEG", icc_profile=b"not an ICC profile")
        with self.assertRaisesRegex(ImageError, "ICC"):
            normalize_image(source)

    @unittest.skipIf(ImageCms is None, "ImageCms is optional")
    def test_converts_valid_embedded_srgb_profile(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        source = image_bytes(fmt="JPEG", icc_profile=profile)
        with Image.open(io.BytesIO(normalize_image(source))) as output:
            self.assertIn("icc_profile", output.info)

    def test_decodes_cmyk_jpeg(self):
        source = image_bytes("CMYK", color=(0, 100, 100, 0), fmt="JPEG")
        with Image.open(io.BytesIO(normalize_image(source))) as output:
            self.assertEqual(output.mode, "RGB")

    def test_safe_names_transliterate_and_deduplicate(self):
        used = set()
        self.assertEqual(safe_filename("café/party__photo 1.PNG", used), "cafe-party-photo-1.jpg")
        self.assertEqual(safe_filename("cafe/party-photo 1.png", used), "cafe-party-photo-1-2.jpg")

    def test_discovery_ignores_metadata_and_keeps_occasion_recursion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "top.jpg").write_bytes(image_bytes(fmt="JPEG"))
            (root / ".hidden.jpg").write_bytes(image_bytes(fmt="JPEG"))
            (root / "@eaDir").mkdir()
            (root / "@eaDir" / "preview.jpg").write_bytes(image_bytes(fmt="JPEG"))
            (root / "Party" / "nested").mkdir(parents=True)
            (root / "Party" / "nested" / "photo2.PNG").write_bytes(image_bytes())
            (root / "Party" / "nested" / ".ignored.png").write_bytes(image_bytes())
            (root / "Party" / "notes.txt").write_text("not an image")
            external = root.parent / f"{root.name}-external.jpg"
            external.write_bytes(image_bytes(fmt="JPEG"))
            (root / "Party" / "nested" / "linked.jpg").symlink_to(external)
            (root / "Party" / "nested" / "root-loop").symlink_to(root, target_is_directory=True)
            sets, warnings = discover_sets(root)
            self.assertFalse(warnings)
            self.assertEqual([item.source_rel for item in sets["root"]], ["top.jpg"])
            self.assertEqual(
                [item.source_rel for item in sets["occasion:Party"]],
                ["Party/nested/linked.jpg", "Party/nested/photo2.PNG"],
            )
            external.unlink()

    def test_natural_order_handles_digit_and_text_prefixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("10.jpg", "2.jpg", "trip.jpg"):
                (root / name).write_bytes(image_bytes(fmt="JPEG"))
            sets, _ = discover_sets(root)
            self.assertEqual([item.name_rel for item in sets["root"]], ["2.jpg", "10.jpg", "trip.jpg"])


if __name__ == "__main__":
    unittest.main()

"""Probe contract controls; run in the clean pinned Pillow evaluator container."""
import unittest
from PIL import Image, ImageOps
import pillow_metadata_probe_v1 as probe


def compliant_control(image, *, in_place=False, strip_metadata=False):
    """Test-only oracle wrapper; never used to repair or score submitted code."""
    returned = ImageOps.exif_transpose(image, in_place=in_place)
    target = image if in_place else returned
    if strip_metadata:
        target.info.clear()
        target._exif = Image.Exif()
    return returned


class MetadataProbeTests(unittest.TestCase):
    def test_contract_control_passes_all_eighteen_cases(self):
        result = probe.run_suite(compliant_control)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["tests_total"], 18)
        self.assertEqual(result["groups"]["strip"], {"passed": 6, "total": 6})
        self.assertEqual(result["groups"]["default_preservation"], {"passed": 12, "total": 12})

    def test_base_rejects_new_argument_but_omitted_default_stays_valid(self):
        result = probe.run_suite()
        cases = result["checks"]
        self.assertEqual(sum(c["passed"] for c in cases if c["argument_mode"] == "default_omitted"), 6)
        self.assertTrue(all(not c["passed"] and c["exception"]["type"] == "TypeError"
                            for c in cases if c["argument_mode"] != "default_omitted"))

    def test_stripping_before_orientation_is_detected(self):
        def broken(image, *, in_place=False, strip_metadata=False):
            if not strip_metadata:
                return compliant_control(image, in_place=in_place)
            target = image if in_place else image.copy()
            target.info.clear()
            target._exif = Image.Exif()
            returned = ImageOps.exif_transpose(target, in_place=in_place)
            return returned
        result = probe.run_suite(broken)
        rotate = [c for c in result["checks"] if c["argument_mode"] == "strip" and c["orientation"] == 6]
        self.assertEqual(len(rotate), 2)
        self.assertTrue(all(not c["checks"]["pixels_size_mode"] for c in rotate))

    def test_missing_and_identity_orientation_are_not_silently_skipped(self):
        def broken(image, *, in_place=False, strip_metadata=False):
            applies = image.getexif().get(0x0112, 1) != 1
            return compliant_control(image, in_place=in_place, strip_metadata=strip_metadata and applies)
        result = probe.run_suite(broken)
        strip = [c for c in result["checks"] if c["group"] == "strip"]
        self.assertEqual(sum(c["passed"] for c in strip), 2)
        self.assertTrue(all(not c["passed"] for c in strip if c["orientation"] in (None, 1)))

    def test_default_metadata_loss_is_detected(self):
        def broken(image, *, in_place=False, strip_metadata=False):
            return compliant_control(image, in_place=in_place, strip_metadata=True)
        result = probe.run_suite(broken)
        defaults = [c for c in result["checks"] if c["group"] == "default_preservation"]
        self.assertEqual(len(defaults), 12)
        self.assertTrue(all(not c["passed"] for c in defaults))

    def test_empty_serialized_exif_is_allowed(self):
        def alternative(image, *, in_place=False, strip_metadata=False):
            returned = compliant_control(image, in_place=in_place, strip_metadata=strip_metadata)
            if strip_metadata:
                target = image if in_place else returned
                target.info["exif"] = Image.Exif().tobytes()
            return returned
        self.assertTrue(probe.run_suite(alternative)["passed"])

    def test_evaluator_subprocess_environment_excludes_credentials(self):
        env = probe.clean_environment({"HOME": "/tmp/test", "LANG": "C", "OPENAI_API_KEY": "secret",
                                       "TOGETHER_API_KEY": "secret", "AWS_SECRET_ACCESS_KEY": "secret"})
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("TOGETHER_API_KEY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import unittest

source = Path(__file__).resolve().parents[1] / "custom_components/ha_image_inspector/analyzer.py"
spec = importlib.util.spec_from_file_location("analyzer", source)
analyzer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = analyzer
spec.loader.exec_module(analyzer)


class LayerAccountingTests(unittest.TestCase):
    def setUp(self):
        self.files = {}
        self.metadata = {}

    def layer(self, index, entries):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as tar:
            for path, value in entries:
                info = tarfile.TarInfo(path)
                if isinstance(value, str):
                    info.type = tarfile.LNKTYPE
                    info.linkname = value
                    tar.addfile(info)
                else:
                    info.size = len(value)
                    tar.addfile(info, io.BytesIO(value))
        data.seek(0)
        with tarfile.open(fileobj=data, mode="r|") as tar:
            return analyzer.read_layer(tar, index, self.files, self.metadata)

    def test_whiteouts_mask_lower_payload_only_and_replacement_hides_old_bytes(self):
        self.assertEqual(self.layer(0, [("opt/old", b"old"), ("opt/replaced", b"x"), ("data/lower", b"y")]), 5)
        self.assertEqual(self.layer(1, [("data/new", b"new"), ("data/.wh..wh..opq", b""),
                                        ("opt/.wh.old", b""), ("opt/replaced", b"1234")]), 7)
        self.assertEqual({path: entry.size for path, entry in self.files.items()}, {"data/new": 3, "opt/replaced": 4})

    def test_hard_link_keeps_lower_payload_when_the_original_is_replaced(self):
        self.layer(0, [("lib/original", b"old"), ("lib/link", "lib/original")])
        self.layer(1, [("lib/original", b"new content")])
        self.assertEqual((self.files["lib/link"].layer, self.files["lib/link"].size), (0, 3))
        self.assertEqual((self.files["lib/original"].layer, self.files["lib/original"].size), (1, 11))

    def test_regular_file_replaces_directory_and_removes_descendants(self):
        self.layer(0, [("opt/tool/executable", b"old")])
        self.layer(1, [("opt/tool", b"new")])
        self.assertEqual(list(self.files), ["opt/tool"])

    def test_source_checkout_and_wheel_are_classified_as_integrations(self):
        self.assertEqual(analyzer.classify("usr/src/homeassistant/homeassistant/components/zwave_js/manifest.json", None),
                         ("integration", "zwave_js"))
        self.assertEqual(analyzer.classify("usr/local/lib/python3.14/site-packages/homeassistant/components/mqtt/__init__.py", "homeassistant"),
                         ("integration", "mqtt"))

    def test_record_and_integration_requirements_link_without_double_counting_bytes(self):
        path = "usr/local/lib/python3.14/site-packages/My_Package-0.1.dist-info/RECORD"
        manifest = "usr/src/homeassistant/homeassistant/components/demo/manifest.json"
        self.layer(0, [(path, b"module/native.so,,\n"), (manifest, b'{"requirements": ["my.package[extra]==0.1"]}')])
        owners = analyzer.package_owners({path: self.metadata[path]})
        self.assertEqual(owners["usr/local/lib/python3.14/site-packages/module/native.so"], "My-Package")
        groups = {("python", "My-Package"): {"name": "My-Package", "bytes": 100}, ("integration", "demo"): {"bytes": 5}}
        analyzer.link_requirements(groups, self.metadata)
        self.assertEqual(groups[("integration", "demo")], {"bytes": 5, "related": ["My-Package"]})
        self.assertEqual(groups[("python", "My-Package")]["related"], ["demo"])

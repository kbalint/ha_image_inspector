import importlib.util
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
        with tarfile.open(fileobj=io.BytesIO(tar_bytes(entries)), mode="r|") as tar:
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

    def test_dependency_graph_distinguishes_direct_transitive_and_unrelated_packages(self):
        group_names = ["Adapter", "Transport", "Accelerator", "Optional", "DifferentOS", "Shared", "Standalone"]
        groups = {("python", name): {"name": name, "bytes": 100} for name in group_names}
        groups[("integration", "demo")] = {"bytes": 5}
        groups[("integration", "support")] = {"bytes": 5}
        metadata = {
            "usr/src/homeassistant/homeassistant/components/demo/manifest.json": '{"requirements":["adapter[fast]"],"dependencies":["support"]}',
            "usr/src/homeassistant/homeassistant/components/support/manifest.json": '{"requirements":["shared"],"dependencies":["demo"]}',
            "custom_components/custom_demo/manifest.json": '{"requirements":["standalone"],"dependencies":["support"]}',
            "usr/local/lib/python3.14/site-packages/adapter-1.dist-info/METADATA":
                'Name: Adapter\nRequires-Dist: Transport\nRequires-Dist: Accelerator; extra == "fast"\n'
                'Requires-Dist: Optional; extra == "unused"\nRequires-Dist: DifferentOS; sys_platform == "win32"\n',
            "usr/local/lib/python3.14/site-packages/transport-1.dist-info/METADATA": "Name: Transport\nRequires-Dist: Adapter\n",
            "usr/src/homeassistant/pyproject.toml": '[project]\ndependencies = ["shared"]\n',
        }
        graph = analyzer.link_requirements(groups, metadata, "registry", "arm64")
        self.assertEqual(graph["demo"], {"Adapter": "direct", "Accelerator": "indirect", "Transport": "indirect", "Shared": "indirect"})
        self.assertEqual(graph["support"]["Shared"], "direct")
        self.assertEqual(graph["custom_demo"]["Standalone"], "direct")
        self.assertEqual(graph["custom_demo"]["Shared"], "indirect")
        self.assertEqual(graph["_core"], {"Shared": "direct"})
        self.assertEqual(groups[("python", "Adapter")]["related"], ["demo"])


def tar_bytes(entries):
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
    return data.getvalue()


class ImageSourceTests(unittest.TestCase):
    def test_container_report_counts_visible_files_once_and_excludes_mount_paths(self):
        with TemporaryDirectory() as work:
            root = Path(work)
            integration = root / "usr/src/homeassistant/homeassistant/components/demo"
            integration.mkdir(parents=True)
            component = integration / "__init__.py"
            component.write_bytes(b"test")
            alias = integration / "copy.py"
            alias.write_bytes(b"test")
            mounted = root / "config"
            mounted.mkdir()
            (mounted / "state.db").write_bytes(b"test database")
            injected = root / "resolv.conf"
            injected.write_bytes(b"managed by Docker")
            lstat = Path.lstat
            info = component.lstat()
            shared_inode = SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size, st_nlink=2,
                                           st_dev=info.st_dev, st_ino=info.st_ino)
            with patch.object(Path, "lstat", lambda item: shared_inode if item in (component, alias) else lstat(item)):
                files, metadata = analyzer.read_filesystem(root, {mounted, injected})
            report = analyzer.summarize("fixture", "", "arm64", "container", [], files, metadata)
        self.assertEqual(report["totals"], {"visible": 4})
        self.assertEqual(report["layers"], [])
        self.assertEqual([(group["category"], group["name"]) for group in report["groups"]], [("integration", "demo")])

    def test_docker_archive_accounts_for_plain_and_compressed_layers_offline(self):
        base = tar_bytes([("opt/tool", b"lower"), ("opt/keep", b"unchanged")])
        top = tar_bytes([("opt/tool", b"upper layer")])
        top_compressed = gzip.compress(top)
        config = {"os": "linux", "architecture": "arm64", "rootfs": {"diff_ids": [digest(base), digest(top)]}}
        manifest = [{"Config": "config.json", "RepoTags": ["fixture:latest"], "Layers": ["base/layer.tar", "blobs/top"]}]
        bundle = tar_bytes([("config.json", json.dumps(config).encode()), ("base/layer.tar", base),
                            ("blobs/top", top_compressed), ("manifest.json", json.dumps(manifest).encode())])
        with TemporaryDirectory() as work, patch.object(analyzer, "Registry", side_effect=AssertionError("network use")):
            path = Path(work) / analyzer.LOCAL_ARCHIVE
            path.write_bytes(bundle)
            report = analyzer.analyze_archive(path)
        self.assertEqual(report["source"], "local")
        self.assertEqual(report["reference"], "fixture:latest")
        self.assertEqual(report["architecture"], "arm64")
        self.assertEqual(report["layers"][1]["digest"], digest(top))
        self.assertEqual(report["totals"], {"compressed": len(base) + len(top_compressed),
                                            "tar": len(base) + len(top), "written": 25, "visible": 20})

    def test_registry_still_verifies_blob_and_returns_registry_source(self):
        layer = tar_bytes([("opt/tool", b"hello")])
        compressed = gzip.compress(layer)
        config_bytes = json.dumps({"os": "linux", "architecture": "arm64"}).encode()
        descriptor = {"digest": digest(compressed), "size": len(compressed),
                      "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}
        manifest = {"config": {"digest": digest(config_bytes)}, "layers": [descriptor]}
        blobs = {f"blobs/{digest(config_bytes)}": config_bytes, f"blobs/{digest(compressed)}": compressed}
        progress = []
        with patch.object(analyzer, "Registry") as registry:
            registry.return_value.manifest.return_value = manifest, "fixture-manifest"
            registry.return_value.open.side_effect = lambda address: io.BytesIO(blobs[address])
            report = analyzer.analyze("fixture", "arm64", lambda *state: progress.append(state))
        self.assertEqual(report["source"], "registry")
        self.assertEqual(report["layers"][0]["digest"], digest(compressed))
        self.assertEqual(report["totals"]["visible"], 5)
        self.assertEqual(progress, [(0, 1, 0, len(compressed)), (1, 1, len(compressed), len(compressed))])


def digest(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()

"""Account for file payloads in registry images and local Docker archives."""

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import Parser
from functools import cache
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import stat
import tarfile
import tomllib
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REGISTRY = "https://ghcr.io"
REPOSITORY = "home-assistant/home-assistant"
REFERENCE = re.compile(r"(?:[\w][\w.\-]{0,127}|sha256:[a-f0-9]{64})", re.ASCII)
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
LOCAL_ARCHIVE = "ha_image_inspector.tar"
ARCHITECTURES = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def host_architecture():
    return ARCHITECTURES.get(platform.machine().lower(), "amd64")


class Registry:
    def __init__(self):
        query = urlencode({"service": "ghcr.io", "scope": f"repository:{REPOSITORY}:pull"})
        with urlopen(f"{REGISTRY}/token?{query}", timeout=30) as response:
            self.token = json.load(response)["token"]

    def open(self, endpoint, accept="application/octet-stream"):
        request = Request(f"{REGISTRY}/v2/{REPOSITORY}/{endpoint}", headers={"Accept": accept})
        # An OCI blob may redirect to signed storage. Keep the registry token at GHCR.
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return urlopen(request, timeout=45)

    def manifest(self, reference):
        with self.open(f"manifests/{reference}", ACCEPT) as response:
            raw = response.read()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if DIGEST.fullmatch(reference) and reference != digest:
            raise ValueError("Registry manifest digest mismatch")
        return json.loads(raw), digest


class CountedReader:
    def __init__(self, stream, digest=None, on_bytes=None):
        self.stream = stream
        self.digest = digest
        self.on_bytes = on_bytes
        self.next_notification = 4 * 1024 * 1024
        self.count = 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.count += len(data)
        if self.digest is not None:
            self.digest.update(data)
        if self.on_bytes is not None and self.count >= self.next_notification:
            self.next_notification = self.count + 4 * 1024 * 1024
            self.on_bytes(self.count)
        return data

    def drain(self):
        while self.read(1024 * 1024):
            pass


@dataclass(eq=False, slots=True)
class Payload:
    size: int
    layer: int


def image_path(path):
    path = posixpath.normpath(path)
    if path in (".", "..") or path.startswith(("/", "../")):
        return None
    return path


def metadata_file(path, size):
    if path.endswith(".dist-info/RECORD"):
        return size <= 16 * 1024 * 1024
    if path.endswith(".dist-info/METADATA") or path == "usr/src/homeassistant/pyproject.toml":
        return size <= 4 * 1024 * 1024
    return path.endswith("/manifest.json") and size <= 256 * 1024 and classify(path, None)[0] == "integration"


def read_layer(tar, index, files, metadata):
    """Apply whiteouts to lower layers, then replay the current layer's entries."""
    entries = []
    whiteouts = set()
    opaque = set()
    written = 0
    for member in tar:
        path = image_path(member.name)
        if not path:
            continue
        parent, name = posixpath.split(path)
        if name == ".wh..wh..opq":
            opaque.add(parent)
        elif name.startswith(".wh."):
            whiteouts.add(posixpath.join(parent, name[4:]))
        elif member.isfile():
            content = None
            if metadata_file(path, member.size):
                content = tar.extractfile(member).read().decode("utf-8", errors="replace")
            entries.append((path, "file", Payload(member.size, index), content))
            written += member.size
        elif member.islnk():
            entries.append((path, "link", image_path(member.linkname), None))
        elif member.isdir():
            entries.append((path, "directory", None, None))
        else:
            entries.append((path, "other", None, None))

    def masked(path):
        if path in whiteouts or "" in opaque:
            return True
        return any(str(parent) in whiteouts or str(parent) in opaque
                   for parent in PurePosixPath(path).parents)

    if whiteouts or opaque:
        for path in list(files):
            if masked(path):
                files.pop(path)
                metadata.pop(path, None)

    pending = {}
    directories = set()
    for path in (*files, *(entry[0] for entry in entries)):
        parent = posixpath.dirname(path)
        while parent and parent not in directories:
            directories.add(parent)
            parent = posixpath.dirname(parent)
    for path, kind, value, content in entries:
        metadata.pop(path, None)
        pending.pop(path, None)
        if kind != "directory":
            # A regular file or symlink can replace a lower-layer directory.
            if path in directories:
                prefix = path + "/"
                for previous in [item for item in files.keys() | pending.keys() if item.startswith(prefix)]:
                    files.pop(previous, None)
                    metadata.pop(previous, None)
                    pending.pop(previous, None)
        if kind == "file":
            files[path] = value
            if content is not None:
                metadata[path] = content
        elif kind == "link":
            payload = files.get(value)
            files.pop(path, None)
            if payload is None:
                pending[path] = value
            else:
                files[path] = payload
        else:
            files.pop(path, None)

    for path, target in pending.items():
        seen = {path}
        while target in pending and target not in seen:
            seen.add(target)
            target = pending[target]
        if target in files:
            files[path] = files[target]
    return written


def package_owners(records):
    owners = {}
    for path, content in sorted(records.items()):
        directory = PurePosixPath(path).parent
        distribution = directory.name.removesuffix(".dist-info").split("-", 1)[0].replace("_", "-")
        try:
            for row in csv.reader(io.StringIO(content)):
                if row and (owned := image_path(posixpath.join(str(directory.parent), row[0]))):
                    owners.setdefault(owned, distribution)
        except csv.Error:
            continue
    return owners


def classify(path, package):
    parts = path.split("/")
    tail = []
    if parts[:4] == ["usr", "src", "homeassistant", "homeassistant"]:
        tail = parts[4:]
    elif "site-packages" in parts:
        site = parts.index("site-packages") + 1
        if parts[site:site + 1] == ["homeassistant"]:
            tail = parts[site + 1:]
    if tail:
        if tail[:1] == ["components"] and len(tail) >= 3:
            return "integration", tail[1]
        return "core", "Home Assistant Core"
    if package == "homeassistant" or parts[:3] == ["usr", "src", "homeassistant"]:
        return "core", "Home Assistant Core"
    if package:
        return "python", package
    if "site-packages" in parts:
        tail = parts[parts.index("site-packages") + 1:]
        return "python", tail[0] if tail else "site-packages"
    if re.match(r"(?:usr/)?(?:local/)?lib/python\d", path):
        return "system", "Python standard library"
    if parts[0] in ("lib", "bin", "sbin") or parts[:2] in (["usr", "lib"], ["usr", "bin"], ["usr", "sbin"]) or parts[:3] in (["usr", "local", "lib"], ["usr", "local", "bin"]):
        return "system", parts[-1] if len(parts) <= (4 if parts[:2] == ["usr", "local"] else 3) else "/".join(parts[:-1][:4])
    return "other", "/" + "/".join(parts[:min(len(parts) - 1 or 1, 2)])


def normalized_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def integration_manifests(groups, metadata):
    manifests = {}
    for path, content in metadata.items():
        if not path.endswith("/manifest.json"):
            continue
        if path.startswith("custom_components/"):
            domain = path.split("/")[1]
        else:
            _, domain = classify(path, None)
            if ("integration", domain) not in groups or posixpath.basename(posixpath.dirname(path)) != domain:
                continue
        try:
            manifest = json.loads(content)
        except ValueError:
            continue
        if isinstance(manifest, dict):
            manifests[domain] = manifest
    return manifests


def link_requirements(groups, metadata, source="container", architecture=None):
    """Trace installed dependencies from HA manifests and Python distribution metadata."""
    try:
        from packaging.markers import default_environment
        from packaging.requirements import InvalidRequirement, Requirement
    except ImportError:
        return None

    packages = {normalized_name(name): group for (category, name), group in groups.items() if category == "python"}
    manifests = integration_manifests(groups, metadata)
    environment = default_environment()
    unknown = set()
    if source != "container":
        environment.update(sys_platform="linux", platform_system="Linux", os_name="posix",
                           implementation_name="cpython", platform_python_implementation="CPython",
                           platform_machine={"arm64": "aarch64", "amd64": "x86_64"}.get(architecture, ""))
        versions = {match[1] for path in metadata if (match := re.search(r"/python(\d+\.\d+)/site-packages/", path))}
        if len(versions) == 1:
            environment["python_version"] = next(iter(versions))
        else:
            unknown.add("python_version")
        unknown.update(("python_full_version", "implementation_version", "platform_release", "platform_version"))

    def parse(requirements):
        if not isinstance(requirements, list):
            return
        for text in requirements:
            try:
                yield Requirement(text)
            except (InvalidRequirement, TypeError):
                continue

    requirements_by_package = {}
    for path, content in metadata.items():
        if not path.endswith(".dist-info/METADATA"):
            continue
        headers = Parser().parsestr(content, headersonly=True)
        if name := headers.get("Name"):
            requirements_by_package[normalized_name(name)] = list(parse(headers.get_all("Requires-Dist", [])))
    if "homeassistant" not in requirements_by_package and (core := metadata.get("usr/src/homeassistant/pyproject.toml")):
        try:
            requirements_by_package["homeassistant"] = list(parse(tomllib.loads(core).get("project", {}).get("dependencies", [])))
        except tomllib.TOMLDecodeError:
            pass

    roots = {domain: list(parse(manifest.get("requirements", []))) for domain, manifest in manifests.items()}
    hard_dependencies = {}
    for domain, manifest in manifests.items():
        dependencies = manifest.get("dependencies")
        if isinstance(dependencies, list):
            hard_dependencies[domain] = [dependency for dependency in dependencies if isinstance(dependency, str)]
    if "homeassistant" in requirements_by_package:
        roots["_core"] = requirements_by_package["homeassistant"]

    def outgoing(domain):
        """Yield the requirements of hard dependencies on other HA integrations."""
        pending = list(hard_dependencies.get(domain, ()))
        seen = {domain}
        while pending:
            target = pending.pop()
            if target not in seen and target in manifests:
                seen.add(target)
                yield from roots[target]
                pending.extend(hard_dependencies.get(target, ()))

    @cache
    def applicable(requirement, extra):
        marker = requirement.marker
        if marker is None:
            return True
        if any(re.search(rf"\b{key}\b", str(marker)) for key in unknown):
            return False
        try:
            return marker.evaluate({**environment, "extra": extra})
        except (KeyError, ValueError):
            return False

    result = {}
    for domain, requirements in sorted(roots.items()):
        pending = [(req, "", True) for req in requirements]
        pending.extend((req, "", False) for req in outgoing(domain))
        visited, links = set(), {}
        while pending:
            requirement, extra, direct = pending.pop()
            name = normalized_name(requirement.name)
            if not applicable(requirement, extra) or name not in packages:
                continue
            label = packages[name]["name"]
            if direct:
                links[label] = "direct"
            else:
                links.setdefault(label, "indirect")
            for requested_extra in ("", *sorted(requirement.extras)):
                key = (name, requested_extra)
                if key not in visited:
                    visited.add(key)
                    pending.extend((req, requested_extra, False) for req in requirements_by_package.get(name, ()))
        result[domain] = links
        if domain != "_core":
            direct_names = sorted(name for name, relation in links.items() if relation == "direct")
            if direct_names and (integration := groups.get(("integration", domain))):
                integration["related"] = direct_names
            for name in direct_names:
                groups[("python", name)].setdefault("related", []).append(domain)
    return result


def inspect_layer(source, media_type, index, files, metadata):
    if media_type.endswith(("+gzip", ".gzip")):
        stream = gzip.GzipFile(fileobj=source)
    elif media_type.endswith("+zstd"):
        from compression import zstd

        stream = zstd.ZstdFile(source)
    elif media_type.endswith(".tar"):
        stream = None
    else:
        raise ValueError(f"Unsupported layer format: {media_type}")
    decoded = CountedReader(stream or source, hashlib.sha256())
    try:
        with tarfile.open(fileobj=decoded, mode="r|") as tar:
            written = read_layer(tar, index, files, metadata)
        decoded.drain()
    finally:
        if stream is not None:
            stream.close()
    return {"compressed": source.count, "tar": decoded.count, "written": written, "visible": 0,
            "diff_id": "sha256:" + decoded.digest.hexdigest()}


def layer_instruction(config, index, count):
    history = [item.get("created_by", "") for item in config.get("history", []) if not item.get("empty_layer")]
    return history[index] if len(history) == count else ""


def analyze(reference, architecture, progress=lambda *_: None):
    if not REFERENCE.fullmatch(reference) or architecture not in {"arm64", "amd64"}:
        raise ValueError("Expected an official image tag or sha256 digest and arm64 or amd64")
    registry = Registry()
    manifest, digest = registry.manifest(reference)
    if "manifests" in manifest:
        match = next((item for item in manifest["manifests"]
                      if item.get("platform", {}).get("os") == "linux"
                      and item["platform"].get("architecture") == architecture), None)
        if not match or not DIGEST.fullmatch(match["digest"]):
            raise ValueError(f"The image has no linux/{architecture} manifest")
        manifest, digest = registry.manifest(match["digest"])
    config_digest = manifest["config"]["digest"]
    if not DIGEST.fullmatch(config_digest):
        raise ValueError("Invalid image config digest")
    with registry.open(f"blobs/{config_digest}") as response:
        config_data = response.read()
    if "sha256:" + hashlib.sha256(config_data).hexdigest() != config_digest:
        raise ValueError("Registry image config digest mismatch")
    config = json.loads(config_data)
    if (config.get("os"), config.get("architecture")) != ("linux", architecture):
        raise ValueError(f"The selected digest is not for linux/{architecture}")
    files, metadata, layers = {}, {}, []
    descriptors = manifest["layers"]
    total_bytes = sum(descriptor["size"] for descriptor in descriptors)
    downloaded = 0
    for index, descriptor in enumerate(descriptors):
        progress(index, len(descriptors), downloaded, total_bytes)
        layer_digest = descriptor["digest"]
        if not DIGEST.fullmatch(layer_digest):
            raise ValueError("Invalid layer digest")
        media_type = descriptor["mediaType"]
        def report_download(count):
            progress(index, len(descriptors), downloaded + count, total_bytes)

        with registry.open(f"blobs/{layer_digest}") as response:
            compressed = CountedReader(response, hashlib.sha256(), report_download)
            layer = inspect_layer(compressed, media_type, index, files, metadata)
            if compressed.count != descriptor["size"] or "sha256:" + compressed.digest.hexdigest() != layer_digest:
                raise ValueError("Registry layer size or digest mismatch")
        layer.update(digest=layer_digest, instruction=layer_instruction(config, index, len(descriptors)))
        layers.append(layer)
        downloaded += layer["compressed"]
    progress(len(layers), len(layers), downloaded, total_bytes)
    return summarize(reference, digest, architecture, "registry", layers, files, metadata)


def analyze_archive(path, progress=lambda *_: None):
    """Read one Docker image export without extracting files or contacting a registry."""
    files, metadata, layers = {}, {}, []
    with tarfile.open(path, "r:") as archive:
        def open_member(name):
            member = archive.getmember(name)
            if not member.isfile():
                raise ValueError(f"Expected a regular Docker archive member: {name}")
            return archive.extractfile(member)

        try:
            with open_member("manifest.json") as stream:
                entries = json.load(stream)
            if len(entries) != 1:
                raise ValueError("Export one image and one platform with docker image save")
            entry = entries[0]
            with open_member(entry["Config"]) as stream:
                config_data = stream.read()
            config = json.loads(config_data)
            if config.get("os") != "linux":
                raise ValueError("The Docker archive must contain a Linux image")
            digest = "sha256:" + hashlib.sha256(config_data).hexdigest()
            names = entry.get("RepoTags") or []
            descriptors = entry["Layers"]
            diff_ids = config.get("rootfs", {}).get("diff_ids", [])
            if len(diff_ids) != len(descriptors):
                raise ValueError("Docker archive layer count differs from the image configuration")
            for index, name in enumerate(descriptors):
                progress(index, len(descriptors))
                with open_member(name) as stream:
                    magic = stream.read(4)
                    stream.seek(0)
                    media_type = "layer.tar"
                    if magic.startswith(b"\x1f\x8b"):
                        media_type += "+gzip"
                    elif magic == b"\x28\xb5\x2f\xfd":
                        media_type += "+zstd"
                    layer = inspect_layer(CountedReader(stream), media_type, index, files, metadata)
                # The config stores hashes of the uncompressed layer tar streams.
                if layer["diff_id"] != diff_ids[index]:
                    raise ValueError(f"Docker archive layer {index + 1} digest mismatch")
                layer.update(digest=layer["diff_id"], instruction=layer_instruction(config, index, len(descriptors)))
                layers.append(layer)
        except (KeyError, TypeError, IndexError) as error:
            raise ValueError("Invalid docker image save archive") from error
    progress(len(layers), len(layers))
    return summarize(names[0] if names else digest, digest, config["architecture"], "local", layers, files, metadata)


def read_filesystem(root, excluded):
    """Count visible regular file contents without crossing any listed mounts."""
    files, metadata, hardlinks = {}, {}, {}
    for directory, children, names in os.walk(root):
        parent = Path(directory)
        children[:] = [name for name in children if parent / name not in excluded]
        for name in names:
            item = parent / name
            if item in excluded:
                continue
            try:
                info = item.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                path = item.relative_to(root).as_posix()
                payload = Payload(info.st_size, 0)
                if info.st_nlink > 1:
                    payload = hardlinks.setdefault((info.st_dev, info.st_ino), payload)
                files[path] = payload
                if metadata_file(path, info.st_size):
                    metadata[path] = item.read_text(encoding="utf-8", errors="replace")
            except (FileNotFoundError, PermissionError):
                continue
    return files, metadata


def analyze_container(reference, config_dir="/config"):
    """Scan the running HA container, leaving mounted volumes and pseudo-filesystems out."""
    if not any(Path(marker).is_file() for marker in ("/.dockerenv", "/run/.containerenv")):
        raise ValueError("container_required")
    excluded = {Path(config_dir).resolve(), *(Path(path) for path in
                ("/dev", "/proc", "/sys", "/.dockerenv", "/run/.containerenv"))}
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4])
        if mount != "/":
            excluded.add(Path(mount))
    files, metadata = read_filesystem(Path("/"), excluded)
    custom_components = Path(config_dir) / "custom_components"
    for manifest in sorted(custom_components.glob("*/manifest.json")):
        domain = manifest.parent.name
        if not re.fullmatch(r"[a-z0-9_]+", domain) or domain == "_core":
            continue
        try:
            if manifest.is_file() and manifest.stat().st_size <= 256 * 1024:
                metadata[f"custom_components/{domain}/manifest.json"] = manifest.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, PermissionError):
            continue
    return summarize(reference, "", host_architecture(), "container", [], files, metadata)


def summarize(reference, digest, architecture, source, layers, files, metadata):
    owners = package_owners({path: value for path, value in metadata.items() if path.endswith(".dist-info/RECORD")})
    groups = {}
    seen = set()
    for path, payload in sorted(files.items(), key=lambda item: (-item[1].size, item[0])):
        if payload in seen:
            continue
        seen.add(payload)
        if layers:
            layers[payload.layer]["visible"] += payload.size
        category, name = classify(path, owners.get(path))
        group = groups.setdefault((category, name), {"category": category, "name": name, "bytes": 0, "files": 0, "largest": []})
        group["bytes"] += payload.size
        group["files"] += 1
        if len(group["largest"]) < 5 and payload.size:
            group["largest"].append({"path": "/" + path, "bytes": payload.size})

    dependencies = link_requirements(groups, metadata, source, architecture)
    totals = {key: sum(layer[key] for layer in layers) for key in ("compressed", "tar", "written", "visible")}
    if source == "container":
        totals = {"visible": sum(group["bytes"] for group in groups.values())}
    return {"generated": datetime.now(timezone.utc).isoformat(), "reference": reference, "source": source,
            "digest": digest, "architecture": architecture, "layers": layers,
            "dependencies": dependencies,
            "totals": totals,
            "groups": sorted(groups.values(), key=lambda group: (-group["bytes"], group["name"]))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("reference", nargs="?", help="Official Home Assistant image tag or manifest digest")
    source.add_argument("--archive", help="Inspect a local docker image save tar archive")
    source.add_argument("--container", metavar="LABEL", help="Read visible files inside the running container")
    parser.add_argument("--architecture", choices=("arm64", "amd64"), default=host_architecture())
    args = parser.parse_args()
    if args.container is not None:
        result = analyze_container(args.container)
    elif args.archive:
        result = analyze_archive(args.archive)
    else:
        result = analyze(args.reference, args.architecture)
    print(json.dumps(result))

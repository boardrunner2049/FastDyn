"""mdBook browser assets for a manual installation (no Node or Nix required)."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tomllib
from urllib.request import urlopen


def stage(root, source, prebuilt=None):
    pins = (root / "assets.toml").read_bytes()
    version = hashlib.sha256(pins).hexdigest()
    destination = root / source / "vendor"
    marker = destination / ".asset-version"
    if marker.exists() and marker.read_text().strip() == version:
        return
    if prebuilt:
        shutil.copytree(prebuilt, destination, dirs_exist_ok=True)
        for path in (destination, *destination.rglob("*")):
            path.chmod(path.stat().st_mode | 0o200)
        marker.write_text(version + "\n")
        return
    settings = tomllib.loads(pins.decode())
    cache = root.parent / "out/docs-downloads"
    cache.mkdir(parents=True, exist_ok=True)
    for name, package in settings.items():
        expected = base64.b64decode(package["hash"].removeprefix("sha256-"))
        archive = cache / (expected.hex() + ".tgz")
        if not archive.exists():
            print(f"Downloading {name} documentation assets", file=sys.stderr)
            with urlopen(package["url"], timeout=60) as response:
                data = response.read()
            if hashlib.sha256(data).digest() != expected:
                raise ValueError(f"Incorrect archive hash for {name}")
            archive.write_bytes(data)
        data = archive.read_bytes()
        if hashlib.sha256(data).digest() != expected:
            raise ValueError(f"Corrupt cached archive: {archive}; remove it and retry")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            files = {"package/LICENSE": f"licenses/{name}-LICENSE"}
            if name == "rumoca":
                files["package/modelica_language.js"] = "rumoca/modelica_language.js"
            else:
                files["package/ThirdPartyNotices.txt"] = "licenses/monaco-ThirdPartyNotices.txt"
                for member in tar.getmembers():
                    prefix = "package/min/"
                    if member.isfile() and member.name.startswith(prefix):
                        files[member.name] = "monaco/" + member.name.removeprefix(prefix)
            for member, relative in files.items():
                target = destination / relative
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError(f"Invalid archive path: {member}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    marker.write_text(version + "\n")


def repair_reference_links(book, context):
    """Included reference pages retain links relative to their original docs/ path."""
    pages = {
        "VirtualsAndModifiers.md": "general/virtuals.md",
        "WritingVirtuals.md": "general/writing-virtuals.md",
        "FunctionCounterPlugin.md": "general/function-counters.md",
        "VariableWatch.md": "general/variable-watches.md",
        "IntrospectionExamples.md": "general/rtos.md",
        "Configuration.md": "general/configuration.md",
    }
    html = context["config"]["output"]["html"]
    repository = html["git-repository-url"].rstrip("/")
    # configure_pages.py supplies the correct default branch in CI.
    edit = html["edit-url-template"]
    revision_url = edit.split("/edit/", 1)[-1].removesuffix("/docs/book/{path}")
    def visit(items):
        for item in items:
            chapter = item.get("Chapter")
            if not chapter:
                continue
            path = chapter.get("source_path") or ""
            if path.startswith("general/") and path not in ("general/overview.md", "general/environment.md", "general/instrumentation.md"):
                def replace(match):
                    link = match.group(1)
                    target, separator, fragment = link.partition("#")
                    if not target or ":" in target or target.startswith("/"):
                        return match.group(0)
                    if target in pages:
                        result = os.path.relpath(pages[target], Path(path).parent)
                    elif target.endswith((".md", ".toml")):
                        relative = os.path.normpath("docs/" + target)
                        result = f"{repository}/blob/{revision_url}/{relative}"
                    else:
                        return match.group(0)
                    return "](" + result + (separator + fragment if separator else "") + ")"
                chapter["content"] = re.sub(r"\]\(([^)]+)\)", replace, chapter["content"])
            visit(chapter.get("sub_items", []))
    visit(book.get("items", book.get("sections", [])))


def main():
    args = sys.argv[1:]
    prebuilt = None
    if args[:1] == ["--prebuilt"]:
        prebuilt, args = args[1], args[2:]
    if args[:1] == ["supports"]:
        raise SystemExit(0 if args[1:] == ["html"] else 1)
    context, book = json.load(sys.stdin)
    stage(Path(context["root"]), context["config"]["book"].get("src", "src"), prebuilt)
    repair_reference_links(book, context)
    json.dump(book, sys.stdout)


if __name__ == "__main__":
    main()

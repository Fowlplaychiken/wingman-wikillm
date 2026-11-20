"""Build the public skill from an explicit file list."""
import argparse
import hashlib
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "wiki_llm"
FILES = (
    "__init__.py", "__main__.py", "main.py", "default_config.yaml",
    "dependencies/wingman_wiki/__init__.py", "dependencies/wingman_wiki/reference.py",
    "dependencies/wingman_wiki/library.py", "dependencies/wingman_wiki/corpus.py",
    "dependencies/wingman_wiki/refresh.py", "guides/README.md",
    "guides/environmental-indicators.md", "guides/engineering-displays.md",
    "guides/getting-started-flight.md", "guides/industrial-careers.md",
    "guides/missions-survival-exploration.md", "guides/wingman-modules.md",
)


def package_members(version):
    # Wingman may reload a skill while Python still has its old imports cached.
    namespace = "wingman_wiki_v" + version.replace(".", "_")
    members = {}
    for name in FILES:
        content = (PACKAGE / name).read_text(encoding="utf-8")
        if name in {"main.py", "__main__.py"}:
            content = content.replace("wingman_wiki.", namespace + ".")
        members[name.replace("dependencies/wingman_wiki/", f"dependencies/{namespace}/")] = content.encode()
    for name in ("LICENSE", "README.md"):
        members[name] = (ROOT / name).read_text(encoding="utf-8").encode()
    return members


def build(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    version = re.search(r"^version: ([0-9]+\.[0-9]+\.[0-9]+)$",
                        (PACKAGE / "default_config.yaml").read_text(encoding="utf-8"), re.M)[1]
    archive = directory / f"wiki_llm-{version}.zip"
    members = package_members(version)
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in sorted(members.items()):
            entry = zipfile.ZipInfo("wiki_llm/" + name, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system, entry.external_attr = 3, 0o100644 << 16
            bundle.writestr(entry, data)
    with zipfile.ZipFile(archive) as bundle:
        if any(bundle.read("wiki_llm/" + name) != data for name, data in members.items()):
            raise ValueError("Packaged content differs from the skill source.")
    return archive


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--out", type=Path, default=ROOT / "dist")
    archive = build(cli.parse_args().out)
    print(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive}")

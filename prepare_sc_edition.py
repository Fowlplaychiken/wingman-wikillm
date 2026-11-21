"""Build a verified, deterministic JSONL gzip from the licensed SC Wiki archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile

from wiki_llm.dependencies.wingman_wiki import sc_archive


def _source_hashes(path: Path) -> tuple[int, str, str]:
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
    return size, sha1.hexdigest(), sha256.hexdigest()


def _verify_source(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ValueError("source archive path must be an existing file")
    size, sha1, sha256 = _source_hashes(path)
    if (size != sc_archive.ARCHIVE_SIZE or sha1 != sc_archive.SHA1
            or sha256 != sc_archive.SHA256):
        raise ValueError("source archive size or digest does not match the pinned edition")
    return {"source_size": size, "source_sha1": sha1, "source_sha256": sha256}


def _row(article: dict[str, object]) -> dict[str, object]:
    """Keep public article provenance while excluding revision contributor names."""
    return {
        "url": article["url"],
        "title": article["title"],
        "text": article["text"],
        "revision": article["revision"],
        "revision_timestamp": article["revision_timestamp"],
        "source_page_id": article["source_page_id"],
        "namespace": 0,
        "source_origin": sc_archive.SOURCE_ORIGIN,
        "archive_identifier": sc_archive.ARCHIVE_IDENTIFIER,
        "archive_url": sc_archive.ARCHIVE_URI,
        "archive_file_url": sc_archive.ARCHIVE_FILE_URI,
        "snapshot_date": sc_archive.SNAPSHOT_DATE,
        "license": "CC BY-SA 4.0",
        "license_url": sc_archive.LICENSE_URL,
        "attribution": sc_archive.ATTRIBUTION,
        "attribution_url": article["history_url"],
        "history_notice": sc_archive.HISTORY_NOTICE,
        "historical_snapshot": True,
    }


def prepare(source_path: Path, output_path: Path) -> dict[str, object]:
    """Verify the exact archive, then atomically write deterministic JSONL gzip."""
    source_path = source_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if source_path == output_path:
        raise ValueError("source and output paths must differ")
    source_receipt = _verify_source(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=output_path.name + ".", suffix=".tmp", dir=output_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    counts: dict[str, int] = {}
    try:
        with source_path.open("rb") as compressed, temporary_path.open("wb") as raw:
            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", compresslevel=9, mtime=0
            ) as output:
                with sc_archive.open_zstd(compressed) as expanded:
                    for article in sc_archive.iter_articles(expanded, counts):
                        encoded = json.dumps(
                            _row(article), ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        output.write(encoded + b"\n")
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    output_size, output_sha256 = _file_sha256(output_path)
    reasons = {
        key: value for key, value in sorted(counts.items())
        if key.startswith("skipped_")
    }
    return {
        **source_receipt,
        "output_path": str(output_path),
        "output_size": output_size,
        "output_sha256": output_sha256,
        "counts": dict(sorted(counts.items())),
        "skip_counts_by_reason": reasons,
    }


def _file_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="verified .zst snapshot archive")
    parser.add_argument("output", type=Path, help="output .jsonl.gz path")
    args = parser.parse_args(argv)
    try:
        receipt = prepare(args.source, args.output)
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import hashlib
import os
import urllib.request
from pathlib import Path

COMMIT = "87416418657359cb625c412a48b6e1d6d41c29bd"
BASE_URL = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/{COMMIT}"
FILES = {
    "eng.traineddata": "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "osd.traineddata": "9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff",
    "script/Latin.traineddata": (
        "6dbdaf8ecc6c40f025c2648bf3b3f3fbffe073e1fd2df2047fde2e2b2f020d53"
    ),
}


def fetch(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for relative, expected_hash in FILES.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and _sha256(target) == expected_hash:
            continue
        partial = target.with_suffix(target.suffix + ".partial")
        try:
            urllib.request.urlretrieve(f"{BASE_URL}/{relative}", partial)
            actual_hash = _sha256(partial)
            if actual_hash != expected_hash:
                raise RuntimeError(
                    f"Checksum mismatch for {relative}: {actual_hash} != {expected_hash}"
                )
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    fetch(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

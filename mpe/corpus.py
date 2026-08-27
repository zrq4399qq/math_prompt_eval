"""Loading the exemplar corpus."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EXEMPLAR_DIR = DATA_DIR / "exemplars"
CACHE_DIR = DATA_DIR / "cache"


@dataclass
class Doc:
    name: str
    text: str
    path: Path

    @property
    def n_chars(self) -> int:
        return len(self.text)


def load_exemplars(directory: Path = EXEMPLAR_DIR) -> List[Doc]:
    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(
            f"No exemplar directory at {directory}. See README for how to populate it."
        )
    docs = []
    for path in sorted(directory.glob("*.md")):
        docs.append(Doc(name=path.stem, text=path.read_text(encoding="utf-8"), path=path))
    if not docs:
        raise FileNotFoundError(f"No .md exemplars found in {directory}")
    return docs


def read_prompt(path) -> Doc:
    path = Path(path)
    return Doc(name=path.stem, text=path.read_text(encoding="utf-8"), path=path)

"""Publish new evidence artifacts without changing an installed review basis."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


RUNTIME_MARKERS = (
    "judgments.sqlite3", "judgments.sqlite3-wal", "judgments.sqlite3-shm",
    "judgments.sqlite3-journal", ".data.lock",
)


def candidate_output_path(output: Path, *, source_tree: Path | None = None,
                          input_artifacts: tuple[Path, ...] = ()) -> Path:
    """Require a new artifact outside source checkouts and review runtime data."""
    requested = output.expanduser().absolute()
    # exists() alone would overlook a dangling symlink. An artifact must never
    # replace a file or use an existing symlink as a new destination.
    if requested.exists() or requested.is_symlink():
        raise FileExistsError(f"candidate output already exists: {requested}; choose a new --output path")
    destination = requested.resolve()
    if destination.name in RUNTIME_MARKERS:
        raise ValueError(f"candidate output cannot use a review runtime filename: {destination}")
    if source_tree is not None and destination.is_relative_to(source_tree.expanduser().resolve()):
        raise ValueError("candidate output must be outside the reviewed-source checkout")
    if destination in {path.expanduser().resolve() for path in input_artifacts}:
        raise ValueError("candidate output must be separate from the input artifacts")
    for parent in destination.parents:
        if any((parent / marker).exists() or (parent / marker).is_symlink()
               for marker in RUNTIME_MARKERS):
            raise ValueError(f"candidate output must be outside the review runtime data directory: {parent}")
    return destination


def write_candidate_artifact(payload: dict, output: Path, *, source_tree: Path | None = None,
                             input_artifacts: tuple[Path, ...] = ()) -> Path:
    """Atomically publish one complete, private candidate, refusing any overwrite."""
    destination = candidate_output_path(output, source_tree=source_tree,
                                        input_artifacts=input_artifacts)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".snapshot-candidate-", delete=False) as artifact:
            temporary = Path(artifact.name)
            json.dump(payload, artifact, ensure_ascii=False, indent=2)
            artifact.write("\n")
            artifact.flush()
            os.fsync(artifact.fileno())
        # Recheck runtime markers after writing, then publish with an exclusive
        # hard link. A racing file or symlink creation cannot be overwritten.
        candidate_output_path(destination, source_tree=source_tree,
                              input_artifacts=input_artifacts)
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    # Persist the directory entry as well as the JSON bytes before success.
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return destination

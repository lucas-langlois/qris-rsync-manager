from __future__ import annotations

import os
import stat
from pathlib import Path


def move_local_paths(
    source_paths: list[str | Path],
    destination_directory: str | Path,
    allowed_root: str | Path,
) -> list[tuple[Path, Path]]:
    moves = plan_local_moves(source_paths, destination_directory, allowed_root)
    completed: list[tuple[Path, Path]] = []
    for source, destination in moves:
        source.rename(destination)
        completed.append((source, destination))
    return completed


def plan_local_moves(
    source_paths: list[str | Path],
    destination_directory: str | Path,
    allowed_root: str | Path,
) -> list[tuple[Path, Path]]:
    root = Path(allowed_root).expanduser().absolute()
    destination_dir = Path(destination_directory).expanduser().absolute()
    if not destination_dir.is_dir():
        raise ValueError("The local drop destination must be an existing folder.")
    if _is_reparse_point(destination_dir):
        raise ValueError("Moving items into a linked folder is not supported.")
    if not destination_dir.is_relative_to(root):
        raise ValueError("The local destination must stay inside the displayed folder.")

    sources = _deduplicate_sources(source_paths)
    if not sources:
        raise ValueError("At least one local source path is required.")
    moves: list[tuple[Path, Path]] = []
    destination_names: set[str] = set()
    for source in sources:
        if not source.exists() and not source.is_symlink():
            raise ValueError(f"Local source no longer exists: {source}")
        if source == root or not source.is_relative_to(root):
            raise ValueError("Local sources must stay inside the displayed folder and cannot be its root.")
        if destination_dir == source or destination_dir.is_relative_to(source):
            raise ValueError("A local folder cannot be moved into itself or one of its subfolders.")
        destination = destination_dir / source.name
        if source.parent == destination_dir:
            raise ValueError(f"{source.name} is already in the destination folder.")
        name_key = source.name.casefold()
        if name_key in destination_names:
            raise ValueError("Selected local items would have duplicate destination names.")
        destination_names.add(name_key)
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"Destination already exists: {destination}")
        moves.append((source, destination))

    return moves


def _deduplicate_sources(source_paths: list[str | Path]) -> list[Path]:
    unique = sorted(
        {Path(path).expanduser().absolute() for path in source_paths},
        key=lambda item: (len(item.parts), str(item).casefold()),
    )
    result: list[Path] = []
    for source in unique:
        if any(source.is_relative_to(parent) for parent in result):
            continue
        result.append(source)
    return result


def _is_reparse_point(path: Path) -> bool:
    info = os.lstat(path)
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)

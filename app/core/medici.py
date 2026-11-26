from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .profiles import Profile
from .remote_dirs import build_remote_ssh_base


def medici_path_for_remote_path(remote_path: str) -> str:
    clean = remote_path.strip().rstrip("/")
    if not clean:
        raise ValueError("Remote path is required.")
    if clean.startswith("/QRISdata/"):
        return clean
    if clean.startswith("/RDS/"):
        return clean
    if clean.startswith("/data/"):
        return "/QRISdata/" + clean.removeprefix("/data/")
    raise ValueError("MeDiCI recall paths must start with /data/, /QRISdata/, or /RDS/.")


def recall_file_path_for_remote_path(remote_path: str) -> str:
    clean = remote_path.strip().rstrip("/")
    if not clean:
        raise ValueError("Remote file path is required.")
    if clean.startswith(("/data/", "/QRISdata/", "/RDS/")):
        return clean
    raise ValueError("MeDiCI recall file paths must start with /data/, /QRISdata/, or /RDS/.")


def glob_escape(path: str) -> str:
    """Make a path match only itself when ``recall_medici`` expands it.

    recall_medici treats every argument as a glob pattern, so a file named
    ``site[1].jpg`` would otherwise not be found.
    """
    return "".join(f"[{character}]" if character in "*?[" else character for character in path)


def unescape_glob(path: str) -> str:
    """Undo :func:`glob_escape`."""
    return re.sub(r"\[([*?\[])\]", r"\1", path)


def qriscloud_path_for_bunya_path(path: str) -> str:
    """/QRISdata/Qxxxx on Bunya is /data/Qxxxx on the QRIScloud data nodes."""
    clean = unescape_glob(path.strip())
    return "/data/" + clean.removeprefix("/QRISdata/") if clean.startswith("/QRISdata/") else clean


def build_recall_medici_command(
    profile: Profile,
    remote_path: str,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    medici_path = medici_path_for_remote_path(remote_path)
    remote_command = (
        "if command -v recall_medici >/dev/null 2>&1; then "
        f"recall_medici {shlex.quote(medici_path)}; "
        "else echo 'recall_medici was not found on this host. Try a QRIScloud/RCC HPC login node or contact UQ ITS.' >&2; "
        "exit 127; fi"
    )
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def build_recall_medici_files_command(
    profile: Profile,
    remote_files: list[str],
    ssh_path: str | None = None,
    batch_mode: bool = True,
    batch_size: int = 25,
) -> list[str]:
    if not remote_files:
        raise ValueError("At least one remote file is required.")
    medici_files = [recall_file_path_for_remote_path(path) for path in remote_files]
    batches: list[str] = []
    size = max(1, int(batch_size))
    for index in range(0, len(medici_files), size):
        batch = medici_files[index : index + size]
        batches.append("recall_medici " + " ".join(shlex.quote(glob_escape(path)) for path in batch))
    remote_command = (
        "if command -v recall_medici >/dev/null 2>&1; then "
        + "; ".join(batches)
        + "; else echo 'recall_medici was not found on this host. Try a QRIScloud/RCC HPC login node or contact UQ ITS.' >&2; "
        "exit 127; fi"
    )
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


# --- Tape status ------------------------------------------------------------------
#
# QRISdata collections are a disk cache of the Medici store. A file moved to
# tape keeps its full listed size but leaves only a stub on disk: checked live
# on /data/Q5722, a 7.7 MB photo on tape used 1 block (512 bytes). Reading it
# triggers a slow recall.
#
# GPFS stores files of up to about 4 KB inside the inode, so they use 0-1
# blocks even on disk; the block count says nothing for them, and they are
# always treated as on disk.

BLOCK_BYTES = 512
INODE_DATA_MAX_BYTES = 4096


@dataclass(frozen=True)
class TapeFile:
    path: str
    size: int
    blocks: int

    @property
    def state(self) -> str:
        if self.size > INODE_DATA_MAX_BYTES and self.blocks * BLOCK_BYTES < self.size // 2:
            return "tape"
        return "disk"


@dataclass
class TapeStatus:
    on_disk_count: int = 0
    on_disk_bytes: int = 0
    on_tape: list[TapeFile] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return self.on_disk_count + len(self.on_tape)

    @property
    def needs_recall(self) -> list[TapeFile]:
        return self.on_tape

    @property
    def recall_bytes(self) -> int:
        return sum(item.size for item in self.on_tape)

    def add(self, item: TapeFile) -> None:
        if item.state == "tape":
            self.on_tape.append(item)
        else:
            self.on_disk_count += 1
            self.on_disk_bytes += item.size


def parse_tape_line(raw_line: str) -> TapeFile | None:
    """Parse one line of ``find -printf`` output: blocks, size and path, tab-separated."""
    parts = raw_line.rstrip("\r\n").split("\t", 2)
    if len(parts) != 3:
        return None
    blocks, size, path = parts
    try:
        return TapeFile(path, int(size), int(blocks))
    except ValueError:
        return None


def build_tape_status_command(
    profile: Profile,
    remote_paths: list[str],
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    """Read-only: list each file's size and allocated disk blocks."""
    if not remote_paths:
        raise ValueError("Choose a folder or files to check.")
    paths = []
    for path in remote_paths:
        clean = recall_file_path_for_remote_path(path)
        if any(ord(character) < 32 for character in clean):
            raise ValueError(f"Remote path contains a control character: {clean!r}")
        paths.append(clean)
    remote_command = (
        "find "
        + " ".join(shlex.quote(path) for path in paths)
        + r" \( -name .afm -o -name .qris-receipts \) -prune -o -type f -printf '%b\t%s\t%p\n'"
    )
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


def build_listed_files_status_command(
    profile: Profile,
    ssh_path: str | None = None,
    batch_mode: bool = True,
) -> list[str]:
    """Read-only: tape state of the files named on stdin, one path per line.

    Prints the same blocks/size/path lines as :func:`build_tape_status_command`.
    Used to follow a recall job's own file list.
    """
    remote_command = r"xargs -r -d '\n' stat --printf '%b\t%s\t%n\n'"
    return [*build_remote_ssh_base(profile, ssh_path=ssh_path, batch_mode=batch_mode), remote_command]


# --- Bunya recall job ------------------------------------------------------------------
#
# RCC asks for recalls to run on a Bunya compute node, not a login node, with
# at most 4 recalls at a time because tape drives are shared. recall_medici
# does not reliably recurse, so the job recalls an explicit list of files.
# https://docs.hpc.rcc.uq.edu.au/guides/data/bunya-userdata-guide/

BUNYA_DEFAULT_ACCOUNT = "a_jcu_hpc"
BUNYA_RECALL_PARALLEL = 4
BUNYA_RECALL_WALLTIME = "7-00:00:00"
BUNYA_PROGRESS_MINUTES = 5
_ACCOUNT_CHARACTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def bunya_recall_files(remote_files: list[str]) -> list[str]:
    """Bunya paths for *remote_files*: /data/Qxxxx is /QRISdata/Qxxxx there."""
    paths = []
    for path in remote_files:
        clean = medici_path_for_remote_path(path)
        if any(ord(character) < 32 for character in clean):
            raise ValueError(f"Remote path contains a control character: {clean!r}")
        paths.append(glob_escape(clean))
    if not paths:
        raise ValueError("There are no files to recall.")
    return paths


def build_bunya_recall_job(account: str, list_name: str, job_name: str = "recall_medici") -> str:
    """Slurm batch script that recalls every file named in *list_name*."""
    account = account.strip()
    if not account or set(account) - _ACCOUNT_CHARACTERS:
        raise ValueError("Enter a Bunya account such as a_jcu_hpc (letters, numbers, _ and - only).")
    if set(list_name) - _ACCOUNT_CHARACTERS - {"."}:
        raise ValueError(f"Unexpected file-list name: {list_name}")
    lines = [
        "#!/bin/bash --login",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --account={account}",
        "#SBATCH --partition=general",
        "#SBATCH --qos=normal",
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        f"#SBATCH --cpus-per-task={BUNYA_RECALL_PARALLEL}",
        "#SBATCH --mem=4G",
        f"#SBATCH --time={BUNYA_RECALL_WALLTIME}",
        f"#SBATCH --output={job_name}_%j.log",
        "#",
        "# Recall QRISdata files from Medici tape. Written by QRIS Rsync Manager.",
        "# It only reads files, so they are brought back to disk; nothing is changed.",
        "#",
        "# 1. Copy this script and its file list to your Bunya scratch or home folder.",
        "#    Do not run it from inside /QRISdata.",
        "# 2. Submit it:     sbatch " + job_name + ".slurm",
        "# 3. Check it:      squeue --me     (log: " + job_name + "_<jobid>.log)",
        f"#    The log shows progress every {BUNYA_PROGRESS_MINUTES} minutes; "
        + job_name + "_<jobid>_done.txt",
        "#    lists each finished file as OK or FAILED.",
        "# To resume after it stops, run Check tape status again in QRIS Rsync Manager",
        "# and create a new job: it lists only the files that are still on tape.",
        "",
        "set -u",
        'cd "$SLURM_SUBMIT_DIR" || exit 1',
        'case "$SLURM_SUBMIT_DIR" in',
        '  /QRISdata*) echo "Submit this job from your scratch or home folder, not /QRISdata." >&2; exit 2 ;;',
        "esac",
        f"LIST={list_name}",
        'if [ ! -f "$LIST" ]; then echo "File list $LIST not found next to this script." >&2; exit 2; fi',
        'TOTAL=$(grep -c . "$LIST")',
        f'DONE="{job_name}_${{SLURM_JOB_ID:-local}}_done.txt"',
        ': > "$DONE"',
        "export DONE",
        "",
        "# One line per finished file. Short appends are atomic, so the 4 parallel",
        "# recalls can share the file.",
        "recall_one() {",
        '  if /usr/local/bin/recall_medici "$1"; then',
        "    printf 'OK\\t%s\\n' \"$1\" >> \"$DONE\"",
        "  else",
        "    printf 'FAILED\\t%s\\n' \"$1\" >> \"$DONE\"",
        "  fi",
        "}",
        "export -f recall_one",
        "",
        "progress() {",
        "  OK=$(grep -c '^OK' \"$DONE\")",
        "  BAD=$(grep -c '^FAILED' \"$DONE\")",
        '  echo "$(date): $((OK + BAD)) of $TOTAL done ($OK recalled, $BAD failed)."',
        "}",
        "",
        f'echo "$(date): recalling $TOTAL files, {BUNYA_RECALL_PARALLEL} at a time."',
        # "sleep & wait" so stopping the progress loop does not wait out the sleep.
        f"( trap 'kill $! 2>/dev/null; exit 0' TERM; while :; do sleep {BUNYA_PROGRESS_MINUTES * 60} & wait $!; progress; done ) &",
        "MONITOR=$!",
        f"xargs -d '\\n' -n 1 -P {BUNYA_RECALL_PARALLEL} bash -c 'recall_one \"$1\"' _ < \"$LIST\"",
        "STATUS=$?",
        '{ kill "$MONITOR" && wait "$MONITOR"; } 2>/dev/null',
        "progress",
        "if grep -q '^FAILED' \"$DONE\"; then",
        '  echo "Files that could not be recalled:"',
        "  # Paths are listed as given to recall_medici; [x] stands for the character x.",
        "  grep '^FAILED' \"$DONE\" | cut -f2-",
        "  [ \"$STATUS\" -eq 0 ] && STATUS=1",
        "fi",
        'echo "$(date): finished with status $STATUS (0 means every file was recalled)."',
        "exit $STATUS",
    ]
    return "\n".join(lines) + "\n"


def write_bunya_recall_job(folder: Path, remote_files: list[str], account: str, stamp: str) -> tuple[Path, Path]:
    """Write ``recall_medici_<stamp>.slurm`` and its file list into *folder*.

    Both use Unix line endings so Bunya can run them as they are.
    """
    name = f"recall_medici_{stamp}"
    list_name = f"{name}_files.txt"
    script_text = build_bunya_recall_job(account, list_name, name)
    files = bunya_recall_files(remote_files)
    folder = Path(folder)
    list_path = folder / list_name
    script_path = folder / f"{name}.slurm"
    list_path.write_text("\n".join(files) + "\n", encoding="utf-8", newline="\n")
    script_path.write_text(script_text, encoding="utf-8", newline="\n")
    return script_path, list_path

"""Submit a tape-recall job to UQ RCC's Bunya cluster over SSH.

Bunya logins use an SSH key plus MFA, so the connection always runs with the
interactive askpass window (``build_interactive_askpass_environment``). One
connection uploads the job files and submits them, so one MFA approval covers
one submission. The app never stores MFA codes.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path

from .app_settings import get_setting, set_setting
from .medici import BUNYA_DEFAULT_ACCOUNT
from .paths import app_data_dir
from .profiles import Profile
from .remote_dirs import build_remote_ssh_base

BUNYA_HOST = "bunya.rcc.uq.edu.au"
_NAME_CHARACTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
_JOB_ID = re.compile(r"Submitted batch job (\d+)")


@dataclass
class BunyaSettings:
    account: str
    username: str
    key_path: str
    folder: str

    @classmethod
    def load(cls, default_username: str = "") -> "BunyaSettings":
        username = get_setting("bunya_username", default_username)
        default_key = Path.home() / ".ssh" / "bunya_ed25519"
        return cls(
            account=get_setting("bunya_account", BUNYA_DEFAULT_ACCOUNT),
            username=username,
            key_path=get_setting("bunya_key_path", str(default_key) if default_key.exists() else ""),
            folder=get_setting("bunya_folder", f"/scratch/user/{username}/qris_recall" if username else ""),
        )

    def save(self) -> None:
        set_setting("bunya_account", self.account)
        set_setting("bunya_username", self.username)
        set_setting("bunya_key_path", self.key_path)
        set_setting("bunya_folder", self.folder)

    def errors(self) -> list[str]:
        problems = []
        if not self.username or set(self.username) - _NAME_CHARACTERS:
            problems.append("Enter your Bunya username (usually your UQ username).")
        if self.key_path and not Path(self.key_path).expanduser().is_file():
            problems.append(f"SSH key not found: {self.key_path}")
        folder = self.folder.strip()
        if not folder.startswith(("/scratch/", "/home/")) or any(ord(c) < 32 for c in folder) or ".." in folder.split("/"):
            problems.append("The Bunya folder must be in /scratch/... or /home/... (not /QRISdata).")
        return problems

    def profile(self) -> Profile:
        return Profile(
            name="Bunya",
            host=BUNYA_HOST,
            username=self.username,
            ssh_key_path=self.key_path,
        )


def build_bunya_submit_command(
    settings: BunyaSettings,
    script_name: str,
    list_name: str,
    script_bytes: int,
    ssh_path: str | None = None,
) -> list[str]:
    """ssh command that saves the job files from stdin into the folder and runs sbatch.

    stdin carries the script (exactly *script_bytes* bytes) followed by the
    file list. Nothing else is changed on Bunya.
    """
    for name in (script_name, list_name):
        if not name or set(name) - _NAME_CHARACTERS:
            raise ValueError(f"Unexpected job file name: {name}")
    folder = shlex.quote(settings.folder.rstrip("/"))
    remote = (
        f"mkdir -p {folder} && cd {folder} && "
        f"head -c {int(script_bytes)} > {shlex.quote(script_name)} && "
        f"cat > {shlex.quote(list_name)} && "
        f"sbatch {shlex.quote(script_name)}"
    )
    base = build_remote_ssh_base(settings.profile(), ssh_path=ssh_path, batch_mode=False)
    # One try at each prompt, so a wrong code does not trigger repeated MFA pushes.
    return [base[0], "-o", "NumberOfPasswordPrompts=1", *base[1:], remote]


def parse_job_id(output: str) -> str | None:
    match = _JOB_ID.search(output)
    return match.group(1) if match else None


# --- Submitted jobs -------------------------------------------------------------------


@dataclass
class BunyaJob:
    """A recall job the app submitted, so its progress can be checked later."""

    job_id: str
    submitted: str
    username: str
    folder: str
    script_name: str
    local_list: str
    file_count: int

    @property
    def stem(self) -> str:
        return Path(self.script_name).stem

    @property
    def log_name(self) -> str:
        return f"{self.stem}_{self.job_id}.log"

    @property
    def done_name(self) -> str:
        return f"{self.stem}_{self.job_id}_done.txt"


def jobs_path() -> Path:
    return app_data_dir() / "bunya_jobs" / "jobs.json"


def load_jobs() -> list[BunyaJob]:
    try:
        raw = json.loads(jobs_path().read_text(encoding="utf-8"))
        return [BunyaJob(**item) for item in raw if isinstance(item, dict)]
    except (OSError, ValueError, TypeError):
        return []


def record_job(job: BunyaJob) -> None:
    jobs = [existing for existing in load_jobs() if existing.job_id != job.job_id]
    jobs.insert(0, job)
    path = jobs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps([asdict(item) for item in jobs], indent=2), encoding="utf-8")
    temporary.replace(path)


def build_job_status_command(settings: BunyaSettings, job: BunyaJob, ssh_path: str | None = None) -> list[str]:
    """Read-only: the job's Slurm state, its recalled/failed counts and the end of its log."""
    if not job.job_id.isdigit():
        raise ValueError(f"Unexpected job ID: {job.job_id}")
    folder = shlex.quote(job.folder.rstrip("/"))
    log = shlex.quote(job.log_name)
    done = shlex.quote(job.done_name)
    remote = (
        f"cd {folder} 2>/dev/null || {{ echo 'Job folder {job.folder} not found on Bunya.'; exit 3; }}; "
        f"echo '@@STATE'; squeue -h -j {job.job_id} -o '%T %M' 2>/dev/null; "
        f"sacct -X -n -j {job.job_id} -o State%20,Elapsed,ExitCode 2>/dev/null | head -1; "
        f"echo '@@COUNTS'; grep -c '^OK' {done} 2>/dev/null; grep -c '^FAILED' {done} 2>/dev/null; "
        f"echo '@@LOG'; tail -n 15 {log} 2>/dev/null"
    )
    base = build_remote_ssh_base(settings.profile(), ssh_path=ssh_path, batch_mode=False)
    return [base[0], "-o", "NumberOfPasswordPrompts=1", *base[1:], remote]


@dataclass
class BunyaJobStatus:
    state: str = ""
    recalled: int | None = None
    failed: int | None = None
    log_tail: str = ""


def parse_job_status(output: str) -> BunyaJobStatus:
    status = BunyaJobStatus()
    section = ""
    counts: list[int] = []
    log_lines: list[str] = []
    state_lines: list[str] = []
    for line in output.splitlines():
        if line.startswith("@@"):
            section = line[2:]
            continue
        if section == "STATE" and line.strip():
            state_lines.append(" ".join(line.split()))
        elif section == "COUNTS" and line.strip().isdigit():
            counts.append(int(line.strip()))
        elif section == "LOG":
            log_lines.append(line)
    status.state = state_lines[0] if state_lines else ""
    if len(counts) >= 2:
        status.recalled, status.failed = counts[0], counts[1]
    status.log_tail = "\n".join(log_lines).strip()
    return status

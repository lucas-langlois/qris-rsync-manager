from __future__ import annotations

import argparse
import getpass
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.logging_utils import new_log_file
from app.core.medici import build_recall_medici_files_command
from app.core.paths import detect_ssh
from app.core.profiles import fallback_hosts, load_profiles, profile_with_host
from app.core.remote_dirs import build_list_remote_entries_command, parse_remote_entries
from app.core.rsync_command import build_rsync_command
from app.core.rsync_runner import RsyncRunner
from app.core.ssh_test import run_ssh_test
from app.core.askpass import build_askpass_environment, scrub_askpass_environment


DEFAULT_REMOTE = (
    "/data/Q8940/Bowen_Megafauna/Bowen_Megafauna/BowenCoastline/"
    "20250427/20250427_BowenCoastline_M3E1_Survey1"
)
DEFAULT_LOCAL = r"E:\MegafaunaAI_webapp\test_data\drone_survey\images"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test a QRIScloud rsync download outside the GUI.")
    parser.add_argument("--profile", default="Q8940", help="Saved QRIS Rsync Manager profile name.")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, help="Remote QRIScloud folder to download.")
    parser.add_argument("--local", default=DEFAULT_LOCAL, help="Local destination folder.")
    parser.add_argument("--download", action="store_true", help="Run the real download. Default is dry-run.")
    parser.add_argument("--recursive", action="store_true", help="Use recursive rsync discovery instead of a flat visible file list.")
    parser.add_argument("--file-by-file", action="store_true", help="Download each visible file in a separate rsync call and continue after failures.")
    parser.add_argument("--chunk-size", type=int, default=5, help="Number of visible files per rsync call. Use 1 for file-by-file.")
    parser.add_argument("--idle-timeout", type=int, default=180, help="Stop each rsync call after this many seconds without output.")
    parser.add_argument("--recall-before-download", action="store_true", help="Run recall_medici on the remote path before rsync.")
    parser.add_argument("--recall-only", action="store_true", help="Run recall_medici and exit without downloading.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profiles = {profile.name: profile for profile in load_profiles()}
    if args.profile not in profiles:
        print(f"Profile not found: {args.profile}")
        print("Available profiles:", ", ".join(sorted(profiles)))
        return 2

    profile = profiles[args.profile]
    local = Path(args.local)
    local.mkdir(parents=True, exist_ok=True)
    ssh_path = detect_ssh()
    passphrase = ""
    if profile.ssh_key_path:
        passphrase = getpass.getpass("SSH key passphrase (leave blank if key is already loaded in ssh-agent): ")

    dry_run = not args.download
    print(f"Mode: {'download' if args.download else 'dry-run'}")
    print(f"Remote: {args.remote}")
    print(f"Local:  {local}")
    print(f"SSH:    {ssh_path}")
    print()

    for host in fallback_hosts(profile):
        attempt_profile = profile_with_host(profile, host)
        print(f"Checking {host}...")
        result = run_ssh_test(attempt_profile, ssh_path=ssh_path, passphrase=passphrase, timeout=30)
        if result.output:
            print(result.output, end="" if result.output.endswith("\n") else "\n")
        if result.returncode != 0:
            print(f"{host} unavailable: exit code {result.returncode}\n")
            continue

        print(f"Using {host}. Starting rsync...\n")
        files_from = None
        listed_files: list[str] = []
        listed_remote_paths: list[str] = []
        if not args.recursive:
            list_command = build_list_remote_entries_command(
                attempt_profile,
                args.remote,
                ssh_path=ssh_path,
                batch_mode=not bool(passphrase),
            )
            env = build_askpass_environment(passphrase, ssh_path=ssh_path)
            try:
                completed = subprocess.run(
                    list_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=90,
                    shell=False,
                    env=env,
                )
            finally:
                scrub_askpass_environment(env)
            if completed.returncode != 0:
                print(completed.stdout)
                print(f"Remote listing failed with exit code {completed.returncode}; falling back to recursive rsync.\n")
            else:
                entries = parse_remote_entries(completed.stdout)
                files = [entry.name for entry in entries if not entry.is_dir]
                listed_remote_paths = [entry.path for entry in entries if not entry.is_dir]
                dirs = [entry.name for entry in entries if entry.is_dir]
                if files and not dirs:
                    listed_files = files
                    files_from = new_log_file("interactive_download_visible_files").with_suffix(".txt")
                    files_from.write_text("\n".join(files) + "\n", encoding="utf-8", newline="\n")
                    print(f"Using flat file list with {len(files):,} files: {files_from}\n")
                elif dirs:
                    print(f"Remote folder contains {len(dirs):,} directories; falling back to recursive rsync.\n")

        if args.recall_before_download or args.recall_only:
            if not listed_remote_paths:
                print("No visible remote files were listed for recall.")
                return 1
            print(f"Running recall_medici for {len(listed_remote_paths):,} files under {args.remote}...")
            recall_command = build_recall_medici_files_command(
                attempt_profile,
                listed_remote_paths,
                ssh_path=ssh_path,
                batch_mode=not bool(passphrase),
            )
            recall_log = new_log_file("interactive_recall_medici_Q8940")
            recall_code = RsyncRunner().run(
                recall_command,
                recall_log,
                lambda text: print(text.replace("\r", "\n"), end=""),
                passphrase=passphrase,
                ssh_path=ssh_path,
                idle_timeout_seconds=None,
            )
            print(f"\nrecall_medici exited with code {recall_code}. Log: {recall_log}\n")
            if args.recall_only or recall_code != 0:
                return recall_code

        if listed_files and (args.file_by_file or args.chunk_size > 0):
            chunk_size = 1 if args.file_by_file else max(1, args.chunk_size)
            failures: list[list[str]] = []
            chunks = [listed_files[index : index + chunk_size] for index in range(0, len(listed_files), chunk_size)]
            for index, chunk in enumerate(chunks, start=1):
                chunk_file_list = new_log_file(f"interactive_download_chunk_{index:04d}").with_suffix(".txt")
                chunk_file_list.write_text("\n".join(chunk) + "\n", encoding="utf-8", newline="\n")
                command = build_rsync_command(
                    attempt_profile,
                    local,
                    remote_path=args.remote,
                    dry_run=dry_run,
                    ssh_path=ssh_path,
                    batch_mode=not bool(passphrase),
                    files_from=chunk_file_list,
                    direction="download",
                )
                first = chunk[0]
                last = chunk[-1]
                print(f"\n[{index:,}/{len(chunks):,}] {len(chunk):,} file(s): {first}" + (f" ... {last}" if last != first else ""))
                log_file = new_log_file(f"interactive_download_test_Q8940_chunk_{index:04d}")
                code = RsyncRunner().run(
                    command,
                    log_file,
                    lambda text: print(text.replace("\r", "\n"), end=""),
                    passphrase=passphrase,
                    ssh_path=ssh_path,
                    idle_timeout_seconds=args.idle_timeout,
                )
                if code != 0:
                    failures.append(chunk)
                    print(f"FAILED: chunk {index} exited with code {code}. Continuing.\n")
            if failures:
                print("\nFailed chunks/files:")
                for chunk in failures:
                    for filename in chunk:
                        print(f"  {filename}")
                return 1
            return 0

        command = build_rsync_command(
            attempt_profile,
            local,
            remote_path=args.remote,
            dry_run=dry_run,
            ssh_path=ssh_path,
            batch_mode=not bool(passphrase),
            files_from=files_from,
            direction="download",
        )
        print("Command:")
        print(" ".join(command))
        print()
        log_file = new_log_file("interactive_download_test_Q8940")
        print(f"Log: {log_file}\n")
        return RsyncRunner().run(
            command,
            log_file,
            lambda text: print(text.replace("\r", "\n"), end=""),
            passphrase=passphrase,
            ssh_path=ssh_path,
            idle_timeout_seconds=args.idle_timeout,
        )

    print("No QRIScloud SSH host was available.")
    return 124


if __name__ == "__main__":
    raise SystemExit(main())

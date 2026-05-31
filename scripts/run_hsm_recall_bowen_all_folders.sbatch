#!/bin/bash
# HSM recall job for all folders under Bowen_Megafauna.
# Uses recall_medici to restore cold-tier files from ZWS/tape back to GPFS cache.
#
# Usage on Bunya:
#   mkdir -p /scratch/user/lucaslanglois/Bowen_Megafauna_UAV/slurm_logs
#   cd /scratch/user/lucaslanglois/Bowen_Megafauna_UAV
#   sbatch run_hsm_recall_bowen_all_folders.sbatch
#
# Monitor:
#   squeue -u lucaslanglois
#   tail -f /scratch/user/lucaslanglois/Bowen_Megafauna_UAV/slurm_logs/hsm_recall_bowen_all-<jobid>.out

#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --job-name=hsm_recall_bowen_all
#SBATCH --time=14-00:00:00
#SBATCH --partition=general
#SBATCH --account=a_carter
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=lucas.langlois@jcu.edu.au
#SBATCH --output=/scratch/user/lucaslanglois/Bowen_Megafauna_UAV/slurm_logs/hsm_recall_bowen_all-%j.out
#SBATCH --error=/scratch/user/lucaslanglois/Bowen_Megafauna_UAV/slurm_logs/hsm_recall_bowen_all-%j.err

set -euo pipefail
shopt -s nullglob

SOURCE_ROOT="/QRISdata/Q8940/Bowen_Megafauna/Bowen_Megafauna"
WORKDIR="/scratch/user/lucaslanglois/Bowen_Megafauna_UAV"

mkdir -p "${WORKDIR}/slurm_logs"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "HSM recall job started"
log "Node        : $(hostname)"
log "Source root : ${SOURCE_ROOT}"
log "Workdir     : ${WORKDIR}"

RECALL_CMD="/usr/local/bin/recall_medici"
if [[ ! -x "${RECALL_CMD}" ]]; then
    log "ERROR: ${RECALL_CMD} not found. Check you are on a compute node, not a login node."
    exit 1
fi

# Trigger automount and verify the root can be read.
ls "${SOURCE_ROOT}/" >/dev/null

TOTAL_FILES=$(find "${SOURCE_ROOT}" -type f 2>/dev/null | wc -l)
TOTAL_DIRS=$(find "${SOURCE_ROOT}" -type d 2>/dev/null | wc -l)
log "Total dirs  : ${TOTAL_DIRS}"
log "Total files : ${TOTAL_FILES}"

log "Sampling cold-tier status (first 5 files):"
find "${SOURCE_ROOT}" -type f 2>/dev/null | sort | head -5 | while read -r file; do
    ls -salh "${file}" || true
done

log "Running recall_medici once per directory that contains files."
log "This can take many hours for cold/tape files. Check progress with squeue or the .out log."

DIR_LIST="${WORKDIR}/slurm_logs/hsm_recall_bowen_all-${SLURM_JOB_ID}-dirs.txt"
FAILED_LIST="${WORKDIR}/slurm_logs/hsm_recall_bowen_all-${SLURM_JOB_ID}-failed_dirs.txt"
: > "${FAILED_LIST}"

find "${SOURCE_ROOT}" -type f -printf '%h\n' 2>/dev/null | sort -u > "${DIR_LIST}"
DIR_COUNT=$(wc -l < "${DIR_LIST}" | tr -d ' ')
log "Directories with files: ${DIR_COUNT}"

i=0
while IFS= read -r dir; do
    i=$((i + 1))
    file_count=$(find "${dir}" -maxdepth 1 -type f 2>/dev/null | wc -l)
    log "[${i}/${DIR_COUNT}] recalling ${file_count} file(s) in: ${dir}"

    if "${RECALL_CMD}" "${dir}/"*; then
        log "[${i}/${DIR_COUNT}] done: ${dir}"
    else
        rc=$?
        log "[${i}/${DIR_COUNT}] FAILED with exit code ${rc}: ${dir}"
        printf '%s\n' "${dir}" >> "${FAILED_LIST}"
    fi
done < "${DIR_LIST}"

FAILED=$(wc -l < "${FAILED_LIST}" | tr -d ' ')

log "Recall job completed"
log "Directories failed: ${FAILED}"

if [[ "${FAILED}" -gt 0 ]]; then
    log "Failed directory list: ${FAILED_LIST}"
    exit 1
fi

log "All directory recalls completed. Files should now be in GPFS cache."

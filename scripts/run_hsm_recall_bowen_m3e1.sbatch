#!/bin/bash
# HSM recall job for Bowen Megafauna UAV M3E1 Survey1.
# Uses recall_medici to restore cold-tier files from ZWS/tape back to GPFS cache.
#
# Usage on Bunya:
#   mkdir -p /scratch/user/lucaslanglois/Bowen_Megafauna_UAV/slurm_logs
#   cd /scratch/user/lucaslanglois/Bowen_Megafauna_UAV
#   sbatch run_hsm_recall_bowen_m3e1.sbatch
#
# Monitor:
#   squeue -u lucaslanglois
#   tail -f /scratch/user/lucaslanglois/Bowen_Megafauna_UAV/slurm_logs/hsm_recall_bowen_m3e1-<jobid>.out
#
# Check if files are cold before submitting (size-on-disk = 0 means cold):
#   ls -salh /QRISdata/Q8940/Bowen_Megafauna/Bowen_Megafauna/BowenCoastline/20250427/20250427_BowenCoastline_M3E1_Survey1/*.JPG | head -5

#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --job-name=hsm_recall_bowen_m3e1
#SBATCH --time=14-00:00:00
#SBATCH --partition=general
#SBATCH --account=a_carter
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=lucas.langlois@jcu.edu.au
#SBATCH --output=/scratch/user/lucaslanglois/Bowen_Megafauna_UAV/slurm_logs/hsm_recall_bowen_m3e1-%j.out
#SBATCH --error=/scratch/user/lucaslanglois/Bowen_Megafauna_UAV/slurm_logs/hsm_recall_bowen_m3e1-%j.err

set -euo pipefail

SOURCE_DIR="/QRISdata/Q8940/Bowen_Megafauna/Bowen_Megafauna/BowenCoastline/20250427/20250427_BowenCoastline_M3E1_Survey1"
WORKDIR="/scratch/user/lucaslanglois/Bowen_Megafauna_UAV"

mkdir -p "${WORKDIR}/slurm_logs"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "HSM recall job started"
log "Node        : $(hostname)"
log "Source      : ${SOURCE_DIR}"
log "Workdir     : ${WORKDIR}"

# Verify recall_medici is available.
RECALL_CMD="/usr/local/bin/recall_medici"
if [[ ! -x "${RECALL_CMD}" ]]; then
    log "ERROR: ${RECALL_CMD} not found. Check you are on a compute node, not a login node."
    exit 1
fi

# Trigger automount and verify the folder can be read.
ls "${SOURCE_DIR}/" >/dev/null

# Count total image/csv files in this survey folder.
TOTAL=$(find "${SOURCE_DIR}" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.csv' \) \
    2>/dev/null | wc -l)
log "Total files : ${TOTAL}"

# Check cold-tier status (size-on-disk = 0 means cold/offline).
log "Sampling cold-tier status (first 5 files):"
ls -salh "${SOURCE_DIR}/" 2>/dev/null | grep -v '^total' | head -5 || true

log "Running recall_medici on ${SOURCE_DIR}/ ..."
log "This can take many hours for cold/tape files. Check progress with squeue or the .out log."

# recall_medici with wildcard, matching the previous RCC-supported method.
"${RECALL_CMD}" "${SOURCE_DIR}/"*

log "recall_medici completed. Files should now be in GPFS cache."
log "Verify with: ls -salh ${SOURCE_DIR}/*.JPG | head -5"
log "The size-on-disk should now match actual file size, not 0."

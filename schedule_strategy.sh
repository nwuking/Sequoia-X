#!/usr/bin/env bash
# Sequoia-X 定时任务管理脚本。
# ./schedule_strategy.sh install|list|remove|run <intraday|daily>

# 调度时间：
# 1. 盘前：09:40、10:30、11:20、13:30、14:30、14:50
# 2. 盘后：19:15 （周一至周五）同步完整日k并运行全部日线策略

# 安装定时任务 ./schedule_strategy.sh install
# 查看定时任务 ./schedule_strategy.sh list
# 移除定时任务 ./schedule_strategy.sh remove
# 手动执行策略 ./schedule_strategy.sh run <intraday|daily>

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${SEQUOIA_PYTHON:-${SCRIPT_DIR}/.venv/bin/python}"
MAIN_FILE="${SCRIPT_DIR}/main.py"
LOG_DIR="${SEQUOIA_LOG_DIR:-${SCRIPT_DIR}/logs}"
CRON_BEGIN="# BEGIN SEQUOIA_X_MANAGED_JOBS"
CRON_END="# END SEQUOIA_X_MANAGED_JOBS"

usage() {
    printf '%s\n' \
        "用法：$0 install|list|remove|run <intraday|daily>" \
        "SEQUOIA_PYTHON 可覆盖Python路径；SEQUOIA_LOG_DIR可覆盖日志目录。"
}

check_runtime() {
    if [[ ! -x "${PYTHON_BIN}" ]]; then
        printf '错误：Python不存在或不可执行：%s\n' "${PYTHON_BIN}" >&2
        exit 1
    fi
    if [[ ! -f "${MAIN_FILE}" ]]; then
        printf '错误：找不到主程序：%s\n' "${MAIN_FILE}" >&2
        exit 1
    fi
    mkdir -p "${LOG_DIR}"
}

run_task() {
    local task="${1:-}"
    check_runtime
    cd "${SCRIPT_DIR}"
    case "${task}" in
        intraday) exec "${PYTHON_BIN}" "${MAIN_FILE}" --intraday ;;
        daily) exec "${PYTHON_BIN}" "${MAIN_FILE}" ;;
        *) printf '错误：未知任务：%s\n' "${task}" >&2; usage; exit 2 ;;
    esac
}

cleanup_task_logs() {
    local task="$1"
    local files=()
    local path
    for path in "${LOG_DIR}/${task}-"*.log; do
        [[ -e "${path}" ]] || continue
        files+=("${path}")
    done
    while (( ${#files[@]} > 10 )); do
        rm -f -- "${files[0]}"
        files=("${files[@]:1}")
    done
}

run_scheduled_task() {
    local task="${1:-}"
    case "${task}" in
        intraday|daily) ;;
        *) printf '错误：未知定时任务：%s\n' "${task}" >&2; exit 2 ;;
    esac
    check_runtime
    local log_file="${LOG_DIR}/${task}-$(date +%F).log"
    touch "${log_file}"
    cleanup_task_logs "${task}"
    cd "${SCRIPT_DIR}"
    case "${task}" in
        intraday) exec "${PYTHON_BIN}" "${MAIN_FILE}" --intraday >> "${log_file}" 2>&1 ;;
        daily) exec "${PYTHON_BIN}" "${MAIN_FILE}" >> "${log_file}" 2>&1 ;;
    esac
}

managed_cron_block() {
    printf '%s\n' \
        "${CRON_BEGIN}" \
        "CRON_TZ=Asia/Shanghai" \
        "40 9 * * 1-5 cd \"${SCRIPT_DIR}\" && \"${SCRIPT_DIR}/schedule_strategy.sh\" scheduled intraday" \
        "30 10 * * 1-5 cd \"${SCRIPT_DIR}\" && \"${SCRIPT_DIR}/schedule_strategy.sh\" scheduled intraday" \
        "20 11 * * 1-5 cd \"${SCRIPT_DIR}\" && \"${SCRIPT_DIR}/schedule_strategy.sh\" scheduled intraday" \
        "30 13 * * 1-5 cd \"${SCRIPT_DIR}\" && \"${SCRIPT_DIR}/schedule_strategy.sh\" scheduled intraday" \
        "30 14 * * 1-5 cd \"${SCRIPT_DIR}\" && \"${SCRIPT_DIR}/schedule_strategy.sh\" scheduled intraday" \
        "50 14 * * 1-5 cd \"${SCRIPT_DIR}\" && \"${SCRIPT_DIR}/schedule_strategy.sh\" scheduled intraday" \
        "15 19 * * 1-5 cd \"${SCRIPT_DIR}\" && \"${SCRIPT_DIR}/schedule_strategy.sh\" scheduled daily" \
        "${CRON_END}"
}

current_crontab() {
    crontab -l 2>/dev/null || true
}

without_managed_block() {
    current_crontab | awk -v begin="${CRON_BEGIN}" -v end="${CRON_END}" '
        $0 == begin { skipping = 1; next }
        $0 == end { skipping = 0; next }
        !skipping { print }
    '
}

list_jobs() {
    current_crontab | awk -v begin="${CRON_BEGIN}" -v end="${CRON_END}" '
        $0 == begin { showing = 1 }
        showing { print }
        $0 == end { showing = 0; found = 1 }
        END { if (!found) print "尚未安装 Sequoia-X 定时任务。" }
    '
}

install_jobs() {
    check_runtime
    local temporary
    temporary="$(mktemp "${TMPDIR:-/tmp}/sequoia-x-cron.XXXXXX")"
    trap "rm -f -- '${temporary}'" EXIT
    { without_managed_block; managed_cron_block; } > "${temporary}"
    crontab "${temporary}"
    printf 'Sequoia-X 定时任务已安装。\n'
    list_jobs
}

remove_jobs() {
    local temporary
    temporary="$(mktemp "${TMPDIR:-/tmp}/sequoia-x-cron.XXXXXX")"
    trap "rm -f -- '${temporary}'" EXIT
    without_managed_block > "${temporary}"
    crontab "${temporary}"
    printf 'Sequoia-X 定时任务已移除，其他crontab条目保持不变。\n'
}

case "${1:-}" in
    install) install_jobs ;;
    list) list_jobs ;;
    remove) remove_jobs ;;
    run) run_task "${2:-}" ;;
    scheduled) run_scheduled_task "${2:-}" ;;
    *) usage; exit 2 ;;
esac

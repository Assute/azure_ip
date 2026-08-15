#!/usr/bin/env bash

set -Eeuo pipefail

readonly REPOSITORY_RAW_URL="${AZURE_IP_REPOSITORY_RAW_URL:-https://raw.githubusercontent.com/Assute/azure_ip/main}"
readonly INSTALL_DIR="${AZURE_IP_INSTALL_DIR:-/opt/azure_ip}"
readonly SERVICE_NAME="azure-ip-monitor"
readonly SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
readonly PYTHON_FILE="${INSTALL_DIR}/azure_ip_monitor.py"
readonly INSTALLER_FILE="${INSTALL_DIR}/azure_ip.sh"
readonly CONFIG_FILE="${INSTALL_DIR}/azure_ip_monitor.json"

action="install"

show_help() {
    cat <<'EOF'
Azure 公网 IP 监控器一键安装脚本

用法：
  sudo bash azure_ip.sh                 安装或更新，并启动后台服务
  sudo bash azure_ip.sh --reconfigure   重新填写 Azure 配置并重启服务
  sudo bash azure_ip.sh --delete-config 删除配置并停止后台服务
  sudo bash azure_ip.sh --cf-ddns       配置 Cloudflare DDNS 并重启服务
  bash azure_ip.sh --status             查看服务状态
  bash azure_ip.sh --logs               持续查看服务日志
  bash azure_ip.sh --help               显示帮助

也可以直接在线安装：
  curl -fsSL https://raw.githubusercontent.com/Assute/azure_ip/main/azure_ip.sh | sudo bash
EOF
}

for argument in "$@"; do
    case "${argument}" in
        --reconfigure)
            action="reconfigure"
            ;;
        --delete-config)
            action="delete-config"
            ;;
        --cf-ddns)
            action="cf-ddns"
            ;;
        --status)
            action="status"
            ;;
        --logs)
            action="logs"
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "错误：未知参数 ${argument}" >&2
            show_help >&2
            exit 2
            ;;
    esac
done

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "错误：系统缺少 $1 命令" >&2
        exit 1
    fi
}

choose_existing_config_action() {
    local choice

    while true; do
        cat >/dev/tty <<EOF

检测到已有配置：${CONFIG_FILE}
请选择操作：
  1. 更新脚本并重启服务（默认）
  2. 重新配置
  3. 删除配置并停止后台服务
  4. Cloudflare DDNS配置
EOF
        read -r -p "请输入选项 [1/2/3/4]：" choice </dev/tty || choice="1"
        case "${choice}" in
            1|"")
                action="install"
                return
                ;;
            2)
                action="reconfigure"
                return
                ;;
            3)
                action="delete-config"
                return
                ;;
            4)
                action="cf-ddns"
                return
                ;;
            *)
                echo "无效选项，请输入 1、2、3 或 4。" >/dev/tty
                ;;
        esac
    done
}

case "${action}" in
    status)
        require_command systemctl
        exec systemctl --no-pager --full status "${SERVICE_NAME}.service"
        ;;
    logs)
        require_command journalctl
        exec journalctl -u "${SERVICE_NAME}.service" -f
        ;;
esac

if [[ "${EUID}" -ne 0 ]]; then
    echo "错误：安装需要 root 权限，请使用 sudo 运行。" >&2
    exit 1
fi

require_command systemctl

if [[ "${action}" == "install" && -f "${CONFIG_FILE}" ]]; then
    if [[ -r /dev/tty ]]; then
        choose_existing_config_action
    else
        echo "检测到现有配置，非交互运行将保留配置并继续更新。"
    fi
fi

if [[ "${action}" == "delete-config" ]]; then
    systemctl disable --now "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
    if [[ -f "${CONFIG_FILE}" ]]; then
        if ! command -v shred >/dev/null 2>&1 || ! shred -u "${CONFIG_FILE}"; then
            rm -f "${CONFIG_FILE}"
        fi
        echo "配置已删除：${CONFIG_FILE}"
    else
        echo "未找到配置文件：${CONFIG_FILE}"
    fi
    echo "后台服务已停止并禁用。再次运行安装脚本可重新配置。"
    exit 0
fi

if [[ "${action}" == "cf-ddns" && ! -f "${CONFIG_FILE}" ]]; then
    echo "错误：请先完成 Azure 配置，再配置 Cloudflare DDNS。" >&2
    exit 1
fi

require_command python3
require_command bash
require_command timeout

if command -v curl >/dev/null 2>&1; then
    downloader="curl"
elif command -v wget >/dev/null 2>&1; then
    downloader="wget"
else
    echo "错误：系统需要 curl 或 wget 才能下载安装文件" >&2
    exit 1
fi

download_file() {
    local source_url="$1"
    local destination="$2"
    local mode="$3"
    local temporary

    temporary="$(mktemp "${INSTALL_DIR}/.download.XXXXXX")"
    if [[ "${downloader}" == "curl" ]]; then
        if ! curl -fL --retry 3 --connect-timeout 15 -o "${temporary}" "${source_url}"; then
            rm -f "${temporary}"
            return 1
        fi
    else
        if ! wget -q --timeout=15 --tries=3 -O "${temporary}" "${source_url}"; then
            rm -f "${temporary}"
            return 1
        fi
    fi
    install -m "${mode}" "${temporary}" "${destination}"
    rm -f "${temporary}"
}

install -d -m 0755 "${INSTALL_DIR}"

echo "正在下载最新版程序到 ${INSTALL_DIR} ..."
download_file "${REPOSITORY_RAW_URL}/azure_ip_monitor.py" "${PYTHON_FILE}" 0755
download_file "${REPOSITORY_RAW_URL}/azure_ip.sh" "${INSTALLER_FILE}" 0755

if [[ "${action}" == "cf-ddns" ]]; then
    if [[ ! -r /dev/tty ]]; then
        echo "错误：Cloudflare DDNS 配置需要交互终端。" >&2
        exit 1
    fi
    service_was_active="false"
    if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
        service_was_active="true"
        systemctl stop "${SERVICE_NAME}.service"
    fi
    echo "开始配置 Cloudflare DDNS ..."
    if ! python3 "${PYTHON_FILE}" --config "${CONFIG_FILE}" --configure-cloudflare </dev/tty; then
        if [[ "${service_was_active}" == "true" ]]; then
            systemctl start "${SERVICE_NAME}.service" || true
        fi
        exit 1
    fi
    chmod 0600 "${CONFIG_FILE}"
elif [[ "${action}" == "reconfigure" || ! -f "${CONFIG_FILE}" ]]; then
    if [[ ! -r /dev/tty ]]; then
        echo "错误：首次配置需要交互终端，请先下载脚本后使用 sudo bash azure_ip.sh 运行。" >&2
        exit 1
    fi
    service_was_active="false"
    if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
        service_was_active="true"
        systemctl stop "${SERVICE_NAME}.service"
    fi
    echo "开始配置 Azure 应用凭据和目标虚拟机 ..."
    if ! python3 "${PYTHON_FILE}" --config "${CONFIG_FILE}" --configure-only </dev/tty; then
        if [[ "${service_was_active}" == "true" ]]; then
            systemctl start "${SERVICE_NAME}.service" || true
        fi
        exit 1
    fi
    chmod 0600 "${CONFIG_FILE}"
else
    echo "检测到现有配置，继续使用 ${CONFIG_FILE}"
fi

python_bin="$(command -v python3)"
service_temporary="$(mktemp "${INSTALL_DIR}/.service.XXXXXX")"
cat >"${service_temporary}" <<EOF
[Unit]
Description=Azure Public IP Monitor and Auto Rotator
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${python_bin} ${PYTHON_FILE} --config ${CONFIG_FILE}
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
install -m 0644 "${service_temporary}" "${SERVICE_FILE}"
rm -f "${service_temporary}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service" >/dev/null
systemctl restart "${SERVICE_NAME}.service"
sleep 2

if ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    echo "错误：后台服务启动失败，最近日志如下：" >&2
    journalctl -u "${SERVICE_NAME}.service" -n 30 --no-pager >&2 || true
    exit 1
fi

echo "安装完成，${SERVICE_NAME}.service 已在后台运行。"
echo "安装目录：${INSTALL_DIR}"
echo "查看状态：${INSTALLER_FILE} --status"
echo "查看日志：${INSTALLER_FILE} --logs"
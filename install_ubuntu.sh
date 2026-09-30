#!/usr/bin/env bash
# ============================================================
#  cangyao 项目 - Ubuntu 环境一键安装脚本
# ------------------------------------------------------------
#  适用系统 : Ubuntu 22.04 / 24.04 (x86_64)，Python >= 3.9
#  功能     : 安装系统依赖 -> 创建虚拟环境 -> 安装 Python
#             依赖 -> 安装 Playwright 浏览器 -> 适配 Linux 配置
#
#  用法     :
#    sudo bash install_ubuntu.sh                     # 完整安装(含 Playwright)
#    sudo bash install_ubuntu.sh --no-playwright     # 跳过 Playwright 浏览器
#    sudo bash install_ubuntu.sh --with-nginx        # (可选) 装 Nginx 反代 11451/1452
#    sudo bash install_ubuntu.sh --with-systemd      # 注册 systemd 开机自启
#    sudo bash install_ubuntu.sh --mirror tsinghua   # 使用清华 PyPI 镜像
#                    (mirror 可选: tsinghua / aliyun)
#
#  端口     : 面板 28788(HTTPS 自签证书, 端口直通) + 图床 11451(独立静态服务)，
#             两者都不依赖 Nginx；放了这两个端口即可公网访问
#  说明     : 脚本可重复执行（幂等），安装日志位于 /tmp/cangyao_*.log
# ============================================================

set -euo pipefail

# ---------------- 颜色输出 ----------------
C_GREEN=$'\033[0;32m'
C_YELLOW=$'\033[0;33m'
C_RED=$'\033[0;31m'
C_CYAN=$'\033[0;36m'
C_NC=$'\033[0m'

log()  { echo -e "[$(date '+%H:%M:%S')] $*"; }
info() { log "${C_CYAN}[INFO]${C_NC} $*"; }
ok()   { log "${C_GREEN}[ OK ]${C_NC} $*"; }
warn() { log "${C_YELLOW}[WARN]${C_NC} $*"; }
err()  { log "${C_RED}[ERR ]${C_NC} $*"; }

# ---------------- 默认参数 ----------------
WITH_PLAYWRIGHT=1
WITH_NGINX=0
WITH_SYSTEMD=0
MIRROR=""

usage() {
    cat <<'EOF'
用法: sudo bash install_ubuntu.sh [选项]

选项:
  --no-playwright      跳过 Playwright 浏览器安装(maimai2 插件将不可用)
  --with-nginx         (可选)安装 Nginx 并适配项目 nginx.conf(11451/1452)。
                       默认用不到：面板 28788 与图床 11451 都是端口直通
  --with-systemd       注册 systemd 服务 cangyao，支持开机自启
  --mirror <name>      PyPI 镜像: tsinghua | aliyun (默认官方源)
  -h, --help           显示本帮助
EOF
}

# ---------------- 解析参数 ----------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-playwright) WITH_PLAYWRIGHT=0 ;;
        --with-nginx)    WITH_NGINX=1 ;;
        --with-systemd)  WITH_SYSTEMD=1 ;;
        --mirror)        shift; MIRROR="${1:-}" ;;
        -h|--help)       usage; exit 0 ;;
        *)               warn "未知参数: $1"; usage; exit 1 ;;
    esac
    shift
done

case "$MIRROR" in
    tsinghua|tuna) INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple" ;;
    aliyun|ali)    INDEX_URL="https://mirrors.aliyun.com/pypi/simple" ;;
    "")            INDEX_URL="${PIP_INDEX_URL:-}" ;;
    *)             warn "未知镜像: $MIRROR，使用默认源"; INDEX_URL="${PIP_INDEX_URL:-}" ;;
esac

# ---------------- 路径变量 ----------------
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PY="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"
APT_LOG="/tmp/cangyao_apt.log"
PIP_LOG="/tmp/cangyao_pip.log"
REQ_LINUX="/tmp/cangyao_requirements_linux.txt"

# ---------------- 0. 环境检查 ----------------
check_env() {
    info "项目目录: $PROJECT_DIR"

    # root / sudo 检查
    SUDO=""
    if [[ $EUID -ne 0 ]]; then
        if command -v sudo &>/dev/null; then
            SUDO="sudo"
            warn "未以 root 运行，系统级操作将使用 sudo"
        else
            err "需要 root 权限，请使用: sudo bash $0"
            exit 1
        fi
    fi

    # Python 版本检查
    if ! command -v python3 &>/dev/null; then
        err "未检测到 python3，请先安装: sudo apt install python3"
        exit 1
    fi
    local major minor
    major=$("python3" -c 'import sys; print(sys.version_info[0])')
    minor=$("python3" -c 'import sys; print(sys.version_info[1])')
    if (( major < 3 || (major == 3 && minor < 9) )); then
        err "系统 Python 版本过低 (${major}.${minor})，需要 >= 3.9"
        err "建议使用 Ubuntu 22.04 (Python 3.10) 或 24.04 (Python 3.12)"
        exit 1
    fi
    info "检测到系统 Python: ${major}.${minor}"

    # requirements.txt 检查
    if [[ ! -f "$PROJECT_DIR/requirements.txt" ]]; then
        err "未找到 requirements.txt，请确认脚本位于项目根目录"
        exit 1
    fi
}

# ---------------- 1. 安装系统依赖 ----------------
install_system_packages() {
    info "更新 apt 软件源 ..."
    $SUDO apt-get update -qq >/dev/null 2>&1 || warn "apt update 失败，继续尝试安装"

    local pkgs=(
        python3 python3-venv python3-pip python3-dev
        build-essential pkg-config git curl wget unzip
        # Pillow 图像处理
        libjpeg-dev libpng-dev libfreetype6-dev zlib1g-dev
        # lxml(部分插件间接依赖)
        libxml2-dev libxslt1-dev
        # cryptography / 编译扩展
        libffi-dev libssl-dev
        # 音视频处理(部分插件依赖)
        ffmpeg
        # numpy/scipy 预编译需要
        libatlas-base-dev
    )

    info "安装系统依赖包 (${#pkgs[@]} 个) ..."
    if ! $SUDO apt-get install -y --no-install-recommends "${pkgs[@]}" >"$APT_LOG" 2>&1; then
        err "系统依赖安装失败，请查看: $APT_LOG (尾部日志如下)"
        tail -n 20 "$APT_LOG" || true
        exit 1
    fi
    ok "系统依赖安装完成"
}

# ---------------- 2. 创建虚拟环境 ----------------
setup_venv() {
    if [[ -x "$PY" ]]; then
        info "虚拟环境已存在，跳过创建: $VENV_DIR"
    else
        info "创建 Python 虚拟环境: $VENV_DIR"
        python3 -m venv "$VENV_DIR"
    fi
    "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || true
}

# ---------------- pip 安装辅助(带镜像/超时) ----------------
pip_install() {
    local args=(--timeout 120 --retries 5 --disable-pip-version-check)
    if [[ -n "$INDEX_URL" ]]; then
        "$PY" -m pip install "${args[@]}" -i "$INDEX_URL" "$@"
    else
        "$PY" -m pip install "${args[@]}" "$@"
    fi
}

# ---------------- 3. 安装 Python 依赖 ----------------
install_python_deps() {
    info "升级 pip / setuptools / wheel ..."
    pip_install --upgrade pip setuptools wheel >"$PIP_LOG" 2>&1 || {
        err "pip 升级失败，请检查网络或镜像配置"; tail -n 15 "$PIP_LOG"; exit 1
    }

    # 优先使用分析生成的精简清单 requirements-linux.txt
    # (基于嵌入式 python/ 实际环境，版本锁定为运行版本)
    local req_src="$PROJECT_DIR/requirements-linux.txt"
    if [[ -f "$req_src" ]]; then
        info "使用精简依赖清单: requirements-linux.txt"
        cp "$req_src" "$REQ_LINUX"
    else
        # 回退: 过滤 requirements.txt 中的 Windows 专属 / 无需安装的包:
        #   windows-curses, win32_setctime, pywin32-ctypes, pefile, pyinstaller
        #   asyncio(标准库自带，PyPI 版本与标准库冲突)
        info "未找到 requirements-linux.txt，回退到过滤 requirements.txt"
        local exclude='^(windows-curses|win32_setctime|pywin32-ctypes|pefile|pyinstaller|asyncio)([=!<>~ ]|$)'
        grep -vE "$exclude" "$PROJECT_DIR/requirements.txt" > "$REQ_LINUX" || true
    fi

    local total filtered
    total=$(wc -l < "$REQ_LINUX")
    filtered=$total
    info "安装 Python 依赖 ($filtered 个) ..."

    if ! pip_install -r "$REQ_LINUX" >>"$PIP_LOG" 2>&1; then
        err "Python 依赖安装失败，请查看: $PIP_LOG (尾部日志如下)"
        tail -n 30 "$PIP_LOG"
        exit 1
    fi
    ok "Python 依赖安装完成"
}

# ---------------- 4. 安装 Playwright 浏览器 ----------------
install_playwright() {
    if [[ "$WITH_PLAYWRIGHT" != "1" ]]; then
        warn "已跳过 Playwright 浏览器安装 (maimai2 插件将不可用)"
        return
    fi
    info "安装 Playwright 系统依赖 (chromium) ..."
    $SUDO "$PY" -m playwright install-deps chromium >>"$PIP_LOG" 2>&1 || warn "playwright install-deps 失败，可稍后手动执行"
    info "下载 Playwright chromium 浏览器 (约 150MB) ..."
    "$PY" -m playwright install chromium >>"$PIP_LOG" 2>&1 || warn "chromium 下载失败，可稍后执行: $PY -m playwright install chromium"
    ok "Playwright 浏览器安装完成"
}

# ---------------- 5. 适配 Linux 配置 ----------------
fix_config() {
    local cfg="$PROJECT_DIR/src/plugins/config.yml"
    if [[ -f "$cfg" ]] && grep -q '^monitor_disk:.*C:\\' "$cfg"; then
        cp "$cfg" "$cfg.bak"
        sed -i 's|^monitor_disk:.*|monitor_disk: "/"|' "$cfg"
        ok "已修正 $cfg: monitor_disk: C:\\ -> / (原文件已备份为 config.yml.bak)"
    fi

    # 确保运行时目录存在
    mkdir -p "$PROJECT_DIR/logs"
    info "运行时目录检查完成"
}

# ---------------- 6. (可选) Nginx ----------------
# 注意: 面板(28788) 与图床(11451) 都是「端口直通」的独立服务，默认**不需要 Nginx**。
# 这个函数只在显式加 --with-nginx 时执行，适合「已有域名/证书，想把 11451 挂到 80/443」的场景。
setup_nginx() {
    info "安装 Nginx ..."
    $SUDO apt-get install -y --no-install-recommends nginx >>"$APT_LOG" 2>&1 || {
        err "Nginx 安装失败"; return
    }

    local src="$PROJECT_DIR/nginx.conf"
    if [[ ! -f "$src" ]]; then
        warn "未找到项目 nginx.conf，跳过站点配置"
        return
    fi
    local out="/etc/nginx/sites-available/cangyao.conf"
    info "由项目 nginx.conf 提取 server 块生成 Ubuntu 站点配置 ..."

    # 只抽取 server{} 块。
    # 项目那份 nginx.conf 是 Windows 用的完整配置(含 worker_processes / events / http，
    # 还有 Windows 专属的 "use select")；而 Ubuntu 的 /etc/nginx/nginx.conf 已经在
    # http 块里 include 了 sites-enabled/*，若把 worker_processes / events / http 再写进
    # sites-enabled，nginx 会报 "directive is not allowed here"，nginx -t 直接失败。
    {
        echo "# 由 install_ubuntu.sh 从项目 nginx.conf 提取 server 块生成，请勿手工修改"
        awk '
            BEGIN { inserver=0; depth=0 }
            {
                if (!inserver && $0 ~ /^[[:space:]]*server[[:space:]]+\{/) {
                    inserver=1; depth=0
                }
                if (inserver) {
                    print
                    n=0
                    for (i=1; i<=length($0); i++) {
                        c=substr($0,i,1)
                        if (c=="{") n++
                        else if (c=="}") n--
                    }
                    depth+=n
                    if (depth<=0) inserver=0
                }
            }
        ' "$src" | sed 's|root html;|root /var/www/html;|'
    } | $SUDO tee "$out" >/dev/null

    # ftv 图床目录：bot 会把生成好的档案/统计图写到这里（见 util/image_host.py）。
    # 用了 nginx 就设 IMAGE_HOST_SERVE=false，让机器人只写图不起 11451 服务。
    $SUDO mkdir -p /var/www/html/ftv
    $SUDO ln -sf "$out" /etc/nginx/sites-enabled/cangyao.conf

    if $SUDO nginx -t >/dev/null 2>&1; then
        $SUDO systemctl enable nginx >/dev/null 2>&1
        $SUDO systemctl restart nginx
        ok "Nginx 已配置并启动 (监听 11451/1452)，静态文件请放置于 /var/www/html"
        info "既然用了 Nginx，建议在 .env 里设 IMAGE_HOST_SERVE=false（图床改由 Nginx 提供）"
        info "guanli 管理面板: https://<服务器地址>:28788/  （端口直通 + 自签证书；需 GUANLI_HOST=0.0.0.0 并放行 28788）"
        info "外网若打不开，检查云安全组/防火墙: $SUDO ufw allow 11451/tcp"
    else
        warn "Nginx 配置校验未通过，请检查: $out"
        $SUDO nginx -t || true
    fi
}

# ---------------- 7. (可选) systemd 服务 ----------------
setup_systemd() {
    local svc="/etc/systemd/system/cangyao.service"
    info "注册 systemd 服务: cangyao"
    $SUDO tee "$svc" >/dev/null <<EOF
[Unit]
Description=Cangyao QQ Bot (NoneBot2)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_DIR/bin/python bot.py
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONUTF8=1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable cangyao >/dev/null 2>&1
    ok "systemd 服务已注册，可用: sudo systemctl start cangyao"
}

# ---------------- 8. 完成提示 ----------------
final_summary() {
    chmod +x "$PROJECT_DIR/start.sh" 2>/dev/null || true
    cat <<EOF

${C_GREEN}==================== 安装完成 ====================${C_NC}

  1. 启动机器人:
       cd $PROJECT_DIR && ./start.sh
     (或使用 systemd: sudo systemctl start cangyao)

  2. 常用命令:
       查看日志: tail -f logs/bot.log
       更新依赖: $PIP install -r $REQ_LINUX

  3. 对外端口(端口直通, 不依赖 Nginx):
       - 管理面板 28788 : https://<公网IP>:28788/  (自带自签证书, 浏览器提示不受信任属正常)
       - 图床静态 11451 : http://<公网IP>:11451/ftv/<文件名>
     放行示例: sudo ufw allow 28788/tcp ; sudo ufw allow 11451/tcp
     (云服务器还要在安全组里放行这两个端口)

  4. 注意事项:
       - 机器人需要配合 OneBot V11 协议端(如 NapCat/go-cqhttp)使用
       - 若后续需要 Playwright: $PY -m playwright install chromium
       - 若需中文字体渲染: sudo apt install fonts-noto-cjk
       - 默认不需要 Nginx；只有想把 11451 挂到 80/443 时才用 --with-nginx

EOF
}

# ---------------- 主流程 ----------------
main() {
    cat <<EOF
${C_CYAN}==================================================${C_NC}
${C_CYAN}  cangyao 项目 - Ubuntu 环境一键安装${C_NC}
${C_CYAN}  参数: Playwright=$WITH_PLAYWRIGHT Nginx=$WITH_NGINX Systemd=$WITH_SYSTEMD 镜像=${INDEX_URL:-官方}${C_NC}
${C_CYAN}==================================================${C_NC}
EOF

    check_env
    install_system_packages
    setup_venv
    install_python_deps
    install_playwright
    fix_config
    [[ "$WITH_NGINX" == "1" ]] && setup_nginx
    [[ "$WITH_SYSTEMD" == "1" ]] && setup_systemd
    final_summary
}

main "$@"

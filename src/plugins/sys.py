import os
import socket
import platform
import subprocess
import psutil
from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageSegment, Message
from nonebot.plugin import PluginMetadata

# -------------------------- 平台判定 --------------------------
IS_WINDOWS = os.name == "nt"
IS_LINUX = platform.system().lower() == "linux"
PLATFORM_NAME = "Windows" if IS_WINDOWS else ("Ubuntu/Linux" if IS_LINUX else platform.system())

# -------------------------- 配置（懒加载） --------------------------
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yml")


def _default_disk() -> str:
    """按平台给出默认监控盘符/挂载点"""
    return "C:\\" if IS_WINDOWS else "/"


DEFAULT_CONFIG = {
    "monitor_disk": _default_disk(),
    "resource_decimal": 2,
    "show_cpu_core": True
}

_config_cache = None


def get_config():
    """懒加载配置，只在首次使用时读取YAML"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    import yaml  # 延迟导入 yaml，避免模块加载时拖慢速度
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                _config_cache = {**DEFAULT_CONFIG, **cfg}
        else:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(DEFAULT_CONFIG, f, allow_unicode=True)
            _config_cache = DEFAULT_CONFIG
    except Exception as e:
        print(f"读取配置文件失败，使用默认配置: {e}")
        _config_cache = DEFAULT_CONFIG

    # 配置里的盘符/挂载点在当前系统不存在时，自动回退到平台默认值
    disk = _config_cache.get("monitor_disk") or _default_disk()
    if not os.path.exists(disk):
        _config_cache["monitor_disk"] = _default_disk()

    return _config_cache


def _decimal() -> int:
    try:
        return int(get_config().get("resource_decimal", 2))
    except Exception:
        return 2


# -------------------------- 缓存静态信息（避免重复获取） --------------------------
_cache = {
    "cpu_model": None,
    "cpu_cores": None,
    "mem_type": None,
    "gpu_info": None
}

# -------------------------- 插件元信息 --------------------------
__plugin_meta__ = PluginMetadata(
    name="系统资源监控",
    description="查看 CPU、内存、磁盘、GPU 占用率及硬件型号、网络信息（兼容 Windows / Ubuntu）",
    usage="发送「系统监控」「系统检查」「sys」或「监控信息」即可查看",
    type="application"
)

# -------------------------- 注册指令 --------------------------
system_all_cmd = on_command(
    cmd="系统监控",
    aliases={"系统检查", "系统状态", "sys", "sysinfo", "监控信息"},
    priority=5,
    block=True,
)


# -------------------------- 通用工具函数 --------------------------
def _run(cmd, encoding="utf-8", timeout=5):
    """跨平台执行外部命令，失败返回空字符串"""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding=encoding,
            errors="ignore",
            timeout=timeout,
            shell=isinstance(cmd, str),
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def _fmt_bytes(num_bytes: int) -> str:
    """字节数格式化为可读字符串"""
    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.{_decimal()}f} GB"
    mb = num_bytes / (1024 ** 2)
    return f"{mb:.{_decimal()}f} MB"


def _is_inner_ip(ip: str) -> bool:
    """判断是否为网卡内网（私有）IP"""
    try:
        import ipaddress
        addr = ipaddress.ip_address(ip)
        return addr.version == 4 and addr.is_private and not addr.is_loopback and not addr.is_link_local
    except Exception:
        return False


# -------------------------- 信息获取工具函数 --------------------------
_CPUINFO_PATH = "/proc/cpuinfo"

# 注意：x86 的 /proc/cpuinfo 里 "model : 85"（CPU 代号数字）排在真正的
# "model name : Intel(R) Xeon..." 之前，必须按优先级取，否则型号会显示成 "85"
_CPUINFO_KEY_ORDER = ("model name", "model", "hardware", "cpu model", "cpu")


def _read_cpuinfo(path: str = _CPUINFO_PATH) -> dict:
    """解析 /proc/cpuinfo，返回 {小写字段名: 首个取值}"""
    info = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()
                if key and value and key not in info:
                    info[key] = value
    except Exception:
        pass
    return info


def _get_cpu_model() -> str:
    """获取 CPU 型号（Windows 用注册表，Ubuntu 读 /proc/cpuinfo）"""
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            ) as key:
                return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except Exception:
            pass
        return platform.processor() or "未知 CPU 型号"

    # Linux / Ubuntu：按优先级挑选，并跳过 "model : 85" 这类纯代号/纯数字值
    info = _read_cpuinfo()
    for key in _CPUINFO_KEY_ORDER:
        value = info.get(key, "").strip()
        if not value:
            continue
        if value.isdigit() or len(value) <= 3:
            continue
        return value

    # ARM 开发板（树莓派等）可从设备树读取型号
    for dt_path in ("/sys/firmware/devicetree/base/model", "/proc/device-tree/model"):
        try:
            with open(dt_path, "r", encoding="utf-8", errors="ignore") as f:
                value = f.read().strip().strip("\x00").strip()
                if value:
                    return value
        except Exception:
            continue

    # 裁剪过的内核可能没有 model name，用 vendor + family + model 兜底
    vendor = info.get("vendor_id", "").strip()
    family = info.get("cpu family", "").strip()
    model = info.get("model", "").strip()
    parts = [vendor, f"family {family}" if family else "", f"model {model}" if model else ""]
    parts = [p for p in parts if p]
    if len(parts) > 1:
        return " ".join(parts)

    return platform.processor() or "未知 CPU 型号"


def _load_hint() -> str:
    """占用率显示为 0 时，补充 1 分钟负载，避免看起来像读取失败"""
    if IS_WINDOWS:
        return ""
    try:
        load1 = os.getloadavg()[0]
        return f"（1 分钟负载 {load1:.2f}）"
    except Exception:
        return ""


def get_cpu_info() -> str:
    """获取 CPU 型号、核心数、总占用率"""
    global _cache
    if _cache["cpu_model"] is None:
        _cache["cpu_model"] = _get_cpu_model() or "未知 CPU 型号"
    if _cache["cpu_cores"] is None:
        try:
            logical = psutil.cpu_count(logical=True)
            physical = psutil.cpu_count(logical=False)
        except Exception:
            logical, physical = None, None
        _cache["cpu_cores"] = (physical, logical)

    # 采样 0.5 秒，保证首次调用也能得到有效数值
    cpu_total = psutil.cpu_percent(interval=0.5)

    physical, logical = _cache["cpu_cores"]
    lines = [f"CPU: {_cache['cpu_model']}"]

    if get_config().get("show_cpu_core", True):
        if logical:
            core_text = f"{logical} 核"
            if physical and physical != logical:
                core_text += f"（物理 {physical} 核 / {logical} 线程）"
            lines.append(f"核心数：{core_text}")

    decimals = _decimal()
    eps = 10 ** (-decimals)
    if 0 < cpu_total < eps:
        cpu_text = f"<{eps:.{decimals}f}%"
    else:
        cpu_text = f"{cpu_total:.{decimals}f}%"
    if cpu_total < eps:
        cpu_text += _load_hint()
    lines.append(f"总占用率：{cpu_text}")
    return "\n".join(lines)


def _get_mem_type() -> str:
    """获取内存类型（Windows 用 wmic，Ubuntu 用 dmidecode，需要 root）"""
    if IS_WINDOWS:
        output = _run(["wmic", "memorychip", "get", "SMBIOSMemoryType"], encoding="gbk")
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        if len(lines) > 1:
            mem_type_map = {
                "20": "DDR", "21": "DDR2", "22": "DDR2 FB-DIMM", "24": "DDR3",
                "26": "DDR4", "27": "LPDDR", "28": "LPDDR2", "29": "LPDDR3",
                "30": "LPDDR4", "31": "LPDDR4X", "32": "LPDDR5", "34": "DDR5",
            }
            code = lines[1]
            return mem_type_map.get(code, f"类型代码:{code}")
        return "未知类型"

    # Linux / Ubuntu：优先 dmidecode（普通用户可能无权限），退化为 lshw
    output = _run(["dmidecode", "-t", "memory"])
    if output:
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("Type:") and "Detail" not in line:
                value = line.split(":", 1)[1].strip()
                upper = value.upper()
                if not value or upper in ("UNKNOWN", "<OUT OF SPEC>", "OTHER"):
                    continue
                # 虚拟机 SMBIOS 常只给 "RAM" 这种通用值，加注说明避免看起来像读取失败
                if upper in ("RAM", "DRAM"):
                    return f"{upper}（虚拟化通用类型，未细分代次）"
                return upper
    hw = _run("lshw -short -class memory 2>/dev/null")
    if hw:
        for line in hw.splitlines():
            if "memory" in line.lower() and "DDR" in line.upper():
                return line.split()[-1].upper()
    return "未知类型"


def get_mem_info() -> str:
    """获取内存类型、总量、已用、占用率"""
    global _cache
    mem = psutil.virtual_memory()

    if _cache["mem_type"] is None:
        try:
            _cache["mem_type"] = _get_mem_type() or "未知类型"
        except Exception:
            _cache["mem_type"] = "未知类型"

    return (
        f"内存：{_cache['mem_type']}\n"
        f"总内存：{_fmt_bytes(mem.total)}\n"
        f"已占用：{_fmt_bytes(mem.used)}\n"
        f"占用率：{mem.percent:.{_decimal()}f}%"
    )


def get_disk_info() -> str:
    """获取磁盘占用信息"""
    path = get_config()["monitor_disk"]
    try:
        disk = psutil.disk_usage(path)
    except Exception:
        path = _default_disk()
        try:
            disk = psutil.disk_usage(path)
        except Exception as e:
            return f"磁盘信息获取失败：{str(e)[:20]}..."

    return (
        f"磁盘 ({path})\n"
        f"总量：{_fmt_bytes(disk.total)}\n"
        f"已用：{_fmt_bytes(disk.used)}\n"
        f"占用：{disk.percent:.{_decimal()}f}%"
    )


def _linux_gpu_names():
    """Ubuntu 下通过 lspci 获取显卡型号"""
    names = []
    output = _run("lspci | grep -iE 'vga|3d|display'")
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # 去掉前缀 "00:02.0 VGA compatible controller: "
        if ":" in line:
            line = line.split(":", 1)[1].strip()
            if ":" in line:
                line = line.split(":", 1)[1].strip()
        names.append(line)
    return names


def _gpu_usage_by_nvidia_smi():
    """Ubuntu 下通过 nvidia-smi 获取实时占用"""
    result = []
    output = _run(["nvidia-smi", "--query-gpu=name,utilization.gpu", "--format=csv,noheader,nounits"])
    for line in output.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            result.append((parts[0], parts[1]))
    return result


def get_gpu_info() -> str:
    """获取 GPU 信息（Windows / Ubuntu 均支持）"""
    global _cache
    if _cache["gpu_info"] is None:
        if IS_WINDOWS:
            output = _run(["wmic", "path", "win32_VideoController", "get", "Name"], encoding="gbk")
            lines = output.splitlines()[1:] if output else []
            _cache["gpu_info"] = [l.strip() for l in lines if l.strip()]
        else:
            _cache["gpu_info"] = _linux_gpu_names()

    gpu_list = []
    try:
        import pynvml
        pynvml.nvmlInit()
        for i in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            utilization = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
            gpu_name = _cache["gpu_info"][i] if i < len(_cache["gpu_info"]) else f"GPU {i}"
            gpu_list.append(f"GPU: {gpu_name}\n占用：{utilization}%")
        pynvml.nvmlShutdown()
    except Exception:
        # 没有 pynvml 时，Ubuntu 尝试 nvidia-smi 获取占用
        if not IS_WINDOWS:
            for name, util in _gpu_usage_by_nvidia_smi():
                gpu_list.append(f"GPU: {name}\n占用：{util}%")

    if gpu_list:
        return "\n".join(gpu_list)
    if _cache["gpu_info"]:
        return "\n".join(f"GPU: {name}" for name in _cache["gpu_info"])
    return "GPU: 未检测到独立显卡"


def _find_iface_by_ip(ip: str):
    """根据 IP 反查网卡名"""
    try:
        for iface, addr_list in psutil.net_if_addrs().items():
            for addr in addr_list:
                if addr.family == socket.AF_INET and addr.address.split("%")[0] == ip:
                    return iface
    except Exception:
        pass
    return None


def get_nic_ip():
    """获取网卡内网 IP，返回 (网卡名, IP)"""
    # 1) 优先：UDP 探测默认路由出口 IP（最贴近真实对外网卡）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if _is_inner_ip(ip):
            return (_find_iface_by_ip(ip) or "默认网卡"), ip
    except Exception:
        pass

    # 2) 兜底：遍历网卡取第一个有效内网 IP（跳过回环/虚拟网卡）
    try:
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        for iface, addr_list in addrs.items():
            st = stats.get(iface)
            if st is not None and not st.isup:
                continue
            low = iface.lower()
            if low == "lo" or "loopback" in low or low.startswith("docker") or low.startswith("veth") or low.startswith("br-"):
                continue
            for addr in addr_list:
                if addr.family != socket.AF_INET:
                    continue
                ip = addr.address.split("%")[0]
                if _is_inner_ip(ip):
                    return iface, ip
    except Exception:
        pass

    return None, "未知"


def _clean_speed(text: str):
    """过滤无效速率（Unknown!、N/A、-1、0 等），无效返回 None"""
    if not text:
        return None
    value = text.strip()
    low = value.lower()
    if not value or "unknown" in low or "n/a" in low or "unavailable" in low:
        return None
    if value.startswith("-") or value == "0":
        return None
    return value


def _get_nic_speed(iface) -> str:
    """获取网卡协商速率（虚拟机 virtio 网卡读不到时返回「未知」并整行省略）"""
    if IS_WINDOWS:
        output = _run(
            ["powershell", "-Command",
             "Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object -First 1 | "
             "ForEach-Object { $_.Name + ':' + $_.LinkSpeed }"],
            encoding="gbk"
        )
        return _clean_speed(output) or "未知"

    # Ubuntu：先读 /sys，其次 ethtool（virtio/vmxnet3 常返回 -1 或 Unknown!）
    if iface:
        try:
            with open(f"/sys/class/net/{iface}/speed", "r", encoding="utf-8") as f:
                speed = f.read().strip()
                if speed.lstrip("-").isdigit() and int(speed) > 0:
                    return f"{iface}:{speed}Mb/s"
        except Exception:
            pass
        output = _run(f"ethtool {iface} 2>/dev/null | grep -i speed")
        if output:
            speed = _clean_speed(output.split(":", 1)[-1])
            # ethtool 对虚拟网卡常回 "Speed: Unknown!"，此时不再输出该行
            if speed and speed.lower() != "unknown":
                return f"{iface}:{speed}"
    return "未知"


def get_network_info() -> str:
    """获取主机名、网卡内网 IP 与协商速率"""
    try:
        hostname = socket.gethostname()
        iface, ip = get_nic_ip()
        net_speed = _get_nic_speed(iface)

        result = f"系统：{PLATFORM_NAME}\n网络域：{hostname}"
        if iface:
            result += f"\nIP 地址：{ip} ({iface})"
        else:
            result += f"\nIP 地址：{ip}"
        if net_speed and net_speed != "未知":
            result += f"\n网络适配器：{net_speed}"
        return result
    except Exception as e:
        return f"网络信息获取失败：{str(e)[:20]}..."


def get_uptime_info() -> str:
    """获取系统运行时长信息"""
    try:
        boot_time = psutil.boot_time()
        import time
        from datetime import datetime

        uptime_seconds = time.time() - boot_time
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)

        boot_str = datetime.fromtimestamp(boot_time).strftime("%Y-%m-%d %H:%M:%S")

        result = f"系统已运行：{days}天 {hours}小时 {minutes}分钟"
        if days > 0:
            result += f"\n启动时间：{boot_str}"
        return result
    except Exception as e:
        return f"运行时长获取失败：{str(e)[:20]}..."


# -------------------------- 指令处理逻辑 --------------------------
@system_all_cmd.handle()
async def handle_all_info():
    """处理指令并返回所有监控信息"""
    from nonebot.exception import FinishedException

    try:
        cpu_text = get_cpu_info()
        mem_text = get_mem_info()
        disk_text = get_disk_info()
        gpu_text = get_gpu_info()
        net_text = get_network_info()
        uptime_text = get_uptime_info()

        separator = "\n" + "-" * 40 + "\n"
        all_info = separator.join([cpu_text, mem_text, disk_text, gpu_text, net_text, uptime_text])

        msg = MessageSegment.text(f"系统资源监控\n{all_info}")
        await system_all_cmd.finish(Message(msg))
    except FinishedException:
        # 正常结束，重新抛出
        raise
    except Exception as e:
        import traceback
        error_msg = f"系统监控执行失败：{str(e)}"
        print(error_msg)
        print(traceback.format_exc())
        await system_all_cmd.finish(MessageSegment.text(error_msg))

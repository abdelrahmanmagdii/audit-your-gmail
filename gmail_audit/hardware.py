import os
import platform
import shutil
import subprocess
import sys


def ram_gb():
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages and page_size:
            return (pages * page_size) / (1024 ** 3)
    except (ValueError, OSError, TypeError):
        pass

    system = platform.system()

    try:
        if system == "Darwin":
            raw = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"],
                stderr=subprocess.DEVNULL
            )
            return int(raw.strip()) / (1024 ** 3)

        if system == "Linux":
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb / (1024 ** 2)

        if system == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / (1024 ** 3)
    except Exception:
        pass

    return 8.0


def has_nvidia():
    nvidia_smi = shutil.which("nvidia-smi")

    if not nvidia_smi:
        return False

    try:
        subprocess.check_output(
            [nvidia_smi, "-L"],
            stderr=subprocess.DEVNULL,
            timeout=5
        )
        return True
    except Exception:
        return False


def mlx_importable():
    try:
        import mlx.core  # noqa: F401
        import mlx_lm  # noqa: F401
        return True
    except Exception:
        return False


def detect_machine():
    system = platform.system()
    arch = platform.machine().lower()
    apple_silicon = (
        system == "Darwin"
        and arch in ("arm64", "aarch64")
    )

    if apple_silicon:
        accelerator = "metal"
    elif has_nvidia():
        accelerator = "cuda"
    else:
        accelerator = "cpu"

    ollama_bin = shutil.which("ollama")

    return {
        "os": system,
        "os_release": platform.release(),
        "arch": arch,
        "python": sys.version.split()[0],
        "ram_gb": round(ram_gb(), 1),
        "apple_silicon": apple_silicon,
        "accelerator": accelerator,
        "ollama_bin": ollama_bin,
        "mlx_available": apple_silicon and mlx_importable(),
        "cwd": os.getcwd()
    }

"""Make pip-installed NVIDIA DLLs visible to this process only (Windows)."""
import ctypes
from importlib.metadata import PackageNotFoundError, distribution
import os

_DIRECTORY_HANDLES = []
_CONFIGURED = False


def prepare_cuda():
    global _CONFIGURED
    if os.name != "nt" or _CONFIGURED:
        return
    directories = []
    for package, folder in (("nvidia-cuda-nvrtc-cu12", "cuda_nvrtc"),
                            ("nvidia-cublas-cu12", "cublas"), ("nvidia-cudnn-cu12", "cudnn")):
        try:
            path = distribution(package).locate_file(f"nvidia/{folder}/bin").resolve()
        except PackageNotFoundError:
            continue
        if path.is_dir():
            directories.append(str(path))
            # Keep handles alive: closing one removes its search directory.
            _DIRECTORY_HANDLES.append(os.add_dll_directory(str(path)))
    if directories:
        # Native libraries may use LoadLibrary rather than Python's DLL loader.
        # Only the current process and its children see this PATH modification.
        os.environ["PATH"] = os.pathsep.join(directories + [os.environ.get("PATH", "")])
    _CONFIGURED = True


def check_cuda_libraries():
    prepare_cuda()
    if os.name != "nt":
        return
    missing = []
    for name in ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(name)
        except OSError:
            missing.append(name)
    if missing:
        raise RuntimeError("Для Whisper CUDA не загружаются: " + ", ".join(missing)
                           + ". Установите .venv\\Scripts\\python.exe -m pip install -r requirements-cuda.txt"
                           + " либо выберите cpu.")

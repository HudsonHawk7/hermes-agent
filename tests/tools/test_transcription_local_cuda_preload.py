"""The Windows cuBLAS 12 preload that makes local whisper's CUDA path loadable.

ctranslate2's Windows wheel ships cuDNN 9 but not cuBLAS 12, and Python no longer searches PATH
for an extension's dependencies, so ``device: cuda`` died with "Library cublas64_12.dll is not
found or cannot be loaded" and the CPU fallback cost 6-8 s per utterance. These pin the loader:
the DLL directory comes from the installed wheel, the pair is loaded in dependency order, and a
missing or broken runtime warns ONCE naming the extra to install instead of degrading silently.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools import transcription_local


class _FakeSpec:
    """What ``importlib.util.find_spec("nvidia.cublas")`` returns for an installed wheel."""

    def __init__(self, locations):
        self.submodule_search_locations = list(locations)


@pytest.fixture(autouse=True)
def _fresh_preload_state(monkeypatch):
    """Each test starts as if nothing had been probed or loaded yet."""
    monkeypatch.setattr(transcription_local, "_cublas_preload", None)
    monkeypatch.setattr(transcription_local, "_cuda_dll_dir_cookies", [])


def _installed_cublas(tmp_path, monkeypatch):
    """Point the probe at a fake installed nvidia-cublas-cu12 wheel; return its bin directory."""
    package_dir = tmp_path / "nvidia" / "cublas"
    bin_dir = package_dir / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setattr(transcription_local, "_ilu",
                        SimpleNamespace(find_spec=lambda name: _FakeSpec([package_dir])))
    return bin_dir


def _recording_loader(monkeypatch):
    """Replace the real DLL loads with a recorder; return the calls list and the cookie."""
    calls = []
    cookie = MagicMock(name="dll_directory_cookie")

    def add_dll_directory(path):
        calls.append(("add_dll_directory", path))
        return cookie

    def load(dll_path):
        calls.append(("load", dll_path))
        return MagicMock(name=dll_path)

    monkeypatch.setattr(transcription_local.os, "add_dll_directory", add_dll_directory)
    monkeypatch.setattr(transcription_local.ctypes, "CDLL", load)
    return calls, cookie


# ---------------------------------------------------------------------------
# Which loads need the preload (pure: the host is data, not sys.platform)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("device", ["cuda", "CUDA", " cuda ", "auto", "Auto"])
def test_needs_preload_for_gpu_devices_on_windows(device):
    assert transcription_local._cuda_preload_needed(device, "Windows") is True


@pytest.mark.parametrize("device", ["cpu", "int8", "", None])
def test_does_not_need_preload_for_cpu_devices(device):
    assert transcription_local._cuda_preload_needed(device, "Windows") is False


@pytest.mark.parametrize("host", ["Linux", "Darwin"])
@pytest.mark.parametrize("device", ["cuda", "auto"])
def test_does_not_need_preload_off_windows(device, host):
    """ctranslate2's Linux/macOS wheels resolve CUDA through LD_LIBRARY_PATH; nothing to preload."""
    assert transcription_local._cuda_preload_needed(device, host) is False


def test_preload_is_skipped_for_a_cpu_device_without_probing(monkeypatch):
    probed = []
    monkeypatch.setattr(transcription_local, "_ilu",
                        SimpleNamespace(find_spec=lambda name: probed.append(name)))

    assert transcription_local._ensure_cublas_preloaded("cpu", host="Windows") is False
    assert probed == []


# ---------------------------------------------------------------------------
# Finding the wheel's DLLs
# ---------------------------------------------------------------------------


def test_cublas_bin_dir_comes_from_the_installed_wheel(tmp_path, monkeypatch):
    bin_dir = _installed_cublas(tmp_path, monkeypatch)
    assert transcription_local._cublas_bin_dir() == bin_dir


def test_cublas_bin_dir_is_none_when_the_package_is_absent(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(transcription_local, "_ilu", SimpleNamespace(find_spec=missing))
    assert transcription_local._cublas_bin_dir() is None


def test_cublas_bin_dir_is_none_without_a_bin_folder(tmp_path, monkeypatch):
    """A wheel tree is only usable if it actually carries the DLLs."""
    package_dir = tmp_path / "nvidia" / "cublas"
    (package_dir / "lib").mkdir(parents=True)
    monkeypatch.setattr(transcription_local, "_ilu",
                        SimpleNamespace(find_spec=lambda name: _FakeSpec([package_dir])))
    assert transcription_local._cublas_bin_dir() is None


# ---------------------------------------------------------------------------
# The load itself
# ---------------------------------------------------------------------------


@pytest.mark.platforms("windows")
def test_preload_adds_the_dll_directory_and_loads_in_dependency_order(tmp_path, monkeypatch):
    bin_dir = _installed_cublas(tmp_path, monkeypatch)
    calls, cookie = _recording_loader(monkeypatch)

    assert transcription_local._ensure_cublas_preloaded("cuda", host="Windows") is True

    assert calls == [
        ("add_dll_directory", str(bin_dir)),
        # cublas64_12.dll depends on cublasLt64_12.dll: the LT pair has to be resident first.
        ("load", str(bin_dir / "cublasLt64_12.dll")),
        ("load", str(bin_dir / "cublas64_12.dll")),
    ]
    # The cookie must outlive the call — the loader drops the directory when it is collected.
    assert transcription_local._cuda_dll_dir_cookies == [cookie]


@pytest.mark.platforms("windows")
def test_preload_happens_once_per_process(tmp_path, monkeypatch):
    _installed_cublas(tmp_path, monkeypatch)
    calls, _cookie = _recording_loader(monkeypatch)

    assert transcription_local._ensure_cublas_preloaded("auto", host="Windows") is True
    assert transcription_local._ensure_cublas_preloaded("cuda", host="Windows") is True

    assert len([call for call in calls if call[0] == "load"]) == 2


@pytest.mark.platforms("windows")
def test_missing_package_warns_once_and_names_the_extra(monkeypatch, caplog):
    monkeypatch.setattr(transcription_local, "_ilu",
                        SimpleNamespace(find_spec=lambda name: (_ for _ in ()).throw(ImportError)))
    calls, _cookie = _recording_loader(monkeypatch)

    with caplog.at_level("WARNING", logger="tools.transcription_tools"):
        assert transcription_local._ensure_cublas_preloaded("cuda", host="Windows") is False
        assert transcription_local._ensure_cublas_preloaded("cuda", host="Windows") is False

    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1, warnings
    assert "'stt-whisper'" in warnings[0]
    assert "nvidia-cublas-cu12" in warnings[0]
    assert "CPU" in warnings[0]
    assert [call for call in calls if call[0] == "load"] == []


@pytest.mark.platforms("windows")
def test_a_dll_that_will_not_load_warns_and_returns_false(tmp_path, monkeypatch, caplog):
    bin_dir = _installed_cublas(tmp_path, monkeypatch)
    monkeypatch.setattr(transcription_local.os, "add_dll_directory", lambda path: MagicMock())

    def broken_load(dll_path):
        raise OSError(f"[WinError 126] The specified module could not be found: {dll_path}")

    monkeypatch.setattr(transcription_local.ctypes, "CDLL", broken_load)

    with caplog.at_level("WARNING", logger="tools.transcription_tools"):
        assert transcription_local._ensure_cublas_preloaded("cuda", host="Windows") is False

    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1, warnings
    assert str(bin_dir / "cublasLt64_12.dll") in warnings[0]
    assert "CPU" in warnings[0]


@pytest.mark.platforms("windows")
def test_the_model_load_preloads_cublas_before_faster_whisper(monkeypatch):
    """The seam: a GPU load must put cuBLAS on the search path BEFORE WhisperModel is built."""
    events = []
    monkeypatch.setattr(transcription_local, "_ensure_cublas_preloaded",
                        lambda device: events.append(("preload", device)))
    monkeypatch.setattr("faster_whisper.WhisperModel",
                        lambda *args, **kwargs: events.append(("model", kwargs["device"])) or object())

    transcription_local._create_whisper_model("base", device="cuda", compute_type="int8_float16")

    assert events == [("preload", "cuda"), ("model", "cuda")]

"""Interactive identity prompts and the `--release` flag (offline).

`yang-downloader` asks for whatever `.env` does not provide (IP, name, release,
port [830], user, password) and records name+release in `<yang-dir>/device.json`.
`yang2sdk` accepts `--release`, reads that file, and asks when nothing supplies
a release -- otherwise every package was `<device>_0_0_0`.

conftest loads an untracked local `.env` into os.environ, so every test clears
the names it depends on, or it passes locally and fails in CI.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from yang2sdk.cli import device_info
from yang2sdk.cli import main as cli_main
from yang2sdk.cli.compiler import _resolve_device_version
from yang2sdk.cli.device_info import read_device_info, write_device_info

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_NAMES = (
    "DEVICE_IP",
    "DEVICE_NAME",
    "DEVICE_VERSION",
    "NETCONF_PORT",
    "DEVICE_USER",
    "DEVICE_PASS",
    "DEVICE_SSH_KEY",
    "DEVICE_SSH_KEY_PASSPHRASE",
)
MINIMAL_YANG = """module tmod {
  prefix tm;
  namespace "urn:test:tmod";
  revision 2026-01-01;
  container top {
    leaf name {
      type string;
    }
  }
}
"""


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _tty(monkeypatch, answers: list[str]):
    """Pretend a human is at the terminal, answering `answers` in order."""
    feed = iter(answers)
    asked: list[str] = []

    def fake_ask(label, default=None, *, secret=False):
        asked.append(label)
        return next(feed) or (default or "")

    for mod in (device_info, cli_main):
        monkeypatch.setattr(mod, "interactive", lambda: True, raising=False)
    monkeypatch.setattr(cli_main, "ask", fake_ask)
    from yang2sdk.cli import downloader

    monkeypatch.setattr(downloader, "interactive", lambda: True)
    monkeypatch.setattr(downloader, "ask", fake_ask)
    return asked


# --- device.json ---------------------------------------------------------


def test_device_info_round_trips_and_tolerates_absence(tmp_path):
    assert read_device_info(tmp_path) == {}
    write_device_info(tmp_path, device="srl", release="25.10.1")
    assert read_device_info(tmp_path) == {"device": "srl", "release": "25.10.1"}
    (tmp_path / "device.json").write_text("not json")
    assert read_device_info(tmp_path) == {}


def test_release_precedence_flag_then_device_json_then_scoped_env(clean_env, tmp_path):
    write_device_info(tmp_path, device="srl", release="25.10.1")
    clean_env.setenv("DEVICE_NAME", "srl")
    clean_env.setenv("DEVICE_VERSION", "1.0.0")

    assert _resolve_device_version("9.9.9", "srl", tmp_path) == "9.9.9"
    assert _resolve_device_version("", "srl", tmp_path) == ""  # explicit "none"
    # The file describes the modules actually compiled: it beats ambient env.
    assert _resolve_device_version(None, "srl", tmp_path) == "25.10.1"
    # Scoped like the env pair: a file for another device is not inherited.
    assert _resolve_device_version(None, "other", tmp_path) == ""
    # No file: the existing scoped env fallback is unchanged.
    assert _resolve_device_version(None, "srl", tmp_path / "missing") == "1.0.0"


def _compile(tmp_path, *extra, env=None):
    (tmp_path / "tmod.yang").write_text(MINIMAL_YANG)
    out = tmp_path / "out"
    code = "import sys; from yang2sdk.cli.main import main; sys.exit(main())"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            "--protocol",
            "restconf",
            "--roots",
            "tmod",
            "--device",
            "lab",
            "--yang-dir",
            str(tmp_path),
            "--output-dir",
            str(out),
            *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    return sorted(p.name for p in out.iterdir())


def _env_without_identity():
    return {k: v for k, v in os.environ.items() if k not in ENV_NAMES}


def test_release_flag_names_the_package(tmp_path):
    """Wiring: `yang2sdk --release` must reach the package name."""
    assert _compile(tmp_path, "--release", "2.1.0", env=_env_without_identity()) == [
        "lab_2_1_0"
    ]


def test_device_json_names_the_package_without_a_flag(tmp_path):
    write_device_info(tmp_path, device="lab", release="3.4.5")
    assert _compile(tmp_path, env=_env_without_identity()) == ["lab_3_4_5"]


def test_no_release_and_no_tty_still_compiles_as_0_0_0(tmp_path):
    """Negative control: without a terminal nothing is asked and CI never blocks."""
    assert _compile(tmp_path, env=_env_without_identity()) == ["lab_0_0_0"]


# --- yang2sdk prompts ----------------------------------------------------


def test_yang2sdk_asks_for_device_and_release_when_missing(clean_env, tmp_path):
    asked = _tty(clean_env, ["lab", "7.0.1"])
    forwarded = cli_main._ask_device(["--yang-dir", str(tmp_path)])
    assert forwarded[-2:] == ["--device", "lab"]
    forwarded = cli_main._ask_release(forwarded)
    assert forwarded[-2:] == ["--release", "7.0.1"]
    assert len(asked) == 2


def test_yang2sdk_does_not_ask_when_release_is_known(clean_env, tmp_path):
    asked = _tty(clean_env, [])
    base = ["--device", "lab", "--yang-dir", str(tmp_path)]
    assert cli_main._ask_release([*base, "--release", "1.0"]) == [
        *base,
        "--release",
        "1.0",
    ]
    write_device_info(tmp_path, device="lab", release="3.4.5")
    assert cli_main._ask_release(base) == base
    assert asked == []


def test_yang2sdk_empty_release_answer_is_an_explicit_none(clean_env, tmp_path):
    _tty(clean_env, [""])
    out = cli_main._ask_release(["--device", "lab", "--yang-dir", str(tmp_path)])
    assert out[-2:] == ["--release", ""]


# --- yang-downloader prompts ---------------------------------------------


def test_downloader_asks_for_every_missing_value(clean_env):
    from yang2sdk.cli import downloader

    asked = _tty(clean_env, ["192.0.2.1", "lab", "4.2", "", "admin"])
    settings = downloader._resolve_settings()
    assert settings == {
        "DEVICE_IP": "192.0.2.1",
        "DEVICE_NAME": "lab",
        "DEVICE_VERSION": "4.2",
        "NETCONF_PORT": "830",  # empty answer -> RFC 6242 Sec 3 default
        "DEVICE_USER": "admin",
    }
    assert len(asked) == 5


def test_downloader_env_values_are_not_asked_again(clean_env):
    from yang2sdk.cli import downloader

    for k, v in {
        "DEVICE_IP": "192.0.2.1",
        "DEVICE_NAME": "lab",
        "DEVICE_VERSION": "4.2",
        "NETCONF_PORT": "1830",
        "DEVICE_USER": "admin",
    }.items():
        clean_env.setenv(k, v)
    asked = _tty(clean_env, [])
    assert downloader._resolve_settings()["NETCONF_PORT"] == "1830"
    assert asked == []


def test_downloader_without_tty_fails_closed_on_missing_values(clean_env, capsys):
    from yang2sdk.cli import downloader

    clean_env.setattr(downloader, "interactive", lambda: False)
    with pytest.raises(SystemExit) as exc:
        downloader._resolve_settings()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    for key in ("DEVICE_IP", "DEVICE_NAME", "DEVICE_USER"):
        assert key in err
    # Optional values are not reported as missing.
    assert "NETCONF_PORT" not in err and "DEVICE_VERSION" not in err


def test_downloader_rejects_a_non_integer_port(clean_env):
    from yang2sdk.cli import downloader

    _tty(clean_env, ["192.0.2.1", "lab", "", "eighty", "admin"])
    with pytest.raises(SystemExit) as exc:
        downloader._resolve_settings()
    assert exc.value.code == 2


def test_downloader_main_asks_password_and_records_device_json(
    clean_env, tmp_path, monkeypatch
):
    from yang2sdk.cli import downloader

    _tty(clean_env, ["192.0.2.1", "lab", "4.2", "", "admin", "s3cret"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(downloader, "load_dotenv", lambda: None)
    seen = {}

    def fake_download_all(self):
        seen["password"] = self.password
        seen["port"] = self.port
        return 0

    monkeypatch.setattr(downloader.YangDownloader, "download_all", fake_download_all)
    downloader.main()
    assert seen == {"password": "s3cret", "port": 830}
    out = tmp_path / "temp" / "yang_modules" / "lab"
    assert read_device_info(out) == {"device": "lab", "release": "4.2"}


def test_ask_reads_a_line_and_applies_the_default(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\nvalue\n"))
    assert device_info.ask("Port", "830") == "830"
    assert device_info.ask("Name") == "value"

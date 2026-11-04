"""ArgoWorker 的环境装配契约。"""

from __future__ import annotations

import os
from pathlib import Path

from worker import ArgoWorker, sanitize_no_proxy


def _worker(**kwargs) -> ArgoWorker:
    base = {
        "python_path": "python",
        "argo_dir": r"C:\argo-root",
        "argo_env_file": r"C:\argo-root\env",
        "temp_image_root": r"C:\argo-temp",
    }
    base.update(kwargs)
    return ArgoWorker(**base)


def test_build_env_drops_safepath_and_prepends_module_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    monkeypatch.setenv("PYTHONPATH", r"C:\host-site-packages")

    env = _worker(temp_image_root=str(tmp_path))._build_env()

    assert "PYTHONSAFEPATH" not in env
    roots = env["PYTHONPATH"].split(os.pathsep)
    assert roots[0] == str(Path(r"C:\argo-root") / "scripts")
    assert roots[1] == r"C:\argo-root"
    assert r"C:\host-site-packages" in roots


def test_build_env_without_existing_pythonpath(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHONPATH", raising=False)

    env = _worker(temp_image_root=str(tmp_path))._build_env()

    assert env["PYTHONPATH"] == os.pathsep.join(
        [str(Path(r"C:\argo-root") / "scripts"), r"C:\argo-root"]
    )
    assert env["ARGO_IMAGE_LOCAL_ROOTS"] == str(tmp_path)
    assert env["ARGO_ENV_FILE"] == r"C:\argo-root\env"


def test_sanitize_no_proxy_drops_ipv6_loopback():
    assert sanitize_no_proxy("127.0.0.1,localhost,[::1]") == "127.0.0.1,localhost"
    assert sanitize_no_proxy("[::1]") == ""
    assert sanitize_no_proxy("") == ""

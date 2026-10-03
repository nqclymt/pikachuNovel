"""Build and smoke-test the Apple Silicon .app, then create a DMG and ZIP."""

from __future__ import annotations

import hashlib
import platform
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(*args: str | Path) -> None:
    subprocess.run([str(arg) for arg in args], cwd=PROJECT_ROOT, check=True)


def main() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("需要在 M 系列 Mac 上使用原生 arm64 Python 构建；不能在 Windows 或 Rosetta Python 中运行。")

    release = PROJECT_ROOT / "release" / "macos-arm64"
    build = PROJECT_ROOT / "build"
    release.mkdir(parents=True, exist_ok=True)
    build.mkdir(parents=True, exist_ok=True)
    version = runpy.run_path(str(PROJECT_ROOT / "webui" / "version.py"))["APP_VERSION"]
    stem = f"PikachuNovel-{version}-macOS-arm64"
    app = release / "PikachuNovel.app"

    with tempfile.TemporaryDirectory(prefix="macos-build-", dir=build) as temporary:
        temporary = Path(temporary)
        run(
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", release, "--workpath", temporary / "pyinstaller",
            PROJECT_ROOT / "packaging" / "PikachuNovel-macos.spec",
        )
        run("/usr/bin/lipo", "-verify_arch", "arm64", app / "Contents" / "MacOS" / "PikachuNovel")
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
        run(sys.executable, PROJECT_ROOT / "scripts" / "validate_macos_build.py", "--app", app)

        stage = temporary / "dmg"
        stage.mkdir()
        run("/usr/bin/ditto", app, stage / app.name)
        (stage / "Applications").symlink_to("/Applications")
        run(
            "/usr/bin/hdiutil", "create", "-volname", "PikachuNovel",
            "-srcfolder", stage, "-format", "UDZO", "-ov", release / f"{stem}.dmg",
        )
        # ditto preserves the executable permissions and framework symlinks.
        run(
            "/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
            app, temporary / f"{stem}.app.zip",
        )
        (temporary / f"{stem}.app.zip").replace(release / f"{stem}.app.zip")

    for suffix in (".dmg", ".app.zip"):
        artifact = release / f"{stem}{suffix}"
        with artifact.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        artifact.with_name(artifact.name + ".sha256").write_text(
            f"{digest}  {artifact.name}\n", encoding="utf-8"
        )
        print(f"已构建：{artifact}")


if __name__ == "__main__":
    main()

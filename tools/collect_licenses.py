"""Copy notices for code included in the Windows distribution."""

from importlib.metadata import distribution
from pathlib import Path
from shutil import copy2
import sys


def collect(output: Path) -> None:
    notices = output / "LICENSES"
    notices.mkdir(exist_ok=True)
    for name in ("PyMuPDF", "PySide6-Essentials", "shiboken6", "PyInstaller", "pypdf"):
        for file in distribution(name).files:
            filename = file.name.lower()
            if "licenses" in file.parts or filename.startswith(("license", "copying", "notice", "copyright")):
                source = Path(file.locate())
                if source.is_file():
                    folder = notices / name
                    folder.mkdir(exist_ok=True)
                    copy2(source, folder / source.name)
    for name in ("LICENSE.txt", "LICENSE", "LICENSE.md"):
        python_license = Path(sys.base_prefix) / name
        if python_license.is_file():
            copy2(python_license, notices / "Python-LICENSE.txt")
            break
    root = Path(__file__).resolve().parents[1]
    for license_text in (root / "licenses").glob("*.txt"):
        copy2(license_text, notices)
    copy2(root / "LICENSE", output / "LICENSE.txt")
    copy2(root / "THIRD_PARTY.md", notices / "THIRD_PARTY.md")


if __name__ == "__main__":
    collect(Path(sys.argv[1]))

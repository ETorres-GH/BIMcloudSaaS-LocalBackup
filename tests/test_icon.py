import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "src" / "bimcloud_backup" / "assets"
ICO_SIZES = {16, 24, 32, 48, 64, 256}
PNG_SIZES = (16, 32, 48, 64, 256)


def ico_sizes(path: Path) -> set[int]:
    """Image sizes listed in the header of an .ico file (a width of 0 means 256)."""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1), "não é um arquivo .ico"
    sizes = set()
    for i in range(count):
        width, height = struct.unpack_from("<BB", data, 6 + 16 * i)
        assert width == height
        sizes.add(width or 256)
    return sizes


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "não é um arquivo PNG"
    return struct.unpack_from(">II", data, 16)


def test_ico_has_every_size_windows_uses():
    assert ico_sizes(ASSETS / "icon.ico") == ICO_SIZES


def test_window_pngs_exist_with_their_size():
    for size in PNG_SIZES:
        assert png_size(ASSETS / f"icon-{size}.png") == (size, size)


def test_svg_source_is_kept():
    assert (ASSETS / "icon.svg").read_text(encoding="utf-8").lstrip().startswith("<svg")


def test_build_script_uses_the_icon():
    script = (ROOT / "scripts" / "build_exe.ps1").read_text(encoding="utf-8-sig")
    line = next(line for line in script.splitlines() if "--icon" in line)
    icon = line.split("--icon", 1)[1].strip().rstrip("`").strip()
    assert (ROOT / Path(icon.replace("\\", "/"))).is_file()

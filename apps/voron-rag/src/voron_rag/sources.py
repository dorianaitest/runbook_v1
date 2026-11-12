"""The approved official documentation sources, pinned to exact commits.

Only files listed here are ever downloaded. Pinning the commit makes the corpus reproducible
and lets every chunk say which version of the docs it came from.
"""

import urllib.request
from dataclasses import dataclass
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parents[2] / "data/raw/docs"

VORON_DOCS = ("VoronDesign/Voron-Documentation", "b8c014f88948c9fca963dfe726e0e66b1287af90")
KLIPPER = ("Klipper3d/klipper", "0cee841e31d701595b3e509a0b502f6e320f52a8")


@dataclass(frozen=True)
class Source:
    source_id: str
    repo: str
    commit: str
    path: str
    url: str  # public page users should open, not the raw file
    board_variant: str = "any"
    # Printers the whole page applies to; a printer name in a heading overrides it for that section.
    printers: tuple[str, ...] = ("any",)
    title: str = ""  # overrides front matter / H1 when a page has neither usable
    # Level-2 heading prefixes to keep; empty = keep everything.
    keep_sections: tuple[str, ...] = ()
    # Interactive-page <div id=...> prefixes to drop (D05: other printer models, UI pages).
    drop_pages: tuple[str, ...] = ()

    @property
    def raw_url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.commit}/{self.path}"

    @property
    def local_path(self) -> Path:
        return DOCS_DIR / f"{self.source_id}.md"


def _voron(source_id: str, path: str, url: str, **kw) -> Source:
    return Source(source_id, *VORON_DOCS, path, url, **kw)


def _klipper(source_id: str, path: str, url: str, **kw) -> Source:
    return Source(source_id, *KLIPPER, path, url, **kw)


SOURCES = [
    _voron("D01", "community/troubleshooting/eddie/klipper_common_errors.md",
           "https://docs.vorondesign.com/community/troubleshooting/eddie/klipper_common_errors.html"),
    _voron("D02", "build/electrical/index.md", "https://docs.vorondesign.com/build/electrical/"),
    _voron("D03", "build/electrical/v2_octopus_wiring.md",
           "https://docs.vorondesign.com/build/electrical/v2_octopus_wiring.html", board_variant="BTT Octopus", printers=("v2",)),
    _voron("D04", "build/software/configuration.md", "https://docs.vorondesign.com/build/software/configuration.html"),
    _voron("D05", "build/startup/startup.md", "https://docs.vorondesign.com/build/startup/startup.html",
           title="Voron Initial Startup Guide", printers=("v2",),
           # v0* = V0, v1*/ztilt = Trident, vsw*/vws* = Switchwire; the rest is UI scaffolding.
           drop_pages=("CHANGE_ME", "0", "ProbeSelect", "info", "v0", "v1", "ztilt", "vsw", "vws")),
    _voron("D06", "maintenance.md", "https://docs.vorondesign.com/maintenance.html"),
    _voron("D07", "build/mechanical/v2_gantry_squaring.md",
           "https://docs.vorondesign.com/build/mechanical/v2_gantry_squaring.html", printers=("v2",)),
    _voron("D08", "community/howto/120decibell/z_endstop_configuration.md",
           "https://docs.vorondesign.com/community/howto/120decibell/z_endstop_configuration.html"),
    _klipper("D09", "docs/Config_checks.md", "https://www.klipper3d.org/Config_checks.html"),
    _klipper("D10", "docs/Config_Reference.md", "https://www.klipper3d.org/Config_Reference.html",
             keep_sections=("Micro-controller configuration", "Common extruder and heated bed support",
                            "Bed level support", "Bed probing hardware", "Custom heaters and sensors",
                            "Temperature sensors")),
    _klipper("D11", "docs/FAQ.md", "https://www.klipper3d.org/FAQ.html",
             keep_sections=("Why can't I move the stepper", "Why is the Z position_endstop",
                            "My TMC motor driver turns off", 'I keep getting random "Lost communication',
                            "Will the heaters be left on", "Do I have to wire my device",
                            "Why does Klipper report errors")),
]


def fetch_docs(sources: list[Source] = SOURCES) -> list[tuple[str, str]]:
    """Download missing files. Returns (source_id, status) pairs; never re-downloads existing files."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for src in sources:
        if src.local_path.exists():
            results.append((src.source_id, "exists"))
            continue
        try:
            with urllib.request.urlopen(src.raw_url, timeout=30) as resp:
                src.local_path.write_bytes(resp.read())
            results.append((src.source_id, "downloaded"))
        except OSError as e:  # network errors and HTTP errors are both OSError subclasses
            results.append((src.source_id, f"FAILED: {e}"))
    return results

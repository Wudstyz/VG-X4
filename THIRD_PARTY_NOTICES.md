# Third-party software / Logiciels tiers

Atelier Traceuse is released under the MIT license (see `LICENSE`). The packaged application (`.dmg`) also
contains the following third-party components, each under its own license (the license texts ship inside
the application, in `Contents/Resources/python/lib/python3.14/site-packages/*/` and `.../python/lib/python3.14/LICENSE.txt`).

| Component | Version at build | License |
|---|---|---|
| CPython (via [python-build-standalone](https://github.com/astral-sh/python-build-standalone)) | 3.14.10 | PSF License |
| pywebview | 6.2.1 | BSD-3-Clause |
| pyserial | 3.5 | BSD-3-Clause |
| Pillow | 13.0.0 | MIT-CMU (HPND) |
| NumPy | 2.5.4 | BSD-3-Clause and others (see its package) |
| fontTools | 4.68.0 | MIT |
| scikit-image | 0.26.0 | BSD-3-Clause and others (see its package) |
| Hershey-Fonts (HersheyFonts) | 2.1.0 | MIT |
| svgelements | 1.9.6 | MIT |
| PyObjC (Cocoa) | 13.0 | MIT |
| python-docx | 1.2.0 | MIT |
| pypdf | 7.0.0 | BSD-3-Clause |
| pdfplumber | 0.11.10 | MIT |

Their own dependencies (SciPy, lxml, pdfminer.six, pypdfium2, …) are installed by `pip` and keep their licenses.

## Hershey fonts

The stroke fonts used for handwriting are the Hershey vector fonts, created by Dr. A. V. Hershey (U.S. National Bureau
of Standards), distributed through the MIT-licensed `Hershey-Fonts` Python package. The original distribution permits
use and redistribution provided the credit is kept: *"Hershey Fonts: Dr. A. V. Hershey, NBS"*.

## GRBL protocol facts / Protocole GRBL

The machine protocol (115200 baud, `M3 S905` = pen down, `M5` = pen up, `G4 P…` dwell, `G92` zero, `!` `~` ctrl-X)
was checked against the MIT-licensed repository Morgulisan/VGX4-Control. No code was copied from it, only facts.

## Trademarks / Marques

"VigoTec", "VG-X4" and "GRBL" belong to their respective owners. This project is independent and is not affiliated
with, endorsed or sponsored by VigoTec.

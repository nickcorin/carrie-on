# Carrie On artwork

The icon joins a C to a forward arrow. The app icon uses porcelain white, `#F4F4FF`, on cobalt, `#354DE8`. The menu bar uses the same silhouette as a monochrome template.

The artwork files have these roles:

- `mark.svg`: canonical vector silhouette, traced from the original Midnight artwork. All outputs share this path, scaled uniformly.
- `mark-reference.png` and `mark-reference-mask.png`: original 380 × 315 reference crop and its 50% luminance silhouette mask. The trace uses Potrace 1.16 with `--turdsize 0 --alphamax 1 --opttolerance 0.01`.
- `app-icon.svg` and `tray-icon.svg`: generated color and monochrome vector assets.
- `app-icon.png` and `CarrieOn.icns`: generated 1024-pixel app artwork and macOS icon family.
- `tray-18.png` and `tray-36.png`: menu-bar previews at standard and Retina resolution.
- `../menubar/Brand.swift`: generated AppKit geometry, used directly by the native tray and popover.

Run `python3 build_icons.py` on macOS with Apple's command-line tools to regenerate every output. Edit the canonical `mark.svg` rather than generated paths. The build uses AppKit for rasterization and `sips` and `iconutil` for packaging. It needs no image-generation service.

The build compares `mark-rendered.png` with the reference silhouette and writes `trace-verification.json`. It requires at least 99% silhouette overlap and rejects edge displacement beyond one pixel at the original reference resolution. This tolerance accounts for raster antialiasing and curve fitting. It does not allow reshaping the mark. App and tray outputs use identical vector geometry.

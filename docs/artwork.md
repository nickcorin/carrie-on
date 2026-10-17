# Artwork

Edit `assets/mark.svg` to change the shared app and menu-bar silhouette. The reference image and mask are in `assets/mark-reference.png` and `assets/mark-reference-mask.png`.

Run `just setup`, then `just icons` on macOS with Apple's command-line tools. This regenerates the SVGs, PNGs, and icon family in `assets/`, plus the native drawing code in `menubar/Brand.swift`. Do not edit generated files directly.

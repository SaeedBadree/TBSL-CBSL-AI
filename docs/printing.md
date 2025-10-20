# Thermal Printing — Star TSP-500

## Browser (Chrome/Edge) print dialog
- Destination: Star TSP-xxx
- More settings:
  - Scale: 100%
  - Margins: None
  - Headers/Footers: Off
  - Paper size: 72–80 mm Receipt (from Star driver), NOT A4/Letter

## StarPRNT driver (Windows)
- Printing Preferences → Advanced:
  - Paper Size: 72 mm x Receipt (or the correct 80 mm profile)
  - Cut: Cut at end of document (or per page if printing multiple receipts in one job)
  - Disable “Fit to page” or extended margins

## Troubleshooting
- If you still get a long blank feed:
  1) Ensure no fixed `height`/`min-height` in print CSS.
  2) Verify `@page { size: 80mm auto; margin: 0; }` is active (print preview should show a narrow, short page).
  3) Print to PDF first. If the receipt sits at bottom in the PDF, it’s a CSS/layout issue; if it’s correct in PDF but printer feeds long, it’s a driver setting.



"""
Task #26 RE follow-up — set up TRUE-STOCK vs MODDED test.

Default mode (no flag): rendered = stock, variant = modded.
  - HecateHub_Mesh           = TRUE STOCK bytes
  - HecateTestVariant_Mesh   = MODDED Hub bytes

With --flip: rendered = modded, variant = stock (tests swap DIRECTION).
  - HecateHub_Mesh           = MODDED Hub bytes
  - HecateTestVariant_Mesh   = TRUE STOCK bytes

In-game: click 'RE #26: Clone-swap test' to remap HecateHub_Mesh -> variant.

Restore: run this again after rebuilding; or let the builder rebuild, then
re-run.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gpk_pack import extract_gpk, pack_gpk

STOCK_GPK  = r"C:/Program Files (x86)/Steam/steamapps/common/Hades II/Content/GR2/_Optimized/Hecate.gpk"
MERGED_GPK = r"C:/Users/ender/AppData/Roaming/r2modmanPlus-local/HadesII/profiles/Default/ReturnOfModding/plugins_data/Enderclem-CG3HBuilder/Hecate.gpk"

TARGET_ENTRY  = "HecateHub_Mesh"
VARIANT_ENTRY = "HecateTestVariant_Mesh"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flip", action="store_true",
                    help="Render modded, swap to stock (tests direction).")
    args = ap.parse_args()

    if not os.path.isfile(STOCK_GPK):
        sys.exit(f"Stock Hecate.gpk not found: {STOCK_GPK}")
    if not os.path.isfile(MERGED_GPK):
        sys.exit(f"Merged Hecate.gpk not found: {MERGED_GPK}")

    stock = extract_gpk(STOCK_GPK)
    if TARGET_ENTRY not in stock:
        sys.exit(f"{TARGET_ENTRY!r} not in stock GPK")

    merged = extract_gpk(MERGED_GPK)
    if TARGET_ENTRY not in merged:
        sys.exit(f"{TARGET_ENTRY!r} not in merged GPK")

    # Pull modded bytes from the CURRENT merged GPK's TestVariant if present
    # (that's what got stashed on the previous run); otherwise fall back to
    # HecateHub_Mesh (first-time setup).
    modded_bytes = merged.get(VARIANT_ENTRY) or merged[TARGET_ENTRY]
    stock_bytes  = stock[TARGET_ENTRY]

    if stock_bytes == modded_bytes:
        print(f"NOTE: merged content matches stock — no mods contributing.")

    if args.flip:
        merged[TARGET_ENTRY]  = modded_bytes   # rendered = MODDED
        merged[VARIANT_ENTRY] = stock_bytes    # variant  = STOCK
        render_label, variant_label = "MODDED Hub", "TRUE STOCK"
    else:
        merged[TARGET_ENTRY]  = stock_bytes    # rendered = STOCK
        merged[VARIANT_ENTRY] = modded_bytes   # variant  = MODDED
        render_label, variant_label = "TRUE STOCK", "MODDED Hub"

    pack_gpk(merged, MERGED_GPK)

    print(f"Wrote {MERGED_GPK}")
    print(f"  {TARGET_ENTRY}:          {len(merged[TARGET_ENTRY]):,} bytes ({render_label})")
    print(f"  {VARIANT_ENTRY}: {len(merged[VARIANT_ENTRY]):,} bytes ({variant_label})")
    print()
    print("Test in game:")
    print("  1. Restart the game so the new GPK is read.")
    print(f"  2. Hub scene should render {render_label}.")
    print(f"  3. Click 'RE #26: Clone-swap test' -> swap to {variant_label}.")
    print("  4. Click 'RE #26: Restore' -> back to initial.")


if __name__ == "__main__":
    main()

"""
CG3H v3.0.0 Manual Test Runner

Reads test cases, walks through them interactively, generates a report.

Usage:
    python tests/run_manual_tests.py
    python tests/run_manual_tests.py --resume  (continue from last saved state)
"""
import json
import os
import sys
from datetime import datetime

TESTS = [
    # (category, test_id, description, steps)
    ("GUI Launch", "1.1", "Window opens without errors", [
        "Run: python tools/converter_gui.py",
        "Window appears with title 'CG3H Mod Builder'",
    ]),
    ("GUI Launch", "1.2", "Three tabs visible", [
        "Tabs: Create, Build, Mods",
    ]),
    ("GUI Launch", "1.3", "Game path auto-detected", [
        "Game path field shows Hades II installation path",
    ]),
    ("GUI Launch", "1.4", "Character dropdown populated", [
        "Create tab > Character dropdown has characters listed",
    ]),

    ("Create", "2.1", "Basic export (mesh + textures)", [
        "Select Melinoe, set mod name + author",
        "Textures ON, animations OFF",
        "Click 'Create Mod Workspace'",
        "Output folder has: GLB, PNG, DDS, manifest.json, mod.json",
    ]),
    ("Create", "2.2", "Export with animations", [
        "Select character, check Animations, set filter 'Idle'",
        "Create workspace",
        "GLB has animation(s) in Blender",
        "manifest.json contains animation hashes",
    ]),
    ("Create", "2.3", "Multi-entry character (Hecate)", [
        "Select Hecate, create workspace",
        "GLB has both HecateBattle and HecateHub meshes",
        "manifest.json has both mesh_entries",
    ]),
    ("Create", "2.4", "Export with mesh entry filter", [
        "Set mesh entries to 'HecateHub_Mesh'",
        "Create workspace",
        "GLB only has Hub meshes",
    ]),

    ("Build", "4.1", "Texture-only mod", [
        "Edit Hecate_Color.png in workspace (paint something visible)",
        "Build tab > browse to workspace, Install to r2modman",
        "Build succeeds, only modified texture(s) in PKG",
        "Launch game via r2modman — texture change visible on Hecate",
    ]),
    ("Build", "4.2", "Mesh add mod", [
        "Use workspace with new mesh added (test 3.2)",
        "Build with r2modman install",
        "Launch game — new mesh visible with custom texture",
    ]),
    ("Build", "4.3", "Mesh edit mod (topology change)", [
        "Use workspace with moved vertices (test 3.3)",
        "Build — topology change detected",
        "Launch game — vertex edits visible",
    ]),
    ("Build", "4.4", "Thunderstore ZIP", [
        "Check 'Also create Thunderstore ZIP'",
        "Build succeeds, ZIP created",
        "ZIP has: mod.json, stripped GLB, .pkg, plugins/, manifest.json",
        "ZIP does NOT contain .gpk",
    ]),

    ("Mods Tab", "5.1", "View installed mods", [
        "After installing a mod, switch to Mods tab",
        "Click Refresh",
        "Mod appears with correct info",
    ]),
    ("Mods Tab", "5.2", "Disable a mod", [
        "Select mod, click Disable",
        "Launch game — mod effect gone",
    ]),
    ("Mods Tab", "5.3", "Remove a mod", [
        "Select mod, click Remove",
        "Mod removed from r2modman",
    ]),
    ("Mods Tab", "5.4", "Edit a mod", [
        "Select mod, click Edit",
        "Workspace folder opens",
    ]),

    ("Multi-Mod", "6.1", "Two texture mods same character", [
        "Install red skin + blue skin for Melinoe",
        "Both appear in Mods tab",
        "Conflict shown (same texture name)",
        "Game shows one (last in priority wins)",
    ]),
    ("Multi-Mod", "6.2", "Two mesh_add mods same character", [
        "Install two additive mesh mods for same character",
        "Click 'Rebuild Merged'",
        "Game shows BOTH meshes",
    ]),
    ("Multi-Mod", "6.3", "Texture + mesh on different characters", [
        "Install texture mod for Melinoe + mesh mod for Moros",
        "No conflict",
        "Both work simultaneously",
    ]),

    ("CLI", "7.1", "cg3h_build.py", [
        "python tools/cg3h_build.py <mod_dir>",
        "Builds successfully",
    ]),
    ("CLI", "7.2", "cg3h_build.py --package", [
        "python tools/cg3h_build.py <mod_dir> --package",
        "Creates Thunderstore ZIP",
    ]),
    ("CLI", "7.3", "mod_merger.py", [
        "python tools/mod_merger.py <r2_dir>",
        "Scans mods, creates cg3h_mod_priority.json",
    ]),

    ("Blender Addon", "8.1", "Import character", [
        "File > Import > Hades II Model (.gpk)",
        "Character imports with armature + meshes",
    ]),
    ("Blender Addon", "8.2", "Export character", [
        "Edit mesh, File > Export > Hades II Model (.gpk)",
        ".gpk file created",
    ]),

    ("Edge Cases", "9.1", "Character with no texture", [
        "Export ClockworkGear or similar",
        "Warning shown, no crash",
    ]),
    ("Edge Cases", "9.2", "Lua texture override (EarthElemental)", [
        "Export EarthElemental with textures",
        "Lua override texture found (EarthElementalTyphon_Color)",
    ]),
    ("Edge Cases", "9.3", "Custom texture > 512x512", [
        "Add 2048x2048 texture in Blender",
        "Build — auto-resized to 512x512, no crash",
    ]),
    ("Edge Cases", "9.4", "Multiple mesh entries (Hecate)", [
        "Export Hecate (both entries)",
        "Edit only Hub meshes",
        "Build — both entries in GPK",
        "Game: Hub modded, Battle original",
    ]),
]

SAVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_results.json')


def load_state():
    if os.path.isfile(SAVE_FILE):
        with open(SAVE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(SAVE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def print_header():
    print("=" * 60)
    print("  CG3H v3.0.0 Manual Test Runner")
    print("=" * 60)
    print()
    print("For each test: perform the steps, then answer:")
    print("  y/yes  = PASS")
    print("  n/no   = FAIL (you'll be asked for details)")
    print("  s/skip = SKIP")
    print("  q/quit = Save and quit (resume with --resume)")
    print()


def run_tests(resume=False):
    state = load_state() if resume else {}
    results = state.get('results', {})
    start_idx = state.get('next_idx', 0) if resume else 0

    print_header()

    if resume and start_idx > 0:
        print(f"Resuming from test {start_idx + 1}/{len(TESTS)}\n")

    for i in range(start_idx, len(TESTS)):
        category, test_id, description, steps = TESTS[i]

        # Skip already completed
        if test_id in results:
            continue

        print(f"\n{'─' * 60}")
        print(f"  [{test_id}] {category}: {description}")
        print(f"{'─' * 60}")
        print()
        for j, step in enumerate(steps, 1):
            print(f"  {j}. {step}")
        print()

        while True:
            answer = input(f"  Result? (y/n/s/q): ").strip().lower()
            if answer in ('y', 'yes'):
                results[test_id] = {'status': 'PASS', 'category': category, 'description': description}
                print("  ✓ PASS")
                break
            elif answer in ('n', 'no'):
                reason = input("  What went wrong? > ").strip()
                results[test_id] = {'status': 'FAIL', 'category': category, 'description': description, 'reason': reason}
                print(f"  ✗ FAIL: {reason}")
                break
            elif answer in ('s', 'skip'):
                results[test_id] = {'status': 'SKIP', 'category': category, 'description': description}
                print("  — SKIP")
                break
            elif answer in ('q', 'quit'):
                state['results'] = results
                state['next_idx'] = i
                save_state(state)
                print(f"\n  Saved progress ({i}/{len(TESTS)} tests). Resume with --resume")
                return
            else:
                print("  Enter y, n, s, or q")

        # Auto-save after each test
        state['results'] = results
        state['next_idx'] = i + 1
        save_state(state)

    # Generate report
    generate_report(results)


def generate_report(results):
    print("\n")
    print("=" * 60)
    print("  TEST REPORT — CG3H v3.0.0")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    categories = {}
    for test_id, info in results.items():
        cat = info['category']
        categories.setdefault(cat, []).append((test_id, info))

    total_pass = total_fail = total_skip = 0

    for cat in dict.fromkeys(t[0] for t in TESTS):
        tests_in_cat = categories.get(cat, [])
        if not tests_in_cat:
            continue

        print(f"\n  {cat}:")
        for test_id, info in sorted(tests_in_cat):
            status = info['status']
            icon = {'PASS': '✓', 'FAIL': '✗', 'SKIP': '—'}[status]
            line = f"    {icon} [{test_id}] {info['description']}"
            if status == 'FAIL':
                line += f"  ← {info.get('reason', '')}"
                total_fail += 1
            elif status == 'PASS':
                total_pass += 1
            else:
                total_skip += 1
            print(line)

    total = total_pass + total_fail + total_skip
    print(f"\n{'─' * 60}")
    print(f"  TOTAL: {total_pass} passed, {total_fail} failed, {total_skip} skipped / {total}")

    if total_fail > 0:
        print(f"\n  FAILURES:")
        for test_id, info in sorted(results.items()):
            if info['status'] == 'FAIL':
                print(f"    [{test_id}] {info['description']}: {info.get('reason', '')}")

    print(f"\n  Result: {'READY FOR RELEASE' if total_fail == 0 else 'NEEDS FIXES'}")
    print()

    # Save report to file
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_report.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(f"CG3H v3.0.0 Test Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(f"Pass: {total_pass}  Fail: {total_fail}  Skip: {total_skip}  Total: {total}\n\n")
        for test_id, info in sorted(results.items()):
            status = info['status']
            line = f"[{status:4s}] [{test_id}] {info['description']}"
            if status == 'FAIL':
                line += f" — {info.get('reason', '')}"
            f.write(line + '\n')
    print(f"  Report saved: {report_path}")


if __name__ == '__main__':
    resume = '--resume' in sys.argv
    run_tests(resume=resume)

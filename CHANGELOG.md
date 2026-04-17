# Changelog

All notable changes to CG3H are documented here.

---

## v3.9.0

Outfit switching.  Install multiple `mesh_replace` mods on the same character and pick between them in-game from the mod manager dropdown — swap happens instantly, no rebuild, no restart.  Install `mesh_add` accessories and toggle each one individually; the checkbox flips visibility within the same frame.

### Added

- **Per-mod outfit picker** — the mod manager's Characters tab shows a **Body** dropdown per scene entry (Hub, Battle, etc.) listing every pure `mesh_replace` mod installed for that character.  Picking one swaps the entry instantly.  Backed by `rom.data.swap_to_variant(stock_entry, variant_entry)` which installs a hash remap at `[sDraw3DCmds+0x28]` so DoDraw3D reads the variant's `GrannyMeshData` instead of the stock entry's.
- **Variant textures** — variants render with their own textures automatically via `rom.data.populate_entry_textures(entry)` which mirrors `ModelAnimation::PrepDraw` by calling `GameAssetManager::GetTexture` to fill `GMD+0x44`.  No vcount/topology limit; modded materials survive.
- **True-stock slim variants** — the builder emits a `{Character}_Stock_V{N}_Mesh` entry per targeted scene entry containing true-stock bytes from the game's GPK.  Auto-applied on the first ImGui frame so the game opens on vanilla content by default rather than on the merged-all view.  User picks persist via `active_variants` in `cg3h_mod_state.json`.
- **Apply to all scenes** — a cascade dropdown picks one mod and applies it across every scene entry the mod covers.
- **Instant accessory toggle** — the Characters tab gains an **Accessories** section with a checkbox per installed `mesh_add` mod.  Toggling hides/shows the mod's contribution within one frame via `rom.data.set_mesh_visible(entry, mesh_name, visible)`, which flips the target mesh's `GMD+0x4C` (mesh_type) between the original value and `2` (shadow) — DoDraw3D's own switch treats type 2 as skip-to-next-iteration, so no cmdDrawIndexed fires.
- **Blender addon auto-classifies mod type** — exporting from Blender now sets `type` to `mesh_add`, `mesh_replace`, or `["mesh_add", "mesh_replace"]` based on whether the selected meshes match stock entry names (tracked via `cg3h_original_meshes`).  Previously hardcoded to `mesh_replace` regardless.
- **`new_mesh_routing` always written** — was previously omitted when a new mesh went to every entry; runtime mesh-visibility needs the routing to locate meshes by (entry, mesh_name) so it's now always emitted.
- **Static pool capacity raised** (Hell2Modding) — `sgg::addShaderEffect`'s vertex-pool allocation bumped from 64 MB → 128 MB, and `sgg::gStaticIndexBuffers` from 32 MB → 64 MB, via two mov-imm32 byte patches at DLL attach.  Both pools live in the DX12 upload heap (system RAM, not VRAM) so the extra capacity is cheap.  Without this, mods that add variant entries overflow the default budget and weapons/enemies fall back to the `Blank_Mesh` placeholder.
- **Patch-status summary log** — plugin init now logs each CG3H patch as `[OK  ]` or `[SKIP]` with a follow-up warning if any skipped — game updates that shift byte patterns surface immediately.
- **Runtime offset sanity canary** — `rom.data.sanity_check_gmd("HecateHub_Mesh")` validates the hardcoded `GrannyMeshData` layout on first frame.  Logs a single `LOG(ERROR)` if the GMD stride, mesh count, or per-mesh fields drift from expected, instead of failing in mystery ways downstream.
- **Pool-usage inspection** — `rom.data.dump_pool_stats()` walks `gStaticDrawBuffers` and logs per-shader-effect cursor + capacity.  Exposed via a "Pool stats" button in the mod manager for diagnosing OOM-ish regressions.
- **Offset registry** in `docs/rendering_pipeline.md` Appendix A — single table of every struct offset CG3H/H2M depends on, plus byte patterns for the H2M patches.

### Changed

- **Default render is now true-stock** — v3.8 rendered the merged-all-mods view by default; v3.9 auto-applies the `"stock"` variant on first frame so the game opens looking vanilla and the user opts IN to each mod via the picker.  Persisted picks survive restart.
- **Mod classification rule** — a mod is a picker variant only if it's **pure** `mesh_replace` (no `mesh_add` in its `type` list).  Anything with `mesh_add`, including mixed `["mesh_add", "mesh_replace"]`, becomes an always-on additive accessory.  v4.x may revisit with GLB-level splitting (treat the replace half as a variant while keeping the add half additive) but that's not built.
- **Blender addon minimum version** — bumped to 4.2 (current LTS) from 4.0.  4.2 is our verified baseline; 4.0/4.1 users now get a clear install-time error instead of mysterious runtime failures.

### Fixed

- **Blender addon "partially initialized cg3h" ImportError on Python 3.12+** — the top-level `importlib.reload(cg3h_core)` ran while the parent package's `__init__` was still executing.  Python 3.12 tightened parent-package state checks and rejects this.  Switched to the Blender-standard `"bpy in locals"` guard — plain import on first load, reload only on addon disable-then-enable.
- **Weapons/enemies rendering as placeholder when variants installed** — static vertex/index pools exhausted by the extra entries (see pool patches above).
- **Duplicate `plugins_data/CG3HBuilder/` write path** — the Python builder's dev-only invocation (`python cg3h_builder_entry.py ...`) wrote to `<plugins_data>/CG3HBuilder/` while the deployed exe writes to `<plugins_data>/Enderclem-CG3HBuilder/`.  H2M's startup GPK scan picked up both → duplicate registrations + "Could not open Granny Packfile" errors.  Dev path now writes to `_cg3h_devbuild/` (not scanned).
- **`dump_pool_stats` reported impossible "%used"** — was multiplying cursor by `gShaderEffects+0x95c` (a bookkeeping counter) instead of the physical vertex stride.  Now reports raw cursor + capacity plus an estimate at 40 B/vert (the character-mesh stride).
- **v3.9 classification fix: mesh_add is additive, not variant-worthy** — an earlier draft treated mixed `mesh_add + mesh_replace` mods (HecateBiMod-style) as picker variants.  Corrected: any presence of `mesh_add` forces additive.

### Known limitations

- **Re-enabling an accessory mid-session** that was disabled at the last build still requires a rebuild.  The runtime mesh-gate hides meshes that ARE in the merged GPK, but can't show meshes that aren't there.  Toggle-within-a-session (where the mod was enabled at build time) is instant.
- **Sanity canary hardcodes `HecateHub_Mesh`** as the probe entry.  Benign warning in the log if Hecate isn't installed / available; no runtime impact.
- **Shadow-cast code cave fails open on game updates** that shift the `cmp byte [r10+0x2d], 0; je` byte sequence.  Detected via signature check; without the cave, hash remap doesn't apply to the shadow-cast pass (visible as stock shadow on a modded body).  Not fatal.
- **Stock variant bytes are raw stock clones**, not re-serialized — design choice.  We have `reserialize_stock_entry` (golden-path `build_gr2_bytes`) for future work, but the current ship uses raw byte-clones under the `{Character}_Stock_V{N}_Mesh` naming convention the engine's skeleton linkage expects.  Works; nothing to fix.

### Architecture notes

The v3.9 draw-path RE + offset registry is in `docs/rendering_pipeline.md`.  Key v3.9 points:

- The drawable's bone palette / per-mesh scratch is sized at scene-load time for the entry currently bound to it.  Swapping to a LARGER entry at runtime overflows.  This is why the builder merges all mods into the default stock entry (keeps the drawable pre-sized for max) and emits per-mod variants as strictly-smaller slim entries.
- PrepDraw only populates `GMD+0x44` (texture handle) for entries the active scene is actively rendering.  Variants aren't walked by PrepDraw, so we pre-populate them via `populate_entry_textures` before installing the remap.
- Mesh-type 2 in the GMD's `+0x4C` byte routes DoDraw3D's per-mesh switch to the loop-end label.  That's the skip mechanism used by `set_mesh_visible`.

---

## v3.8.0

Instant in-game mesh visibility toggle.  Enable/disable mods from the mod manager and see the result immediately — no rebuild, no restart.  Built on a new `rom.data.set_draw_visible` H2M API that hooks the game's draw dispatch to suppress draw calls per model entry.

### Added

- **Instant toggle** — the mod manager checkbox now hides/shows character meshes in real time.  Internally calls `rom.data.set_draw_visible(entry_name, visible)` per mesh entry.  The change takes effect on the next frame; no GPK rebuild, no game restart, no data mutation.
- **Full shadow suppression** — toggling a character off also suppresses its shadow.  DoDraw3D, DoDrawShadow3D, and DoDraw3DThumbnail are covered by detour hooks on param4 (HashGuid).  DoDrawShadowCast3D (different signature, no hash param) is covered by a manual code cave patched into the draw dispatch via VirtualProtect — the hash is read directly from the draw entry at `[r10+0x28]`.
- **Fallback path** — on older H2M builds without the draw hooks, the toggle gracefully falls back to the v3.7 rebuild + restart flow.
- **`rom.data.set_draw_visible` H2M binding** — new Lua API in a standalone `draw.cpp` module (`lua::hades::draw` namespace).  Generic framework feature, not CG3H-specific.  Designed for upstream merge into Hell2Modding.
- **Builder GPK gating** — `register_gpks()` now checks `is_enabled()` before registering a character's GPK redirect.  Disabled mods no longer load their modded meshes at startup; the stock model appears instead.

### Fixed

- **Checkbox stuck on** — `state.is_enabled(mod.id) or true` always evaluated to `true` due to Lua operator precedence (`and` binds tighter than `or`).  Replaced with `state.is_enabled(mod.id)`.  The checkbox now correctly reflects the persisted enable/disable state.
- **Hash system timing** — `HashGuid::Lookup` returns 0 before the first scene loads.  Startup `apply_visibility()` was removed (it would insert hash 0 for every entry).  Visibility is now only applied at toggle time, when the hash system is guaranteed active.

### Changed

- **H2M draw module** — all draw-call hook code lives in `src/lua_extensions/bindings/hades/draw.cpp` / `draw.hpp`, registered via `lua_manager_extension.cpp`.  Removed from `data.cpp`.  Namespace: `lua::hades::draw`.
- **Lua API rename** — `rom.data.set_entry_visible` renamed to `rom.data.set_draw_visible` for the generic H2M API.
- **Banner text** — "live" toggle outcome shows "toggled" instead of "reloaded" since no reload occurs.

### Architecture

The draw dispatch loop (6336-byte function at `DoDraw3D + 0x148E0`) iterates an array of draw entries.  Each entry has a HashGuid at `[+0x28]` and variant flags at `[+0x2C..0x2E]` that select one of four draw functions.  Three share the signature `(const vector<RenderMesh*>&, uint, int, HashGuid)` and are hooked with standard detour hooks.  The fourth (`DoDrawShadowCast3D`) has a different signature without HashGuid, but the hash is still in the draw entry.  SafetyHook mid-hooks fail in this dispatch area (instruction relocation issues with short conditional jumps), so we patch the 7-byte shadow-flag check (`cmp byte [r10+0x2d], 0; je`) with a `jmp` to an allocated code cave that reads the hash, calls `is_hash_hidden()`, and either skips to loop-next or replays the original instructions.

### Why

v3.7 added the mod manager UI but every toggle required a full GPK rebuild + game restart.  v3.8 makes the toggle instant by intercepting the render pipeline.  This is the foundation for v3.9 outfit switching — the same code cave can be extended from a boolean visibility gate to a hash remap that swaps which mModelData entry draws.

---

## v3.7.0

In-game mod manager (read-only), plus a pile of Hell2Modding compliance fixes discovered while shipping it. First in-game UI surface for CG3H, built on the H2M ImGui binding. The existing runtime plugin is split into modular layers so future UI changes only touch one file.

### Added

- **In-game mod manager** — press the H2M GUI toggle (INSERT), open the `CG3HBuilder → Mod Manager` menu. Three tabs:
  - **Characters** — collapsing header per character with build state, GPK path, last-build time, error (if failed), and the mod list.
  - **Mods** — flat table of every installed CG3H mod (name, author, version, character, status).
  - **Summary** — totals, builder version, last build timestamp.
  - A **Refresh** button re-reads `cg3h_status.json` and re-scans `plugins_data/` without touching the builder.
- **`cg3h_status.json`** — the runtime builder now writes `{builder_dir}/cg3h_status.json` at the end of every `--scan-all` pass. Schema version 1, captures per-character state (`built` / `cached` / `failed`), mod list + metadata, GPK path, error, duration_ms. Best-effort write; failures never block the build.
- **Modular CG3HBuilder plugin** — the single 190-line `main.lua` is now split into:
  - `main.lua` — path discovery, module loading, glue
  - `mod_state.lua` — pure data layer: scan, parse, group, load status JSON
  - `runtime.lua` — all game-facing side effects: texture loading, builder invocation, GPK registration
  - `ui.lua` — ImGui rendering only; reads `mod_state` and never touches the filesystem
  - The hard rule: the UI reads the state table and nothing else. Swap `ui.lua` freely; everything else stays put.
- **Version centralization** — `tools/cg3h_constants.py::CG3H_VERSION` is now the single source of truth for the release version and plugin folder name. `CG3H_BUILDER_FOLDER`, `CG3H_BUILDER_DEPENDENCY`, and the builder's `cg3h_status.json` all derive from it. A new `test_version_consistency` enforces that `.github/thunderstore/manifest.json` stays in sync (111 tests total, all passing).
- **`tools/install_plugin_local.py`** — developer-facing local installer. Rebuilds `cg3h_builder.exe` via PyInstaller, wipes and recreates `plugins/Enderclem-CG3HBuilder/` and `plugins_data/Enderclem-CG3HBuilder/` in the default r2modman profile. Used for testing without going through CI.

### Fixed (Hell2Modding compliance)

- **Plugin folder layout** — H2M's `lua_manager.cpp:89` requires the folder containing `main.lua` to have exactly one hyphen (the `AuthorName-ModName` convention). The pre-v3.7 ZIP shipped files under `plugins/CG3HBuilder/`, which r2modman placed at `plugins/Enderclem-CG3HBuilder/CG3HBuilder/` — the innermost `CG3HBuilder` folder has zero hyphens, so H2M logged `Bad folder name` and the plugin loaded in a weird half-registered state. v3.7 ships files directly at the ZIP's `plugins/` root; r2modman now places them at `plugins/Enderclem-CG3HBuilder/` which passes the check.
- **PKG filename GUID check** — H2M's `data.cpp` `LoadPackages` binding requires the `.pkg` filename stem to contain a registered module's GUID, and `TerminateProcess`es the game on mismatch. Data-only CG3H mods (no `main.lua`) aren't registered as H2M modules, so their own GUID can't satisfy the check. `cg3h_build.py` now prefixes PKGs with `{CG3H_BUILDER_FOLDER}-` (i.e. `Enderclem-CG3HBuilder-`) instead of `CG3HBuilder-` — the builder plugin's registered GUID is always in the stem.
- **Legacy PKG migration** — `runtime.lua` detects legacy-named `CG3HBuilder-*.pkg` files from pre-v3.7 builds and renames them in-place to the new `Enderclem-CG3HBuilder-*.pkg` before loading. Mods built before v3.7 no longer hard-crash the game; they get silently upgraded on first launch.
- **`plugins_data_dir` path derivation** — the old `gsub` trick stripped the last two path segments from the derived data folder, which worked for the pre-v3.7 nested layout but returned the wrong (too-high) directory for the flat v3.7 layout. Replaced with a `string.find "plugins_data"` lookup that handles both layouts.
- **`rom.path.get_directories` recursion** — H2M's Lua binding returns directories recursively, not immediate children. The scan loop now filters entries to only include direct children of `plugins_data_dir` and dedupes by `mod.json` path. Previously this produced duplicate "ghost" entries in the mod list.
- **Menu path** — `rom.gui.add_to_menu_bar` auto-wraps the callback inside the plugin's own submenu. Emitting `BeginMenu("CG3H")` inside that callback produced the path `CG3HBuilder → CG3H → Mod Manager`. Now the callback emits the `MenuItem` directly, so the path is `CG3HBuilder → Mod Manager`.

### Changed

- **`cg3h_builder_entry.py`**: `scan_and_build_all()` now records per-character state as it iterates and writes the status JSON before returning. Every exit branch (cached, built, GLB-merge failed, GPK-SDB missing, convert failed) writes a record. Empty-mods case still writes an empty status doc so uninstall-all clears stale state.
- **`.github/workflows/release.yml`**: Thunderstore packaging flattened (`plugins/` at ZIP root, not wrapped in a `CG3HBuilder/` subfolder) and now copies all four Lua modules instead of just `main.lua`.
- **CG3HBuilder plugin manifest** bumped to `3.7.0`.

### Why

Modders and players had no in-game visibility into what CG3H built, what got cached, or why a character failed. You had to read the H2M console at startup and hope you caught the scroll. v3.7 puts that state behind a toggle-able ImGui window you can check whenever. No actions yet — that's v3.8 (enable/disable, rebuild, hot-reload).

The modular split pays for itself the first time we redesign the UI: every iteration on `ui.lua` is a self-contained change, no risk of breaking the runtime.

The H2M compliance fixes surfaced because the v3.0-v3.6 releases shipped a plugin that was technically broken but happened to work anyway. H2M tightened its validators; v3.7 brings us back into compliance.

### Not in v3.7

- Enable/disable individual mods → v3.8
- Trigger rebuild from the UI → v3.8
- Hot-reload after rebuild → v3.8 (RE-gated)

---

## v3.6.0

Texture name deduplication. Closes the v3.2 dedup gap so two mods can ship a custom texture with the same name without one silently overwriting the other.

### Added

- **Custom texture auto-prefix at build time**: `tools/cg3h_build.py` rewrites every `assets.textures[*]` entry with `custom: true` to `{mod_id}_{name}` before baking the per-mod PKG. Idempotent — re-running build does not double-prefix.
- **GLB image rename**: when textures are renamed, `cg3h_build.py` mutates the shipped GLB's image names to match so the runtime converter writes the prefixed name into the GR2 material chain. The modder's source files (`mod.json`, source GLB) are never touched — only the build output under `build/plugins_data/{mod_id}/`.
- **`tools/mod_info.check_conflicts` texture policy split**: custom textures (`custom: true`) shipped by multiple mods now produce an INFO warning ("will be auto-prefixed with mod id at build time") instead of a hard error. `texture_replace` overrides — where two mods overwrite the same game-asset name — remain a hard ERROR.
- **2 new tests** in `test_core.py` covering the v3.6 conflict policy: shared custom texture → INFO, custom + replace at the same name → no false-positive error.

### Changed

- `package_thunderstore()` now reads the GLB from `build_dir/plugins_data/{mod_id}/` first (the v3.6-renamed copy) and falls back to the source only if the build copy is missing — so the prefixed image names land in the Thunderstore zip.
- The build output's `mod.json` is now written from the in-memory mutated mod dict instead of `shutil.copy2`-ing the source, so the texture rename never bleeds back into the modder's repo.

### Why

Mesh names already get auto-prefixed at runtime by `_merge_glbs()` when two `mesh_add` mods collide. Textures could not follow the same pattern because PKGs are baked once at modder build time, not at runtime — a collision would silently let the last-loaded PKG win. v3.6 closes the gap by always-prefixing at build time, making collisions impossible by construction.

---

## v3.5.3

Patch release — documentation accuracy pass.

### Changed

- **`docs/architecture.md`**: removed stale references to `compute_coverage()` (deleted in v3.4) and the bone color overlay (replaced by visibility presets in v3.4). Documents the actual `cg3h_bone_preset` system.
- **`docs/architecture.md`**: rewrote the multi-entry import paragraph — multi-entry shipped in v3.1, no longer "planned".
- **`docs/architecture.md`**: dropped the `v3.1 diff format` future-work entry. Future work table now lists in-game mod manager, skeleton editing, and native animation codec.
- **`docs/architecture.md`**: documented the v3.3 smart change detection system (`_is_mesh_changed`, `.baseline_positions.npz`, normal-split tolerance).
- **`docs/mod_spec.md`**: replaced two outdated "v3.1 diff format will enable CC-free distribution" notes with accurate descriptions of how the runtime CG3HBuilder rebuilds GPKs from local game files.

### Fixed

- No code changes — documentation only.

---

## v3.5.2

Patch release — unified test runner and `mod_info` test coverage.

### Added

- **11 new tests for `tools/mod_info.py`** — covers `group_by_character`, `check_conflicts` (mesh_replace conflict, mesh_add compatibility, texture conflicts, mixed types), `load_priority`/`save_priority` roundtrip, `generate_default_priority`, `scan_cg3h_mods` with format filtering.

### Changed

- **`python tests/test_core.py` now runs the full suite** — `_run_all` discovers tests in `test_coverage.py` automatically. Single command goes from 62 → 108 passing tests (96 from the merged suites + 12 new mod_info tests).

---

## v3.5.1

Patch release — `mod_merger.py` removed.

### Changed

- **`tools/mod_merger.py` deleted** (455 lines). Marked deprecated since v3.0.1. The runtime merging path has been handled by `cg3h_builder_entry.py` since v3.0; the only reason mod_merger lingered was that 6 tests still imported `check_conflicts` and `load_priority`/`save_priority`/`group_by_character`/`scan_cg3h_mods` from it.
- **New `tools/mod_info.py`** carries those still-used helpers (155 lines, no merge logic). Test imports updated.
- **Docs updated** — `architecture.md` and `mod_spec.md` no longer reference `mod_merger.py`. The "multi-mod merger" section now correctly attributes runtime merging to `cg3h_builder_entry.py`.
- **Manual test checklist updated** — removed `7b. mod_merger.py from command line` and the related references in `v3_release_tests.md` and `run_manual_tests.py`.

### Removed

- `tools/mod_merger.py` — deprecated wrapper around the obsolete sequential `convert()` merge path
- `merge_character_mods()`, `merge_all()`, `main()` (CLI entry) — never called from elsewhere

---

## v3.5.0

Quality-of-life cleanup release. No new features or behavior changes — purely code health.

### Changed

- **Imports consolidated** — deferred `import` statements moved to module top across `tools/` and `blender_addon/cg3h/`. Optional dependencies (`lz4`, `pygltflib`, `texture2ddecoder`, `PIL`, `etcpak`, `xxhash`, `winreg`) remain wrapped in `try/except ImportError` patterns.
- **Silent exception handlers documented** — every `except: pass` block now either logs or carries an inline comment explaining why the failure is intentional.
- **Semicolon-chained statements expanded** — GPK/PKG parser loops now use one statement per line.
- **`pyproject.toml` ruff config updated** — codifies the project's ignore rules so future runs are consistent. Excludes `tools/debug/`.

### Removed

- **`tools/debug/` trimmed** from 32 files to 6. Deleted ~25 exhausted reverse-engineering probes (`probe_write_api2-19.py`, hybrid probes, write-API hypothesis tests). Kept `gr2_serializer.py`, `granny_datatree_roundtrip.py`, `probe_golden_path.py`, and the diagnostic helpers as reference material with a new `tools/debug/README.md`.
- **Dead helper `_read_gr2_texture_names()`** in `gr2_to_gltf.py` — referenced from a fallback path that used an undefined name (`fi`); the path was unreachable.
- **Unused local variables** caught by ruff (`character`, `gpk_dir`, `dll_path`, `pkg_dir`, `dds_mips`, `compressed`, `total_sz_off`, XNB header field reads).
- **Unused import** `shutil` in `gpk_pack.py`.
- **F-strings without placeholders** converted to plain strings.

### Fixed

- `converter_gui.py`: `pygltflib` import now wrapped in `try/except` and guarded at use site (was an unconditional `import` that would crash if missing).
- `granny_types.py`: loop variable `struct` no longer shadows the `struct` module import.
- `gltf_to_gr2.py`: removed dead fallback path that referenced undefined `fi`.

### Notes

- 62/62 tests pass, ruff clean on `tools/`, `blender_addon/cg3h/`, and `tests/` (with codified ignores).
- v3.5.x patch lane: bug fixes go to 3.5.1, 3.5.2, etc.; new features wait for v3.6.

---

## v3.4.0

Blender skinning UX — bone color overlay, template toggle, export validation.

### Added

- **Bone color overlay** — in pose/weight-paint mode, armature bones are colored green/yellow/red based on availability for the selected mesh's BoneBindings. Green = available on all sibling meshes; yellow = partial; red = silent fallback to root.
- **Template override dropdown** — sidebar dropdown lets modders pick which existing mesh's BoneBindings their new mesh inherits, instead of relying on automatic selection. Default "Auto (smart)" picks the best match by bone overlap.
- **Export validation** — pre-flight check walks vertex groups before exporting and shows a popup listing weights painted on bones outside the resolved BoneBindings. Lists violations grouped by mesh; user can cancel or export anyway.
- **`bb_names` in manifest** — exporter now stores per-mesh bone binding names. Already extracted at export time but previously discarded.
- **`cg3h_core.py`** — new pure-helper module in `blender_addon/cg3h/` containing `select_template`, `compute_coverage`, `find_weight_violations`. Importable from tests without bpy.
- **Bone coloring kill switch** — `enable_bone_coloring` preference in addon settings.

### Changed

- Blender addon `bl_info["version"]` bumped to (3, 4, 0).
- Manifest reading wired into `CG3H_OT_Import` — manifest JSON cached on the scene as `cg3h_manifest_json` for use by sidebar/export validation.
- `_get_characters` enum callback now caches items at module level (fixes pre-existing Blender enum-callback GC pitfall).
- `gr2_to_gltf.py` manifest construction extracted into `build_manifest()` pure function for testability.

### Fixed

- Pre-existing `_get_characters` enum callback rebuilt its list every call (potential GC crash). Now cached.

---

## v3.4.0

Blender skinning UX — bone visibility presets and pre-export weight validation.

### Added

- **Bone visibility presets** — sidebar dropdown filters the armature view by entry or template. Cycle with arrow buttons or pick directly. Presets:
  - **Whole armature** — every bone in the rig
  - **All routed bones** — union of bones for the active mesh's routed entries
  - **Entry: X** — only bones used by meshes in entry X
  - **Mesh: X** — only the bones in template mesh X
- **Pre-export weight validation** — when you export, a popup lists any vertex weights painted on bones outside the resolved BoneBindings (which would silently fall back to root at engine load). Cancel to fix, or export anyway.
- **Setup for Skinning button** — one-click parent + Armature modifier so a new mesh is immediately weight-paintable.
- **Manifest carries `bb_names`** — each mesh's BoneBindings list is stored in `manifest.json` so the addon and validator know which bones every mesh can use.
- **Skinning guide** — `docs/skinning_guide.md` walks modders through entries, templates, BoneBindings, and the routing/dropdown workflow.

### Changed

- Blender addon `bl_info["version"]` bumped to (3, 4, 0). Minimum Blender version stays at 4.0.
- `_get_characters` enum callback now uses module-level item caching (fixes a pre-existing Blender enum-callback GC pitfall).

### Fixed

- Trailing/empty entries in `cg3h_entries` no longer create malformed `cg3h_entry_` properties on meshes.
- `_read_gpk_entries` decodes filenames with `errors='replace'` and wraps the parser in try/except so a malformed GPK doesn't crash the import dialog.

---

## v3.3.0

Smart mesh change detection — Blender normal-split tolerance and position edit detection.

### Added

- **Baseline position file** — exporter saves `.baseline_positions.npz` alongside manifest for per-vertex comparison at build time
- **Normal-split tolerance** — meshes with extra vertices from Blender's normal splitting are correctly detected as unchanged (unique UV + position count ≤ original)
- **Position edit detection** — same vertex count meshes compared against baseline with 1e-4 tolerance (absorbs Blender ~1e-5 float noise, catches any visible edit ≥ 0.001)
- **`_is_mesh_changed()` function** — shared change detection used by both `_strip_unchanged_data()` and `_sync_mod_json()`, replacing broken per-function checks
- **Manifest vertex metadata** — `vertex_count`, `index_count`, `position_hash` stored per mesh in manifest for change detection

### Changed

- `_strip_unchanged_data()` uses `_is_mesh_changed()` instead of unreliable vertex count comparison
- `_sync_mod_json()` uses same shared function — mod type detection (`mesh_replace` vs `mesh_add`) now accurate
- Original meshes correctly stripped from mod packages when unchanged (previously kept due to Blender normal-split vertex inflation)
- `.baseline_positions.npz` excluded from Thunderstore ZIP packages

### Fixed

- Blender re-export adding 4-200+ vertices per mesh from normal splits no longer triggers false `mesh_replace` tagging
- Stripped mesh count in build output no longer shows negative numbers when new meshes are present

---

## v3.2.0

Multi-mod robustness: name deduplication, animation merge, and conflict detection.

### Added

- **Name deduplication** — when two `mesh_add` mods use the same mesh name, both are kept with `{mod_id}_` prefix (e.g. "ModA_Crown" and "ModB_Crown"). Transparent to mod creators.
- **Animation merge** — `_merge_glbs()` copies animations from all mods with name-based node remapping. Previously only the first mod's animations survived.
- **Animation dedup** — same animation name in multiple mods: last mod wins with warning printed.
- **Mesh name conflict warnings** — `check_conflicts()` warns about shared mesh names across `mesh_add` mods (informational, not blocking).
- **`_copy_accessor()` helper** — shared function for copying binary accessor data between GLBs, used by both mesh and animation merge.
- **Ruff linter** — `pyproject.toml` with ruff config for code quality. Zero warnings on all core files.

### Changed

- `_merge_glbs()` returns `(merged_path, collisions)` instead of just `merged_path`. Collisions set propagated to manifest merge and routing.
- `_merge_manifests()` accepts `collisions` set instead of flat rename map, derives prefixed names per-mod.
- `patch_animation_entries()` filter is now optional (was required). Builder no longer passes filter — GLB contents are sole authority on what gets patched. CLI `--anim-patch-filter` still works as optional pre-filter.
- Routing keys updated per-mod when collisions exist (not a flat rename after the fact).

### Removed

- Import-side animation filter in builder — was redundant with export-side filter.
- Unused variables caught by ruff: `needs_reserialize`, `dim`, `first_id`, `idx_path`.
- Unused import: `STEAM_PATHS` from `cg3h_constants`.

### Known Limitations

- Name deduplication covers meshes only (not textures/images in PKGs — planned for future).
- New mesh bone bindings limited to template mesh's bindings (v4.0).

---

## v3.1.0

Multi-entry character support and improved game path detection.

### Added

- **Multi-entry GPK export** — all mesh entries exported by default (e.g. Hecate: Battle + Hub)
- **Skeleton merge** — bones from all entries merged into unified skeleton for the GLB
- **Multi-entry import** — manifest routes GLB meshes to correct GPK entries, each serialized independently
- **Per-entry mesh routing** — `new_mesh_routing` in mod.json targets new meshes to specific entries (e.g. battle-only accessories)
- **Smart template selection** — new meshes pick the existing mesh with best bone overlap as template
- **Manifest merging** — multi-mod builds merge manifests from all mods
- **Steam auto-detection** — game path found via Windows registry + `libraryfolders.vdf` (supports custom Steam libraries)
- **Stale texture cache fallback** — if texture index misses entries, full .pkg scan with auto-cleanup
- **Blender addon CG3H panel** — sidebar panel for assigning meshes to entries with checkboxes
- **Blender addon auto-build** — export runs `cg3h_build.py` via system Python to produce PKG + Thunderstore ZIP
- **Clear error on missing deps** — `texture2ddecoder` / `Pillow` missing now shows explicit error instead of silent failure
- **requirements.txt** for easy dependency install
- **Cross-platform guards** — `ctypes.windll` wrapped in try/except for non-Windows import compatibility

### Changed

- Export defaults to all `_Mesh` entries instead of body entry only
- GUI Create tab shows entry checkboxes for multi-entry characters
- GUI mod name/author moved to Build tab with persistent author
- `_build_entry_routing()` replaces single-entry body selection
- `load_gr2()` refactored into `load_sdb()` + `load_gr2_entry()` for SDB reuse
- `os.path.abspath` for gpk_dir normalization (fixes mixed slash paths)

### Known Limitations

- New mesh bone bindings limited to template mesh's bindings (v4.0)

---

## v3.0.0

Shared runtime builder, Blender addon, and r2modman integration.

**This is a major workflow shift.** Mods are data-only packages (GLB + textures). The shared CG3HBuilder plugin handles all runtime logic — building GPKs, merging mods, loading textures. Zero game files modified.

### Added

- **CG3HBuilder Thunderstore plugin** — shared runtime dependency for all CG3H mods:
  - Scans `plugins_data/` for installed CG3H mods at game launch
  - Merges multiple mods targeting the same character into one GLB
  - Builds GPKs from GLBs + player's local game files (no Python needed)
  - Registers GPKs via `rom.data.add_granny_file` (no restart needed)
  - Caches built GPKs, rebuilds when mods change, cleans up on mod removal
  - Loads custom textures via biome overrides + LoadPackages
- **Data-only mod packages** — mods contain only assets (GLB, PKG, mod.json)
  - Minimal `plugins/` stub (H2M manifest with CG3HBuilder dependency)
  - No executable or Lua code per mod
- **Auto texture detection** — textures embedded in GLBs are automatically found at build time, extracted, and packaged (only new mesh textures, not original character textures)
- **Blender addon** — self-contained (bundled exporter/importer/builder exes):
  - Import: File > Import > Hades II Model (.gpk) with textures and animations
  - Export: File > Export > Hades II Mod (CG3H) — creates workspace + Thunderstore ZIP
  - Author saved in addon preferences
  - Character auto-detected from selected objects on export
- **GUI overhaul**:
  - Create tab: searchable character list, export to workspace
  - Build tab: mod name + author fields, Thunderstore ZIP, one-click r2modman install
  - Mods tab: CG3HBuilder status, GPK build state, installed mod list
  - Author name persisted across sessions
- **r2modman integration** — install registers mods in `mods.yml`, handles nested directory layout, cache icons
- **Conflict detection** — per-operation analysis:
  - Same texture replaced by multiple mods = conflict
  - Multiple mesh_replace for same character = conflict
  - mesh_add + mesh_add = compatible (merged)
- **Smart data stripping** — Thunderstore ZIPs strip unchanged meshes/textures vs manifest baseline
- **GitHub Actions** — tag `v*` builds CG3HBuilder ZIP, Blender addon ZIP, and tools ZIP
- **Shared constants** — `cg3h_constants.py` for Steam paths and dependency versions
- **PyInstaller specs** — for exporter, importer, and builder executables
- **Name collision warnings** — merger logs when a mesh from a second mod is skipped due to duplicate name
- **All biome texture loading** — custom textures register for all 16 biomes

### Changed

- GUI rewritten from technical tabs to workflow tabs (Create/Build/Mods)
- Mods no longer bundle `cg3h_builder.exe` or `main.lua` — all runtime logic centralized
- Build pipeline auto-detects textures from GLB instead of requiring manual listing

### Removed

- Per-mod `main.lua` generation
- Per-mod `cg3h_builder.exe` bundling
- Backup/restore system (H2M mods are non-destructive)
- Checksum management (standalone packages bypass validation)
- Per-mod `main.lua` generation
- Per-mod `cg3h_builder.exe` bundling
- `mesh_patch` type — folded into `mesh_replace` (true diff patching planned for v3.3)
- `mod_merger.py` merge functions — replaced by CG3HBuilder runtime
- Direct game file modification workflow

### Known Limitations

- **Requires H2M patches** — `rom.data.add_granny_file` API and GPK exact-match fix (PRs pending)

---

## v2.1.0

Custom materials and new texture support for added meshes.

### Added

- **New material support** -- new meshes added in Blender can have their own custom textures
- Material chain creation in DLL memory (granny_material + granny_material_map + granny_texture)
- `FromFileName` hash lookup verified in-game: game finds custom textures via `"GR2/" + basename`
- **Add texture entries to .pkg** -- custom textures inserted as new 0xAD entries (no existing textures replaced)
- Custom texture auto-resize to 512x512 (prevents oversized chunks)
- GLB material/texture extraction: `parse_glb` reads material_name, texture_name per mesh
- `extract_glb_textures` returns embedded PNG bytes by image name
- Unmatched GLB meshes (added in Blender) auto-created as new meshes in the first entry
- Outline/shadow template selection: new meshes named with "Outline" or "ShadowMesh" inherit the correct template material (Mat_Outline / shadow shader)
- `fi->Materials` and `fi->Textures` arrays expanded for new material structs

### Changed

- Custom texture target .pkg chosen from manifest (same pkg as character's existing textures) instead of hardcoded Fx.pkg
- `_create_new_mesh` accepts `material_ptr` parameter to override template MaterialBindings
- Outline/shadow meshes skip custom material creation (use template's shader material)

### Known Issues

- Custom outline meshes need manual vertex offset (~0.6 units via Alt+S in Blender) to produce correct silhouette effect. Game's outline meshes are pre-authored with different topology, not simple duplicates.
- Custom texture image in Blender should be named meaningfully (not "Image_0") as the name becomes the .pkg entry key

---

## v2.0.0

Full end-to-end texture pipeline, multi-entry GPK support, unified install workflow, and animation fixes.

**Breaking changes:** Export directory structure changed (per-character folders with manifest). Old exports without `manifest.json` are not compatible with the new Install tab. Re-export your characters to use the new pipeline.

### Added

- **Texture import pipeline** -- edit textures in Blender, Photoshop/GIMP (PNG), or paint.net (DDS) and install them back into the game
- **PNG -> DDS compression** with mipmaps via `etcpak` (BC7/BC3/BC1 output matching original format)
- **DDS auto-truncation** for extra mip levels (paint.net compatibility)
- **Multi-PKG texture replacement** -- textures duplicated across biome packages (BiomeF, BiomeHub, BiomeIHouse, etc.) are ALL replaced
- **XXH64 checksum validation** -- game checks `checksums.txt`; tool updates it after every replacement and restores on uninstall
- **Lua GrannyTexture override parsing** -- 48 entities override their texture at runtime via game `Scripts/`; exporter respects these
- **Variant textures** (e.g. HecateEM_Color for Extreme Measures) exported as standalone files, marked in manifest
- **Texture index** (`_texture_index.json`) for fast batch lookups, auto-built on first use
- **Export manifest** (`manifest.json`) per character -- tracks mesh entries, per-mesh GR2 index mapping, texture metadata (pkg source, format, dimensions, mip count, png_hash), variant flags, and full pkgs list
- **Multi-entry GPK support** -- characters with multiple mesh entries (e.g. Hecate: HecateBattle_Mesh + HecateHub_Mesh) export ALL entries by default
- `--mesh-entry` flag to filter specific entries, `--list-entries` to inspect
- Importer routes GLB meshes to correct GR2 entry via manifest
- Each entry patched and serialized separately, packed into single output GPK
- **Unified Install tab** in GUI -- browse to export folder, reads manifest, imports mesh + installs textures in one click
- **Mod registry** (`_mods.json`) -- tracks all changes per character (mesh + texture PKGs + checksums)
- **One-click uninstall** -- restores ALL files (GPK + PKGs + checksums) at once; multi-mod safe
- **GLB texture extraction** with hash-based change detection (png_hash comparison)
- **Animation gap-fill** -- if a bone has any animated channel, emit all three (pos + rot + scale) with rest pose constants; prevents Blender bogus scale on partially-animated bones
- **_static suffix stripping** for animation track-to-bone matching
- **bone_index field** preserved in animation track data
- **Parallel animation extraction** with `multiprocessing.Pool` and `--anim-workers` flag
- **25 unit tests** (`tests/test_core.py`)
- **Manual test checklist** (`tests/TEST_CHECKLIST.md`)
- New dependencies: `etcpak` (BC7 compression), `xxhash` (checksum validation)
- Per-character export directories: `Character/Character.glb` + DDS + PNG + manifest

### Changed

- GUI restructured from 3 tabs (Export/Import/Install) to 2 tabs (Export/Install); Install tab now handles both mesh import and texture installation
- Install runs in background thread (no UI freeze)
- GUI auto-scales outer/inner parallelism based on batch size for animation export
- Non-blocking subprocess stdout via reader threads (fixes UI stalling during batch export)
- Duplicate mesh names now get `_2`, `_3` suffixes (not `_LOD1`, `_LOD2`) -- same-name meshes are split parts, not LODs
- Importer strips both new `_2`/`_3` and legacy `_LOD` suffixes
- "Export all LODs" checkbox removed (all parts exported by default)
- Standalone PNG files saved alongside DDS for Photoshop/GIMP editing path
- Multi-texture per character: each mesh gets its own texture via material chain walking

### Fixed

- **PKG 0xAA (Texture3D) parsing** -- size field is big-endian (was misread as little-endian, causing scanner to skip rest of chunk); ALL character textures now found
- **Animation scale channels** -- skip if 3x3 matrix has significant off-diagonal (shear/rotation, not pure scale); skip NaN/Inf/extreme values (>100)
- **Animation rotation channels** -- normalize quaternions to unit length (non-unit causes mesh stretching)
- **Animation translation channels** -- skip NaN/Inf and extreme values (>1000)
- **UTF-8 encoding** on all subprocess stdout (fixes cp1252 crash on Windows)

### Known Issues

- `_static` suffix on animation tracks: mechanism not fully understood
- ErymanthianBoar spine bones have no animation tracks in Idle (may be correct for that clip)
- Some characters have textures referenced in GR2 but not shipped in any `.pkg` (e.g. ClockworkGear's gear_diffuse)

---

## v1.2.0

### Added

- Add new meshes to characters (accessories, armor, custom geometry) via `--allow-topology-change`
- New meshes added to Model MeshBindings so the game renders them
- Body mesh used as template for new meshes (not outline mesh)

### Fixed

- Bone index remapping for new meshes

---

## v1.1.0

### Added

- Animation export (`--animations` flag) with curve decoding and post-processing
- Animation import (`--patch-animations` flag) with in-place DaK8uC8u/DaK16uC16u curve patching
- `--anim-patch-filter` to prevent mass animation re-encoding
- Animation patch filter field in GUI import tab
- Progress output during animation export

### Fixed

- Flush animation progress output for GUI visibility

---

## v1.0.0

Initial release.

### Added

- GPK format parsing (Format B, no `uncompressed_size` field)
- `granny_types.py` -- dynamic Granny struct offset resolver
- `gr2_to_gltf.py` -- export any character to `.glb` (skinned + rigid meshes)
- `gltf_to_gr2.py` -- import edited `.glb` back to `.gpk` (golden path serialization)
- `gpk_pack.py` -- GPK archive pack/unpack
- `converter_gui.py` -- GUI with Export, Import, and Install tabs
- Blender addon (`blender_addon/cg3h/`) for File > Import/Export
- 40-byte physical GPU stride override (engine invariant)
- Index extraction via `GrannyCopyMeshIndices` (handles 16-bit and 32-bit)
- Bone weight remapping from Blender joint order to GR2 BoneBinding order
- Index buffer patching (triangle/face edits)
- Variable vertex count support (`--allow-topology-change`)
- Blender `.001` duplicate suffix stripping
- uint16 vertex count limit validation (65,535 max)
- Backup and restore of original files before installing
- Bounding box (OBB) recomputation for frustum culling

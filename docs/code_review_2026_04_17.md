# CG3H + H2M Code Review — 2026-04-17

Scope: ~19k LOC across `GhidraHades2/tools/*.py`, `GhidraHades2/.github/thunderstore/*.lua`, `GhidraHades2/blender_addon/**`, and the H2M C++ files touched by CG3H. Reviewed in five parallel passes then consolidated here. **All [ERR] findings have been verified against the source; [LOGIC] / [UNDOC] / [HARDCODED] are flagged for follow-up but weren't individually proven.**

Severity legend: `[ERR]` proven bug · `[LOGIC]` plausible logic issue · `[UNDOC]` implicit contract · `[HARDCODED]` magic value / path.

---

## Confirmed bugs (verified line-by-line)

### `tools/cg3h_builder_entry.py`
- **[ERR] `all_variants` dict declared but never populated (line 774 vs 1107–1117)** — emits `cg3h_variants.json` only if `all_variants` is truthy, but nothing ever assigns into it. The file is never written. **Functional impact: none today** because the runtime reads the same data from `cg3h_status.json` (`status_characters[char]["variants"]`). Dead code from an earlier refactor. **Action: remove the dead block, or assign into `all_variants` inside the build loop for API symmetry.**

### `tools/pdb_lookup.py`
- **[ERR] Duplicate execution block at lines 216–234** — the entire "resolve names + optional disassembly" loop runs twice because the first loop at 182–214 doesn't return before falling into the second copy. Every symbol lookup prints twice. **Functional impact: cosmetic — confused me earlier when Hades2 symbols appeared doubled in output.** **Action: delete lines 216–234.**

### `blender_addon/cg3h/__init__.py`
- **[ERR] Empty-string split at line 705** — `original_meshes = set(context.scene.get("cg3h_original_meshes", "").split(","))` returns `set([''])` when the scene prop is missing, not `set()`. Every mesh object then falls through to the `has_add` path (correct for exports from a clean scene). **Impact: works only by accident when the prop is unset.** **Action: guard — `original_meshes = set(x for x in raw.split(",") if x)`.**

### `tools/cg3h_builder_entry.py`
- **[LOGIC-verified] `_variants_map_for` on cache-hit re-filters with a DIFFERENT condition than the build path** — build uses `is_pure_replacer AND has_entries`; cache-hit uses `'mesh_replace' in types and mesh_entries` (no `'mesh_add' not in types` guard). A mixed-type mod gets NO variant emission during a fresh build but GETS a variants entry in status JSON on subsequent cache hits. Run only fires on a cache-hit session that happens after the classification rule landed mid-session. **Action: share one classifier fn between both paths.**

---

## Likely issues (high confidence, not individually verified)

### `blender_addon/cg3h/__init__.py`
- **[LOGIC] Legacy PKG rename race at 49–63** — `os.rename` with no destination check; second run while the file is in use loses atomicity.
- **[HARDCODED] Fallback Steam paths are Windows C:/D: only (79–82)** — Linux/Mac non-discoverable.
- **[HARDCODED] glTF export hardcodes `export_yup=True`, no normals/tangents (691–694)** — incompatible with rigs that ship their own tangent data.
- **[HARDCODED] `_read_gpk_entries` parses GPK bytes inline with magic offsets (949–972)** — duplicates `tools/gpk_pack.py` logic; silently diverges if the format ever changes.

### `.github/thunderstore/runtime.lua`
- **[LOGIC] `swap_entry` checks `rom.data.swap_to_variant` availability but not `rom.data.restore_stock`** (lines 193 vs 205). Nil deref on older H2M builds that have one but not the other.
- **[HARDCODED] `PKG_PREFIX = "Enderclem-CG3HBuilder"` (line 20)** — H2M's filename-GUID check hard-crashes (TerminateProcess) if a PKG's stem doesn't contain a registered module GUID. Rename-the-plugin-prefix = brick. Comment acknowledges; no guard.
- **[HARDCODED] `ALL_BIOMES` list (lines 24–28)** — game update that adds a biome → textures don't override there. Silent regression.

### `.github/thunderstore/mod_state.lua`
- **[LOGIC] Type-parsing fallback pattern returns empty string (lines 136–141)** — mod.json missing a `type` field yields `type_block = ""`, then `has_mesh_add = false` / `has_mesh_replace = false`. The mod is classified as neither variant nor accessory and merges into stock without triggering any picker UI. Safe default but not logged.
- **[HARDCODED] Scan exclusion list hardcodes `"build"` (line 127)** — a mod author who names their dir `build` gets silently skipped.

### `.github/thunderstore/ui.lua`
- **[LOGIC] `on_first_frame` is called on every ImGui frame until `did_init_variants` flips (lines 158–168)**. If a future Lua reload re-enters `ui.init` while keeping `did_init_variants = true`, apply-active will never re-run. Minor.
- **[HARDCODED] Scene-label derivation `char .. "(.-)_Mesh"` (line 153)** — entries that don't match the pattern fall back to the full entry name. Works but opaque.

### `tools/gltf_to_gr2.py`
- **[LOGIC] `_keepalive.clear()` ordering (line 1936)** — cleared before `GrannyFreeFile(sdb_file)`; if the DLL retains a pointer into buffers past `GrannyEndFileToWriter`, GC could reclaim mid-read. Defensive fix: clear after all `GrannyFreeFile`.
- **[HARDCODED] Material chain offsets (0x54, 0x0C, 0x08, 0x14 at lines 1341/1350/1677/1720)** — not derived from the runtime type map like elsewhere in the file. Granny SDK update → silent corruption.
- **[LOGIC] New-mesh material chain: no null check before dereferencing `maps_ptr` (line 1719)** — garbage `custom_mat_ptr` would crash, but Python-side traceback is the symptom, not a corrupt GR2.

### `tools/gr2_to_gltf.py`
- **[HARDCODED] `src_stride < 40` rigid-vs-skinned heuristic (lines 420–423)** — future format with a 36-byte vertex gets misclassified. Should compare against known primitives, not a literal.

### `tools/gpk_pack.py`
- **[HARDCODED] `uncompressed_size=128 * 1024 * 1024` sentinel (line 51)** — a GR2 entry >128 MB decompresses to partial data silently. Unlikely for character meshes but worth documenting. *Clean otherwise.*

### `tools/pkg_texture.py`
- *(Agent flagged `_swap32` as bugged — **I traced it for 0x12345678 → 0x78563412, the code is correct**. Disregard.)*
- **[LOGIC] DDS truncation path at 954–964** doesn't align to mip boundaries on shrink; corrupted mip chain if a replacement happens to be smaller than the original.
- **[HARDCODED] Fixed target `Fx.pkg` (line 555)** — texture install assumes it.
- **[HARDCODED] XNB version byte `0x06` (line 662)** — undocumented; game update breaks silently.

### `tools/converter_gui.py`
- **[LOGIC] Subprocess stdout handling** streams line-by-line; works until a child writes a huge non-newlined payload and fills the pipe.
- **[HARDCODED] `DEFAULT_OUTPUT` assumes `~/Documents/` (line 43)** — OneDrive-redirected Documents folder lands in a non-obvious place.

### `Hell2Modding/src/main.cpp`
- **[LOGIC] Pool-size patches fail-open (~line 2407 / 2429)** — `gmAddress::scan` miss → `LOG(WARNING)`, continues boot. Mods that depend on the raised cap silently OOM on the upload heap when a game update shifts the byte pattern. **Action: on the scan miss, at least `LOG(ERROR)` and consider flagging into the mod-manager UI so users see why weapons regressed.**
- **[HARDCODED] `.offset(8)` from the scan point (2407, 2429)** — assumes the exact instruction encoding layout. Document the pattern with the decoded instruction alongside.

### `Hell2Modding/src/lua_extensions/bindings/hades/draw.cpp`
- **[LOGIC] `g_saved_mesh_type` keyed by `uintptr_t gmd` pointer** — if the engine ever reallocs the GMD vector (it doesn't in Hades II's static load model, but not guaranteed), saved entries point at freed memory and the "show" path writes a byte into wherever that memory now belongs. **Low practical risk today** because GMD vectors aren't mutated post-`LoadAllModelAndAnimationData`, but brittle. **Action: re-key by `(entry_hash, mesh_hash, index_within_entry)`.** Cleared on nothing — the map grows with every toggle. Bound is low (one entry per hidden mesh ever); fine for session lifetime.
- **[LOGIC] `hook_DoDraw3D` releases the shared_lock (line 154) before calling the original.** If the Lua thread sets a new remap for the same hash between release and the tail call, the original draws with the old target. Hades II's Lua is serialised with the render thread in practice, but the architecture isn't enforcing it.
- **[LOGIC] Shadow-cast code cave's VirtualAlloc retry loop (lines 220–228)** silently gives up after ±2 GB if no free page is found. Ships a half-patched hook state (other hooks installed, cave missing). Current CG3H ship has the cave failing-open post-game-update anyway — worth a pre-flight log line summarising which hooks are and aren't live.

### `Hell2Modding/src/lua_extensions/bindings/hades/data.cpp`
- **[LOGIC-CRITICAL] vtable-hijack on the `FileStream` object (~lines 251–252)** is not guarded — `hook_PlatformOpenFile` writes a new vtable while another thread may be mid-call into the original. The author left a TODO acknowledging this exact race at line 246. Not a CG3H-introduced bug; worth upstreaming a fix.
- **[LOGIC] `g_current_file_stream` is a plain static written by two hooks, read by a third.** Two parallel opens (unlikely but possible under LoadTaskScheduler) race. Again pre-existing.

### Others (brief)
- `tools/cg3h_constants.py` — hardcoded Steam paths for common locations; safe but monoculture. Registry fallback is fine.
- `tools/mod_info.py` — **clean**.
- `tools/install_plugin_local.py` — assumes `%APPDATA%\r2modmanPlus-local\...`; no env-var override.
- `tools/clone_gpk_entry.py` — **clean** (one-shot RE helper).
- `tools/task26_stock_vs_modded.py` — **by design** user-specific; it's a throwaway.
- `blender_addon/cg3h/cg3h_core.py` — `select_template` tie-break is unstable but low consequence.
- `.github/thunderstore/main.lua` — **clean**.
- `Hell2Modding/src/lua_extensions/bindings/hades/audio.cpp` — the author's own TODO calls out the thread-safety issue on `g_fixed_path_fsAppendPathComponent`.
- `.../gpk.cpp`, `.../inputs.cpp`, `.../tethers.cpp` — **clean** for CG3H purposes.

---

## Cross-cutting observations

1. **Pattern scans with no hard-fail.** Both H2M's pool patches and `main.cpp`'s array extenders log a warning and move on when `gmAddress::scan` misses. That's the right default, but a plugin-level "patch status" summary (list of patches applied / skipped) would save us ten minutes of log archaeology next time a game update ships.

2. **Hardcoded struct offsets are the single biggest post-update fragility.** GR2 material chain (`0x54, 0x0C, 0x08, 0x14`), GMD stride/fields (`0x38, 0x40, 0x44, 0x48, 0x4C`), ForgeGeometryBuffers (`+0x20, +0x40`), Buffer (`+0x38`). These are survived by design through PDB symbol resolution of the function addresses, but the offsets inside are raw. A single validation pass at plugin-init time (read a known entry, assert expected structure) would detect game-update drift immediately instead of silently.

3. **Two distinct classifier implementations for mods.** `_variants_map_for` (cache-hit) and the main build loop each decide who's a "variant mod" with slightly different rules. Unify. See [ERR] under cg3h_builder_entry.py.

4. **Agent-review miss rate is non-trivial.** The first review group flagged `_swap32` as a critical byte-rotation bug — it isn't. Confirms the rule: agent syntheses require verification before acting, per `feedback_agent_synthesis.md`.

## Recommended next cleanup (ordered)

1. Delete the dead `all_variants` block in `cg3h_builder_entry.py` (and the second loop in `pdb_lookup.py`).
2. Guard the empty-string `split` in `blender_addon/cg3h/__init__.py:705`.
3. Unify the two mod-classifier functions in `cg3h_builder_entry.py`.
4. Swap `g_saved_mesh_type` key from raw GMD pointer to (entry_hash, mesh_hash) — single-line change with real robustness payoff.
5. Add a plugin-init patch-status summary log to H2M covering the pool-size patches + shadow-cast cave.
6. Document the hardcoded struct offsets in one place (e.g. extend `docs/rendering_pipeline.md`) and ideally add a runtime sanity check.

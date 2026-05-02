# Changelog

All notable changes to CG3H are documented here.

---

## v3.15.0

**`animation_add` mods can now carry the full Hades II animation
metadata set — loop, fade durations, chain-to, gameplay flags
(invulnerability, immobility, no-collision, untargetable, hold-last-frame,
disable-input, allow-restart), per-source blend tables, and overrides
for inheritance, scale, native move speed, and 3D shadow.**

### Full SJSON Animation field schema

`mod.json` `target.new_animations[]` entries accept the complete set
of fields the Hades II engine consumes when reading per-character
Animation SJSON files.  At runtime, `CG3HBuilder` injects every
field set on each entry:

```json
{
  "logical_name":          "MyHeroSalute",
  "granny_name":           "Melinoe_X_MyHeroSalute_C_00",
  "clone_from":            "Melinoe_NoWeapon_Base_Salute_00",
  "source_glb_action":     "MyHeroSalute",
  "loop":                  false,
  "speed":                 0.5,
  "blend_in_frames":       8,
  "chain_to":              "MelinoeIdleWeaponless",
  "inherit_from":          "MelinoeBaseAnimation",
  "cancel_on_owner_move":  true,
  "hold_last_frame":       false,
  "allow_restart":         false,
  "owner_invulnerable":    true,
  "owner_immobile":        true,
  "owner_has_no_collision":true,
  "owner_untargetable":    true,
  "disable_owner_manual_interact": false,
  "enable_3d_shadow":      true,
  "scale":                 1.27,
  "native_move_speed":     540,
  "blends": [
    { "from": "MelinoeIdle",      "duration": 6 },
    { "from": "MelinoeRunFront",  "duration": 4 }
  ]
}
```

Every field is optional except `logical_name`, `granny_name`, and
`clone_from`.  The Blender addon still writes only the minimal
fields it has UI for (`logical_name`, `loop`); modders enrich the
rest by hand-editing `mod.json`.  A future authoring UX is in the
backlog.

### Per-source blend tables

When transitioning into your animation, the engine looks at the
incoming animation's name and finds the matching `blends` entry.
For example, in the snippet above the salute fades in over 6
frames when the player was idle but only 4 frames when they were
running.  This matches how stock Hades II animations express
context-dependent transitions (~80 % of stock anims with a
`Blends` array specify per-source durations).

### Misc

- `CG3HBuilder` plugin's runtime SJSON injector emits every v3.15
  field one-to-one — no need for `_sjson` passthrough today.
- `mod_state.lua` parser handles structured `blends` arrays and
  numeric overrides.
- All v3.14 performance wins (sub-10-second Mel imports, parallel
  per-entry patching, hand-rolled GLB writer) carry forward
  unchanged.

---

## v3.14.0

**Animation-heavy mods are no longer painful.  Building at game
launch is seconds-not-minutes, and importing a character into
Blender goes from "go get a coffee" to "blink and miss it".**

### Faster character import in Blender

Pulling Melinoe into Blender used to mean staring at the progress
log for 10+ minutes (or worse — depending on the build, the export
could effectively hang on the largest characters).  After a deep
rework of the export pipeline, a full Melinoe import — every mesh,
every bone, every animation — now finishes in **under 10 seconds**.

### Faster mod build at game launch

Every time you boot Hades II with CG3H mods installed, the
runtime builder reassembles the merged GPKs.  For mods that touch
animations, this could previously add 30-60 seconds to startup
while the GLB was parsed and patched.  After replacing the GLB
parser with a direct buffer reader, the parse step is sub-second
even on the largest characters, and the parser side of `convert()`
no longer scales linearly with the number of animations the GLB
carries.

### Parallel animation patching

For mods that edit many animations on a single character, the
runtime builder now spreads the per-entry work across CPU cores.
Auto-engages when there's enough work to amortize the worker
spin-up; small mods stay on the serial path with no overhead.

### Skip-load short-circuit

When you author a mod that edits a handful of animations, the
builder no longer loads every other animation entry on the
character just to check if it has a matching action — it
pre-filters against the GLB's action list before touching the
DLL.  The savings are biggest for the typical case (a 5-anim mod
on Melinoe used to load all ~850 stock entries; now it loads 5).

### Correctness verified

The reworked pipeline produces byte-equivalent output to v3.13's
exporter on every sampled mesh, animation channel, and bone bind
matrix — across 1385 sampled animation channels, 9 meshes, and
123 bones, all data rounds-trips identically.  The speedup is
purely architectural; nothing about the resulting mods changes.

---

## v3.13.0

**One-click outline + shadow on every new accessory, and accessories
no longer drift forward on scene load.**

### Auto-gen outline + shadow siblings

Adding a hat or a pair of glasses used to mean shipping just the
body mesh — the engine can't render an outline rim or cast a
shadow for a mesh that's missing those sibling passes, so
accessories looked flat and floated above the floor without
grounding.

The Blender addon's CG3H panel now exposes two checkboxes per new
mesh: **Shadow** (with a decimate ratio for the proxy) and
**Outline** (with a push percentage for the rim thickness).  At
export time the addon duplicates the source mesh, applies the
chosen modifier, renames it with the engine-recognised suffix, and
ships it alongside.  Modders don't have to author the proxies by
hand; defaults (decimate 0.30, push 1%) match how stock characters
ship their siblings.

Original (imported) meshes don't expose the toggles — they already
have stock outline and shadow siblings in the GR2.

### No more bind-pose lag on accessories

Freshly-loaded accessories used to render at a bind-pose offset for
several seconds after a scene load — the hat would float in front
of Melinoe at hub start and only snap into place when the in-game
mod manager was opened.  The scene-load hook now runs the
draw-state refresh pass automatically, so accessory bone tracks
bind on the very first frame the character is on screen.

### Inspectable export workflow

`File > Export > Hades II Mod (CG3H)` now leaves the assembled mod
under `<workspace>/build/plugins[_data]/<mod_id>/...` as a plain
folder you can browse, instead of producing a Thunderstore zip
directly.  When you're happy with what's in the folder, zip its
contents yourself for upload.  Intermediate scaffolding (the
source GLB, manifest, baseline) is swept after the build so the
workspace stays clean between exports.

### Misc

- Selective per-mesh routing on `mesh_add` mods is now stable
  across re-exports within a single Blender session — previously
  an orphan mesh data-block from a prior export could silently
  drift the routing keys away from the GLB mesh names, requiring
  a Blender restart between exports for the routing to apply.

---

## v3.12.0

**`texture_replace` mods can now be authored without Blender, and a
new Skin dropdown in the Characters tab swaps textures live —
no restart.**

### Folder-mirror authoring

A `texture_replace` mod can ship its overrides under a top-level
`textures/` folder whose internal layout mirrors the game's PKG
entry paths.  The builder walks the folder, packs every PNG into
the mod's `.pkg`, fills in `mod.json`.  No JSON editing required.

```
MyMelinoeRedDress/
  mod.json                            (type: texture_replace)
  textures/
    GR2/Melinoe_Color512.png          → overrides GR2\Melinoe_Color512
    UI/Portraits/Melinoe.png          → overrides UI\Portraits\Melinoe
```

Any PKG path is fair game — 3D model textures and 2D UI textures
(portraits, codex art, dialogue overlays) go through the same
authoring path.

The existing flat-PNGs-with-manifest layout that comes out of the
Blender addon keeps working unchanged.

### Skin picker in the in-game manager

The Characters tab gains a **Skin** dropdown next to Body.  Lists
"Stock" plus every `texture_replace` mod targeting that character.
Selecting an entry swaps the character's textures on the spot — no
restart, no area transition needed.  Picking "Stock" reverts to the
unmodded textures.

For NPCs that aren't currently in the scene, the selection is saved
and applied automatically the next time the character loads — a
freshly-spawned Hecate picks up her skin without you re-opening the
manager.

### Public API

`rom.game.CG3H_API` exposes:

- `set_active_skin(character, mod_id)` — apply and persist.
- `clear_skin(character)` — revert to stock and persist.

Other plugins can drive skin selection directly.

### Builder validation

When a `texture_replace` mod ships a texture for a PKG path the
game doesn't actually own (typo, wrong slash, missing prefix), the
builder logs a warning naming the mod and the offending path.
Build continues — the warning is advisory, not blocking.

### State persistence

Skin selections are saved to `cg3h_mod_state.json` and replayed at
launch.

### Breaking change

If you previously installed multiple `texture_replace` mods at once
and relied on them all applying together, that no longer happens —
the new picker is explicit-selection.  By default no skin is active
(vanilla textures show); pick one from the manager to apply it.

---

## v3.11.2

**Animations tab redesign + per-Action loop checkbox in the Blender
addon.**

### In-game Animations tab (clean redesign)

The first-pass tab grouped rows by mod and used a global Hero / Hub
NPC radio with a hardcoded `NPC_Hecate_01` text input.  Replaced
with a per-character layout — one CollapsingHeader per character,
listing every animation (new + patched) shipped by enabled mods,
with mod attribution per row.  Each Play button targets the row's
character implicitly: Melinoe / YoungMel → hero; everything else →
`GetClosestUnitOfType` with the character name (then
`NPC_<Char>_01` as a fallback).  No global picker, no hardcoded
NPC names.

Banner feedback when no live target is found —
`Play 'X': Hecate not in current scene`.

### Blender addon: per-Action Loop checkbox

When you author a new animation in Blender (an Action whose name
doesn't match any stock entry), the Export dialog now lists each
non-stock Action with a Loop checkbox.  State is stored on
`action["cg3h_loop"]` so it persists with the .blend file.  No
more hand-editing mod.json after export to flip Loop on a custom
clip.

The addon writes `target.new_animations: [{logical_name, loop}]`
for each non-stock Action; the builder's `_sync_mod_json` upserts
to fill in `granny_name`, `clone_from`, and `source_glb_action`
without overwriting anything the modder set.

### Misc

- `tools/install_plugin_local.py` auto-appends `/ReturnOfModding`
  when you pass the profile root by mistake.  H2M only loads from
  the subdir; deploying to the parent silently lands in an
  unused folder.

---

## v3.11.1

**Compatibility fix.** When CG3HBuilder was active alongside other
multi-file ImGui mods (notably `zerp-MelSkin`, plus the rest of the
SGG_Modding-DemonDaemon-driven stack), those mods' menus would fail
to render with "error happened on plugin." Disabling CG3HBuilder
unblocked them. v3.11.1 ships the fix.

### What was happening

CG3HBuilder loaded its helper Lua files (`mod_state.lua`,
`runtime.lua`, `ui.lua`) with `dofile()`. `dofile` runs the loaded
chunk in a *default* environment, not the per-plugin `_PLUGIN`-tagged
environment Hell2Modding sets up for our plugin. Functions defined
inside those helpers therefore carried the wrong environment as
their `_ENV`. When such a function later called an ImGui binding,
H2M's per-plugin ImGui dispatch saw a "ghost" plugin context and
trampled on whichever plugin happened to draw next — alphabetically,
that was MelSkin.

### Fix

`main.lua` now loads each helper module via
`loadfile(path, "t", _ENV)()` instead of `dofile(path)`. The fourth
argument explicitly attaches main.lua's `_ENV` (= the plugin's m_env)
to the loaded chunk, so functions defined inside the helpers carry
the correct plugin marker. ImGui calls dispatch to CG3HBuilder's
slot, MelSkin's slot stays untouched.

`ui.lua` also lost its `M.init(state, ctx)` entrypoint; it now
exposes `render_menu_bar`, `render`, and `is_open` as plain helpers.
main.lua wraps them in closures defined in main.lua's chunk before
passing them to `rom.gui.add_to_menu_bar` / `rom.gui.add_imgui` —
that way the closures sol2 captures carry main.lua's `_ENV`.

### Other changes

- **Toggling an animation-only mod** now shows
  *"animation mods rebuild on next game launch"* (yellow banner)
  instead of *"toggle failed"* (red). The toggle takes effect — it
  just requires a rebuild because animation_patch / animation_add
  content is baked into the merged GPK + SJSON aliases at build
  time.
- **SJSON injection callback** returns `nil` (not `content`) when
  there's nothing to inject. Returning a string triggered H2M's
  buffer-rewrite path on every SJSON read; `nil` skips it cleanly.
- **In-game Animations tab removed.** The first-pass tab hardcoded
  per-character NPC `DestinationName`s and tried to play GR2 entry
  keys via SetAnimation. Both were broken patterns. The clean
  redesign is on the roadmap (target picker driven by installed
  mods, GR2-key → SJSON-alias reverse lookup so patched-stock rows
  work too).
- **Bulk reference dump tool**:
  `tools/bulk_export_reference.py` exports every character GPK to
  a single folder (`<Character>.glb` + manifest + extracted PNG
  textures), suitable as an offline modding reference. Idempotent
  rerun.

### Compatibility

- H2M dependency stays at 1.0.95 (works against 1.0.95+, including
  the recent post-launch rebases).
- No mod.json schema changes.
- No data-format changes — existing v3.11.0 mods are unaffected.

---

## v3.11.0

**Brand-new animations.**  `animation_add` is the next step beyond
v3.10's `animation_patch` — instead of editing existing clips,
modders can ship animation entries the game has never seen, with
custom names, and trigger them from Lua.

This unlocks emote mods, custom death/idle/win-pose animations
selectable per modder, cinematic mods that play scripted sequences,
and mods that compose existing motion (a "fast idle" built by
re-timing the stock idle at 2× speed).

### What this enables

- New emote animations bound to a hotkey or a UI button.
- Custom death / idle / win-pose animations selectable per modder.
- Mods that compose existing motion under custom names.

### What it does NOT enable

- Engine-driven triggers (e.g., "play this when health drops below
  20%").  Modders call `SetAnimation` themselves from their own
  H2M plugin — CG3H exposes a small helper at
  `rom.game.CG3H_API.play_animation(target_id, anim_name)`.
- New animation tracks bound to bones the character doesn't have.
  Skeleton extension stays out of scope (v4.0).

### For mod authors

Two write modes are supported:

1. **Pure clone** (`clone_from`) — alias an existing GR2 animation
   under a new name.  Useful for re-targeting a stock animation
   to a new logical name your plugin can call.  No GLB content
   required.
2. **GLB-authored** (`source_glb_action` + `clone_from`) — the
   modder's Blender action provides the keyframes; the
   `clone_from` template provides the bone-track scaffolding.  The
   builder grafts the keyframes onto the template's bone structure
   and serializes a fresh GR2 entry.

`mod.json`:

```json
{
  "format": "cg3h-mod/1.0",
  "type": "animation_add",
  "metadata": { ... },
  "target": {
    "character": "Melinoe",
    "new_animations": [
      {
        "logical_name": "FunkyDance",
        "granny_name": "Melinoe_Author_FunkyDance_C_00",
        "clone_from": "Melinoe_NoWeapon_Base_Idle_00",
        "source_glb_action": "FunkyDance",
        "loop": true,
        "inherit_from": "MelinoeBaseAnimation",
        "chain_to": "MelinoeIdleWeaponless"
      }
    ]
  },
  "assets": { "glb": "MyMod.glb" }
}
```

`cg3h_build` auto-populates `target.new_animations` from any
Blender action whose name doesn't match a stock animation, so
hand-editing the schema is optional.

**Naming guidance**: animation aliases (`logical_name`) live in a
single global table the game reads at startup, so two mods that
ship the same alias name will collide.  Pick action names that
won't clash — prefix with your author handle (`Enderclem_Dance`
instead of `Dance`) until the addon does this for you.  GR2 entry
keys (`granny_name`) are auto-namespaced with the author slug, so
GPK-level collisions are not an issue.

### How it works under the hood

The Lua `SetAnimation({Name = X})` resolves `X` against an internal
Animation alias table the engine builds at startup from
`Content/Game/Animations/Model/*.sjson`.  `Name` is the SJSON
logical alias, NOT the GR2 entry key.  v3.11 ships two halves:

1. The new GR2 entry is baked into the merged per-character GPK at
   build time.
2. CG3HBuilder's runtime registers an `on_sjson_read_as_string`
   callback (Hell2Modding API) that appends a synthesized
   `Animation { Name = ..., GrannyAnimation = ..., Loop = ..., ... }`
   entry to the character's SJSON file before the engine parses it.

The engine then accepts the new alias as if it had always existed.
Animation aliases live in `cg3h_status.json` under
`characters[*].alias_animations` so they survive cache hits without
re-running the builder.

### Compatibility

- Existing `animation_patch` mods are unchanged — v3.11 is purely
  additive.
- `_classify_mod` returns a 4-tuple now; `is_animation_add` is
  orthogonal to mesh classification, so a single mod can be both
  a variant AND ship new animations.
- H2M dependency stays at 1.0.95.

---

## v3.10.0

**Standalone animation mods.**  `animation_patch` is now a first-class
mod type — modders can ship a GLB containing only edited animation
clips (no mesh content required) and CG3HBuilder will patch the
matching GR2 entries onto the stock character.

Animation editing was always supported, but only as a rider on a
mesh mod.  v3.10 makes it standalone: edit a clip in Blender, export,
publish, done.

### For mod players

- New mods may show up under the `animation_patch` type in mod
  manifests — they ship as small Thunderstore packages (often a few
  hundred KB) and apply on next game launch.
- **Animation patches are baked at build time and aren't live-toggleable.**
  Disabling an animation_patch mod requires a rebuild + restart (the
  v3.8+ instant-toggle covers `_Mesh` entries only).
- Animation conflicts (two mods editing the same clip) surface as
  warnings in the in-game CG3H log.  Use `priority.json` to pick a
  winner, or the alphabetically-later mod ID wins by default.

### For mod authors

- **Blender addon auto-detects animation-only edits.**  If you only
  modified armature animation tracks (no mesh position changes), the
  exporter writes `"type": "animation_patch"` to mod.json.  Mixed
  mesh + animation mods get the combined type list.
- **`target.animations` is auto-populated at build time.**
  `cg3h_build.py` walks the GLB's animation tracks, hashes each
  channel against the manifest baseline, and writes the canonical
  list of edited clip names to `mod.json::target.animations`.  This
  is what the conflict detector reads at scan time — pure metadata,
  no extra GLB I/O.
- Schema example in `docs/mod_spec.md` and the v3.10 plan doc at
  `docs/v3_10_animation_patch_plan.md`.

### Internal

- `tools/gltf_to_gr2.py::convert()` no longer raises
  "No meshes were patched" when called on a mesh-less GLB with
  animations to apply.  Per-entry mesh loop short-circuits when
  `glb_meshes` is empty.
- `tools/cg3h_builder_entry.py::_classify_mod` returns a third
  bool, `is_animation_only`, used by the build path to skip
  variant/accessory routing for pure animation mods.
- `tools/mod_info.py::check_conflicts` adds the animation overlap
  rule (set-intersect on `target.animations`).
- 12 new tests in `tests/test_core.py` covering classifier outcomes,
  animation overlap, three-way overlap, and missing-field fallback.

### Other

- `README.md` cleaned up: dropped the stale Hell2Modding-fork
  warning (replaced by upstream `Hell2Modding-Hell2Modding-1.0.92+`
  in v3.9.3) and added the animation-toggle caveat.

---

## v3.9.3

Bumps the upstream Hell2Modding dependency from `1.0.92` to `1.0.95`.
v3.9.2 wasn't shipped to Thunderstore — `1.0.92` was missing the most
recent post-launch offset patches and crashed on the current Hades II
build, so this is the first installable patch on top of v3.9.1.

### Changed

- **Dependency: `Hell2Modding-Hell2Modding-1.0.95`** (was `1.0.92`).

---

## v3.9.2

Drops the temporary `Enderclem-Hell2ModdingCG3H` fork and depends on
upstream `Hell2Modding-Hell2Modding-1.0.92` directly — the first nightly
that carries all the draw-path bindings, pool size patches, and
`add_granny_file` / `add_package_file` APIs CG3H needs.

### Fixed

- **Blender animation import hung indefinitely** on any character with
  20 or more animation clips (e.g. YoungMel, Melinoe). The PyInstaller-
  bundled `cg3h_exporter.exe` was missing `multiprocessing.freeze_support()`,
  so worker processes spawned by the parallel animation-extraction path
  re-executed the exe and ran `main()` from scratch — recursively
  spawning more workers. Export now completes in normal time.

### Changed

- **Dependency switched to upstream Hell2Modding.** Installing CG3H now
  pulls `Hell2Modding-Hell2Modding-1.0.92` instead of the fork. The
  Thunderstore fork (`Enderclem-Hell2ModdingCG3H`) is retired and will
  not receive further updates.
- **Runtime Lua uses the `draw_` API prefix** (`rom.data.draw_set_visible`,
  `rom.data.draw_swap_to_variant`, etc.) now that upstream ships the
  prefixed names from PR #31.

---

## v3.9.1

Bugfix release for accessory bone bindings on the Stock outfit picker.

### Fixed

- **Accessories bound to the wrong bone on the Stock variant** when multiple accessory mods were installed and their source GLBs had differently-ordered skin joint lists. Example: `MelinoeLArmRing`'s ring appeared on the side of the arm (not the hand) when `MelinoeGlasses` was also installed, because the GLB merger copied `JOINTS_0` buffers byte-for-byte while the merged node silently referenced the first accessory's skin. Bodies/variants were unaffected — their GLBs happened to share the ring's joint order. The merger now remaps joint indices by bone name so every merged mesh resolves against the base skeleton correctly.

### Internal

- `H2M_FORK_VERSION` split from `CG3H_VERSION` in `tools/cg3h_constants.py` so CG3H patch releases don't force a re-tag of the Hell2Modding fork. The fork stays pinned at `3.9.0`.

---

## v3.9.0

**Outfit switching.**  Install multiple body-replacement mods for the same character and pick between them in-game — no restart, no rebuild.  Install accessories alongside and toggle each one individually.  Requires the temporary `Enderclem-Hell2ModdingCG3H` fork on Thunderstore until upstream Hell2Modding merges the draw-path bindings.

### For mod players

- **In-game outfit picker.**  Mod manager → Characters → expand a character → new **Body** dropdowns per scene (Hub, Battle, Overlook, etc.).  Pick any installed body-replacement mod; the swap happens on the next frame.
- **Apply to all scenes** cascade — pick one mod and apply it across every scene that mod targets.
- **Accessories checkboxes** — the Characters tab has an **Accessories** section with a checkbox per accessory mod.  Instant toggle, no rebuild.
- **Default is vanilla.**  Installing CG3H mods no longer changes your character until you pick something in the mod manager.  Selections persist across launches.
- **Multiple body mods coexist.**  Two authors can ship different bodies for the same scene — players see both in the dropdown and pick their favorite.  Previously these conflicted; now they're variants.
- **Fewer placeholder-mesh bugs.**  Internal buffer budgets are raised, so loading many mods no longer crashes weapons/enemies into the "blank mesh" fallback.

### For mod authors

- **Blender addon auto-detects mod type.**  Export from Blender and the addon sets `mesh_add` / `mesh_replace` / both, based on whether you edited stock meshes, added new ones, or both.  No more manual `type` editing.
- **Build-time GLB stripping.**  Unchanged stock meshes are stripped from the deployed mod, shrinking distribution size and speeding up the runtime build.
- **`new_mesh_routing` always written** when you add a new mesh, so the runtime per-mesh visibility gate can locate it.
- **Blender addon requires Blender 4.2 LTS** (was 4.0).  Older versions now fail at install with a clear error instead of breaking at runtime.
- **`mesh_replace` authoring tip** — the pipeline uses a `.baseline_positions.npz` saved at import time to detect which meshes you actually edited.  If you author meshes by hand outside the addon, drop the baseline next to your GLB or you'll trigger fallback heuristics that may misclassify edits.

### Fixed

- Blender addon import error on Python 3.12+.
- Weapons/enemies rendering as placeholder when many mods were installed (static buffer pool capacity).
- `cg3h_status.json` lying about installed variants when the cached GPK was built from a stale `mod.json`.  Cache key now also tracks `mod.json` mtime, and the status file is cross-checked against the actual GPK contents on cache hits.
- Thunderstore ZIPs without a root `icon.png` caused r2modman to mis-extract them into double-nested `plugins/<mod>/<mod>/`.  The build now hard-errors if no icon is findable.
- GUI-installed mods populate r2modman's cache (icon + manifest), so tiles show the authored name and icon instead of the hyphen-slug placeholder.

### Removed

- **`mesh_patch` mod type.**  Was hand-authored only (no tool produced it) and overlapped `mesh_replace` in behavior.  If you wrote a mesh_patch mod, change `"type": "mesh_patch"` to `"type": "mesh_replace"` and rebuild.

### Known limitations

- **Shadow pass on a replaced body may still be stock-shaped** on some game updates — the engine hook for shadows uses a byte-pattern signature that shifts occasionally.  The mod still renders correctly in the main and outline passes; only the cast shadow may not match.
- **Toggling an accessory that was uninstalled at build time** requires a rebuild (the runtime can hide existing meshes but can't produce new ones).  Standard toggle (mod installed and enabled at build time) is instant.

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

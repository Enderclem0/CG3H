# v3.11 — animation_add: ship new animation entries

`animation_patch` (v3.10) lets modders edit existing animation clips
that the game already knows about.  v3.11 adds **`animation_add`** —
ship brand-new animation entries the game has never seen, with custom
names, and trigger them from Lua.

## What this enables

- New emote animations bound to a hotkey or a UI button.
- Custom death / idle / win-pose animations selectable per modder.
- Cinematic mods that play scripted sequences using author-defined
  clips.
- Compositing existing motion: a mod that defines "MyMod_FastIdle" by
  resampling stock Idle at 2x speed.

## What it does NOT enable

- New animation tracks bound to bones the character doesn't have.
  That requires skeleton extension (v4.0).
- Engine-driven triggers (e.g. "play this when health drops below
  20%").  Triggers are user-Lua: modders call `SetAnimation` (already
  exposed by H2M) or use ModUtil's `Path.Wrap` to inject into stock
  animation paths.

## Audit findings (existing pipeline)

`tools/gltf_to_gr2.py::patch_animation_entries`:

- **Edits** existing GR2 animation entries in place.  Walks each GR2,
  finds the entry's single Animation struct, and rewrites the curve
  data via Granny's write API.
- We have working code for: reading the Animation struct, locating
  the TrackGroup → bones → vector/quaternion tracks, and writing
  curve data back.
- What we DON'T have: code that constructs an Animation struct from
  scratch and serializes it as a NEW GR2 entry.

`granny_types.py` has type metadata for `granny_animation`,
`granny_track_group`, `granny_transform_track`, etc. — the structs
are already mapped.  We just haven't exercised the write path for
fresh objects.

GPK packing (`gpk_pack.py`) is layout-agnostic — adding a new entry
key is one line.

H2M side: `rom.data.add_granny_file` already registers the GPK at
runtime.  The game loads animation entries on-demand via HashGuid
lookup, so as long as the entry exists in the merged GPK and its
name is in the SDB string database, the engine will find it when
something calls `SetAnimation({Name = "..."})`.

## Schema

```json
{
  "format": "cg3h-mod/1.0",
  "type": "animation_add",
  "metadata": { ... },
  "target": {
    "character": "Melinoe",
    "new_animations": [
      "Melinoe_Enderclem_FunkyDance_C_00",
      "Melinoe_Enderclem_TauntPose_C_00"
    ]
  },
  "assets": { "glb": "Melinoe.glb" }
}
```

- `type: "animation_add"` (or in a list with other types).
- `target.new_animations` — auto-populated by `cg3h_build` from
  Blender actions whose names don't match any stock animation.
- Naming convention: `<Character>_<Author>_<ClipName>_C_00`.  Author
  prefix prevents cross-mod collisions; `_C_00` matches stock format
  so the engine's filename heuristics don't choke.

## Naming + collision strategy

Modders author actions in Blender with their own short names
(e.g. `FunkyDance`).  The Blender addon auto-prefixes at export
time to `<Character>_<Author>_<ClipName>_C_00`.  The exact author
slug comes from the addon preferences (already stored).

Stock-name collision: the addon refuses to ship a Blender action
whose name (after prefix) matches a stock animation — modders must
either rename it or use `animation_patch` instead.

Cross-mod collision: same author + same clip name across two mods
→ scan-time warning.  No overlap auto-resolution; modders pick
unique clip names.

## RE findings (smoke test, 2026-04-25)

`tests/scripts/test_animation_roundtrip.py` cloned
`YoungMel_NoWeapon_Base_Idle_C_00` raw bytes under a new entry
`YoungMel_Enderclem_RoundtripTest_C_00`, registered the GPK via
`rom.data.add_granny_file`, and triggered playback on Melinoe with
`SetAnimation({Name = "YoungMel_Enderclem_RoundtripTest_C_00",
DestinationId = CurrentRun.Hero.ObjectId})`.

**Confirmed:**

1. The engine accepts non-stock animation entry names. No SDB
   pre-population is required — `HashGuid::Lookup` hashes the
   string at call time, and the resulting hash matches whatever
   we hash on the encoder side.  `SetAnimation returned OK` was
   observed.
2. The internal `Animation.Name` field (still pointing at the
   original "YoungMel_NoWeapon_Base_Idle_C_00" string in the cloned
   bytes) does NOT need to match the new GPK entry key.  The
   engine uses the entry key as the canonical lookup, not the
   internal name.
3. Animation playback **resolves bones by name at runtime**.
   Playing the cloned YoungMel idle on Melinoe (different skeleton
   bone-name prefix) caused the mesh to collapse to a point —
   none of YoungMel's track names matched Melinoe's bones, so all
   transforms went to identity and the visible body disappeared.

**Implication for the feature:**
modders must author animation_add clips against the target
character's actual skeleton.  In practice this is automatic —
the Blender addon imports each character's full armature, so
exported GR2s will reference that character's bone names natively.
Cross-character sharing of an animation_add clip would require
explicit retargeting (out of scope for v3.11).

**Implication for the GPK registration path:** when standalone
plugins call `add_granny_file("X.gpk", path)`, the H2M map is
keyed by filename.  CG3HBuilder owns each *modded* character's
GPK key, so a smoke-test plugin must use a basename CG3HBuilder
isn't claiming.  We sidestep by sourcing from YoungMel (no user
mods → no CG3HBuilder claim).  For the production builder, the
new animation entries get baked into CG3HBuilder's existing
per-character GPK directly — no add_granny_file conflict.

## Required RE / write recipe

Phase 1 of implementation: prove we can write a fresh Animation
struct as a new GR2 entry.

1. Take a stock entry (e.g. `YoungMel_NoWeapon_Base_Idle_C_00`).
2. Read its Animation struct + TrackGroup + tracks.
3. Build a new `granny_file_info` with that Animation as its single
   exported object.
4. Serialize with `GrannyWriteEntireRuntimeFile` using the same
   golden-path setup we use for mesh writes.
5. Verify roundtrip: load it back, compare against the source.

Once roundtrip works, Phase 2 builds the Animation struct from
GLB-derived track data instead of cloning a stock one.

## Builder routing

In `convert()`:

- Existing `patch_animations=True` keeps editing in-place clips.
- New: `add_animations=True` (or list of new names) appends fresh
  GR2 entries to `gpk_entries` before `pack_gpk`.
- Mixed mods (animation_add + animation_patch + mesh_*) fall through
  the standard order: mesh first, then patch, then add.

In `cg3h_builder_entry.scan_and_build_all`: extend the per-character
build dispatcher to recognize `animation_add` as a separate axis
(not variant, not accessory, not animation-only-patch).

## Lua helper

Add to `runtime.lua`:

```lua
function M.play_animation(target_id, anim_name)
    if not (anim_name and target_id) then return false end
    local set_anim = rom.game and rom.game.SetAnimation
    if not set_anim then return false end
    local hash = rom.data.get_hash_guid_from_string(anim_name)
    if hash == 0 then
        rom.log.warning(LOG_PREFIX .. " play_animation: '"
            .. anim_name .. "' not registered in SDB")
        return false
    end
    set_anim({ Name = anim_name, DestinationId = target_id })
    return true
end
```

Expose under `rom.mods["Enderclem-CG3HBuilder"].play_animation`.
Modders call:

```lua
local cg3h = rom.mods["Enderclem-CG3HBuilder"]
cg3h.play_animation(CurrentRun.Hero.ObjectId, "Melinoe_Enderclem_FunkyDance_C_00")
```

## Phases (ordered)

1. **[DONE] RE: roundtrip a stock animation as a new entry.**
   Confirmed: `tests/scripts/test_animation_roundtrip_melinoe.py`
   proved engine accepts non-stock names and resolves bones at
   runtime.  Critical RE finding: `SetAnimation({Name=X})` resolves
   X via SJSON alias table, NOT the GR2 entry key — so we need to
   ship both halves (custom GR2 entry + SJSON alias).
2. **[DONE] Build new Animation from GLB.**
   `tools/gltf_to_gr2.py::_apply_glb_tracks_to_gr2` factored out of
   `patch_animation_entries`'s inner loop.  `convert(add_animations=
   [{template, target, glb_action}])` clones a stock entry's bytes,
   overwrites curves with the GLB action's data, and serializes
   under the new entry key.  Verified by
   `tests/scripts/test_animation_add_build.py` on YoungMel.
3. **[DONE] Schema + classifier + builder routing.**
   `_classify_mod` returns 4-tuple including `is_animation_add`
   (orthogonal to mesh axes).  `_collect_animation_adds` produces
   convert() specs + alias entries.  Builder threads
   `add_animations` into convert() and persists alias entries to
   `cg3h_status.json::characters[*].alias_animations`.
4. **[FUTURE] Blender addon export.**
   Auto-prefix Blender actions and refuse stock-name collisions.
   Not blocking for v3.11.0 — modders ship by naming actions with
   their author prefix manually.
5. **[DONE] `cg3h_build._sync_mod_json` populates `target.new_animations`.**
   Detects GLB actions whose names don't appear in
   `manifest.animations.hashes`, auto-fills `logical_name`,
   `granny_name = "<Char>_<Author>_<Action>_C_00"`,
   `source_glb_action`, and a default `clone_from` template.
6. **[DONE] Lua helper + cross-mod export.**
   `rom.game.CG3H_API.play_animation(target_id, anim_name)` exposed
   in `main.lua`.  `runtime.register_animation_aliases` hooks
   `rom.data.on_sjson_read_as_string` per character SJSON file at
   plugin init (before `ReadAllAnimationData`).
7. **[DONE] End-to-end fixture.**
   `tests/scripts/deploy_animation_add_fixture.py` ships two
   alias entries (byte-clone) for Melinoe; trigger plugin exposes
   per-alias ImGui buttons that call SetAnimation on Hero.
8. **[DONE] Release prep.**
   `CG3H_VERSION` → `3.11.0`, manifest bumped, CHANGELOG entry,
   docs refreshed.

## Out of scope for v3.11

- Triggering via mod.json declarative config (e.g. "play on health
  threshold X").  v3.12 if there's demand.
- Engine-side state machine integration — modders must call
  `SetAnimation` themselves; we don't replace stock state machines.
- Blender addon UX — modders prefix action names manually.  Slated
  for v3.11.1.
- Bones — v4.0.

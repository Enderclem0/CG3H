# CG3H Mod Builder — GUI Redesign Plan

## Current Problems

The GUI has 4 tabs organized by technical operation (Export, Import, Install, Build for H2M). A modder has to mentally map their workflow to the right tab. The legacy v2.x tabs (Import, Install) are confusing alongside the v3.0 H2M workflow.

## Design Principles

1. **Workflow-first**: Tabs match what the user WANTS to do, not the underlying operation
2. **One path**: No legacy/advanced split — one unified workflow
3. **Visible state**: Always show what mods exist and their status
4. **Minimal clicks**: Export → Edit → Build → Play should be 3-4 clicks

## Proposed Layout

### Tab 1: Create

The starting point. Pick a character, configure what to export, export to a mod workspace.

```
┌─ Create New Mod ──────────────────────────────────────┐
│ Character: [dropdown of all characters      ] [v]     │
│ Mod name:  [____________________]                     │
│ Author:    [____________________]                     │
│                                                       │
│ Options:                                              │
│   [x] Include textures                                │
│   [ ] Include animations                              │
│   [ ] Animation filter: [____________]                │
│   Mesh entries: [blank = all_________________]        │
│                                                       │
│ Output: C:\Users\...\Mods\MyMod\                      │
│                                                       │
│ [ Create Mod Workspace ]                              │
│                                                       │
│ Creates: mod.json + Character.glb + textures + manifest│
│ Next step: edit in Blender, then come back to Build    │
└───────────────────────────────────────────────────────┘
```

**What it does**:
1. Exports the character to GLB (gr2_to_gltf)
2. Auto-generates mod.json with metadata
3. Creates a clean workspace folder

### Tab 2: Build

Take an existing mod workspace and build it for H2M.

```
┌─ Build Mod ───────────────────────────────────────────┐
│ Mod workspace: [__________________________] [Browse]  │
│                                                       │
│ Mod: "Dark Armor" by Enderclem                        │
│ Character: Melinoe | Operations: adds_meshes,         │
│                                   replaces_textures   │
│ Assets: Melinoe.glb (2 new meshes), 1 custom texture  │
│                                                       │
│ [ ] Also create Thunderstore ZIP                      │
│ [ ] Install to r2modman after build                   │
│                                                       │
│ [ Build ]                                             │
│                                                       │
│ ┌─ Build Log ─────────────────────────────────────┐   │
│ │ Building GPK from Melinoe.glb...                │   │
│ │ Patched 4 meshes, added 2 new                   │   │
│ │ Built standalone .pkg (1 texture)               │   │
│ │ Installed to r2modman                           │   │
│ │ Done!                                           │   │
│ └─────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────┘
```

### Tab 3: Mods

See all mods, their status, edit, rebuild, manage conflicts.

```
┌─ Installed Mods ──────────────────────────────────────┐
│ ┌──────────────────────────────────────────────────┐  │
│ │ ● Dark Armor (Melinoe)           [mesh+texture]  │  │
│ │   by Enderclem v1.0.0 | Built 2026-04-05        │  │
│ │                                                  │  │
│ │ ● Cool Sword (Melinoe)           [mesh_add]     │  │
│ │   by SwordMaker v1.2.0 | Built 2026-04-04       │  │
│ │   ⚠ Conflict: shares Melinoe with Dark Armor    │  │
│ │                                                  │  │
│ │ ○ Red Skin (Zagreus)             [texture]      │  │
│ │   by TexMod v2.0.0 | Disabled                   │  │
│ └──────────────────────────────────────────────────┘  │
│                                                       │
│ [ Edit ] [ Rebuild ] [ Disable ] [ Remove ]           │
│                                                       │
│ ── Merge Order (Melinoe) ──────────────────────       │
│ 1. Dark Armor     [▲] [▼]                             │
│ 2. Cool Sword     [▲] [▼]                             │
│ [ Rebuild Merged ]                                    │
│                                                       │
│ ── Conflict Details ───────────────────────────       │
│ Both mods add meshes to Melinoe.                      │
│ They will be merged (both rendered).                   │
│ Priority: Dark Armor loads first, Cool Sword last.    │
└───────────────────────────────────────────────────────┘
```

**Edit button**: Opens the mod workspace folder (or the GLB in Blender if configured).
**Rebuild**: Re-runs cg3h_build on the mod workspace.
**Disable**: Removes from H2M plugins_data (keeps workspace).
**Remove**: Deletes the H2M install (keeps workspace).

### Settings (gear icon or menu)

```
┌─ Settings ────────────────────────────────────────────┐
│ Game path:    [C:\...\Hades II\         ] [Browse]    │
│ r2modman:     [auto-detected            ] [Browse]    │
│ Default author: [Enderclem              ]             │
│ Default output: [C:\Users\...\Mods\     ] [Browse]    │
└───────────────────────────────────────────────────────┘
```

## Implementation Plan

### Phase 1: Restructure (rename + reorganize)
- Rename window: "CG3H Mod Builder"
- Replace 4 tabs with 3: Create, Build, Mods
- Move settings to a top menu or settings dialog
- Remove legacy Import/Install tabs

### Phase 2: Create tab
- Character dropdown from GPK list (existing code)
- Export options (textures, animations, mesh entries)
- Auto-generate mod.json
- Output to mod workspace folder

### Phase 3: Build tab
- Merge existing "Build for H2M" + "Quick Create" into one clean UI
- Show mod info from mod.json
- Build log
- Install to r2modman checkbox

### Phase 4: Mods tab
- Scan r2modman for installed CG3H mods
- Show mod list with status (active/disabled/conflict)
- Edit/Rebuild/Disable/Remove buttons
- Merge order UI per character (up/down arrows)
- Conflict details panel

### Phase 5: Polish
- Status bar at bottom
- Progress bars during builds
- Keyboard shortcuts
- Remember last used paths

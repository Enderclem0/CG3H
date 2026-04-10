# Skinning Guide — Entries, Templates, and BoneBindings

This guide explains the three concepts you need to understand to skin a new mesh
onto a Hades II character: **entries**, **templates**, and **BoneBindings**.

If you're getting "weights fall back to root" warnings on export, this guide
shows you why and how to fix them.

---

## Quick reference

| Concept | What it is | When you set it |
|---|---|---|
| **Entry** | A self-contained version of the character (e.g. "Battle" vs "Hub") | Routing checkboxes in the CG3H panel |
| **Template** | An existing mesh whose bone permissions your new mesh inherits | Auto, or pick from the dropdown |
| **BoneBindings** | The list of bones a mesh is allowed to weight to | Inherited from the template |

---

## What is an entry?

A character's GPK file contains one or more **entries**. Each entry is a
complete copy of the character — its own meshes, bones, materials. The game
loads different entries in different contexts:

- **HecateBattle_Mesh** — Hecate as you see her in combat
- **HecateHub_Mesh** — Hecate as she appears in the hub world (Crossroads)

Most characters have just one entry. A few have two or three. You can see them
in the CG3H sidebar after importing — each entry gets a checkbox.

### Routing — choosing which entries your new mesh appears in

When you add a new mesh in Blender (a hat, glasses, an accessory), you decide
which entries it should appear in by checking the boxes in the CG3H panel:

```
Character: Hecate (2 entries)
Cube
[x] HecateBattle_Mesh
[ ] HecateHub_Mesh
```

This is **routing**. Above, the cube only appears on Battle Hecate, not Hub
Hecate. If both boxes are checked, the cube appears in both contexts. The
routing is saved into your mod's `mod.json` and applied at build time.

---

## What are BoneBindings?

Hades II meshes don't reference the full skeleton when they're skinned —
they each have a **BoneBindings** list, which is a subset of the skeleton's
bones that the mesh is allowed to use. If you weight a vertex to a bone that
isn't in the mesh's BoneBindings, the engine **silently falls back to the
root bone** at load time and your weight is lost.

Different meshes on the same character can have very different BoneBindings:

| Mesh | BoneBindings |
|---|---|
| HecateBattle_MeshShape (main body) | 80 bones |
| HecateBattle_MeshShape_1 (skirt) | 63 bones |
| HecateBattleOutline_MeshShape | 80 bones |
| HecateBattleOutline_MeshShape_1 | 68 bones |
| HecateCape_MeshShape | 34 bones |

The skirt mesh, for example, doesn't have face bones in its bindings — there's
no point, the skirt never deforms with the face. The cape only has cape-spine
bones. This is a memory optimization in the engine.

---

## What is a template?

When you add a **new** mesh (one that didn't exist in the original character),
the importer copies the BoneBindings from one **existing** mesh — that mesh is
the **template**. The new mesh inherits the template's BoneBindings exactly.

### Auto template selection

By default the importer picks a template automatically: it looks at which bones
you've actually painted weights on, and picks the existing mesh whose
BoneBindings overlap your weights the most. Tie-breaks favor templates with
more bones.

### Manual override

If you don't like the automatic pick, the **BoneBindings Template dropdown**
in the CG3H sidebar lets you force a specific template. Pick a mesh from the
list and:
- The viewport hides bones that aren't in that template's BoneBindings
- The export validator checks your weights against THAT template

---

## How routing and templates interact

Routing decides which entries your new mesh exists in. Templates are picked
**within** those entries. If you route to Battle only, the auto-template only
considers Battle meshes. If you route to both, the importer picks one template
per entry — they could be different.

```
Routing checkboxes:    [x] HecateBattle_Mesh   [ ] HecateHub_Mesh
                              ↓
              Cube goes to Battle entry only
                              ↓
            select_template() picks ONE template
            from existing meshes IN THE BATTLE ENTRY
            → e.g. HecateBattle_MeshShape (80 bones)
                              ↓
              Cube inherits those 80 bones
                              ↓
       Weights painted on bones not in those 80
                  → fall back to root
```

---

## The Blender sidebar

Open the **N panel → CG3H tab** to see the controls:

```
Character: Hecate (2 entries)
Cube
[ ] HecateBattle_Mesh   ← routing
[x] HecateHub_Mesh

BoneBindings Template
[◀] All routed bones [▶]
Validate vs: HecateHub_MeshShape (80 bones)

Armature: Hecate_Rig
Not linked to armature
[Setup for Skinning]
```

### Routing checkboxes
Choose which entries your new mesh appears in. Affects the entries baked
into `mod.json` and which templates are eligible.

### BoneBindings Template dropdown
The single source of truth for "what bones can I weight to right now". The
arrows cycle through presets:

- **Whole armature** — show every bone in the rig (including bones used by no
  mesh — useful as a reference view)
- **All routed bones** — union of all bones used by any mesh in the routed
  entries (the "what's possible if I had every template available")
- **Entry: HecateBattle_Mesh** — show only Battle bones (union)
- **Entry: HecateHub_Mesh** — show only Hub bones
- **Mesh: HecateBattle_MeshShape** — show ONLY this template's 80 bones

Picking a specific mesh forces validation against THAT template. Picking
"All" or an "Entry:" preset lets the auto-template selection run at validate
time.

The "Validate vs:" line under the dropdown shows you which template the
export popup will check your weights against.

### Setup for Skinning button
Adds an Armature modifier to your new mesh and parents it to the character's
armature, so you can immediately Tab into Weight Paint mode and start painting.

---

## Workflow

1. **Import** the character (File > Import > Hades II Model)
2. **Add your new mesh** in Blender (Add > Mesh > Cube, etc.)
3. In the **CG3H panel**, check which entries the mesh should appear in
4. Click **Setup for Skinning** to parent and add the Armature modifier
5. Pick a **Template** from the dropdown (or leave on All routed bones)
6. **Tab** into Weight Paint mode — only the relevant bones are visible
7. Paint your weights
8. **File > Export > Hades II Mod (CG3H)** — if any weights are on out-of-palette
   bones, a popup will list them and ask whether to export anyway

---

## Common issues

**"My new mesh doesn't deform with the body bone."**
The template you inherited doesn't include that body bone in its
BoneBindings. Pick a different template from the dropdown — usually the
character's main body mesh (e.g. `HecateBattle_MeshShape`).

**"Export popup says weights fall back to root."**
You painted weight on a bone that isn't in the template's BoneBindings. Either:
- Pick a different template that includes that bone, or
- Re-weight to a bone the current template supports, or
- Click OK and accept that those weights will be lost

**"I can't see all the bones."**
The dropdown is set to a restrictive preset. Switch to "Whole armature" to see
the entire rig.

**"My mesh appears in Battle but not Hub (or vice versa)."**
Check the routing checkboxes in the CG3H panel.

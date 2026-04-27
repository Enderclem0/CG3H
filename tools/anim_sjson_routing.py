"""Character → alias-home SJSON file mapping for v3.11 animation_add.

Engine loads ALL `.sjson` files under `Content/Game/Animations/Model/`
into one logical Animations table at startup.  Which file we inject
our synthesized alias into is cosmetic — the engine flattens them.
For each character (= GPK basename CG3HBuilder uses) we pick one
"alias home" file to receive its custom animation aliases.

If a character isn't in this map, the builder will emit a warning and
fall back to the heuristic guess (`NPC_<Character>_Animation.sjson`,
then `Enemy_<Character>_Animation.sjson`, then `Familiar_<Character>_
Animation.sjson`).  Mods can override at the mod.json level via
`target.alias_sjson`.
"""

# Character name (CG3H GPK basename) -> SJSON basename living under
# Content/Game/Animations/Model/ that the engine reads.
ALIAS_HOME_SJSON = {
    # Player character — multiple files exist; Personality has plenty
    # of spare room and is loaded universally.
    "Melinoe":      "Hero_Melinoe_Animation_Personality.sjson",
    "YoungMel":     "Hero_Melinoe_Young_Animation.sjson",

    # Hub NPCs — one file each.
    "Achilles":     "NPC_Achilles_Animation.sjson",
    "Apollo":       "NPC_Apollo_Animation.sjson",
    "Arachne":      "NPC_Arachne_Animation.sjson",
    "Artemis":      "NPC_Artemis_Animation.sjson",
    "Athena":       "NPC_Athena_Animation.sjson",
    "Cerberus":     "NPC_Cerberus_Animation.sjson",
    "Charon":       "NPC_Charon_Animation.sjson",
    "Circe":        "NPC_Circe_Animation.sjson",
    "Demeter":      "NPC_Demeter_Animation.sjson",
    "Dionysus":     "NPC_Dionysus_Animation.sjson",
    "Dora":         "NPC_Dora_Animation.sjson",
    "Echo":         "NPC_Echo_Animation.sjson",

    # Hub variants of battle characters split across files in-game;
    # CG3H's GPK keys for these are HecateHub / HecateBattle etc.
    "HecateHub":    "NPC_Hecate_Animation.sjson",
    "HecateBattle": "Enemy_Hecate_Animation.sjson",
    "MorosHub":     "NPC_Moros_Animation.sjson",
    "MorosBattle":  "Enemy_Moros_Animation.sjson",
    "NemesisHub":   "NPC_Nemesis_Animation.sjson",

    # Familiars.
    "Cat":          "Familiar_Cat_Animation.sjson",
    "Frog":         "Familiar_Frog_Animation.sjson",
    "Hound":        "Familiar_Hound_Animation.sjson",
    "Polecat":      "Familiar_Polecat_Animation.sjson",
    "Raven":        "Familiar_Raven_Animation.sjson",
}


_HEURISTIC_PREFIXES = ("NPC_", "Enemy_", "Familiar_", "Hero_")


def alias_home_for(character):
    """Best-effort SJSON home file for a character's custom aliases.

    Returns None when no explicit mapping exists — caller should warn
    and let the modder specify `target.alias_sjson` explicitly.
    """
    return ALIAS_HOME_SJSON.get(character)


def candidate_alias_homes(character):
    """All SJSON basenames the runtime should be willing to patch for
    this character.  When the modder doesn't override, returns the
    explicit mapping if present; otherwise returns the heuristic
    guesses in priority order so the runtime can probe them."""
    explicit = ALIAS_HOME_SJSON.get(character)
    if explicit:
        return [explicit]
    return [f"{prefix}{character}_Animation.sjson" for prefix in _HEURISTIC_PREFIXES]

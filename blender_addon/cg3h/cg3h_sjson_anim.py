"""v3.15: parser for stock Hades II Animation SJSON files.

Used at import time to lay every character's stock Animation entry
onto the imported armature's NLA tracks, with metadata pre-populated.
Modders then duplicate / rename / tweak strips to author new animations.

We only need to read the subset of SJSON that maps to v3.15's
animation_add field set — no need for a full SJSON implementation.
The grammar SGG ships is regular enough to handle with regex on
braced-block boundaries.

Public surface:
  load_animations(game_dir, character)
    -> { sjson_basename: [ {effective fields per anim}, ... ] }
"""
from __future__ import annotations

import os
import re
from typing import Optional


# ── Tokenisation helpers ────────────────────────────────────────────

_LINE_COMMENT = re.compile(r'//[^\n]*')
_BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.DOTALL)
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_NUMBER = re.compile(r'-?\d+(?:\.\d+)?')
_BOOL = re.compile(r'\b(?:true|false)\b')
_KEY = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=')


def _strip_comments(text: str) -> str:
    """Drop SJSON `//` line and `/* ... */` block comments so the
    braced-block regex isn't tripped up by example syntax inside the
    block comments — Hades II's hand-authored SJSON files lead with
    a long `/* attribute legend */` block at the top of each file."""
    text = _BLOCK_COMMENT.sub('', text)
    return _LINE_COMMENT.sub('', text)


def _find_balanced(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Return the index of the close-char that balances `open_ch` at
    text[start].  -1 if unbalanced."""
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == '"':
            # Skip the contents of a string literal — braces inside
            # strings don't count.
            j = i + 1
            while j < len(text):
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            i = j + 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_value(text: str, pos: int):
    """Parse a single SJSON value starting at `pos` (whitespace already
    skipped).  Returns (value, end_index)."""
    if pos >= len(text):
        return None, pos
    c = text[pos]
    if c == '"':
        m = _STRING.match(text, pos)
        if not m:
            return None, pos
        return m.group(1), m.end()
    if c == '{':
        end = _find_balanced(text, pos, '{', '}')
        if end < 0:
            return None, pos
        return _parse_object(text, pos + 1, end), end + 1
    if c == '[':
        end = _find_balanced(text, pos, '[', ']')
        if end < 0:
            return None, pos
        return _parse_array(text, pos + 1, end), end + 1
    m = _BOOL.match(text, pos)
    if m:
        return m.group(0) == 'true', m.end()
    m = _NUMBER.match(text, pos)
    if m:
        s = m.group(0)
        try:
            return (int(s) if '.' not in s else float(s)), m.end()
        except ValueError:
            return None, m.end()
    return None, pos


def _parse_object(text: str, start: int, end: int) -> dict:
    """Parse the body of a {…} block (between the braces)."""
    out = {}
    pos = start
    while pos < end:
        # Skip whitespace and stray commas.
        while pos < end and (text[pos].isspace() or text[pos] == ','):
            pos += 1
        if pos >= end:
            break
        m = _KEY.match(text, pos)
        if not m:
            # Probably trailing whitespace or malformed — bail rather
            # than hang.
            break
        key = m.group(1)
        vpos = m.end()
        while vpos < end and text[vpos].isspace():
            vpos += 1
        value, after = _parse_value(text, vpos)
        out[key] = value
        pos = after
    return out


def _parse_array(text: str, start: int, end: int) -> list:
    """Parse the body of a […] block (between the brackets)."""
    out = []
    pos = start
    while pos < end:
        while pos < end and (text[pos].isspace() or text[pos] == ','):
            pos += 1
        if pos >= end:
            break
        value, after = _parse_value(text, pos)
        if value is None and after == pos:
            break  # safety against parse-loop hang
        out.append(value)
        pos = after
    return out


def _parse_sjson_file(path: str) -> dict:
    """Parse a Hades II SJSON file into a Python dict.  Top-level keys
    (e.g. `Animations`, `BaseAnimations`) become dict keys."""
    with open(path, encoding='utf-8', errors='replace') as f:
        text = _strip_comments(f.read())
    # Top level is implicit object — wrap if not already braced.
    text_stripped = text.lstrip()
    if not text_stripped.startswith('{'):
        text = '{' + text + '}'
    end = _find_balanced(text, 0, '{', '}')
    if end < 0:
        return {}
    return _parse_object(text, 1, end)


# ── Inheritance resolution + character file discovery ───────────────

def _resolve_inheritance(entries: list) -> list:
    """Resolve `InheritFrom` chains in-place.  Each entry inherits any
    field NOT already set on it from the named parent.  Multi-level
    chains are flattened by repeated lookup; cycles are broken at a
    visited-set boundary."""
    by_name = {e.get('Name'): e for e in entries if isinstance(e, dict) and e.get('Name')}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        seen = set()
        cur = entry
        while True:
            parent_name = cur.get('InheritFrom')
            if not parent_name or parent_name in seen:
                break
            seen.add(parent_name)
            parent = by_name.get(parent_name)
            if not parent:
                break
            for k, v in parent.items():
                if k != 'Name' and k not in entry:
                    entry[k] = v
            cur = parent
    return entries


def _discover_character_sjson(game_dir: str, character: str) -> list:
    """Return paths to every SJSON file under
    `<game>/Content/Game/Animations/Model` whose name plausibly belongs
    to `character`.  Heuristic: file name contains the character substring.
    Catches Hero_Melinoe_*, NPC_<Char>_*, Enemy_<Char>_*, Familiar_<Char>_*."""
    root = os.path.join(game_dir, 'Content', 'Game', 'Animations', 'Model')
    if not os.path.isdir(root):
        return []
    out = []
    for fname in sorted(os.listdir(root)):
        if not fname.endswith('.sjson'):
            continue
        if character.lower() in fname.lower():
            out.append(os.path.join(root, fname))
    return out


def load_animations(game_dir: str, character: str) -> dict:
    """Top-level entry point — gather every Animation entry the engine
    knows for `character`, indexed by source SJSON file basename.

    Returns:
      { "Hero_Melinoe_Animation_Idle.sjson": [ {Name: ..., GrannyAnimation: ..., Loop: ..., Blends: [...], ...}, ... ],
        ... }

    InheritFrom chains within and across the character's SJSON files
    are resolved.  Modders see effective field values per anim, no
    need to chase parents.
    """
    if not (game_dir and character):
        return {}
    files = _discover_character_sjson(game_dir, character)
    if not files:
        return {}

    # First pass — gather all entries across all files so InheritFrom
    # can resolve cross-file (a character's "BaseAnimation" entry often
    # lives in one file and is referenced from many).
    all_entries: list = []
    by_file: dict = {}
    for path in files:
        try:
            doc = _parse_sjson_file(path)
        except Exception as e:
            print(f"[CG3H] SJSON parse failed: {path} ({e})")
            continue
        anims = doc.get('Animations') or []
        if not isinstance(anims, list):
            continue
        # Some SJSON files use 'BaseAnimations' for inheritance roots —
        # include them in the inheritance pool but don't surface them
        # to the modder (no GrannyAnimation, just a parent template).
        bases = doc.get('BaseAnimations') or []
        if isinstance(bases, list):
            all_entries.extend(bases)
        all_entries.extend(anims)
        by_file[os.path.basename(path)] = anims

    _resolve_inheritance(all_entries)
    return by_file

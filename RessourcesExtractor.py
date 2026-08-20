#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VMF Dependency Exporter & Traceability Tool (GMod / Source SDK 2013)
===========================================================================

Exporte les dépendances d'un VMF avec traçabilité complète, recherche intelligente
de secours pour les dossiers manquants/déplacés, scan VPK et extraction Lua/Soundscapes.
"""

import os
import re
import sys
import shutil
import argparse
import struct
import csv
from pathlib import Path
from collections import deque, defaultdict

# ----------------------------------------------------------------------------
# Regex & Constantes
# ----------------------------------------------------------------------------
EXCLUDED_MATERIAL_PREFIXES = ("tools/", "debug/", "editor/")

TEXTURE_KEYS = {
    "$basetexture", "$basetexture2", "$bumpmap", "$bumpmap2", "$detail",
    "$envmapmask", "$selfillummask", "$lightwarptexture",
    "$normalmap", "$phongwarptexture", "$blendmodulatetexture",
    "$parallaxmap", "$tintmasktexture", "$displacementmap", "$flow_noise_texture",
    "$flowmap", "$flow_normalmap", "$iris", "$ambientocclusiontexture", 
    "$ambientoccltexture", "$emissiveblendbasetexture", "$emissiveblendflowtexture", 
    "$emissiveblendtexture", "$detail1", "$detail2", "$refracttexture",
    "$dudvmap", "$hdrcompressedtexture0", "$hdrcompressedtexture1",
    "$hdrcompressedtexture2", "$hdrcompressedtexture3", "$corneatexture",
    "$ambientocclcolor", "$tooltexture", "$speculartexture"
}

BUILTIN_TEXTURES = {
    "env_cubemap", "flat", "normalize", "null", "error", "white", "black", "grey", "gray",
    "animatedtextureframenumvar", "animatedtexturevar", "frame"
}

PATH_CHARS_RE = re.compile(r'^[A-Za-z0-9_\-./\\ ]{2,90}$')

# ----------------------------------------------------------------------------
# Traçabilité (Provenance)
# ----------------------------------------------------------------------------
class ProvenanceTracker:
    def __init__(self):
        self.data = defaultdict(set)

    def add(self, category, item, source_info):
        if item:
            key = (category, item.lower().strip("/"))
            self.data[key].add(source_info)

    def get_sources(self, category, item):
        key = (category, item.lower().strip("/"))
        sources = self.data.get(key, set())
        return " | ".join(sorted(sources)) if sources else "Origine inconnue"

# ----------------------------------------------------------------------------
# Parseur VPK avec Recherche Intelligente
# ----------------------------------------------------------------------------
class VPKFS:
    def __init__(self):
        self.entries = {}  # relpath (lower) -> dict(dir_vpk, header_size, tree_size, archive_index, entry_offset, entry_length, preload)
        self.by_filename = defaultdict(list)

    def scan_game_vpks(self, game_src_path):
        root = Path(game_src_path)
        scan_dirs = [root]

        if root.parent.exists() and root.parent != root:
            scan_dirs.append(root.parent)

        common_dir = None
        for p in [root, root.parent, root.parent.parent]:
            if p.name.lower() == "common":
                common_dir = p
                break
            elif p.parent and p.parent.name.lower() == "common":
                common_dir = p.parent
                break

        if common_dir and common_dir.exists():
            for child in common_dir.iterdir():
                if child.is_dir() and child not in scan_dirs:
                    scan_dirs.append(child)

        vpk_files = []
        for sdir in scan_dirs:
            try:
                for dirpath, _, filenames in os.walk(sdir):
                    for fn in filenames:
                        fn_low = fn.lower()
                        if fn_low.endswith(".vpk"):
                            # Les archives numérotées (pak01_000.vpk...) ne contiennent
                            # pas d'arborescence, seulement les données brutes ; on ne les
                            # indexe pas comme "dir vpk" mais on ira les lire directement
                            # au moment de l'extraction (voir extract()).
                            if re.search(r'_\d{3}\.vpk$', fn_low): continue
                            vpk_files.append(Path(dirpath) / fn)
            except OSError: pass

        vpk_files = list(set(vpk_files))
        for vpk in vpk_files: self._index_vpk(vpk)
        print(f"  -> VPK Scanner : {len(vpk_files)} VPKs analysés ({len(self.entries)} fichiers uniques indexés)")

    def _index_vpk(self, dir_vpk_path):
        try:
            with open(dir_vpk_path, "rb") as f:
                header = f.read(12)
                if len(header) < 12: return
                magic, version, tree_size = struct.unpack("<III", header)
                if magic != 0x55aa1234: return

                header_size = 12 if version == 1 else 28
                f.seek(header_size)
                tree_data = f.read(tree_size)

                pos = 0
                def read_null_str(p):
                    end = tree_data.find(b"\x00", p)
                    if end == -1: return None, len(tree_data)
                    return tree_data[p:end].decode("utf-8", errors="ignore"), end + 1

                while pos < len(tree_data):
                    ext, pos = read_null_str(pos)
                    if ext is None or ext == "": break
                    while pos < len(tree_data):
                        path, pos = read_null_str(pos)
                        if path is None or path == "": break
                        while pos < len(tree_data):
                            filename, pos = read_null_str(pos)
                            if filename is None or filename == "": break

                            if pos + 18 > len(tree_data): break
                            crc, preload_bytes, archive_index, entry_offset, entry_length, terminator = struct.unpack(
                                "<IHHIIH", tree_data[pos:pos+18]
                            )
                            pos += 18
                            preload_data = tree_data[pos:pos+preload_bytes] if preload_bytes else b""
                            pos += preload_bytes

                            clean_path = path.strip()
                            rel = f"{clean_path}/{filename}.{ext}" if clean_path and clean_path != " " else f"{filename}.{ext}"
                            rel_clean = rel.replace("\\", "/").lower().strip("/")

                            self.entries[rel_clean] = {
                                "dir_vpk": dir_vpk_path,
                                "header_size": header_size,
                                "tree_size": tree_size,
                                "archive_index": archive_index,
                                "entry_offset": entry_offset,
                                "entry_length": entry_length,
                                "preload": preload_data,
                            }
                            self.by_filename[f"{filename}.{ext}".lower()].append(rel_clean)
        except Exception: pass

    def contains_smart(self, relpath, search_dir_prefix=""):
        clean_rel = relpath.replace("\\", "/").lower().strip("/")
        if clean_rel in self.entries:
            return True, clean_rel, None

        filename = Path(clean_rel).name
        candidates = self.by_filename.get(filename, [])
        if search_dir_prefix:
            prefix_low = search_dir_prefix.lower().strip("/") + "/"
            candidates = [c for c in candidates if c.startswith(prefix_low)]

        suffix_matches = [c for c in candidates if c.endswith(clean_rel)]
        if suffix_matches:
            return True, suffix_matches[0], f"Redirigé VPK : demandé '{clean_rel}', trouvé '{suffix_matches[0]}'"

        if len(candidates) == 1:
            return True, candidates[0], f"Redirigé VPK par nom : demandé '{clean_rel}', trouvé '{candidates[0]}'"

        return False, None, None


# ----------------------------------------------------------------------------
# Indexation du disque local avec recherche intelligente
# ----------------------------------------------------------------------------
class GameFS:
    def __init__(self, root):
        self.root = Path(root)
        self.index = {}
        self.by_filename = defaultdict(list)

    def index_subtree(self, subdir):
        # BUG corrigé : on ne scannait QUE gameSrc/<subdir> littéralement. Un
        # addon "legacy" (dossier non-.gma, ex: addons/gm_kindercity/lua/...)
        # a sa propre arborescence lua/materials/models/etc SOUS addons/<nom>/,
        # jamais fusionnée physiquement avec gameSrc/<subdir>. Ces fichiers
        # étaient donc invisibles à l'indexation même s'ils existaient bel et
        # bien sur le disque.
        bases = [self.root / subdir]
        addons_dir = self.root / "addons"
        if addons_dir.exists():
            try:
                for child in addons_dir.iterdir():
                    if child.is_dir():
                        bases.append(child / subdir)
            except OSError:
                pass

        count = 0
        for base in bases:
            if not base.exists(): continue
            for dirpath, _dirnames, filenames in os.walk(base):
                for fn in filenames:
                    full = Path(dirpath) / fn
                    try: rel = full.relative_to(self.root)
                    except ValueError: continue
                    rel_clean = str(rel).replace("\\", "/").lower()
                    self.index[rel_clean] = full
                    self.by_filename[fn.lower()].append((rel_clean, full))
                    count += 1
        print(f"  -> {subdir} (disque, y compris addons/*/{subdir}) : {count} fichiers indexés")

    def resolve(self, relpath):
        return self.index.get(relpath.replace("\\", "/").lower())

    def resolve_smart(self, relpath, search_dir_prefix=""):
        clean_rel = relpath.replace("\\", "/").lower().strip("/")
        if clean_rel in self.index:
            return self.index[clean_rel], clean_rel, None

        filename = Path(clean_rel).name
        candidates = self.by_filename.get(filename, [])

        if search_dir_prefix:
            prefix_low = search_dir_prefix.lower().strip("/") + "/"
            candidates = [c for c in candidates if c[0].startswith(prefix_low)]

        if not candidates:
            return None, None, None

        # Recherche par suffixe de chemin (ex: demandait metal/elevator_door.vmt, trouve havstrand_materials/metal/elevator_door.vmt)
        suffix_matches = [c for c in candidates if c[0].endswith(clean_rel)]
        if suffix_matches:
            chosen_rel, chosen_path = suffix_matches[0]
            return chosen_path, chosen_rel, f"Redirigé : demandé '{clean_rel}', trouvé '{chosen_rel}'"

        # Si un seul fichier porte ce nom exact dans la catégorie
        if len(candidates) == 1:
            chosen_rel, chosen_path = candidates[0]
            return chosen_path, chosen_rel, f"Redirigé par nom de fichier : demandé '{clean_rel}', trouvé '{chosen_rel}'"

        # Correspondance partielle la plus proche
        best_cand, best_score = None, -1
        target_parts = clean_rel.split("/")
        for cand_rel, cand_path in candidates:
            cand_parts = cand_rel.split("/")
            score = sum(1 for p in target_parts if p in cand_parts)
            if score > best_score:
                best_score = score
                best_cand = (cand_rel, cand_path)

        if best_cand:
            return best_cand[1], best_cand[0], f"Redirigé par correspondance approximative : demandé '{clean_rel}', trouvé '{best_cand[0]}'"

        return None, None, None

# ----------------------------------------------------------------------------
# Normalisation des chemins
# ----------------------------------------------------------------------------
def norm_material(v):
    if not v: return ""
    v = str(v).strip().replace("\\", "/").strip("/")
    if v.startswith("$") or v.startswith("%") or v.isdigit(): return ""
    low = v.lower()
    if low in BUILTIN_TEXTURES or low.startswith("_rt_"): return ""

    idx = low.find("materials/")
    if idx != -1: v = v[idx + len("materials/"):]
    if v.lower().endswith(".vmt"): v = v[:-4]
    return v.strip("/")

def norm_texture(v):
    if not v: return ""
    v = str(v).strip().replace("\\", "/").strip("/")
    if v.startswith("$") or v.startswith("%") or v.isdigit(): return ""
    low = v.lower()
    if low in BUILTIN_TEXTURES or low.startswith("_rt_"): return ""

    idx = low.find("materials/")
    if idx != -1: v = v[idx + len("materials/"):]
    if v.lower().endswith(".vtf"): v = v[:-4]
    return v.strip("/")

def norm_model(v):
    if not v: return ""
    v = str(v).strip().replace("\\", "/").strip("/")
    if v.startswith("$") or v.startswith("%"): return ""
    low = v.lower()
    if low.startswith("models/"): v = v[len("models/"):]
    if not low.endswith(".mdl"): v += ".mdl"
    return ("models/" + v).strip("/")

def norm_sound(v):
    v = str(v).strip().replace("\\", "/").strip("/")
    if v.lower().startswith("sound/"): v = v[len("sound/"):]
    return v

def norm_lua(v):
    v = str(v).strip().replace("\\", "/").strip("/")
    low = v.lower()
    if low.startswith("lua/"):
        return v
    elif low.startswith("maps/"):
        return v
    else:
        return f"lua/{v}"

def norm_sprite(v):
    # Les sprites legacy .spr (format binaire, pas un .vmt) vivent directement
    # sous "sprites/" à la racine du jeu (ex: garrysmod/sprites/glow01.spr),
    # PAS sous materials/sprites/. C'est une catégorie de ressource à part.
    v = str(v).strip().replace("\\", "/").strip("/")
    low = v.lower()
    if low.startswith("materials/"):
        v = v[len("materials/"):]
        low = v.lower()
    if not low.startswith("sprites/"):
        v = f"sprites/{v}"
    if not v.lower().endswith(".spr"):
        v += ".spr"
    return v

def is_excluded_material(name):
    low = name.lower()
    return any(low.startswith(p) for p in EXCLUDED_MATERIAL_PREFIXES)

# ----------------------------------------------------------------------------
# Parsing du VMF avec suivi de ligne et d'entité
# ----------------------------------------------------------------------------
def parse_vmf_detailed(vmf_text, vmf_stem, tracker):
    materials, models, sounds, particles = set(), set(), set(), set()
    skynames, effect_names, soundscapes, lua_scripts = set(), set(), set(), set()
    legacy_sprites = set()

    current_entity = "world"
    current_id = "0"
    re_kv = re.compile(r'^\s*"([^"]+)"\s+"([^"]*)"')

    for line_num, line in enumerate(vmf_text.splitlines(), start=1):
        line_str = line.strip()
        if not line_str or line_str.startswith("//"): continue

        if line_str == "entity" or line_str == "world":
            current_entity = line_str
            current_id = "0"
            continue

        m_kv = re_kv.match(line)
        if not m_kv: continue

        key, val = m_kv.group(1).lower(), m_kv.group(2).strip()
        if not val: continue

        if key == "id":
            current_id = val
            continue
        elif key == "classname":
            current_entity = val
            continue

        source_info = f"VMF Ligne {line_num} ({current_entity} id:{current_id})"

        if key == "material":
            if val.lower().endswith(".mdl"):
                m = norm_model(val)
                models.add(m); tracker.add("model", m, source_info)
            else:
                mat = norm_material(val)
                if mat and not is_excluded_material(mat):
                    materials.add(mat); tracker.add("material", mat, source_info)

        elif key == "model":
            val_low = val.lower()
            # env_sprite / env_glow / env_sun stockent soit un chemin de MATERIAL
            # moderne (materials/sprites/xxx.vmt) soit un sprite LEGACY .spr
            # (fichier binaire qui vit directement sous sprites/ à la racine du
            # jeu, PAS sous materials/). Le moteur distingue les deux uniquement
            # via l'extension ".spr" -- avant, tout était envoyé vers materials/,
            # donc les .spr littéraux étaient cherchés au mauvais endroit et
            # toujours "manquants" même quand ils existaient bien sous sprites/.
            if val_low.endswith(".spr"):
                spr = norm_sprite(val)
                legacy_sprites.add(spr); tracker.add("sprite", spr, source_info)
            else:
                is_sprite_entity = current_entity.lower() in ("env_sprite", "env_glow", "env_sun")
                if is_sprite_entity or val_low.endswith((".vmt", ".vtf")) or val_low.startswith("sprites/") or "/sprites/" in val_low:
                    mat = norm_material(val)
                    if mat and not is_excluded_material(mat):
                        materials.add(mat); tracker.add("material", mat, source_info)
                else:
                    mdl = norm_model(val)
                    models.add(mdl); tracker.add("model", mdl, source_info)

        elif key == "texture":
            # info_decal (et certains overlays custom) stockent leur material dans
            # la clé "texture", pas "material". C'était totalement absent avant :
            # un decal entier -- et donc toutes ses textures -- n'existait jamais
            # pour le script.
            mat = norm_material(val)
            if mat and not is_excluded_material(mat):
                materials.add(mat); tracker.add("material", mat, source_info)

        elif key == "skyname":
            skynames.add(val); tracker.add("skyname", val, source_info)

        elif key == "effect_name":
            effect_names.add(val); tracker.add("effect", val, source_info)

        elif key == "soundscape":
            soundscapes.add(val); tracker.add("soundscape", val, source_info)

        elif key in ("message", "wave", "sound"):
            if val.lower().endswith((".wav", ".mp3", ".ogg")):
                snd = norm_sound(val)
                sounds.add(snd); tracker.add("sound", snd, source_info)

        elif key in ("vscripts", "scriptfile") or "lua" in key:
            lua_path = norm_lua(val)
            lua_scripts.add(lua_path); tracker.add("lua", lua_path, source_info)

        if key == "code":
            for m_lua in re.finditer(r'["\']([^"\']+\.lua)["\']', val, re.IGNORECASE):
                lp = norm_lua(m_lua.group(1))
                lua_scripts.add(lp); tracker.add("lua", lp, source_info)

    map_lua_candidates = [
        f"maps/{vmf_stem}.lua",
        f"lua/autorun/{vmf_stem}.lua",
        f"lua/autorun/client/{vmf_stem}.lua",
        f"lua/autorun/server/{vmf_stem}.lua"
    ]
    for cand in map_lua_candidates:
        lua_scripts.add(cand)
        tracker.add("lua", cand, "Script automatique lié à la map")

    return {
        "materials": materials, "models": models, "sounds": sounds,
        "particles": particles, "skynames": skynames,
        "effect_names": effect_names, "soundscapes": soundscapes,
        "lua_scripts": lua_scripts, "legacy_sprites": legacy_sprites
    }

# ----------------------------------------------------------------------------
# Parsing VMT avec suivi du parent
# ----------------------------------------------------------------------------
def parse_vmt(vmt_actual_path, parent_mat_name, tracker, fs, vpk_fs):
    textures, nested_materials = set(), set()
    try: content = vmt_actual_path.read_text(encoding="utf-8", errors="ignore")
    except OSError: return textures, nested_materials

    content = re.sub(r'//.*', '', content)
    pairs = re.findall(r'"(\$[A-Za-z0-9_]+)"\s+"([^"]+)"', content, re.IGNORECASE)
    pairs += re.findall(r'(\$[A-Za-z0-9_]+)\s+"([^"]+)"', content, re.IGNORECASE)
    # BUG corrigé : le shader "Patch" (très utilisé pour les variantes teintées
    # de decals/overlays) référence un autre .vmt via la clé "include", SANS
    # signe $. L'ancien code ne capturait que les clés préfixées par $, donc
    # ces materials imbriqués n'étaient jamais détectés du tout.
    pairs += re.findall(r'"(include)"\s+"([^"]+)"', content, re.IGNORECASE)

    source_info = f"VMT: materials/{parent_mat_name}.vmt"

    for key, val in pairs:
        key_low, val_clean = key.lower(), val.strip()
        if not val_clean or val_clean.startswith("$") or val_clean.startswith("%") or val_clean.isdigit(): continue

        if key_low in ("include", "$bottommaterial", "$fallbackmaterial"):
            mat = norm_material(val_clean)
            if mat and not is_excluded_material(mat):
                nested_materials.add(mat)
                tracker.add("material", mat, f"{source_info} (clé: {key})")
        elif key_low in TEXTURE_KEYS:
            tex = norm_texture(val_clean)
            if tex:
                textures.add(tex)
                tracker.add("texture", tex, f"{source_info} (clé: {key})")
        else:
            # Filet de sécurité : certains shaders (decals, sprites, effets)
            # utilisent des clés de texture qui ne sont pas dans TEXTURE_KEYS
            # (liste forcément incomplète). Si la valeur correspond à un
            # fichier .vtf qui existe réellement (local ou VPK), on la garde
            # quand même plutôt que de la perdre silencieusement.
            tex = norm_texture(val_clean)
            if tex:
                rel_vtf = f"materials/{tex}.vtf"
                found_local = fs.resolve(rel_vtf) is not None
                found_vpk = vpk_fs.contains_smart(rel_vtf, "materials/")[0] if not found_local else False
                if found_local or found_vpk:
                    textures.add(tex)
                    tracker.add("texture", tex, f"{source_info} (clé inconnue: {key})")

    return textures, nested_materials

# ----------------------------------------------------------------------------
# Extraction des Soundscapes
# ----------------------------------------------------------------------------
def process_soundscapes(gameSrc, soundscape_names, tracker):
    waves, txt_files = set(), set()
    scripts_dir = Path(gameSrc) / "scripts"
    if not scripts_dir.exists(): return waves, txt_files

    for f in scripts_dir.glob("soundscapes*.txt"):
        try: text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError: continue

        file_rel = f"scripts/{f.name}"
        matched_in_file = False

        for ss in soundscape_names:
            target = f'"{ss.lower()}"'
            pos = text.lower().find(target)
            if pos == -1: continue

            matched_in_file = True
            source_info = f"Soundscape File: {file_rel} ({ss})"

            brace_start = text.find("{", pos)
            if brace_start == -1: continue

            depth, end = 0, None
            for i in range(brace_start, len(text)):
                if text[i] == "{": depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        end = i; break
            if end is None: continue

            block = text[brace_start:end]
            for m in re.finditer(r'"wave"\s+"([^"]+)"', block, re.IGNORECASE):
                w = norm_sound(m.group(1))
                waves.add(w)
                tracker.add("sound", w, source_info)

        if matched_in_file:
            txt_files.add(file_rel)
            tracker.add("script_txt", file_rel, "Soundscape chargé par la map")

    return waves, txt_files

def extract_ascii_strings(data, min_len=3):
    return [m.group().decode("ascii") for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data)]

def guess_model_materials(mdl_actual_path, mdl_name, fs, vpk_fs, tracker):
    try: data = mdl_actual_path.read_bytes()
    except OSError: return set()

    raw_strings = extract_ascii_strings(data, 3)
    candidates = [s.strip().replace("\\", "/").strip("/") for s in raw_strings]
    candidates = sorted(set(s for s in candidates if s and PATH_CHARS_RE.match(s)))

    found = set()
    source_info = f"Model: {mdl_name}"

    for c in candidates:
        mat_rel = f"materials/{c}.vmt"
        actual, res_rel, _ = fs.resolve_smart(mat_rel, "materials/")
        in_vpk, _, _ = vpk_fs.contains_smart(mat_rel, "materials/")
        if actual or in_vpk:
            found.add(c); tracker.add("material", c, source_info)

    dircands = [c for c in candidates if c.endswith("/") or "/" in c]
    dircands = sorted(set(d if d.endswith("/") else d + "/" for d in dircands)) + [""]
    namecands = [c for c in candidates if "/" not in c]

    if len(dircands) * len(namecands) <= 20000:
        for d in dircands:
            for n in namecands:
                mat_path = f"materials/{d}{n}.vmt"
                actual, _, _ = fs.resolve_smart(mat_path, "materials/")
                in_vpk, _, _ = vpk_fs.contains_smart(mat_path, "materials/")
                if actual or in_vpk:
                    m_name = f"{d}{n}".strip("/")
                    found.add(m_name); tracker.add("material", m_name, source_info)
    return found

def find_model_companions(mdl_actual_path):
    stem = mdl_actual_path.stem.lower()
    result = []
    try:
        for f in mdl_actual_path.parent.iterdir():
            if f.is_file() and f.name.lower().startswith(stem + "."): result.append(f)
    except OSError: pass
    return result

# ----------------------------------------------------------------------------
# Résolution Globale
# ----------------------------------------------------------------------------
def resolve_all(vmf_text, vmf_stem, gameSrc, fs, vpk_fs):
    tracker = ProvenanceTracker()
    base = parse_vmf_detailed(vmf_text, vmf_stem, tracker)

    materials_q, models_q = deque(base["materials"]), deque(base["models"])
    raw_sounds = set(base["sounds"])

    ss_waves, ss_txt_files = process_soundscapes(gameSrc, base["soundscapes"], tracker)
    raw_sounds.update(ss_waves)

    processed_materials, processed_models = set(), set()

    local_materials, vpk_materials, missing_materials = set(), set(), set()
    local_textures, vpk_textures, missing_textures = set(), set(), set()
    local_models, vpk_models, missing_models = set(), set(), set()
    local_sounds, vpk_sounds, missing_sounds = set(), set(), set()
    local_lua, vpk_lua, missing_lua = set(), set(), set()
    local_txt, vpk_txt, missing_txt = set(), set(), set()
    local_sprites, vpk_sprites, missing_sprites = set(), set(), set()

    model_companions = {}
    pending_textures = set()

    # --- MATERIALS & MODELS ---
    while materials_q or models_q:
        while materials_q:
            mat = materials_q.popleft()
            if not mat or mat in processed_materials or is_excluded_material(mat): continue
            processed_materials.add(mat)

            rel_vmt = f"materials/{mat}.vmt" if not mat.lower().endswith(".spr") else f"materials/{mat}"
            actual, res_rel, note = fs.resolve_smart(rel_vmt, "materials/")
            in_vpk, vpk_rel, vpk_note = vpk_fs.contains_smart(rel_vmt, "materials/")

            if actual:
                local_materials.add(res_rel)
                if note: tracker.add("material", mat, f"[Auto-Fix] {note}")
                if res_rel.endswith(".vmt"):
                    clean_mat_name = res_rel[len("materials/"): -4]
                    texs, nested = parse_vmt(actual, clean_mat_name, tracker, fs, vpk_fs)
                    pending_textures.update(texs)
                    for n in nested:
                        if n not in processed_materials: materials_q.append(n)
            elif in_vpk:
                vpk_materials.add(vpk_rel)
                if vpk_note: tracker.add("material", mat, f"[Auto-Fix] {vpk_note}")
            else:
                missing_materials.add(mat)

        while models_q:
            mdl = models_q.popleft()
            if not mdl or mdl in processed_models: continue
            processed_models.add(mdl)

            actual, res_rel, note = fs.resolve_smart(mdl, "models/")
            in_vpk, vpk_rel, vpk_note = vpk_fs.contains_smart(mdl, "models/")

            if actual:
                local_models.add(res_rel)
                if note: tracker.add("model", mdl, f"[Auto-Fix] {note}")
                model_companions[res_rel] = find_model_companions(actual)
                for mat in guess_model_materials(actual, mdl, fs, vpk_fs, tracker):
                    if mat not in processed_materials and not is_excluded_material(mat):
                        materials_q.append(mat)
            elif in_vpk:
                vpk_models.add(vpk_rel)
                if vpk_note: tracker.add("model", mdl, f"[Auto-Fix] {vpk_note}")
            else:
                missing_models.add(mdl)

    # --- TEXTURES ---
    for tex in pending_textures:
        if not tex: continue
        rel_vtf = f"materials/{tex}.vtf"
        actual, res_rel, note = fs.resolve_smart(rel_vtf, "materials/")
        in_vpk, vpk_rel, vpk_note = vpk_fs.contains_smart(rel_vtf, "materials/")

        if actual:
            local_textures.add(res_rel)
            if note: tracker.add("texture", tex, f"[Auto-Fix] {note}")
        elif in_vpk:
            vpk_textures.add(vpk_rel)
            if vpk_note: tracker.add("texture", tex, f"[Auto-Fix] {vpk_note}")
        else:
            missing_textures.add(tex)

    # --- SOUNDS ---
    for snd in raw_sounds:
        if not snd: continue
        rel_snd = f"sound/{snd}"
        actual, res_rel, note = fs.resolve_smart(rel_snd, "sound/")
        in_vpk, vpk_rel, vpk_note = vpk_fs.contains_smart(rel_snd, "sound/")

        if actual:
            local_sounds.add(res_rel)
            if note: tracker.add("sound", snd, f"[Auto-Fix] {note}")
        elif in_vpk:
            vpk_sounds.add(vpk_rel)
            if vpk_note: tracker.add("sound", snd, f"[Auto-Fix] {vpk_note}")
        else:
            missing_sounds.add(snd)

    # --- LUA SCRIPTS ---
    for lua in base["lua_scripts"]:
        if not lua: continue
        actual, res_rel, note = fs.resolve_smart(lua)
        in_vpk, vpk_rel, vpk_note = vpk_fs.contains_smart(lua)

        if actual:
            local_lua.add(res_rel)
            if note: tracker.add("lua", lua, f"[Auto-Fix] {note}")
        elif in_vpk:
            vpk_lua.add(vpk_rel)
            if vpk_note: tracker.add("lua", lua, f"[Auto-Fix] {vpk_note}")
        else:
            if not lua.startswith("maps/"):
                missing_lua.add(lua)

    # --- SOUNDSCAPE TXT FILES ---
    for txt in ss_txt_files:
        actual, res_rel, note = fs.resolve_smart(txt)
        in_vpk, vpk_rel, vpk_note = vpk_fs.contains_smart(txt)

        if actual:
            local_txt.add(res_rel)
            if note: tracker.add("script_txt", txt, f"[Auto-Fix] {note}")
        elif in_vpk:
            vpk_txt.add(vpk_rel)
            if vpk_note: tracker.add("script_txt", txt, f"[Auto-Fix] {vpk_note}")
        else:
            missing_txt.add(txt)

    # --- SPRITES LEGACY (.spr, dossier sprites/ à la racine) ---
    for spr in base["legacy_sprites"]:
        if not spr: continue
        actual, res_rel, note = fs.resolve_smart(spr, "sprites/")
        in_vpk, vpk_rel, vpk_note = vpk_fs.contains_smart(spr, "sprites/")

        if actual:
            local_sprites.add(res_rel)
            if note: tracker.add("sprite", spr, f"[Auto-Fix] {note}")
        elif in_vpk:
            vpk_sprites.add(vpk_rel)
            if vpk_note: tracker.add("sprite", spr, f"[Auto-Fix] {vpk_note}")
        else:
            missing_sprites.add(spr)

    return {
        "materials": local_materials, "vpk_materials": vpk_materials, "missing_materials": missing_materials,
        "textures": local_textures, "vpk_textures": vpk_textures, "missing_textures": missing_textures,
        "models": local_models, "vpk_models": vpk_models, "missing_models": missing_models,
        "sounds": local_sounds, "vpk_sounds": vpk_sounds, "missing_sounds": missing_sounds,
        "lua": local_lua, "vpk_lua": vpk_lua, "missing_lua": missing_lua,
        "txt": local_txt, "vpk_txt": vpk_txt, "missing_txt": missing_txt,
        "sprites": local_sprites, "vpk_sprites": vpk_sprites, "missing_sprites": missing_sprites,
        "model_companions": model_companions, "tracker": tracker
    }

# ----------------------------------------------------------------------------
# Exportation & Output
# ----------------------------------------------------------------------------
def copy_file(src, dest):
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return True
    except OSError as e:
        print(f"Erreur de copie pour {src}: {e}")
        return False

def build_report(data):
    lines = [
        "\n===== Rapport d'export & Diagnostic VPK =====",
        f"Materials   : {len(data['materials']):>4} à copier | {len(data['vpk_materials']):>4} en VPK | {len(data['missing_materials']):>4} TRULY MISSING",
        f"Textures    : {len(data['textures']):>4} à copier | {len(data['vpk_textures']):>4} en VPK | {len(data['missing_textures']):>4} TRULY MISSING",
        f"Models      : {len(data['models']):>4} à copier | {len(data['vpk_models']):>4} en VPK | {len(data['missing_models']):>4} TRULY MISSING",
        f"Sons        : {len(data['sounds']):>4} à copier | {len(data['vpk_sounds']):>4} en VPK | {len(data['missing_sounds']):>4} TRULY MISSING",
        f"Sprites .spr: {len(data['sprites']):>4} à copier | {len(data['vpk_sprites']):>4} en VPK | {len(data['missing_sprites']):>4} TRULY MISSING",
        f"Scripts Lua : {len(data['lua']):>4} à copier | {len(data['vpk_lua']):>4} en VPK | {len(data['missing_lua']):>4} TRULY MISSING",
        f"Soundscapes : {len(data['txt']):>4} à copier | {len(data['vpk_txt']):>4} en VPK | {len(data['missing_txt']):>4} TRULY MISSING",
        "=============================================\n"
    ]
    return "\n".join(lines)

def export_all(data, gameSrc, dest, fs):
    base, dest = Path(gameSrc), Path(dest)

    for cat in ("materials", "textures", "models", "sounds", "lua", "txt", "sprites"):
        for rel_path in data[cat]:
            actual = fs.resolve(rel_path)
            if actual:
                copy_file(actual, dest / rel_path)

    for mdl_rel, companions in data["model_companions"].items():
        for comp in companions:
            try:
                rel = comp.relative_to(base)
                copy_file(comp, dest / rel)
            except ValueError: pass

def print_missing_details(data):
    tracker = data["tracker"]
    print("\n--- DÉTAIL DES RESSOURCES TOTALEMENT MANQUANTES (TRULY MISSING) ET LEUR PROVENANCE ---")
    mapping = [
        ("missing_materials", "material"), ("missing_textures", "texture"),
        ("missing_models", "model"), ("missing_sounds", "sound"),
        ("missing_lua", "lua"), ("missing_txt", "script_txt"),
        ("missing_sprites", "sprite"),
    ]
    for cat_key, item_type in mapping:
        for item in sorted(data[cat_key]):
            sources = tracker.get_sources(item_type, item)
            print(f"[TRULY MISSING] {item_type}: {item}\n  └─ Demandé par : {sources}")

def write_csv(data, csv_path):
    tracker = data["tracker"]
    rows = []
    mapping = [
        ("materials", "material", "local_copy"), ("vpk_materials", "material", "vpk_native"), ("missing_materials", "material", "missing"),
        ("textures", "texture", "local_copy"), ("vpk_textures", "texture", "vpk_native"), ("missing_textures", "texture", "missing"),
        ("models", "model", "local_copy"), ("vpk_models", "model", "vpk_native"), ("missing_models", "model", "missing"),
        ("sounds", "sound", "local_copy"), ("vpk_sounds", "sound", "vpk_native"), ("missing_sounds", "sound", "missing"),
        ("lua", "lua", "local_copy"), ("vpk_lua", "lua", "vpk_native"), ("missing_lua", "lua", "missing"),
        ("txt", "soundscape_txt", "local_copy"), ("vpk_txt", "soundscape_txt", "vpk_native"), ("missing_txt", "soundscape_txt", "missing"),
        ("sprites", "sprite", "local_copy"), ("vpk_sprites", "sprite", "vpk_native"), ("missing_sprites", "sprite", "missing"),
    ]
    for key, item_type, status in mapping:
        for item in sorted(data[key]):
            sources = tracker.get_sources(item_type, item)
            rows.append((item_type, item, status, sources))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["type", "path", "status", "requested_by"])
        w.writerows(rows)
    print(f"CSV écrit : {csv_path}")

def main():
    parser = argparse.ArgumentParser(description="Exporte les dépendances d'une map VMF avec suivi de provenance")
    parser.add_argument("-source", required=True, help="Chemin du fichier .vmf")
    parser.add_argument("-gameSrc", required=True, help="Dossier garrysmod source")
    parser.add_argument("-dest", required=True, help="Dossier custom de sortie")
    parser.add_argument("-scan", action="store_true", help="Analyse seulement, ne copie rien")
    parser.add_argument("-force", action="store_true", help="Ne pas demander de confirmation avant de copier")
    parser.add_argument("-missing", action="store_true", help="Affiche le détail des ressources manquantes avec leur provenance")
    parser.add_argument("-csv", metavar="FICHIER", help="Écrit un rapport CSV détaillé vers ce fichier")
    args = parser.parse_args()

    vmf_path = Path(args.source)
    if not vmf_path.exists():
        print(f"Fichier VMF introuvable : {vmf_path}")
        sys.exit(1)

    vmf_text = vmf_path.read_text(encoding="utf-8", errors="ignore")

    print("1/2. Indexation du disque dur local...")
    fs = GameFS(args.gameSrc)
    for sub in ("materials", "models", "sound", "particles", "scripts", "lua", "maps", "sprites"):
        fs.index_subtree(sub)

    print("2/2. Indexation des VPK du jeu et des jeux montés...")
    vpk_fs = VPKFS()
    vpk_fs.scan_game_vpks(args.gameSrc)

    print("\nRésolution récursive des dépendances et traçabilité...")
    data = resolve_all(vmf_text, vmf_path.stem, args.gameSrc, fs, vpk_fs)

    print(build_report(data))

    if args.missing:
        print_missing_details(data)

    if args.csv:
        write_csv(data, args.csv)

    if args.scan:
        return

    if not args.force:
        ans = input("Procéder à l'export ? [Y/N] ")
        if ans.strip().lower() != "y": return

    print("Copie des fichiers locaux en cours...")
    export_all(data, args.gameSrc, args.dest, fs)
    print("Export terminé avec succès.")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VMF Dependency Exporter & Traceability Tool (GMod / Source SDK 2013) - v3.2
===========================================================================

Exporte les dépendances d'un VMF avec traçabilité complète, recherche intelligente
de secours pour les dossiers manquants/déplacés, scan VPK, extraction Lua/Soundscapes,
résolution multithreadée et rapport en couleur.
"""

import os
import re
import sys
import time
import shutil
import argparse
import struct
import csv
import threading
from concurrent.futures import ThreadPoolExecutor
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
# Présentation terminal "vintage" en couleurs (ANSI). Se désactive proprement
# si le terminal ne les supporte pas (sortie redirigée vers un fichier, etc.)
# ou si NO_COLOR est défini dans l'environnement.
# ----------------------------------------------------------------------------
if sys.platform == "win32":
    os.system("")  # active le rendu ANSI/VT100 dans cmd.exe / PowerShell (Windows 10+)

def _supports_color():
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    except Exception:
        return False

class C:
    ON = _supports_color()
    RESET = "\033[0m" if ON else ""
    BOLD = "\033[1m" if ON else ""
    DIM = "\033[2m" if ON else ""
    GREEN = "\033[38;5;114m" if ON else ""
    AMBER = "\033[38;5;179m" if ON else ""
    CYAN = "\033[38;5;80m" if ON else ""
    RED = "\033[38;5;203m" if ON else ""
    MAGENTA = "\033[38;5;140m" if ON else ""
    GREY = "\033[38;5;244m" if ON else ""

def cprint(text, color="", bold=False, end="\n"):
    prefix = (C.BOLD if bold else "") + color
    sys.stdout.write(f"{prefix}{text}{C.RESET}{end}")
    sys.stdout.flush()

def print_banner(threads):
    bar = "═" * 63
    lines = [
        f"{C.CYAN}╔{bar}╗{C.RESET}",
        f"{C.CYAN}║{C.RESET}{C.BOLD}{C.AMBER}{'V M F   R E S O U R C E   E X T R A C T O R':^62}{C.RESET}{C.CYAN} ║{C.RESET}",
        f"{C.CYAN}║{C.RESET}{C.GREY}{'-- traçabilité, VPK, lua & sprites -- GMod / Source SDK 2013 --':^62}{C.RESET}{C.CYAN}║{C.RESET}",
        f"{C.CYAN}╚{bar}╝{C.RESET}",
        f"{C.GREY}   threads: {threads}   {time.strftime('%Y-%m-%d %H:%M:%S')}{C.RESET}",
    ]
    print("\n".join(lines))

# ----------------------------------------------------------------------------
# Traçabilité (Provenance)
# ----------------------------------------------------------------------------
class ProvenanceTracker:
    def __init__(self):
        self.data = defaultdict(set)
        self._lock = threading.Lock()

    def add(self, category, item, source_info):
        if item:
            key = (category, item.lower().strip("/"))
            with self._lock:
                self.data[key].add(source_info)

    def get_sources(self, category, item):
        key = (category, item.lower().strip("/"))
        with self._lock:
            sources = set(self.data.get(key, set()))
        return " | ".join(sorted(sources)) if sources else "Origine inconnue"

# ----------------------------------------------------------------------------
# Parseur VPK avec Recherche Intelligente
# ----------------------------------------------------------------------------
class VPKFS:
    """Index des fichiers présents dans les VPK (jeu de base + jeux montés).

    On ne fait QUE de la détection de présence, jamais de copie/extraction :
    ce contenu (CS:S, HL2, TF2, autres addons en .vpk...) est déjà présent
    chez tout joueur possédant ces jeux/addons montés, donc pas besoin de le
    repacker dans l'addon. Le rôle de cette classe est uniquement d'éviter
    de classer à tort en "TRULY MISSING" une ressource qui est en fait tout
    à fait disponible côté jeu.
    """
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
        print(f"{C.GREY}  -> VPK Scanner : {len(vpk_files)} VPKs analysés ({len(self.entries)} fichiers uniques indexés){C.RESET}")

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
        print(f"{C.GREY}  -> {subdir} (disque, y compris addons/*/{subdir}) : {count} fichiers indexés{C.RESET}")

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

    def find_by_stem_in_dirs(self, stem, dir_prefixes, exclude_addons=True):
        """Recherche floue par ressemblance de nom (pas de correspondance exacte
        exigée), restreinte à une liste de dossiers. Sert au lua "deviné" lié au
        nom de la map : rien dans le VMF n'impose son nom exact, donc on cherche
        par proximité. exclude_addons=True pour ne JAMAIS chercher sous addons/
        (sur demande explicite : ces scripts-là sont censés être dans le jeu de
        base, pas dans un addon tiers)."""
        stem_norm = re.sub(r'[^a-z0-9]', '', stem.lower())
        if not stem_norm: return []
        results = []
        for rel, path in self.index.items():
            if exclude_addons and rel.startswith("addons/"):
                continue
            if dir_prefixes and not any(rel.startswith(p) for p in dir_prefixes):
                continue
            name_norm = re.sub(r'[^a-z0-9]', '', Path(rel).stem.lower())
            if not name_norm: continue
            if stem_norm == name_norm or stem_norm in name_norm or name_norm in stem_norm:
                results.append((rel, path))
        return results

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

    # Note : le lua "deviné" lié au nom de la map (autorun/*.lua portant un nom
    # proche du .vmf) n'est PAS ajouté ici -- rien dans le VMF ne garantit son
    # nom exact, donc une recherche floue est faite séparément dans resolve_all,
    # restreinte au jeu de base (jamais sous addons/).

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
def resolve_all(vmf_text, vmf_stem, gameSrc, fs, vpk_fs, threads=1, progress=True):
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

    n_workers = max(1, int(threads or 1))
    executor = ThreadPoolExecutor(max_workers=n_workers) if n_workers > 1 else None

    # -- fonctions "pures" : ne lisent que fs/vpk_fs (lecture seule, donc
    # thread-safe), n'écrivent nulle part -- tout le merge dans les sets/
    # tracker partagés se fait ensuite dans le thread principal uniquement.
    def _resolve_material(mat):
        rel_vmt = f"materials/{mat}.vmt" if not mat.lower().endswith(".spr") else f"materials/{mat}"
        actual, res_rel, note = fs.resolve_smart(rel_vmt, "materials/")
        in_vpk, vpk_rel, vpk_note = (False, None, None)
        texs, nested = set(), set()
        if actual:
            if res_rel.endswith(".vmt"):
                clean_mat_name = res_rel[len("materials/"): -4]
                texs, nested = parse_vmt(actual, clean_mat_name, tracker, fs, vpk_fs)
        else:
            in_vpk, vpk_rel, vpk_note = vpk_fs.contains_smart(rel_vmt, "materials/")
        return mat, actual, res_rel, note, in_vpk, vpk_rel, vpk_note, texs, nested

    def _resolve_model(mdl):
        actual, res_rel, note = fs.resolve_smart(mdl, "models/")
        in_vpk, vpk_rel, vpk_note = (False, None, None)
        mats_found, companions = set(), []
        if actual:
            companions = find_model_companions(actual)
            mats_found = guess_model_materials(actual, mdl, fs, vpk_fs, tracker)
        else:
            in_vpk, vpk_rel, vpk_note = vpk_fs.contains_smart(mdl, "models/")
        return mdl, actual, res_rel, note, in_vpk, vpk_rel, vpk_note, mats_found, companions

    # --- MATERIALS & MODELS (BFS par vagues, chaque vague traitée en //) ---
    round_no = 0
    while materials_q or models_q:
        round_no += 1
        mat_batch = []
        while materials_q:
            m = materials_q.popleft()
            if not m or m in processed_materials or is_excluded_material(m): continue
            processed_materials.add(m)
            mat_batch.append(m)

        mdl_batch = []
        while models_q:
            d = models_q.popleft()
            if not d or d in processed_models: continue
            processed_models.add(d)
            mdl_batch.append(d)

        if not mat_batch and not mdl_batch:
            break

        if executor and (len(mat_batch) + len(mdl_batch) > 1):
            mat_results = list(executor.map(_resolve_material, mat_batch)) if mat_batch else []
            mdl_results = list(executor.map(_resolve_model, mdl_batch)) if mdl_batch else []
        else:
            mat_results = [_resolve_material(m) for m in mat_batch]
            mdl_results = [_resolve_model(d) for d in mdl_batch]

        for mat, actual, res_rel, note, in_vpk, vpk_rel, vpk_note, texs, nested in mat_results:
            if actual:
                local_materials.add(res_rel)
                if note: tracker.add("material", mat, f"[Auto-Fix] {note}")
                pending_textures.update(texs)
                for n in nested:
                    if n not in processed_materials: materials_q.append(n)
            elif in_vpk:
                vpk_materials.add(vpk_rel)
                if vpk_note: tracker.add("material", mat, f"[Auto-Fix] {vpk_note}")
            else:
                missing_materials.add(mat)

        for mdl, actual, res_rel, note, in_vpk, vpk_rel, vpk_note, mats_found, companions in mdl_results:
            if actual:
                local_models.add(res_rel)
                if note: tracker.add("model", mdl, f"[Auto-Fix] {note}")
                model_companions[res_rel] = companions
                for mat in mats_found:
                    if mat not in processed_materials and not is_excluded_material(mat):
                        materials_q.append(mat)
            elif in_vpk:
                vpk_models.add(vpk_rel)
                if vpk_note: tracker.add("model", mdl, f"[Auto-Fix] {vpk_note}")
            else:
                missing_models.add(mdl)

        if progress:
            done = len(processed_materials) + len(processed_models)
            left = len(materials_q) + len(models_q)
            cprint(f"  Vague {round_no} : {done} traités ({len(processed_materials)} materials / {len(processed_models)} models) | {left} en attente pour la vague suivante   ", C.DIM, end="\r")

    if progress:
        print()  # newline final après les vagues (\r utilisé pendant la boucle)

    # --- Fonction générique pour les catégories "à plat" (pas de récursion) ---
    def _resolve_flat(item, dir_prefix=""):
        actual, res_rel, note = fs.resolve_smart(item, dir_prefix)
        in_vpk, vpk_rel, vpk_note = (False, None, None)
        if not actual:
            in_vpk, vpk_rel, vpk_note = vpk_fs.contains_smart(item, dir_prefix)
        return item, actual, res_rel, note, in_vpk, vpk_rel, vpk_note

    def _run_flat_batch(items, dir_prefix=""):
        items = [i for i in items if i]
        if not items:
            return []
        if executor and len(items) > 1:
            return list(executor.map(lambda i: _resolve_flat(i, dir_prefix), items))
        return [_resolve_flat(i, dir_prefix) for i in items]

    # --- TEXTURES ---
    for tex, actual, res_rel, note, in_vpk, vpk_rel, vpk_note in _run_flat_batch(
            [f"materials/{t}.vtf" for t in pending_textures if t], "materials/"):
        tex_clean = norm_texture(tex)
        if actual:
            local_textures.add(res_rel)
            if note: tracker.add("texture", tex_clean, f"[Auto-Fix] {note}")
        elif in_vpk:
            vpk_textures.add(vpk_rel)
            if vpk_note: tracker.add("texture", tex_clean, f"[Auto-Fix] {vpk_note}")
        else:
            missing_textures.add(tex_clean)

    # --- SOUNDS ---
    for snd, actual, res_rel, note, in_vpk, vpk_rel, vpk_note in _run_flat_batch(
            [f"sound/{s}" for s in raw_sounds if s], "sound/"):
        snd_clean = norm_sound(snd)
        if actual:
            local_sounds.add(res_rel)
            if note: tracker.add("sound", snd_clean, f"[Auto-Fix] {note}")
        elif in_vpk:
            vpk_sounds.add(vpk_rel)
            if vpk_note: tracker.add("sound", snd_clean, f"[Auto-Fix] {vpk_note}")
        else:
            missing_sounds.add(snd_clean)

    # --- LUA SCRIPTS (références explicites trouvées dans le VMF) ---
    for lua, actual, res_rel, note, in_vpk, vpk_rel, vpk_note in _run_flat_batch(base["lua_scripts"]):
        if actual:
            local_lua.add(res_rel)
            if note: tracker.add("lua", lua, f"[Auto-Fix] {note}")
        elif in_vpk:
            vpk_lua.add(vpk_rel)
            if vpk_note: tracker.add("lua", lua, f"[Auto-Fix] {vpk_note}")
        else:
            if not lua.startswith("maps/"):
                missing_lua.add(lua)

    # --- LUA "DEVINÉ" LIÉ AU NOM DE LA MAP ---
    # Rien dans le VMF n'impose le nom exact de ce script (si il existe), donc
    # recherche par ressemblance de nom plutôt que correspondance exacte.
    # UNIQUEMENT dans le jeu de base : jamais sous addons/ (demande explicite).
    guess_dirs = ["maps/", "lua/autorun/", "lua/autorun/client/", "lua/autorun/server/"]
    for rel, _path in fs.find_by_stem_in_dirs(vmf_stem, guess_dirs, exclude_addons=True):
        local_lua.add(rel)
        tracker.add("lua", rel, "Script probablement lié à la map (nom proche), jeu de base uniquement")
    # Pas d'ajout à missing_lua ici : ce n'était qu'une supposition, pas une
    # dépendance confirmée par le VMF -- l'absence de résultat n'est pas une erreur.

    # --- SOUNDSCAPE TXT FILES ---
    for txt, actual, res_rel, note, in_vpk, vpk_rel, vpk_note in _run_flat_batch(ss_txt_files):
        if actual:
            local_txt.add(res_rel)
            if note: tracker.add("script_txt", txt, f"[Auto-Fix] {note}")
        elif in_vpk:
            vpk_txt.add(vpk_rel)
            if vpk_note: tracker.add("script_txt", txt, f"[Auto-Fix] {vpk_note}")
        else:
            missing_txt.add(txt)

    # --- SPRITES LEGACY (.spr, dossier sprites/ à la racine) ---
    for spr, actual, res_rel, note, in_vpk, vpk_rel, vpk_note in _run_flat_batch(base["legacy_sprites"], "sprites/"):
        if actual:
            local_sprites.add(res_rel)
            if note: tracker.add("sprite", spr, f"[Auto-Fix] {note}")
        elif in_vpk:
            vpk_sprites.add(vpk_rel)
            if vpk_note: tracker.add("sprite", spr, f"[Auto-Fix] {vpk_note}")
        else:
            missing_sprites.add(spr)

    if executor:
        executor.shutdown(wait=True)

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

def _line_color(missing_count):
    return C.RED if missing_count > 0 else C.GREEN

def build_report(data):
    rows = [
        ("Materials   ", "materials", "vpk_materials", "missing_materials"),
        ("Textures    ", "textures", "vpk_textures", "missing_textures"),
        ("Models      ", "models", "vpk_models", "missing_models"),
        ("Sons        ", "sounds", "vpk_sounds", "missing_sounds"),
        ("Sprites .spr", "sprites", "vpk_sprites", "missing_sprites"),
        ("Scripts Lua ", "lua", "vpk_lua", "missing_lua"),
        ("Soundscapes ", "txt", "vpk_txt", "missing_txt"),
    ]
    bar = "─" * 62
    lines = [f"\n{C.CYAN}┌{bar}┐{C.RESET}",
             f"{C.CYAN}│{C.RESET}{C.BOLD}{' RAPPORT D’EXPORT & DIAGNOSTIC VPK':^62}{C.RESET}{C.CYAN}│{C.RESET}",
             f"{C.CYAN}├{bar}┤{C.RESET}"]
    for label, local_k, vpk_k, miss_k in rows:
        n_local, n_vpk, n_miss = len(data[local_k]), len(data[vpk_k]), len(data[miss_k])
        miss_col = _line_color(n_miss)
        line = (f"{label} : {C.GREEN}{n_local:>4} à copier{C.RESET} | "
                f"{C.AMBER}{n_vpk:>4} en VPK{C.RESET} | "
                f"{miss_col}{n_miss:>4} TRULY MISSING{C.RESET}")
        lines.append(f"{C.CYAN}│{C.RESET} {line}")
    lines.append(f"{C.CYAN}└{bar}┘{C.RESET}\n")
    return "\n".join(lines)

def export_all(data, gameSrc, dest, fs):
    base, dest = Path(gameSrc), Path(dest)
    total = sum(len(data[c]) for c in ("materials", "textures", "models", "sounds", "lua", "txt", "sprites"))
    done, failed = 0, 0

    for cat in ("materials", "textures", "models", "sounds", "lua", "txt", "sprites"):
        for rel_path in data[cat]:
            actual = fs.resolve(rel_path)
            done += 1
            if actual:
                if not copy_file(actual, dest / rel_path):
                    failed += 1
            else:
                failed += 1
            if done % 200 == 0 or done == total:
                cprint(f"  Copie... {done}/{total}", C.DIM, end="\r")

    for mdl_rel, companions in data["model_companions"].items():
        for comp in companions:
            try:
                rel = comp.relative_to(base)
                copy_file(comp, dest / rel)
            except ValueError: pass

    print()
    return failed

def print_missing_details(data):
    tracker = data["tracker"]
    cprint("\n--- DÉTAIL DES RESSOURCES TOTALEMENT MANQUANTES (TRULY MISSING) ET LEUR PROVENANCE ---", C.RED, bold=True)
    mapping = [
        ("missing_materials", "material"), ("missing_textures", "texture"),
        ("missing_models", "model"), ("missing_sounds", "sound"),
        ("missing_lua", "lua"), ("missing_txt", "script_txt"),
        ("missing_sprites", "sprite"),
    ]
    for cat_key, item_type in mapping:
        for item in sorted(data[cat_key]):
            sources = tracker.get_sources(item_type, item)
            cprint(f"[TRULY MISSING] {item_type}: {item}", C.RED)
            print(f"  └─ Demandé par : {C.GREY}{sources}{C.RESET}")

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
    cprint(f"CSV écrit : {csv_path}", C.GREEN)

def main():
    parser = argparse.ArgumentParser(description="Exporte les dépendances d'une map VMF avec suivi de provenance")
    parser.add_argument("-source", required=True, help="Chemin du fichier .vmf")
    parser.add_argument("-gameSrc", required=True, help="Dossier garrysmod source")
    parser.add_argument("-dest", required=True, help="Dossier custom de sortie")
    parser.add_argument("-scan", action="store_true", help="Analyse seulement, ne copie rien")
    parser.add_argument("-force", action="store_true", help="Ne pas demander de confirmation avant de copier")
    parser.add_argument("-missing", action="store_true", help="Affiche le détail des ressources manquantes avec leur provenance")
    parser.add_argument("-csv", metavar="FICHIER", help="Écrit un rapport CSV détaillé vers ce fichier")
    parser.add_argument("-Threads", "-threads", dest="threads", type=int, default=1, metavar="NUM",
                         help="Nombre de threads pour la résolution récursive (I/O disque parallèle -- utile sur les grosses maps). Défaut: 1 (séquentiel)")
    parser.add_argument("-noColor", action="store_true", help="Désactive la sortie en couleur")
    args = parser.parse_args()

    if args.noColor:
        os.environ["NO_COLOR"] = "1"
        C.ON = False
        for attr in ("RESET", "BOLD", "DIM", "GREEN", "AMBER", "CYAN", "RED", "MAGENTA", "GREY"):
            setattr(C, attr, "")

    print_banner(args.threads)

    vmf_path = Path(args.source)
    if not vmf_path.exists():
        cprint(f"Fichier VMF introuvable : {vmf_path}", C.RED, bold=True)
        sys.exit(1)

    vmf_text = vmf_path.read_text(encoding="utf-8", errors="ignore")

    cprint("[1/3] Indexation du disque dur local...", C.CYAN, bold=True)
    fs = GameFS(args.gameSrc)
    for sub in ("materials", "models", "sound", "particles", "scripts", "lua", "maps", "sprites"):
        fs.index_subtree(sub)

    cprint("[2/3] Indexation des VPK du jeu et des jeux montés...", C.CYAN, bold=True)
    vpk_fs = VPKFS()
    vpk_fs.scan_game_vpks(args.gameSrc)

    cprint(f"[3/3] Résolution récursive des dépendances ({args.threads} thread{'s' if args.threads > 1 else ''})...", C.CYAN, bold=True)
    t0 = time.time()
    data = resolve_all(vmf_text, vmf_path.stem, args.gameSrc, fs, vpk_fs, threads=args.threads)
    elapsed = time.time() - t0
    cprint(f"  Résolution terminée en {elapsed:.1f}s", C.DIM)

    print(build_report(data))

    if args.missing:
        print_missing_details(data)

    if args.csv:
        write_csv(data, args.csv)

    if args.scan:
        return

    if not args.force:
        ans = input(f"{C.AMBER}Procéder à l'export ? [Y/N] {C.RESET}")
        if ans.strip().lower() != "y": return

    cprint("Copie des fichiers locaux en cours...", C.CYAN)
    failed = export_all(data, args.gameSrc, args.dest, fs)
    if failed:
        cprint(f"Export terminé avec {failed} erreur(s) de copie.", C.AMBER, bold=True)
    else:
        cprint("Export terminé avec succès.", C.GREEN, bold=True)

if __name__ == "__main__":
    main()

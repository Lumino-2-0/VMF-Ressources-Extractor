#!/usr/bin/env python3
# VMF Dependency Exporter (GMod/Source SDK 2013)

import os
import re
import sys
import shutil
import argparse
from pathlib import Path

VMF_KEY_PATTERNS = {
    "material": re.compile(r'"material"\s+"([^"]+)"'),
    "model": re.compile(r'"model"\s+"([^"]+)"'),
    "sound": re.compile(r'"message"\s+"([^"]+)"'),
    "soundscape": re.compile(r'"soundscape"\s+"([^"]+)"'),
    "particle": re.compile(r'"effect_name"\s+"([^"]+)"'),
    "decal": re.compile(r'"texture"\s+"([^"]+)"'),
}

VMT_TEXTURE_KEYS = [
    "$basetexture",
    "$bumpmap",
    "$detail",
    "$envmap",
    "$envmapmask",
    "$selfillummask",
    "$lightwarptexture",
]

def read_file_safe(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except:
        return ""

def find_all(pattern, text):
    return pattern.findall(text)

def vmf_parse(vmf_text):
    found = {
        "materials": set(),
        "models": set(),
        "sounds": set(),
        "particles": set(),
        "decals": set()
    }

    for key, pat in VMF_KEY_PATTERNS.items():
        for match in find_all(pat, vmf_text):
            if key == "material":
                found["materials"].add(match)
            elif key == "model":
                found["models"].add(match)
            elif key == "sound":
                found["sounds"].add(match)
            elif key == "soundscape":
                found["sounds"].add(match)
            elif key == "particle":
                found["particles"].add(match)
            elif key == "decal":
                found["decals"].add(match)

    return found


def vmt_parse(vmt_path):
    deps = set()
    content = read_file_safe(vmt_path)

    for key in VMT_TEXTURE_KEYS:
        pattern = re.compile(rf'\{key}"\s+"([^"]+)"')
        for m in pattern.findall(content):
            if m:
                deps.add(m)

    return deps


def resolve_vmt_to_textures(material, game_path):
    mat_path = Path(game_path) / "materials" / (material + ".vmt")
    if not mat_path.exists():
        return []

    textures = []
    deps = vmt_parse(mat_path)

    for tex in deps:
        textures.append(tex + ".vtf")

    return textures


def copy_file(src, dest):
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.copy2(src, dest)
            return True
    except:
        pass
    return False


def collect_files(vm, gamePath):
    results = {
        "materials": set(),
        "models": set(),
        "textures": set(),
        "sounds": set(),
        "particles": set(),
        "missing": set()
    }

    for mat in vm["materials"]:
        results["materials"].add(mat)
        texs = resolve_vmt_to_textures(mat, gamePath)
        for t in texs:
            results["textures"].add(t)

    for mdl in vm["models"]:
        results["models"].add(mdl)

    for snd in vm["sounds"]:
        results["sounds"].add(snd)

    for p in vm["particles"]:
        results["particles"].add(p + ".pcf")

    return results


def build_report(data):
    total = sum(len(v) for v in data.values())
    return f"""
Models     : {len(data['models'])}
Materials  : {len(data['materials'])}
Textures   : {len(data['textures'])}
Sounds     : {len(data['sounds'])}
Particles  : {len(data['particles'])}

Total      : {total}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-source", required=True)
    parser.add_argument("-gameSrc", required=True)
    parser.add_argument("-dest", required=True)
    parser.add_argument("-scan", action="store_true")
    parser.add_argument("-force", action="store_true")
    parser.add_argument("-csv", action="store_true")
    parser.add_argument("-missing", action="store_true")

    args = parser.parse_args()

    vmf_text = read_file_safe(args.source)
    vm = vmf_parse(vmf_text)
    data = collect_files(vm, args.gameSrc)

    print(build_report(data))

    if args.scan:
        return

    if not args.force:
        ans = input("Proceed export ? [Y/N] ")
        if ans.lower() != "y":
            return

    base = Path(args.dest)

    def export(category, root):
        for item in data[category]:
            if category == "materials":
                src = Path(args.gameSrc) / "materials" / (item + ".vmt")
                dst = root / "materials" / (item + ".vmt")
            elif category == "textures":
                src = Path(args.gameSrc) / "materials" / item
                dst = root / "materials" / item
            elif category == "models":
                src = Path(args.gameSrc) / item
                dst = root / item
            elif category == "sounds":
                src = Path(args.gameSrc) / "sound" / item
                dst = root / "sound" / item
            elif category == "particles":
                src = Path(args.gameSrc) / "particles" / item
                dst = root / "particles" / item
            else:
                continue

            if not copy_file(src, dst):
                if args.missing:
                    print("MISSING:", src)

    export("materials", base)
    export("textures", base)
    export("models", base)
    export("sounds", base)
    export("particles", base)


if __name__ == "__main__":
    main()
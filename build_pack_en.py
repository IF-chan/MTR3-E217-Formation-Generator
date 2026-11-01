#!/usr/bin/env python3
from __future__ import annotations
import argparse
import copy
import json
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "output"
PRESETS_FILE = ROOT / "presets_cn.json"

LIVERIES = {
    "yokosuka": {"texture": ROOT / "base_pack/assets/mtr/e217/e217r.png", "name": "E217 Yokosuka Line", "color": "0033FF", "series": "E217"},
    "shonan": {"texture": ROOT / "base_pack/assets/mtr/e217/e217t.png", "name": "E217 Shōnan–Shinjuku Line", "color": "FF0000", "series": "E217"},
}
ALIASES = {"1":"yokosuka", "2":"shonan", "yokosuka":"yokosuka", "shonan":"shonan"}

@dataclass(frozen=True)
class CarSpec:
    token: str
    role: str
    pantograph: str = "none"  # none/front/rear/both
    label: str = ""

# F = toward TCF side (model -Z); R = toward TCB side (model +Z).
TOKEN_SPECS = {
 "TCF": CarSpec("TCF", "1", "none", "Front cab car"),
 "TCB": CarSpec("TCB", "6", "none", "Rear cab car"),
 "T": CarSpec("T", "2", "none", "Standard intermediate car without pantograph"),
 "M": CarSpec("M", "2", "none", "Standard motor car without pantograph (same appearance as T)"),
 "TS": CarSpec("TS", "4", "none", "Semi-cross seating car without pantograph"),
 "MS": CarSpec("MS", "4", "none", "Semi-cross seating motor car without pantograph (same appearance as TS)"),
}
for prefix, role in (("MP", "3"), ("MPS", "5")):
 for suffix, mode, label in (("1F","front","single front pantograph"),("1R","rear","single rear pantograph"),("2","both","dual pantographs")):
  token=prefix+suffix
  TOKEN_SPECS[token]=CarSpec(token, role, mode, ("Standard interior, " if role=="3" else "Semi-cross seating interior, ")+label)
TOKEN_ALIASES = {"MP":"MP2", "M'":"MP2", "MPS":"MPS2", "MP1":"MP1F", "MPS1":"MPS1F"}
VALID_TOKENS = list(TOKEN_SPECS)


def load_presets() -> dict:
    if not PRESETS_FILE.exists():
        return {}
    data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
    return data.get("presets", data)


def sanitize_id(value: str) -> str:
    value = value.strip().lower().replace(" ", "_")
    value = re.sub(r"[^a-z0-9_.-]", "_", value)
    value = re.sub(r"_+", "_", value).strip("_.-")
    if not value:
        raise ValueError("Resource-pack/train ID cannot be empty")
    return value


def normalize_token(token: str) -> str:
    t = token.strip().upper().replace("’", "'").replace("′", "'")
    # normalize apostrophe alias separately
    if t == "M'": return "MP2"
    t = TOKEN_ALIASES.get(t, t)
    if t not in TOKEN_SPECS:
        raise ValueError(f"Unknown car token {token!r}. Valid: {','.join(VALID_TOKENS)} (MP/M' alias MP2)")
    return t


def parse_formation(text: str) -> list[CarSpec]:
    raw = [x.strip() for x in text.split(",")]
    if not raw:
        raise ValueError("Formation is empty")
    return [TOKEN_SPECS[normalize_token(t)] for t in raw]


def reverse_formation(specs: list[CarSpec]) -> list[CarSpec]:
    swap = {"TCF":"TCB", "TCB":"TCF", "MPS1F":"MPS1R", "MPS1R":"MPS1F", "MP1F":"MP1R", "MP1R":"MP1F"}
    result=[]
    for spec in reversed(specs):
        token = swap.get(spec.token, spec.token)
        result.append(TOKEN_SPECS[token])
    return result


def canonical_formation(specs: list[CarSpec]) -> str:
    return ",".join(s.token for s in specs)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def descendants(group: dict) -> list[str]:
    result=[]
    for child in group.get("children", []):
        if isinstance(child, str):
            result.append(child)
        elif isinstance(child, dict):
            result.extend(descendants(child))
    return result


def merge_models(specs):
    return load_json(ROOT / "base_pack/assets/mtr/e217/e217.bbmodel")


def keep_pantograph_part(part: dict, mode: str) -> bool:
    if part.get("name") != "pantograph":
        return True
    if mode == "both":
        return True
    if mode == "none":
        return False
    # Current tested template has one non-mirrored panto at Z=-144 and one mirrored at Z=+144.
    mirror = bool(part.get("mirror", False))
    positions = part.get("positions", [])
    z = positions[0][1] if positions and len(positions[0]) >= 2 else 0
    if mode == "front":
        return (not mirror) and z <= 0
    if mode == "rear":
        return mirror and z >= 0
    return True


def build_properties(specs):
    source=load_json(ROOT / "templates/source_properties.json")
    out={k:copy.deepcopy(v) for k,v in source.items() if k != "parts"}
    numbered=[]
    for car_no,spec in enumerate(specs,1):
        template=[p for p in source["parts"] if p.get("whitelisted_cars")==spec.role]
        if not template: raise ValueError("Missing car template: "+spec.role)
        for part in template:
            if keep_pantograph_part(part,spec.pantograph):
                p=copy.deepcopy(part); p["whitelisted_cars"]=str(car_no); numbered.append(p)
    globals_=[copy.deepcopy(p) for p in source["parts"] if not p.get("whitelisted_cars") and not p.get("blacklisted_cars")]
    # Retain shared bogies once; original ordering is verified by test_generator.py.
    if not source["parts"][0].get("whitelisted_cars"):
        out["parts"]=globals_+numbered
    else:
        out["parts"]=numbered+globals_
    return out


def validate_formation(specs: list[CarSpec]) -> list[str]:
    warnings=[]
    if len(specs) < 2:
        warnings.append("The formation has fewer than 2 cars and is generally unsuitable for use as a train.")
    if specs and specs[0].token not in ("TCF",):
        warnings.append("The first car is not TCF; the generator allows this, but the front cab car should normally be first.")
    if specs and specs[-1].token not in ("TCB",):
        warnings.append("The last car is not TCB; the generator allows this, but the rear cab car should normally be last.")
    for i,s in enumerate(specs[1:-1], start=2):
        if s.token in ("TCF","TCB"):
            warnings.append(f"Car {i} uses the {s.token} cab car model.")
    return warnings


def copy_common_assets(pack_root, train_id, texture, include_sound=True):
    shutil.copytree(ROOT / "base_pack", pack_root, dirs_exist_ok=True)
    asset_dir=pack_root / "assets/mtr" / train_id
    asset_dir.mkdir(parents=True,exist_ok=True)
    shutil.copy2(texture,asset_dir / "texture.png")
    for path in (ROOT / "base_pack/assets/mtr/e217").glob("r103_connector_*.png"):
        shutil.copy2(path,asset_dir/path.name)
    if not include_sound:
        shutil.rmtree(pack_root / "assets/mtr/sounds")
        (pack_root / "assets/mtr/sounds.json").unlink()
    return asset_dir


def zip_directory(root: Path, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(root).as_posix())


def build_pack(livery: str, formation: str|None, train_id: str, output: Path, name: str|None=None, color: str|None=None,
               description: str|None=None, pack_format: int=15, preset_id: str|None=None, reverse: bool=False,
               include_sound: bool=True):
    if pack_format < 1: raise ValueError("pack-format must be a positive integer")
    presets=load_presets()
    preset=None
    if preset_id:
        if preset_id not in presets:
            raise ValueError(f"Unknown preset {preset_id!r}. Use --list-presets")
        preset=presets[preset_id]
        livery=preset["livery"]
        formation=preset["formation"]
    livery=ALIASES.get((livery or "").strip().lower(), (livery or "").strip().lower())
    if livery not in LIVERIES:
        raise ValueError("Unknown livery. Use yokosuka or shonan")
    if not formation:
        raise ValueError("Formation is required")
    specs=parse_formation(formation)
    if reverse:
        specs=reverse_formation(specs)
    canonical=canonical_formation(specs)
    warnings=validate_formation(specs)
    if preset:
        warnings.extend(preset.get("limitations", []))
    train_id=sanitize_id(train_id)
    liv=LIVERIES[livery]
    default_title = preset.get("name") if preset else f"{liv['name']} ({len(specs)}-car custom consist)"
    name=name or default_title
    color=(color or liv["color"]).replace("#", "").upper()
    if not re.fullmatch(r"[0-9A-F]{6}", color):
        raise ValueError("Color must be a 6-digit RGB hex value, e.g. 003FFF")
    description=description or f"{liv['series']} formation: {canonical} | generated for MTR 3.2.2"

    model=merge_models(specs)
    props=build_properties(specs)
    panto_count=sum(2 if s.pantograph=="both" else 1 if s.pantograph in ("front","rear") else 0 for s in specs)
    formation_report={
        "generator_version": "1.1",
        "preset": preset_id,
        "preset_note": preset.get("note", "") if preset else "",
        "reference_direction": preset.get("direction", "") if preset else "",
        "sources": preset.get("sources", []) if preset else [],
        "livery": livery,
        "series": liv["series"],
        "train_id": train_id,
        "name": name,
        "cars": len(specs),
        "formation": canonical,
        "reversed": bool(reverse),
        "pantographs": panto_count,
        "car_details": [
            {"car":i, "token":s.token, "role":s.role, "pantograph":s.pantograph, "label":s.label}
            for i,s in enumerate(specs,1)
        ],
        "warnings": warnings,
    }
    if preset and preset.get("real_cars"):
        real_cars=copy.deepcopy(preset["real_cars"])
        if reverse: real_cars.reverse()
        for detail, real_car in zip(formation_report["car_details"], real_cars):
            detail["real_car"]=real_car
    with tempfile.TemporaryDirectory(prefix="e217_mtr3_") as td:
        root=Path(td)
        asset_dir=copy_common_assets(root, train_id, liv["texture"], include_sound)
        (asset_dir / "model_properties.json").write_text(json.dumps(props,ensure_ascii=False,indent=2),encoding="utf-8")
        train_def={
            "name": name,
            "description": description,
            "wikipedia_article": "e217_series",
            "color": color,
            "gangway_connection_id": f"mtr:{train_id}/r103",
            "train_barrier_id": "",
            "door_animation_type": "R179",
            "render_door_overlay": False,
            "rider_offset": 0.0,
            "texture_id": f"mtr:{train_id}/texture",
            "model": "mtr:e217/e217.bbmodel",
            "model_properties": f"mtr:{train_id}/model_properties.json",
        }
        if include_sound:
            train_def["bve_sound_base_id"]="e217"
        custom={"custom_trains": {train_id: train_def}}
        mtr=root / "assets" / "mtr"
        (mtr / "mtr_custom_resources.json").write_text(json.dumps(custom,ensure_ascii=False,indent=2),encoding="utf-8")
        pack_mcmeta={"pack":{"pack_format":int(pack_format),"description":f"{name} - generated for MTR 3.2.2"}}
        (root / "pack.mcmeta").write_text(json.dumps(pack_mcmeta,ensure_ascii=False,indent=2),encoding="utf-8")
        (root / "formation.json").write_text(json.dumps(formation_report,ensure_ascii=False,indent=2),encoding="utf-8")
        (root / "FORMATION.txt").write_text(
            (f"Preset note: {preset.get('note', '')}\n" if preset else "") +
            f"{name}\nID: {train_id}\nLivery: {livery}\nFormation: {canonical}\nCars: {len(specs)}\nPantographs: {panto_count}\n" +
            ("Warnings:\n- " + "\n- ".join(warnings) + "\n" if warnings else ""), encoding="utf-8")
        zip_directory(root, output)
    return {"output": output, "cars": len(specs), "parts": len(props["parts"]), "groups": len(model.get("outliner",[])),
            "elements": len(model.get("elements",[])), "id":train_id, "name":name, "formation":canonical,
            "pantographs":panto_count, "warnings":warnings, "preset":preset_id}


def print_tokens():
    print("\nAvailable car tokens:")
    for k in VALID_TOKENS:
        s=TOKEN_SPECS[k]
        print(f"  {k:<5} {s.label}")
    print("  MP / M'  = compatibility aliases for MP2")
    print("MP1F/MP1R orientation: F is toward the front of the train, R is toward the rear.")


def print_presets():
    presets=load_presets()
    print("\nBuilt-in/editable presets:")
    for pid,p in presets.items():
        print(f"  {pid:<22} {p.get('name',pid)}")
        print(f"    {p['livery']}: {p['formation']}")
        if p.get("note"): print("    " + p["note"])


def interactive():
    presets=load_presets()
    print("\nMTR3 E217 Formation & Resource-Pack Generator")
    print("-----------------------------------")
    preset_keys=list(presets)
    print("0) Custom formation")
    for i,pid in enumerate(preset_keys,1):
        print(f"{i}) {presets[pid].get('name',pid)}")
    choice=input("Select a preset, or 0 for a custom formation [0]: ").strip() or "0"
    preset_id=None
    if choice.isdigit() and 1 <= int(choice) <= len(preset_keys):
        preset_id=preset_keys[int(choice)-1]
        p=presets[preset_id]
        livery=p["livery"]
        formation=p["formation"]
        print(f"Preset formation: {formation}")
        if p.get("note"): print(p["note"])
    else:
        print("\n1) Yokosuka·Sōbu Rapid Line\n2) Shōnan livery")
        livery=input("Select a livery [1/2]: ").strip() or "1"
        livery=ALIASES.get(livery.lower(), livery.lower())
        print_tokens()
        formation=input("Enter a formation, e.g. TCF,M,MP2,TCB: ").strip()
        if not formation: raise ValueError("Formation cannot be empty")
    specs=parse_formation(formation)
    default_id=f"e217_{livery}_{len(specs)}car_{preset_id or 'custom'}"
    train_id=input(f"Resource ID [{default_id}]: ").strip() or default_id
    custom_name=input("Display name [press Enter for default]: ").strip() or None
    rev=(input("Reverse the entire train orientation? [y/N]: ").strip().lower() in ("y","yes","1","\u662f"))
    out_name=input(f"Output ZIP filename [{train_id}.zip]: ").strip() or f"{train_id}.zip"
    if not out_name.lower().endswith(".zip"): out_name += ".zip"
    return livery,formation,train_id,DEFAULT_OUTPUT_DIR/out_name,custom_name,preset_id,rev


def main():
    ap=argparse.ArgumentParser(description="Generate a complete MTR3 E217 resource-pack ZIP")
    ap.add_argument("--preset", help="Preset ID from presets.json")
    ap.add_argument("--list-presets", action="store_true")
    ap.add_argument("--list-tokens", action="store_true")
    ap.add_argument("--livery", choices=["yokosuka","shonan"])
    ap.add_argument("--formation", help="Comma separated detailed car tokens")
    ap.add_argument("--reverse", action="store_true", help="Reverse consist and swap cab/single-panto orientation")
    ap.add_argument("--id", dest="train_id", help="MTR custom train/resource ID")
    ap.add_argument("--name", help="Display name")
    ap.add_argument("--color", help="6-digit RGB hex without #")
    ap.add_argument("--description")
    ap.add_argument("--pack-format", type=int, default=15, help="Minecraft 1.20/1.20.1 = 15")
    ap.add_argument("--no-sound", action="store_true", help="Do not bundle/register E217 SiC BVE sounds")
    ap.add_argument("--force", action="store_true", help="Overwrite an existing output ZIP")
    ap.add_argument("--output", help="Output ZIP path; defaults to output/<id>.zip")
    args=ap.parse_args()
    try:
        if args.list_presets:
            print_presets(); return
        if args.list_tokens:
            print_tokens(); return
        if len(sys.argv) == 1:
            livery,formation,train_id,output,name,preset_id,reverse=interactive()
            color=None; description=None; pack_format=15; include_sound=True
        else:
            if args.preset and args.formation: raise ValueError("--preset and --formation cannot be used together")
            preset_id=args.preset
            presets=load_presets()
            if preset_id:
                if preset_id not in presets: raise ValueError(f"Unknown preset {preset_id!r}; use --list-presets")
                livery=presets[preset_id]["livery"]
                formation=presets[preset_id]["formation"]
            else:
                if not args.livery or not args.formation:
                    ap.error("custom CLI mode requires --livery and --formation (or use --preset)")
                livery=args.livery; formation=args.formation
            specs=parse_formation(formation)
            default_id=f"e217_{livery}_{len(specs)}car_{preset_id or 'custom'}"
            train_id=args.train_id or default_id
            output=Path(args.output) if args.output else DEFAULT_OUTPUT_DIR/f"{sanitize_id(train_id)}.zip"
            name,color,description,pack_format=args.name,args.color,args.description,args.pack_format
            reverse=args.reverse; include_sound=not args.no_sound
        if Path(output).exists() and not args.force:
            raise ValueError("Output file already exists; choose another name or use --force to overwrite")
        if Path(output).suffix.lower() != ".zip":
            raise ValueError("Output must be a .zip file")
        info=build_pack(livery,formation,train_id,Path(output),name,color,description,pack_format,preset_id,reverse,include_sound)
        print("\nGeneration complete:")
        print(f"  {info['output']}")
        print(f"  ID: {info['id']}")
        print(f"  Formation: {info['formation']}")
        print(f"  Cars: {info['cars']} | Pantographs: {info['pantographs']}")
        print(f"  Properties: {info['parts']} entries")
        print(f"  Model: {info['groups']} groups / {info['elements']} elements")
        if info['warnings']:
            print("  Warnings:")
            for w in info['warnings']: print(f"    - {w}")
        print("\nPlace the ZIP in resourcepacks and enable it; the in-game car count must match the generated formation.")
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One-command AC1 Xbox 360 Czech rebuild from user-supplied clean files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ac1_build_all_mgb_archive import decode_pc, decode_xbox_prefix
from ac1_build_complete_text import forge_entry_payload
from ac1_forge import parse_forge


ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
BIN = ROOT / "bin"
CONFIG = ROOT / "config"
THIRD_PARTY = ROOT / "third_party"


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().lower()


def resolve_executable(value: str, label: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    discovered = shutil.which(value)
    if discovered:
        return Path(discovered).resolve()
    raise FileNotFoundError(f"{label} nebyl nalezen: {value}")


def require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"{label} nebyl nalezen: {path}")
    return path


class Builder:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.xbox = args.xbox_dir.resolve()
        self.pc = args.pc_dir.resolve()
        self.output = args.output.resolve()
        self.build = self.output / "_build"
        self.ready = self.output / "Ready_To_Copy"
        self.reports = self.build / "reports"
        self.cache = self.build / "audio_cache"
        self.text = self.build / "text"
        self.audio = self.build / "audio"
        self.steps = self.build / "steps"
        self.log_path = self.build / "build.log"
        self.recipe = json.loads(args.recipe.read_text(encoding="utf-8"))
        self.final_manifest = json.loads(
            args.final_manifest.read_text(encoding="utf-8")
        )
        self.quickbms = resolve_executable(str(args.quickbms), "QuickBMS")
        self.decoder = resolve_executable(str(args.decoder), "Xbox dekodér")
        self.ffmpeg = resolve_executable(args.ffmpeg, "FFmpeg")
        self.xma2encode = resolve_executable(args.xma2encode, "xma2encode")
        self.pc_raw = require_file(args.pc_raw.resolve(), "PC BMS skript")
        self.compress = require_file(
            args.compress_script.resolve(), "XMem/LZX kompresní skript"
        )
        self.language_name = "Data360_StreamedSoundsEng.forge"
        self.pc_language_name = "DataPC_StreamedSoundscze.forge"

    def validate(self) -> None:
        if not self.xbox.is_dir():
            raise FileNotFoundError(f"Xbox adresář neexistuje: {self.xbox}")
        if not self.pc.is_dir():
            raise FileNotFoundError(f"PC adresář neexistuje: {self.pc}")
        if (
            self.output.exists()
            and any(self.output.iterdir())
            and not self.args.resume
        ):
            raise FileExistsError(
                f"Výstupní adresář není prázdný: {self.output}. "
                "Použijte nový adresář."
            )

        selected = self.recipe["files"]
        if self.args.stop_after == "core":
            core_names = {
                "Data360.forge",
                "Data360_Map_Menu.forge",
                "Data360_Present_Room.forge",
            }
            selected = [item for item in selected if item["xbox"] in core_names]

        for spec in selected:
            require_file(self.xbox / spec["xbox"], "Xbox vstup")
            require_file(self.pc / spec["pc"], "PC vstup")
        if self.args.stop_after != "core":
            require_file(
                self.pc / self.pc_language_name, "česká PC zvuková banka"
            )

        # Parse every Xbox FORGE before the long audio conversion starts.
        for spec in selected:
            parse_forge(self.xbox / spec["xbox"])

    def plan(self) -> dict[str, object]:
        return {
            "mode": "plan-only" if self.args.plan_only else "build",
            "xbox": str(self.xbox),
            "pc": str(self.pc),
            "output": str(self.output),
            "files": len(self.recipe["files"]),
            "steps": [
                "kontrola 16 Xbox a odpovídajících PC archivů",
                "bezpečný slot-stable převod textu a GUI",
                "sestavení celé české diakritiky v Xbox fontech",
                "převod dabingu po jednotlivých oblastech",
                "obnovení nelokalizovaných efektů v localized-only režimu",
                "externalizace pěti dlouhých replik v Šalamounově chrámu",
                "časová osa Animu a potvrzení ukončení",
                "závěrečná FORGE a hashová kontrola",
            ],
        }

    def prepare_output(self) -> None:
        for directory in (
            self.output,
            self.build,
            self.ready,
            self.reports,
            self.cache,
            self.text,
            self.audio,
            self.steps,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.args.resume or not self.log_path.exists():
            self.log_path.write_text("", encoding="utf-8")

    def log(self, message: str) -> None:
        print(message, flush=True)
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")

    def run(self, label: str, command: list[object]) -> None:
        rendered = subprocess.list2cmdline([str(item) for item in command])
        self.log(f"\n=== {label} ===\n{rendered}")
        with self.log_path.open("a", encoding="utf-8") as log:
            completed = subprocess.run(
                [str(item) for item in command],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Krok '{label}' selhal (exit {completed.returncode}). "
                f"Viz {self.log_path}"
            )

    def core_ui(self) -> None:
        self.log("\n[1/7] Základní konzolové GUI")
        for spec in self.recipe["files"]:
            source = self.xbox / spec["xbox"]
            if source.exists():
                shutil.copy2(source, self.text / spec["xbox"])

        # Map_Menu is the one small archive whose rebuilt FILEDATA payload is
        # intentionally appended.  Its original 902 KiB slot cannot contain
        # the complete Czech MGB set, while the append/index mode was accepted
        # by the game and retains the untouched original physical copy.
        map_rebuilt = self.all_mgb(
            self.text / "Data360_Map_Menu.forge",
            self.pc / "DataPC_Map_Menu.forge",
            ["Map_Menu"],
            "core_map_menu",
            append=True,
        )
        shutil.copy2(map_rebuilt, self.text / "Data360_Map_Menu.forge")

    def all_mgb(
        self,
        source: Path,
        pc_forge: Path,
        entries: list[str],
        step_name: str,
        *,
        template: Path | None = None,
        template_entry: str | None = None,
        resources: list[str] | None = None,
        optimize: bool = False,
        append: bool = False,
    ) -> Path:
        destination = self.steps / step_name
        destination.mkdir(exist_ok=self.args.resume)
        result = destination / source.name
        if self.args.resume and result.is_file():
            self.log(f"PŘESKOČENO (hotovo): {step_name}")
            return result
        command: list[object] = [
            sys.executable,
            TOOLS / "ac1_build_all_mgb_archive.py",
            "--source-forge",
            source,
            "--pc-forge",
            pc_forge,
        ]
        for entry in entries:
            command += ["--entry-name", entry]
        command += [
            "--output",
            destination,
            "--decoder",
            self.decoder,
            "--quickbms",
            self.quickbms,
            "--pc-raw-script",
            self.pc_raw,
            "--compress-script",
            self.compress,
        ]
        command.append("--append" if append else "--slot-stable")
        if template and template_entry:
            command += [
                "--template-forge",
                template,
                "--template-entry-name",
                template_entry,
            ]
        for resource in resources or []:
            command += ["--resource-name", resource]
        if optimize:
            command += [
                "--optimize-all-chunks",
                "--optimize-unchanged-limit",
                "1",
            ]
        self.run(f"Text: {source.name} ({step_name})", command)
        require_file(result, "výsledek textového kroku")
        return result

    def text_and_fonts(self) -> None:
        self.log("\n[2/7] Kompletní texty")
        talal_template = self.xbox / "Data360_Assassination_Talal.forge"

        # Bootstrap was the most offset-sensitive archive in testing.  Keep
        # the proven three fixed-extent passes instead of one oversized pass.
        data360 = self.text / "Data360.forge"
        data360 = self.all_mgb(
            data360,
            self.pc / "DataPC.forge",
            ["Game Bootstrap Settings"],
            "bootstrap_popup",
            resources=["PopUpTutorial _MGB"],
            optimize=True,
        )
        data360 = self.all_mgb(
            data360,
            self.pc / "DataPC.forge",
            ["Game Bootstrap Settings"],
            "bootstrap_all",
            optimize=True,
        )
        data360 = self.all_mgb(
            data360,
            self.pc / "DataPC.forge",
            ["Game Bootstrap Settings"],
            "bootstrap_stabilize",
            template=data360,
            template_entry="Game Bootstrap Settings",
            optimize=True,
        )
        shutil.copy2(data360, self.text / "Data360.forge")

        for spec in self.recipe["files"]:
            name = spec["xbox"]
            entries = spec["text_entries"]
            if not entries or name in (
                "Data360.forge",
                "Data360_Map_Menu.forge",
            ):
                continue
            source = (
                self.text / name
                if (self.text / name).exists()
                else self.xbox / name
            )
            if spec.get("externalize_solomon"):
                optimized = self.all_mgb(
                    source,
                    self.pc / spec["pc"],
                    entries,
                    "text_solomon_optimize",
                    template=talal_template,
                    template_entry="Cell01364_DataBlock",
                    optimize=True,
                )
                rebuilt = self.all_mgb(
                    optimized,
                    self.pc / spec["pc"],
                    entries,
                    f"text_{Path(name).stem}",
                    template=talal_template,
                    template_entry="Cell01364_DataBlock",
                )
            else:
                rebuilt = self.all_mgb(
                    source,
                    self.pc / spec["pc"],
                    entries,
                    f"text_{Path(name).stem}",
                    template=talal_template,
                    template_entry="Cell01364_DataBlock",
                )
            shutil.copy2(rebuilt, self.text / name)

        for spec in self.recipe["files"]:
            source = self.xbox / spec["xbox"]
            destination = self.text / spec["xbox"]
            if not destination.exists():
                shutil.copy2(source, destination)

        self.log("\n[3/7] České konzolové fonty")
        font_root = self.steps / "font_decode"
        font_output = self.steps / "font_output" / "Data360.forge"
        if self.args.resume and font_output.is_file():
            self.log("PRESKOCENO (hotovo): ceske fonty")
            shutil.copy2(font_output, self.text / "Data360.forge")
            return
        font_root.mkdir(exist_ok=self.args.resume)
        font_output.parent.mkdir(exist_ok=self.args.resume)
        current = self.text / "Data360.forge"
        current_decoded, _rc, _out = decode_xbox_prefix(
            forge_entry_payload(current, "Game Bootstrap Settings"),
            self.decoder,
            font_root,
            "current_font",
        )
        clean_decoded, _rc, _out = decode_xbox_prefix(
            forge_entry_payload(
                self.xbox / "Data360.forge", "Game Bootstrap Settings"
            ),
            self.decoder,
            font_root,
            "clean_font",
        )
        self.run(
            "České fonty",
            [
                sys.executable,
                TOOLS / "ac1_build_xbox_czech_fonts.py",
                "--xbox-directory",
                current_decoded / "0.dat",
                "--xbox-data",
                current_decoded / "1.dat",
                "--clean-font-directory",
                clean_decoded / "0.dat",
                "--clean-font-data",
                clean_decoded / "1.dat",
                "--source-forge",
                current,
                "--output-forge",
                font_output,
                "--quickbms",
                self.quickbms,
                "--compress-script",
                self.compress,
                "--report",
                self.reports / "fonts.json",
                "--preview",
                self.reports / "fonts.png",
            ],
        )
        shutil.copy2(font_output, current)

    def audio_area(
        self,
        spec: dict[str, object],
        language_source: Path,
        tag: str,
        *,
        localized_only: bool = False,
    ) -> tuple[Path, Path]:
        source = self.text / str(spec["xbox"])
        destination = self.steps / f"audio_{tag}"
        destination.mkdir(exist_ok=self.args.resume)
        area_output = destination / str(spec["xbox"])
        language_output = destination / self.language_name
        if (
            self.args.resume
            and area_output.is_file()
            and language_output.is_file()
        ):
            self.log(f"PŘESKOČENO (hotovo): audio_{tag}")
            return area_output, language_output
        command: list[object] = [
            sys.executable,
            TOOLS / "ac1_build_complete_dub_area.py",
            "--pc-forge",
            self.pc / str(spec["pc"]),
            "--xbox-area-forge",
            source,
            "--pc-language-forge",
            self.pc / self.pc_language_name,
            "--xbox-language-forge",
            language_source,
            "--language-output-forge",
            language_output,
            "--output-area-forge",
            area_output,
            "--decoder",
            self.decoder,
            "--quickbms",
            self.quickbms,
            "--pc-raw-script",
            self.pc_raw,
            "--ffmpeg",
            self.ffmpeg,
            "--xma2encode",
            self.xma2encode,
            "--cache-dir",
            self.cache,
            "--report",
            self.reports / f"audio_{tag}.json",
        ]
        if localized_only:
            command.append("--localized-only")
        else:
            command.append("--consume-all-xbox-variants")
        self.run(f"Dabing: {spec['xbox']}", command)
        return area_output, language_output

    def audio_all(self) -> tuple[Path, Path, Path]:
        if self.args.skip_audio:
            self.log("\n[4/7] Dabing přeskočen parametrem --skip-audio")
            for spec in self.recipe["files"]:
                name = str(spec["xbox"])
                if name != self.language_name:
                    shutil.copy2(self.text / name, self.audio / name)
            language = self.xbox / self.language_name
            shutil.copy2(language, self.audio / self.language_name)
            return (
                self.audio / "Data360.forge",
                self.audio / "Data360_Present_Room.forge",
                self.audio / self.language_name,
            )

        self.log("\n[4/7] Kompletní český dabing")
        specs = {str(item["xbox"]): item for item in self.recipe["files"]}
        base_language = self.xbox / self.language_name

        # Talal's conversion supplies the tested shared language-bank state.
        talal = specs["Data360_Assassination_Talal.forge"]
        talal_area, retained_language = self.audio_area(
            talal, base_language, "talal"
        )
        shutil.copy2(talal_area, self.audio / talal_area.name)

        special = {
            "Data360.forge",
            "Data360_Present_Room.forge",
            "Data360_SolomonTemple.forge",
            "Data360_Assassination_Talal.forge",
            "Data360_StreamedSoundsEng.forge",
            "Data360_Extra.forge",
            "Data360_Map_Menu.forge",
        }
        for name, spec in specs.items():
            if name in special:
                continue
            area, _discarded_language = self.audio_area(
                spec, base_language, Path(name).stem.lower()
            )
            shutil.copy2(area, self.audio / name)

        data360, _ = self.audio_area(
            specs["Data360.forge"],
            retained_language,
            "data360_localized",
            localized_only=True,
        )
        present, _ = self.audio_area(
            specs["Data360_Present_Room.forge"],
            retained_language,
            "present_full",
        )
        solomon, _ = self.audio_area(
            specs["Data360_SolomonTemple.forge"],
            retained_language,
            "solomon_full",
        )
        shutil.copy2(data360, self.audio / data360.name)
        shutil.copy2(present, self.audio / present.name)

        for name in ("Data360_Extra.forge", "Data360_Map_Menu.forge"):
            shutil.copy2(self.text / name, self.audio / name)

        self.log("\n[5/7] Pět dlouhých replik v Šalamounově chrámu")
        external_root = self.steps / "solomon_external_pc"
        external_root.mkdir(exist_ok=self.args.resume)
        solomon_final = self.steps / "solomon_external" / solomon.name
        language_final = self.steps / "solomon_external" / self.language_name
        solomon_final.parent.mkdir(exist_ok=self.args.resume)
        if (
            self.args.resume
            and solomon_final.is_file()
            and language_final.is_file()
        ):
            self.log("PRESKOCENO (hotovo): externalizace peti replik")
            shutil.copy2(solomon_final, self.audio / solomon_final.name)
            shutil.copy2(language_final, self.audio / language_final.name)
            return data360, present, language_final

        cached_pc = external_root / "pc_solomon_decoded" / "pc_solomon"
        if (
            self.args.resume
            and (cached_pc / "0.dat").is_file()
            and (cached_pc / "1.dat").is_file()
        ):
            pc_decoded = cached_pc
        else:
            pc_decoded = decode_pc(
                forge_entry_payload(
                    self.pc / "DataPC_SolomonTemple.forge", "SolomonTemple"
                ),
                self.quickbms,
                self.pc_raw,
                external_root,
                "pc_solomon",
            )
        command: list[object] = [
            sys.executable,
            TOOLS / "ac1_build_dub_externalized_probe.py",
            "--pc-directory",
            pc_decoded / "0.dat",
            "--pc-data",
            pc_decoded / "1.dat",
            "--xbox-language-forge",
            retained_language,
            "--xbox-area-forge",
            solomon,
            "--entry-name",
            "SolomonTemple",
            "--decoder",
            self.decoder,
            "--output-language-forge",
            language_final,
            "--output-area-forge",
            solomon_final,
            "--report",
            self.reports / "solomon_external_five.json",
            "--ffmpeg",
            self.ffmpeg,
        ]
        for resource in self.recipe["solomon_resources"]:
            command += ["--include-resource", resource]
        self.run("Externalizace pěti replik", command)
        shutil.copy2(solomon_final, self.audio / solomon_final.name)
        shutil.copy2(language_final, self.audio / language_final.name)
        return data360, present, language_final

    def patch_resource_name(
        self,
        source: Path,
        output: Path,
        old: str,
        new: str,
        report: Path,
        data_offset: int | None = None,
    ) -> None:
        command: list[object] = [
            sys.executable,
            TOOLS / "ac1_patch_resource_name.py",
            "--source-forge",
            source,
            "--entry-name",
            "Game Bootstrap Settings",
            "--old-name",
            old,
            "--new-name",
            new,
        ]
        if data_offset is not None:
            command += ["--data-offset", hex(data_offset)]
        command += [
            "--decoder",
            self.decoder,
            "--quickbms",
            self.quickbms,
            "--compress-script",
            self.compress,
            "--output-forge",
            output,
            "--report",
            report,
        ]
        self.run(f"Název: {old} -> {new}", command)

    def patch_utf16(
        self,
        source: Path,
        output: Path,
        entry: str,
        resource: str,
        replacements: list[str],
        report: Path,
    ) -> None:
        command: list[object] = [
            sys.executable,
            TOOLS / "ac1_patch_utf16_resource_strings.py",
            "--source-forge",
            source,
            "--entry-name",
            entry,
            "--resource-name",
            resource,
        ]
        for replacement in replacements:
            command += ["--replace", replacement]
        command += [
            "--decoder",
            self.decoder,
            "--quickbms",
            self.quickbms,
            "--compress-script",
            self.compress,
            "--output-forge",
            output,
            "--report",
            report,
        ]
        self.run(f"UTF-16 fallbacky: {resource}", command)

    def final_text_fallbacks(self) -> None:
        self.log("\n[6/7] Konzolové fallbacky a časová osa")
        source = self.audio / "Data360.forge"
        stage1 = self.steps / "fallback_1" / "Data360.forge"
        stage2 = self.steps / "fallback_2" / "Data360.forge"
        stage3 = self.steps / "fallback_3" / "Data360.forge"
        final = self.steps / "fallback_final" / "Data360.forge"
        for path in (stage1, stage2, stage3, final):
            path.parent.mkdir(exist_ok=self.args.resume)
        self.patch_resource_name(
            source,
            stage1,
            "Memory Block 1",
            "Blok paměti 1",
            self.reports / "memory_block_resource.json",
        )
        self.patch_resource_name(
            stage1,
            stage2,
            "Memory Block 1",
            "Blok paměti 1",
            self.reports / "memory_block_animus.json",
            data_offset=7832,
        )
        self.patch_resource_name(
            stage2,
            stage3,
            "Memory Block 1 - Masyaf",
            "Blok paměti 1 - Masyaf",
            self.reports / "memory_block_sequence.json",
            data_offset=36544,
        )
        self.patch_utf16(
            stage3,
            final,
            "Game Bootstrap Settings",
            "Timeline_MGB",
            self.recipe["timeline_replacements"],
            self.reports / "timeline_fallbacks.json",
        )
        shutil.copy2(final, self.audio / "Data360.forge")

        present_source = self.audio / "Data360_Present_Room.forge"
        present_final = (
            self.steps / "present_exit" / "Data360_Present_Room.forge"
        )
        present_final.parent.mkdir(exist_ok=self.args.resume)
        self.patch_utf16(
            present_source,
            present_final,
            "Cell00027_DataBlock",
            "PressStart_MGB",
            self.recipe["exit_replacements"],
            self.reports / "present_exit.json",
        )
        shutil.copy2(present_final, present_source)

    def finalize(self) -> dict[str, object]:
        self.log("\n[7/7] Závěrečná kontrola")
        expected = {item["name"]: item for item in self.final_manifest["files"]}
        results = []
        for name, golden in expected.items():
            source = self.audio / name
            require_file(source, "finální soubor")
            parse_forge(source)
            destination = self.ready / name
            shutil.copy2(source, destination)
            digest = sha256_path(destination)
            item = {
                "name": name,
                "size": destination.stat().st_size,
                "sha256": digest,
                "golden_size": golden["size"],
                "golden_sha256": golden["sha256"],
                "golden_match": (
                    destination.stat().st_size == golden["size"]
                    and digest == golden["sha256"]
                ),
            }
            results.append(item)

        mismatches = [item["name"] for item in results if not item["golden_match"]]
        report = {
            "schema": 1,
            "mode": "rebuild-from-clean-originals",
            "title_id": self.recipe["title_id"],
            "media_id": self.recipe["media_id"],
            "files": results,
            "golden_mismatches": mismatches,
            "note": (
                "Rozdíl hashů může způsobit jiná verze xma2encode; FORGE "
                "struktura byla ověřena samostatně."
            ),
        }
        (self.output / "validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if self.args.strict_final_hashes and mismatches:
            raise RuntimeError(
                "Výstup se liší od testované zlaté sestavy: "
                + ", ".join(mismatches)
            )
        self.log(
            f"Hotovo: {len(results)} souborů; shoda se zlatou sadou "
            f"{len(results) - len(mismatches)}/{len(results)}."
        )
        return report

    def execute(self) -> dict[str, object]:
        self.prepare_output()
        self.core_ui()
        if self.args.stop_after == "core":
            return {"mode": "development-stop", "completed": "core"}
        self.text_and_fonts()
        if self.args.stop_after == "text":
            return {"mode": "development-stop", "completed": "text"}
        self.audio_all()
        self.final_text_fallbacks()
        return self.finalize()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xbox-dir", required=True, type=Path)
    parser.add_argument("--pc-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--xma2encode", required=True)
    parser.add_argument(
        "--quickbms",
        type=Path,
        default=THIRD_PARTY / "quickbms" / "quickbms.exe",
    )
    parser.add_argument(
        "--decoder", type=Path, default=BIN / "ac1_x360_raw_decode.exe"
    )
    parser.add_argument(
        "--pc-raw", type=Path, default=TOOLS / "assassin_creed_raw.bms"
    )
    parser.add_argument(
        "--compress-script",
        type=Path,
        default=TOOLS / "ac1_xmemlzx_compress_chunks.bms",
    )
    parser.add_argument(
        "--recipe", type=Path, default=CONFIG / "rebuild_recipe.json"
    )
    parser.add_argument(
        "--final-manifest", type=Path, default=CONFIG / "final_files.json"
    )
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="pokračovat v existujícím výstupním adresáři",
    )
    parser.add_argument("--skip-audio", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--stop-after", choices=("core", "text"), help=argparse.SUPPRESS
    )
    parser.add_argument("--strict-final-hashes", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        builder = Builder(args)
        builder.validate()
        print(json.dumps(builder.plan(), ensure_ascii=False, indent=2))
        if args.plan_only:
            return 0
        report = builder.execute()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:  # user-facing one-command entry point
        print(f"CHYBA: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

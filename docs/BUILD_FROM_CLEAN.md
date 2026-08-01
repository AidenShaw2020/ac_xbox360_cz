# Sestavení z čistých originálů

Tento dokument popisuje vývojářský postup použitý pro finální testovanou sestavu.
Nejde o jediné univerzální tlačítko: archivy FORGE obsahují fyzické odkazy a některé
zdroje vyžadují individuální bezpečné umístění. Skripty proto vždy spouštějte nad
kopiemi a po každé oblasti kontrolujte report.

## Požadavky

- Windows a Python 3.11 nebo novější;
- nejméně 35 GiB volného pracovního prostoru;
- vlastní čistá evropská Xbox 360 verze (`Title ID 555307D4`,
  `Media ID 59A9DD10`);
- vlastní PC instalace s českou lokalizací a dabingem od CD Projektu;
- [FFmpeg](https://ffmpeg.org/);
- vlastní oprávněná kopie `xma2encode.exe`.

`xma2encode.exe`, česká PC data ani hotové herní archivy nejsou součástí
repozitáře.

## Doporučené rozložení

```text
ac1-build/
  input/
    xbox-clean/       # čisté Data360*.forge
    pc-czech/         # české DataPC*.forge
  external/
    ffmpeg.exe
    xma2encode.exe
  cache/
  reports/
  output/
```

Pracovní kopii Xbox archivů vytvořte před prvním zásahem. Názvy vstupních
`DataPC*.forge` se mohou mezi PC vydáními lišit; nejprve je proto inventarizujte.

## 1. Inventarizace

```powershell
python tools/ac1_forge.py "input/xbox-clean/Data360.forge" --json
python tools/ac1_inventory_forge_bundles.py "input/xbox-clean"
python tools/ac1_inventory_pc_forge_bundles.py "input/pc-czech"
```

Výpisy slouží k nalezení odpovídajících Xbox/PC položek a MGB zdrojů. Pomocné
skripty `ac1_compare_mgb_slots.py`, `ac1_mgb_strings.py` a
`ac1_resource_bundle.py` umožňují zkontrolovat jednotlivé kandidáty.

## 2. Text a GUI

Základní převod MGB položek provádí `ac1_build_all_mgb_archive.py`. Bezpečný
výchozí režim zachovává sloty:

```powershell
python tools/ac1_build_all_mgb_archive.py `
  --source-forge "input/xbox-clean/Data360_Map_Menu.forge" `
  --pc-forge "input/pc-czech/DataPC_Map_Menu.forge" `
  --entry-name "NAZEV_POLOZKY_Z_INVENTARE" `
  --output "output/map-menu" `
  --decoder "bin/ac1_x360_raw_decode.exe" `
  --quickbms "third_party/quickbms/quickbms.exe" `
  --pc-raw-script "tools/assassin_creed_raw.bms" `
  --compress-script "tools/ac1_xmemlzx_compress_chunks.bms" `
  --slot-stable
```

`--entry-name` lze opakovat. U fyzicky odkazovaných položek použijte až po
porovnání reportu parametry `--target-offset`, `--mirror-offset` a případně
`--truncate-size`. Hodnoty se nesmějí přebírat mezi odlišnými vydáními hry.

Některé čistě konzolové řetězce nemají PC protějšek. Pro ně slouží přesná
náhrada uvnitř konkrétního zdroje:

```powershell
python tools/ac1_patch_utf16_resource_strings.py `
  --source-forge "output/Data360.forge" `
  --entry-name "Game Bootstrap Settings" `
  --resource-name "Timeline_MGB" `
  --replace "Memory Block 1=Blok paměti 1" `
  --replace "Solomon's Temple=Šalamounův chrám" `
  --replace "Jerusalem=Jeruzalém" `
  --decoder "bin/ac1_x360_raw_decode.exe" `
  --quickbms "third_party/quickbms/quickbms.exe" `
  --compress-script "tools/ac1_xmemlzx_compress_chunks.bms" `
  --output-forge "output/Data360.final.forge" `
  --report "reports/timeline.json"
```

Stejným nástrojem byly doplněny i zbývající bloky 2–7 a konzolové potvrzení
ukončení v `Data360_Present_Room.forge` (`Cell00027_DataBlock`,
`PressStart_MGB`).

## 3. České fonty

Fonty sestavuje `ac1_build_xbox_czech_fonts.py`. Vstupní `xbox-directory` a
`xbox-data` jsou rozbalené části příslušného resource bundle z inventarizace:

```powershell
python tools/ac1_build_xbox_czech_fonts.py `
  --xbox-directory "cache/font/0.dat" `
  --xbox-data "cache/font/1.dat" `
  --source-forge "output/Data360.forge" `
  --output-forge "output/Data360.fonts.forge" `
  --quickbms "third_party/quickbms/quickbms.exe" `
  --compress-script "tools/ac1_xmemlzx_compress_chunks.bms" `
  --report "reports/fonts.json" `
  --preview "reports/fonts.png"
```

Kontrolujte nejen tvary celé české diakritiky, ale také metriky znaků; skládání
samostatného háčku vedlo v původních pokusech ke kolizím a ořezu.

## 4. Český dabing

Každou oblast převádějte samostatně a sdílejte cache zakódovaných stop:

```powershell
python tools/ac1_build_complete_dub_area.py `
  --pc-forge "input/pc-czech/DataPC_Masyaf.forge" `
  --xbox-area-forge "output/Data360_Masyaf.forge" `
  --pc-language-forge "input/pc-czech/DataPC_StreamedSounds.forge" `
  --xbox-language-forge "output/Data360_StreamedSoundsEng.forge" `
  --language-output-forge "output/Data360_StreamedSoundsEng.next.forge" `
  --output-area-forge "output/Data360_Masyaf.next.forge" `
  --decoder "bin/ac1_x360_raw_decode.exe" `
  --quickbms "third_party/quickbms/quickbms.exe" `
  --pc-raw-script "tools/assassin_creed_raw.bms" `
  --ffmpeg "external/ffmpeg.exe" `
  --xma2encode "external/xma2encode.exe" `
  --cache-dir "cache/audio" `
  --report "reports/masyaf.json" `
  --localized-only
```

Režim `--localized-only` je zásadní: přenáší lokalizované dialogy, ale obnovuje
nelokalizované efekty z čisté Xbox zvukové banky. Tím zůstane zachován například
zvuk skryté čepele.

Pět stop v Šalamounově chrámu se nevešlo do původních slotů. Tyto výjimky byly
bezpečně přesměrovány nástrojem `ac1_build_dub_externalized_probe.py` do
existujících streamovacích slotů. Parametry `--include-resource` a
`--reuse-stream` určujte pouze podle reportu konkrétní čisté verze.

## 5. Finální kontrola

Výsledná testovaná sada má přesně názvy uvedené v
`config/final_files.json`. Ověření:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/Verify-FinalFiles.ps1 `
  -Directory "output/final"
```

Před kopírováním na konzoli musí souhlasit počet, délka i SHA-256 všech 16
souborů. Testujte postupně: start hry, novou hru, Abstergo, Šalamounův chrám,
Animus/timeline, zvuk efektů a několik následných paměťových bloků.

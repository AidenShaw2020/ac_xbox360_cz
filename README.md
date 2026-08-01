# Assassin's Creed – česká lokalizace a dabing pro Xbox 360

Port českého textu, českého uživatelského rozhraní a českého dabingu z PC
verze původního **Assassin's Creed** do verze pro Xbox 360.

Projekt je určen pro konzole s RGH/JTAG. Testovaná verze:

- Title ID: `555307D4`
- Media ID: `59A9DD10`
- evropská vícejazyčná verze hry

## Co je přeloženo

- hlavní nabídka, nabídka Animu, pauza a nastavení;
- výukové texty, cíle, rychlé zprávy, e-maily a časová osa DNA;
- názvy paměťových bloků, měst a vzpomínek;
- české znaky v původních konzolových fontech;
- český dabing příběhových dialogů v základní hře;
- konzolové ovládací prvky a symboly tlačítek zůstávají zachované.

Finální testovaná sada obsahuje 16 upravených archivů `.forge`. Kontrolní
velikosti a SHA-256 jsou v [config/final_files.json](config/final_files.json).

## Důležité: repozitář neobsahuje herní data

GitHub verze obsahuje pouze vlastní zdrojové a diagnostické nástroje. Nejsou
zde hotové archivy `.forge`, české soubory z PC hry ani jiné soubory hry.

Pro vlastní sestavení potřebujete:

1. legálně získanou a rozbalenou podporovanou Xbox 360 verzi hry;
2. nainstalovanou PC verzi se zapnutou původní českou lokalizací a dabingem;
3. Windows, Python 3.11 nebo novější a dostatek volného místa;
4. FFmpeg;
5. vlastní oprávněnou kopii `xma2encode.exe` — tento proprietární nástroj
   není součástí projektu a nelze jej zde distribuovat.

Python závislost pro nástroje pracující s fonty nainstalujete příkazem:

```powershell
python -m pip install -r requirements.txt
```

## Automatické sestavení jedním BAT

Pro běžné použití spusťte `Rebuild_AC1_CZ_From_Clean.bat`. Bez parametrů se
zobrazí interaktivní průvodce, který si vyžádá pět cest. Patcher zdrojové soubory
nemění a hotové archivy uloží do nové složky `Ready_To_Copy`.

Lze jej spustit také plně parametricky:

```bat
Rebuild_AC1_CZ_From_Clean.bat ^
  "D:\Xbox Games\Assassins Creed Clean" ^
  "D:\Games\Assassin's Creed" ^
  "D:\AC1_X360_CZ_Rebuilt" ^
  "C:\Tools\ffmpeg.exe" ^
  "C:\Tools\xma2encode.exe"
```

Parametry v pořadí:

1. kořen čisté rozbalené Xbox 360 hry;
2. kořen české PC instalace;
3. nový nebo prázdný výstupní adresář;
4. cesta k `ffmpeg.exe`;
5. cesta k `xma2encode.exe`;
6. volitelně `--plan-only` pro kontrolu vstupů bez sestavení, nebo `--resume`
   pro pokračování po přerušení ve stejném výstupu.

Výstup obsahuje:

- `Ready_To_Copy` – hotové `.forge` soubory určené ke zkopírování do kořene hry;
- `_build/build.log` – úplný protokol;
- `_build/reports` – dílčí ověřovací reporty;
- `validation.json` – konečnou strukturální a hashovou kontrolu.

Sestavení dabingu je časově i prostorově náročné. Doporučuje se alespoň 35 GiB
volného místa. Při přerušení spusťte stejný příkaz znovu s `--resume`; hotové
oblasti se přeskočí.

Podrobný vývojářský a ruční postup je v
[docs/BUILD_FROM_CLEAN.md](docs/BUILD_FROM_CLEAN.md).

## Instalace hotové soukromé sady

Pokud máte soukromý kompletní balík `AC1_X360_CZ_Complete_v1`, postupujte
podle jeho `README_COMPLETE_CZ.md`. Stručně:

1. zazálohujte původní `.forge` soubory hry;
2. obsah složky `Ready_To_Copy` zkopírujte do kořene Xbox hry a potvrďte
   nahrazení souborů;
3. hru úplně ukončete a konzoli restartujte;
4. nepoužívejte současně starší experimentální varianty stejných archivů.

## Obsah repozitáře

- `Rebuild_AC1_CZ_From_Clean.bat` – doporučený automatický patcher;
- `tools/` – převod, bezpečné přestavování FORGE/MGB a diagnostika BAO;
- `bin/` – otevřený nativní dekodér Xbox XMem/LZX a jeho zdroj;
- `scripts/` – kontrola vstupů, instalace a ověření výsledků;
- `docs/` – reprodukční postup a technické poznámky;
- `config/` – manifest finální testované sady.

## Bezpečnostní pravidla formátu

Assassin's Creed 1 na Xboxu používá vedle hlavního indexu také přímé fyzické
odkazy. Přestavované položky proto musí zůstat ve svých původních slotech,
pokud nástroj výslovně neprokáže bezpečnou relokaci. Přepsání celých PC MGB
objektů nebo bezmyšlenkovité zvětšení BAO slotů vede k zamrznutí, nekonečnému
načítání nebo pádu hry.

## Autoři a poděkování

- původní česká lokalizace a dabing pro PC: **CD Projekt**;
- port pro Xbox 360, testování a vydání: **AidenShaw2020**;
- [QuickBMS](https://aluigi.altervista.org/quickbms.htm) – Luigi Auriemma;
- [FFmpeg](https://ffmpeg.org/) a
  [vgmstream](https://github.com/vgmstream/vgmstream) – převod a kontrola zvuku.

Projekt je určen pouze pro osobní použití s vlastními legálně získanými
kopiemi hry. Uvedení původních autorů nemění práva k herním ani lokalizačním
datům.

# Assassin's Creed Xbox 360 CZ – kompletní soukromá sada v1

Tato varianta obsahuje hotové upravené herní archivy a je určena pro osobní
zálohu autora projektu. Není určena k nahrání do veřejného repozitáře.

## Instalace

1. Hru na Xboxu úplně ukončete.
2. Zazálohujte všechny stejnojmenné soubory v kořeni hry.
3. Zkopírujte všech 16 souborů ze složky `Ready_To_Copy` do kořene hry,
   například:

   ```text
   Hdd1:\Games\Assassin’s Creed\
   ```

4. Potvrďte nahrazení existujících souborů.
5. Odstraňte nebo přejmenujte starší experimentální kopie, které by mohl
   správce her načítat místo nové verze.
6. Konzoli úplně restartujte a spusťte hru.

Sada neupravuje `default.xex` a neinstaluje Title Update. Je určena pro
Title ID `555307D4`, Media ID `59A9DD10` a konzole RGH/JTAG.

## Kontrola

Před kopírováním spusťte:

```powershell
powershell -ExecutionPolicy Bypass -File .\Source_Tools\scripts\Verify-FinalFiles.ps1 `
  -Directory .\Ready_To_Copy
```

Očekávaným výsledkem je 16 shodných souborů. Jejich manifest je v
`Source_Tools\config\final_files.json`.

## Obsah

- kompletní české texty a GUI;
- české konzolové fonty;
- český dabing;
- opravená časová osa Animu a potvrzovací dialogy;
- obnovené herní zvukové efekty, které nepatří do dabingu.

Původní PC lokalizaci a dabing vytvořil **CD Projekt**. Port pro Xbox 360,
testování a vydání: **AidenShaw2020**.

# Technické poznámky

## Formát

Xbox archivy používají kontejner Scimitar FORGE, obálky `FILEDATA`, kompresi
XMem/LZX a uvnitř páry resource directory/data. Textové objekty MAGMA/MGB
obsahují několik jazykových tabulek i samostatné UTF-16 fallbacky. Zvuk je
uložen v BAO hlavičkách a v paměťových nebo externích proudech.

## Zásady funkčního portu

- kopírují se textové hodnoty, nikoli PC bytecode nebo celé PC MGB objekty;
- lokalizační záznamy zachovávají původní offsety a alokace;
- kratší UTF-16 fallbacky se doplňují mezerami, aby následující objektová data
  zůstala na stejném místě;
- přestavované FORGE položky se nahrazují in-place;
- do dabingu se vybírají jen vícejazykové PC BAO; jedno-variantní efekty se
  obnovují z čistého Xbox archivu;
- české repliky, které se nevejdou do malých vestavěných slotů, se převádějí
  na externí BAO proudy místo zvětšování struktury oblasti.

## Finální vrstvy

- v20 `Data360.forge`: kompletní text, dabing a UTF-16 fallbacky časové osy;
- v21 `Data360_Present_Room.forge`: kompletní text/dabing a české potvrzení
  ukončení hry;
- v16 `Data360_SolomonTemple.forge` + jazykový archiv: pět původně příliš
  dlouhých českých replik jako externí proudy;
- ostatní oblasti: stabilní lokalizované a nadabované sestavení v14.

Tyto názvy verzí jsou interní označení projektu, nikoli verze Title Update.

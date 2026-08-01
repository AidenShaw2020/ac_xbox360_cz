#!/usr/bin/env python3
"""Safe text-only localization helpers for Assassin's Creed 1 Xbox 360.

The Xbox MAGMA objects contain platform-specific code and must not be replaced
with whole PC objects.  This module changes only serialized text tables while
keeping every Xbox object, table extent, resource size, and surrounding byte
at its original offset.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ac1_build_gui_forge import magma_offsets
from ac1_mgb_strings import StringChain, find_chains


@dataclass(frozen=True)
class ChainPatch:
    chain_index: int
    entries: int
    used_bytes: int
    capacity_bytes: int


# Concise console wording for strings whose verbatim PC translation is longer
# than the complete Xbox Globals string table.  All placeholders are kept.
GLOBALS_OVERRIDES = {
    0x5D4BADEF: "Krádež",
    0x19554736: "Sídlo",
    0x4DBF1F75: "Království",
    0x545DB80E: "Masyaf",
    0x86935BDB: "Jeruzalém",
    0x92CB4383: "Zvěd",
    0x0922EC6E: "Pomoz občanu",
    0x6177B33D: "Všechny zn.",
    0xF8CCA575: "Město",
    0xE54AC1E8: "Všechny prv.",
    0xD3EF7E26: ">Prvky HUD",
    0xD46B1037: ">Úspěchy",
    0x8438988A: ">Autoři Animu",
    0xFAAB4DE9: ">Monitoring Ubisoftu",
    0xBF8610B6: "ZA",
    0xA7D91189: "VYP",
    0x2BF0E252: ">Hlas SFX",
    0x2DCCE16A: ">Hlas hudby",
    0x569C3A2A: ">Hlas dialogů",
    0xCBF68A9C: ">Úložiště",
    0xA52D5E67: ">Řídicí HUD",
    0xC69723A1: ">Ikona zbr.",
    0xDB583C67: ">Panel synchronizace",
    0xF2F33DD6: "hlas: %1% \\/ %2%",
    0xCA47F101: "disk: %1%",
    0x87836619: "ovládání: %1%",
    0x937301EB: "synchronizace: %1%",
    0xA384D1A1: "zbraň: %1%",
    0x39089849: "Arsuf",
    0x927FE6B3: "převr. osa X: %1%",
    0x2B843D5B: "převr. osa Y: %1%",
    0xDAA71E91: "citlivost X: %1% \\/ %2%",
    0xCD8F7A51: "citlivost Y: %1% \\/ %2%",
    0xEC336665: "Rychlé převíjení",
    0x1B2EDA9D: "Praporky inform.",
    0xF61A77B1: "Cíl informátora",
    0x7510F6C0: "Chudá čtvrť",
    0xF5E18895: "Střední čtvrť",
}


EMAIL_METADATA_OVERRIDES = {
    0x9685EB59: "Analýza",
    0xF62AA5AC: "Denní zprávy",
    0x787E40E6: "OD:",
    0xD74087F6: "Administrativa",
    0x448153A8: "[NEZNÁMÝ ODESILATEL]",
}

EMAIL_HEADER_OVERRIDES = {
    0x5EAD10D3: "Bezpečnostní_dokumentace",
    0x509E84A5: "vítejte, vedoucí #9",
    0xB553DD65: "DORUČENÉ - %1%",
    0x1E442524: "ODESLANÉ - %1%",
    0x88BA4215: "SMAZANÉ - %1%",
    0x335D3083: "Lucy",
    0x364713DD: "Vidic",
    0xD6850D6B: "vedoucí projektu #9",
}

ANIMUS_OVERRIDES = {
    0x95355595: "ANIMUS\nMOŽNOSTI",
    0xDEF86BD6: "MOŽNOSTI\nOBECNÉ",
    0xB86368B8: "MOŽNOSTI\nHUD",
    0xD09DA4CF: "MOŽNOSTI\nOVLÁDÁNÍ",
    0x17E6A844: "MOŽNOSTI\nAUTOŘI",
    0x32E298C0: "BLOK\nPAMĚTI 2",
    0x45E5A856: "BLOK\nPAMĚTI 3",
    0xDB813DF5: "BLOK\nPAMĚTI 4",
    0xAC860D63: "BLOK\nPAMĚTI 5",
    0x358F5CD9: "BLOK\nPAMĚTI 6",
    0x42886C4F: "BLOK\nPAMĚTI 7",
    0xD23771DE: "BLOK\nPAMĚTI 8",
    0xA5304148: "BLOK\nPAMĚTI 9",
    0x7ED8B1A1: "PAMĚŤ\nLOG",
    0x4466CB63: "nastavení Animu\novládací panel",
    0xE45C3534: "nápověda\nAnimu",
    0x1C43B26B: ">Opustit pam.",
    0x969D6FA0: "načíst paměť",
    0xE69AFAD1: ">Pokračovat",
    0xD93EBB3A: "načíst poslední",
    0x1B4B6319: "Paměťový blok hotov.\nPřehrát?",
}


# Visible Animus labels also occur as standalone length-prefixed UTF-16
# strings outside the keyed localization chain. Replacements must fit the
# original Xbox allocation, so a few labels use concise console wording.
ANIMUS_INLINE_REPLACEMENTS = {
    "LOADING": "NAČÍTÁ",
    "MEMORY DNA TIMELINE": "PŘEHLED PAMĚTI DNA",
    "Browse": "Projít",
    "Select": "Vybrat",
    "Exit": "Zpět",
    ">Options": ">Volby",
    ">Continue": ">Pokrač.",
    ">Exit Animus": ">Konec Animu",
    "Memory Complete": "Paměť hotova",
    "Memory Incomplete": "Paměť neúplná",
    "Back": "Zpět",
    ">View Attachment": ">Zobraz přílohu",
    ">Replay": ">Přehr.",
    ">Yes": ">Ano",
    ">No": ">Ne",
    "Scroll Text": "Posun textu",
    "browse": "projít",
    "select": "vybrat",
    ">General": ">Obecné",
    ">Credits": ">Autoři",
    ">HUD Elements": ">Prvky HUD",
    ">Controls": ">Ovládání",
    ">SFX Volume": ">Hlas. SFX",
    ">Music Volume": ">Hlas. hudby",
    ">Animus Blood": ">Animus: krev",
    ">Voice Volume": ">Hlas. řeči",
    ">Brightness": ">Jas",
    ">Storage Device": ">Úložiště",
    ">Invert Y Look": ">Převr. osa Y",
    ">Invert X Look": ">Převr. osa X",
    ">Vibration": ">Vibrace",
    ">Hold ² to Lock": ">Stisk ² zamkne",
    ">Look Y Sensitivity": ">Citlivost osy Y",
    ">Look X Sensitivity": ">Citlivost osy X",
    ">Control HUD": ">Řízení HUD",
    ">Weapon Icon": ">Ikona zbr.",
    ">Sync Bar": ">Synchr.",
    ">GPS System": ">Systém GPS",
    "Question?": "Otázka?",
    "Do you want to travel to Masyaf instantly?":
        "Chcete okamžitě odcestovat do Masyafu?",
    "MEMORY": "PAMĚŤ",
    "DNA TIMELINE": "PŘEHLED DNA",
    "Animus version 1.28": "Animus verze 1.28",
    "Objective Title": "Název úkolu",
    "Objective Subtitle": "Podnázev úkolu",
}


MEMORY_PAUSED_KEY_OVERRIDES = {
    0xA16BC410: "Všechny zn.",
    0x25410372: "Kapsářství",
    0x0C3A066C: "Odposlech",
    0x87E40939: "Pomoz občanu",
    0xF139EC74: "Šikana občana",
    0x7216E88E: "Sídlo Al Mualima",
    0xB4B01025: ">Paměť DNA",
    0x14D9B2D4: "otevřít protokol DNA",
    0x95840E3E: ">Pokračovat",
    0xB9A4B727: ">Možnosti",
    0xECC198E4: "nastavení Animu",
    0x62FC9351: "nápověda Animu",
    0x4B7E6205: ">Opustit pam.",
    0xFAB873DE: "rychle do Masyafu",
    0x0DF6C7096: "Převinout do Masyafu?",
    0x3A131D4F: "ANIMUS",
    0xA31A4CF5: "MOŽNOSTI",
    0x202657EF: "MOŽNOSTI",
    0xB92F0655: "OBECNÉ",
    0xB0FD2FB3: "MOŽNOSTI",
    0x29F47E09: "OVLÁDÁNÍ",
    0x425126E7: "MOŽNOSTI",
    0xDB58775D: "HUD",
    0x613CE563: "ANIMUS",
    0xF835B4D9: "AUTOŘI",
    0xFDFDEE13: "ANIMUS",
    0x64F4BFA9: "POKROKY",
    0x9C61EC8F: "ANIMUS",
    0x0568BD35: "VIP",
    0xFC342B3E: "PAUZA\nPAMĚTI",
    0x0172622E: "PAMĚŤ\nLOG",
    0x95355595: "ANIMUS\nMOŽNOSTI",
    0xDEF86BD6: "MOŽNOSTI\nOBECNÉ",
    0xB86368B8: "MOŽNOSTI\nHUD",
    0xD09DA4CF: "MOŽNOSTI\nOVLÁDÁNÍ",
    0xC03E462A: "přídavné paměti",
    0x583FE0BE: "Převinout do Masyafu?",
    0xC136B104: "Opustit paměť?\nNeuložený postup bude ztracen.",
    0x081AB5CF: ">Konec hry",
    0x55DFE544: "konec hry",
    0x70A4EA92: "Opravdu ukončit hru?\nNeuložený postup bude ztracen.",
    0xB446165A:
        "VAROVÁNÍ – Opuštění města obnoví paměť\n"
        "a neuložený postup bude ztracen.",
}


MEMORY_PAUSED_INLINE_REPLACEMENTS = {
    ">Memory Log": ">Protokol",
    ">Map": "Mapa",
    ">Resume Session": ">Pokračovat",
    ">Options": ">Volby",
    ">Additional Memories": ">Další paměti",
    ">Exit Memory": ">Opustit pam",
    "Browse": "Projít",
    "Select": "Vybrat",
    "Back": "Zpět",
    ">View Attachment": ">Zobraz přílohu",
    ">Replay": ">Přehr.",
    "Zoom": "Lupa",
    "Set Marker": "Nast. zn.",
    "Legend": "Popis",
    ">General": ">Obecné",
    ">HUD Elements": ">Prvky HUD",
    ">Controls": ">Ovládání",
    ">SFX Volume": ">Hlas. SFX",
    ">Music Volume": ">Hlas. hudby",
    ">Animus Blood": ">Animus: krev",
    ">Voice Volume": ">Hlas. řeči",
    ">Brightness": ">Jas",
    ">Storage Device": ">Úložiště",
    ">Invert Y Look": ">Převr. osa Y",
    ">Invert X Look": ">Převr. osa X",
    ">Vibration": ">Vibrace",
    ">Hold ² to Lock": ">Stisk ² zamkne",
    ">Look Y Sensitivity": ">Citlivost osy Y",
    ">Look X Sensitivity": ">Citlivost osy X",
    ">Control HUD": ">Řízení HUD",
    ">Weapon Icon": ">Ikona zbr.",
    ">Sync Bar": ">Synchr.",
    ">GPS System": ">Systém GPS",
    ">Practice Fight": ">Cvičný boj",
    ">Combo Kill": ">Kombo",
    ">Counter Attack": ">Protiútok",
    ">Throwing Knives": ">Vrh. nožů",
    ">Grab Breaks": ">Únik z chv.",
    ">Dodging": ">Úhyb",
    ">Defense Breaks": ">Průlom obr.",
    ">Exit Tutorial": ">Konec výuky",
    "browse": "projít",
    "select": "vybrat",
    "exit": "zpět",
    ">Damascus": ">Damašek",
    ">Jerusalem": ">Jeruzalém",
    ">Kingdom": ">Králov.",
    ">No": ">Ne",
    ">Yes": ">Ano",
    "Scroll Text": "Posun textu",
    "Fast forward memory to Masyaf?": "Převinout do Masyafu?",
    "_Yes": "_Ano",
    "_No": "_Ne",
    "Exit Tutorial?": "Ukončit výuku?",
    "Retry Tutorial?": "Opakovat výuku?",
    "Filter Markers": "Filtr značek",
    "Bureau": "Sídlo",
    "City - Acre": "Město: Acre",
    "City - Damascus": "Město: Damašek",
    "Target": "Cíl",
    "City - Jerusalem": "Město Jeruzalém",
    "City - Masyaf": "Město Masyaf",
    "Eavesdrop": "Odposlech",
    "Save Citizen": "Pomoz občanu",
    "Other Objective": "Jiný úkol",
    "View Point": "Rozhledna",
    "Interrogation": "Výslech",
    "Pickpocket": "Kapsářství",
    "Informer": "Inform.",
    "Informer's Flag": "Prap. inform.",
    "Informer's Target": "Cíl inform.",
    "Scholars": "Učenci",
    "Hide Spot": "Úkryt",
    "Vigilantes": "Psanci",
    "Marker": "Značka",
    "Objective Title": "Název úkolu",
    "Objective Subtitle": "Podnázev úkolu",
    "Question ?": "Otázka?",
    "Select your destination:": "Vyberte destinaci:",
    "MEMORY": "PAMĚŤ",
    "DNA TIMELINE": "PŘEHLED DNA",
    "Memory Block": "Blok paměti",
    "Status:": "Stav:",
}


# Console-only literals used by Globals_MGB and the shared pre-game/pause
# widgets.  The PC build keeps several of these in a different PC-specific
# object, so they cannot be obtained by pairing the two same-named resources.
# Every replacement deliberately fits in the original Xbox allocation.
CONSOLE_GLOBAL_INLINE_REPLACEMENTS = {
    "The following morning...": "Následující ráno...",
    ">Insert Title String Here": ">Sem vložte název",
    "insert subtitle string line 1 here": "sem vložte 1. řádek podtitulku",
    "insert subtitle string line 2 here": "sem vložte 2. řádek podtitulku",
    "<\\ PRESS ANY BUTTON TO INTERACT >": "<\\ STISKNĚTE LIBOVOLNÉ TLAČÍTKO >",
    "Saving game": "Ukládání hry",
    "Player Marker": "Značka hráče",
    "Eavesdropping": "Odposlech",
    "to Kingdom": "do Království",
    "to Masyaf": "do Masyafu",
    "to Acre": "do Akkonu",
    "to Damascus": "do Damašku",
    "to Jerusalem": "do Jeruzaléma",
    "All Markers": "Všechny značky",
    "Assassination Target": "Cíl vraždy",
    "to City": "do města",
    "All Elements": "Všechny prvky",
    "Objectives": "Úkoly",
    "Optional Objectives": "Volitelné úkoly",
    "graphic and sound\nadjustments": "nastavení obrazu\na zvuku",
    "interface\nadjustments": "nastavení\nrozhraní",
    ">Achievements": ">Úspěchy",
    "Animus\nachievement recognition": "úspěchy\nAnimu",
    ">Animus Credits": ">Autoři Animu",
    "Animus development team": "vývojový tým Animu",
    ">VIP Program": ">Program VIP",
    "Ubisoft Program monitoring": "monitoring Ubisoftu",
    "ON": "ZA",
    "OFF": "VYP",
    ">Blood": ">Krev",
    ">Synchronization Bar": ">Panel synchronizace",
    "volume: %1% \\/ %2%": "hlas: %1% \\/ %2%",
    "blood: %1%": "krev: %1%",
    "brightness: %1% \\/ %2%": "jas: %1% \\/ %2%",
    "storage: %1%": "disk: %1%",
    "control HUD: %1%": "řízení HUD: %1%",
    "synchronization bar: %1%": "synchronizace: %1%",
    "GPS system: %1%": "systém GPS: %1%",
    "weapon icon: %1%": "ikona zbr.: %1%",
    "To Arsuf": "Do Arsufu",
    "invert X axis: %1%": "převr. osa X: %1%",
    "invert Y axis: %1%": "převr. osa Y: %1%",
    "vibration: %1%": "vibrace: %1%",
    "X sensitivity: %1% \\/ %2%": "citlivost X: %1% \\/ %2%",
    "Y sensitivity: %1% \\/ %2%": "citlivost Y: %1% \\/ %2%",
    "Memory Fast Forward": "Rychlé převíjení",
    "hold to lock: %1%": "držet zamkne: %1%",
    "All Objectives": "Všechny úkoly",
    "Rich District": "Bohatá čtvrť",
    "Poor District": "Chudá čtvrť",
    "Middle District": "Střední čtvrť",
}


ALL_KEY_OVERRIDES = {
    **GLOBALS_OVERRIDES,
    **EMAIL_METADATA_OVERRIDES,
    **EMAIL_HEADER_OVERRIDES,
    **ANIMUS_OVERRIDES,
    **MEMORY_PAUSED_KEY_OVERRIDES,
}


# Compact translations for fixed-offset Xbox localization tables.  These
# strings deliberately fit inside the original English record so every hash,
# length field, and following record keeps its original byte offset.
SLOT_STABLE_KEY_OVERRIDES = {
    0x7A083318: "Hra uložena.",
    0xA3D9014E: "Kontrolní bod.",
    0x3774ABC7: "Paměť obnovena.",
    0x7946C582: "#\nKrást lze jen anonymně.",
    0xAE7E47B0: "Obnova...",
    0xE8B44903: "Varování -\nPříliš blízko.",
    0x845C5E62: "Chyba -\nSektor nedostupný",
    0x3B782924: "Mapa Jeruzaléma upravena",
    0xBC57BB27: "Mapa Acre upravena",
    0x1853527E: "Mapa Damašku upravena",
    0xF78C2520: "Mapa Masyafu upravena",
    0x36DBF132: "Krádež",
    0x4AF072DA: "Odposlech",
    0xD5DB5B0D: "Pomoc občanu",
    0x2BB73C22: "Občan volný",
    0xA6B31658: "Stiskem ² promluvíte s občanem.",
    0x7F8547FD: "#\nS občanem lze mluvit\njen v anonymním stavu.",
    0x74086A5B: "Krádež znovu spuštěna",
    0xE8782F30: "Odposlech obnoven",
    0xCDDB5008: "Znaky",
    0xACB6495C: "Království: %1% \\/ %2% praporků",
    0x53938945: "Masyaf: %1% \\/ %2% praporků",
    0x40DDD1F8: "Jeruzalém: %1% \\/ %2% praporků",
    0xE7C5918B: "Damašek: %1% \\/ %2% praporků",
    0x981A2463: "Acre: %1% \\/ %2% templář. prap.",
    0x322ECAD5: "Acre: %1% z %2% špitálnických prap.",
    0x9EAB8888: "Varování -\nPříliš daleko.\nPřibližte se.",
    0x4A2F7C98: "Desynchronizace - smrt\nNačítání paměti...",
    0x8B1BF105: "Desynchronizace - cíl uprchl\nNačítání paměti...",
    0xCAAE1A4F: "Vražda pro informátora",
    # Compact pause-menu labels.  The PC wording is one or two characters
    # longer than the fixed Xbox allocation.
    0xC4DEE681: "Kancel",
    0x4988281D: "Vojenské",
    0xB15823A2: "Spoj",
    0xB9A4B727: ">Volby",
    0x4B7E6205: ">Opustit pam",
    0xA31A4CF5: "VOLBY",
    0x202657EF: "VOLBY",
    0xB0FD2FB3: "VOLBY",
    0x425126E7: "VOLBY",
    0x3B40A236: "MAPA\nPAMĚŤ",
    0x95355595: "ANIMUS\nVOLBY",
    0xB86368B8: "VOLBY\nHUD",
    0xD09DA4CF: "VOLBY\nOVLÁDÁNÍ",
    0x17E6A844: "ANIMUS\nAUTOŘI",
}


# Source-specific compact translations for strings whose hash is reused by
# several platform variants.  Keeping these keyed by the complete source text
# avoids replacing a different Xbox controller wording with the wrong label.
SLOT_STABLE_SOURCE_OVERRIDES = {
    "Press and hold ³ and ¢\nand move ƒ to SPRINT.":
        "Držte ³ a ¢ a pomocí ƒ SPRINTUJTE.",
    "GRAB AND THROW": "CHYTIT A HODIT",
    "WEAPON RESTRICTION": "OMEZENÍ ZBRANÍ",
    "HORSE BLEND": "KRYTÍ NA KONI",
    "CHASE CAM": "KAMERA STÍHÁNÍ",
    "HIGH/LOW PROFILE": "NÁPADNĚ/NENÁPADNĚ",
    "BLEND": "KRYTÍ",
    "COMBO KILL": "KOMBO SMRT",
    "EXIT TUTORIAL": "KONEC VÝUKY",
    "Rank up": "ÚROVEŇ+",
    "SAVE CITIZEN FAILED": "ZÁCHRANA SELHALA",
    "FIRST PERSON": "1. OSOBA",
    "RANK UPGRADE": "NOVÁ ÚROVEŇ",
    "OBSOLETE:\nPress and hold ³ and press £ to GRAB.\n"
    "Move ƒ to direct your Throw into the scaffold.":
        "ZASTARALÉ:\nDržte ³ a £ pro CHVAT.\n"
        "Pomocí ƒ hoďte soupeře do lešení.",
    "OBSOLETE Grab and Throw": "STARÉ: CHYTIT/HODIT",
    "#\nPay attention to the Eye Icon.\n"
    "Blend to prevent being Exposed.":
        "#\nSledujte ikonu oka.\nKrytím se vyhnete odhalení.",
    "SOCIAL BEHAVIOR TIP #1": "CHOVÁNÍ – TIP 1",
    "SOCIAL BEHAVIOR TIP #2": "CHOVÁNÍ – TIP 2",
    "SOCIAL BEHAVIOR TIP #3": "CHOVÁNÍ – TIP 3",
    "SOCIAL BEHAVIOR TIP #4": "CHOVÁNÍ – TIP 4",
    "#\nUse Scholars to get past guard posts.":
        "#\nUčenci vás provedou kolem stráží.",
    "#\nPress £ while falling to grab onto a\nledge.":
        "#\nPři pádu stiskněte £ pro zachycení.",
    "#\nWhen Exposed, break the Line of Sight to\nhide. "
    "Wait until your status returns to\nAnonymous before coming out.":
        "#\nPo odhalení zmizte z dohledu a ukryjte se.\n"
        "Vyčkejte na anonymní stav.",
    "EXPOSED TIP #1": "ODHALENÍ TIP 1",
    "EXPOSED TIP #2": "ODHALENÍ TIP 2",
    "EXPOSED TIP #3": "ODHALENÍ TIP 3",
    "EXPOSED TIP #4": "ODHALENÍ TIP 4",
    "EXPOSED TIP #5": "ODHALENÍ TIP 5",
    "EXPOSED TIP #6": "ODHALENÍ TIP 6",
    "#\nWhen Exposed, press and hold ³ and £ while\nmoving to Tackle.":
        "#\nPo odhalení držte ³ a £ za pohybu\npro sražení.",
    "#\nAfter countering a Strong Attack:\n"
    "Press ¤ for an extra attack\n"
    "Press ¢ to break his legs\n"
    "Press £ to throw to the ground and assassinate":
        "#\nPo vykrytí silného útoku:\n"
        "¤ – další útok\n¢ – podražení\n£ – sražení a zabití",
    "#\nPress ~ UP to select your Hidden Blade.\n"
    "Press ¤ to finish your enemy.":
        "#\nŠipkou NAHORU zvolte skrytou čepel.\n"
        "¤ dorazí nepřítele.",
    "Press ²\nto speak with a saved citizen.":
        "Stiskněte ² a mluvte\nse zachráněným.",
    "SAVE CITIZEN": "ZACHRAŇ LIDI",
    "BLEND ON HORSE": "KRYTÍ NA KONI",
    "LEAP OF FAITH": "SKOK VÍRY",
    "#\nFind the people to eavesdrop\non with ‰ and press ² to lock.":
        "#\nNajděte cíl pomocí ‰ a zaměřte jej ².",
    "Press ‰ to center the camera.":
        "Stiskněte ‰ pro střed kamery.",
    "#\nWeapon Lost: Hidden Blade": "#\nZtracena: Skrytá čepel",
    "#\nWeapon Lost: Short Blade": "#\nZtracena: Krátká čepel",
    "#\nAbility Lost: Counter Kills": "#\nZtraceno: Protiútok",
    "#\nAbility Lost: Tackle": "#\nZtraceno: Sražení",
    "#\nAbility Lost: Catch Ledge": "#\nZtraceno: Zachycení",
    "#\nAbility Lost: Grab Break": "#\nZtraceno: Únik z chvatu",
    "#\nAbility Lost: Balance Regain": "#\nZtraceno: Rovnováha",
    "#\nAbility Lost: Dodging": "#\nZtraceno: Úhyb",
    "#\nWeapon Lost: Extra Throwing Knives":
        "#\nZtraceny: Vrhací nože navíc",
    "#\nAbility Lost: Defense Break":
        "#\nZtraceno: Proražení obrany",
    "#\nAbility Lost: Short Blade Expertise (Damage)":
        "#\nZtraceno: Expert krátké čepele",
    "#\nAbility Lost: Sword Expertise (Damage)":
        "#\nZtraceno: Expert na meč",
    "You've been demoted!": "Byli jste sesazeni!",
    "Press and hold ²\nto enter and exit fight mode.":
        "Držte ² pro zapnutí či vypnutí boje.",
    "#\nFind the people to eavesdrop on with ‰\n"
    "and press and hold ² to lock.":
        "#\nNajděte cíl pomocí ‰ a podržením ² ho zaměřte.",
}

# Final compact wording for the tutorial strings that do not fit the fixed
# Xbox allocations.  These entries intentionally preserve controller glyphs
# and line breaks while avoiding any change to the surrounding MGB records.
SLOT_STABLE_SOURCE_OVERRIDES.update({
    # Compact Xbox controller HUD actions.
    "crouch": "skrčit",
    "sprint/free-run": "sprint/parkur",
    "free-run": "parkur",
    "dismount": "sesedni",
    "get off": "sesedni",
    "hide": "kryt",
    "jump": "skok",
    "pick pocket": "okrást",
    "punch": "udeř",
    "drop": "pusť",
    "sheath": "zasuň",
    "tackle": "srazit",
    "speak": "mluv",
    "talk": "mluv",
    "exit blend": "opusť kryt",
    "steal": "ukraď",
    "WALK": "KROK",
    "SYNCHRONIZATION BAR": "LIŠTA SYNCHRONIZACE",
    "#\nHead = ¼\nEmpty Hand = £\nArmed Hand = ¤\nLegs = ¢":
        "#\nHlava = ¼\nRuka = £\nZbraň = ¤\nNohy = ¢",
    "Press ¼ while standing still\nto enter first person view.":
        "Stůjte a stiskněte ¼\npro pohled z 1. osoby.",
    "GENTLE PUSH": "JEMNÝ TLAK",
    "#\nPress and hold ³ for Action Oriented Moves.\n"
    "Release ³ for Socially Acceptable Moves.\n\n"
    "PRESS AND HOLD ³ TO SEE THE HUD CHANGE.":
        "#\nDržte ³ pro akční pohyby.\n"
        "Uvolněte ³ pro nenápadné pohyby.\n\n"
        "PODRŽTE ³ A SLEDUJTE ZMĚNU HUD.",
    "HIGH/LOW PROFILE": "NÁPADNĚ/TIŠE",
    "SOCIAL STATUS ICON": "IKONA STAVU",
    "Press ² to enter and exit fight mode.":
        "Tlačítkem ² zapněte/vypněte boj.",
    "LOCK TO FIGHT": "ZAMĚŘIT BOJ",
    "Press ¤ to ATTACK.": "ÚTOK: stiskněte ¤.",
    "#\nSOCIAL STATUS: Exposed (Red).\n"
    "Soldiers are after you.\n\n"
    "BREAK THE LINE OF SIGHT BY\n"
    "CLIMBING THE LADDER.":
        "#\nSTAV: Odhalen (červená).\n"
        "Vojáci vás stíhají.\n\n"
        "VYLEZTE PO ŽEBŘÍKU\nA ZMIZTE Z DOHLEDU.",
    "EXPOSED": "ODHALEN",
    "UNSEEN": "SKRYT",
    "#\nTutorial Complete": "#\nVÝUKA HOTOVA",
    "#\nTutorial Successful": "#\nVÝUKA ÚSPĚŠNÁ",
    "#\nFast Forward Memory": "#\nPŘEVINOUT PAMĚŤ",
    "#\nSkip Memory": "#\nPŘESKOČIT",
    "FREE-STEP": "VOLNĚ",
    "Press † to view your Map.": "Stiskněte † pro mapu.",
    "MAP": "GPS",
    "Move ƒ in LOW PROFILE\ntowards the bench to sit.":
        "Nenápadně jděte ƒ\nk lavičce a sedněte.",
    "Press ¼ to eavesdrop.": "¼ = ODPOSLECH",
    "PICKPOCKET": "KAPSÁŘSTVÍ",
    "Press ²\nto lock onto your Pickpocket target from afar.":
        "Z dálky zaměřte ² cíl\npro kapsářství.",
    "#\nFollow the Despot to a secluded place\n"
    "and beat him until he speaks.":
        "#\nSledujte despotu do ústraní\na bijte jej, až promluví.",
    "#\nFollow the Despot to a secluded place and\n"
    "beat him until he speaks.":
        "#\nSledujte despotu do ústraní\na bijte jej, až promluví.",
    "HORSE MOUNT": "NASEDNOUT",
    "HORSE BLEND": "KRYTÍ: KŮŇ",
    "HORSE JUMP": "SKOK KONĚ",
    "#\nBuildings with an eagle\n"
    "circling above are View Points.\n"
    "Climb to the top to scan the area.":
        "#\nKrouží-li nad budovou orel,\n"
        "jde o rozhlednu. Vylezte nahoru\n"
        "a prozkoumejte okolí.",
    "#\nTo dismount, slow or stop the horse,\n"
    "press £ in LOW PROFILE.":
        "#\nZpomalte či zastavte koně\na nenápadně stiskněte £.",
    "HORSE DISMOUNT": "SESEDNOUT",
    "#\nCitizens you save from soldiers\nwill help you in return.":
        "#\nZachránění obyvatelé\nvám na oplátku pomohou.",
    "Press ²\nto try and save a citizen.":
        "Podržte ² a zachraňte člověka.",
    "CITY ALERT": "POPLACH",
    "#\nThe Scholars help you get past guard posts.\n"
    "Press ¢ to Blend with them.\n"
    "Look for their Icon on your GPS.":
        "#\nUčenci vás provedou stráží.\n"
        "Stiskněte ¢ a splyňte s nimi.\n"
        "Hledejte jejich ikonu na GPS.",
    "BUREAU CLOSED": "ÚŘAD ZAVŘEN",
    "TACKLE": "SRAZIT",
    "#\nWhen \"Chase Cam\" is written in the HUD,\n"
    "press µ to see who's chasing you.":
        "#\nKdyž HUD hlásí „Kamera“,\n"
        "stiskněte µ a spatříte pronásledovatele.",
    "CHASE CAM": "KAM. HONU",
    "#\nPress ANY BUTTON when you see a glitch.":
        "#\nPři chybě stiskněte\nLIBOVOLNÉ TLAČÍTKO.",
    "GRAB": "CHYT",
    "GRAB BREAK": "ÚNIK CHVAT",
    "Press ¤ to Attack.": "ÚTOK: ¤",
    "DEFLECT": "VYKRYTÍ",
    "#\nWhen fighting, press and hold ³ to use\n"
    "Defensive moves, and release to use\n"
    "Offensive moves.":
        "#\nV boji držte ³ pro obranu.\n"
        "Uvolněte ³ pro útok.",
    "#\nPress ¢ to Step towards your enemy.":
        "#\nTlačítkem ¢ vykročte k sokovi.",
    "Press £ to Grab.\n"
    "Throw in the wanted direction with ƒ.\n"
    "Try throwing him into Merchant Stands or\n"
    "Scaffolds.":
        "Stiskněte £ pro chvat.\n"
        "Směr hodu určete pomocí ƒ.\n"
        "Hoďte soka na stánek nebo lešení.",
    "#\nTime your Fast Attacks to get a combo.":
        "#\nNačasujte rychlé útoky pro kombo.",
    "COMBO ATTACK": "KOMBO ÚTOK",
    "DEFENSE BREAK": "PRŮLOM OBRANY",
    "Tutorial successfully completed.\nRetry?":
        "Výuka dokončena.\nOpakovat?",
    "#\nAbility Lost: Tackle\n\n"
    "Ability Lost: Catch Ledge\n\n"
    "Ability Lost: Grab Break":
        "#\nZtraceno: Sražení\n\n"
        "Ztraceno: Římsa\n\n"
        "Ztraceno: Únik",
    "Ancestor's memory synchronized\nNow recording...":
        "Paměť předka sladěna\nNahrávám...",
    "LOADING": "NAČÍTÁ",
})

# Central timeline, save-system and memory-block strings whose full PC Czech
# wording is longer than the fixed Xbox allocation.
SLOT_STABLE_SOURCE_OVERRIDES.update({
    "None": "Nic",
    "Skip": "Dál",
    "Retry": "Znovu",
    "[tbd]": "Zpět",
    "Saving...": "Ukládá...",
    "Memory Open": "Paměť volná",
    "Citizen Saved": "Občan zachr.",
    "Memory Locked": "Paměť zamčena",
    "Glory Complete": "Sláva splněna",
    "MEMORY\nBLOCK 1": "PAMĚŤ\nBLOK 1",
    "MEMORY\nBLOCK 2": "PAMĚŤ\nBLOK 2",
    "MEMORY\nBLOCK 3": "PAMĚŤ\nBLOK 3",
    "MEMORY\nBLOCK 4": "PAMĚŤ\nBLOK 4",
    "MEMORY\nBLOCK 5": "PAMĚŤ\nBLOK 5",
    "MEMORY\nBLOCK 6": "PAMĚŤ\nBLOK 6",
    "MEMORY\nBLOCK 7": "PAMĚŤ\nBLOK 7",
    "Memory Block 1": "Paměť: blok 1",
    "Memory Block 2": "Paměť: blok 2",
    "Memory Block 3": "Paměť: blok 3",
    "Memory Block 4": "Paměť: blok 4",
    "Memory Block 5": "Paměť: blok 5",
    "Memory Block 6": "Paměť: blok 6",
    "Memory Block 7": "Paměť: blok 7",
    "< Memory Locked >": "< Paměť zamčena >",
    "No Data Available": "Data nenalezena",
    "Return to Al Mualim": "Zpět k Al Mualimovi",
    "Xbox 360 Hard Drive": "Disk Xbox 360",
    "Speak with Al Mualim": "Mluv s Al Mualimem",
    "William de Monferrat": "Vilém z Montferratu",
    "Xbox 360 Memory Unit": "Paměť Xbox 360",
    "Masyaf Flag Collected": "Praporek z Masyafu",
    "Memory: Save Citizens": "Paměť: Zachraň lidi",
    "Memory: Templar Flags": "Paměť: Praporky řádu",
    "Memory: Saracens Flags": "Paměť: Saracénské vl.",
    "Memory: Teutonic Flags": "Paměť: Německé vlajky",
    "Talal the Slave Trader": "Talal, otrokář",
    "Templar Flag Collected": "Získán praporek řádu",
    "Memory: Assassins Flags": "Paměť: Vlajky asasínů",
    "Saracens Flag Collected": "Saracénský praporek",
    "Find Robert and stop him": "Zastavte Roberta",
    "Assassin's Creed Savegame": "Uložení Assassin's Creed",
    "Memory: Kill All Templars": "Paměť: Zabij templáře",
    "Memory: King Richard Flags": "Paměť: Richardovy vlajky",
    "Assassin's Creed saved game": "Uložení Assassin's Creed",
    "King Richard Flag Collected": "Richardův praporek",
    "Locate Al Mualim in Masyaf.": "Al Mualim je v Masyafu.",
    "Return to the Bureau Leader": "Zpět k veliteli ústředí",
    "Bring Masun before Al Mualim": "Masun k Al Mualimovi",
    "Overwrite existing saved game": "Přepsat uloženou hru",
    "%1% \\/ %2% Templars eliminated.":
        "%1% \\/ %2% templářů zabito.",
    "What are Robert's true motives?": "Jaké jsou Robertovy cíle?",
    "Speak with Bureau Leader in Acre": "Promluv s velitelem v Acre",
    "Continue without using saved games": "Pokračovat bez uložených her",
    "Investigate and find Masyaf's Traitor": "Najděte masyafského zrádce",
    "Speak with Bureau Leader in Jerusalem":
        "Promluv s velitelem v Jeruzalémě",
    "Don't face all of Robert's men\nat once.":
        "Nebojujte se všemi\nRobertovými muži.",
    "Speak with the Bureau Leader in Jerusalem":
        "Promluv s velitelem v Jeruzalémě",
    "All View Points scaled within the Kingdom.":
        "Všechny rozhledny v Království nalezeny.",
    "All Teutonic Flags were collected from Acre.":
        "Všechny německé vlajky z Acre získány.",
    "Find aid within the city to help escape\nthe souk.":
        "Najděte ve městě pomoc\nk úniku z tržiště.",
    "Jubair holds daily meetings within\n the Madrasah.":
        "Jubair se denně schází\nv madrase.",
    "%1% \\/ %2% Teutonic Flags\nwere collected from Acre.":
        "Získáno %1% \\/ %2% německých\nvlajek z Acre.",
    "Saving content. Please don't turn off your console.":
        "Ukládám data. Nevypínejte konzoli.",
    "Another man in the harbor\nseems connected to William.":
        "Další muž v přístavu\nmá spojení s Williamem.",
    "Acquired map detailing Robert's\nposition during the funeral.":
        "Mapa ukazuje Robertovu\npozici při pohřbu.",
    "Talal inspects his warehouse\ndaily. Strike during next inspection.":
        "Talal denně kontroluje sklad.\nZaútočte při další kontrole.",
    "If the citadel's gates close,\nclimbing the walls will be the\nescape.":
        "Zavřou-li se brány citadely,\nunikněte přes hradby.",
    "Strike after William meets with\nRichard. He'll be distracted by\nhis meeting.":
        "Zaútočte po Williamově schůzce\ns Richardem. Bude rozrušen.",
    "\"I must return to Al Mualim and tell him of our troubles beneath Solomon's Temple.\"":
        "„Musím se vrátit k Al Mualimovi a říct mu o neúspěchu v Šalamounově chrámu.“",
    "\"It appears the souk's northeastern rooftops provide easy access to the central courtyard.\"":
        "„Střechy na severovýchodě tržiště vedou snadno na centrální nádvoří.“",
    "Strike Majd Addin during the\nexecution today. He'll be near the\nwestern edge of Solomon's temple.":
        "Zaútočte na Majd Addina při popravě.\nBude u západního okraje chrámu.",
    "Acquired map of the souk showing the\nnortheastern roofs easily connecting\nto the central courtyard.":
        "Mapa tržiště ukazuje cestu po\nseverovýchodních střechách na nádvoří.",
    "Target_City: Damascus\n\nTarget_Name: Tamir\n\n"
    "Target_Title: Black Market Merchant\n\n"
    "Target_Location: Souk Al-Silaah":
        "Město: Damašek\n\nCíl: Tamir\n\n"
        "Role: Překupník\n\nMísto: Tržiště Al-Silaah",
    "\"Malik has brought the enemy to our village. I must protect my people. "
    "I will go to them and save as many as I can.\"":
        "„Malik přivedl nepřítele do vsi. Musím chránit své lidi a zachránit jich co nejvíc.“",
    "Target_City: Jerusalem\n\nTarget_Name: Talal\n\n"
    "Target_Title: Slave Trader\n\n"
    "Target_Location: Northern Barbican Warehouse":
        "Město: Jeruzalém\n\nCíl: Talal\n\n"
        "Role: Otrokář\n\nMísto: Sklad v severním barbakanu",
    "Target_City: Acre\n\nTarget_Name: Garnier de Naplouse\n\n"
    "Target_Title: Leader of the Hospitaliers\n\n"
    "Target_Location: Hospitalier Fortress":
        "Město: Acre\n\nCíl: Garnier de Naplouse\n\n"
        "Role: Velitel johanitů\n\nMísto: Johanitská pevnost",
    "\"Some of Garnier's guards have abandoned their posts. "
    "The archers patrolling the roof are at a disadvantage. "
    "A few seconds is all I need to clear a path.\"":
        "„Část Garnierových stráží opustila stanoviště. "
        "Střešní lučištníci jsou v nevýhodě. Pár sekund mi stačí k uvolnění cesty.“",
    "\"The people of Damascus despise the Merchant King for spending their money "
    "on extravagant parties held inside his palace. Attending one of these "
    "celebrations should bring me close enough to strike.\"":
        "„Lidé Damašku nenávidí krále obchodníků za plýtvání na palácové oslavy. "
        "Na jedné z nich se dostanu dost blízko k útoku.“",
    "\"Robert's men are well prepared for battle. To fight them all at once would "
    "be unwise. Should I lose control of the situation, it's best I make a brief "
    "escape and return later to eliminate them one by one.\"":
        "„Robertovi muži jsou připraveni. Bojovat se všemi je pošetilé. "
        "Když ztratím kontrolu, uniknu a vrátím se je vyřídit jednoho po druhém.“",
    "\"Robert and his men walk the streets of Jerusalem finely dressed, bearing "
    "expensive gifts. They plan to attend a funeral. Were it anyone other than "
    "the monster Majd Addin being buried, I might have second thoughts. But as "
    "it is, it seems a most fitting time to take the Templar's life.\"":
        "„Robert a jeho muži kráčejí Jeruzalémem v drahých šatech s dary na pohřeb. "
        "Kdyby nepohřbívali stvůru Majd Addina, váhal bych. Takto je to vhodná "
        "chvíle vzít templáři život.“",
})

# Present-day Animus and e-mail UI strings.
SLOT_STABLE_SOURCE_OVERRIDES.update({
    ">Replay": ">Znovu",
    ">Options": ">Volby",
    "False Alarm": "Planý alarm",
    "INBOX - %1%": "DOŠLÉ %1%",
    "Nancy Nilop": "Nancy Nilop",
    ">Exit Memory": ">Opustit pam",
    "OUTBOX - %1%": "ODESL. %1%",
    "Re: Leila...": "Odp: Leila..",
    "Re: Your pen": "Re: Tvé pero",
    "welcome Lucy": "Vítej, Lucy",
    "ACCESS DENIED": "VSTUP ZAKÁZÁN",
    "Lucy Stillman": "Lucy Stillman",
    "welcome Vidic": "Vítej, Vidicu",
    "ACCESS GRANTED": "VSTUP POVOLEN",
    "Daily Headlines": "Denní zprávy",
    "Project Lead #9": "Vedoucí #9",
    "[SENDER UNKNOWN]": "[NEZNÁMÝ AUTOR]",
    "Re: conf room door": "Re: dveře porady",
    "Re: Case File #1394 [Leila Marino]":
        "Re: Spis #1394 [Leila Marino]",
    "Re: Case Filed #1394 [Leila Marino]":
        "Re: Spis #1394 [Leila Marino]",
    "Hundreds Hospitalized in Wake of Water Tampering Scandal:\n"
    "Abstergo Holdings stands accused of secretly manipulating a small town's "
    "water supply in order to test a synthetic drug referred to internally as "
    "New Fluoride.\n\n"
    "Nation Mourns the Loss of Final Film Studio:\n"
    "History was made today with the closing of the country's last movie studio. "
    "The rising prominence of video games coupled with rampant piracy are "
    "considered to be the leading causes of its demise.":
        "Stovky lidí hospitalizovány po kontaminaci vody:\n"
        "Abstergo Holdings čelí obvinění, že tajně upravilo zásobování malého "
        "města vodou, aby otestovalo syntetickou drogu interně zvanou Nový "
        "fluorid.\n\n"
        "Národ oplakává poslední filmové studio:\n"
        "Dnes zavřelo poslední filmové studio v zemi. Za hlavní příčiny jeho "
        "zániku jsou považovány rostoucí obliba videoher a rozsáhlé pirátství.",
})


# Conservative phrase shortening used only when the full Czech PC text does
# not fit the original Xbox record.  Newlines, controller glyphs and format
# placeholders remain untouched.
SLOT_STABLE_COMPACTIONS = (
    ("Stiskněte a podržte", "Podržte"),
    ("Stisknutím a podržením", "Podržením"),
    ("stiskněte a podržte", "podržte"),
    ("stisknutím a podržením", "podržením"),
    (" a pomocí ƒ", " a ƒ"),
    ("Pomocí ƒ ", "ƒ "),
    ("pomocí ƒ ", "ƒ "),
    ("NENÁPADNÉM STAVU", "NENÁPADNĚ"),
    ("NÁPADNÉM STAVU", "NÁPADNĚ"),
    ("nenápadném stavu", "nenápadně"),
    ("nápadném stavu", "nápadně"),
    ("Získána nová schopnost -", "Nová schopnost:"),
    ("Získána nová zbraň -", "Nová zbraň:"),
    ("Schopnost ztracena:", "Ztraceno:"),
    ("Zbraň ztracena:", "Ztracena:"),
    ("Synchronizace paměti úrovně", "Paměť: úroveň"),
    ("Můžete se pocvičit na nádvoří.", "Cvičte na nádvoří."),
    ("Chcete-li", "Pro"),
    ("chcete-li", "pro"),
    ("abyste mohli", "pro"),
    ("abyste", "ať"),
    ("protivníka", "soupeře"),
    ("pronásledovatelům", "strážím"),
    ("synchronizace", "synchr."),
    ("Synchronizace", "Synchr."),
    ("úvodní část", "výuka"),
    ("ÚVODNÍ ČÁST", "VÝUKA"),
)


def compact_slot_text(value: str, allocation: int) -> str:
    """Shorten Czech wording without changing tokens or record boundaries."""
    compact = value.rstrip()
    if len(compact) <= allocation:
        return compact
    for source, target in SLOT_STABLE_COMPACTIONS:
        compact = compact.replace(source, target)
        if len(compact) <= allocation:
            return compact
    return compact


PRESS_START_REPLACEMENTS = {
    "Inspired by historical events and characters.":
        "Inspirováno historickými událostmi.",
    "This work of fiction was designed, developed and produced \n"
    "by a multicultural team of various religious faiths and beliefs.":
        "Toto fiktivní dílo navrhl, vyvinul a vytvořil\n"
        "mezinárodní tým lidí různých vyznání a názorů.",
    "Visit http://www.assassinscreed.com/help for help and game tips.":
        "Rady a tipy najdete na http://www.assassinscreed.com/help.",
    "< Press the START button >": "<Stiskněte tlačítko START>",
    "PAUSE": "PAUZA",
    "Select": "Vybrat",
    "Back": "Zpět",
    "NEW": "NOV",
    "< NEW >": "< NOVÁ>",
    "CONTINUE": "HRÁT DÁL",
    "< CONTINUE >": "< HRÁT DÁL >",
    "Memory Block %1%": "Blok paměti %1%",
    "RESUME": "NÁVRAT",
    "< RESUME >": "< NÁVRAT >",
    "QUIT GAME": "KONEC HRY",
    "< QUIT GAME >": "< KONEC HRY >",
    "YES": "ANO",
    "< YES >": "< ANO >",
    "NO": "NE",
    "< NO >": "< NE >",
    "The save game is corrupted and cannot be loaded":
        "Uložená hra je poškozená a nelze ji načíst",
    "Starting a new game will erase the current saved game. Do you want to continue?":
        "Nová hra smaže aktuální uloženou hru. Chcete pokračovat?",
    "Are you sure you want to quit?": "Opravdu chcete ukončit hru?",
    "Are you sure you want to exit?": "Opravdu chcete odejít?",
    "Any unsaved progress will be lost.": "Neuložený postup bude ztracen.",
}


# A few hashes are reused for different platform variants, so these compact
# fallbacks are keyed by the complete English source text rather than by hash.
SOURCE_TEXT_OVERRIDES = {
    "#\nAbility Lost: Defense Break\n\n"
    "Ability Lost: Short Blade Expertise (Damage)\n\n"
    "Ability Lost: Sword Expertise (Damage)":
        "#\nZtraceno: Proražení obrany\n\n"
        "Ztraceno: Expert na krátké čepele\n\n"
        "Ztraceno: Expert na meč",
    "#\nWeapon Lost: Throwing Knives": "#\nZtracena zbraň: Vrhací nože",
    "Return to Masyaf and speak with\nAl Mualim":
        "Vraťte se do Masyafu za\nAl Mualimem.",
    "Speak with Al Mualim": "Promluv s Al Mualimem",
    "Escape and return to the\nBureau Leader":
        "Unikněte a vraťte se\nk veliteli",
    "%1% \\/ %2% Investigations Complete.\n"
    "%3% Investigations are needed to\n"
    "access Assassination Memory.\n"
    "Climb a view point to locate them.":
        "Hotovo %1% \\/ %2% vyšetřování.\n"
        "K paměti zabití je třeba %3%.\n"
        "Najděte je z rozhledny.",
    "%1% \\/ %2% Investigations retrieved.\n"
    "%3% Investigations are needed to\n"
    "access Memory Strand: Assassination.":
        "Hotovo %1% \\/ %2% vyšetřování.\n"
        "K paměti zabití je třeba %3%.",
    "Save options failed. The storage device containing your gamer profile "
    "has been removed.":
        "Uložení voleb selhalo. Zařízení s profilem bylo odpojeno.",
    "Save options failed. The storage device in use has been removed.":
        "Uložení voleb selhalo. Použité zařízení bylo odpojeno.",
    "Kingdom Map Updated...": "Mapa Království: změna",
    "Map Updated. Press † for details.": "Mapa obnovena. † ukáže detaily.",
    "Scholars are a moving Hiding spot.\n"
    "Use them to get by guard posts.":
        "Učenci jsou pohyblivý úkryt.\n"
        "Projděte s nimi kolem stráží.",
    "Found %1% \\/ %2% Teutonic flags in Acre":
        "Praporky v Akkonu: %1% \\/ %2%",
    "Press and hold ² to activate the save citizen.\n"
    "If you attack any soldiers before activating,\n"
    "the save citizen will fail.\n"
    "Return later and it will be available again.":
        "Podržte ² pro záchranu.\n"
        "Předtím neútočte.\n"
        "Při selhání se vraťte.",
    "Press ² to activate the save citizen.\n"
    "If you attack any soldiers before activating,\n"
    "the save citizen will fail.\n"
    "Return later and it will be available again.":
        "Stiskněte ² pro záchranu.\n"
        "Předtím neútočte.\n"
        "Při selhání se vraťte.",
    "%1% \\/ %2% Flags collected.\n"
    "Collect all Assassins' flags\n"
    "and report to the Informer.":
        "Praporky: %1% \\/ %2%.\n"
        "Seberte všechny a vraťte se k informátorovi.",
    "%1% \\/ %2% Flags collected. Collect all Assassins' flags and "
    "report to the Informer.":
        "Praporky: %1% \\/ %2%. Seberte všechny a vraťte se k informátorovi.",
    "Memory Update Failed\n"
    "To reinitialize, vanish if you're\n"
    "exposed, and return to the informer.":
        "Aktualizace selhala.\n"
        "Setřeste pronásledovatele\na vraťte se k informátorovi.",
    "%1% \\/ %2% Targets Assassinated. Eliminate all Targets and "
    "report to the Informer.":
        "Cíle: %1% \\/ %2%. Zlikvidujte všechny a vraťte se k informátorovi.",
    "%1% \\/ %2% Targets Assassinated.\n"
    "Eliminate all Targets\n"
    "and report to the Informer.":
        "Cíle: %1% \\/ %2%.\n"
        "Zlikvidujte všechny\na vraťte se k informátorovi.",
    "Find and collect %1% Masyaf flags\n"
    "and come back to see the Informer\n"
    "before time runs out.":
        "Seberte %1% praporků v Masyafu\na vraťte se k informátorovi\nvčas.",
    "All Flags have been collected. Report to the Informer to complete "
    "this memory.":
        "Máte všechny praporky. Vraťte se k informátorovi.",
    "All Flags have been collected.\n"
    "Report to the Informer\n"
    "to complete this memory.":
        "Máte všechny praporky.\nVraťte se k informátorovi.",
    "All Targets have been Assassinated. Report to the Informer to "
    "complete this memory.":
        "Všechny cíle jsou mrtvé. Vraťte se k informátorovi.",
    "All Targets have been Assassinated.\n"
    "Report to the Informer\n"
    "to complete this memory.":
        "Všechny cíle jsou mrtvé.\nVraťte se k informátorovi.",
    "Stealth assassinate all targets\n"
    "and return to the Informer\n"
    "without becoming exposed.":
        "Nenápadně zabijte všechny cíle\na vraťte se k informátorovi.",
    "Memory Update Failed\n"
    "To reinitialize, vanish if you're\n"
    "exposed, or wait a few seconds.":
        "Aktualizace selhala.\nSetřeste stráže nebo chvíli vyčkejte.",
    "Desynchronized - Overkilling Innocents (Tenet 1)\n"
    "Reloading memory...":
        "Desynchronizace – zabíjení nevinných.\nObnova paměti...",
    "Warning -\nCannot lock on a different Informer\n"
    "while an Informer mission is in progress.":
        "Varování – během úkolu nelze vybrat jiného informátora.",
    "An Informer has useful information for your investigation. Locate him "
    "to receive a special Assignment.":
        "Informátor má informace k vyšetřování. Najděte ho.",
    "Vigilantes are allies.\nUse them to block pursuing soldiers.":
        "Psanci jsou spojenci.\nZadrží pronásledovatele.",
    "Warning -\nYou are too far to talk to the victim.\nGet closer.":
        "Varování – jste příliš daleko od oběti.\nPřibližte se.",
    "Find and collect %1% Masyaf flags\n"
    "and come back to see the Informer.":
        "Seberte %1% praporků v Masyafu\na vraťte se k informátorovi.",
    "Sit on a nearby bench\nand lock on the target\n"
    "to begin eavesdropping":
        "Sedněte si na lavičku,\nzaměřte cíl\na naslouchejte.",
    "Warning -\nYou are too far to pickpocket.\nGet closer.":
        "Varování – jste příliš daleko.\nPřibližte se ke kapse.",
    "WAITING\nFOR INPUT": "ČEKÁM\nNA VSTUP",
    "ANIMUS\nACHIEVEMENTS": "ANIMUS\nPOKROKY",
    "Animus Version 1.28": "Animus verze 1.28",
    ">Tutorials": ">Výuka",
    "ANIMUS\nTUTORIAL": "ANIMUS\nVÝUKA",
    "calm": "klid",
    "assassinate": "zabít",
    "kill": "zab",
    "grab break": "únik chv.",
    "c-grab": "chvat",
    "grab": "chyt",
    "push": "strč",
    "interact": "akce",
    "kick": "kop",
    "listen": "slyšet",
    "rear": "vzad",
    "sit": "sed",
    "draw": "tas",
    "unsheath": "vytasit",
    "blend": "skrýt",
    "walk": "jít",
    "pray": "modl",
    "synchronize": "synchroniz.",
    "gentle push": "lehce strč",
    "info": "info",
    "swing": "švih",
    "exit blend": "konec krytí",
    "mount": "nased",
    "grasp": "chop",
}


ALL_INLINE_REPLACEMENTS = {
    **CONSOLE_GLOBAL_INLINE_REPLACEMENTS,
    **ANIMUS_INLINE_REPLACEMENTS,
    **MEMORY_PAUSED_INLINE_REPLACEMENTS,
    **PRESS_START_REPLACEMENTS,
}


def first_locale(blob: bytes) -> tuple[int, int]:
    offsets = magma_offsets(blob)
    if len(offsets) < 2:
        raise ValueError("MGB resource does not contain multiple MAGMA locales")
    return offsets[0], offsets[1]


def main_chain(locale: bytes) -> StringChain:
    chains = find_chains(locale, 3)
    if not chains:
        raise ValueError("no keyed string chain found")
    return max(chains, key=lambda chain: len(chain.entries))


def rebuild_chain(
    locale: bytearray,
    chain: StringChain,
    translations: dict[int, str],
    *,
    chain_index: int,
) -> ChainPatch:
    encoded = bytearray()
    for entry in chain.entries:
        value = translations.get(entry.key, entry.value)
        raw = value.encode("utf-16le")
        encoded += struct.pack("<II", entry.key, len(value))
        encoded += raw
    capacity = chain.end - chain.start
    if len(encoded) > capacity:
        raise ValueError(
            f"chain {chain_index} needs {len(encoded)} bytes but has {capacity}"
        )
    locale[chain.start : chain.end] = encoded + b"\0" * (capacity - len(encoded))
    return ChainPatch(chain_index, len(chain.entries), len(encoded), capacity)


def patch_length_prefixed_literals(
    locale: bytearray,
    replacements: dict[str, str],
    *,
    excluded_ranges: list[tuple[int, int]] | None = None,
) -> dict[str, int]:
    """Patch standalone [u32 length][UTF-16LE] strings without moving data."""
    original = bytes(locale)
    occupied: list[tuple[int, int]] = []
    excluded_ranges = excluded_ranges or []
    counts: dict[str, int] = {}
    for source, target in sorted(
        replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if len(target) > len(source):
            raise ValueError(
                f"inline replacement {target!r} exceeds allocation for {source!r}"
            )
        needle = struct.pack("<I", len(source)) + source.encode("utf-16le")
        cursor = 0
        count = 0
        while True:
            position = original.find(needle, cursor)
            if position < 0:
                break
            end = position + len(needle)
            cursor = end
            if any(start < end and position < stop for start, stop in excluded_ranges):
                continue
            if any(start < end and position < stop for start, stop in occupied):
                continue
            encoded = target.encode("utf-16le")
            struct.pack_into("<I", locale, position, len(target))
            text_start = position + 4
            allocation = len(source) * 2
            locale[text_start : text_start + allocation] = (
                encoded + b"\0" * (allocation - len(encoded))
            )
            occupied.append((position, end))
            count += 1
        if count:
            counts[source] = count
    return counts


def patch_length_prefixed_literals_slot_stable(
    locale: bytearray,
    replacements: dict[str, str],
    *,
    excluded_ranges: list[tuple[int, int]] | None = None,
) -> dict[str, int]:
    """Patch standalone strings without changing their recorded length.

    Early Scimitar widgets place binary fields immediately after the UTF-16
    allocation.  Shortening the length prefix while leaving the allocation in
    place makes the runtime parse padding as object data.  Keep the original
    prefix and pad the translated text with spaces so the next field remains
    at exactly the same byte offset.
    """
    original = bytes(locale)
    occupied: list[tuple[int, int]] = []
    excluded_ranges = excluded_ranges or []
    counts: dict[str, int] = {}
    for source, target in sorted(
        replacements.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if len(target) > len(source):
            raise ValueError(
                f"inline replacement {target!r} exceeds allocation for {source!r}"
            )
        needle = struct.pack("<I", len(source)) + source.encode("utf-16le")
        cursor = 0
        count = 0
        while True:
            position = original.find(needle, cursor)
            if position < 0:
                break
            end = position + len(needle)
            cursor = end
            if any(start < end and position < stop for start, stop in excluded_ranges):
                continue
            if any(start < end and position < stop for start, stop in occupied):
                continue
            padded = target + " " * (len(source) - len(target))
            text_start = position + 4
            locale[text_start : text_start + len(source) * 2] = (
                padded.encode("utf-16le")
            )
            occupied.append((position, end))
            count += 1
        if count:
            counts[source] = count
    return counts


def patch_complete_mgb(
    xbox_blob: bytes, pc_blob: bytes
) -> tuple[bytes, dict[str, object]]:
    """Transfer every safe Czech string while retaining the Xbox MAGMA code.

    Keyed tables are paired by their stable localization hashes. Standalone
    console literals are translated from the shared compact replacement map.
    If a PC table is too large for its fixed Xbox allocation, the longest
    growing entries are conservatively left unchanged and reported instead of
    moving any following object data.
    """
    xs, xe = first_locale(xbox_blob)
    ps, pe = first_locale(pc_blob)
    xbox_locale = bytearray(xbox_blob[xs:xe])
    pc_locale = pc_blob[ps:pe]
    xbox_chains = find_chains(bytes(xbox_locale), 3)
    pc_chains = find_chains(pc_locale, 3)

    pc_values: dict[int, str] = {}
    for chain in pc_chains:
        for entry in chain.entries:
            pc_values.setdefault(entry.key, entry.value)

    chain_reports: list[dict[str, object]] = []
    excluded: list[tuple[int, int]] = []
    for index, chain in enumerate(xbox_chains):
        xbox_keys = [entry.key for entry in chain.entries]
        candidates = [
            candidate
            for candidate in pc_chains
            if [entry.key for entry in candidate.entries] == xbox_keys
        ]
        if candidates:
            # PC objects may retain an untranslated platform fallback with the
            # same hashes. Prefer the candidate carrying Czech orthography;
            # only use difference from Xbox as a secondary discriminator.
            czech_letters = set("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")
            pc_chain = max(
                candidates,
                key=lambda candidate: (
                    sum(
                        character in czech_letters
                        for entry in candidate.entries
                        for character in entry.value
                    ),
                    sum(
                        left.value != right.value
                        for left, right in zip(
                            chain.entries, candidate.entries
                        )
                    ),
                ),
            )
            paired_values = {
                index: entry.value
                for index, entry in enumerate(pc_chain.entries)
            }
        else:
            paired_values = {}
        translations = [
            SOURCE_TEXT_OVERRIDES.get(
                entry.value,
                ALL_KEY_OVERRIDES.get(
                    entry.key,
                    paired_values.get(
                        entry_index, pc_values.get(entry.key, entry.value)
                    ),
                ),
            )
            for entry_index, entry in enumerate(chain.entries)
        ]
        capacity = chain.end - chain.start

        def encoded_size() -> int:
            return sum(
                8 + len(value.encode("utf-16le"))
                for value in translations
            )

        reverted: list[tuple[int, int]] = []
        if encoded_size() > capacity:
            growth = sorted(
                (
                    (
                        len(translations[entry_index].encode("utf-16le"))
                        - len(entry.value.encode("utf-16le")),
                        entry_index,
                        entry,
                    )
                    for entry_index, entry in enumerate(chain.entries)
                    if translations[entry_index] != entry.value
                ),
                key=lambda item: item[0],
                reverse=True,
            )
            for _growth, entry_index, entry in growth:
                translations[entry_index] = entry.value
                reverted.append((entry_index, entry.key))
                if encoded_size() <= capacity:
                    break
        if encoded_size() > capacity:
            raise ValueError(
                f"chain {index} cannot fit even after reverting translations"
            )

        changed_entries = sum(
            value != entry.value
            for entry, value in zip(chain.entries, translations)
        )
        if changed_entries:
            encoded = bytearray()
            for entry, value in zip(chain.entries, translations):
                encoded += struct.pack("<II", entry.key, len(value))
                encoded += value.encode("utf-16le")
            xbox_locale[chain.start : chain.end] = (
                encoded + b"\0" * (capacity - len(encoded))
            )
            chain_reports.append(
                {
                    "chain_index": index,
                    "entries": len(chain.entries),
                    "used_bytes": len(encoded),
                    "capacity_bytes": capacity,
                    "changed_entries": changed_entries,
                    "reverted_keys": [
                        f"{entry_index}:0x{key:08X}"
                        for entry_index, key in reverted
                    ],
                }
            )
        excluded.append((chain.start, chain.end))

    safe_inline = {
        source: target
        for source, target in ALL_INLINE_REPLACEMENTS.items()
        if len(target) <= len(source)
    }
    inline_report = patch_length_prefixed_literals(
        xbox_locale,
        safe_inline,
        excluded_ranges=excluded,
    )
    report: dict[str, object] = {
        "xbox_chains": len(xbox_chains),
        "pc_chains": len(pc_chains),
        "chains": chain_reports,
        "inline_strings": inline_report,
    }
    return xbox_blob[:xs] + bytes(xbox_locale) + xbox_blob[xe:], report


def patch_complete_mgb_slot_stable(
    xbox_blob: bytes, pc_blob: bytes
) -> tuple[bytes, dict[str, object]]:
    """Transfer Czech strings while preserving every Xbox table offset.

    Some early Scimitar resources reference localization records by byte
    offset in addition to their stable hash. Repacking a whole string chain
    keeps it syntactically valid but invalidates those external offsets and
    can crash the Xbox loader. This variant keeps each original length field
    and record position, pads shorter Czech strings with spaces, and reports
    any translation that cannot fit its individual English slot.
    """
    xs, xe = first_locale(xbox_blob)
    ps, pe = first_locale(pc_blob)
    xbox_locale = bytearray(xbox_blob[xs:xe])
    pc_locale = pc_blob[ps:pe]
    xbox_chains = find_chains(bytes(xbox_locale), 3)
    pc_chains = find_chains(pc_locale, 3)

    pc_values: dict[int, str] = {}
    for chain in pc_chains:
        for entry in chain.entries:
            pc_values.setdefault(entry.key, entry.value)

    czech_letters = set(
        "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"
    )
    chain_reports: list[dict[str, object]] = []
    for chain_index, chain in enumerate(xbox_chains):
        keys = [entry.key for entry in chain.entries]
        candidates = [
            candidate
            for candidate in pc_chains
            if [entry.key for entry in candidate.entries] == keys
        ]
        pc_chain = (
            max(
                candidates,
                key=lambda candidate: (
                    sum(
                        character in czech_letters
                        for entry in candidate.entries
                        for character in entry.value
                    ),
                    sum(
                        left.value != right.value
                        for left, right in zip(
                            chain.entries, candidate.entries
                        )
                    ),
                ),
            )
            if candidates
            else None
        )
        changed = 0
        overlong: list[str] = []
        for entry_index, entry in enumerate(chain.entries):
            exact_pair = (
                pc_chain.entries[entry_index].value
                if pc_chain is not None
                else pc_values.get(entry.key, entry.value)
            )
            hash_pair = pc_values.get(entry.key, entry.value)
            paired = max(
                (exact_pair, hash_pair),
                key=lambda value: (
                    sum(character in czech_letters for character in value),
                    value != entry.value,
                ),
            )
            target = SLOT_STABLE_SOURCE_OVERRIDES.get(
                entry.value,
                SLOT_STABLE_KEY_OVERRIDES.get(
                    entry.key,
                    SOURCE_TEXT_OVERRIDES.get(
                        entry.value,
                        ALL_KEY_OVERRIDES.get(entry.key, paired),
                    ),
                ),
            )
            if target == entry.value:
                continue
            target = compact_slot_text(target, len(entry.value))
            if len(target) > len(entry.value):
                overlong.append(
                    f"{entry_index}:0x{entry.key:08X}:"
                    f"{len(target)}>{len(entry.value)}"
                )
                continue
            allocation = len(entry.value) * 2
            encoded = target.encode("utf-16le")
            padding = (" " * (len(entry.value) - len(target))).encode(
                "utf-16le"
            )
            text_start = entry.offset + 8
            xbox_locale[text_start : text_start + allocation] = (
                encoded + padding
            )
            changed += 1
        chain_reports.append(
            {
                "chain_index": chain_index,
                "entries": len(chain.entries),
                "changed_entries": changed,
                "overlong_entries": overlong,
                "record_offsets_preserved": True,
            }
        )

    excluded = [(chain.start, chain.end) for chain in xbox_chains]
    safe_inline = {
        source: target
        for source, target in ALL_INLINE_REPLACEMENTS.items()
        if len(target) <= len(source)
    }
    inline_report = patch_length_prefixed_literals_slot_stable(
        xbox_locale,
        safe_inline,
        excluded_ranges=excluded,
    )
    report = {
        "mode": "slot-stable",
        "xbox_chains": len(xbox_chains),
        "pc_chains": len(pc_chains),
        "chains": chain_reports,
        "inline_strings": inline_report,
    }
    return xbox_blob[:xs] + bytes(xbox_locale) + xbox_blob[xe:], report


def patch_globals(xbox_blob: bytes, pc_blob: bytes) -> tuple[bytes, list[ChainPatch]]:
    xs, xe = first_locale(xbox_blob)
    ps, pe = first_locale(pc_blob)
    xbox_locale = bytearray(xbox_blob[xs:xe])
    xbox_chain = main_chain(bytes(xbox_locale))
    pc_chain = main_chain(pc_blob[ps:pe])
    pc_values = {entry.key: entry.value for entry in pc_chain.entries}
    if [entry.key for entry in xbox_chain.entries] != [
        entry.key for entry in pc_chain.entries[: len(xbox_chain.entries)]
    ]:
        raise ValueError("Globals PC/Xbox string keys are not an ordered prefix")
    translations = {
        entry.key: GLOBALS_OVERRIDES.get(entry.key, pc_values[entry.key])
        for entry in xbox_chain.entries
    }
    report = [rebuild_chain(xbox_locale, xbox_chain, translations, chain_index=0)]
    return xbox_blob[:xs] + bytes(xbox_locale) + xbox_blob[xe:], report


def patch_animus(xbox_blob: bytes, pc_blob: bytes) -> tuple[bytes, dict[str, object]]:
    xs, xe = first_locale(xbox_blob)
    ps, pe = first_locale(pc_blob)
    xbox_locale = bytearray(xbox_blob[xs:xe])
    xbox_chain = main_chain(bytes(xbox_locale))
    pc_chains = find_chains(pc_blob[ps:pe], 3)
    pc_chain = next(
        chain
        for chain in pc_chains
        if [entry.key for entry in chain.entries]
        == [entry.key for entry in xbox_chain.entries]
    )
    translations = {entry.key: entry.value for entry in pc_chain.entries}
    translations.update(ANIMUS_OVERRIDES)
    chain_patch = rebuild_chain(xbox_locale, xbox_chain, translations, chain_index=0)
    inline_report = patch_length_prefixed_literals(
        xbox_locale,
        ANIMUS_INLINE_REPLACEMENTS,
        excluded_ranges=[(xbox_chain.start, xbox_chain.end)],
    )
    report: dict[str, object] = {
        "chains": [chain_patch.__dict__],
        "inline_strings": inline_report,
    }
    return xbox_blob[:xs] + bytes(xbox_locale) + xbox_blob[xe:], report


def patch_memory_paused(
    xbox_blob: bytes, pc_blob: bytes
) -> tuple[bytes, dict[str, object]]:
    xs, xe = first_locale(xbox_blob)
    ps, pe = first_locale(pc_blob)
    xbox_locale = bytearray(xbox_blob[xs:xe])
    xbox_chain = main_chain(bytes(xbox_locale))
    pc_chain = main_chain(pc_blob[ps:pe])
    pc_values = {entry.key: entry.value for entry in pc_chain.entries}
    translations = {
        entry.key: MEMORY_PAUSED_KEY_OVERRIDES.get(
            entry.key, pc_values.get(entry.key, entry.value)
        )
        for entry in xbox_chain.entries
    }
    chain_patch = rebuild_chain(xbox_locale, xbox_chain, translations, chain_index=0)
    inline_report = patch_length_prefixed_literals(
        xbox_locale,
        MEMORY_PAUSED_INLINE_REPLACEMENTS,
        excluded_ranges=[(xbox_chain.start, xbox_chain.end)],
    )
    report: dict[str, object] = {
        "chains": [chain_patch.__dict__],
        "inline_strings": inline_report,
    }
    return xbox_blob[:xs] + bytes(xbox_locale) + xbox_blob[xe:], report


def patch_email(xbox_blob: bytes, pc_blob: bytes) -> tuple[bytes, list[ChainPatch]]:
    xs, xe = first_locale(xbox_blob)
    ps, pe = first_locale(pc_blob)
    xbox_locale = bytearray(xbox_blob[xs:xe])
    xbox_chains = find_chains(bytes(xbox_locale), 3)
    pc_chains = find_chains(pc_blob[ps:pe], 3)
    patches: list[ChainPatch] = []
    for index, xbox_chain in enumerate(xbox_chains):
        keys = [entry.key for entry in xbox_chain.entries]
        matches = [
            chain
            for chain in pc_chains
            if [entry.key for entry in chain.entries] == keys
        ]
        if matches:
            translations = {entry.key: entry.value for entry in matches[0].entries}
            translations.update(
                {
                    key: value
                    for key, value in EMAIL_HEADER_OVERRIDES.items()
                    if key in keys
                }
            )
        else:
            translations = {
                entry.key: EMAIL_METADATA_OVERRIDES.get(entry.key, entry.value)
                for entry in xbox_chain.entries
            }
        patches.append(
            rebuild_chain(xbox_locale, xbox_chain, translations, chain_index=index)
        )
    return xbox_blob[:xs] + bytes(xbox_locale) + xbox_blob[xe:], patches


def _balanced_padding(value: str, length: int) -> str:
    if len(value) > length:
        raise ValueError(f"replacement {value!r} exceeds {length} UTF-16 units")
    remaining = length - len(value)
    left = remaining // 2
    right = remaining - left
    return " " * left + value + " " * right


def patch_press_start(xbox_blob: bytes) -> tuple[bytes, dict[str, int]]:
    xs, xe = first_locale(xbox_blob)
    locale = bytearray(xbox_blob[xs:xe])
    counts: dict[str, int] = {}
    original = bytes(locale)
    occupied: list[tuple[int, int]] = []
    # Replace enclosing/decorated labels before their shorter substrings
    # (for example "< NEW >" before "NEW"). Search only the original bytes,
    # so later rules such as NO -> NE cannot corrupt the translated word NOVÁ.
    for source, target in sorted(
        PRESS_START_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        needle = source.encode("utf-16le")
        positions: list[int] = []
        cursor = 0
        while True:
            position = original.find(needle, cursor)
            if position < 0:
                break
            end = position + len(needle)
            cursor = end
            if any(start < end and position < stop for start, stop in occupied):
                continue
            positions.append(position)
            occupied.append((position, end))
        if not positions:
            raise ValueError(f"PressStart string not found: {source!r}")
        # Short labels are centred inside their original allocation.  Long
        # prose is left-aligned so line wrapping remains natural.
        if "\n" not in source and len(source) <= 30:
            target = _balanced_padding(target, len(source))
        else:
            target = target + " " * (len(source) - len(target))
        replacement = target.encode("utf-16le")
        if len(replacement) != len(needle):
            raise AssertionError(f"PressStart replacement length differs: {source!r}")
        for position in positions:
            locale[position : position + len(needle)] = replacement
        counts[source] = len(positions)
    return xbox_blob[:xs] + bytes(locale) + xbox_blob[xe:], counts

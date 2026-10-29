# BkNG-Konverter V5 – Anleitung

Der Konverter überführt ein **Bk2000-Projekt** oder ein **BkNG-Projekt aus Automation
Studio 4** in das **BkNG-Basisprojekt für Automation Studio 6**. Am Ende steht ein
Bericht mit allen Punkten, die noch von Hand zu erledigen sind.

## Voraussetzungen

- Windows
- Python 3.10 oder neuer mit tkinter (beim Installer von python.org enthalten).
  Weitere Pakete sind nicht nötig.
- **Quellprojekt:** ein Bk2000-Projekt mit `dplist.dat`, `gruppen.dat` und `global.var`
  oder ein BkNG-Projekt ab Automation Studio 4.4 (mit dem Programm `VisuTask`), auch
  ein BkNG-Projekt von Automation Studio 6.x, als Projektordner oder als ZIP-Datei.
- **Ziel:** ein leeres BkNG-Basisprojekt (AS6), als Projektordner oder als ZIP-Datei.
- Beide Projekte sind in Automation Studio geschlossen.

> **Achtung:** Der Konverter ändert das Zielprojekt direkt (Global.var, Cpu.sw,
> Hardware, I/O-Mapping, OPC UA, Visu, Kontaktpläne). Nur in ein frisches
> Basisprojekt konvertieren und vorher eine Sicherung anlegen. Am einfachsten ist der
> ZIP-Modus: Er entpackt bei jedem Lauf ein neues Basisprojekt.

## Start

Im Ordner des Konverters:

```powershell
python bkng_converter.py
```

Die Sprache der Oberfläche (Deutsch/English) lässt sich oben rechts umstellen.

## Betriebsarten

Der Konverter erkennt die Betriebsart selbst und zeigt sie unter dem Quellprojekt an:

- **Konvertierung (Bk2000-Projekt):** Die Datenpunkte werden in die `Global.var` des
  BkNG-Projekts geschrieben. Ihre Initialwerte (Klartext, Parameter, Typ-Vorgaben)
  kommen aus der `dplist.dat`, ihre Gruppe aus der `gruppen.dat`. Der UST-Name wird in
  `UST_Name` der VisuTask eingetragen. Je nach Auswahl kommen Kontaktpläne,
  Datenmodule, Hardware, Ethernet, OPC UA und die Benutzervisu dazu.
- **Migration (BkNG-Projekt aus AS 4 oder AS 6):** Die projektspezifischen Teile werden
  in das AS6-Basisprojekt übernommen: fehlende globale Deklarationen,
  VisuTask-Einstellungen, Tasks, Datenmodule, Kundenbibliotheken, Hardware, Benutzer,
  OPC UA und die Kundenvisu. Die Beispiele und Testseiten des Basisprojekts werden
  entfernt (und auch aus einem AS6-Quellprojekt nicht übernommen). Ob das Quellprojekt
  aus AS 4 oder AS 6 stammt, steht in seiner `.apj`-Datei. Die Kontaktpläne eines
  AS6-Projekts liegen schon im AS6-Format und werden nicht gewandelt.

## Bedienung

1. Konverter starten.
2. Unter **Quelle** bei *Quellprojekt (.apj/.zip)* auf **Durchsuchen…** klicken und die
   Projektdatei (`.apj`) oder die ZIP-Datei mit dem Projekt auswählen. Eine ZIP-Datei
   wird in einen temporären Ordner entpackt, der beim Beenden gelöscht wird; die
   ZIP-Datei selbst bleibt unverändert. Darunter erscheint die erkannte Betriebsart,
   bei einem BkNG-Projekt mit seiner BkNG-Version (Version des Programms `VisuTask`).
3. Unter **Ziel** eine der beiden Möglichkeiten wählen:
   - **Bestehendes BkNG-Projekt:** die Projektdatei (`.apj`) des leeren Basisprojekts.
   - **BkNG-Basisprojekt aus ZIP-Datei:** die ZIP-Datei und einen Zielordner. Das
     Projekt wird dort als `<Quellname>_BkNG<Version>` entpackt, `<Version>` ist die
     BkNG-Version des Basisprojekts ohne Punkte (z. B. `A0648_BkNG301`). Gibt es diesen Ordner schon,
     bricht der Konverter ab.
4. Die angebotenen Optionen prüfen (siehe unten). Standardmäßig sind alle angehakt.
5. Auf **Konvertieren** klicken. Gibt es eine Datei oder Hardware-Konfiguration
   mehrfach, öffnet sich ein Auswahlfenster.
6. Enthält das Projekt danach Kontaktpläne im alten Format, führt ein Fenster durch
   ihre Konvertierung mit Automation Studio 6 (siehe [Kontaktpläne](#kontaktpläne)).
7. Am Ende zeigt ein Fenster die Zahl der offenen Punkte und bietet an, den Bericht zu
   öffnen. Die offenen Punkte abarbeiten, danach das Projekt in Automation Studio 6
   öffnen und übersetzen.

## Optionen

### Konvertierung eines Bk2000-Projekts

| Option | Wirkung |
|---|---|
| Kontaktplan-Tasks (.ld) kopieren | Kopiert alle Tasks mit Kontaktplänen samt Ordnerstruktur und trägt sie in dieselbe Taskklasse (Cpu.sw) ein wie im Bk2000-Projekt. |
| Datenmodule (.dat) kopieren | Kopiert alle Datenmodule außer `dplist`, `gruppen` und den Bk2000-Standardmodulen und trägt sie in die Cpu.sw ein. |
| X20-I/O-Module inkl. I/O-Mapping | Übernimmt die X20-Module (ohne die alte SPS) mit I/O-Mapping in die aktive Hardware-Konfiguration. PROFIBUS-Slaves werden mitgenommen, wenn ihre GSD-Datei im Bk2000-Projekt liegt. |
| Ethernet-Einstellungen der SPS | IF2: IP-Adresse, Subnetzmaske, Hostname, Modus (feste IP oder DHCP); Gateway; alle Einstellungen der IF3. |
| Bk-Datenpunkte in OpcUaBkV.uad freigeben | Gibt alle Bk-Datenpunkte mit ihren Unterelementen in OPC UA frei. |
| UVIS-Seiten umwandeln (experimentell) | Wandelt die VC4-Benutzervisu (UVis-Seiten) in BkVisu-Seiten um und legt Navigationsbuttons an. |

### Migration eines BkNG-Projekts (AS 4 oder AS 6)

| Option | Wirkung |
|---|---|
| Tasks, Datenmodule und Kundenbibliotheken | Übernimmt die in der alten Cpu.sw eingetragenen Tasks und Datenmodule (außer VisuTask) in dieselben Taskklassen, dazu weitere Dateien direkt unter `Logical` (z. B. `.url`-Verknüpfungen). Kundenbibliotheken nur mit Quellcode (`.c`, `.st`); keine B&R- oder Burklimat-Bibliotheken. |
| X20-I/O-Module / Ethernet-Einstellungen | Wie bei der Konvertierung (AS4-Projekt). |
| Hardware 1:1 (nur AS6-Projekt) | Übernimmt die Hardware des alten Projekts unverändert: CPU mit allen Einstellungen (auch Ethernet), alle Module, I/O-Mapping, DTM-Konfiguration und GSD-Dateien. Der CPU-Ordner des Basisprojekts wird nach der alten CPU benannt. Fehlende Einstellungen des Basisprojekts nennt der Bericht. |
| Benutzer und Rollen | Übernimmt Benutzer und Rollen, auch in die mapp-Benutzerverwaltung. Passwörter eines AS6-Projekts werden übernommen; die eines AS4-Projekts müssen neu eingegeben werden (AS6 speichert sie als Argon2-Hash). |
| Bk-Datenpunkte und OPC-UA-Variablen | Gibt die Bk-Datenpunkte und die weiteren OPC-UA-Variablen des alten Projekts frei. |
| Zertifikate und SSL-Konfigurationen | Übernimmt kundenspezifische Zertifikate, private Schlüssel und `*.sslcfg` in dieselben Ordner. Dateien der BkNG-Basis nur, wenn eine übernommene SSL-Konfiguration sie verwendet. Standardmäßig abgewählt. |
| mappView-Konfiguration | Ersetzt die `Config.mappviewcfg` des Basisprojekts durch die alte. Standardmäßig abgewählt. |
| OPC-UA-Default-View / Client/Server | Ersetzen die `UaDvConfig.uadcfg` bzw. `UaCsConfig.uacfg` des Basisprojekts durch die alte (falls vorhanden). Standardmäßig abgewählt. |
| Kundenvisu | Kopiert die eigenen Seiten mit Contents, Dialogen, eigenen Layouts, Medien, Texten, Session-Variablen, Bindings und Event-Bindings. `BkV_StartContent` und `BkV_NavigationContent` werden durch die alten ersetzt. |

Immer ausgeführt werden bei der Migration: Entfernen der Beispiele (`BkV_Sample`,
Testseiten), Ergänzen fehlender Deklarationen aus `Global.var`/`Global.typ` und
Übernahme der VisuTask-Einstellungen (`UST_Name`, `TimeAutoLogout`,
`InfosAufStartAusblenden`, `NavNurBeiLogin`).

## Was der Konverter zusätzlich erledigt

- **Kontaktpläne** im Format von Automation Studio < 6 werden mit Hilfe von Automation
  Studio 6 in dessen Format gebracht (siehe unten; entfällt bei einem AS6-Quellprojekt).
- **Parameternamen** in den Initialisierungen der Bk-Datenpunkte werden so geschrieben,
  wie der Baustein sie deklariert (z. B. `YMin` → `Ymin`). Automation Studio 6
  unterscheidet hier Groß- und Kleinschreibung.
- **PtoP** ist abgekündigt: PtoP-Tasks und -Bibliotheken werden nicht übernommen, das
  Datenmodul `ptop` schon. Der Bericht weist darauf hin, die Kopplung durch OPC UA zu
  ersetzen.
- **Abgekündigte Bibliotheken und Funktionsbausteine** von AS6 (z. B. `AsArLog` →
  `ArEventLog`, `MEMInfo` → `MEMxInfo`) werden mit Grund und Nachfolger gemeldet. Die
  Funktionsbausteine werden dafür im Programmcode der übernommenen Tasks gesucht.

## Kontaktpläne

Alte Kontaktpläne (Format von Automation Studio < 6) baut AS6 nicht (Build-Fehler 1113).
Automation Studio 6 konvertiert sie aber selbst, und zwar alle auf einmal, wenn es ein
Projekt von Automation Studio 4 öffnet (Projekt-Upgrade). Das nutzt der Konverter:

1. Als letzten Schritt legt er neben dem Zielprojekt ein kleines Hilfsprojekt für
   Automation Studio 4.12 an (`<Zielprojekt>_KOP_Migration`) und kopiert die Programme
   mit den alten Kontaktplänen hinein.
2. Ein Fenster führt durch die Konvertierung:
   - **Automation Studio 6 starten** öffnet das Hilfsprojekt in Automation Studio 6
     (oder man öffnet es selbst in AS6).
   - Das Projekt-Upgrade bestätigen und warten, bis das Projekt geöffnet ist. Keine
     Datei öffnen und nichts übersetzen.
   - **Fertig** schließt Automation Studio mit dem Hilfsprojekt, übernimmt die
     konvertierten Kontaktpläne in das Zielprojekt und löscht das Hilfsprojekt. Andere
     geöffnete Automation-Studio-Fenster bleiben unberührt. Schließt sich Automation
     Studio nicht innerhalb von 30 Sekunden (z. B. wegen einer offenen Rückfrage), wird
     es beendet; die konvertierten Pläne sind dann bereits gespeichert. Sind nicht alle
     Pläne konvertiert, fragt der Konverter nach; das Hilfsprojekt mit den übrigen Plänen
     bleibt dann erhalten.
   - **Überspringen** lässt die Pläne im alten Format und löscht das Hilfsprojekt.
3. Pläne, die im alten Format bleiben, nennt der Bericht als offenen Punkt: jeden
   einmal in Automation Studio 6 öffnen und speichern.

Vorher ergänzt der Konverter in den alten Plänen den Bausteintyp: Im alten Format 1.0
steht er nur im oberen Teil eines Bausteins, Automation Studio 6 übernimmt ihn beim
Konvertieren aber aus dem Kopf. Ohne diese Ergänzung wären Bausteine wie `BkAl` nach der
Konvertierung leer.

## Konvertierungsbericht

Nach jedem Lauf speichert der Konverter im BkNG-Projektordner einen Bericht
(`Konvertierungsbericht_<Datum>_<Uhrzeit>.txt`). Oben stehen unter **ZU ERLEDIGEN**
alle Warnungen und Fehler als offene Punkte `[ ]`, darunter die Details jedes
Schritts. Bei einer Bk2000-Konvertierung enthält er außerdem eine Analyse: eine
PtoP-Kopplung und alle kundenspezifischen Bibliotheken.

### Typische offene Punkte

- Kontaktpläne, die im alten Format geblieben sind: jeden einmal in Automation Studio 6
  öffnen und speichern (sonst Build-Fehler 1113).
- Verschlüsselte Bibliotheken: mit dem *Decrypt Files Wizard* von Automation Studio
  entschlüsseln (Passwort nötig).
- Fehlende B&R-Bibliotheken über die Toolbox hinzufügen; abgekündigte Bibliotheken
  und Funktionsbausteine durch ihre Nachfolger ersetzen; Kundenbibliotheken ohne
  Quellcode durch eine AS6-Version ersetzen.
- PtoP-Kopplung durch OPC UA ersetzen.
- Passwörter der übernommenen Benutzer neu eingeben.
- CPU-Mappings werden auf die neue SPS umbenannt: prüfen, ob die Kanäle dort
  existieren.
- PROFIBUS-Geräte: die Gerätekonfiguration (DTM) in Automation Studio öffnen und
  prüfen.
- Nicht übernommene Geräte und Objekte bei Bedarf von Hand ergänzen.

### Fehler

Bricht ein Schritt ab, steht der Fehler als offener Punkt im Bericht; die folgenden
Schritte werden nicht ausgeführt. Ursache beheben und die Konvertierung erneut
ausführen, am besten in ein frisches Basisprojekt. Fehlen im Bk2000-Projekt
`dplist.dat` oder `gruppen.dat`, meldet der Konverter das vorab und ändert nichts.

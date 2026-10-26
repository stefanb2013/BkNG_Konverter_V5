# BkNG-Konverter V4 / BkNG Converter V4

[Deutsch](#deutsch) | [English](#english)

---

## Deutsch

Der Konverter überführt Burklimat-Bk2000-Projekte und BkNG-Projekte aus Automation
Studio 4 in das BkNG-Basisprojekt für Automation Studio 6.

Anleitung für Anwender: [ANLEITUNG.md](ANLEITUNG.md)

> **Hinweis:** Dieses Projekt wurde mit Agentic Engineering erstellt, wird nicht
> supportet und erhebt keinen Anspruch auf Vollständigkeit. Siehe
> [Haftungsausschluss](#haftungsausschluss).
>
> **Hinweis:** Eine Übersicht der verwendeten Komponenten und ihrer Lizenzen finden Sie
> im Abschnitt [Lizenzen und Bibliotheken](#lizenzen-und-bibliotheken).

### Voraussetzungen

- Windows

- Python 3.10 oder neuer (getestet mit 3.14) mit tkinter, wie es der Installer von
  python.org mitbringt. Der Konverter nutzt nur die Standardbibliothek von Python;
  mit pip ist nichts zu installieren.
- Für die Konvertierung alter Kontaktpläne: Automation Studio 6 (siehe
  [Kontaktpläne](#kontaktpläne)).

#### Schritt für Schritt: Python installieren und den Konverter starten

**1. Python installieren** (nur einmal nötig)

1. Im Browser <https://www.python.org/downloads/> öffnen und auf den gelben Knopf
   **„Download Python 3.x“** klicken.
2. Die heruntergeladene Datei (z. B. `python-3.14.x-amd64.exe`) per Doppelklick starten.
3. **Wichtig:** Im ersten Fenster ganz unten den Haken bei
   **„Add python.exe to PATH“** setzen.
4. Auf **„Install Now“** klicken und warten, bis „Setup was successful“ erscheint.
   Dann **„Close“**.

**2. Prüfen, ob Python funktioniert**

1. Windows-Taste drücken, `cmd` eintippen und die **Eingabeaufforderung** öffnen.
2. `python --version` eintippen und Enter drücken.
3. Erscheint z. B. `Python 3.14.0`, ist alles in Ordnung. Das Fenster kann zu.

**3. Den Konverter holen**

1. Auf der Seite des Repositorys auf den grünen Knopf **„Code“** und dann auf
   **„Download ZIP“** klicken (oder die ZIP-Datei nehmen, die man bekommen hat).
2. Die ZIP-Datei mit der rechten Maustaste anklicken → **„Alle extrahieren…“** und
   einen Ordner wählen, z. B. `C:\BkNG-Konverter`.

**4. Den Konverter starten**

1. Im Explorer den Ordner mit `bkng_converter.py` öffnen.
2. Oben in die Adressleiste klicken, `cmd` eintippen und Enter drücken. Es öffnet sich
   eine Eingabeaufforderung direkt in diesem Ordner.
3. `python bkng_converter.py` eintippen und Enter drücken. Das Fenster des Konverters
   erscheint.
4. Das schwarze Fenster der Eingabeaufforderung offen lassen, solange der Konverter
   läuft (es schließt sich nicht von selbst; danach einfach schließen).

Tipp: Für einen schnellen Start eine Verknüpfung anlegen: `bkng_converter.py` mit der
rechten Maustaste anklicken → (unter Windows 11 zuerst **„Weitere Optionen anzeigen“**)
→ **„Senden an“** →
**„Desktop (Verknüpfung erstellen)“**. Ein Doppelklick darauf startet den Konverter
(falls Windows fragt, womit die Datei geöffnet werden soll: **„Python“** wählen).

**Wenn etwas nicht klappt**

| Meldung / Verhalten | Lösung |
|---|---|
| `"python" wird nicht als interner oder externer Befehl … erkannt` | Bei der Installation fehlte der Haken „Add python.exe to PATH“. Statt `python` den Befehl `py` verwenden (`py bkng_converter.py`) oder den Python-Installer erneut starten → **„Modify“** → weiter → Haken **„Add Python to environment variables“** setzen → **„Install“**. |
| Statt Python öffnet sich der **Microsoft Store** | Windows-Einstellungen → **Apps** → **Erweiterte App-Einstellungen** → **App-Ausführungsaliase**: die Schalter für `python.exe` und `python3.exe` ausschalten. Oder `py` statt `python` verwenden. |
| `ModuleNotFoundError: No module named 'tkinter'` | Python-Installer erneut starten → **„Modify“** → Haken **„tcl/tk and IDLE“** setzen → weiter → **„Install“**. |
| `can't open file … bkng_converter.py` | Die Eingabeaufforderung ist im falschen Ordner. Wie in Schritt 4 über die Adressleiste des richtigen Ordners öffnen. |

### Start

Repository holen (klonen oder als ZIP herunterladen) und die Anwendung in seinem
Ordner starten:

```powershell
python bkng_converter.py
```

Die Projektdateien (`.apj`) des Bk2000-Projekts und des BkNG-Projekts auswählen. Das
BkNG-Projekt muss ein leeres BkNG-Projekt (BkNG-Basisprojekt) sein.

Das Quellprojekt kann auch eine ZIP-Datei sein. Der Konverter entpackt sie in einen
temporären Ordner, der beim Beenden gelöscht wird; die ZIP-Datei selbst bleibt
unverändert. Wie beim Basisprojekt darf das Projekt direkt in der ZIP-Datei oder in
einem Ordner darin liegen.

Statt eines bestehenden BkNG-Projekts kann ein gezipptes BkNG-Basisprojekt mit einem
Zielordner gewählt werden. Der Konverter entpackt das Projekt nach
`<Zielordner>\<Quellname>_AS6`, benennt seine `.apj`-Datei in `<Quellname>_AS6.apj`
um und konvertiert dann in dieses Projekt. Das Projekt darf direkt in der ZIP-Datei
oder in einem Ordner darin liegen. Gibt es den Projektordner schon, wird nichts
entpackt.

Nach der Konvertierung speichert der Konverter im BkNG-Projektordner einen Bericht
(`Konvertierungsbericht_<Datum>_<Uhrzeit>.txt`, auf Englisch
`Conversion_report_…`). Oben stehen alle Warnungen und Fehler als offene Punkte
(`[ ]`), darunter die Details jedes Schritts. Das Abschlussfenster zeigt nur die Zahl
der offenen Punkte und bietet an, den Bericht zu öffnen.

### Konvertierung eines Bk2000-Projekts

Der Bericht enthält eine Analyse des Bk2000-Projekts: Eine PtoP-Kopplung
(`ptop.dat`) wird als offener Punkt gemeldet (sie sollte durch OPC UA ersetzt
werden), und alle Bibliotheken der Bk2000-Cpu.sw, die keine B&R-Standardbibliotheken
sind, werden als kundenspezifische Bibliotheken aufgeführt. Optional werden alle
Datenmodule (`.dat`) außer `dplist` und `gruppen` in das BkNG-Projekt kopiert und in
seine Cpu.sw eingetragen.

Beim Kopieren der I/O-Konfiguration werden PROFIBUS-Slaves an kopierten Modulen
mitgenommen, wenn ihre GSD-Datei im Bk2000-Projekt liegt (`AS\Hardware`): das Gerät,
sein I/O-Mapping, die GSD-Datei mit ihrer `.meta`-Datei und die DTM-Konfiguration
des Masters (`<CPU>\DTM\<Master>`). Andere Geräte, die keine X20-Module sind, stehen
als offene Punkte im Bericht.

Der Konverter durchsucht den Ordner jeder `.apj`-Datei mit allen Unterordnern nach
diesen Dateien:

- Bk2000-Projekt: `dplist.dat`, `gruppen.dat` und `global.var`
- BkNG-Projekt: genau eine `global.var`

Fehlt im Bk2000-Projekt `dplist.dat` oder `gruppen.dat`, nennt der Konverter die
fehlenden Dateien und fordert auf, ein Bk2000-Projekt zu wählen.

Er ersetzt die neue `global.var` durch die konvertierten Deklarationen. Fehlt eine
benötigte Datei oder gibt es mehrere passende, bricht die Konvertierung ab, ohne das
neue Projekt zu ändern.

Das Ziel ist immer ein Projekt für Automation Studio 6.

### Migration eines BkNG-Projekts von Automation Studio 4 nach AS6

Ist das gewählte Quellprojekt ein BkNG-Projekt (ab AS 4.4, mit dem Programm
`VisuTask`), migriert der Konverter es, statt es zu konvertieren. Die Oberfläche
zeigt die erkannte Art unter dem Quellprojekt an und bietet die passenden Optionen.
Im ZIP-Modus wird das Basisprojekt als `<Quellname>_AS6` entpackt. Die Migration
braucht keine `dplist.dat`, `gruppen.dat` oder VC4-Visu. Sie läuft so ab:

1. Sie entfernt die Beispiele aus dem neuen Basisprojekt: das Programm und die Seiten
   `BkV_Sample` sowie die Testseiten `PageTest` und `PageLineTest*`, dazu deren
   Bindings, `.vis`-Einträge, Cpu.sw-Einträge und OPC-UA-Einträge.
2. Sie ergänzt die Deklarationen der alten `Global.var` und `Global.typ`, die dem
   Basisprojekt fehlen. Deklarationen, die in beiden stehen, bleiben wie im
   Basisprojekt.
3. Sie übernimmt die VisuTask-Einstellungen `UST_Name`, `TimeAutoLogout`,
   `InfosAufStartAusblenden` und `NavNurBeiLogin`. Die Grenzen `*_MAXDP` bleiben wie
   im Basisprojekt.
4. Sie kopiert die in der alten Cpu.sw eingetragenen Tasks und Datenmodule (außer der
   VisuTask) und trägt sie in dieselben Taskklassen ein. Kundenbibliotheken werden nur
   übernommen, wenn ihr Quellcode (`.c`, `.st`) enthalten ist; `bktools` und andere
   Burklimat- oder B&R-Bibliotheken nie.
5. Sie übernimmt die I/O-Konfiguration und die Ethernet-Einstellungen wie bei der
   Bk2000-Konvertierung.
6. Sie übernimmt Benutzer und Rollen, auch in die mapp-Benutzerverwaltung
   (`.mpuserx`). Passwörter lassen sich nicht übernehmen, weil AS6 sie als
   Argon2-Hash speichert; der Bericht fordert auf, sie neu einzugeben.
7. Sie gibt die Bk-Datenpunkte und die weiteren OPC-UA-Variablen des alten Projekts
   frei.
8. Sie kopiert die Kundenvisu: die Seiten, die das Basisprojekt nicht kennt
   (BkVisu-Seiten), mit ihren Contents, Dialogen, Medien, Texten und
   Session-Variablen. `BkV_StartContent` und `BkV_NavigationContent` werden durch die
   alten ersetzt. Alle Bindings und Event-Bindings dieser Contents kommen in
   `BkV_Bindings.binding` / `BkV_EventBindings.eventbinding` (Package `BkVisu` neben
   der `.vis`-Datei). Event-Bindings, die das Basisprojekt schon hat, werden nicht
   doppelt angelegt.

### Kontaktpläne

Kontaktpläne im Format von AS < 6 baut Automation Studio 6 nicht (Build-Fehler 1113).
Es konvertiert sie aber alle selbst, wenn es ein Projekt von Automation Studio 4 öffnet
(dokumentiertes Projekt-Upgrade). Darauf baut der letzte Schritt des Konverters auf (in
beiden Betriebsarten):

1. Er legt neben dem Zielprojekt ein minimales Projekt für Automation Studio 4.12 an
   (`<Zielprojekt>_KOP_Migration`: Projektdatei, Packages, Hardware nur mit einer CPU;
   vom Konverter selbst geschrieben, ohne Dateien von B&R) und kopiert die Programme mit
   den alten Kontaktplänen hinein.
2. Ein Dialog bittet, das Hilfsprojekt in Automation Studio 6 zu öffnen (auf Wunsch
   startet der Konverter AS6 damit) und das Upgrade zu bestätigen.
3. Mit „Fertig“ schließt er das Automation Studio, in dem das Hilfsprojekt geöffnet ist
   (erkannt am Fenstertitel; schließt es sich nicht innerhalb von 30 Sekunden, wird es
   beendet), übernimmt die konvertierten Pläne in das Zielprojekt und löscht das
   Hilfsprojekt. Pläne, die nicht konvertiert wurden oder bei „Überspringen“, bleiben im
   alten Format; der Bericht nennt sie zum einmaligen Öffnen und Speichern in AS6.

Vorher ergänzt der Konverter in jedem Teil eines Bausteins den Bausteintyp, den AS6
beim Konvertieren aus dem Kopf des Bausteins übernimmt (das alte Format 1.0 hat ihn nur
im oberen Teil).

### Offene Punkte im Bericht

Der Bericht nennt als offene Punkte, was der Konverter nicht selbst erledigen kann:

- Kontaktpläne, die im alten Format geblieben sind, einmal in AS6 öffnen und speichern
  (sonst Build-Fehler 1113).
- Verschlüsselte Bibliotheksquellen entschlüsseln.
- Fehlende B&R-Bibliotheken ergänzen; in AS6 abgekündigte Bibliotheken und Funktionen
  ersetzen.
- PtoP ist abgekündigt: Seine Tasks und Bibliotheken werden nicht übernommen (sein
  Datenmodul schon); die Kopplung ist durch OPC UA zu ersetzen.
- Andere nicht eingetragene Objekte bei Bedarf von Hand übernehmen.

### Haftungsausschluss

- **Agentic Engineering:** Dieses Projekt wurde mit Agentic Engineering erstellt:
  Quellcode, Tests und Dokumentation sind überwiegend von einem KI-Agenten (Claude
  Code von Anthropic) unter Anleitung und Prüfung eines Menschen geschrieben worden.
  Es kann trotz dieser Prüfung Fehler enthalten.
- **Kein Produkt:** Weder der Autor noch der Eigentümer des Repositorys verkaufen
  Inhalte dieses Repositorys als Produkt. Für die Codebasis wird kein kommerzieller
  Support angeboten. Die Inhalte dieses Repositorys werden nicht als kommerzielles
  Softwareprodukt gepflegt und sind nicht als solches zu verstehen.
- **Kein Support:** Das Programm wird nicht supportet. Es gibt keine Wartung, keine
  Fehlerbehebung auf Anfrage und keine zugesicherten Antwortzeiten.
- **Keine Vollständigkeit:** Es besteht kein Anspruch auf Vollständigkeit oder
  Richtigkeit, weder der Konvertierung noch dieser Dokumentation oder der folgenden
  Aufstellung der Lizenzen und Bibliotheken.
- **Keine Gewährleistung:** Die Software wird „wie besehen“ bereitgestellt, ohne
  jede ausdrückliche oder stillschweigende Gewährleistung (siehe [LICENSE](LICENSE)).
  Der Konverter ändert Projekte direkt: Er darf nur auf Kopien bzw. nach einer
  Sicherung angewendet werden. Jedes Ergebnis ist vor dem Einsatz an einer Anlage
  fachkundig zu prüfen, zu übersetzen und zu testen. Die Verantwortung für den Einsatz
  liegt allein beim Anwender.
- **Marken:** B&R, Automation Studio und mappView sind Marken der B&R Industrial
  Automation GmbH, Windows eine Marke der Microsoft Corporation, Python eine
  Marke der Python Software Foundation. Dieses Projekt steht in keiner Verbindung zu
  diesen Unternehmen und wird von ihnen weder unterstützt noch geprüft.

### Lizenzen und Bibliotheken

Der Konverter selbst steht unter der **MIT-Lizenz** (siehe [LICENSE](LICENSE)). Das
Repository enthält nur Quellcode und das Programmsymbol; Python und Windows werden nicht
mitgeliefert, sondern beim Anwender vorausgesetzt. Für sie gelten ihre eigenen
Lizenzen:

| Komponente | Verwendung | Lizenz | Mitgeliefert |
|---|---|---|---|
| BkNG-Konverter (`bkng_converter.py`) | das Programm selbst | MIT | ja |
| Programmsymbol (`BkNgKonvIcon.ico`) | Symbol des Programmfensters, eigene Erstellung | MIT | ja |
| Python 3 mit Standardbibliothek | Laufzeitumgebung | Python Software Foundation License v2 | nein |
| Tcl/Tk (über `tkinter`, Teil der Python-Installation) | Oberfläche | Tcl/Tk-Lizenz (BSD-artig) | nein |
| Weitere Bestandteile der Python-Installation, z. B. Expat (XML), zlib (ZIP), libffi (`ctypes`) | Standardbibliothek | MIT, zlib-Lizenz | nein |
| Automation Studio 6 | wird auf Wunsch gestartet, um alte Kontaktpläne per Projekt-Upgrade zu konvertieren | proprietär, B&R Industrial Automation GmbH | nein |

**Verwendete Python-Module** (alle aus der Standardbibliothek, keine pip-Pakete):
`tkinter` (`ttk`, `filedialog`, `messagebox`, `scrolledtext`), `pathlib`, `os`,
`shutil`, `stat`, `subprocess`, `tempfile`, `zipfile`, `re`, `xml.etree.ElementTree`,
`threading`, `queue`, `ctypes`, `uuid`, `datetime`, `dataclasses`, `collections`,
`typing`.

---

## English

The converter transfers Burklimat Bk2000 projects and BkNG projects of Automation
Studio 4 into the BkNG base project of Automation Studio 6.

User guide (German): [ANLEITUNG.md](ANLEITUNG.md)

> **Notice:** This project was created with agentic engineering, is not supported and
> makes no claim to completeness. See [Disclaimer](#disclaimer).
>
> **Notice:** For an overview of the components used and their licenses, see
> [Licenses and libraries](#licenses-and-libraries).

### Requirements

- Windows
- Python 3.10 or newer (tested with 3.14) including tkinter, as installed by the
  installer from python.org. The converter uses only Python's standard library;
  there is nothing to install with pip.
- For the conversion of old Ladder Diagrams: Automation Studio 6 (see
  [Ladder Diagrams](#ladder-diagrams)).

#### Step by step: install Python and start the converter

**1. Install Python** (needed only once)

1. Open <https://www.python.org/downloads/> in the browser and click the yellow button
   **"Download Python 3.x"**.
2. Start the downloaded file (e.g. `python-3.14.x-amd64.exe`) with a double-click.
3. **Important:** In the first window, tick **"Add python.exe to PATH"** at the very
   bottom.
4. Click **"Install Now"** and wait until "Setup was successful" appears. Then
   **"Close"**.

**2. Check that Python works**

1. Press the Windows key, type `cmd` and open the **Command Prompt**.
2. Type `python --version` and press Enter.
3. If e.g. `Python 3.14.0` appears, everything is fine. The window can be closed.

**3. Get the converter**

1. On the page of the repository, click the green button **"Code"** and then
   **"Download ZIP"** (or take the ZIP file you were given).
2. Right-click the ZIP file → **"Extract All…"** and choose a folder, e.g.
   `C:\BkNG-Konverter`.

**4. Start the converter**

1. Open the folder containing `bkng_converter.py` in Explorer.
2. Click into the address bar at the top, type `cmd` and press Enter. A Command Prompt
   opens directly in this folder.
3. Type `python bkng_converter.py` and press Enter. The window of the converter appears.
4. Leave the black Command Prompt window open while the converter is running (it does
   not close by itself; just close it afterwards).

Tip: For a quick start, create a shortcut: right-click `bkng_converter.py` →
(on Windows 11 first **"Show more options"**) → **"Send to"** → **"Desktop (create
shortcut)"**. A
double-click on it starts the converter (if Windows asks which program to open the
file with: choose **"Python"**).

**If something does not work**

| Message / behaviour | Solution |
|---|---|
| `'python' is not recognized as an internal or external command …` | The tick "Add python.exe to PATH" was missing during installation. Use `py` instead of `python` (`py bkng_converter.py`), or start the Python installer again → **"Modify"** → next → tick **"Add Python to environment variables"** → **"Install"**. |
| The **Microsoft Store** opens instead of Python | Windows Settings → **Apps** → **Advanced app settings** → **App execution aliases**: switch off `python.exe` and `python3.exe`. Or use `py` instead of `python`. |
| `ModuleNotFoundError: No module named 'tkinter'` | Start the Python installer again → **"Modify"** → tick **"tcl/tk and IDLE"** → next → **"Install"**. |
| `can't open file … bkng_converter.py` | The Command Prompt is in the wrong folder. Open it via the address bar of the right folder as in step 4. |

### Running

Get the repository (clone or download it as ZIP) and start the desktop
application in its folder:

```powershell
python bkng_converter.py
```

Choose the Automation Studio project files (`.apj`) of the Bk2000 project and the
BkNG project. The BkNG project must be an empty BkNG project (BkNG base project).

The source project may also be a ZIP file. The converter unpacks it into a
temporary folder, which is deleted when the program exits; the ZIP file itself is
not changed. As with the base project, the project may lie at the top of the ZIP
file or in a folder of it.

Instead of an existing BkNG project you can select a zipped BkNG base project and
a target folder. The converter unpacks the project into
`<target folder>\<source name>_AS6` and renames its `.apj` file to
`<source name>_AS6.apj`; it then converts into this project. The project may lie
at the top of the ZIP file or in a folder of it. If the project folder already
exists, nothing is unpacked.

After the conversion the converter saves a report
(`Konvertierungsbericht_<date>_<time>.txt`, or `Conversion_report_…` in English)
in the BkNG project folder. It lists all warnings and errors as open items
(`[ ]`) at the top, followed by the details of every conversion step. The final
dialog only shows the number of open items and offers to open the report.

### Converting a Bk2000 project

The report contains an analysis of the Bk2000 project: a PtoP coupling
(`ptop.dat`) is reported as open item (it should be replaced by OPC UA), and all
libraries of the Bk2000 Cpu.sw that are not B&R standard libraries are listed as
customer-specific libraries. Optionally, all data modules (`.dat`) except
`dplist` and `gruppen` are copied into the BkNG project and added to its Cpu.sw.

When copying the I/O configuration, PROFIBUS slaves at copied modules are taken
over as well if their GSD file is stored in the Bk2000 project (`AS\Hardware`):
the device, its I/O mapping, the GSD file with its `.meta` file and the DTM
configuration of the master (`<CPU>\DTM\<master>`). Other non-X20 devices are
listed as open items in the report.

The converter uses the folder containing each `.apj` file and searches all of its
subdirectories for these files:

- Bk2000 project: `dplist.dat`, `gruppen.dat`, and `global.var`
- BkNG project: exactly one `global.var`

If `dplist.dat` or `gruppen.dat` is missing from the Bk2000 project, the converter
lists the missing files and asks you to select a Bk2000 project.

It replaces the new `global.var` with the converted declarations. If a required
file is absent, or more than one matching file is present, conversion stops
without modifying the new project.

The target is always an Automation Studio 6 project.

### Migrating a BkNG project from Automation Studio 4 to AS6

If the selected source project is a BkNG project (AS 4.4 or later, with a
program `VisuTask`), the converter migrates it instead of converting it. The
GUI shows the detected kind below the source project and offers the matching
options. In ZIP mode the base project is unpacked as `<source name>_AS6`.
Migration needs no `dplist.dat`, `gruppen.dat` or VC4 visu. It works as follows:

1. It removes the examples from the new base project: the `BkV_Sample` program and
   pages, and the test pages `PageTest` and `PageLineTest*`. It also removes their
   bindings, `.vis` entries, Cpu.sw entries and OPC UA entries.
2. It adds the declarations of the old `Global.var` and `Global.typ` that the base
   project lacks. Declarations present in both are kept as in the base project.
3. It takes over the VisuTask settings `UST_Name`, `TimeAutoLogout`,
   `InfosAufStartAusblenden` and `NavNurBeiLogin`. The `*_MAXDP` limits stay as in
   the base project.
4. It copies the tasks and data modules that are deployed in the old Cpu.sw (except
   the VisuTask) and adds them to the same task classes. Customer libraries are
   taken over only if their source code (`.c`, `.st`) is included. It never takes
   over `bktools` or other Burklimat or B&R libraries.
5. It takes over the I/O configuration and Ethernet settings, as in the Bk2000
   conversion.
6. It takes over the users and roles, also into the mapp user management
   (`.mpuserx`). Passwords cannot be taken over because AS6 stores them as Argon2
   hashes. The report asks you to enter them again.
7. It enables the Bk datapoints and the further OPC UA variables of the old project.
8. It copies the customer visu: the pages unknown to the base project (BkVisu pages)
   with their contents, dialogs, media, texts and session variables.
   `BkV_StartContent` and `BkV_NavigationContent` are replaced by the old ones. All
   bindings and event bindings of these contents go into `BkV_Bindings.binding` /
   `BkV_EventBindings.eventbinding` (package `BkVisu` next to the `.vis` file).
   Event bindings the base project already has are not duplicated.

### Ladder Diagrams

Automation Studio 6 does not build Ladder Diagram files in the format of AS < 6 (build
error 1113). It converts all of them itself, however, when it opens a project of
Automation Studio 4 (documented project upgrade). The last step of the converter (both
modes) relies on this:

1. It creates a minimal project of Automation Studio 4.12 next to the target project
   (`<target project>_KOP_Migration`: project file, packages, hardware with one CPU only;
   written by the converter itself, without files of B&R) and copies the programs with
   the old Ladder Diagrams into it.
2. A dialog asks to open the helper project in Automation Studio 6 (on request the
   converter starts AS6 with it) and to confirm the upgrade.
3. With "Done" it closes the Automation Studio that has the helper project open
   (recognized by its window title; if it does not close within 30 seconds, it is ended),
   takes the converted diagrams over into the target project and deletes the helper
   project. Diagrams that were not converted, or after "Skip", stay in the old
   format; the report lists them to be opened and saved once in AS6.

Before, the converter completes the block type in every part of a block, which AS6
takes from the header of the block when it converts the file (the old format 1.0 has it
in the top part only).

### Open items in the report

The report lists as open items what the converter cannot do itself:

- Ladder Diagram files that stayed in the old format must be opened and saved once in
  AS6 (otherwise build error 1113).
- Encrypted library sources must be decrypted.
- Missing B&R libraries must be added; libraries and functions discontinued in AS6
  must be replaced.
- PtoP is discontinued: its tasks and libraries are not taken over (its data module
  is); the coupling must be replaced by OPC UA.
- Other objects that are not deployed must be taken over manually if needed.

### Disclaimer

- **Agentic engineering:** This project was created with agentic engineering: its
  source code, tests and documentation were mostly written by an AI agent (Claude
  Code by Anthropic), directed and reviewed by a human. Despite this review it may
  contain errors.
- **No product:** Neither the author nor the repository owner do sell anything out of this content as a product. A commercial support service for the codebase is not operated. The content of this repository is not maintained as a commercial software product and should not be interpreted as such.
- **No support:** The program is not supported. There is no maintenance, no bug
  fixing on request and no guaranteed response time.
- **No claim to completeness:** No claim is made to completeness or correctness,
  neither of the conversion nor of this documentation or of the following list of
  licenses and libraries.
- **No warranty:** The software is provided "as is", without warranty of any kind,
  express or implied (see [LICENSE](LICENSE)). The converter changes projects
  directly: only use it on copies or after a backup. Every result must be checked,
  built and tested by a qualified person before it is used on a plant. The user
  alone is responsible for its use.
- **Trademarks:** B&R, Automation Studio and mappView are trademarks of B&R
  Industrial Automation GmbH, Windows a trademark of Microsoft Corporation,
  Python a trademark of the Python Software Foundation. This project is not
  affiliated with, endorsed or reviewed by these companies.

### Licenses and libraries

The converter itself is licensed under the **MIT License** (see [LICENSE](LICENSE)).
The repository contains source code and the program icon only; Python and Windows are
not included but required on the user's computer. Their own licenses apply:

| Component | Used for | License | Included |
|---|---|---|---|
| BkNG Converter (`bkng_converter.py`) | the program itself | MIT | yes |
| Program icon (`BkNgKonvIcon.ico`) | icon of the program window, own work | MIT | yes |
| Python 3 with its standard library | runtime | Python Software Foundation License v2 | no |
| Tcl/Tk (via `tkinter`, part of the Python installation) | user interface | Tcl/Tk license (BSD-style) | no |
| Further parts of the Python installation, e.g. Expat (XML), zlib (ZIP), libffi (`ctypes`) | standard library | MIT, zlib License | no |
| Automation Studio 6 | started on request to convert old Ladder Diagrams by its project upgrade | proprietary, B&R Industrial Automation GmbH | no |

**Python modules used** (all from the standard library, no pip packages):
`tkinter` (`ttk`, `filedialog`, `messagebox`, `scrolledtext`), `pathlib`, `os`,
`shutil`, `stat`, `subprocess`, `tempfile`, `zipfile`, `re`, `xml.etree.ElementTree`,
`threading`, `queue`, `ctypes`, `uuid`, `datetime`, `dataclasses`, `collections`,
`typing`.
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

### Voraussetzungen

- Windows
- Python 3.10 oder neuer (getestet mit 3.14) mit tkinter, wie es der Installer von
  python.org mitbringt. Der Konverter nutzt nur die Standardbibliothek von Python;
  mit pip ist nichts zu installieren.
- Optional: Automation Studio 6, um alte Kontaktpläne automatisch zu konvertieren
  (siehe unten).

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

Mit der Option *Alte Kontaktpläne mit Automation Studio 6 konvertieren* (standardmäßig
aus; beim Anhaken erscheint ein Hinweis, dass AS6 installiert sein muss) werden
Kontaktpläne im Format von AS < 6 in beiden Betriebsarten mit der Konvertierung des
installierten Automation Studio 6 in das AS6-Format gebracht, also mit derselben, die
sein Editor beim Öffnen und Speichern einer solchen Datei nutzt. Das Hilfsprogramm `as6_ladder/LdConvert.cs` ruft sie auf; der Konverter übersetzt es
beim ersten Einsatz mit dem C#-Compiler des .NET Framework und legt es unter
`%LOCALAPPDATA%\BkNG-Konverter` ab. Vorher ergänzt er, was der AS6-Editor aus den
Bibliotheken nehmen würde: den Typ jedes Bausteinteils, die Namen erweiterbarer
Eingänge (`IN1`, `IN2`, …) und ein leeres Netzwerk in einem leeren Plan; den
Bausteintyp ergänzt er auch ohne die Option. Ohne die Option, ohne Automation Studio 6
(oder wenn die Umgebungsvariable `BKNG_AS6_BIN` leer gesetzt ist) bleiben die Dateien
im alten Format, und der Bericht weist darauf hin. Über die
Umgebungsvariable lässt sich auch der `Bin-de`-Ordner eines bestimmten AS6 vorgeben.

### Offene Punkte im Bericht

Der Bericht nennt als offene Punkte, was der Konverter nicht selbst erledigen kann:

- Kontaktpläne, die nicht konvertiert werden konnten, einmal in AS6 öffnen und
  speichern (sonst Build-Fehler 1113).
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
  Automation GmbH, Windows und .NET Marken der Microsoft Corporation, Python eine
  Marke der Python Software Foundation. Dieses Projekt steht in keiner Verbindung zu
  diesen Unternehmen und wird von ihnen weder unterstützt noch geprüft.

### Lizenzen und Bibliotheken

Der Konverter selbst steht unter der **MIT-Lizenz** (siehe [LICENSE](LICENSE)). Das
Repository enthält nur Quellcode; Python, Windows und Automation Studio werden nicht
mitgeliefert, sondern beim Anwender vorausgesetzt. Für sie gelten ihre eigenen
Lizenzen:

| Komponente | Verwendung | Lizenz | Mitgeliefert |
|---|---|---|---|
| BkNG-Konverter (`bkng_converter.py`, `as6_ladder/LdConvert.cs`) | das Programm selbst | MIT | ja |
| Python 3 mit Standardbibliothek | Laufzeitumgebung | Python Software Foundation License v2 | nein |
| Tcl/Tk (über `tkinter`, Teil der Python-Installation) | Oberfläche | Tcl/Tk-Lizenz (BSD-artig) | nein |
| Weitere Bestandteile der Python-Installation, z. B. Expat (XML), zlib (ZIP), OpenSSL (`hashlib`) | Standardbibliothek | MIT, zlib-Lizenz, Apache 2.0 | nein |
| .NET Framework 4.x mit C#-Compiler `csc.exe` | übersetzt und führt das Hilfsprogramm für die Kontaktplan-Konvertierung aus (optional) | Microsoft-Lizenz (Bestandteil von Windows) | nein |
| Automation Studio 6: `BR.AS.Iec.GraphicalEditor.Data.dll`, `BR.AS.Iec.GraphicalEditor.CodeAnalysis.dll` und deren Abhängigkeiten | Kontaktplan-Konvertierung (optional), zur Laufzeit aus der Installation des Anwenders geladen | proprietär, B&R Industrial Automation GmbH | nein |

**Verwendete Python-Module** (alle aus der Standardbibliothek, keine pip-Pakete):
`tkinter` (`ttk`, `filedialog`, `messagebox`, `scrolledtext`), `pathlib`, `os`,
`shutil`, `tempfile`, `zipfile`, `subprocess`, `re`, `xml.etree.ElementTree`,
`threading`, `queue`, `hashlib`, `uuid`, `datetime`, `dataclasses`, `collections`,
`typing`.

**Verwendete .NET-Namensräume** im Hilfsprogramm `LdConvert.cs`: `System`,
`System.IO`, `System.Linq`, `System.Reflection`, `System.Text`.

**Hinweis zu Automation Studio:** Die Kontaktplan-Konvertierung ruft interne, nicht
dokumentierte Klassen von Automation Studio 6 auf. Sie ist nur mit einer gültig
lizenzierten Installation nutzbar; ob ihr Einsatz mit den Lizenzbedingungen von B&R
vereinbar ist, muss der Anwender selbst prüfen. Die Funktion ist eine Option, die
standardmäßig ausgeschaltet ist.

---

## English

The converter transfers Burklimat Bk2000 projects and BkNG projects of Automation
Studio 4 into the BkNG base project of Automation Studio 6.

User guide (German): [ANLEITUNG.md](ANLEITUNG.md)

> **Notice:** This project was created with agentic engineering, is not supported and
> makes no claim to completeness. See [Disclaimer](#disclaimer).

### Requirements

- Windows
- Python 3.10 or newer (tested with 3.14) including tkinter, as installed by the
  installer from python.org. The converter uses only Python's standard library;
  there is nothing to install with pip.
- Optional: Automation Studio 6, to convert old Ladder Diagrams automatically
  (see below).

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

With the option *Convert old Ladder Diagrams into the AS6 format with Automation
Studio 6* (off by default; when it is ticked, a dialog says that AS6 must be
installed), Ladder Diagram files in the format of AS < 6 (both modes) are converted
into the format of AS6 with the conversion of the installed Automation Studio 6, the
same one its editor uses when such a file is opened and saved. The helper program
`as6_ladder/LdConvert.cs` calls it; the converter compiles it on first use with the
C# compiler of the .NET Framework and keeps it in
`%LOCALAPPDATA%\BkNG-Konverter`. Before, it completes what the AS6 editor would take
from the libraries: the type of every part of a block, the names of extensible
inputs (`IN1`, `IN2`, ...) and an empty network in an empty diagram; the block types
are completed even without the option. Without the option, without Automation
Studio 6 (or with the environment variable `BKNG_AS6_BIN` set to an empty value) the
files stay in the old format and the report says so. The
environment variable can also name the `Bin-de` folder of a particular AS6.

### Open items in the report

The report lists as open items what the converter cannot do itself:

- Ladder Diagram files that could not be converted must be opened and saved once
  in AS6 (otherwise build error 1113).
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
  Industrial Automation GmbH, Windows and .NET trademarks of Microsoft Corporation,
  Python a trademark of the Python Software Foundation. This project is not
  affiliated with, endorsed or reviewed by these companies.

### Licenses and libraries

The converter itself is licensed under the **MIT License** (see [LICENSE](LICENSE)).
The repository contains source code only; Python, Windows and Automation Studio are
not included but required on the user's computer. Their own licenses apply:

| Component | Used for | License | Included |
|---|---|---|---|
| BkNG Converter (`bkng_converter.py`, `as6_ladder/LdConvert.cs`) | the program itself | MIT | yes |
| Python 3 with its standard library | runtime | Python Software Foundation License v2 | no |
| Tcl/Tk (via `tkinter`, part of the Python installation) | user interface | Tcl/Tk license (BSD-style) | no |
| Further parts of the Python installation, e.g. Expat (XML), zlib (ZIP), OpenSSL (`hashlib`) | standard library | MIT, zlib License, Apache 2.0 | no |
| .NET Framework 4.x with the C# compiler `csc.exe` | compiles and runs the helper program of the Ladder Diagram conversion (optional) | Microsoft license (part of Windows) | no |
| Automation Studio 6: `BR.AS.Iec.GraphicalEditor.Data.dll`, `BR.AS.Iec.GraphicalEditor.CodeAnalysis.dll` and their dependencies | Ladder Diagram conversion (optional), loaded at runtime from the user's installation | proprietary, B&R Industrial Automation GmbH | no |

**Python modules used** (all from the standard library, no pip packages):
`tkinter` (`ttk`, `filedialog`, `messagebox`, `scrolledtext`), `pathlib`, `os`,
`shutil`, `tempfile`, `zipfile`, `subprocess`, `re`, `xml.etree.ElementTree`,
`threading`, `queue`, `hashlib`, `uuid`, `datetime`, `dataclasses`, `collections`,
`typing`.

**.NET namespaces used** by the helper program `LdConvert.cs`: `System`,
`System.IO`, `System.Linq`, `System.Reflection`, `System.Text`.

**Note on Automation Studio:** The Ladder Diagram conversion calls internal,
undocumented classes of Automation Studio 6. It can only be used with a validly
licensed installation; whether its use complies with the license terms of B&R must
be checked by the user. The function is an option that is switched off by default.

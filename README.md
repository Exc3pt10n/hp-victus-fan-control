# HP Victus – intelligente Lüftersteuerung

Lüftersteuerung für das **HP Victus 15-fb3xxx (BV8D4EA#ABD)** unter **Windows 11**. Sie
ersetzt die oft hektische Standardregelung durch eine ruhige, vorausschauende Lüfterkurve,
läuft unsichtbar ab dem Systemstart und aktualisiert sich selbst.

![Fenster der Lüftersteuerung](docs/fenster.png)

## Installation

1. Repository herunterladen (**Code → Download ZIP**) und entpacken.
2. **OMEN Gaming Hub** schließen bzw. dessen Lüftereinstellungen nicht parallel nutzen –
   beide Programme würden sich sonst gegenseitig überschreiben.
3. Doppelklick auf **`install\windows\install.cmd`** und die Administrator-Abfrage bestätigen.

Der Installer installiert bei Bedarf Python 3.12, legt alles unter
`C:\Program Files\VictusFan` ab, prüft die HP-BIOS-Schnittstelle und richtet ein:

- **VictusFanControl** – die Lüftersteuerung; startet mit Windows, läuft unsichtbar als
  SYSTEM und startet sich nach einem Absturz neu
- **VictusFanUpdate** – sucht 10 Minuten nach dem Start und danach alle 6 Stunden nach Updates
- den Startmenü-Eintrag **Victus Lüftersteuerung** für Fenster und Tray-Icon

Das **Tray-Icon** startet mit der Anmeldung (pro Benutzer, ohne Fenster). Abschalten lässt sich
das unter **Extras ▸ Mit Windows starten** oder in Windows unter *Einstellungen ▸ Apps ▸
Autostart*. Die Lüftersteuerung selbst läuft davon unabhängig immer ab dem Systemstart.

**Entfernen:** *Einstellungen ▸ Apps ▸ Installierte Apps ▸ Victus Lüftersteuerung ▸ Deinstallieren*
(oder `install\windows\uninstall.cmd`). Das entfernt Dienst, Tray-Icon, Autostart, Updater,
Konfiguration und Logs und gibt die Lüfter sofort an das BIOS zurück.

## Bedienung

Das **Tray-Icon** (unten rechts, ggf. hinter dem `^`) zeigt die **höchste Temperatur**
von CPU und GPU:

![Tray-Icons](docs/tray-icons.png)

Grün unter 70 °C · Orange ab 70 °C · Rot ab 85 °C · Grau = pausiert · Lüftersymbol = Dienst läuft nicht.
Beim Überfahren zeigt es CPU- und GPU-Temperatur sowie die **tatsächliche Drehzahl beider Lüfter**.
**Klick** öffnet das Fenster mittig auf dem Bildschirm, **Rechtsklick** das Menü.

Das Fenster zeigt Temperaturen und die gemessenen Lüfterdrehzahlen (der Balken zeigt die Drehzahl,
der Strich darin das Ziel der Steuerung), den Verlauf der letzten 10 Minuten und die Einstellungen.
Es folgt dem hellen oder dunklen Design von Windows. Seltener gebrauchte Funktionen
(Updates, Hardware-Test, Sensoren, Konfiguration, Log, Dienst neu starten, Autostart) liegen
unter **Extras**.
Schließen (✕) blendet das Fenster nur aus.

Zwei Einstellungen lassen sich wählen:

**Lüfterprofil** – wie stark die Lüfter auf die Temperatur reagieren (gilt bei Steuerung „Intelligent“):

| Profil | Verhalten |
|---|---|
| **Automatisch** | Ausgewogen am Netzteil, Leise im Akkubetrieb |
| **Leise** | Lüfter stehen unter 50 °C still, danach sanfter Anstieg |
| **Ausgewogen** | Mindestens 20 % (≈ 1100 U/min, leiser als die BIOS-Automatik), steigt ab 50 °C |
| **Leistung** | Mindestens 30 %, steigt früher und steiler |

**Steuerung** – wer die Lüfter steuert:

| Steuerung | Bedeutung |
|---|---|
| **Intelligent** | Regelung nach Lüfterprofil |
| **Volle Drehzahl** | Dauerhaft Maximum (≈ 5400 U/min), z. B. beim Rendern |
| **BIOS** | Steuerung pausiert, das BIOS regelt wie ab Werk |

**Leistung:** Einen Umschalter für den BIOS-Leistungsmodus (wie im OMEN Gaming Hub) gibt es
bewusst nicht. Gemessen mit 3DMark Speed Way bringen Standard, Performance und Kühl auf diesem
Gerät dieselbe Punktzahl (2 284 / 2 275 / 2 275). Was die Leistung wirklich bestimmt, ist die
**Stromversorgung**: Am HP-Netzteil darf die CPU dauerhaft 54 W aufnehmen (65 W im Boost), an
einer USB-C-Dockingstation nur 35 W.

Die Oberfläche läuft mit normalen Benutzerrechten; nur der Hintergrunddienst spricht mit
der Hardware. Beendest du das Tray-Icon, regelt der Dienst unverändert weiter.

## So arbeitet die Steuerung

- **CPU und GPU gemeinsam:** Beide Lüfter folgen dem höheren Bedarf, weil sich CPU und GPU
  die Heatpipes teilen.
- **Ruhig statt hektisch:** Steigende Temperaturen werden schnell erkannt, kurze Lastspitzen
  aber geglättet. Heruntergeregelt wird erst, wenn es 4 °C kühler ist und das 20 s anhält –
  dann sanft mit 3 %/s.
- **Sicherheit:** Ab 92 °C oder ohne Sensordaten sofort volle Drehzahl. Nach wiederholten
  Fehlern übernimmt wieder das BIOS; dessen Überhitzungsschutz bleibt immer aktiv.
- **Standby:** Im Standby und bei ausgeschaltetem Bildschirm regelt die Firmware die Lüfter
  selbst (sie ignoriert dann Drehzahlbefehle). Beim Aufwachen setzt die Steuerung Leistungsmodus
  und Drehzahl sofort neu.
- **Anlaufhilfe:** Ein stehender Lüfter läuft bei niedriger Drehzahl nicht von selbst an. Er
  bekommt dann kurz ≈ 3000 U/min, bis er dreht, und läuft danach mit der Zieldrehzahl.
- **Selbstkontrolle:** Die gemessene Drehzahl wird laufend mit der Vorgabe verglichen. Weicht
  sie ab, setzt die Steuerung Leistungsmodus und Drehzahl neu – jede Minute, bis es wieder
  passt. Nach drei erfolglosen Versuchen erscheint zusätzlich ein Hinweis im Fenster.

Gemessene Eigenschaften des Victus BV8D4EA (Ryzen 5 240, RTX 5050):

| | |
|---|---|
| Lüfter | 1100 bis 5400 / 5200 U/min stufenlos, Stufe 0 = Stillstand |
| Drehzahlvorgabe | wirkt erst, nachdem ein BIOS-Leistungsmodus gesetzt wurde – die Steuerung setzt ihn beim Start und etwa alle 50 s |
| Anlauf | ein stehender Lüfter startet bei ≈ 1100 U/min nicht von selbst – daher die Anlaufhilfe |
| CPU-Temperatur | der HP-BIOS-Sensor liefert nichts; die ACPI-Zone `THRM` folgt der CPU (Tctl) mit etwa 3 °C Abstand |
| GPU-Temperatur | über `nvidia-smi` |

## Updates

Neue Versionen werden automatisch installiert, deine Einstellungen bleiben erhalten. Das
Tray-Icon startet sich danach selbst neu. Version und Update-Status stehen unten im Fenster;
dort und im Tray-Menü gibt es **Nach Updates suchen**.

Installiert werden nur **freigegebene Versionen**: die Tags `vX.Y.Z` bzw. GitHub-Releases von
[`Exc3pt10n/hp-victus-fan-control`](https://github.com/Exc3pt10n/hp-victus-fan-control), die der
Workflow nach einem Merge in `main` anlegt. Commits ohne neue Version werden nicht verteilt,
und auf eine ältere Version wird nie zurückgestuft. Unter `[update]` in der Konfiguration lässt
sich das abschalten, auf „nur anzeigen“ stellen oder – zum Testen – einem Branch folgen.

> **Wichtig:** Updates laufen mit SYSTEM-Rechten und installieren, was als Version getaggt wird.
> Halte den Schreibzugriff auf das Repository daher auf dich beschränkt.

## Tests bei Problemen

Beide Tests findest du im Tray-Menü; sie fragen nach Administratorrechten und übernehmen ihr
Ergebnis automatisch in die Konfiguration. Bei Problemen bitte die Zeile **„Rohdaten“** am
Ende der Ausgabe schicken.

- **Hardware-Test** (≈ 1,5 Minuten): misst, ob Max-Lüfter und feste Drehzahlen wirklich
  umgesetzt werden, in welchem BIOS-Leistungsmodus, wie hoch die Maximaldrehzahl ist und
  wie weit herunter die Lüfter laufen. Setzt das BIOS feste Drehzahlen gar nicht um
  (etwa nach einem BIOS-Update), stellt er auf **Max an/aus** um: BIOS-Automatik bei
  normaler Last, volle Drehzahl bei hohem Bedarf.
- **Temperatursensoren testen** (≈ 30 Sekunden): listet alle Temperaturquellen auf, erzeugt
  20 Sekunden volle CPU-Last und wählt die Quelle, die am schnellsten reagiert.

| Datei in `C:\ProgramData\VictusFan` | Inhalt |
|---|---|
| `config.toml` | Konfiguration – Änderungen gelten nach dem Speichern sofort ([Beispiel](config.example.toml)) |
| `victus-fan.log` | Log der Lüftersteuerung |
| `update.log`, `update-install.log` | Log der Updates |

Die Logs halten sich selbst klein: `victus-fan.log` höchstens ≈ 4 MB (1 MB + 3 ältere Dateien),
`update.log` höchstens ≈ 1,5 MB, `update-install.log` wird bei jedem Update überschrieben.

## Kommandozeile

Alles aus dem Fenster geht auch in der Konsole (`victus-fan --help`):

```bash
victus-fan status
```

| Befehl | Wirkung |
|---|---|
| `victus-fan profile quiet\|balanced\|performance\|auto` | Profil wählen |
| `victus-fan mode normal\|max\|paused` | Steuerung wählen (Intelligent, Volle Drehzahl, BIOS) |
| `victus-fan curve balanced` | Lüfterkurve eines Profils anzeigen |
| `victus-fan selftest --apply` | Hardware-Test (Administrator) |
| `victus-fan sensors --apply` | Temperaturquellen vergleichen (Administrator) |
| `victus-fan update --check` | Nach Updates suchen |
| `victus-fan diagnose` | Hardware-Zugriff prüfen (Administrator) |
| `victus-fan restore` | Lüfter an das BIOS zurückgeben (Administrator) |

## Entwicklung

```bash
python -m unittest
```

```bash
python -m victus_fan -c config.example.toml run --backend simulate -v
```

**Versionen:** Die Version steht in `pyproject.toml` und `victus_fan/__init__.py` (ein Test
prüft, dass beide gleich sind). Nach jedem Merge nach `main` laufen die Tests; ist die Version
neu, legt der Workflow [`release.yml`](.github/workflows/release.yml) den Tag `v<Version>` und ein
GitHub-Release mit automatischen Änderungsnotizen an. Ohne Versionssprung wird nichts getaggt.

| Modul | Aufgabe |
|---|---|
| `controller.py` | Regelkern (Kurven, Glättung, Hysterese, Rampen) – ohne Hardware testbar |
| `service.py` | Dienstschleife, Steuerung, Selbstkontrolle |
| `backends/windows.py` | HP-BIOS über WMI (`hpqBIntM`); Befehlscodes aus der Community-Dokumentation (z. B. OmenMon) |
| `gui.py` | Tray-Icon und Fenster |
| `selftest.py`, `sensors.py` | Hardware-Test und Sensor-Test |
| `updater.py` | Auto-Update über GitHub |
| `state.py` | Austausch zwischen Dienst und Oberfläche (`status.json`, `override.json`) |

Lizenz: MIT

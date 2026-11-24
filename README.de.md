[English](README.md) | [Deutsch](README.de.md)

# Raspberry-Pi-HMI für eine UR30-Entpuderungszelle

Touchscreen-HMI-Software für eine cobotgestützte Entpuderungszelle mit
Raspberry Pi. Die Anwendung bietet rollenbasierte Bedienoberflächen,
Programm-Presets und Warteschlangen, Kommunikation über den UR Dashboard
Server, GPIO-Ausgänge, eine Roboterstatusanzeige sowie ein optionales Medienfeld
und einen Demo-Bildschirmschoner.

> **Sicherheit:** Diese HMI ist kein sicherheitsgerichtetes Steuerungssystem
> und ersetzt weder das Roboter-Handbediengerät noch Not-Halt-Einrichtungen,
> Schutzeinrichtungen oder eine dokumentierte Risikobeurteilung. Roboterprogramme
> und Sicherheitskonfiguration müssen an der tatsächlichen Anlage geprüft
> werden. Betrieb nur mit geeigneten Schutzmaßnahmen und Aufsicht.

## Funktionen

- Touchfreundliche CustomTkinter-Oberfläche für Raspberry Pi.
- Rollen für User, Demo, Super User und Developer Mode.
- Roboterprogramme, Presets, positionsgeprüfte Warteschlange und Wiederholungen.
- Befehle über den UR Dashboard Server und regelmäßige Anzeige des
  Sicherheitsstatus.
- Raspberry-Pi-GPIO-Ausgänge mit simuliertem Ersatzbetrieb, wenn keine
  GPIO-Hardware verfügbar ist.
- Medienfeld für Bilder/Videos und optionaler Bildschirmschoner im Demo-Modus.
- Geräteeinstellungen für Rollen-PINs und Roboter-IP.

Die Anwendung sendet keine Nutzlastdaten an den Roboter. Die Anzeige des
Sicherheitsstatus dient nur der Information und ist keine Sicherheitsfunktion.

## Voraussetzungen

- Raspberry Pi OS (der mitgelieferte Installer ist auf Bookworm ausgelegt).
- Python 3, Tkinter und die durch `install.sh` installierten Abhängigkeiten.
- Ein vom Raspberry Pi aus erreichbarer Universal-Robots-Controller mit
  verfügbarem Dashboard Server. Die in `programs.py` referenzierten
  Roboterprogramme müssen auf dem Controller installiert sein.
- Optional: unterstützte GPIO-Hardware für physische Ausgänge.
- Optional: zur Weitergabe freigegebene Mediendateien im Ordner `media/`.

## Installation und Start

Repository auf den Raspberry Pi klonen oder kopieren und anschließend ausführen:

```bash
bash install.sh
bash start.sh
```

`start.sh` kann fehlende Abhängigkeiten beim ersten Start installieren. Der
Installer nutzt nach Möglichkeit Raspberry-Pi-OS-Pakete und benötigt
Internetzugang sowie `sudo`.

Optional: Desktop-Verknüpfung und Autostart einrichten:

```bash
bash create_desktop_launcher.sh
bash start.sh --autostart
# Autostart deaktivieren:
bash start.sh --no-autostart
```

## Vor der Verbindung mit dem Roboter konfigurieren

1. Die Beispiel-PINs in `settings.example.json` durch individuelle PINs mit
   4–8 Ziffern ersetzen und `robot_ip` auf die Adresse des Controllers setzen.
2. Die konfigurierte Datei neben der Anwendung als `settings.json` speichern.
   Diese lokale Datei wird von Git ignoriert und darf nicht veröffentlicht
   werden.
3. Ohne `settings.json` vor der Bereitstellung die Standardwerte in
   `programs.py` und `robotControl.py` ändern. In einem öffentlichen Repository
   sichtbare Standardwerte sind keine geeigneten Produktions-Zugangsdaten.
4. Dashboard-Server-Verbindung und alle referenzierten Roboterprogramme vor dem
   Betrieb prüfen. Die Controller-Pfade in `programs.py` an die eigene
   Roboterinstallation anpassen.

`settings.example.json` enthält Platzhalter und keine verwendbaren Zugangsdaten.
Vor dem Start alle `CHANGE_ME`-Werte ersetzen. PINs und Roboter-IP lassen sich
auch in der Anwendung über Developer Mode ändern; gespeichert werden sie in
der lokalen Datei `settings.json`. Details stehen in `settings.py`.

## Demo-Verhalten

Die Demo-Rolle führt das `DEMO`-Preset 200-mal mit zwei Sekunden Pause zwischen
den Roboterprogramm-Snippets aus. Während eines aktiven Demo-Laufs gilt:

- Fünf Taps auf das Cobot-Control-Zeichen ganz oben rechts innerhalb von
  1,5 Sekunden schalten CANCEL frei.
- Drei Taps auf den Ansichts-/Standbildbereich innerhalb von 1,5 Sekunden
  schalten PAUSE frei.
- CANCEL wartet das aktuelle Snippet ab, schaltet noch ausstehende Demo-Ausgänge
  aus und startet kein weiteres Snippet.
- CONTINUE sperrt PAUSE wieder. Ein neuer Demo-Lauf beginnt von vorne.

Außerhalb des Demo-Modus pausiert CANCEL das aktive Programm und löscht die
Warteschlange; CONTINUE lässt nur das aktive Programm zu Ende laufen.

Der Bildschirmschoner verwendet, sofern vorhanden,
`media/screensaver.mp4`. Details zu Medien und Wiedergabe stehen in
[`media/README.md`](media/README.md). Nur Medien hinzufügen, für die eine
Weitergabeberechtigung besteht.

## Verzeichnisübersicht

| Pfad | Zweck |
|---|---|
| `main.py` | Hauptanwendung und UI-Layout |
| `gui_core.py` | Anwendungslogik und Roboterlauf-Steuerung |
| `programs.py` | Roboterprogramm-Referenzen, Presets, Rollen und PIN-Standardwerte |
| `robotControl.py` | Kommunikation mit dem UR Dashboard Server |
| `io_wrapper.py` | Abstraktion der GPIO-Ausgänge |
| `settings.py` | Speichern und Prüfen lokaler Einstellungen |
| `cobot_theme.py` | Neutrales Theme und Cobot-Control-Kopfzeile |
| `media_panel.py` | Medienfeld und Bildschirmschoner |
| `media/` | Standbild und Mediendokumentation |
| `settings.example.json` | Vorlage für lokale Einstellungen |

Hilfsskripte für Installation, Netzwerkeinrichtung, Desktop-Start und
GPIO-Diagnose sind ebenfalls enthalten.

## Hinweise zur Veröffentlichung

- `settings.json`, Laufzeitprotokolle, generierter Python-Bytecode,
  Demo-Laufzähler und vom Betreiber bereitgestellte Videodateien werden durch
  `.gitignore` ausgeschlossen.
- Das Repository enthält derzeit keine Lizenz. Aus der öffentlichen
  Verfügbarkeit folgt keine Erlaubnis zur Wiederverwendung des Codes oder
  Drittmedien.
- Die Standard-PINs in `programs.py` sind im Quellcode sichtbar. Vor der
  Verbindung mit einem Roboter ändern und nicht als sichere Zugangsdaten
  verwenden.
- Die Controller-Programmpfade in `programs.py` sind Beispiele und müssen an
  die Roboterinstallation angepasst werden.
- Dieses Projekt ist eine HMI und keine zertifizierte Sicherheitskomponente.
  Roboterverhalten, GPIO-Verdrahtung, Bedienerzugang und Zellabsicherung liegen
  weiterhin in der Verantwortung des Betreibers.

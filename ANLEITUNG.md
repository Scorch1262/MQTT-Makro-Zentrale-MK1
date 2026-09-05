# MQTT Makro-Zentrale — Anleitung

## 1. Was ist das?

Ein lokal laufendes Web-Tool (`app.py`), das MQTT-Makros zentral
verwaltet und ausfuehrt. Nach dem Start ist es unter
`http://<IP-des-PCs>:8010` im gesamten LAN erreichbar (nicht nur auf
dem PC selbst, auf dem es laeuft).

Alle Broker und Makros liegen in `config.json` im selben Ordner wie
`app.py` bzw. wie die spaeter per PyInstaller erzeugte `.exe` /
`.app`.

Aktuelle Version: **1.0.0** (wird auch oben rechts auf der Webseite
angezeigt, Quelle: `APP_VERSION` in `app.py`).

## 2. Benoetigte Python-Pakete

| Paket | Zweck |
|---|---|
| `Flask` | Web-Oberflaeche / REST-API |
| `paho-mqtt` | MQTT-Verbindungen (Makro-Schritte + Trigger) |
| `pyinstaller` | Nur zum Bauen der .exe / .app benoetigt, nicht zur Laufzeit |

Alle Versionen stehen in `requirements.txt`. Installation (lokal,
zum Testen/Entwickeln):

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
python app.py
```

Danach ist die Seite unter `http://localhost:8010` erreichbar, bzw.
im LAN unter der IP des PCs (z. B. `http://192.168.1.20:8010`) —
den Port bei Bedarf in `config.json` unter `server.port` aendern.

## 3. PyInstaller-Befehl (lokal, manuell)

Windows (eine einzelne .exe, keine Konsole):

```bash
pyinstaller --onefile --noconsole --name MqttMakroZentrale --collect-all paho app.py
```

macOS (.app-Bundle):

```bash
pyinstaller --windowed --name MqttMakroZentrale --collect-all paho app.py
```

`--collect-all paho` sorgt dafuer, dass PyInstaller alle internen
Teile von `paho-mqtt` findet (wird sonst gelegentlich nicht
automatisch erkannt). Das Ergebnis liegt danach in `dist/`.

Wichtig: Die `config.json` muss nach dem Bauen manuell in denselben
Ordner wie die `.exe` bzw. neben das `.app`-Bundle gelegt werden
(bzw. wird beim ersten Start automatisch mit Standardwerten neu
angelegt, falls sie fehlt).

## 4. Automatischer Build per GitHub Actions

Die Datei `.github/workflows/build.yml` baut bei jedem Push nach
`main`/`master`, bei jedem Tag `vX.Y.Z` und auch manuell (Reiter
"Actions" → "Run workflow") automatisch:

- `MqttMakroZentrale.exe` (Windows, Einzeldatei)
- `MqttMakroZentrale-macos.zip` (enthaelt `MqttMakroZentrale.app`)

**Einrichtung:**

1. Repository auf GitHub anlegen (falls noch nicht geschehen).
2. Diese Dateien ins Repository legen (Struktur siehe unten).
3. Push nach `main` — der Workflow startet automatisch; die fertigen
   Dateien stehen danach im jeweiligen Workflow-Lauf unter
   "Artifacts" bereit.
4. Fuer ein "echtes" Release mit Download-Link: einen Tag setzen,
   z. B.:
   ```bash
   git tag v1.0.0
   git push origin v1.0.0
   ```
   Dadurch wird zusaetzlich automatisch ein GitHub-Release mit beiden
   Dateien angehaengt erstellt.

**Erwartete Ordnerstruktur im Repository:**

```
.
├── app.py
├── config.json
├── requirements.txt
├── ANLEITUNG.md
└── .github/
    └── workflows/
        └── build.yml
```

Kein GitHub-Secret noetig — der Workflow nutzt ausschliesslich
Standard-Actions (`checkout`, `setup-python`, `upload-artifact`,
`softprops/action-gh-release`, die alle mit dem automatisch
bereitgestellten `GITHUB_TOKEN` funktionieren).

## 5. Versionierung & Commit-Texte

Bei jeder ausgelieferten Aenderung:

1. `APP_VERSION` oben in `app.py` erhoehen
   (PATCH = Bugfix, MINOR = neue Funktion, MAJOR = Breaking Change
   am Config-Format).
2. Commit-Text wie unten vorgeschlagen verwenden (wird bei jeder
   Aenderung an dieser Stelle mitgeliefert).

### Aktueller Commit-Text (fuer diese erste Version)

```
v1.0.0: Erste Version der MQTT Makro-Zentrale

- Web-Oberflaeche im Lattice-Dashboard-Design (dunkles Theme)
- Broker-Verwaltung (Name, Host/IP, Port, optional Zugangsdaten, TLS)
- Makros anlegen/bearbeiten/loeschen, uebereinander als Karten dargestellt
- Beliebig viele Schritte pro Makro: Broker, Topic, Payload,
  Wiederholungen, Wartezeit danach (Sekunden oder Minuten)
- Optionaler MQTT-Trigger pro Makro (Auto-Start bei eingehender Nachricht)
- Live-Anzeige des laufenden Schritts inkl. aktueller Wiederholung
  bzw. Wartephase, manuelles Starten/Stoppen
- Konfiguration in menschenlesbarer config.json neben der exe/app
- GitHub-Actions-Workflow fuer automatischen Windows-exe- und macOS-app-Build
```

## 6. Kurzer Funktionsueberblick der Oberflaeche

- **"Broker verwalten"**: MQTT-Broker anlegen/loeschen (Name, Host,
  Port, optional Benutzername/Passwort, optional TLS). Ein Broker
  kann erst geloescht werden, wenn ihn kein Makro mehr verwendet.
- **"+ Neues Makro"**: Name vergeben, optional einen Auto-Start-
  Trigger (Broker + Topic) aktivieren, beliebig viele Schritte
  hinzufuegen. Jeder Schritt hat einen eigenen Broker, ein Topic,
  eine Payload, eine Anzahl Wiederholungen sowie eine Wartezeit
  (Sekunden/Minuten), die danach vor dem naechsten Schritt abgewartet
  wird.
- Makro-Karten lassen sich per Klick auf den Titel aufklappen — dort
  wird bei laufendem Makro der aktuelle Schritt farblich markiert und
  angezeigt, ob gerade gesendet oder gewartet wird.
- "Ausfuehren" startet ein Makro manuell; ein laufendes Makro kann
  weder ein zweites Mal gestartet, bearbeitet noch geloescht werden
  (nur gestoppt).

# Changelog

Alle nennenswerten Aenderungen an der MQTT Makro-Zentrale, neueste
zuerst. Versionsnummer ist `APP_VERSION` in `app.py` (wird auch oben
rechts auf der Webseite angezeigt).

## v1.1.1

- Dokumentation aufgeraeumt: Versionshistorie aus der README in diese
  eigene `CHANGELOG.md` ausgelagert, README auf eine kompakte
  Kurzuebersicht mit Links reduziert.
- `docs/ANLEITUNG-GLiNet-Brume2.md` konsolidiert: einheitlicher Pfad
  `/root/mqtt-makro-zentrale` durchgaengig, Abschnitt zur alten
  Installation entfernt ausschliesslich die vorherige Installation
  dieses Programms (kein Anfassen anderer Ordner/Dienste auf dem
  Router), Fehlerbehebungsabschnitt fuer die haeufigsten `scp`-Stolper-
  steine (fehlender Zielordner, Dropbear ohne `sftp-server`).

## v1.1.0

Trigger-Payload fuer Makro-Ausloeser:

- Trigger eines Makros kann jetzt zusaetzlich zu Broker + Topic auch
  eine erwartete Payload festlegen (neues Feld "Trigger-Payload" im
  Makro-Dialog, optional).
- Leeres Trigger-Payload-Feld = Makro startet weiterhin bei jeder
  Nachricht auf dem Topic (bisheriges Verhalten).
- Ist ein Wert eingetragen, startet das Makro nur noch, wenn die
  ankommende MQTT-Payload exakt damit uebereinstimmt.
- Trigger-Badge auf der Makro-Karte zeigt die hinterlegte Payload
  (falls gesetzt) mit an.
- `config.json`: `trigger`-Objekt jedes Makros hat ein neues Feld
  `payload` (Standard: leerer String; bestehende Konfigurationen
  werden beim naechsten Start automatisch ergaenzt).

## v1.0.1

Programm laeuft immer mit sichtbarem Terminalfenster:

- Windows-Build ohne `--noconsole`: EXE zeigt beim Start ein
  Konsolenfenster (Beenden per Fenster schliessen oder Strg+C).
- macOS-Build ohne `--windowed`: erzeugt eine Konsolen-Binary statt
  eines unsichtbaren `.app`-Bundles.
- Neues Doppelklick-Startskript `Start-MqttMakroZentrale.command`
  fuer macOS, oeffnet automatisch ein Terminal-Fenster.
- `ANLEITUNG.md` durch `README.md` ersetzt (inkl. Hinweis, wie man
  das Programm unter Windows/macOS wieder beendet).

## v1.0.0

Erste Version der MQTT Makro-Zentrale:

- Web-Oberflaeche im Lattice-Dashboard-Design (dunkles Theme).
- Broker-Verwaltung (Name, Host/IP, Port, optional Zugangsdaten,
  TLS).
- Makros anlegen/bearbeiten/loeschen, uebereinander als Karten
  dargestellt.
- Beliebig viele Schritte pro Makro: Broker, Topic, Payload,
  Wiederholungen, Wartezeit danach (Sekunden oder Minuten).
- Optionaler MQTT-Trigger pro Makro (Auto-Start bei eingehender
  Nachricht).
- Live-Anzeige des laufenden Schritts inkl. aktueller Wiederholung
  bzw. Wartephase, manuelles Starten/Stoppen.
- Konfiguration in menschenlesbarer `config.json` neben der exe/app.
- GitHub-Actions-Workflow fuer automatischen Windows-exe- und
  macOS-app-Build.

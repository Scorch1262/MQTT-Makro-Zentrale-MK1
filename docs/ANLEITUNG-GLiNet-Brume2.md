# Installation auf dem GL.iNet Brume 2

Diese Anleitung beschreibt, wie die MQTT Makro-Zentrale direkt auf
einem **GL.iNet Brume 2** (GL-MT2500 / GL-MT2500A) installiert und
dauerhaft (inkl. Autostart nach Neustart) betrieben wird.

## Warum das hier anders ist als bei Windows/macOS

Der Brume 2 ist kein PC, sondern ein kleiner Router mit **OpenWrt
21.02** auf einem 64-Bit-ARM-Prozessor (MediaTek MT7981B, Architektur
`aarch64_cortex-a53`), 1 GB RAM und 8 GB eMMC-Speicher. Es gibt
weder Windows noch macOS, also auch **kein PyInstaller-Build noetig**
— stattdessen laeuft `app.py` direkt mit dem auf dem Router
installierten Python-Interpreter. Es gibt auch kein "Terminal-
fenster" wie am PC; Beenden/Neustarten laeuft ueber einen normalen
OpenWrt-Dienst (siehe Abschnitt 6).

Wichtig: Der Brume 2 hat **kein WLAN** — er wird ausschliesslich per
Ethernet (LAN/WAN) angesteuert.

## Voraussetzungen

- Brume 2 ist eingerichtet und per Ethernet-Kabel erreichbar (Standard-
  Adresse `192.168.8.1`, ggf. deine abweichende LAN-IP verwenden).
- Der Router hat eine funktionierende Internetverbindung (WAN), da
  fuer die Installation Pakete aus dem Internet geladen werden.
- Ein SSH-Client (unter Windows z. B. PowerShell, unter macOS/Linux
  das eingebaute Terminal).
- Das Projekt-Verzeichnis (`app.py`, `config.json`, ...) liegt lokal
  auf deinem PC bereit, z. B. aus der zuvor heruntergeladenen ZIP-Datei
  entpackt.

## 1. Per SSH auf den Router verbinden

```bash
ssh root@192.168.8.1
```

Passwort = das Passwort des GL.iNet-Adminpanels. SSH ist bei GL.iNet-
Routern ab Werk aus dem LAN heraus erreichbar, es muss nichts extra
aktiviert werden.

## 2. Paketquellen aktualisieren und Python3 installieren

```bash
opkg update
opkg install python3 python3-pip
```

Das laedt Python 3.9 sowie `pip3` (Paketname ist `python3-pip`, der
Befehl danach heisst aber `pip3`, nicht `pip`). Pruefen:

```bash
python3 --version
pip3 --version
```

## 3. Flask und paho-mqtt installieren

Beides sind reine Python-Pakete ohne Compiler-Abhaengigkeiten, daher
funktioniert die Installation direkt per `pip3` ohne zusaetzliche
Build-Werkzeuge:

```bash
pip3 install flask paho-mqtt
```

## 4. Speicherplatz pruefen

Der Brume 2 hat 8 GB eMMC — mehr als genug fuer dieses Projekt. Kurz
gegenchecken:

```bash
df -h /
```

Ein paar hundert MB frei reichen locker aus.

## 5. Projektdateien auf den Router kopieren

Zuerst auf dem Router den Zielordner direkt im Root-Verzeichnis
anlegen (per SSH-Sitzung aus Schritt 1):

```bash
mkdir -p /mqtt-makro-zentrale
```

Danach auf deinem PC (nicht auf dem Router!), im Ordner mit `app.py`
und `config.json`:

```bash
scp -O -r app.py config.json root@192.168.8.1:/mqtt-makro-zentrale/
```

(IP-Adresse ggf. an deine tatsaechliche Router-IP anpassen.)

> **Hinweis fuer macOS/neuere OpenSSH-Versionen:** Der Brume 2 nutzt
> Dropbear als SSH-Server — das bringt keinen `sftp-server` mit.
> Neuere `scp`-Clients (macOS Sonoma/Sequoia, aktuelle Linux-
> Distributionen) verwenden aber standardmaessig das SFTP-Protokoll
> und scheitern dadurch mit einer Fehlermeldung wie
> `ash: /usr/libexec/sftp-server: not found`. Das oben gezeigte
> `-O` erzwingt das aeltere, klassische SCP-Protokoll, das ohne
> `sftp-server` auskommt und mit Dropbear funktioniert. Kennt dein
> `scp` das `-O`-Flag nicht (sehr alte OpenSSH-Version), einfach ohne
> `-O` versuchen.
>
> Falls stattdessen `Permission denied` erscheint: Das ist meist ein
> Tippfehler beim (nicht sichtbar angezeigten) Passwort — es ist das
> Passwort des GL.iNet-Adminpanels, kein separates SSH-Passwort.

## 6. Testlauf (manuell, im Vordergrund)

Auf dem Router:

```bash
cd /mqtt-makro-zentrale
python3 app.py
```

Die Konsole zeigt jetzt die gewohnte Log-Zeile
`MQTT-Makro-Zentrale laeuft auf http://0.0.0.0:8010 ...`. Von einem
beliebigen Geraet im selben LAN aus im Browser aufrufen:

```
http://192.168.8.1:8010
```

(bzw. die tatsaechliche LAN-IP des Routers, falls abweichend).
Zum Beenden dieses Testlaufs auf dem Router einfach **Strg+C**
druecken — das ist hier das Aequivalent zum "Terminalfenster
schliessen" am PC.

## 7. Als dauerhaften Dienst einrichten (Autostart nach Neustart)

Damit die Makro-Zentrale automatisch mit dem Router startet und im
Hintergrund weiterlaeuft (auch nach einem Reboot), ein init-Skript
anlegen:

```bash
cat > /etc/init.d/mqttmakro << 'EOF'
#!/bin/sh /etc/rc.common
# MQTT Makro-Zentrale - OpenWrt-Dienst

START=99
STOP=10
USE_PROCD=1

PROG=/usr/bin/python3
APP_DIR=/mqtt-makro-zentrale

start_service() {
    procd_open_instance
    procd_set_param command "$PROG" "$APP_DIR/app.py"
    procd_set_param directory "$APP_DIR"
    procd_set_param respawn           # startet automatisch neu, falls es abstuerzt
    procd_set_param stdout 1          # Logs landen in "logread"
    procd_set_param stderr 1
    procd_close_instance
}
EOF
chmod +x /etc/init.d/mqttmakro
```

Dienst aktivieren (startet kuenftig automatisch beim Booten) und
sofort starten:

```bash
/etc/init.d/mqttmakro enable
/etc/init.d/mqttmakro start
```

## 8. Dienst bedienen (Start/Stopp/Neustart/Logs)

Das ist hier das Aequivalent zum "Terminalfenster schliessen" bzw.
"Strg+C" am PC:

```bash
/etc/init.d/mqttmakro stop        # beenden
/etc/init.d/mqttmakro start       # starten
/etc/init.d/mqttmakro restart     # neu starten (z. B. nach Aenderung an app.py)
logread -f                        # Logs live mitlesen (Strg+C zum Beenden der Ansicht)
```

## 9. Firewall (normalerweise nicht noetig, aber zur Sicherheit)

OpenWrt erlaubt standardmaessig allen Geraeten aus dem LAN Zugriff
auf Dienste des Routers, daher sollte `http://192.168.8.1:8010` ohne
weiteres Zutun funktionieren. Falls nicht, folgende Firewall-Regel
ergaenzen:

```bash
uci add firewall rule
uci set firewall.@rule[-1].name='Allow-MQTT-Makro-Web'
uci set firewall.@rule[-1].src='lan'
uci set firewall.@rule[-1].proto='tcp'
uci set firewall.@rule[-1].dest_port='8010'
uci set firewall.@rule[-1].target='ACCEPT'
uci commit firewall
/etc/init.d/firewall restart
```

## 10. Wichtige Besonderheiten dieser Plattform

- **Config-Datei-Ort:** `config.json` liegt (wie gewuenscht) im
  selben Ordner wie `app.py`, hier also
  `/mqtt-makro-zentrale/config.json`.
- **Aenderungen an `app.py`:** Datei per `scp` erneut hochladen, dann
  `/etc/init.d/mqttmakro restart`.
- **Firmware-Update des Routers:** Ein Firmware-Upgrade (`sysupgrade`)
  kann manuell installierte Pakete/Dateien ausserhalb der Standard-
  konfiguration entfernen. Vor einem Firmware-Update daher sicherheits-
  halber `config.json` per `scp` auf den PC sichern und die
  Installationsschritte danach einmal wiederholen.
- **MQTT-Broker im selben LAN:** Solange die Bambu-Lab-Drucker bzw.
  sonstigen MQTT-Broker im selben Netz wie der Brume 2 haengen,
  funktioniert die Verbindung genauso wie am PC — es muss lediglich
  die jeweilige Broker-IP beim Anlegen des Brokers in der Weboberflaeche
  eingetragen werden.
- **Geringer Stromverbrauch:** Der Brume 2 zieht im Betrieb nur ca.
  2 W, eignet sich also gut, um die Makro-Zentrale dauerhaft laufen
  zu lassen (kein separater PC noetig).

"""
MQTT Makro-Zentrale
====================
Ein lokal laufendes Web-Tool, um MQTT-Makros (Abfolgen von MQTT-
Nachrichten mit Wartezeiten und Wiederholungen) zentral anzulegen,
zu verwalten und auszuloesen.

Ein Makro kann:
- manuell per Knopfdruck auf der Webseite gestartet werden, und/oder
- automatisch gestartet werden, sobald auf einem bestimmten MQTT-
  Broker eine Nachricht auf einem bestimmten Topic ankommt (Trigger).

Jeder Schritt eines Makros:
- veroeffentlicht eine Payload auf einem Topic bei einem frei waehl-
  baren MQTT-Broker,
- kann mehrfach wiederholt werden, bevor es weitergeht,
- wird danach von einer frei einstellbaren Wartezeit (Sekunden bis
  Minuten) gefolgt, bevor der naechste Schritt beginnt.

Start (Entwicklung):   python app.py
Erreichbar unter:      http://<IP-DES-PCS>:8010  (im gesamten LAN)
Konfiguration:         config.json (liegt im selben Ordner wie das
                        Skript bzw. wie die von PyInstaller erzeugte
                        .exe / .app)
"""

# Versionsnummer (semantische Versionierung: MAJOR.MINOR.PATCH).
# Einzige Quelle der Wahrheit fuer die Version - wird vom GitHub-
# Actions-Workflow per Regex ausgelesen, um Release-Tag und Datei-
# Namen zu erzeugen, und wird zusaetzlich auf der Webseite angezeigt.
# Bei jeder ausgelieferten Aenderung hier erhoehen:
#   PATCH (x.x.+1) -> Bugfix, keine neuen Funktionen
#   MINOR (x.+1.0) -> neue Funktion, abwaertskompatibel
#   MAJOR (+1.0.0) -> Breaking Change (z. B. Config-Format aendert sich)
APP_VERSION = "1.0.0"

import os
import sys
import json
import ssl
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, jsonify, request, render_template_string

import paho.mqtt.client as mqtt


# ----------------------------------------------------------------------
# Pfade: Config liegt neben der EXE/APP (bzw. neben app.py im Dev-Betrieb)
# ----------------------------------------------------------------------
def base_dir() -> str:
    if getattr(sys, "frozen", False):          # laeuft als PyInstaller-EXE
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(base_dir(), "config.json")
LOCK = threading.RLock()

DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8010
    },
    "brokers": [],
    "macros": []
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("server", json.loads(json.dumps(DEFAULT_CONFIG["server"])))
    cfg.setdefault("brokers", [])
    cfg.setdefault("macros", [])
    for b in cfg["brokers"]:
        b.setdefault("port", 1883)
        b.setdefault("username", "")
        b.setdefault("password", "")
        b.setdefault("tls", False)
    for m in cfg["macros"]:
        m.setdefault("trigger", {"enabled": False, "broker_id": "", "topic": ""})
        m.setdefault("steps", [])
        for s in m["steps"]:
            s.setdefault("payload", "")
            s.setdefault("repeat_count", 1)
            s.setdefault("repeat_delay_sec", 0.5)
            s.setdefault("wait_after_sec", 0)
    return cfg


def save_config(cfg: dict) -> None:
    with LOCK:
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        os.replace(tmp_path, CONFIG_PATH)


# ----------------------------------------------------------------------
# MQTT: einmaliges Veroeffentlichen einer Nachricht (fuer Makro-Schritte)
# ----------------------------------------------------------------------
def mqtt_publish_once(broker: dict, topic: str, payload: str, timeout: float = 5.0) -> None:
    """Baut eine kurzlebige Verbindung zu genau diesem Broker auf,
    veroeffentlicht eine einzelne Nachricht und trennt die Verbindung
    wieder. Bewusst eine eigene Verbindung pro Veroeffentlichung (statt
    einer dauerhaft offenen, geteilten Verbindung), damit gleichzeitig
    laufende Makros sich nicht gegenseitig blockieren koennen, selbst
    wenn sie denselben Broker verwenden."""
    done = threading.Event()
    result = {"error": None}
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"makro-{uuid.uuid4().hex[:8]}", protocol=mqtt.MQTTv311
    )

    if broker.get("username"):
        client.username_pw_set(broker.get("username"), broker.get("password") or None)
    if broker.get("tls"):
        ctx = ssl._create_unverified_context()
        client.tls_set_context(ctx)
        client.tls_insecure_set(True)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code == 0:
            c.publish(topic, payload, qos=0)
        else:
            result["error"] = f"Verbindung zum Broker fehlgeschlagen ({reason_code})"
            done.set()

    def on_publish(c, userdata, mid, reason_code, properties):
        done.set()

    client.on_connect = on_connect
    client.on_publish = on_publish

    try:
        client.connect(broker["host"], int(broker.get("port", 1883)), keepalive=10)
    except Exception as e:
        raise RuntimeError(f"Verbindung zu {broker['host']}:{broker.get('port', 1883)} fehlgeschlagen: {e}")

    client.loop_start()
    finished_in_time = done.wait(timeout)
    client.loop_stop()
    try:
        client.disconnect()
    except Exception:
        pass

    if result["error"]:
        raise RuntimeError(result["error"])
    if not finished_in_time:
        raise RuntimeError("Zeitueberschreitung beim Senden der Nachricht.")


# ----------------------------------------------------------------------
# MQTT: dauerhafte "Lauscher"-Verbindungen fuer Makro-Trigger
# ----------------------------------------------------------------------
class TriggerManager:
    """Haelt pro Broker, der von mindestens einem aktiven Trigger
    verwendet wird, eine dauerhafte MQTT-Verbindung offen und startet
    das passende Makro, sobald eine Nachricht auf dem hinterlegten
    Topic ankommt."""

    def __init__(self):
        self._clients = {}      # broker_id -> mqtt.Client
        self._topic_map = {}    # broker_id -> {topic: [macro_id, ...]}
        self._lock = threading.RLock()

    def rebuild(self, cfg: dict) -> None:
        with self._lock:
            needed = {}  # broker_id -> {topic: [macro_id, ...]}
            for m in cfg["macros"]:
                tr = m.get("trigger", {})
                if tr.get("enabled") and tr.get("broker_id") and tr.get("topic"):
                    needed.setdefault(tr["broker_id"], {}).setdefault(tr["topic"], []).append(m["id"])

            # Verbindungen zu nicht mehr benoetigten Brokern trennen
            for bid in list(self._clients.keys()):
                if bid not in needed:
                    self._stop_client(bid)

            brokers_by_id = {b["id"]: b for b in cfg["brokers"]}
            for bid, topic_map in needed.items():
                broker = brokers_by_id.get(bid)
                if not broker:
                    continue
                self._topic_map[bid] = topic_map
                if bid not in self._clients:
                    self._start_client(bid, broker)
                else:
                    client = self._clients[bid]
                    for topic in topic_map.keys():
                        try:
                            client.subscribe(topic)
                        except Exception:
                            pass

    def _start_client(self, bid: str, broker: dict) -> None:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"trigger-{bid}-{uuid.uuid4().hex[:6]}", protocol=mqtt.MQTTv311
        )
        if broker.get("username"):
            client.username_pw_set(broker.get("username"), broker.get("password") or None)
        if broker.get("tls"):
            ctx = ssl._create_unverified_context()
            client.tls_set_context(ctx)
            client.tls_insecure_set(True)

        def on_connect(c, userdata, flags, reason_code, properties):
            if reason_code == 0:
                with self._lock:
                    topics = list(self._topic_map.get(bid, {}).keys())
                for t in topics:
                    try:
                        c.subscribe(t)
                    except Exception:
                        pass

        def on_message(c, userdata, msg):
            with self._lock:
                macro_ids = list(self._topic_map.get(bid, {}).get(msg.topic, []))
            for mid in macro_ids:
                start_macro(mid)

        client.on_connect = on_connect
        client.on_message = on_message
        client.reconnect_delay_set(min_delay=1, max_delay=15)
        try:
            client.connect(broker["host"], int(broker.get("port", 1883)), keepalive=30)
            client.loop_start()
            self._clients[bid] = client
        except Exception:
            # Verbindung derzeit nicht moeglich - versucht es beim
            # naechsten rebuild() (z. B. nach Speichern eines Makros)
            # erneut. Ein staendiger Hintergrund-Retry ist hier bewusst
            # weggelassen, um die Implementierung einfach zu halten;
            # ein manuelles "Speichern" im Broker-Dialog stoesst einen
            # neuen Verbindungsversuch an.
            pass

    def _stop_client(self, bid: str) -> None:
        client = self._clients.pop(bid, None)
        self._topic_map.pop(bid, None)
        if client:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass


TRIGGERS = TriggerManager()


# ----------------------------------------------------------------------
# Makro-Ausfuehrung
# ----------------------------------------------------------------------
RUN_STATE = {}   # macro_id -> {running, step_index, rep, phase, started_at, stop_flag, last_error}
RUN_LOCK = threading.RLock()


def get_run_state(mid: str) -> dict:
    with RUN_LOCK:
        st = RUN_STATE.get(mid)
        if not st:
            return {"running": False, "step_index": -1, "rep": 0, "phase": "idle",
                     "started_at": None, "last_error": None}
        return {
            "running": st["running"],
            "step_index": st["step_index"],
            "rep": st["rep"],
            "phase": st["phase"],
            "started_at": st["started_at"],
            "last_error": st.get("last_error")
        }


def start_macro(mid: str) -> bool:
    """Startet ein Makro in einem eigenen Thread. Gibt False zurueck,
    wenn es bereits laeuft (kein ueberlappender Doppelstart)."""
    with RUN_LOCK:
        st = RUN_STATE.get(mid)
        if st and st["running"]:
            return False
        stop_evt = threading.Event()
        RUN_STATE[mid] = {
            "running": True, "step_index": -1, "rep": 0, "phase": "starting",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "stop_flag": stop_evt, "last_error": None
        }
    threading.Thread(target=_run_macro_thread, args=(mid, stop_evt), daemon=True).start()
    return True


def stop_macro(mid: str) -> bool:
    with RUN_LOCK:
        st = RUN_STATE.get(mid)
        if st and st["running"]:
            st["stop_flag"].set()
            return True
    return False


def _sleep_interruptible(seconds: float, stop_evt: threading.Event) -> bool:
    """Schlaeft in kleinen Schritten, damit ein Stop-Signal zuegig
    greift. Gibt False zurueck, wenn wegen Stop abgebrochen wurde."""
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        if stop_evt.is_set():
            return False
        time.sleep(min(0.25, max(0.0, end - time.time())))
    return not stop_evt.is_set()


def _run_macro_thread(mid: str, stop_evt: threading.Event) -> None:
    cfg = load_config()
    macro = next((m for m in cfg["macros"] if m["id"] == mid), None)
    brokers_by_id = {b["id"]: b for b in cfg["brokers"]}

    if not macro or not macro.get("steps"):
        with RUN_LOCK:
            RUN_STATE.pop(mid, None)
        return

    try:
        for idx, step in enumerate(macro["steps"]):
            if stop_evt.is_set():
                break
            with RUN_LOCK:
                RUN_STATE[mid]["step_index"] = idx
                RUN_STATE[mid]["phase"] = "publishing"
                RUN_STATE[mid]["rep"] = 0

            broker = brokers_by_id.get(step.get("broker_id"))
            repeat = max(1, int(step.get("repeat_count", 1) or 1))

            for rep in range(repeat):
                if stop_evt.is_set():
                    break
                with RUN_LOCK:
                    RUN_STATE[mid]["rep"] = rep + 1
                if broker:
                    try:
                        mqtt_publish_once(broker, step.get("topic", ""), step.get("payload", ""))
                    except Exception as e:
                        with RUN_LOCK:
                            RUN_STATE[mid]["last_error"] = f"Schritt {idx + 1}: {e}"
                else:
                    with RUN_LOCK:
                        RUN_STATE[mid]["last_error"] = f"Schritt {idx + 1}: Broker nicht gefunden."
                if rep < repeat - 1:
                    if not _sleep_interruptible(float(step.get("repeat_delay_sec", 0.5) or 0), stop_evt):
                        break

            if stop_evt.is_set():
                break

            wait_s = float(step.get("wait_after_sec", 0) or 0)
            if wait_s > 0:
                with RUN_LOCK:
                    RUN_STATE[mid]["phase"] = "waiting"
                if not _sleep_interruptible(wait_s, stop_evt):
                    break
    finally:
        with RUN_LOCK:
            if mid in RUN_STATE:
                RUN_STATE[mid]["running"] = False
                RUN_STATE[mid]["phase"] = "idle"
                RUN_STATE[mid]["step_index"] = -1


# ----------------------------------------------------------------------
# Flask-App
# ----------------------------------------------------------------------
app = Flask(__name__)


@app.route("/api/version")
def api_version():
    return jsonify({"version": APP_VERSION})


@app.route("/api/state")
def api_state():
    cfg = load_config()
    macros = []
    for m in cfg["macros"]:
        d = json.loads(json.dumps(m))
        d["run"] = get_run_state(m["id"])
        macros.append(d)
    return jsonify({"brokers": cfg["brokers"], "macros": macros})


# --- Broker-Verwaltung --------------------------------------------------
@app.route("/api/brokers", methods=["POST"])
def api_add_broker():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    host = (data.get("host") or "").strip()
    if not name or not host:
        return jsonify({"error": "Name und Host/IP sind erforderlich."}), 400
    try:
        port = int(data.get("port") or 1883)
    except (TypeError, ValueError):
        return jsonify({"error": "Port muss eine Zahl sein."}), 400

    cfg = load_config()
    broker = {
        "id": new_id("broker"),
        "name": name,
        "host": host,
        "port": port,
        "username": (data.get("username") or "").strip(),
        "password": data.get("password") or "",
        "tls": bool(data.get("tls"))
    }
    cfg["brokers"].append(broker)
    save_config(cfg)
    TRIGGERS.rebuild(cfg)
    return jsonify(broker)


@app.route("/api/brokers/<bid>", methods=["DELETE"])
def api_delete_broker(bid):
    cfg = load_config()
    in_use = any(
        (m.get("trigger", {}).get("broker_id") == bid) or
        any(s.get("broker_id") == bid for s in m.get("steps", []))
        for m in cfg["macros"]
    )
    if in_use:
        return jsonify({"error": "Dieser Broker wird noch von mindestens einem Makro verwendet und kann nicht geloescht werden."}), 400
    cfg["brokers"] = [b for b in cfg["brokers"] if b["id"] != bid]
    save_config(cfg)
    TRIGGERS.rebuild(cfg)
    return jsonify({"ok": True})


# --- Makro-Verwaltung ----------------------------------------------------
def _validate_macro_payload(data: dict, cfg: dict):
    name = (data.get("name") or "").strip()
    if not name:
        return None, "Name ist erforderlich."

    broker_ids = {b["id"] for b in cfg["brokers"]}

    trigger_in = data.get("trigger") or {}
    trigger = {
        "enabled": bool(trigger_in.get("enabled")),
        "broker_id": trigger_in.get("broker_id") or "",
        "topic": (trigger_in.get("topic") or "").strip()
    }
    if trigger["enabled"]:
        if trigger["broker_id"] not in broker_ids:
            return None, "Fuer den Auto-Start muss ein gueltiger Broker gewaehlt werden."
        if not trigger["topic"]:
            return None, "Fuer den Auto-Start wird ein Trigger-Topic benoetigt."

    steps_in = data.get("steps") or []
    if not steps_in:
        return None, "Mindestens ein Schritt ist erforderlich."

    steps = []
    for i, s in enumerate(steps_in):
        bid = s.get("broker_id") or ""
        topic = (s.get("topic") or "").strip()
        if bid not in broker_ids:
            return None, f"Schritt {i + 1}: Es muss ein gueltiger Broker gewaehlt werden."
        if not topic:
            return None, f"Schritt {i + 1}: Topic ist erforderlich."
        try:
            repeat = max(1, int(s.get("repeat_count", 1)))
        except (TypeError, ValueError):
            repeat = 1
        try:
            repeat_delay = max(0.0, float(s.get("repeat_delay_sec", 0.5)))
        except (TypeError, ValueError):
            repeat_delay = 0.5
        try:
            wait_after = max(0.0, float(s.get("wait_after_sec", 0)))
        except (TypeError, ValueError):
            wait_after = 0.0
        steps.append({
            "id": s.get("id") or new_id("step"),
            "broker_id": bid,
            "topic": topic,
            "payload": s.get("payload", ""),
            "repeat_count": repeat,
            "repeat_delay_sec": repeat_delay,
            "wait_after_sec": wait_after
        })

    return {"name": name, "trigger": trigger, "steps": steps}, None


@app.route("/api/macros", methods=["POST"])
def api_add_macro():
    cfg = load_config()
    data = request.get_json(force=True) or {}
    parsed, err = _validate_macro_payload(data, cfg)
    if err:
        return jsonify({"error": err}), 400
    macro = {"id": new_id("macro"), "created_at": datetime.now().isoformat(timespec="seconds")}
    macro.update(parsed)
    cfg["macros"].append(macro)
    save_config(cfg)
    TRIGGERS.rebuild(cfg)
    return jsonify(macro)


@app.route("/api/macros/<mid>", methods=["PUT"])
def api_edit_macro(mid):
    cfg = load_config()
    macro = next((m for m in cfg["macros"] if m["id"] == mid), None)
    if not macro:
        return jsonify({"error": "Makro nicht gefunden."}), 404
    if get_run_state(mid)["running"]:
        return jsonify({"error": "Ein laufendes Makro kann nicht bearbeitet werden."}), 400
    data = request.get_json(force=True) or {}
    parsed, err = _validate_macro_payload(data, cfg)
    if err:
        return jsonify({"error": err}), 400
    macro.update(parsed)
    save_config(cfg)
    TRIGGERS.rebuild(cfg)
    return jsonify(macro)


@app.route("/api/macros/<mid>", methods=["DELETE"])
def api_delete_macro(mid):
    if get_run_state(mid)["running"]:
        return jsonify({"error": "Ein laufendes Makro kann nicht geloescht werden."}), 400
    cfg = load_config()
    cfg["macros"] = [m for m in cfg["macros"] if m["id"] != mid]
    save_config(cfg)
    TRIGGERS.rebuild(cfg)
    with RUN_LOCK:
        RUN_STATE.pop(mid, None)
    return jsonify({"ok": True})


@app.route("/api/macros/<mid>/run", methods=["POST"])
def api_run_macro(mid):
    cfg = load_config()
    if not any(m["id"] == mid for m in cfg["macros"]):
        return jsonify({"error": "Makro nicht gefunden."}), 404
    if not start_macro(mid):
        return jsonify({"error": "Dieses Makro laeuft bereits."}), 400
    return jsonify({"ok": True})


@app.route("/api/macros/<mid>/stop", methods=["POST"])
def api_stop_macro(mid):
    if not stop_macro(mid):
        return jsonify({"error": "Dieses Makro laeuft gerade nicht."}), 400
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


# ----------------------------------------------------------------------
# Frontend (dunkles, technisches Dashboard-Design - angelehnt an das
# Farbschema/Layout des Drucker-Dashboards)
# ----------------------------------------------------------------------
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MQTT Makros</title>
<style>
  :root{
    --bg:#0a0c0f;
    --panel:#12151a;
    --panel-2:#171b21;
    --border:#242a33;
    --text:#e5e8ec;
    --text-dim:#8891a0;
    --accent:#ff9142;
    --accent-2:#3ddc97;
    --danger:#ff5d5d;
    --mono: 'JetBrains Mono', 'Consolas', 'SFMono-Regular', monospace;
    --sans: 'Inter', 'Segoe UI', system-ui, sans-serif;
  }
  *{box-sizing:border-box;}
  body{
    margin:0; background:var(--bg); color:var(--text);
    font-family:var(--sans); letter-spacing:0.1px;
  }
  header{
    display:flex; align-items:center; justify-content:space-between;
    padding:22px 32px; border-bottom:1px solid var(--border);
    background:linear-gradient(180deg,#0d1014,#0a0c0f);
  }
  header h1{
    font-size:18px; font-weight:600; margin:0; letter-spacing:0.5px;
    text-transform:uppercase; color:var(--text);
  }
  header h1 span{ color:var(--accent); }
  .header-actions{ display:flex; align-items:center; gap:10px; }
  .btn{
    background:var(--accent); color:#12100c; border:none; border-radius:6px;
    padding:10px 18px; font-weight:600; font-size:13px; cursor:pointer;
    letter-spacing:0.3px; transition:filter .15s ease;
  }
  .btn:hover{ filter:brightness(1.1); }
  .btn-ghost{
    background:transparent; color:var(--text-dim); border:1px solid var(--border);
    border-radius:6px; padding:10px 16px; font-size:13px; cursor:pointer;
  }
  .btn-ghost:hover{ color:var(--text); border-color:#3a4250; }
  .btn-mini{
    background:#1b2027; color:var(--text); border:1px solid var(--border);
    border-radius:5px; padding:6px 12px; font-size:11.5px; cursor:pointer;
    font-family:var(--mono);
  }
  .btn-mini:hover{ border-color:var(--accent-2); }
  .btn-mini.stop{ color:var(--danger); }
  .btn-mini.stop:hover{ border-color:var(--danger); }
  .btn-mini.danger:hover{ border-color:var(--danger); color:var(--danger); }
  main{ padding:28px 32px; max-width:1100px; margin:0 auto; }

  .macro-card{
    background:var(--panel); border:1px solid var(--border); border-radius:10px;
    margin-bottom:20px; overflow:hidden;
  }
  .macro-head{
    display:flex; align-items:center; justify-content:space-between;
    padding:16px 20px; background:var(--panel-2); cursor:pointer;
    border-bottom:1px solid var(--border);
  }
  .macro-head .left{ display:flex; align-items:center; gap:12px; min-width:0; }
  .expand-arrow{
    font-family:var(--mono); color:var(--text-dim); font-size:11px;
    transition:transform .15s ease; flex-shrink:0;
  }
  .macro-card.expanded .expand-arrow{ transform:rotate(90deg); }
  .macro-name{ font-size:15px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .trigger-badge{
    font-family:var(--mono); font-size:11px; padding:3px 9px; border-radius:20px;
    border:1px solid var(--border); color:var(--text-dim); white-space:nowrap;
  }
  .trigger-badge.on{ color:var(--accent-2); border-color:#2bb98755; }
  .head-right{ display:flex; align-items:center; gap:10px; flex-shrink:0; }
  .state-badge{
    font-family:var(--mono); font-size:11px; padding:3px 9px; border-radius:20px;
    border:1px solid var(--border); color:var(--text-dim); text-transform:uppercase;
  }
  .state-badge.running{ color:var(--accent-2); border-color:#2bb98755; }
  .state-badge.waiting{ color:var(--accent); border-color:#ff914255; }
  .del-icon{ cursor:pointer; color:var(--text-dim); font-size:18px; padding:0 4px;}
  .del-icon:hover{ color:var(--danger); }

  .macro-body{ display:none; padding:18px 20px 20px 20px; }
  .macro-card.expanded .macro-body{ display:block; }

  .macro-error{
    font-family:var(--mono); font-size:11.5px; color:var(--danger);
    background:#2a1414; border:1px solid #4a1f1f; border-radius:6px;
    padding:8px 10px; margin-bottom:14px;
  }

  .step-list{ display:flex; flex-direction:column; gap:8px; }
  .step-row{
    display:flex; align-items:flex-start; gap:14px; padding:12px 14px;
    background:var(--panel-2); border:1px solid var(--border); border-radius:8px;
  }
  .step-row.active{ border-color:var(--accent-2); background:#122019; }
  .step-num{
    width:24px; height:24px; border-radius:50%; background:#1b2027;
    border:1px solid var(--border); display:flex; align-items:center; justify-content:center;
    font-family:var(--mono); font-size:11.5px; color:var(--text-dim); flex-shrink:0;
  }
  .step-row.active .step-num{ border-color:var(--accent-2); color:var(--accent-2); }
  .step-info{ flex:1; min-width:0; }
  .step-line1{ font-family:var(--mono); font-size:12.5px; margin-bottom:4px; word-break:break-all; }
  .step-line1 .broker-name{ color:var(--accent-2); }
  .step-line1 .topic{ color:var(--text); }
  .step-payload{
    font-family:var(--mono); font-size:11.5px; color:var(--text-dim);
    background:#0d1014; border:1px solid var(--border); border-radius:5px;
    padding:5px 8px; margin-top:4px; word-break:break-all; max-height:60px; overflow-y:auto;
  }
  .step-meta{ display:flex; gap:14px; margin-top:6px; flex-wrap:wrap; }
  .step-chip{
    font-family:var(--mono); font-size:11px; color:var(--text-dim);
    background:#1b2027; border:1px solid var(--border); border-radius:5px; padding:3px 8px;
  }
  .step-phase{ font-family:var(--mono); font-size:11px; color:var(--accent-2); margin-top:6px; }
  .step-connector{
    width:1px; height:10px; background:var(--border); margin-left:12px;
  }

  .macro-actions{ display:flex; gap:8px; margin-top:16px; }

  .empty-state{ text-align:center; padding:70px 20px; color:var(--text-dim); }
  .empty-state .btn{ margin-top:16px; }

  .ver-badge{
    font-family:var(--mono); font-size:11px; color:var(--text-dim);
    font-weight:400; vertical-align:middle; margin-left:4px;
  }

  .toast-container{
    position:fixed; top:18px; right:18px; z-index:80;
    display:flex; flex-direction:column; gap:10px; max-width:340px;
  }
  .toast{
    background:var(--panel); border:1px solid var(--border); border-radius:8px;
    padding:12px 14px; font-size:13px; box-shadow:0 6px 18px rgba(0,0,0,.45);
  }
  .toast.ok{ border-color:#2bb98755; color:var(--accent-2); }
  .toast.err{ border-color:#c0392b55; color:var(--danger); }

  /* Modal */
  .modal-backdrop{
    display:none; position:fixed; inset:0; background:rgba(0,0,0,.6);
    align-items:center; justify-content:center; z-index:50; padding:20px;
  }
  .modal-backdrop.show{ display:flex; }
  .modal{
    background:var(--panel); border:1px solid var(--border); border-radius:10px;
    width:560px; max-width:100%; padding:24px; max-height:88vh; overflow-y:auto;
  }
  .modal.narrow{ width:440px; }
  .modal h2{ font-size:15px; margin:0 0 18px 0; text-transform:uppercase; letter-spacing:0.5px;}
  .modal label{ font-size:11px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; display:block; }
  .modal input, .modal select, .modal textarea{
    width:100%; background:#0d1014; border:1px solid var(--border); color:var(--text);
    padding:9px 10px; border-radius:6px; margin:6px 0 14px 0; font-family:var(--mono); font-size:13px;
  }
  .modal textarea{ resize:vertical; min-height:50px; }
  .modal input:focus, .modal select:focus, .modal textarea:focus{ outline:none; border-color:var(--accent); }
  .checkbox-row{ display:flex; align-items:center; gap:8px; margin:6px 0 14px 0; }
  .checkbox-row input{ width:auto; margin:0; }
  .modal-actions{ display:flex; justify-content:flex-end; gap:10px; margin-top:6px;}
  .error-msg{ color:var(--danger); font-size:12px; margin-bottom:10px; display:none;}
  .hint-text{ font-size:11px; color:var(--text-dim); margin:-8px 0 14px 0; line-height:1.4;}

  .row-2{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .row-3{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }

  .steps-editor{ margin-top:4px; }
  .step-editor-row{
    background:var(--panel-2); border:1px solid var(--border); border-radius:8px;
    padding:14px; margin-bottom:12px; position:relative;
  }
  .step-editor-head{ display:flex; align-items:center; justify-content:space-between; margin-bottom:6px; }
  .step-editor-title{ font-size:12px; font-weight:600; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px; }
  .step-editor-remove{ cursor:pointer; color:var(--text-dim); font-size:16px; }
  .step-editor-remove:hover{ color:var(--danger); }
  .step-editor-move{ cursor:pointer; color:var(--text-dim); font-size:12px; margin-right:8px; font-family:var(--mono); }
  .step-editor-move:hover{ color:var(--accent-2); }

  .broker-table{ width:100%; border-collapse:collapse; margin-bottom:18px; }
  .broker-table th{
    text-align:left; font-size:10.5px; text-transform:uppercase; color:var(--text-dim);
    border-bottom:1px solid var(--border); padding:6px 8px; letter-spacing:0.5px;
  }
  .broker-table td{
    font-family:var(--mono); font-size:12px; padding:8px; border-bottom:1px solid var(--border);
  }
  .broker-table .del-icon{ font-size:16px; }
  .no-brokers-hint{ font-size:12px; color:var(--text-dim); margin-bottom:14px; }
</style>
</head>
<body>

<header>
  <h1>MQTT<span>Makros</span> <span class="ver-badge" id="verBadge"></span></h1>
  <div class="header-actions">
    <button class="btn-ghost" onclick="openBrokerModal()">Broker verwalten</button>
    <button class="btn" onclick="openMacroModal()">+ Neues Makro</button>
  </div>
</header>

<main id="macroList"></main>

<!-- Modal: Broker verwalten -->
<div class="modal-backdrop" id="brokerModal">
  <div class="modal narrow">
    <h2>MQTT-Broker verwalten</h2>
    <div class="error-msg" id="brokerError"></div>

    <table class="broker-table" id="brokerTable"></table>
    <div class="no-brokers-hint" id="noBrokersHint" style="display:none;">Noch keine Broker angelegt.</div>

    <label>Name</label>
    <input id="b_name" placeholder="z. B. Werkstatt-Broker">
    <div class="row-2">
      <div>
        <label>Host / IP</label>
        <input id="b_host" placeholder="192.168.1.10">
      </div>
      <div>
        <label>Port</label>
        <input id="b_port" placeholder="1883">
      </div>
    </div>
    <div class="row-2">
      <div>
        <label>Benutzername (optional)</label>
        <input id="b_user" placeholder="optional">
      </div>
      <div>
        <label>Passwort (optional)</label>
        <input id="b_pass" type="password" placeholder="optional">
      </div>
    </div>
    <div class="checkbox-row">
      <input type="checkbox" id="b_tls">
      <label style="margin:0;">TLS verwenden</label>
    </div>

    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeBrokerModal()">Schliessen</button>
      <button class="btn" onclick="submitBroker()">Broker hinzufuegen</button>
    </div>
  </div>
</div>

<!-- Modal: Makro anlegen/bearbeiten -->
<div class="modal-backdrop" id="macroModal">
  <div class="modal">
    <h2 id="macroModalTitle">Neues Makro</h2>
    <div class="error-msg" id="macroError"></div>

    <label>Name des Makros</label>
    <input id="m_name" placeholder="z. B. Kammerheizung hochfahren">

    <div class="checkbox-row">
      <input type="checkbox" id="m_trigger_enabled" onchange="toggleTriggerFields()">
      <label style="margin:0;">Automatisch starten, wenn eine MQTT-Nachricht ankommt</label>
    </div>
    <div id="triggerFields">
      <div class="row-2">
        <div>
          <label>Broker</label>
          <select id="m_trigger_broker"></select>
        </div>
        <div>
          <label>Trigger-Topic</label>
          <input id="m_trigger_topic" placeholder="z. B. haus/schalter/start">
        </div>
      </div>
      <div class="hint-text">Sobald auf diesem Broker eine Nachricht (egal welchen Inhalts) auf diesem Topic ankommt, startet das Makro automatisch.</div>
    </div>

    <label style="margin-top:6px;">Schritte</label>
    <div class="steps-editor" id="stepsEditor"></div>
    <button class="btn-mini" onclick="addStepRow()" style="margin-bottom:18px;">+ Schritt hinzufuegen</button>

    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeMacroModal()">Abbrechen</button>
      <button class="btn" onclick="submitMacro()">Speichern</button>
    </div>
  </div>
</div>

<div class="toast-container" id="toasts"></div>

<script>
let STATE = { brokers: [], macros: [] };
let expandedIds = new Set();
let editingMacroId = null;
let stepsDraft = [];

function toast(msg, ok=true){
  const container = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (ok ? 'ok' : 'err');
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

function brokerName(bid){
  const b = STATE.brokers.find(x => x.id === bid);
  return b ? b.name : '(unbekannter Broker)';
}

function formatWait(sec){
  sec = Number(sec) || 0;
  if (sec <= 0) return null;
  if (sec < 60) return (sec % 1 === 0 ? sec : sec.toFixed(1)) + ' Sek.';
  const min = Math.floor(sec / 60);
  const rest = Math.round(sec % 60);
  return rest > 0 ? `${min} Min. ${rest} Sek.` : `${min} Min.`;
}

async function refresh(){
  try{
    const res = await fetch('/api/state');
    STATE = await res.json();
    render();
  } catch(e){ /* naechster Poll-Zyklus versucht es erneut */ }
}

function render(){
  const list = document.getElementById('macroList');
  if (STATE.macros.length === 0){
    list.innerHTML = `
      <div class="empty-state">
        Noch keine Makros angelegt.<br>
        <button class="btn" onclick="openMacroModal()">+ Neues Makro anlegen</button>
      </div>`;
    return;
  }
  list.innerHTML = STATE.macros.map(renderMacroCard).join('');
}

function renderMacroCard(m){
  const run = m.run || { running:false, step_index:-1, rep:0, phase:'idle' };
  const expanded = expandedIds.has(m.id);
  const trig = m.trigger || {};
  const trigBadge = trig.enabled
    ? `<span class="trigger-badge on">Auto: ${brokerName(trig.broker_id)} &rarr; ${trig.topic}</span>`
    : `<span class="trigger-badge">manuell</span>`;
  const stateBadge = run.running
    ? `<span class="state-badge ${run.phase === 'waiting' ? 'waiting' : 'running'}">${run.phase === 'waiting' ? 'wartet' : 'laeuft'}</span>`
    : `<span class="state-badge">bereit</span>`;

  const stepsHtml = (m.steps || []).map((s, idx) => {
    const active = run.running && run.step_index === idx;
    const repeatChip = s.repeat_count > 1 ? `<span class="step-chip">${s.repeat_count}x wiederholen</span>` : '';
    const waitText = formatWait(s.wait_after_sec);
    const waitChip = waitText ? `<span class="step-chip">danach ${waitText} warten</span>` : '';
    let phaseText = '';
    if (active){
      if (run.phase === 'waiting') phaseText = `<div class="step-phase">wartet vor dem naechsten Schritt&hellip;</div>`;
      else phaseText = `<div class="step-phase">sendet&hellip; (Wiederholung ${run.rep}/${s.repeat_count})</div>`;
    }
    return `
      <div class="step-row ${active ? 'active' : ''}">
        <div class="step-num">${idx + 1}</div>
        <div class="step-info">
          <div class="step-line1"><span class="broker-name">${brokerName(s.broker_id)}</span> &rarr; <span class="topic">${s.topic}</span></div>
          ${s.payload ? `<div class="step-payload">${escapeHtml(s.payload)}</div>` : ''}
          <div class="step-meta">${repeatChip}${waitChip}</div>
          ${phaseText}
        </div>
      </div>`;
  }).join('');

  const errorHtml = m.run && m.run.last_error
    ? `<div class="macro-error">${escapeHtml(m.run.last_error)}</div>` : '';

  return `
    <div class="macro-card ${expanded ? 'expanded' : ''}">
      <div class="macro-head" onclick="toggleExpand('${m.id}')">
        <div class="left">
          <span class="expand-arrow">&#9654;</span>
          <span class="macro-name">${escapeHtml(m.name)}</span>
          ${trigBadge}
        </div>
        <div class="head-right" onclick="event.stopPropagation()">
          ${stateBadge}
          ${run.running
            ? `<button class="btn-mini stop" onclick="stopMacro('${m.id}')">Stop</button>`
            : `<button class="btn-mini" onclick="runMacro('${m.id}')">Ausfuehren</button>
               <button class="btn-mini" onclick="openMacroModal('${m.id}')">Bearbeiten</button>`
          }
          <span class="del-icon" title="Loeschen" onclick="deleteMacro('${m.id}')">&times;</span>
        </div>
      </div>
      <div class="macro-body">
        ${errorHtml}
        <div class="step-list">${stepsHtml}</div>
      </div>
    </div>`;
}

function escapeHtml(str){
  const div = document.createElement('div');
  div.textContent = str == null ? '' : String(str);
  return div.innerHTML;
}

function toggleExpand(id){
  if (expandedIds.has(id)) expandedIds.delete(id);
  else expandedIds.add(id);
  render();
}

async function runMacro(id){
  const res = await fetch(`/api/macros/${id}/run`, { method:'POST' });
  const data = await res.json();
  if (!res.ok){ toast(data.error || 'Fehler beim Starten.', false); return; }
  toast('Makro gestartet.');
  refresh();
}

async function stopMacro(id){
  const res = await fetch(`/api/macros/${id}/stop`, { method:'POST' });
  const data = await res.json();
  if (!res.ok){ toast(data.error || 'Fehler beim Stoppen.', false); return; }
  toast('Makro gestoppt.');
  refresh();
}

async function deleteMacro(id){
  if (!confirm('Dieses Makro wirklich loeschen?')) return;
  const res = await fetch(`/api/macros/${id}`, { method:'DELETE' });
  const data = await res.json();
  if (!res.ok){ toast(data.error || 'Fehler beim Loeschen.', false); return; }
  expandedIds.delete(id);
  toast('Makro geloescht.');
  refresh();
}

// ---------------- Broker-Modal ----------------
function openBrokerModal(){
  renderBrokerTable();
  document.getElementById('brokerError').style.display = 'none';
  document.getElementById('brokerModal').classList.add('show');
}
function closeBrokerModal(){
  document.getElementById('brokerModal').classList.remove('show');
  ['b_name','b_host','b_port','b_user','b_pass'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('b_tls').checked = false;
}
function renderBrokerTable(){
  const table = document.getElementById('brokerTable');
  const hint = document.getElementById('noBrokersHint');
  if (STATE.brokers.length === 0){
    table.innerHTML = '';
    hint.style.display = 'block';
    return;
  }
  hint.style.display = 'none';
  table.innerHTML = `
    <tr><th>Name</th><th>Host</th><th>Port</th><th>TLS</th><th></th></tr>
    ${STATE.brokers.map(b => `
      <tr>
        <td>${escapeHtml(b.name)}</td>
        <td>${escapeHtml(b.host)}</td>
        <td>${b.port}</td>
        <td>${b.tls ? 'ja' : 'nein'}</td>
        <td><span class="del-icon" onclick="deleteBroker('${b.id}')" title="Loeschen">&times;</span></td>
      </tr>`).join('')}`;
}
async function submitBroker(){
  const payload = {
    name: document.getElementById('b_name').value.trim(),
    host: document.getElementById('b_host').value.trim(),
    port: document.getElementById('b_port').value.trim() || 1883,
    username: document.getElementById('b_user').value.trim(),
    password: document.getElementById('b_pass').value,
    tls: document.getElementById('b_tls').checked
  };
  const res = await fetch('/api/brokers', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
  });
  const data = await res.json();
  const errEl = document.getElementById('brokerError');
  if (!res.ok){ errEl.textContent = data.error; errEl.style.display = 'block'; return; }
  errEl.style.display = 'none';
  ['b_name','b_host','b_port','b_user','b_pass'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('b_tls').checked = false;
  await refresh();
  renderBrokerTable();
  toast('Broker hinzugefuegt.');
}
async function deleteBroker(id){
  if (!confirm('Diesen Broker wirklich loeschen?')) return;
  const res = await fetch(`/api/brokers/${id}`, { method:'DELETE' });
  const data = await res.json();
  if (!res.ok){ toast(data.error || 'Fehler beim Loeschen.', false); return; }
  await refresh();
  renderBrokerTable();
  toast('Broker geloescht.');
}

// ---------------- Makro-Modal ----------------
function brokerOptionsHtml(selectedId){
  if (STATE.brokers.length === 0){
    return `<option value="">-- zuerst einen Broker anlegen --</option>`;
  }
  return STATE.brokers.map(b =>
    `<option value="${b.id}" ${b.id === selectedId ? 'selected' : ''}>${escapeHtml(b.name)} (${escapeHtml(b.host)})</option>`
  ).join('');
}

function openMacroModal(macroId=null){
  editingMacroId = macroId;
  document.getElementById('macroError').style.display = 'none';
  const title = document.getElementById('macroModalTitle');

  if (macroId){
    const m = STATE.macros.find(x => x.id === macroId);
    title.textContent = 'Makro bearbeiten';
    document.getElementById('m_name').value = m.name;
    document.getElementById('m_trigger_enabled').checked = !!m.trigger.enabled;
    stepsDraft = JSON.parse(JSON.stringify(m.steps));
    setTimeout(() => {
      document.getElementById('m_trigger_broker').innerHTML = brokerOptionsHtml(m.trigger.broker_id);
      document.getElementById('m_trigger_topic').value = m.trigger.topic || '';
    }, 0);
  } else {
    title.textContent = 'Neues Makro';
    document.getElementById('m_name').value = '';
    document.getElementById('m_trigger_enabled').checked = false;
    stepsDraft = [];
    setTimeout(() => {
      document.getElementById('m_trigger_broker').innerHTML = brokerOptionsHtml(null);
      document.getElementById('m_trigger_topic').value = '';
    }, 0);
  }
  toggleTriggerFields();
  if (stepsDraft.length === 0) addStepRow();
  else renderStepsEditor();
  document.getElementById('macroModal').classList.add('show');
}

function closeMacroModal(){
  document.getElementById('macroModal').classList.remove('show');
  editingMacroId = null;
  stepsDraft = [];
}

function toggleTriggerFields(){
  const on = document.getElementById('m_trigger_enabled').checked;
  document.getElementById('triggerFields').style.display = on ? 'block' : 'none';
}

function addStepRow(){
  stepsDraft.push({
    broker_id: STATE.brokers.length ? STATE.brokers[0].id : '',
    topic: '', payload: '', repeat_count: 1, repeat_delay_sec: 0.5,
    wait_after_sec: 0, wait_unit: 'sec'
  });
  renderStepsEditor();
}
function removeStepRow(idx){
  stepsDraft.splice(idx, 1);
  renderStepsEditor();
}
function moveStepRow(idx, dir){
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= stepsDraft.length) return;
  const tmp = stepsDraft[idx];
  stepsDraft[idx] = stepsDraft[newIdx];
  stepsDraft[newIdx] = tmp;
  renderStepsEditor();
}
function updateStepField(idx, field, value){
  stepsDraft[idx][field] = value;
}

function renderStepsEditor(){
  const container = document.getElementById('stepsEditor');
  container.innerHTML = stepsDraft.map((s, idx) => {
    // Wartezeit: intern immer in Sekunden gespeichert, Anzeige je
    // nach gewaehlter Einheit (Sekunden oder Minuten) umgerechnet.
    const unit = s.wait_unit || 'sec';
    const waitDisplay = unit === 'min' ? (s.wait_after_sec / 60) : s.wait_after_sec;
    return `
    <div class="step-editor-row">
      <div class="step-editor-head">
        <span class="step-editor-title">Schritt ${idx + 1}</span>
        <div>
          <span class="step-editor-move" onclick="moveStepRow(${idx}, -1)" title="nach oben">&uarr;</span>
          <span class="step-editor-move" onclick="moveStepRow(${idx}, 1)" title="nach unten">&darr;</span>
          <span class="step-editor-remove" onclick="removeStepRow(${idx})" title="Schritt entfernen">&times;</span>
        </div>
      </div>
      <div class="row-2">
        <div>
          <label>Broker</label>
          <select onchange="updateStepField(${idx}, 'broker_id', this.value)">
            ${brokerOptionsHtml(s.broker_id)}
          </select>
        </div>
        <div>
          <label>Topic</label>
          <input value="${escapeHtml(s.topic)}" placeholder="z. B. drucker/1/befehl"
                 oninput="updateStepField(${idx}, 'topic', this.value)">
        </div>
      </div>
      <label>Payload</label>
      <textarea placeholder="z. B. ON oder {&quot;cmd&quot;:&quot;start&quot;}"
                oninput="updateStepField(${idx}, 'payload', this.value)">${escapeHtml(s.payload)}</textarea>
      <div class="row-3">
        <div>
          <label>Wiederholungen</label>
          <input type="number" min="1" step="1" value="${s.repeat_count}"
                 oninput="updateStepField(${idx}, 'repeat_count', this.value)">
        </div>
        <div>
          <label>Wartezeit nach Schritt</label>
          <input type="number" min="0" step="0.5" value="${waitDisplay}"
                 oninput="onWaitInput(${idx}, this.value)">
        </div>
        <div>
          <label>Einheit</label>
          <select onchange="onWaitUnitChange(${idx}, this.value)">
            <option value="sec" ${unit === 'sec' ? 'selected' : ''}>Sekunden</option>
            <option value="min" ${unit === 'min' ? 'selected' : ''}>Minuten</option>
          </select>
        </div>
      </div>
    </div>`;
  }).join('');
}

function onWaitInput(idx, value){
  const unit = stepsDraft[idx].wait_unit || 'sec';
  const num = parseFloat(value) || 0;
  stepsDraft[idx].wait_after_sec = unit === 'min' ? num * 60 : num;
}
function onWaitUnitChange(idx, unit){
  stepsDraft[idx].wait_unit = unit;
  renderStepsEditor();
}

async function submitMacro(){
  const errEl = document.getElementById('macroError');
  const payload = {
    name: document.getElementById('m_name').value.trim(),
    trigger: {
      enabled: document.getElementById('m_trigger_enabled').checked,
      broker_id: document.getElementById('m_trigger_broker').value,
      topic: document.getElementById('m_trigger_topic').value.trim()
    },
    steps: stepsDraft.map(s => ({
      broker_id: s.broker_id, topic: s.topic, payload: s.payload,
      repeat_count: s.repeat_count, repeat_delay_sec: s.repeat_delay_sec,
      wait_after_sec: s.wait_after_sec
    }))
  };

  const url = editingMacroId ? `/api/macros/${editingMacroId}` : '/api/macros';
  const method = editingMacroId ? 'PUT' : 'POST';
  const res = await fetch(url, {
    method, headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (!res.ok){ errEl.textContent = data.error; errEl.style.display = 'block'; return; }
  errEl.style.display = 'none';
  closeMacroModal();
  toast(editingMacroId ? 'Makro aktualisiert.' : 'Makro angelegt.');
  await refresh();
}

async function loadVersion(){
  try{
    const res = await fetch('/api/version');
    const data = await res.json();
    document.getElementById('verBadge').textContent = 'v' + data.version;
  } catch(e){ /* Version ist rein informativ */ }
}

loadVersion();
refresh();
setInterval(refresh, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    _cfg = load_config()
    TRIGGERS.rebuild(_cfg)
    host = _cfg["server"].get("host", "0.0.0.0")
    port = int(_cfg["server"].get("port", 8010))
    print(f"MQTT-Makro-Zentrale laeuft auf http://{host}:{port}  (im lokalen Netz erreichbar ueber die IP dieses PCs)")
    app.run(host=host, port=port, debug=False, threaded=True)

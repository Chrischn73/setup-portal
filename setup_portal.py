#!/usr/bin/env python3
"""
=============================================================================
WICHTIG - AN KI-ASSISTENTEN UND MENSCHEN, DIE DIESE DATEI BEARBEITEN:

Dies ist das eigenstaendige GitHub-Repo "setup-portal" (lokal unter
/media/SSD/Sichern/claude/setup-portal/) - NICHT Teil von HonigBox
(honigbox-webseite/) oder der Imker-App/BeeTown (imker-app/), auch wenn es
von beiden genutzt wird. Frueher (bis 2026-08-10) gab es hiervon noch
manuell synchronisierte Kopien in beiden App-Repos (per sync.sh) - das
entfaellt jetzt vollstaendig:

    - HonigBox und die Imker-App laden dieses Repo per install.sh EINMALIG
      als Bootstrap-Schritt herunter (nur falls /opt/setup-portal noch
      nicht existiert) und fuehren dessen eigenes install.sh aus.
    - Danach aktualisiert sich das Portal SELBST taeglich (--self-update,
      siehe setup-portal-update-check.timer) direkt aus den Releases
      dieses Repos - unabhaengig von jedem weiteren install.sh-Lauf einer
      der beiden Apps.

Workflow fuer Aenderungen: hier bearbeiten, PORTAL_VERSION weiter unten
erhoehen, committen, GitHub-Release mit passendem Tag veroeffentlichen -
fertig, keine zweite Kopie, kein sync.sh mehr noetig.

Warum kein Git submodule/subtree in den beiden App-Repos: der Update-
Mechanismus der Apps laedt automatisch den von GitHub generierten Tarball
eines Release - GitHub packt dabei NIE den Inhalt von Submodulen mit ein
(nur einen Commit-Hash-Verweis). Der Bootstrap-Download hier funktioniert
trotzdem, weil er ein EIGENSTAENDIGES Repo per curl+tar herunterlaedt, kein
Submodul eines anderen Repos ist.

Diese Datei kennt trotzdem NIE App-Namen oder App-spezifische Logik (kein
HonigBox-/BeeTown-Code hier) - alles App-Spezifische kommt ausschliesslich
aus den zur Laufzeit unter apps.d/*.json registrierten Descriptor-Dateien.
=============================================================================

Dauerhaft laufender, generischer Setup-Webserver fuer Raspberry-Pi-Projekte
(ein einziger Port, http://<hostname>.local), faellt auf einen Ausweich-Port
aus (SETUP_PORTAL_LANDING_PORT), falls 80 beim Einrichten schon belegt war.

Anders als die beiden Vorlagen, aus denen dieses Skript entstanden ist
(imkerei_wifi_portal.py fuer "BeeTown", honigbox_setup_portal.py fuer
"HonigBox"), ist hier KEINE Anwendung hart verdrahtet. Stattdessen liest
dieses Skript pro registrierter App eine kleine JSON-Beschreibung aus
APPS_DIR (siehe load_apps()) und rendert Landing-Page, Backup- und
Update-Seiten je nach Anzahl registrierter Apps (0, 1 oder mehrere). Jede
App traegt ihre Beschreibung selbst per eigenem install.sh ein - dieses
Skript hier weiss nichts Anwendungsspezifisches (keine Tuer-Sensor-Logik,
kein Foto-Handling, nichts), es kennt nur das generische Schema.

- Startseite: ein Oeffnen-Button pro registrierter App, IPs, System-Buttons
  (WLAN-Einstellungen + Neustart/Herunterfahren nur auf einem echten
  Raspberry Pi sichtbar bzw. erreichbar, siehe IS_PI)
- /wifi: WLAN einrichten/wechseln/trennen (voellig app-unabhaengig)
- /backup: pro App ein Abschnitt (sichern/wiederherstellen/herunterladen),
  ein gemeinsamer "Alle sichern"-Button, USB-Stick-Einrichtung (geteilt)
- /update: pro App ein Abschnitt (Version pruefen/aktualisieren/
  zurueckwechseln, automatische Updates), ein gemeinsamer
  "Alle aktualisieren"-Button
- Startseite: zusaetzlich pro installierter App ein Hinweis+Button, falls
  ihr Descriptor ein optionales "companion"-Feld setzt (Partner-App, die
  noch nicht registriert ist) - laedt deren neuestes GitHub-Release herunter
  und fuehrt dessen setup/install.sh aus (siehe _run_companion_install_in_
  background). Rein deklarativ: dieses Skript kennt auch dabei keine
  App-Namen, nur was im "companion"-Objekt steht.
- /update: zusaetzlich pro App ein Button "Komplett von GitHub
  aktualisieren" (siehe _run_install_script_in_background) - ein normales, file_map-
  basiertes Update kopiert nur App-eigene Dateien, NIE install.sh-eigene
  Aenderungen (Boot-Bildschirm, apps.d-Descriptor-Felder der jeweiligen App).
  Erspart dafuer den manuellen SSH-Login. Betrifft NICHT den Portal-Code
  selbst - der aktualisiert sich unabhaengig davon selbst, siehe naechster
  Punkt.
- /update: ausserdem eine eigene Karte "Setup-Portal selbst" mit einem
  Button, der den taeglichen Selbst-Update-Check manuell sofort anstoesst
  (siehe trigger_self_update_check()) - ueber den unabhaengigen systemd-
  Dienst (systemctl start --no-block), NICHT als Thread im laufenden
  Webserver-Prozess, aus demselben Sicherheitsgrund wie bei --self-update
  unten.
- CLI --self-update (siehe setup-portal-update-check.timer, taeglich):
  prueft das eigene GitHub-Repo (SELF_UPDATE_GITHUB_REPO) auf ein neueres
  Release und aktualisiert sich bei Bedarf selbst. Bewusst NUR per Timer/
  CLI, nicht per Web-UI-Button (siehe Kommentar bei _self_update()).

Nur Python-Standardbibliothek.
"""
import html
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote

PORTAL_VERSION = "1.8.5"

PORTAL_DIR = "/opt/setup-portal"
# Jede App legt hier per eigenem install.sh genau eine Datei <app-id>.json
# ab (siehe load_apps() fuer das erwartete Schema). Wird bei JEDEM Request
# neu eingelesen (kein Cache) - kleine lokale Dateien, kein Perf-Problem,
# dafuer werden neu registrierte Apps ohne Neustart des Portals erkannt.
APPS_DIR = f"{PORTAL_DIR}/apps.d"
# Pro App ein Unterordner <app-id>/ mit update_check.json, update.conf,
# last_update_result.json.
STATE_DIR = f"{PORTAL_DIR}/state"
# Pro App optional ein Unterordner <app-id>/ mit selbst abgelegten
# VPN-Screenshots. "_shared" ist ein reservierter Pseudo-App-Ordner fuer die
# (app-unabhaengige) Fritzbox/WireGuard-Anleitung, siehe _vpn_image_app_id().
HILFE_IMAGES_DIR = f"{PORTAL_DIR}/hilfe-bilder"

HOST = "0.0.0.0"
PORT_LANDING = int(os.environ.get("SETUP_PORTAL_LANDING_PORT", "80"))

# Wird von do_POST() waehrend eines WLAN-Verbindungsversuchs aktualisiert und
# von GET /wifi/status abgefragt (Polling von der "Verbinde..."-Seite aus).
CONN_STATE = {"done": False, "ok": None, "detail": None}

# Wird waehrend des (langwierigen) Formatierens eines USB-Sticks aktualisiert
# und von GET /backup/usb/format-status abgefragt. EIN globaler USB-Stick
# fuer alle Apps - kein App-Bezug noetig.
FORMAT_STATE = {"done": True, "ok": None, "detail": None}

# Ein Update-Status je App-ID (plus dem Pseudo-Schluessel "_all" fuer
# "Alle aktualisieren") - anders als CONN_STATE/FORMAT_STATE, die als
# global inhaerent nur einen Vorgang gleichzeitig kennen.
UPDATE_STATE = {}

# Analog zu UPDATE_STATE, aber fuer das Nachinstallieren einer Partner-App
# (siehe "companion"-Feld im Descriptor) - bewusst NICHT UPDATE_STATE
# mitbenutzt: die GET /update/status/<id>-Route lehnt unbekannte App-IDs
# frueh ab (siehe dortiger Kommentar), waehrend eine Partner-App per
# Definition WAEHREND der Installation noch KEINEN eigenen apps.d/-Eintrag
# hat - genau dann muss das Polling trotzdem funktionieren.
COMPANION_INSTALL_STATE = {}


def _companion_install_state(app_id):
    return COMPANION_INSTALL_STATE.setdefault(app_id, {"done": True, "ok": None, "detail": None})


def _known_companion_ids():
    """Alle App-IDs, die IRGENDEINE aktuell registrierte App als 'companion'
    deklariert - Whitelist fuer GET /companion/install/status/<id>, damit
    dort nicht beliebige Pfade COMPANION_INSTALL_STATE unbegrenzt wachsen
    lassen koennen (gleiches Muster wie bei GET /update/status/<id>)."""
    return {app["companion"]["app_id"] for app in load_apps() if app.get("companion")}


def _update_state(app_id):
    return UPDATE_STATE.setdefault(app_id, {"done": True, "ok": None, "detail": None})


def _detect_is_pi():
    """True nur auf einem echten Raspberry Pi (per Device-Tree-Modellname) -
    steuert, ob WLAN-Einstellungen und Neustart/Herunterfahren ueberhaupt
    angeboten werden. Backup und Update bleiben auch ohne Pi nutzbar."""
    try:
        with open("/proc/device-tree/model") as f:
            return "raspberry pi" in f.read().lower()
    except OSError:
        return False


IS_PI = _detect_is_pi()
# Auf einem Linux-Server ist der lokale Backup-Ort keine SD-Karte, sondern
# die normale Server-Platte - "SD-Karte" waere dort irrefuehrend.
LOCAL_BACKUP_LABEL = "SD-Karte" if IS_PI else "Lokal"
LOCAL_BACKUP_PHRASE = "auf der SD-Karte" if IS_PI else "lokal"

HILFE_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.(png|jpg|jpeg)$", re.IGNORECASE)

# Backups/USB-Stick sind geteilte, app-unabhaengige Infrastruktur - beide
# Vorlagen-Projekte verwenden bereits dieselben Pfade dafuer.
BACKUP_DIR = "/opt/backup"
BACKUP_CONFIG_PATH = "/opt/backup-scripts/backup.conf"
MIN_MAX_BACKUPS = 20
DEFAULT_MAX_BACKUPS = 30
USB_MOUNT = "/mnt/backup-usb"

STYLE = """
  :root {{
    --bg: #faf6ee; --fg: #241f17; --muted: #6e6353; --box-bg: #ece3d2;
    --msg-ok-bg: #dfd; --msg-err-bg: #fdd;
    --input-bg: #fff; --input-border: #ece3d2;
    --btn-bg: #d98e04; --btn-fg: #fff; --btn-active: #b87503;
    --danger-bg: #c92a2a; --danger-fg: #fff; --danger-active: #a02020;
    --open-bg: #4caf50; --open-fg: #fff; --open-active: #3d8b40;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #15120d; --fg: #efe7d8; --muted: #b0a48d; --box-bg: #211c15;
      --msg-ok-bg: #17301d; --msg-err-bg: #3a1c1c;
      --input-bg: #211c15; --input-border: #352d22;
      --btn-bg: #eaa92a; --btn-fg: #1a1a1a; --btn-active: #c9901a;
      --danger-bg: #ff6b6b; --danger-fg: #1a1a1a; --danger-active: #e05555;
      --open-bg: #66bb6a; --open-fg: #0f1f10; --open-active: #57a05b;
    }}
  }}
  body {{ font-family: sans-serif; max-width: 420px; margin: 2rem auto; padding: 0 1rem;
          background: var(--bg); color: var(--fg); }}
  h1 {{ font-size: 1.3rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 2rem; }}
  p {{ line-height: 1.5; }}
  label {{ display: block; margin-top: 1rem; font-weight: bold; }}
  select, input {{ width: 100%; padding: .6rem; font-size: 1rem; box-sizing: border-box; margin-top: .25rem;
            background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); }}
  button, .btn {{ display: block; width: 100%; padding: .8rem; font-size: 1rem; margin-top: 1.5rem;
            background: var(--btn-bg); border: none; border-radius: 8px; box-sizing: border-box;
            text-align: center; text-decoration: none; color: var(--btn-fg); font-weight: bold; }}
  button:active, .btn:active {{ background: var(--btn-active); }}
  .btn-danger {{ background: var(--danger-bg); color: var(--danger-fg); }}
  .btn-danger:active {{ background: var(--danger-active); }}
  .btn-open {{ background: var(--open-bg); color: var(--open-fg); }}
  .btn-open:active {{ background: var(--open-active); }}
  .msg {{ padding: .8rem; border-radius: 6px; margin-bottom: 1rem; background: var(--box-bg); }}
  .err {{ background: var(--msg-err-bg); }}
  .ok  {{ background: var(--msg-ok-bg); }}
  .loading-bee {{ width: 28px; height: 28px; display: inline-block; vertical-align: -0.5em;
              margin-right: .4em; animation: bee-fly 0.5s ease-in-out infinite alternate; }}
  @keyframes bee-fly {{ from {{ transform: translateY(0px) rotate(-4deg); }}
                        to   {{ transform: translateY(-4px) rotate(4deg); }} }}
  .header {{ display: flex; align-items: center; gap: .6rem; margin-bottom: 1rem; }}
  .header .logo {{ font-size: 1.8rem; }}
  .header .name {{ font-weight: bold; font-size: 1.1rem; }}
  .header .portal-version {{ display: block; font-weight: normal; font-size: .7rem; opacity: .6; }}
  .btn-row {{ display: flex; gap: .5rem; margin-top: 1.5rem; }}
  .btn-row form {{ flex: 1; margin: 0; }}
  .btn-small {{ margin-top: 0; padding: .5rem; font-size: .85rem; }}
  .muted {{ color: var(--muted); }}
  .app-section {{ text-align: left; }}
  .app-section h2 {{ margin-top: 0; }}
  .donate-box {{ margin-top: 1.2rem; padding-top: 1rem; border-top: 1px solid var(--input-border);
                  text-align: center; }}
  .donate-box p {{ color: var(--muted); font-size: .85rem; margin: 0 0 .6rem; }}
  .donate-box .btn {{ display: inline-flex; width: auto; margin-top: 0; padding: .35rem .9rem;
                       font-size: .78rem; font-weight: 500; border-radius: 999px;
                       background: transparent; color: var(--muted); border: 1px solid var(--input-border); }}
  .modal-backdrop {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5);
                      align-items: center; justify-content: center; z-index: 1000; }}
  .modal-backdrop.show {{ display: flex; }}
  .modal-box {{ background: var(--bg); color: var(--fg); border-radius: 12px; padding: 1.5rem;
                max-width: 320px; width: 85%; text-align: center; }}
  .modal-box h1 {{ font-size: 1.1rem; }}
"""

# Gleiche Wackel-Biene wie in beiden Vorlagen-Projekten - rein kosmetisch,
# App-unabhaengig, daher unveraendert uebernommen (nur umbenannt).
SPINNER_SVG = (
    '<svg class="loading-bee" viewBox="0 0 40 40" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">'
    '<ellipse cx="20" cy="24" rx="9" ry="12" fill="#f5c518"/>'
    '<rect x="11" y="21" width="18" height="4" rx="2" fill="#241f17" opacity=".7"/>'
    '<rect x="11" y="27" width="18" height="4" rx="2" fill="#241f17" opacity=".7"/>'
    '<circle cx="20" cy="12" r="6" fill="#241f17"/>'
    '<line x1="17" y1="7" x2="14" y2="3" stroke="#241f17" stroke-width="1.5" stroke-linecap="round"/>'
    '<line x1="23" y1="7" x2="26" y2="3" stroke="#241f17" stroke-width="1.5" stroke-linecap="round"/>'
    '<circle cx="14" cy="3" r="1.5" fill="#f5c518"/>'
    '<circle cx="26" cy="3" r="1.5" fill="#f5c518"/>'
    '<ellipse cx="10" cy="18" rx="7" ry="4" fill="rgba(200,230,255,0.75)" transform="rotate(-20 10 18)"/>'
    '<ellipse cx="30" cy="18" rx="7" ry="4" fill="rgba(200,230,255,0.75)" transform="rotate(20 30 18)"/>'
    "</svg>"
)

SYSTEM_BUTTONS = """
<div class="btn-row">
<form method="post" action="/system/reboot" onsubmit="return confirm('Pi wirklich neu starten?');">
  <button type="submit" class="btn-danger btn-small">🔄 Neu starten</button>
</form>
<form method="post" action="/system/shutdown" onsubmit="return confirm('Pi wirklich herunterfahren? Danach muss der Strom manuell getrennt und wieder verbunden werden, um ihn erneut zu starten.');">
  <button type="submit" class="btn-danger btn-small">⏻ Herunterfahren</button>
</form>
</div>
"""

PAGE_LANDING = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>""" + STYLE + """</style>
</head><body>
{header}
{status}
{update_banner}
{app_cards}
{companion_section}
{wifi_link}<a class="btn" href="/backup">📦 Backups</a>
<a class="btn" href="/update">🔄 Update</a>
<a class="btn" href="/hilfe">❓ Hilfe</a>
<div class="msg" style="font-size:.9rem;">
<strong>IP-Adressen:</strong><br>
{ip_lines}
</div>
{system_buttons}
{donate_section}

<div id="companion-install-modal" class="modal-backdrop">
  <div class="modal-box" id="companion-install-modal-content"></div>
</div>
<script>
function companionInstallPoll(appId, content) {{
  fetch('/companion/install/status/' + appId).then(r => r.json()).then(function(d) {{
    if (!d.done) {{ setTimeout(function() {{ companionInstallPoll(appId, content); }}, 3000); return; }}
    content.innerHTML = d.ok
      ? '<div class="msg ok">✅ ' + d.detail + '</div>'
      : '<div class="msg err">❌ ' + (d.detail || 'Installation abgebrochen.') + '</div>';
    setTimeout(function() {{ window.location.reload(); }}, 3000);
  }}).catch(function() {{ setTimeout(function() {{ companionInstallPoll(appId, content); }}, 3000); }});
}}
function startCompanionInstall(hostAppId, companionAppId, companionLabel) {{
  if (!confirm(companionLabel + ' jetzt automatisch von GitHub herunterladen und installieren?')) {{
    return false;
  }}
  var modal = document.getElementById('companion-install-modal');
  var content = document.getElementById('companion-install-modal-content');
  content.innerHTML = '<h1>""" + SPINNER_SVG + """Installiere ' + companionLabel + '…</h1>' +
    '<p class="muted">Neueste Version wird von GitHub geladen und eingerichtet. ' +
    'Das kann einige Minuten dauern – bitte die Seite nicht schließen.</p>';
  modal.classList.add('show');
  fetch('/companion/install/' + hostAppId, {{method: 'POST'}}).then(r => r.json()).then(function(d) {{
    if (!d.started) {{
      content.innerHTML = '<div class="msg err">❌ ' + (d.error || 'Konnte nicht gestartet werden.') + '</div>';
      return;
    }}
    companionInstallPoll(companionAppId, content);
  }});
  return false;
}}
</script>
</body></html>
"""

TIPS_CONTENT = """
<p class="muted" style="text-align:center; font-size:.9rem;">Für ein eigenes App-Symbol ohne Adressleiste
auf dem Home-Bildschirm. Die App sieht dann auf dem Handy wie eine echte App aus.</p>

<div class="msg ok" style="margin-top:1.5rem;">
🍎 <strong>iPhone/iPad</strong><br>
Das geht nur im <strong>Safari</strong>-Browser – andere Browser (z. B. Chrome)
können auf dem iPhone kein App-Symbol anlegen.
<ol style="margin:.6rem 0 0; padding-left:1.2rem;">
  <li>Die App im <strong>Safari</strong>-Browser öffnen</li>
  <li>Unten in der Leiste die drei <strong>…</strong> antippen, dann (bei iPad: oben) das
      <strong>Teilen-Symbol</strong> ⬆️ antippen</li>
  <li>Im aufklappenden Menü nach unten scrollen und <strong>„Zum Home-Bildschirm“</strong> antippen</li>
  <li>Oben rechts auf <strong>„Hinzufügen“</strong> tippen</li>
</ol>
</div>

<div class="msg ok" style="margin-top:1.5rem;">
🤖 <strong>Android</strong><br>
Am einfachsten im <strong>Chrome</strong>-Browser:
<ol style="margin:.6rem 0 0; padding-left:1.2rem;">
  <li>Die App in <strong>Chrome</strong> öffnen</li>
  <li>Oben rechts auf das <strong>Drei-Punkte-Menü</strong> ⋮ tippen</li>
  <li><strong>„Zum Startbildschirm hinzufügen“</strong> antippen (heißt je nach
      Chrome-Version auch „App installieren“)</li>
  <li>Mit <strong>„Hinzufügen“</strong> bzw. „Installieren“ bestätigen</li>
</ol>
</div>
"""

PAGE_TIPPS = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Handy-Tipps</title>
<style>""" + STYLE + """</style>
</head><body>
{header}
<h1>📱 Handy-Tipps</h1>""" + TIPS_CONTENT + """
<a class="btn" href="/">← Zurück zur Übersicht</a>
</body></html>
"""

PAGE_HILFE = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hilfe</title>
<style>""" + STYLE + """</style>
</head><body>
{header}
<h1>❓ Hilfe</h1>
{app_beschreibungen}
<a class="btn" href="/tipps">📱 Handy-Tipps</a>
<p class="muted" style="font-size:.85rem; margin-top:.4rem;">Die App sieht dann aus wie eine echte App und wird
nicht direkt im Browser geöffnet.</p>
<a class="btn" href="/hilfe/vpn" style="margin-top:1.5rem;">🔒 VPN-Einrichtung</a>
<p class="muted" style="font-size:.85rem; margin-top:.4rem;">Für den Zugriff von unterwegs, außerhalb
des Heimnetzes.</p>
<a class="btn" href="/" style="margin-top:1.5rem;">← Zurück zur Übersicht</a>
</body></html>
"""

PAGE_VPN = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VPN-Einrichtung</title>
<style>""" + STYLE + """
  .hilfe-step {{ margin-top: 1rem; }}
  .hilfe-step img {{ max-width: 100%; border-radius: 8px; margin-top: .5rem; display: block; }}
</style>
</head><body>
{header}
<h1>🔒 VPN-Einrichtung</h1>
<p class="muted">Mit einem VPN lässt sich die App auch von unterwegs sicher erreichen, ohne den Zugriff
öffentlich ins Internet freizugeben. Die folgenden Schritte gelten für eine Fritzbox (ab FRITZ!OS 7.39, WireGuard
ist dort eingebaut) und die WireGuard-App auf dem Handy.</p>

<h2>1. VPN-Zugang auf der Fritzbox einrichten</h2>
<div class="hilfe-step">
  <p>Im Heimnetz auf <code>fritz.box</code> mit dem Fritzbox-Kennwort anmelden.</p>
  <img src="{vpn_img_prefix}/fritzbox-1.png" alt="" onerror="this.style.display='none'">
</div>
<div class="hilfe-step">
  <p>Zu „Internet" → „Freigaben" → Reiter „VPN (WireGuard)" wechseln und auf „WireGuard-Verbindung hinzufügen" tippen.</p>
  <img src="{vpn_img_prefix}/fritzbox-2.png" alt="" onerror="this.style.display='none'">
</div>
<div class="hilfe-step">
  <p>„Einzelgerät verbinden" auswählen.</p>
  <img src="{vpn_img_prefix}/fritzbox-3.png" alt="" onerror="this.style.display='none'">
</div>
<div class="hilfe-step">
  <p>Einen beliebigen Namen vergeben (z. B. „Handy CF").</p>
  <img src="{vpn_img_prefix}/fritzbox-4.png" alt="" onerror="this.style.display='none'">
</div>
<div class="hilfe-step">
  <p>Zur Bestätigung muss an der Fritzbox nun ein beliebiger Knopf gedrückt werden.</p>
</div>
<div class="hilfe-step">
  <p>Nun wird ein QR-Code angezeigt. Diesen abspeichern und danach mit der WireGuard-App am Handy einscannen.</p>
  <img src="{vpn_img_prefix}/fritzbox-5.png" alt="" onerror="this.style.display='none'">
</div>

<h2>2. WireGuard auf dem Handy einrichten</h2>
<div class="hilfe-step">
  <p>Die App „WireGuard" aus dem App Store (iPhone) bzw. Play Store (Android) installieren.</p>
  <img src="{vpn_img_prefix}/wireguard-1.png" alt="" onerror="this.style.display='none'">
</div>
<div class="hilfe-step">
  <p>App öffnen, auf „+" tippen und „Aus QR-Code scannen" wählen, dann den QR-Code von der Fritzbox-Seite abscannen.</p>
  <img src="{vpn_img_prefix}/wireguard-2.png" alt="" onerror="this.style.display='none'">
</div>
<div class="hilfe-step">
  <p>Verbindung benennen und speichern.</p>
  <img src="{vpn_img_prefix}/wireguard-3.png" alt="" onerror="this.style.display='none'">
</div>

<div class="msg ok" style="margin-top:1.5rem;">Von unterwegs: in der WireGuard-App den Schalter aktivieren, um sich
mit dem Heimnetz zu verbinden – danach ist die App wie gewohnt erreichbar.</div>

<p class="muted" style="font-size:.8rem; margin-top:1rem;">Menübezeichnungen können sich je nach FRITZ!OS-/App-Version
leicht unterscheiden.</p>

<a class="btn" href="/hilfe" style="margin-top:1.5rem;">← Zurück zur Hilfe</a>
</body></html>
"""

PAGE_FORM = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WLAN-Einstellungen</title>
<style>""" + STYLE + """</style>
</head><body>
{header}
<h1>📶 WLAN-Einstellungen</h1>
{status}
{message}
<form method="post" action="/wifi/connect">
  <label for="ssid">WLAN-Name (SSID)</label>
  <div id="ssid-loading" class="msg">""" + SPINNER_SVG + """Suche nach WLAN-Netzen…</div>
  <select id="ssid" name="ssid" style="display:none"></select>
  <input id="ssid_manual" name="ssid_manual" placeholder="SSID manuell" style="display:none; margin-top:.5rem">
  <label for="password">WLAN-Passwort</label>
  <input type="password" id="password" name="password" autocomplete="off">
  <button type="submit">Verbinden</button>
</form>
{disconnect_form}
<a class="btn" href="/" style="margin-top:1.5rem;">← Zurück zur Übersicht</a>
<script>
fetch('/wifi/networks').then(r => r.json()).then(function(nets) {{
  var loading = document.getElementById('ssid-loading');
  var sel = document.getElementById('ssid');
  var manual = document.getElementById('ssid_manual');
  if (nets && nets.length) {{
    nets.forEach(function(n) {{
      var opt = document.createElement('option');
      opt.value = n.ssid; opt.textContent = n.ssid;
      sel.appendChild(opt);
    }});
    var manualOpt = document.createElement('option');
    manualOpt.value = ''; manualOpt.textContent = '– manuell eingeben –';
    sel.appendChild(manualOpt);
    sel.style.display = '';
  }} else {{
    manual.placeholder = 'SSID (kein Netz gefunden)';
  }}
  manual.style.display = '';
  loading.style.display = 'none';
}}).catch(function() {{
  var loading = document.getElementById('ssid-loading');
  loading.textContent = '❌ Fehler beim Suchen nach WLAN-Netzen.';
  document.getElementById('ssid_manual').style.display = '';
}});
</script>
</body></html>
"""

DISCONNECT_FORM = """
<form method="post" action="/wifi/disconnect" onsubmit="return confirmDisconnect()">
  <button type="submit" class="btn-danger">🔌 WLAN trennen</button>
</form>
<script>
function confirmDisconnect() {
  if (!confirm('WLAN wirklich trennen? Die App ist danach eventuell ' +
               'nicht mehr erreichbar, falls kein Netzwerkkabel angeschlossen ist.')) {
    return false;
  }
  return confirm('Ganz sicher? Diese WLAN-Einstellungen-Seite bleibt zwar erreichbar, ' +
                  'aber die App kann offline gehen, bis ein neues WLAN eingerichtet ist.');
}
</script>
"""

PAGE_CONNECTING = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verbinde…</title>
<style>""" + STYLE + """</style>
</head><body>
<div id="status">
  <h1>""" + SPINNER_SVG + """Verbinde mit „{ssid}“…</h1>
  <p>Der Pi verbindet sich jetzt mit dem WLAN. Falls gerade eine andere
  WLAN-Verbindung aktiv war, bleibt sie bestehen, falls die neue nicht
  klappt.</p>
</div>
<a class="btn" href="/">← Zurück zur Übersicht</a>
<script>
(function poll() {{
  fetch('/wifi/status').then(r => r.json()).then(data => {{
    if (!data.done) {{ setTimeout(poll, 1500); return; }}
    var el = document.getElementById('status');
    if (data.ok) {{
      el.innerHTML = '<div class="msg ok">✅ Verbindung erfolgreich hergestellt! ' +
        'Weiter zur Setup-Seite …</div>';
      setTimeout(function() {{
        window.location.href = 'http://' + location.hostname + '/';
      }}, 2500);
    }} else {{
      el.innerHTML = '<div class="msg err">❌ Verbindung fehlgeschlagen'
        + (data.detail ? ': ' + data.detail : '') + '</div>'
        + '<a class="btn" href="/wifi">Zurück zu den WLAN-Einstellungen</a>';
    }}
  }}).catch(() => setTimeout(poll, 1500));
}})();
</script>
</body></html>
"""

PAGE_BACKUP = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backups</title>
<style>""" + STYLE + """</style>
</head><body>
{header}
<h1>📦 Backups</h1>
{message}
{app_sections}
{all_backup_button}

<h2>Einstellungen</h2>
<p class="muted">Backups laufen automatisch jede Nacht (03:30 Uhr). Aufbewahrung
nach dem Vater-Sohn-Prinzip: die letzten 14 Tage einzeln, danach automatisch
ausgedünnt auf eine Sicherung pro Woche, Monat und Jahr – so bleibt auch
ältere Historie sinnvoll erhalten, ohne dass du Zeitpläne oder Stufen selbst
verwalten musst. Einzige Einstellung ist die Gesamtanzahl (gilt gemeinsam
für alle Apps je Ort).</p>
<form method="post" action="/backup/settings">
  <label for="max_backups">Max. Anzahl Backups insgesamt (je Ort)</label>
  <input type="number" id="max_backups" name="max_backups" min="20" max="200" value="{max_backups}">
  <button type="submit">Einstellung speichern</button>
</form>

<h2>USB-Stick</h2>
{usb_section}

<a class="btn" href="/">← Zurück zur Übersicht</a>

<div id="format-modal" class="modal-backdrop">
  <div class="modal-box" id="format-modal-content"></div>
</div>
<script>
function confirmFormat(warning) {{
  return confirm(warning) &&
         confirm('Wirklich ganz sicher? Formatieren löscht alle vorhandenen Daten auf dem Stick unwiderruflich.');
}}
function startFormat(form, warning) {{
  if (!confirmFormat(warning)) return false;
  var modal = document.getElementById('format-modal');
  var content = document.getElementById('format-modal-content');
  content.innerHTML = '<h1>""" + SPINNER_SVG + """Formatiere…</h1>' +
    '<p class="muted">Bitte warten – das kann je nach Stick-Größe einige Minuten dauern.</p>';
  modal.classList.add('show');
  fetch('/backup/usb/format', {{method: 'POST', body: new URLSearchParams(new FormData(form))}});
  (function poll() {{
    fetch('/backup/usb/format-status').then(r => r.json()).then(function(d) {{
      if (!d.done) {{ setTimeout(poll, 2000); return; }}
      content.innerHTML = d.ok
        ? '<div class="msg ok">✅ ' + d.detail + '</div>'
        : '<div class="msg err">❌ ' + d.detail + '</div>';
      setTimeout(function() {{ window.location.reload(); }}, 2000);
    }}).catch(function() {{ setTimeout(poll, 2000); }});
  }})();
  return false;
}}
</script>
</body></html>
"""

PAGE_RESTORE = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backup wiederherstellen</title>
<style>""" + STYLE + """</style>
</head><body>
{header}
<h1>♻ {app_emoji} {app_label} wiederherstellen</h1>
<p>Ersetzt <strong>{restored_label}</strong> durch den gewählten Stand.
Der App-Code bleibt unangetastet. {app_label} startet danach automatisch neu
und ist sofort wieder voll funktionsfähig.</p>
{message}
<form method="post" action="/backup/restore/{app_id}"
      onsubmit="return confirmRestore(this.querySelector('select').selectedOptions[0]
                ? this.querySelector('select').selectedOptions[0].text : 'diesem Backup')">
  <label for="backup_select">Vorhandenes Backup auswählen</label>
  <select id="backup_select" name="backup_key">
    {options}
  </select>
  <button type="submit" class="btn-danger">Backup wiederherstellen</button>
</form>

<h2>Backup direkt vom PC wiederherstellen</h2>
<form method="post" action="/backup/restore-upload/{app_id}" enctype="multipart/form-data"
      onsubmit="return confirmRestore('der ausgewählten Datei')">
  <label for="upload_file">Backup-Datei auf diesem PC auswählen (.tar.gz)</label>
  <input type="file" id="upload_file" name="file" accept=".gz,.tar.gz" required>
  <button type="submit" class="btn-danger">Backup vom PC wiederherstellen</button>
</form>

<a class="btn" href="/backup">← Zurück</a>
<script>
function confirmRestore(name) {{
  return confirm('{restored_label} wirklich aus "' + name + '" wiederherstellen? ' +
                 'Alle Änderungen seit diesem Backup gehen dabei verloren.') &&
         confirm('Ganz sicher? Dieser Schritt lässt sich nicht rückgängig machen.');
}}
</script>
</body></html>
"""

PAGE_DOWNLOAD_SELECT = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backup herunterladen</title>
<style>""" + STYLE + """</style>
</head><body>
{header}
<h1>⬇ {app_emoji} {app_label} – Backup herunterladen</h1>
<p>Lädt das gewählte Backup-Archiv auf dieses Gerät herunter.</p>
<form onsubmit="event.preventDefault(); var v = document.getElementById('download_select').value;
                if (v) window.location.href = '/backup/download/{app_id}/' + v.replace('|', '/');">
  <label for="download_select">Vorhandenes Backup auswählen</label>
  <select id="download_select" name="backup_key">
    {options}
  </select>
  <button type="submit">⬇ Backup herunterladen</button>
</form>
<a class="btn" href="/backup">← Zurück</a>
</body></html>
"""

PAGE_UPDATE = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Update</title>
<style>""" + STYLE + """</style>
</head><body>
{header}
<h1>🔄 Update</h1>
{message}
{app_sections}
{all_update_button}
{self_update_card}
<a class="btn" href="/">← Zurück zur Übersicht</a>

<div id="update-modal" class="modal-backdrop">
  <div class="modal-box" id="update-modal-content"></div>
</div>
<script>
function updatePoll(appId, content, modal) {{
  fetch('/update/status/' + appId).then(r => r.json()).then(function(d) {{
    if (!d.done) {{ setTimeout(function() {{ updatePoll(appId, content, modal); }}, 2000); return; }}
    content.innerHTML = d.ok
      ? '<div class="msg ok">✅ ' + d.detail + '</div>'
      : '<div class="msg err">❌ ' + d.detail + '</div>';
    setTimeout(function() {{ window.location.reload(); }}, 2500);
  }}).catch(function() {{ setTimeout(function() {{ updatePoll(appId, content, modal); }}, 2000); }});
}}
function startUpdate(appId, tag) {{
  if (!confirm('Auf Version ' + tag + ' aktualisieren? Vorher wird automatisch ein Backup erstellt.')) {{
    return false;
  }}
  var modal = document.getElementById('update-modal');
  var content = document.getElementById('update-modal-content');
  content.innerHTML = '<h1>""" + SPINNER_SVG + """Aktualisiere…</h1>' +
    '<p class="muted">Backup wird erstellt, neue Version heruntergeladen und installiert. ' +
    'Das kann einige Minuten dauern – bitte die Seite nicht schließen.</p>';
  modal.classList.add('show');
  fetch('/update/run/' + appId, {{method: 'POST'}});
  updatePoll(appId, content, modal);
  return false;
}}
function startVersionSwitch(form, appId) {{
  var tag = form.querySelector('select').value;
  if (!tag) return false;
  if (!confirm('Wirklich auf Version ' + tag + ' wechseln? Vorher wird automatisch ein Backup erstellt.')) {{
    return false;
  }}
  var modal = document.getElementById('update-modal');
  var content = document.getElementById('update-modal-content');
  content.innerHTML = '<h1>""" + SPINNER_SVG + """Wechsle Version…</h1>' +
    '<p class="muted">Backup wird erstellt, gewählte Version heruntergeladen und installiert. ' +
    'Das kann einige Minuten dauern – bitte die Seite nicht schließen.</p>';
  modal.classList.add('show');
  fetch('/update/switch/' + appId, {{method: 'POST', body: new URLSearchParams(new FormData(form))}});
  updatePoll(appId, content, modal);
  return false;
}}
function startUpdateAll() {{
  if (!confirm('Wirklich alle Apps aktualisieren? Vorher wird pro App automatisch ein Backup erstellt.')) {{
    return false;
  }}
  var modal = document.getElementById('update-modal');
  var content = document.getElementById('update-modal-content');
  content.innerHTML = '<h1>""" + SPINNER_SVG + """Aktualisiere alle Apps…</h1>' +
    '<p class="muted">Das kann je nach Anzahl der Apps einige Minuten dauern – bitte die Seite nicht schließen.</p>';
  modal.classList.add('show');
  fetch('/update/run-all', {{method: 'POST'}});
  updatePoll('_all', content, modal);
  return false;
}}
function startInstallRun(appId) {{
  if (!confirm('install.sh jetzt erneut von GitHub laden und ausführen? Sinnvoll nach einem ' +
               'Update, das auch install.sh selbst betrifft (z. B. neue Setup-Funktionen).')) {{
    return false;
  }}
  var modal = document.getElementById('update-modal');
  var content = document.getElementById('update-modal-content');
  content.innerHTML = '<h1>""" + SPINNER_SVG + """install.sh wird ausgeführt…</h1>' +
    '<p class="muted">Lädt die neueste Version von GitHub und führt deren install.sh aus. ' +
    'Das kann einige Minuten dauern – bitte die Seite nicht schließen.</p>';
  modal.classList.add('show');
  fetch('/update/run-install/' + appId, {{method: 'POST'}}).then(r => r.json()).then(function(d) {{
    if (!d.started) {{
      content.innerHTML = '<div class="msg err">❌ ' + (d.error || 'Konnte nicht gestartet werden.') + '</div>';
      return;
    }}
    updatePoll(appId, content, modal);
  }});
  return false;
}}
function updateVersionSwitchButton(select, appId) {{
  var btn = document.getElementById('version-switch-btn-' + appId);
  var isCurrent = select.value === select.dataset.current;
  btn.disabled = isCurrent;
  btn.classList.toggle('btn-danger', !isCurrent);
  btn.style.opacity = isCurrent ? '.5' : '1';
}}
document.querySelectorAll('select[data-app-id]').forEach(function(sel) {{
  updateVersionSwitchButton(sel, sel.dataset.appId);
}});
function selfUpdatePoll(content, modal) {{
  fetch('/update/self-update-check/status').then(r => r.json()).then(function(d) {{
    if (!d.done) {{ setTimeout(function() {{ selfUpdatePoll(content, modal); }}, 2000); return; }}
    content.innerHTML = d.ok
      ? '<div class="msg ok">✅ ' + d.detail + '</div>'
      : '<div class="msg err">❌ ' + (d.detail || 'Fehler.') + '</div>';
    setTimeout(function() {{ window.location.reload(); }}, 2500);
  }}).catch(function() {{ setTimeout(function() {{ selfUpdatePoll(content, modal); }}, 2000); }});
}}
function startSelfUpdateCheck() {{
  if (!confirm('Jetzt auf eine neue Portal-Version prüfen? Falls eine gefunden wird, startet der Dienst kurz neu - diese Seite ist dann kurz nicht erreichbar.')) {{
    return false;
  }}
  var modal = document.getElementById('update-modal');
  var content = document.getElementById('update-modal-content');
  content.innerHTML = '<h1>""" + SPINNER_SVG + """Prüfe auf neue Version…</h1>' +
    '<p class="muted">Das kann bis zu einer Minute dauern.</p>';
  modal.classList.add('show');
  fetch('/update/self-update-check', {{method: 'POST'}}).then(r => r.json()).then(function(d) {{
    if (!d.started) {{
      content.innerHTML = '<div class="msg err">❌ ' + (d.error || 'Konnte nicht gestartet werden.') + '</div>';
      return;
    }}
    selfUpdatePoll(content, modal);
  }}).catch(function() {{
    selfUpdatePoll(content, modal);
  }});
  return false;
}}
</script>
</body></html>
"""

PAGE_SYSTEM_ACTION = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{action}…</title>
<style>""" + STYLE + """</style>
</head><body>
<h1>""" + SPINNER_SVG + """Pi {verb}…</h1>
<p>{hint}</p>
{retry_script}
</body></html>
"""

RETRY_SCRIPT = """
<script>
setTimeout(function poll() {
  fetch('/', {cache: 'no-store'}).then(function(r) {
    if (r.ok) { window.location.href = '/'; } else { setTimeout(poll, 3000); }
  }).catch(function() { setTimeout(poll, 3000); });
}, 15000);
</script>
"""

STOP_SPINNER_SCRIPT = """
<script>
setTimeout(function() {
  var el = document.querySelector('.loading-bee');
  if (el) { el.style.animation = 'none'; el.style.opacity = '.4'; }
}, 20000);
</script>
"""


# --------------------------------------------------------------------------
# App-Registry
# --------------------------------------------------------------------------

# Optionale Felder (nicht in _REQUIRED_TOP_LEVEL_FIELDS, weil ein Descriptor
# ohne sie trotzdem vollstaendig gueltig ist):
#   "donate": {"text", "url", "button_label"} - Spenden-Hinweis auf der Startseite.
#   "companion": {"app_id", "label", "github_repo"} + optional "emoji",
#     "install_script_path" (Default "setup/install.sh"), "beschreibung" -
#     Partner-App, die sich per Button dieser App aus nachinstallieren laesst
#     (siehe render_landing()/_run_companion_install_in_background()).
#   "beschreibung": kurzer Text, was die App macht - erscheint auf /hilfe
#     (siehe render_app_beschreibungen()).
_REQUIRED_TOP_LEVEL_FIELDS = (
    "id", "label", "emoji", "app_port_default", "app_port_env_file", "app_port_env_var",
    "backup", "update",
)
_REQUIRED_BACKUP_FIELDS = ("script", "prefix", "restore_data_prefix", "restore_target_dir")
_REQUIRED_UPDATE_FIELDS = ("github_repo", "version_file", "version_regex")


def _validate_app_descriptor(app, name):
    """Prueft, ob ein Descriptor alle Felder mitbringt, die render_header()/
    render_landing()/render_backup_overview()/render_update_overview() und
    die Backup-/Update-Engine ungeprueft per app[...] auslesen. Nur 'id' zu
    pruefen (wie fruehere Version) reichte nicht - ein Descriptor mit z.B.
    'id' aber ohne 'label' liess den kompletten Server bei JEDER Seite mit
    KeyError abstuerzen, nicht nur bei der betroffenen App. Gibt (True, None)
    oder (False, Fehlertext) zurueck."""
    if not isinstance(app, dict) or not app.get("id"):
        return False, "kein gueltiges 'id'-Feld"
    for field in _REQUIRED_TOP_LEVEL_FIELDS:
        if field not in app:
            return False, f"Feld '{field}' fehlt"
    if not isinstance(app["backup"], dict):
        return False, "'backup' ist kein Objekt"
    for field in _REQUIRED_BACKUP_FIELDS:
        if field not in app["backup"]:
            return False, f"Feld 'backup.{field}' fehlt"
    if not isinstance(app["update"], dict):
        return False, "'update' ist kein Objekt"
    for field in _REQUIRED_UPDATE_FIELDS:
        if field not in app["update"]:
            return False, f"Feld 'update.{field}' fehlt"
    return True, None


def load_apps():
    """Liest alle *.json-Beschreibungen aus APPS_DIR ein (siehe Modul-
    Docstring fuer das erwartete Schema). Kaputte/unvollstaendige Dateien
    werden uebersprungen (nach stderr geloggt) statt den Server abstuerzen
    zu lassen. Kein Cache - immer frisch von der Platte, damit eine frisch
    per install.sh registrierte App ohne Neustart dieses Portals auftaucht."""
    apps = []
    try:
        names = sorted(os.listdir(APPS_DIR))
    except OSError:
        return []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(APPS_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                app = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"setup_portal: apps.d/{name} konnte nicht gelesen werden: {e}", file=sys.stderr)
            continue
        valid, reason = _validate_app_descriptor(app, name)
        if not valid:
            print(f"setup_portal: apps.d/{name} ist unvollstaendig ({reason}) - ignoriert.", file=sys.stderr)
            continue
        apps.append(app)
    apps.sort(key=lambda a: a["id"])
    return apps


def get_app(app_id):
    for app in load_apps():
        if app["id"] == app_id:
            return app
    return None


def _read_env_port(path, var_name, default):
    """Liest einen Port aus einer systemd-EnvironmentFile-artigen Datei
    (KEY=VALUE je Zeile) - fuer Ports, die ein ANDERER Dienst (mit eigenem
    Prozess/Environment) gewaehlt hat, z. B. die eigentliche App."""
    try:
        with open(path) as f:
            content = f.read()
        m = re.search(rf"^{re.escape(var_name)}=(\d+)", content, re.MULTILINE)
        if m:
            return int(m.group(1))
    except OSError:
        pass
    return default


def _host_without_port(host):
    if host.startswith("["):  # IPv6-Literal, z.B. [::1]:8080
        idx = host.rfind("]")
        return host[:idx + 1] if idx != -1 else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def app_url(app, request_host=None):
    port = _read_env_port(app["app_port_env_file"], app["app_port_env_var"], app["app_port_default"])
    host = _host_without_port(request_host) if request_host else f"{socket.gethostname()}.local"
    return f"http://{host}" if port == 80 else f"http://{host}:{port}"


def _apps_heading():
    """(Emoji-Praefix, Titel-Text) fuer Header/Landing-Titel. Nutzerwunsch
    2026-08-10: fest auf "BeeTown-Setup-Portal" statt frueher dynamisch aus
    Anzahl registrierter Apps/IS_PI abgeleitet (z.B. "BeeTown-Pi" vs.
    "BeeTown-Setup" je nach Geraet) - verwirrte mehr, als es half, sobald
    HonigBox und die Imkerei-App (beide unter der Marke "BeeTown") auf
    unterschiedlichen Geraeten liefen. Einzige Konstante hier bewusst NICHT
    ueber apps.d/*.json konfigurierbar (anders als donate/companion/
    beschreibung) - der Nutzer hat nur genau diese beiden BeeTown-Apps,
    ein generischer Mechanismus wuerde hier keinen echten Zweck erfuellen."""
    return "🐝", "BeeTown-Setup-Portal"


def render_header():
    emoji, text = _apps_heading()
    return (f'<div class="header"><span class="logo">{emoji or "🐝"}</span>'
            f'<div class="name">{html.escape(text)}'
            f'<span class="portal-version">Setup-Portal v{PORTAL_VERSION}</span></div></div>')


def render_app_beschreibungen():
    """Kurzbeschreibung jeder installierten App mit optionalem "beschreibung"-
    Feld im Descriptor - rein deklarativ, dieses Skript kennt auch hier keine
    App-Namen. Leerer String, falls keine App eine Beschreibung mitbringt
    (z. B. noch komplett frische Portal-Installation ohne registrierte App)."""
    parts = []
    for app in load_apps():
        beschreibung = app.get("beschreibung")
        if not beschreibung:
            continue
        parts.append(
            '<div class="msg" style="margin-bottom:.8rem;">'
            f'<p><strong>{app.get("emoji", "")} {html.escape(app["label"])}</strong></p>'
            f'<p style="margin-top:.3rem;">{html.escape(beschreibung)}</p>'
            '</div>'
        )
    return "".join(parts)


def render_hilfe():
    return PAGE_HILFE.format(header=render_header(), app_beschreibungen=render_app_beschreibungen())


def _landing_title():
    emoji, text = _apps_heading()
    return f"{emoji} {text}" if emoji else text


def _vpn_image_app_id():
    """Die VPN/Fritzbox-Anleitung ist app-unabhaengig (jede App profitiert
    gleich davon) - Screenshots liegen deshalb unter dem reservierten
    Pseudo-App-Ordner '_shared', nicht unter einer bestimmten App-ID."""
    return "_shared"


# --------------------------------------------------------------------------
# WLAN (komplett app-unabhaengig, unveraendert aus den Vorlagen uebernommen)
# --------------------------------------------------------------------------

def scan_networks():
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL", "device", "wifi", "list", "--rescan", "yes"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    seen, nets = set(), []
    for line in out.splitlines():
        if not line or ":" not in line:
            continue
        ssid, _, signal = line.rpartition(":")
        ssid = ssid.strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        nets.append((ssid, signal))
    nets.sort(key=lambda t: int(t[1] or 0), reverse=True)
    return nets


def current_wifi_connection():
    """(ssid, connected) fuer wlan0 - ssid ist None wenn nicht verbunden."""
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None, False
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == "wlan0":
            connected = parts[1] == "connected"
            ssid = parts[2] if connected and parts[2] != "--" else None
            return ssid, connected
    return None, False


def get_ip(iface):
    try:
        out = subprocess.run(
            ["nmcli", "-g", "IP4.ADDRESS", "device", "show", iface],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out.split("/")[0] if out else None


def all_ips():
    """Alle IPv4-Adressen ueber 'ip' ermitteln - fuer den (seltenen) Fall
    eines Nicht-Pi-Systems, auf dem nmcli das Interface nicht verwaltet."""
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    result = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1] == "lo":
            continue
        result.append((parts[1], parts[3].split("/")[0]))
    return result


def status_banner():
    ssid, connected = current_wifi_connection()
    if connected:
        return f'<div class="msg ok">📶 Aktuell verbunden mit <strong>{ssid}</strong></div>'
    return '<div class="msg err">📡 Kein WLAN verbunden</div>'


def previously_active_connection():
    """Name des aktuell aktiven Verbindungsprofils auf wlan0 (oder None)."""
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in out.splitlines():
        name, _, device = line.partition(":")
        if device == "wlan0":
            return name
    return None


def connect_wifi(ssid, password):
    previous = previously_active_connection()
    if previous == ssid:
        # Bug (2026-08-10): war hier vorher nur "previous = None" (kein
        # Rueckfall-Netz noetig) - das eigentliche "nmcli connection delete"
        # weiter unten lief aber TROTZDEM, und loeschte damit das GERADE
        # AKTIVE Verbindungsprofil. Das trennt die bestehende Verbindung
        # sofort, noch bevor "nmcli device wifi connect" ueberhaupt versucht
        # wird - schlaegt dieser Versuch dann fehl (z.B. weil das Formular
        # kein Passwort mitschickt, man ist ja "schon verbunden" und tippt es
        # nicht erneut ein), gibt es wegen des genullten "previous" auch
        # keinen Rueckfall mehr. Ergebnis: aus dem WLAN geflogen, nur noch per
        # Kabel wieder erreichbar. Fix: bei bereits aktiver Verbindung zum
        # SELBEN Netz gar nichts tun - insbesondere NICHT das Profil loeschen.
        return True, f"Bereits mit '{ssid}' verbunden."

    subprocess.run(["nmcli", "connection", "delete", ssid], capture_output=True, text=True)

    cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", "wlan0"]
    if password:
        cmd += ["password", password]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    ok = result.returncode == 0
    detail = (result.stderr or result.stdout).strip()

    if not ok and previous:
        rollback = subprocess.run(
            ["nmcli", "connection", "up", previous],
            capture_output=True, text=True, timeout=30,
        )
        if rollback.returncode == 0:
            detail += f" (alte Verbindung '{previous}' wiederhergestellt)"
        else:
            detail += f" (Wiederherstellen von '{previous}' ebenfalls fehlgeschlagen!)"

    return ok, detail


def disconnect_wifi():
    name = previously_active_connection()
    if name:
        subprocess.run(["nmcli", "connection", "modify", name, "autoconnect", "no"],
                        capture_output=True, text=True)
    result = subprocess.run(["nmcli", "device", "disconnect", "wlan0"],
                             capture_output=True, text=True, timeout=15)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def render_form(request_host=None, message=""):
    _, connected = current_wifi_connection()
    return PAGE_FORM.format(
        header=render_header(),
        status=status_banner(),
        message=message,
        disconnect_form=DISCONNECT_FORM if connected else "",
    )


# --------------------------------------------------------------------------
# Backup (geteilte Infrastruktur je App-Prefix, USB-Stick global)
# --------------------------------------------------------------------------

def backup_name_re(prefix):
    return re.compile(rf"^{re.escape(prefix)}-[0-9-]+\.tar\.gz$")


def list_backups(directory, prefix):
    name_re = backup_name_re(prefix)
    try:
        names = [f for f in os.listdir(directory) if name_re.match(f)]
    except OSError:
        return []
    names.sort(reverse=True)
    backups = []
    for name in names:
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)
            size_mb = f"{st.st_size / (1024 * 1024):.1f}"
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
        except OSError:
            size_mb, mtime = "?", "?"
        backups.append({"name": name, "size_mb": size_mb, "mtime": mtime})
    return backups


def create_backup_now(app):
    try:
        result = subprocess.run(["bash", app["backup"]["script"]], capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def _restore_from_tar(app, tar, label):
    """Ersetzt genau den in app['backup']['restore_target_dir'] konfigurierten
    Ordner durch den Inhalt des geoeffneten tar-Archivs (App-Code bleibt
    unangetastet), stoppt/startet dabei die konfigurierten Dienste und setzt
    danach entweder einen Besitzer (restore_owner) oder einen Modus
    (restore_chmod) - je nachdem, was die App in ihrem Deskriptor angibt.

    filter="data" bei extractall() ist sicherheitskritisch: ohne das wuerde
    ein Tar-Mitglied mit z.B. "<prefix>/../../../etc/cron.d/x" den Praefix-
    Check bestehen (reiner String-Vergleich) und trotzdem ausserhalb von
    tmpdir landen - dieser Endpunkt nimmt beliebige hochgeladene .tar.gz
    ohne Login an, das laeuft als root."""
    backup_cfg = app["backup"]
    prefix = backup_cfg["restore_data_prefix"]
    target_dir = backup_cfg["restore_target_dir"]
    stop_services = backup_cfg.get("restore_stop_services", [])
    start_services = backup_cfg.get("restore_start_services", [])
    pre_hook = backup_cfg.get("pre_restore_hook")
    post_hook = backup_cfg.get("post_restore_hook")

    def _restart_services():
        for svc in start_services:
            subprocess.run(["systemctl", "start", svc], capture_output=True, text=True)

    def _run_hook(hook_cmd):
        # Generischer Erweiterungspunkt fuer Apps, die vor/nach dem Restore
        # noch etwas Eigenes erledigen muessen - z.B. HonigBox: falls
        # restore_target_dir einen aktiven tmpfs-Mount (RAM-Disk) enthaelt,
        # wuerde das rmtree() weiter unten versehentlich dessen Inhalt
        # loeschen UND den Mount hinterher verwaist zuruecklassen. Der Hook
        # schaltet dafuer vorher auf Platte um und danach wieder zurueck.
        # hook_cmd ist ein simpler, von der App selbst vorgegebener Text
        # (kein Nutzereingabe-Pfad) - shlex.split() reicht daher aus.
        if not hook_cmd:
            return
        try:
            subprocess.run(shlex.split(hook_cmd), capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass  # Hook-Fehler duerfen den eigentlichen Restore nicht verhindern

    for svc in stop_services:
        subprocess.run(["systemctl", "stop", svc], capture_output=True, text=True)
    _run_hook(pre_hook)
    try:
        members = [m for m in tar.getmembers()
                   if m.name == prefix or m.name.startswith(prefix + "/")]
        if not members:
            _run_hook(post_hook)
            _restart_services()
            return False, f"'{prefix}' nicht im Archiv gefunden."
        with tempfile.TemporaryDirectory() as tmpdir:
            tar.extractall(path=tmpdir, members=members, filter="data")
            extracted_dir = os.path.join(tmpdir, prefix)
            # Vorherigen Ordner erst zur Seite umbenennen statt sofort zu
            # loeschen (rename ist atomar, gleiches Dateisystem) - schlaegt
            # der anschliessende move() fehl (z.B. ENOSPC bei einem
            # Cross-Filesystem-Fallback auf Kopieren), sind die alten Daten
            # noch da und werden zurueckgeholt, statt komplett verloren zu
            # gehen.
            aside_dir = None
            if os.path.isdir(target_dir):
                aside_dir = target_dir.rstrip("/") + ".vor-wiederherstellung"
                shutil.rmtree(aside_dir, ignore_errors=True)
                os.rename(target_dir, aside_dir)
            try:
                shutil.move(extracted_dir, target_dir)
            except OSError:
                if aside_dir is not None:
                    shutil.rmtree(target_dir, ignore_errors=True)
                    os.rename(aside_dir, target_dir)
                raise
            if aside_dir is not None:
                shutil.rmtree(aside_dir, ignore_errors=True)
        if backup_cfg.get("restore_chmod"):
            subprocess.run(["chmod", "-R", backup_cfg["restore_chmod"], target_dir],
                            capture_output=True, text=True)
        elif backup_cfg.get("restore_owner"):
            subprocess.run(["chown", "-R", backup_cfg["restore_owner"], target_dir],
                            capture_output=True, text=True)
    except (OSError, tarfile.TarError) as e:
        _run_hook(post_hook)
        _restart_services()
        return False, f"Fehler bei der Wiederherstellung: {e}"
    _run_hook(post_hook)
    _restart_services()
    restored_label = backup_cfg.get("restored_label", "Daten")
    return True, f"{restored_label} aus '{label}' wiederhergestellt – {app['label']} läuft wieder."


def restore_backup(app, location, filename):
    prefix = app["backup"]["prefix"]
    if not filename or not backup_name_re(prefix).match(filename):
        return False, "Ungültiger Dateiname."
    directory = USB_MOUNT if location == "usb" else BACKUP_DIR
    path = os.path.join(directory, filename)
    if not os.path.isfile(path):
        return False, "Backup nicht gefunden."
    try:
        with tarfile.open(path, "r:gz") as tar:
            return _restore_from_tar(app, tar, filename)
    except (tarfile.TarError, OSError) as e:
        return False, f"Fehler beim Lesen des Archivs: {e}"


def restore_backup_from_bytes(app, data, filename):
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            return _restore_from_tar(app, tar, filename or "der hochgeladenen Datei")
    except (tarfile.TarError, OSError) as e:
        return False, f"Fehler beim Lesen der hochgeladenen Datei: {e}"


def parse_multipart_file(body, content_type):
    """Sehr einfacher multipart/form-data-Parser fuer genau EIN Datei-Feld
    (keine externen Abhaengigkeiten, Python-Standardbibliothek reicht).
    Gibt (dateiname, bytes) oder (None, None) zurueck."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        return None, None
    boundary = ("--" + m.group(1)).encode()
    for part in body.split(boundary):
        if b"Content-Disposition" not in part:
            continue
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        headers = part[:header_end].decode("utf-8", "replace")
        data = part[header_end + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        fm = re.search(r'filename="([^"]*)"', headers)
        if fm and fm.group(1):
            return fm.group(1), data
    return None, None


def get_max_backups():
    try:
        with open(BACKUP_CONFIG_PATH) as f:
            content = f.read()
        m = re.search(r"^MAX_BACKUPS=(\d+)", content, re.MULTILINE)
        if m:
            return int(m.group(1))
    except OSError:
        pass
    return DEFAULT_MAX_BACKUPS


def set_backup_settings(max_backups_raw):
    try:
        max_backups = int(max_backups_raw)
    except (TypeError, ValueError):
        return False, "Ungültige Anzahl."
    if not (MIN_MAX_BACKUPS <= max_backups <= 200):
        return False, f"Anzahl muss zwischen {MIN_MAX_BACKUPS} und 200 liegen."
    try:
        os.makedirs(os.path.dirname(BACKUP_CONFIG_PATH), exist_ok=True)
        with open(BACKUP_CONFIG_PATH, "w") as f:
            f.write(f"MAX_BACKUPS={max_backups}\n")
    except OSError as e:
        return False, str(e)
    return True, f"Aufbewahrung gespeichert (max. {max_backups} Backups je Ort, Vater-Sohn-Rotation)."


def get_root_disk():
    """Name (z. B. 'mmcblk0') der Festplatte, von der das System bootet."""
    try:
        src = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "/"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
        pkname = subprocess.run(["lsblk", "-no", "PKNAME", src],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        return pkname or re.sub(r"p?\d+$", "", src.replace("/dev/", ""))
    except (subprocess.TimeoutExpired, OSError):
        return None


def list_usb_disks():
    """Per USB angeschlossene Festplatten, OHNE die System-Platte - reine
    Sicherheitsmassnahme, damit diese niemals formatierbar angeboten wird.
    Faellt bewusst geschlossen aus: kann get_root_disk() die System-Platte
    nicht sicher bestimmen (z.B. weil findmnt/lsblk gerade haengen oder
    unerwartet leer antworten), wird lieber GAR KEIN USB-Stick angeboten,
    statt die laufende System-Platte faelschlich als formatierbar zu
    listen (z.B. bei einem von USB-SSD bootenden Pi 4/5)."""
    root_disk = get_root_disk()
    if not root_disk:
        return []
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TRAN,SIZE,FSTYPE,LABEL,MOUNTPOINT,TYPE"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        data = json.loads(out)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return []
    disks = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk" or dev.get("tran") != "usb":
            continue
        if dev.get("name") == root_disk:
            continue
        mountpoints = [dev.get("mountpoint")] + [c.get("mountpoint") for c in dev.get("children", []) or []]
        labels = [dev.get("label")] + [c.get("label") for c in dev.get("children", []) or []]
        disks.append({
            "name": dev["name"],
            "size": dev.get("size") or "?",
            "fstype": dev.get("fstype") or "unformatiert",
            "is_target": USB_MOUNT in mountpoints,
            "is_known_backup_stick": "BACKUP" in [l for l in labels if l],
        })
    return disks


def _register_fstab_and_mount(device):
    """Traegt das Dateisystem von `device` (per UUID) in /etc/fstab fuer
    USB_MOUNT ein und haengt es ein."""
    try:
        uuid = subprocess.run(["blkid", "-s", "UUID", "-o", "value", device],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"UUID konnte nicht ermittelt werden: {e}"
    if not uuid:
        return False, "UUID des Dateisystems konnte nicht ermittelt werden."

    try:
        os.makedirs(USB_MOUNT, exist_ok=True)
    except OSError as e:
        return False, f"{USB_MOUNT} konnte nicht angelegt werden: {e}"
    try:
        try:
            with open("/etc/fstab") as f:
                lines = [l for l in f if USB_MOUNT not in l]
        except FileNotFoundError:
            lines = []
        lines.append(f"UUID={uuid} {USB_MOUNT} ext4 defaults,nofail,x-systemd.device-timeout=5 0 2\n")
        with open("/etc/fstab", "w") as f:
            f.writelines(lines)
    except OSError as e:
        return False, f"/etc/fstab konnte nicht aktualisiert werden: {e}"

    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True)
    mount_result = subprocess.run(["mount", USB_MOUNT], capture_output=True, text=True, timeout=30)
    if mount_result.returncode != 0:
        return False, f"Einhängen fehlgeschlagen: {(mount_result.stderr or mount_result.stdout).strip()}"
    return True, None


def format_and_setup_usb(device_name):
    if not re.match(r"^[a-z][a-z0-9]*$", device_name or ""):
        return False, "Ungültiger Gerätename."
    root_disk = get_root_disk()
    if device_name == root_disk:
        return False, "Sicherheitsstopp: das ist die System-Festplatte - wird nicht formatiert."
    if device_name not in {d["name"] for d in list_usb_disks()}:
        return False, "Gerät ist kein erkannter USB-Stick."
    device = f"/dev/{device_name}"

    subprocess.run(["umount", USB_MOUNT], capture_output=True, text=True)
    subprocess.run(["umount", device], capture_output=True, text=True)
    for i in range(1, 5):
        subprocess.run(["umount", f"{device}{i}"], capture_output=True, text=True)

    result = subprocess.run(["mkfs.ext4", "-F", "-L", "BACKUP", device],
                             capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        return False, f"Formatieren fehlgeschlagen: {(result.stderr or result.stdout).strip()}"

    ok, err = _register_fstab_and_mount(device)
    if not ok:
        return False, f"Formatiert, aber {err}"
    return True, f"USB-Stick formatiert und als zusätzliches Backup-Ziel eingerichtet ({USB_MOUNT})."


def mount_existing_usb(device_name):
    """Bindet einen USB-Stick ein, der bereits frueher als Backup-Ziel
    formatiert wurde - OHNE ihn neu zu formatieren."""
    if not re.match(r"^[a-z][a-z0-9]*$", device_name or ""):
        return False, "Ungültiger Gerätename."
    root_disk = get_root_disk()
    if device_name == root_disk:
        return False, "Sicherheitsstopp: das ist die System-Festplatte."
    if device_name not in {d["name"] for d in list_usb_disks()}:
        return False, "Gerät ist kein erkannter USB-Stick."
    device = f"/dev/{device_name}"

    ok, err = _register_fstab_and_mount(device)
    if not ok:
        return False, err
    return True, f"Vorhandener USB-Stick eingebunden ({USB_MOUNT}) – bestehende Backups sind erhalten."


def eject_usb():
    result = subprocess.run(["umount", USB_MOUNT], capture_output=True, text=True, timeout=15)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def try_remount_usb():
    """Bestversuch: haengt einen bereits eingerichteten USB-Stick automatisch
    wieder ein, falls er zwischenzeitlich ab- und wieder angesteckt wurde."""
    if os.path.ismount(USB_MOUNT):
        return
    subprocess.run(["mount", USB_MOUNT], capture_output=True, text=True, timeout=15)


def _run_format_in_background(device_name):
    # Sicherheitsnetz: irgendeine unerwartete Ausnahme hier wuerde den
    # Hintergrund-Thread stillschweigend beenden, OHNE FORMAT_STATE auf
    # done=True zu setzen - das "Formatiere..."-Overlay im Browser wuerde
    # dann fuer immer weiterpollen, ohne jemals ein Ergebnis zu zeigen.
    try:
        ok, detail = format_and_setup_usb(device_name)
    except Exception as e:
        ok, detail = False, f"Unerwarteter Fehler: {e}"
    FORMAT_STATE.update(done=True, ok=ok, detail=detail)


def disk_usage(path):
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        return f"{free_gb:.1f} GB frei von {total_gb:.1f} GB"
    except OSError:
        return "?"


def _usb_section_html():
    usb_mounted = os.path.ismount(USB_MOUNT)
    usb_disks = list_usb_disks()
    parts = []
    if usb_mounted:
        parts.append(
            f'<div class="msg ok">📦 USB-Stick eingerichtet ({disk_usage(USB_MOUNT)}) – '
            f'Backups werden {LOCAL_BACKUP_PHRASE} und zusätzlich hier abgelegt.</div>'
        )
        parts.append(
            '<form method="post" action="/backup/usb/eject" '
            'onsubmit="return confirm(\'USB-Stick sicher aushängen? Danach kann er entfernt werden.\');">'
            '<button type="submit">⏏ USB-Stick sicher entfernen</button></form>'
        )
    elif usb_disks:
        for d in usb_disks:
            if d.get("is_known_backup_stick"):
                parts.append(f"""
<div class="msg ok">
  Bekannter Backup-Stick gefunden: <strong>/dev/{d['name']}</strong> ({d['size']}) –
  aktuell nicht eingebunden (z. B. nach einer Neuinstallation dieses Pi).
  Vorhandene Backups bleiben beim Einbinden erhalten.
  <form method="post" action="/backup/usb/mount">
    <input type="hidden" name="device" value="{d['name']}">
    <button type="submit">📌 Vorhandenen Stick einbinden</button>
  </form>
</div>""")
                continue
            warn = (f"USB-Stick /dev/{d['name']} ({d['size']}, {d['fstype']}) wirklich formatieren? "
                    f"ALLE Daten darauf gehen unwiderruflich verloren!")
            parts.append(f"""
<div class="msg err">
  USB-Stick gefunden: <strong>/dev/{d['name']}</strong> ({d['size']}, {d['fstype']}) –
  noch nicht als Backup-Ziel eingerichtet.
  <form onsubmit="return startFormat(this, '{warn}')">
    <input type="hidden" name="device" value="{d['name']}">
    <button type="submit" class="btn-danger">⚙ Formatieren &amp; als Backup-Ziel einrichten</button>
  </form>
</div>""")
    elif IS_PI:
        parts.append(
            '<div class="msg err">⚠️ <strong>Kein USB-Stick angeschlossen.</strong> '
            'Backups liegen nur auf der SD-Karte – bei einem Ausfall der SD-Karte sind dann '
            '<strong>alle</strong> Daten unwiderruflich verloren. Einen Stick anschließen und '
            'diese Seite neu laden, um ihn einzurichten.</div>'
        )
    else:
        parts.append('<p class="muted">Kein USB-Stick angeschlossen – optional, nicht erforderlich.</p>')
    return "".join(parts)


def render_backup_overview(message="", skip_remount=False):
    if not skip_remount:
        try_remount_usb()
    apps = load_apps()

    if not apps:
        app_sections = '<p class="muted">Keine Anwendung registriert.</p>'
        all_backup_button = ""
    else:
        sections = []
        for app in apps:
            prefix = app["backup"]["prefix"]
            entries = [(b, LOCAL_BACKUP_LABEL) for b in list_backups(BACKUP_DIR, prefix)]
            if os.path.ismount(USB_MOUNT):
                entries += [(b, "USB-Stick") for b in list_backups(USB_MOUNT, prefix)]
            entries.sort(key=lambda e: e[0]["name"], reverse=True)
            if entries:
                b0, loc0 = entries[0]
                latest_info = f'<p class="muted" style="font-size:.85rem;">Letztes Backup: {b0["name"]} ({b0["mtime"]}, {loc0})</p>'
            else:
                latest_info = '<p class="muted" style="font-size:.85rem;">Noch keine Backups vorhanden.</p>'
            sections.append(f"""
<div class="msg app-section">
<h2>{app['emoji']} {html.escape(app['label'])}</h2>
{latest_info}
<form method="post" action="/backup/create/{app['id']}">
  <button type="submit">📦 Jetzt sichern</button>
</form>
<a class="btn" href="/backup/restore/{app['id']}">♻ Wiederherstellen</a>
<a class="btn" href="/backup/downloads/{app['id']}">⬇ Herunterladen</a>
</div>""")
        app_sections = "".join(sections)
        if len(apps) > 1:
            emojis = "".join(a["emoji"] for a in apps)
            all_backup_button = f"""
<form method="post" action="/backup/create-all">
  <button type="submit">{emojis} Alle sichern</button>
</form>"""
        else:
            all_backup_button = ""

    return PAGE_BACKUP.format(
        header=render_header(),
        message=message,
        app_sections=app_sections,
        all_backup_button=all_backup_button,
        usb_section=_usb_section_html(),
        max_backups=get_max_backups(),
    )


def backup_options_html(app):
    try_remount_usb()
    prefix = app["backup"]["prefix"]
    entries = [(b, "local", LOCAL_BACKUP_LABEL) for b in list_backups(BACKUP_DIR, prefix)]
    if os.path.ismount(USB_MOUNT):
        entries += [(b, "usb", "USB-Stick") for b in list_backups(USB_MOUNT, prefix)]
    entries.sort(key=lambda e: e[0]["name"], reverse=True)
    if not entries:
        return '<option value="">– keine Backups vorhanden –</option>'
    return "".join(
        f'<option value="{loc}|{b["name"]}">{b["name"]} ({b["mtime"]}, {label})</option>'
        for b, loc, label in entries)


def render_restore_page(app, message=""):
    return PAGE_RESTORE.format(
        header=render_header(),
        message=message,
        options=backup_options_html(app),
        app_id=app["id"],
        app_emoji=app["emoji"],
        app_label=html.escape(app["label"]),
        restored_label=app["backup"].get("restored_label", "Daten"),
    )


def render_download_select_page(app):
    return PAGE_DOWNLOAD_SELECT.format(
        header=render_header(),
        options=backup_options_html(app),
        app_id=app["id"],
        app_emoji=app["emoji"],
        app_label=html.escape(app["label"]),
    )


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------

def _state_dir(app_id):
    return os.path.join(STATE_DIR, app_id)


def _update_check_state_path(app_id):
    return os.path.join(_state_dir(app_id), "update_check.json")


def _auto_update_config_path(app_id):
    return os.path.join(_state_dir(app_id), "update.conf")


def app_version(app):
    try:
        with open(app["update"]["version_file"]) as f:
            content = f.read()
        m = re.search(app["update"]["version_regex"], content)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "?"


def parse_version(v):
    """'v1.2.3' -> (1, 2, 3), fuer korrekten numerischen Vergleich (nicht
    alphabetisch - sonst waere z. B. 'v1.10' < 'v1.9')."""
    parts = []
    for p in (v or "").lstrip("vV").split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


def _fetch_latest_release_for_repo(repo):
    """Kern von fetch_latest_release() - auf den blanken Repo-String
    ('Nutzer/Repo') statt auf ein volles App-Objekt bezogen, damit auch das
    Nachinstallieren einer noch unregistrierten Partner-App (die ja noch
    kein app['update']-Objekt hat) dieselbe Abfrage nutzen kann."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Pi-Setup-Update-Check"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = data.get("tag_name") or ""
        if not tag:
            return None
        return {"tag": tag, "notes": (data.get("body") or "").strip(), "tarball_url": data.get("tarball_url") or ""}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


SELF_UPDATE_GITHUB_REPO = "Chrischn73/setup-portal"
SELF_UPDATE_FILES = ("setup_portal.py", "setup-portal.sh", "regen-issue.sh")
# Ergebnis des letzten Selbst-Update-Checks (egal ob vom taeglichen Timer
# oder manuell per "Jetzt prüfen"-Button ausgeloest) - EINE gemeinsame
# Datei, kein App-Bezug. Schreibender Prozess ist immer die --self-update-
# CLI-Instanz (eigener, kurzlebiger Prozess), gelesen wird sie vom
# laufenden Webserver fuer die Live-Anzeige/Polling in der UI.
SELF_UPDATE_CHECK_STATE_PATH = f"{STATE_DIR}/self_update_check.json"


def _write_self_update_state(done, ok=None, detail=None):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(SELF_UPDATE_CHECK_STATE_PATH, "w") as f:
            json.dump({"done": done, "ok": ok, "detail": detail}, f)
    except OSError:
        pass  # Anzeige bleibt dann einfach beim vorherigen Stand bzw. "laeuft" haengen


def read_self_update_check_state():
    try:
        with open(SELF_UPDATE_CHECK_STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"done": True, "ok": None, "detail": None}


def _self_update():
    """Aktualisiert das Portal selbst auf die neueste GitHub-Release-Version,
    OHNE ueber die generische App-Update-Engine (perform_update()) zu gehen -
    bewusst ein eigener, einfacherer Pfad, weil hier Update-Werkzeug und
    Update-Ziel dieselbe Datei sind. Wird vom taeglichen Timer ODER manuell
    per "Jetzt prüfen"-Button (siehe trigger_self_update_check()) aufgerufen
    - in BEIDEN Faellen als eigener, kurzlebiger --self-update-Prozess,
    NIEMALS als Thread in der laufenden Webserver-Instanz - ein
    "systemctl restart" des eigenen Dienstes, ausgeloest aus einem
    Anfrage-Thread DIESES Dienstes heraus, waere fragil (der Thread, der
    darauf wartet, wird beim Neustart mit beendet). Jeder Ausgang schreibt
    das Ergebnis nach SELF_UPDATE_CHECK_STATE_PATH, damit die Web-UI es
    per Polling anzeigen kann (der Button selbst loest nur den separaten
    systemd-Dienst aus, siehe trigger_self_update_check())."""
    print(f"Setup-Portal-Update-Check (aktuell: v{PORTAL_VERSION})...", file=sys.stderr)
    release = _fetch_latest_release_for_repo(SELF_UPDATE_GITHUB_REPO)
    if not release or not release.get("tarball_url"):
        print("Konnte keine Release-Information abrufen.", file=sys.stderr)
        _write_self_update_state(True, False, "Konnte keine Release-Information von GitHub abrufen.")
        return
    if parse_version(release["tag"]) <= parse_version(PORTAL_VERSION):
        print(f"Bereits aktuell (neueste Version: {release['tag']}).", file=sys.stderr)
        _write_self_update_state(True, True, f"Du hast bereits die neueste Version ({release['tag']}).")
        return
    print(f"Neuere Version gefunden: {release['tag']} - lade herunter...", file=sys.stderr)
    try:
        req = urllib.request.Request(release["tarball_url"], headers={"User-Agent": "Setup-Portal-Self-Update"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            archive_data = resp.read()
        with tempfile.TemporaryDirectory() as tmpdir:
            with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as tar:
                tar.extractall(path=tmpdir, filter="data")
            entries = os.listdir(tmpdir)
            if len(entries) != 1:
                print("Unerwarteter Archivinhalt (GitHub-Tarball-Struktur hat sich geaendert).", file=sys.stderr)
                _write_self_update_state(True, False, "Unerwarteter Archivinhalt (GitHub-Tarball-Struktur hat sich geändert).")
                return
            src_root = os.path.join(tmpdir, entries[0])
            for name in SELF_UPDATE_FILES:
                src = os.path.join(src_root, name)
                if os.path.isfile(src):
                    shutil.copy(src, os.path.join(PORTAL_DIR, name))
            os.chmod(os.path.join(PORTAL_DIR, "setup-portal.sh"), 0o755)
            os.chmod(os.path.join(PORTAL_DIR, "regen-issue.sh"), 0o755)
            # Die .service-Datei separat und NICHT-fatal behandeln: sie
            # aendert sich viel seltener als der Python-Code, und ein
            # Fehler hier (z. B. /etc kurzzeitig nicht schreibbar) soll
            # nicht dazu fuehren, dass der bereits erfolgreich kopierte
            # neue Code nie durch einen Neustart aktiv wird - siehe
            # naechster Schritt, der IMMER versucht wird.
            service_src = os.path.join(src_root, "setup-portal.service")
            if os.path.isfile(service_src):
                try:
                    shutil.copy(service_src, "/etc/systemd/system/setup-portal.service")
                    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True)
                except OSError as e:
                    print(f"WARNUNG: .service-Datei konnte nicht aktualisiert werden: {e}", file=sys.stderr)
    except (urllib.error.URLError, OSError, tarfile.TarError) as e:
        print(f"Update fehlgeschlagen: {e}", file=sys.stderr)
        _write_self_update_state(True, False, f"Update fehlgeschlagen: {e}")
        return
    print(f"Update auf {release['tag']} installiert - starte Dienst neu...", file=sys.stderr)
    # Bewusst VOR dem Neustart schreiben: der Neustart beendet diesen
    # Prozess ggf. mit, eine Schreibaktion danach waere nicht mehr sicher.
    _write_self_update_state(True, True, f"Auf {release['tag']} aktualisiert - Dienst wird neu gestartet.")
    subprocess.run(["systemctl", "restart", "setup-portal.service"], capture_output=True, text=True)


def trigger_self_update_check():
    """Stoesst den taeglichen Selbst-Update-Check des Portals manuell sofort
    an, fuer den "Jetzt auf neue Version pruefen"-Button auf /update. Laeuft
    ueber den eigenen, unabhaengigen systemd-Dienst (systemctl start),
    NICHT als Thread im laufenden Webserver-Prozess - _self_update() kann
    "systemctl restart setup-portal.service" ausloesen, und das aus einem
    Anfrage-Thread DESSELBEN Dienstes heraus zu tun waere fragil (der
    Thread stirbt mit dem Neustart, siehe Docstring dort). "--no-block"
    laesst systemctl sofort zurueckkehren, statt auf den Abschluss des
    Oneshot-Dienstes zu warten - das tatsaechliche Ergebnis (auch bei
    einem zwischenzeitlichen Dienst-Neustart) liest die UI per Polling
    aus SELF_UPDATE_CHECK_STATE_PATH (siehe read_self_update_check_state()),
    NICHT aus der Antwort dieser Funktion. Gibt (True, None) oder
    (False, Fehlertext) zurueck - Fehler hier bedeuten "Start selbst hat
    nicht geklappt", nicht "keine neue Version gefunden"."""
    _write_self_update_state(False)  # "laeuft" - erst der --self-update-Prozess selbst setzt done=True
    try:
        result = subprocess.run(
            ["systemctl", "start", "--no-block", "setup-portal-update-check.service"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        _write_self_update_state(True, False, f"Konnte die Prüfung nicht starten: {e}")
        return False, f"Konnte die Prüfung nicht starten: {e}"
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "Unbekannter Fehler").strip()
        _write_self_update_state(True, False, error)
        return False, error
    return True, None


def fetch_latest_release(app):
    return _fetch_latest_release_for_repo(app["update"]["github_repo"])


def fetch_all_releases(app, limit=10):
    repo = app["update"]["github_repo"]
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases?per_page={limit}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Pi-Setup-Update-Check"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [{"tag": r["tag_name"], "tarball_url": r.get("tarball_url") or "",
                  "notes": (r.get("body") or "").strip(), "published_at": r.get("published_at") or ""}
                for r in data if r.get("tag_name")]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError, KeyError):
        return []


def fetch_release_by_tag(app, tag):
    repo = app["update"]["github_repo"]
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/tags/{quote(tag, safe='')}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Pi-Setup-Update-Check"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag_name = data.get("tag_name") or ""
        if not tag_name:
            return None
        return {"tag": tag_name, "notes": (data.get("body") or "").strip(), "tarball_url": data.get("tarball_url") or ""}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def get_auto_update(app_id):
    """Standard AN, falls die Konfigurationsdatei fehlt oder unlesbar ist."""
    try:
        with open(_auto_update_config_path(app_id)) as f:
            content = f.read()
        m = re.search(r"^AUTO_UPDATE=(\d)", content, re.MULTILINE)
        if m:
            return m.group(1) == "1"
    except OSError:
        pass
    return True


def set_auto_update(app_id, enabled):
    try:
        path = _auto_update_config_path(app_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"AUTO_UPDATE={1 if enabled else 0}\n")
        return True, "Einstellung gespeichert."
    except OSError as e:
        return False, str(e)


def read_update_check_state(app):
    try:
        with open(_update_check_state_path(app["id"])) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"current": app_version(app), "latest": None, "update_available": False, "checked_at": None}


def run_update_check_once(app):
    """Einmaliger Versions-Check fuer EINE App, Ergebnis wird zwischen-
    gespeichert (per Timer regelmaessig aufgerufen, siehe --check-update)."""
    app_id = app["id"]
    current = app_version(app)
    release = fetch_latest_release(app)
    update_available = bool(release) and parse_version(release["tag"]) > parse_version(current)
    auto_updated_version = read_update_check_state(app).get("auto_updated_version")
    if update_available and get_auto_update(app_id) and release.get("tarball_url"):
        ok, detail = perform_update(app, release["tarball_url"], release["tag"])
        _update_state(app_id).update(done=True, ok=ok, detail=detail)
        if ok:
            current = app_version(app)
            update_available = False
            auto_updated_version = current
    state = {
        "current": current,
        "latest": release["tag"] if release else None,
        "update_available": update_available,
        "checked_at": time.strftime("%Y-%m-%d %H:%M"),
        "notes": (release.get("notes") if release else None),
        "auto_updated_version": auto_updated_version,
    }
    try:
        path = _update_check_state_path(app_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


def perform_update(app, tarball_url, target_tag):
    """Legt zuerst ein Backup an, laedt dann den Source-Tarball des GitHub-
    Release herunter und kopiert die in app['update']['file_map']
    beschriebenen Dateien/Ordner an ihren Zielort. Aktualisiert bewusst NICHT
    dieses Setup-Portal selbst (das war frueher in beiden Vorlagen-Projekten
    der Fall, siehe SETUP_PORTAL_FILE_MAP dort - hier entfaellt das: das
    Portal wird unabhaengig ueber install.sh der jeweiligen App versioniert).
    Gibt (True, Meldung) oder (False, Fehlertext) zurueck."""
    ok, detail = create_backup_now(app)
    if not ok:
        return False, f"Backup vor dem Update fehlgeschlagen - Update abgebrochen: {detail}"
    try:
        req = urllib.request.Request(tarball_url, headers={"User-Agent": "Pi-Setup-Update"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            archive_data = resp.read()
    except (urllib.error.URLError, OSError) as e:
        return False, f"Herunterladen fehlgeschlagen: {e}"

    services = app["update"].get("services_to_restart", [])

    def _restart_services():
        for svc in services:
            subprocess.run(["systemctl", "start", svc], capture_output=True, text=True)

    for svc in services:
        subprocess.run(["systemctl", "stop", svc], capture_output=True, text=True)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as tar:
                tar.extractall(path=tmpdir, filter="data")
            entries = os.listdir(tmpdir)
            if len(entries) != 1:
                _restart_services()
                return False, "Unerwarteter Archivinhalt (GitHub-Tarball-Struktur hat sich geaendert)."
            src_root = os.path.join(tmpdir, entries[0])

            any_unit_changed = False
            for entry in app["update"].get("file_map", []):
                src = os.path.join(src_root, entry["src"])
                dest = entry["dest"]
                if entry.get("mode") == "dir":
                    if not os.path.isdir(src):
                        continue
                    if os.path.isdir(dest):
                        shutil.rmtree(dest)
                    shutil.copytree(src, dest)
                else:
                    if not os.path.isfile(src):
                        continue
                    shutil.copy(src, dest)
                    os.chmod(dest, int(entry.get("mode", "0644"), 8))
                if dest.startswith("/etc/systemd/system/"):
                    any_unit_changed = True
                if entry.get("chown"):
                    subprocess.run(["chown", "-R", entry["chown"], dest], capture_output=True, text=True)

            if any_unit_changed:
                subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True)
    except (tarfile.TarError, OSError) as e:
        _restart_services()
        return False, f"Fehler beim Aktualisieren: {e}"

    _restart_services()
    return True, f"Auf Version {target_tag} aktualisiert - {app['label']} läuft wieder."


def _run_update_in_background(app):
    # Sicherheitsnetz wie bei _run_format_in_background: ohne dieses
    # try/except wuerde eine unerwartete Ausnahme (z.B. in fetch_latest_
    # release()) den Thread beenden, ohne done=True zu setzen - das
    # "Aktualisiere..."-Overlay wuerde dann fuer immer weiterpollen.
    app_id = app["id"]
    try:
        release = fetch_latest_release(app)
        if not release or not release.get("tarball_url"):
            _update_state(app_id).update(done=True, ok=False, detail="Neueste Version konnte nicht ermittelt werden.")
            return
        ok, detail = perform_update(app, release["tarball_url"], release["tag"])
        _update_state(app_id).update(done=True, ok=ok, detail=detail)
        run_update_check_once(app)
    except Exception as e:
        _update_state(app_id).update(done=True, ok=False, detail=f"Unerwarteter Fehler: {e}")


def _run_version_switch_in_background(app, tag):
    """Installiert gezielt eine bestimmte Version (z. B. Rueckwechsel auf
    eine aeltere bei Problemen mit der neuesten). Schaltet automatische
    Updates ab, falls es sich dabei um einen echten Rueckschritt handelt."""
    app_id = app["id"]
    try:
        previous_version = app_version(app)
        release = fetch_release_by_tag(app, tag)
        if not release or not release.get("tarball_url"):
            _update_state(app_id).update(done=True, ok=False, detail=f"Version '{tag}' konnte nicht gefunden werden.")
            return
        ok, detail = perform_update(app, release["tarball_url"], release["tag"])
        if ok and parse_version(release["tag"]) < parse_version(previous_version):
            set_auto_update(app_id, False)
            detail += (" Automatische Updates wurden dabei ausgeschaltet, damit der Pi nicht "
                       "gleich wieder auf die neuere Version zurueckaktualisiert.")
        _update_state(app_id).update(done=True, ok=ok, detail=detail)
        run_update_check_once(app)
    except Exception as e:
        _update_state(app_id).update(done=True, ok=False, detail=f"Unerwarteter Fehler: {e}")


def _run_update_all_in_background():
    try:
        apps = load_apps()
        results = []
        overall_ok = True
        for app in apps:
            release = fetch_latest_release(app)
            if not release or not release.get("tarball_url"):
                overall_ok = False
                results.append(f"{app['label']}: neueste Version konnte nicht ermittelt werden.")
                continue
            ok, detail = perform_update(app, release["tarball_url"], release["tag"])
            run_update_check_once(app)
            overall_ok = overall_ok and ok
            results.append(f"{app['label']}: {detail}")
        UPDATE_STATE["_all"] = {"done": True, "ok": overall_ok, "detail": " / ".join(results) or "Keine Anwendung registriert."}
    except Exception as e:
        UPDATE_STATE["_all"] = {"done": True, "ok": False, "detail": f"Unerwarteter Fehler: {e}"}


def _download_and_run_install_script(github_repo, install_script_path, label):
    """Laedt das neueste GitHub-Release herunter und fuehrt darin das
    angegebene install.sh aus - bewusst KEIN Nachbau der Installationslogik
    hier (die steckt bereits vollstaendig, getestet und gepflegt in
    install.sh selbst: apt-Pakete, systemd-Dienste, Hostname, Kamera/GPIO,
    apps.d-Descriptor usw.). Dieses Skript hier laeuft laut
    setup-portal.service bereits als root, ein "sudo" vor dem bash-Aufruf
    ist deshalb nicht noetig - install.sh prueft selbst per 'id -u', dass es
    als root laeuft. Gemeinsamer Kern fuer ZWEI Faelle: eine Partner-App per
    "companion"-Feld nachinstallieren (siehe _run_companion_install_in_
    background) UND das install.sh einer bereits installierten App erneut
    ausfuehren (siehe _run_install_script_in_background) - z. B. weil eine
    Aenderung NUR install.sh selbst betrifft und deshalb nie per normalem,
    file_map-basiertem Update (perform_update()) uebernommen wird. Gibt
    (ok, detail) zurueck, wirft KEINE Exception weiter (Aufrufer muss trotzdem
    subprocess.TimeoutExpired/Exception selbst abfangen - die Ausnahmen aus
    urllib/tarfile hier drin sind bewusst NICHT gefangen, damit der Aufrufer
    seinen jeweils eigenen State-Speicher im except-Block aktualisieren kann)."""
    release = _fetch_latest_release_for_repo(github_repo)
    if not release or not release.get("tarball_url"):
        return False, "Neueste Version konnte nicht ermittelt werden."
    req = urllib.request.Request(release["tarball_url"], headers={"User-Agent": "Pi-Setup-Install-Run"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        archive_data = resp.read()
    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as tar:
            tar.extractall(path=tmpdir, filter="data")
        entries = os.listdir(tmpdir)
        if len(entries) != 1:
            return False, "Unerwarteter Archivinhalt (GitHub-Tarball-Struktur hat sich geaendert)."
        src_root = os.path.join(tmpdir, entries[0])
        script = os.path.join(src_root, install_script_path)
        if not os.path.isfile(script):
            return False, f"Installationsskript nicht gefunden ({install_script_path})."
        # Kann je nach App mehrere Minuten dauern (apt-get, Kamera-Setup
        # usw.) - grosszuegiges Timeout, damit ein echter Haenger trotzdem
        # irgendwann als Fehler zurueckkommt statt den Thread fuer immer zu
        # blockieren.
        result = subprocess.run(["bash", script], cwd=src_root, capture_output=True, text=True, timeout=1800)
        if result.returncode == 0:
            return True, f"{label}: install.sh wurde erfolgreich ausgefuehrt."
        fehlerausgabe = (result.stderr or result.stdout or "").strip()[-800:]
        return False, f"install.sh fehlgeschlagen (Exit-Code {result.returncode}): {fehlerausgabe}"


def _run_companion_install_in_background(companion):
    app_id = companion["app_id"]
    try:
        ok, detail = _download_and_run_install_script(
            companion["github_repo"], companion.get("install_script_path", "setup/install.sh"), companion["label"])
        _companion_install_state(app_id).update(done=True, ok=ok, detail=detail)
    except subprocess.TimeoutExpired:
        _companion_install_state(app_id).update(
            done=True, ok=False, detail="Installation hat zu lange gedauert (Timeout).")
    except Exception as e:
        _companion_install_state(app_id).update(done=True, ok=False, detail=f"Unerwarteter Fehler: {e}")


def _run_install_script_in_background(app):
    """Fuehrt install.sh der App SELBST erneut aus (die App ist hier -
    anders als bei _run_companion_install_in_background - schon registriert).
    Fuer Aenderungen, die ausschliesslich install.sh selbst betreffen und
    deshalb nie per normalem Update (perform_update(), kopiert nur die
    Dateien aus 'update.file_map') uebernommen werden: Boot-Bildschirm
    (/etc/issue), apps.d/<id>.json-Descriptor-Felder, der gemeinsame
    Portal-Code selbst (siehe Warnhinweis in perform_update()). Nutzt
    bewusst UPDATE_STATE/_update_state() statt eines eigenen State-Speichers -
    anders als bei einer noch unregistrierten Partner-App blockt die GET
    /update/status/<id>-Route hier nichts, die App ist ja schon in apps.d/
    vorhanden."""
    app_id = app["id"]
    try:
        ok, detail = _download_and_run_install_script(
            app["update"]["github_repo"], app.get("install_script_path", "setup/install.sh"), app["label"])
        _update_state(app_id).update(done=True, ok=ok, detail=detail)
    except subprocess.TimeoutExpired:
        _update_state(app_id).update(done=True, ok=False, detail="Ausführung hat zu lange gedauert (Timeout).")
    except Exception as e:
        _update_state(app_id).update(done=True, ok=False, detail=f"Unerwarteter Fehler: {e}")


def render_update_card(app, message=""):
    app_id = app["id"]
    current = app_version(app)
    release = fetch_latest_release(app)
    if release is None:
        latest = "konnte nicht abgerufen werden"
        status_class = "err"
        notes_block = ""
        action_block = '<p class="muted">Prüfe, ob der Pi Internetzugang hat, und lade die Seite neu.</p>'
    else:
        latest = release["tag"]
        update_available = parse_version(latest) > parse_version(current)
        status_class = "err" if update_available else "ok"
        notes_block = (f'<div class="msg" style="white-space:pre-wrap;">{html.escape(release["notes"])}</div>'
                       if update_available and release["notes"] else "")
        if update_available:
            action_block = (
                f'<form onsubmit="return startUpdate(\'{app_id}\', \'{latest}\')">'
                f'<button type="submit" class="btn-danger">⬇ Auf {latest} aktualisieren</button>'
                f'</form>'
            )
        else:
            action_block = '<p class="muted">Du hast bereits die neueste Version.</p>'

    all_releases = fetch_all_releases(app)
    if all_releases:
        version_options = "".join(
            f'<option value="{html.escape(r["tag"])}" {"selected" if r["tag"] == current else ""}>'
            f'{html.escape(r["tag"])}{" (installiert)" if r["tag"] == current else ""}</option>'
            for r in all_releases
        )
    else:
        version_options = '<option value="">– keine Releases abrufbar –</option>'

    changelog_items = "".join(
        f'<div style="margin-bottom:.9rem">'
        f'<strong>{html.escape(r["tag"])}</strong>'
        + (f' <span class="muted" style="font-size:.8rem">· {html.escape(r["published_at"][:10])}</span>' if r.get("published_at") else "")
        + f'<div class="muted" style="white-space:pre-wrap;font-size:.85rem;margin-top:.2rem">{html.escape(r["notes"])}</div>'
        f'</div>'
        for r in all_releases if r.get("notes")
    )
    changelog_block = (
        f'<details style="margin-top:1rem">'
        f'<summary class="muted" style="cursor:pointer">Änderungsverlauf früherer Versionen</summary>'
        f'<div style="margin-top:.8rem">{changelog_items}</div>'
        f'</details>'
    ) if changelog_items else ""

    return f"""
<div class="msg app-section">
<h2>{app['emoji']} {html.escape(app['label'])}</h2>
{message}
<div class="msg {status_class}">
<strong>Installierte Version:</strong> {current}<br>
<strong>Neueste Version:</strong> {latest}
</div>
{notes_block}
{action_block}

<p class="muted" style="font-size:.85rem; margin-top:1rem;">Andere Version installieren (automatische Updates
werden dabei ausgeschaltet, falls es ein Rueckschritt ist):</p>
<form onsubmit="return startVersionSwitch(this, '{app_id}')">
  <select name="tag" data-app-id="{app_id}" data-current="{current}" onchange="updateVersionSwitchButton(this, '{app_id}')">
    {version_options}
  </select>
  <button type="submit" class="btn-danger" id="version-switch-btn-{app_id}">Version installieren</button>
</form>

<form method="post" action="/update/settings/{app_id}" style="margin-top:1rem;">
  <label style="display:flex; align-items:center; gap:.5rem; font-weight:normal;">
    <input type="checkbox" name="auto_update" value="1" {"checked" if get_auto_update(app_id) else ""} style="width:auto; margin:0;">
    Automatisch aktualisieren, sobald eine neue Version verfügbar ist
  </label>
  <button type="submit" class="btn-small">Einstellung speichern</button>
</form>

<p class="muted" style="font-size:.85rem; margin-top:1rem;">Ein normales Update kopiert nur die
App-eigenen Dateien - Änderungen an <code>install.sh</code> selbst (z. B. neue Setup-Funktionen,
Descriptor-Änderungen) werden dabei NICHT übernommen. Falls nötig, hier ohne SSH nachholen:</p>
<button type="button" class="btn-small" onclick="return startInstallRun('{app_id}')">🔄 Komplett von GitHub aktualisieren</button>
{changelog_block}
</div>"""


def render_self_update_card():
    """Zeigt zusaetzlich das Ergebnis des letzten Checks an (egal ob vom
    taeglichen Timer oder manuell ausgeloest) - liest denselben Zustand,
    den auch das Live-Polling nach einem Klick abfragt, siehe
    read_self_update_check_state()."""
    state = read_self_update_check_state()
    status_line = ""
    if state.get("done") and state.get("detail"):
        cls = "ok" if state.get("ok") else "err"
        icon = "✅" if state.get("ok") else "❌"
        status_line = f'<div class="msg {cls}" style="font-size:.85rem;">{icon} {html.escape(state["detail"])}</div>'
    return f"""
<div class="msg app-section">
<h2>🔧 Setup-Portal-Update</h2>
<p class="muted">Version {PORTAL_VERSION}. Aktualisiert sich taeglich automatisch (02:30 Uhr) direkt aus den
GitHub-Releases von <code>{html.escape(SELF_UPDATE_GITHUB_REPO)}</code> - unabhaengig von den Apps oben.</p>
{status_line}
<button type="button" class="btn-small" onclick="return startSelfUpdateCheck()">🔄 Jetzt auf neue Version prüfen</button>
</div>"""


def render_update_overview(message=""):
    apps = load_apps()
    if not apps:
        app_sections = '<p class="muted">Keine Anwendung registriert.</p>'
        all_update_button = ""
    else:
        app_sections = "".join(render_update_card(app) for app in apps)
        if len(apps) > 1:
            emojis = "".join(a["emoji"] for a in apps)
            all_update_button = f'<form onsubmit="return startUpdateAll()"><button type="submit">{emojis} Alle aktualisieren</button></form>'
        else:
            all_update_button = ""
    return PAGE_UPDATE.format(
        header=render_header(),
        message=message,
        app_sections=app_sections,
        all_update_button=all_update_button,
        self_update_card=render_self_update_card(),
    )


# --------------------------------------------------------------------------
# Landing-Page
# --------------------------------------------------------------------------

def render_landing(request_host=None):
    apps = load_apps()

    if not apps:
        app_cards = '<p class="muted" style="text-align:center;">Keine Anwendung registriert.</p>'
        donate_section = ""
    else:
        parts = []
        donate_parts = []
        for app in apps:
            parts.append(f'<a class="btn btn-open" href="{app_url(app, request_host)}">{app["emoji"]} '
                         f'{html.escape(app["label"])} öffnen</a>')
            donate = app.get("donate")
            if donate:
                # Bewusst NICHT direkt hier bei den Oeffnen-Buttons, sondern
                # gesammelt ganz unten auf der Seite (siehe donate_section in
                # PAGE_LANDING) - sonst haette die Position/Reihenfolge der
                # Seite je nachdem, welche App gerade registriert ist bzw.
                # ob sie ueberhaupt ein "donate"-Feld deklariert, unterschied-
                # lich ausgesehen (Nutzer-Verwirrung 2026-08-10: "wieso
                # unterscheiden sich die Portale").
                donate_parts.append(
                    '<div class="donate-box">'
                    f'<p>{html.escape(donate["text"])}</p>'
                    f'<a class="btn" href="{donate["url"]}" target="_blank" rel="noopener">'
                    f'{html.escape(donate["button_label"])}</a>'
                    '</div>'
                )
        app_cards = "".join(parts)
        donate_section = "".join(donate_parts)

    # Fuer jede installierte App mit "companion"-Feld, deren Partner-App
    # NOCH NICHT registriert ist (kein eigener apps.d/-Eintrag) - Button
    # startet den Download+install.sh-Lauf, siehe _run_companion_install_
    # in_background(). Sind beide Apps schon installiert, deklariert also
    # z.B. sowohl HonigBox als auch BeeTown den anderen als companion,
    # verschwinden hier automatisch beide Karten.
    installed_ids = {a["id"] for a in apps}
    companion_parts = []
    for app in apps:
        comp = app.get("companion")
        if not comp or comp["app_id"] in installed_ids:
            continue
        beschreibung_block = (
            f'<p class="muted" style="font-size:.85rem; margin:.4rem 0 0;">{html.escape(comp["beschreibung"])}</p>'
            if comp.get("beschreibung") else ""
        )
        companion_parts.append(
            '<div class="msg" style="text-align:center;">'
            f'<p>{comp.get("emoji", "⬇️")} <strong>{html.escape(comp["label"])}</strong> '
            'ist auf diesem Pi noch nicht installiert.</p>'
            f'{beschreibung_block}'
            f'<button class="btn btn-small" style="margin-top:.6rem;" onclick="return startCompanionInstall(\'{app["id"]}\', '
            f'\'{comp["app_id"]}\', \'{comp["label"]}\')">⚙️ {html.escape(comp["label"])} installieren</button>'
            '</div>'
        )
    companion_section = "".join(companion_parts)

    update_banner_parts = []
    for app in apps:
        state = read_update_check_state(app)
        if state.get("update_available"):
            update_banner_parts.append(
                f'<div class="msg ok">🔄 Update verfügbar für {html.escape(app["label"])}: '
                f'Version {state["latest"]}</div>'
            )
    update_banner = "".join(update_banner_parts)

    if IS_PI:
        ip_lines = (f'Kabel (eth0): {get_ip("eth0") or "nicht verbunden"}<br>'
                    f'WLAN (wlan0): {get_ip("wlan0") or "nicht verbunden"}')
    else:
        ips = all_ips()
        ip_lines = "<br>".join(f"{iface}: {addr}" for iface, addr in ips) if ips else "nicht verbunden"

    title = _landing_title()
    return PAGE_LANDING.format(
        title=title,
        header=render_header(),
        status=status_banner() if IS_PI else "",
        update_banner=update_banner,
        app_cards=app_cards,
        companion_section=companion_section,
        wifi_link='<a class="btn" href="/wifi">📶 WLAN-Einstellungen</a>\n' if IS_PI else "",
        ip_lines=ip_lines,
        system_buttons=SYSTEM_BUTTONS if IS_PI else "",
        donate_section=donate_section,
    )


def _delayed_system_call(cmd):
    time.sleep(1.5)
    subprocess.run(cmd)


# --------------------------------------------------------------------------
# HTTP-Handler
# --------------------------------------------------------------------------

class BaseHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_html(self, body, status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self):
        self.send_response(404)
        self.end_headers()

    def serve_hilfe_image(self, app_id, filename):
        """Screenshots fuer die VPN-Hilfeseite (von der jeweiligen App selbst
        oder als Fritzbox-Anleitung unter '_shared' abgelegt) - optional,
        fehlende Dateien werden im <img onerror> auf der Seite ausgeblendet."""
        if not re.match(r"^[A-Za-z0-9_-]+$", app_id or "") or not HILFE_IMAGE_NAME_RE.match(filename):
            self.send_response(400)
            self.end_headers()
            return
        try:
            with open(os.path.join(HILFE_IMAGES_DIR, app_id, filename), "rb") as f:
                data = f.read()
        except OSError:
            self._not_found()
            return
        ext = filename.rsplit(".", 1)[-1].lower()
        content_type = "image/png" if ext == "png" else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _serve_backup_download(self, app, location, filename):
        if location not in ("local", "usb") or not backup_name_re(app["backup"]["prefix"]).match(filename):
            self.send_response(400)
            self.end_headers()
            return
        directory = USB_MOUNT if location == "usb" else BACKUP_DIR
        path = os.path.join(directory, filename)
        if not os.path.isfile(path):
            self._not_found()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def handle_system_action(self):
        """True, wenn der Pfad eine System-Aktion war (Reboot/Shutdown).
        Nur auf einem echten Pi erreichbar."""
        if self.path not in ("/system/reboot", "/system/shutdown"):
            return False
        if not IS_PI:
            self._not_found()
            return True
        if self.path == "/system/reboot":
            self._send_html(PAGE_SYSTEM_ACTION.format(
                action="Neustart", verb="startet neu",
                hint="Diese Seite versucht in Kürze automatisch, sich neu zu verbinden, "
                     "und lädt sich dann selbst neu.",
                retry_script=RETRY_SCRIPT,
            ))
            threading.Thread(target=_delayed_system_call, args=(["systemctl", "reboot"],), daemon=True).start()
            return True
        if self.path == "/system/shutdown":
            self._send_html(PAGE_SYSTEM_ACTION.format(
                action="Herunterfahren", verb="fährt herunter",
                hint="Der Pi muss danach manuell wieder eingeschaltet werden "
                     "(Strom trennen/verbinden).",
                retry_script=STOP_SPINNER_SCRIPT,
            ))
            threading.Thread(target=_delayed_system_call, args=(["systemctl", "poweroff"],), daemon=True).start()
            return True
        return False

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/tipps":
            self._send_html(PAGE_TIPPS.format(header=render_header()))
            return
        if path == "/hilfe":
            self._send_html(render_hilfe())
            return
        if path == "/hilfe/vpn":
            self._send_html(PAGE_VPN.format(
                header=render_header(),
                vpn_img_prefix=f"/hilfe/bilder/{_vpn_image_app_id()}",
            ))
            return
        m = re.match(r"^/hilfe/bilder/([^/]+)/([^/]+)$", path)
        if m:
            self.serve_hilfe_image(unquote(m.group(1)), unquote(m.group(2)))
            return
        if path == "/backup":
            self._send_html(render_backup_overview())
            return
        m = re.match(r"^/backup/restore/([^/]+)$", path)
        if m:
            app = get_app(unquote(m.group(1)))
            if not app:
                self._not_found()
                return
            self._send_html(render_restore_page(app))
            return
        m = re.match(r"^/backup/downloads/([^/]+)$", path)
        if m:
            app = get_app(unquote(m.group(1)))
            if not app:
                self._not_found()
                return
            self._send_html(render_download_select_page(app))
            return
        if path == "/backup/usb/format-status":
            self._send_json(FORMAT_STATE)
            return
        if path == "/update":
            self._send_html(render_update_overview())
            return
        if path == "/update/self-update-check/status":
            self._send_json(read_self_update_check_state())
            return
        m = re.match(r"^/update/status/([^/]+)$", path)
        if m:
            app_id = unquote(m.group(1))
            # _update_state() legt bei unbekannten IDs per setdefault() einen
            # neuen Eintrag an - ohne diese Pruefung koennte ein Scan ueber
            # beliebige Pfade UPDATE_STATE unbegrenzt wachsen lassen (Prozess-
            # Lebensdauer, kein Neustart). "_all" ist der einzige erlaubte
            # Pseudo-Schluessel neben echten App-IDs.
            if app_id != "_all" and not get_app(app_id):
                self._send_json({"done": True, "ok": None, "detail": None})
                return
            self._send_json(_update_state(app_id))
            return
        m = re.match(r"^/companion/install/status/([^/]+)$", path)
        if m:
            comp_id = unquote(m.group(1))
            if comp_id not in _known_companion_ids():
                self._send_json({"done": True, "ok": None, "detail": None})
                return
            self._send_json(_companion_install_state(comp_id))
            return
        if path in ("/wifi", "/wifi/status", "/wifi/networks") and not IS_PI:
            self._not_found()
            return
        if path == "/wifi":
            self._send_html(render_form(self.headers.get("Host")))
            return
        if path == "/wifi/status":
            self._send_json(CONN_STATE)
            return
        if path == "/wifi/networks":
            self._send_json([{"ssid": s, "signal": signal} for s, signal in scan_networks()])
            return
        m = re.match(r"^/backup/download/([^/]+)/(local|usb)/([^/]+)$", path)
        if m:
            app = get_app(unquote(m.group(1)))
            if not app:
                self._not_found()
                return
            self._serve_backup_download(app, m.group(2), unquote(m.group(3)))
            return
        self._send_html(render_landing(self.headers.get("Host")))

    def do_POST(self):
        if self.handle_system_action():
            return
        path = self.path.split("?", 1)[0]

        m = re.match(r"^/backup/create/([^/]+)$", path)
        if m:
            app = get_app(unquote(m.group(1)))
            if not app:
                self._not_found()
                return
            ok, detail = create_backup_now(app)
            msg = ('<div class="msg ok">✅ Backup erstellt.</div>' if ok
                   else f'<div class="msg err">Fehler: {detail}</div>')
            self._send_html(render_backup_overview(msg))
            return
        if path == "/backup/create-all":
            apps = load_apps()
            if not apps:
                self._send_html(render_backup_overview('<div class="msg err">Keine Anwendung registriert.</div>'))
                return
            results = []
            overall_ok = True
            for app in apps:
                ok, detail = create_backup_now(app)
                overall_ok = overall_ok and ok
                results.append(f"{app['label']}: {'✅' if ok else '❌'} {detail}")
            msg_class = "ok" if overall_ok else "err"
            self._send_html(render_backup_overview(f'<div class="msg {msg_class}">' + "<br>".join(results) + '</div>'))
            return
        m = re.match(r"^/backup/restore/([^/]+)$", path)
        if m:
            app = get_app(unquote(m.group(1)))
            if not app:
                self._not_found()
                return
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            backup_key = fields.get("backup_key", [""])[0]
            location, _, filename = backup_key.partition("|")
            if not filename:
                self._send_html(render_restore_page(app, '<div class="msg err">Bitte ein Backup auswählen.</div>'))
                return
            ok, detail = restore_backup(app, location, filename)
            msg = (f'<div class="msg ok">✅ {detail}</div>' if ok else f'<div class="msg err">{detail}</div>')
            self._send_html(render_restore_page(app, msg))
            return
        m = re.match(r"^/backup/restore-upload/([^/]+)$", path)
        if m:
            app = get_app(unquote(m.group(1)))
            if not app:
                self._not_found()
                return
            content_type = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            filename, data = parse_multipart_file(body, content_type)
            if not filename or not data:
                self._send_html(render_restore_page(
                    app, '<div class="msg err">Keine Datei hochgeladen oder Datei nicht lesbar.</div>'))
                return
            ok, detail = restore_backup_from_bytes(app, data, filename)
            msg = (f'<div class="msg ok">✅ {detail}</div>' if ok else f'<div class="msg err">{detail}</div>')
            self._send_html(render_restore_page(app, msg))
            return
        if path == "/backup/settings":
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            max_backups = fields.get("max_backups", [""])[0]
            ok, detail = set_backup_settings(max_backups)
            msg = (f'<div class="msg ok">✅ {detail}</div>' if ok else f'<div class="msg err">{detail}</div>')
            self._send_html(render_backup_overview(msg))
            return
        if path == "/backup/usb/format":
            if FORMAT_STATE.get("done") is False:
                self._send_json({"started": False, "error": "Es laeuft bereits ein Formatier-Vorgang."})
                return
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            device = fields.get("device", [""])[0]
            FORMAT_STATE.update(done=False, ok=None, detail=None)
            threading.Thread(target=_run_format_in_background, args=(device,), daemon=True).start()
            self._send_json({"started": True})
            return
        if path == "/backup/usb/mount":
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            device = fields.get("device", [""])[0]
            ok, detail = mount_existing_usb(device)
            msg = (f'<div class="msg ok">✅ {detail}</div>' if ok else f'<div class="msg err">{detail}</div>')
            self._send_html(render_backup_overview(msg))
            return
        if path == "/backup/usb/eject":
            ok, detail = eject_usb()
            msg = ('<div class="msg ok">✅ USB-Stick sicher entfernt.</div>' if ok
                   else f'<div class="msg err">Aushängen fehlgeschlagen: {detail}</div>')
            self._send_html(render_backup_overview(msg, skip_remount=ok))
            return

        m = re.match(r"^/update/run/([^/]+)$", path)
        if m:
            app = get_app(unquote(m.group(1)))
            if not app:
                self._not_found()
                return
            if _update_state(app["id"]).get("done") is False:
                self._send_json({"started": False, "error": "Fuer diese App laeuft bereits ein Update."})
                return
            _update_state(app["id"]).update(done=False, ok=None, detail=None)
            threading.Thread(target=_run_update_in_background, args=(app,), daemon=True).start()
            self._send_json({"started": True})
            return
        m = re.match(r"^/update/run-install/([^/]+)$", path)
        if m:
            app = get_app(unquote(m.group(1)))
            if not app:
                self._not_found()
                return
            if _update_state(app["id"]).get("done") is False:
                self._send_json({"started": False, "error": "Fuer diese App laeuft bereits ein Vorgang."})
                return
            _update_state(app["id"]).update(done=False, ok=None, detail=None)
            threading.Thread(target=_run_install_script_in_background, args=(app,), daemon=True).start()
            self._send_json({"started": True})
            return
        m = re.match(r"^/companion/install/([^/]+)$", path)
        if m:
            host_app = get_app(unquote(m.group(1)))
            companion = host_app.get("companion") if host_app else None
            if not companion:
                self._not_found()
                return
            comp_id = companion["app_id"]
            if get_app(comp_id):
                self._send_json({"started": False, "error": f"{companion['label']} ist bereits installiert."})
                return
            if _companion_install_state(comp_id).get("done") is False:
                self._send_json({"started": False, "error": "Installation läuft bereits."})
                return
            _companion_install_state(comp_id).update(done=False, ok=None, detail=None)
            threading.Thread(target=_run_companion_install_in_background, args=(companion,), daemon=True).start()
            self._send_json({"started": True})
            return
        if path == "/update/run-all":
            if UPDATE_STATE.get("_all", {}).get("done", True) is False:
                self._send_json({"started": False, "error": "Es laeuft bereits ein Sammel-Update."})
                return
            UPDATE_STATE["_all"] = {"done": False, "ok": None, "detail": None}
            threading.Thread(target=_run_update_all_in_background, daemon=True).start()
            self._send_json({"started": True})
            return
        m = re.match(r"^/update/switch/([^/]+)$", path)
        if m:
            app = get_app(unquote(m.group(1)))
            if not app:
                self._not_found()
                return
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            tag = fields.get("tag", [""])[0]
            if not tag:
                self._send_json({"started": False, "error": "Keine Version ausgewählt."})
                return
            if _update_state(app["id"]).get("done") is False:
                self._send_json({"started": False, "error": "Fuer diese App laeuft bereits ein Update."})
                return
            _update_state(app["id"]).update(done=False, ok=None, detail=None)
            threading.Thread(target=_run_version_switch_in_background, args=(app, tag), daemon=True).start()
            self._send_json({"started": True})
            return
        m = re.match(r"^/update/settings/([^/]+)$", path)
        if m:
            app = get_app(unquote(m.group(1)))
            if not app:
                self._not_found()
                return
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            enabled = fields.get("auto_update", [""])[0] == "1"
            ok, detail = set_auto_update(app["id"], enabled)
            msg = (f'<div class="msg ok">✅ {detail}</div>' if ok else f'<div class="msg err">{detail}</div>')
            self._send_html(render_update_overview(msg))
            return
        if path == "/update/self-update-check":
            started, error = trigger_self_update_check()
            if not started:
                self._send_json({"started": False, "error": error})
                return
            self._send_json({"started": True})
            return

        if path in ("/wifi/connect", "/wifi/disconnect") and not IS_PI:
            self._not_found()
            return
        if path == "/wifi/disconnect":
            ok, detail = disconnect_wifi()
            if ok:
                self._send_html(render_form(self.headers.get("Host"), '<div class="msg ok">🔌 WLAN getrennt.</div>'))
            else:
                self._send_html(render_form(
                    self.headers.get("Host"), f'<div class="msg err">Trennen fehlgeschlagen: {detail}</div>'))
            return
        if path == "/wifi/connect":
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            ssid = (fields.get("ssid_manual", [""])[0] or fields.get("ssid", [""])[0]).strip()
            password = fields.get("password", [""])[0]
            if not ssid:
                self._send_html(render_form(
                    self.headers.get("Host"), '<div class="msg err">Bitte eine SSID auswaehlen oder eingeben.</div>'))
                return
            CONN_STATE.update(done=False, ok=None, detail=None)
            self._send_html(PAGE_CONNECTING.format(ssid=ssid))
            ok, detail = connect_wifi(ssid, password)
            CONN_STATE.update(done=True, ok=ok, detail=None if ok else detail)
            if ok:
                print(f"WLAN-Verbindung zu '{ssid}' erfolgreich.", file=sys.stderr)
            else:
                print(f"WLAN-Verbindung zu '{ssid}' fehlgeschlagen: {detail}", file=sys.stderr)
            return
        self._not_found()


def main():
    server = ThreadingHTTPServer((HOST, PORT_LANDING), BaseHandler)
    print(f"Pi-Setup-Seite (v{PORTAL_VERSION}) laeuft dauerhaft auf {HOST}:{PORT_LANDING}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    if "--check-update" in sys.argv:
        idx = sys.argv.index("--check-update")
        app_id = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else None
        if not app_id:
            print("Nutzung: setup_portal.py --check-update <app-id>", file=sys.stderr)
            sys.exit(1)
        app = get_app(app_id)
        if not app:
            print(f"Keine registrierte App mit ID '{app_id}' gefunden (apps.d/{app_id}.json fehlt?).", file=sys.stderr)
            sys.exit(1)
        run_update_check_once(app)
    elif "--self-update" in sys.argv:
        _self_update()
    else:
        main()

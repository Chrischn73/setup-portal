#!/bin/bash
# Installiert/aktualisiert das gemeinsame Setup-Portal (WLAN, Backup,
# Update, Hilfe fuer beliebig viele registrierte Apps) auf Port 80.
#
# Wird NORMALERWEISE nicht von Hand aufgerufen, sondern automatisch als
# Bootstrap-Schritt aus dem install.sh von HonigBox bzw. der Imker-App
# (BeeTown) heraus, falls das Portal auf einem frischen Pi noch nicht
# existiert - siehe deren jeweiliges install.sh (laedt dafuer automatisch
# das neueste Release dieses Repos herunter und fuehrt dieses Skript hier
# aus). Danach aktualisiert sich das Portal SELBST taeglich per
# setup-portal-update-check.timer, unabhaengig von jedem weiteren
# install.sh-Lauf einer der beiden Apps.
#
# Manueller Aufruf (z. B. zum Reparieren einer kaputten Installation):
#   sudo bash install.sh
#
# Mehrfach ausfuehrbar (idempotent).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte mit sudo ausfuehren: sudo bash $0"
    exit 1
fi

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo; echo "==> $*"; }

# ---------------------------------------------------------------------------
log "Pruefe benoetigte Dateien in $SETUP_DIR"
for f in setup_portal.py setup-portal.sh setup-portal.service regen-issue.sh \
         setup-portal-update-check.service setup-portal-update-check.timer; do
    if [ ! -e "$SETUP_DIR/$f" ]; then
        echo "FEHLER: $SETUP_DIR/$f fehlt. Wurde der komplette Ordner uebertragen?"
        exit 1
    fi
done

# ---------------------------------------------------------------------------
log "Setup-Portal-Verzeichnis einrichten (/opt/setup-portal)"
mkdir -p /opt/setup-portal/apps.d /opt/setup-portal/issue.d \
         /opt/setup-portal/state /opt/setup-portal/hilfe-bilder/_shared

cp "$SETUP_DIR/setup_portal.py" /opt/setup-portal/setup_portal.py
cp "$SETUP_DIR/setup-portal.sh" /opt/setup-portal/setup-portal.sh
cp "$SETUP_DIR/regen-issue.sh" /opt/setup-portal/regen-issue.sh
chmod +x /opt/setup-portal/setup-portal.sh /opt/setup-portal/regen-issue.sh

cp "$SETUP_DIR/setup-portal.service" /etc/systemd/system/setup-portal.service
cp "$SETUP_DIR/setup-portal-update-check.service" /etc/systemd/system/setup-portal-update-check.service
cp "$SETUP_DIR/setup-portal-update-check.timer" /etc/systemd/system/setup-portal-update-check.timer

# ---------------------------------------------------------------------------
log "Pruefe Port 80"
# Koennte von einem bereits laufenden Webserver (z. B. Linux-Server-Fall)
# belegt sein - dann auf einen Ausweich-Port wechseln, statt den
# Dienststart fehlschlagen zu lassen. Ist der aktuelle Belegungsinhaber das
# Portal selbst (erneuter Lauf), zaehlt das nicht als Konflikt.
SETUP_PID="$(systemctl show -p MainPID --value setup-portal.service 2>/dev/null || echo 0)"
PORT80_PID="$(ss -H -ltnp "sport = :80" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
if [ -z "$PORT80_PID" ] || { [ "$PORT80_PID" = "$SETUP_PID" ] && [ "$SETUP_PID" != "0" ]; }; then
    LANDING_PORT=80
    echo "Port 80 ist frei (oder bereits durch das Portal selbst belegt) - Portal laeuft dort."
    rm -f /etc/default/setup-portal
else
    LANDING_PORT=8082
    echo "Port 80 ist von einem anderen Prozess belegt (PID $PORT80_PID) - Portal laeuft stattdessen auf Port $LANDING_PORT."
    echo "SETUP_PORTAL_LANDING_PORT=$LANDING_PORT" > /etc/default/setup-portal
fi

# ---------------------------------------------------------------------------
log "systemd-Dienste aktivieren"
systemctl daemon-reload
systemctl enable --now setup-portal.service
# Explizit neu starten, damit ein aktualisiertes/reparierstes Skript auch
# bei einem erneuten Lauf hier tatsaechlich uebernommen wird - "enable --now"
# allein wuerde einen bereits laufenden Dienst unveraendert weiterlaufen
# lassen. Anders als bei den beiden App-install.sh-Skripten ist das hier
# unproblematisch: dieses Skript wird nur aufgerufen, wenn tatsaechlich das
# Portal (neu) eingerichtet werden soll, nie "nebenbei" durch eine
# App-Installation.
systemctl restart setup-portal.service
systemctl enable --now setup-portal-update-check.timer
# Einmaligen ersten Check gleich jetzt anstossen, damit nicht bis zum ersten
# Timer-Lauf ohne Update-Information dasteht.
systemctl start setup-portal-update-check.service || true

echo
echo "======================================================================"
if [ "$LANDING_PORT" -eq 80 ]; then
    echo " Setup-Portal laeuft auf http://$(hostname).local"
else
    echo " Setup-Portal laeuft auf http://$(hostname).local:$LANDING_PORT"
fi
echo " Aktualisiert sich ab jetzt taeglich automatisch selbst (GitHub-Release-"
echo " Check um 02:30 Uhr) - kein erneuter install.sh-Lauf mehr dafuer noetig."
echo "======================================================================"

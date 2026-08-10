#!/bin/bash
# Teil des Repos "setup-portal" (siehe Warnhinweis in setup_portal.py).
#
# Baut /etc/issue komplett neu auf: fester, generischer Kopf (IP-Adressen
# per agetty-Escape, sichtbar sobald ein Monitor am Pi haengt) plus alle
# *.txt-Fragmente aus /opt/setup-portal/issue.d/ (sortiert nach
# Dateiname). Jedes Fragment enthaelt bereits den fertig formatierten
# Block einer einzelnen registrierten App (URL usw.) - geschrieben von
# deren eigenem install.sh, z. B. als
# /opt/setup-portal/issue.d/10-honigbox.txt.
#
# Zustandslos: baut /etc/issue bei jedem Lauf komplett aus den aktuell
# vorhandenen Fragmenten neu auf, unabhaengig vom bisherigen Inhalt -
# gefahrlos jederzeit erneut ausfuehrbar (z. B. nach dem Registrieren
# oder Entfernen einer App). Fehlt der issue.d-Ordner oder ist er leer,
# wird trotzdem ein minimaler, generischer Kopf geschrieben (kein Absturz,
# kein leeres /etc/issue).
set -u

ISSUE_DIR="/opt/setup-portal/issue.d"
OUT="/etc/issue"

{
    printf '\n'
    printf ' \360\237\220\235\360\237\215\257 Pi-Setup\n'
    printf ' ======================================================\n'
    printf '   IP-Adressen:  Kabel \\4{eth0}   WLAN \\4{wlan0}\n'
    printf ' ======================================================\n'
    printf '\n'

    if [ -d "$ISSUE_DIR" ]; then
        shopt -s nullglob
        for f in "$ISSUE_DIR"/*.txt; do
            [ -f "$f" ] || continue
            cat "$f"
        done
        shopt -u nullglob
    fi
} > "$OUT"

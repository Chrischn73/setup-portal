# Setup-Portal

Gemeinsame, App-unabhängige Setup-Seite für Raspberry-Pi-Projekte: WLAN-Einrichtung, tägliches Backup (inkl. USB-Stick), Selbst-Update über GitHub-Releases, Hilfe-Seiten (Handy-Tipps, VPN per Fritzbox+WireGuard). Läuft dauerhaft auf Port 80 (Ausweich-Port, falls belegt).

Dieses Portal kennt selbst **keine** Anwendung namentlich – jede App registriert sich zur Laufzeit mit einer kleinen JSON-Beschreibung unter `/opt/setup-portal/apps.d/<app-id>.json` (Pfade, Ports, Backup-/Restore-Konfiguration, Update-Quelle). Aktuell nutzen [HonigBox](https://github.com/Chrischn73/honigbox) und [BeeTown](https://github.com/Chrischn73/beetown) dieses Portal gemeinsam auf demselben Pi – die Landing-Page zeigt dann einen Button pro installierter App.

## Nutzung

**Normalerweise nichts zu tun.** HonigBox und BeeTown bringen in ihrem eigenen `install.sh` einen kleinen Bootstrap-Schritt mit: ist `/opt/setup-portal` noch nicht vorhanden, laden sie automatisch das neueste Release dieses Repos herunter und führen dessen `install.sh` aus. Danach läuft alles eigenständig weiter.

**Manuelle Installation** (z. B. zum Reparieren, oder ganz ohne eine der beiden Apps):

```bash
curl -L https://github.com/Chrischn73/setup-portal/archive/refs/heads/main.tar.gz -o portal.tar.gz
tar xzf portal.tar.gz
cd setup-portal-main
sudo bash install.sh
```

## Selbst-Update

Ein täglicher Timer (`setup-portal-update-check.timer`, 02:30 Uhr) prüft dieses Repo auf ein neueres Release und aktualisiert sich bei Bedarf automatisch (`setup_portal.py --self-update`). Kein erneuter `install.sh`-Lauf einer der beiden Apps nötig, um eine neue Portal-Version zu bekommen.

Für eine neue Version: `PORTAL_VERSION`-Konstante in `setup_portal.py` erhöhen, committen, GitHub-Release mit passendem Tag veröffentlichen.

## Enthaltene Dateien

| Datei | Zweck |
|---|---|
| `setup_portal.py` | Der eigentliche Webserver (reine Python-Standardbibliothek) |
| `setup-portal.sh` | Startskript (WLAN-Modul vorbereiten, dann den Server starten) |
| `setup-portal.service` | systemd-Unit für den dauerhaften Betrieb |
| `setup-portal-update-check.service`/`.timer` | Täglicher Selbst-Update-Check |
| `regen-issue.sh` | Baut den Boot-Bildschirm (`/etc/issue`) aus den Fragmenten aller registrierten Apps neu zusammen |
| `install.sh` | Installiert/aktualisiert alles oben (idempotent) |

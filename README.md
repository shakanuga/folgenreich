# Folgenreich

Selbst gehosteter Tracker für Serien, Animes, Filme, Bücher und Spiele. Mehrere Konten, ein Docker-Container, eine SQLite-Datei.

- **Technik:** Python 3.12, Flask, SQLite, Gunicorn, ohne Build-Schritt
- **Image:** `ghcr.io/shakanuga/folgenreich:latest`, sobald das Repo auf GitHub liegt

## Was die App macht

| Bereich | Funktion |
|---|---|
| **Serien, Anime** | Jede Serie ist ein Eintrag, Staffeln hängen automatisch daran. Reiter: Aktiv, Warten, Archiv, Merkliste. |
| **Abhaken** | Nächste Folge mit einem Tipp, „Bis hier“, ganze Staffel, alles. Rückgängig direkt nach dem Abhaken. |
| **Automatik** | Neue Folge: zurück nach Aktiv. Serie beendet und alles gesehen: Archiv. Neue Staffel angekündigt: aus dem Archiv nach Warten. |
| **Filme** | Merkliste und Gesehen. |
| **Bücher** | Mein Regal, Lese ich, Gelesen, Merkliste. Foto vom Strichcode oder Cover, Suche nach Titel oder ISBN, Warnung bei Doppelkauf. |
| **Spiele** | Sammlung mit Exemplaren je Plattform (Format, Zustand, Preis, Kaufdatum, Standort), Durchgespielt-Status, Wunschliste, Statistik. Strichcode-Scan mit lernender Zuordnung, Covererkennung als Rückfall. |
| **Jellyfin** | Gesehene Folgen und Filme werden automatisch abgehakt. |

Aktiv, Warten und Archiv sind nirgends gespeichert. Die App berechnet sie bei jedem Aufruf aus dem Fortschritt und den TMDB-Daten (`logic.py`). Laufende Serien werden alle 12 Stunden abgeglichen, beendete wöchentlich.

## Installation auf Unraid

**1. Datenordner vorbereiten.** Der Container läuft als `nobody:users`:

```bash
mkdir -p /mnt/user/appdata/folgenreich
chown 99:100 /mnt/user/appdata/folgenreich
```

**2. Container anlegen.** Mit `docker-compose.yml` oder von Hand:

| Einstellung | Wert |
|---|---|
| Repository | `ghcr.io/shakanuga/folgenreich:latest` |
| Port | Host `8765` auf Container `8000` |
| Pfad | `/mnt/user/appdata/folgenreich` auf `/data` |
| `TMDB_API_KEY` | TMDB-Schlüssel, API-Key oder Lese-Token |
| `IGDB_ID`, `IGDB_SECRET` | optional, für die Spielesuche |
| `GOOGLE_BOOKS_API_KEY` | optional, falls Google ohne Schlüssel drosselt |
| `JELLYFIN_URL`, `JELLYFIN_API_KEY` | optional, um bisher Gesehenes aus Jellyfin zu übernehmen |
| `COOKIE_SECURE` | `1`, wenn die App nur über HTTPS läuft |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | optional, Anmeldung mit Pocket ID (siehe unten) |

Ohne fertiges Image geht es auch direkt aus dem Ordner: `docker build -t folgenreich .`

**3. Domain.** Im Nginx Proxy Manager eine eigene Subdomain auf `<Server-IP>:8765` zeigen lassen.

**4. Erster Start.** Seite öffnen, erstes Konto anlegen, dann im Konto die Yamtrack-CSV hochladen. Der erste Abgleich läuft im Hintergrund und dauert je nach Sammlung einige Minuten.

**5. Jellyfin.** Webhook-Plugin installieren und den Anweisungen im Konto unter „Jellyfin einrichten“ folgen.

**6. Bisher Gesehenes aus Jellyfin.** Im Konto „Jellyfin prüfen“ wählen. Du siehst zuerst, was fehlt, und ergänzt es dann per Klick. Entfernt wird nichts.

## Anmeldung mit Pocket ID

Folgenreich kann sich über Pocket ID (OpenID Connect) anmelden. Bestehende Konten werden beim ersten Login über den **Benutzernamen** gefunden und danach fest mit dem Pocket-ID-Konto verknüpft.

1. In Pocket ID einen OIDC-Client `Folgenreich` anlegen, Callback-URL `https://folgenreich.example.de/anmelden/pocketid/zurueck`.
2. Am Container setzen:

| Variable | Wert |
|---|---|
| `OIDC_ISSUER` | Adresse von Pocket ID, z. B. `https://id.example.de` |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | aus Pocket ID |
| `OIDC_LABEL` | optional, Text auf dem Knopf |
| `OIDC_AUTO_REDIRECT` | `1` = Anmeldeseite überspringen |
| `OIDC_DISABLE_PASSWORD` | `1` = nur noch Pocket ID, kein Passwort-Login |
| `OIDC_AUTO_CREATE` | `1` = unbekannte Pocket-ID-Nutzer bekommen automatisch ein Konto |
| `OIDC_ALLOWED_GROUPS` | optional, nur diese Pocket-ID-Gruppen dürfen rein |
| `OIDC_ADMIN_GROUPS` | optional, neue Konten aus diesen Gruppen werden Admin |
| `OIDC_REDIRECT_URI` | optional, falls die Rücksprung-Adresse falsch gebildet wird |

Der Name in Folgenreich muss beim ersten Login dem Benutzernamen in Pocket ID entsprechen (Groß- und Kleinschreibung egal). Gibt es noch kein Konto, wird das erste Pocket-ID-Konto Admin.

## Schlüssel

- **TMDB:** kostenlos unter themoviedb.org in den Kontoeinstellungen unter API.
- **Jellyfin:** im Dashboard unter API-Schlüssel anlegen. `JELLYFIN_URL` ist die Adresse, unter der der Container Jellyfin erreicht.
- **IGDB:** kostenlos über eine Anwendung in der Twitch-Developer-Konsole, Client-ID und Secret eintragen. Ohne IGDB lassen sich Spiele von Hand eintragen.

## Daten und Sicherung

Alles liegt in `/data`: Datenbank `folgenreich.db`, eigene Cover und `secret.key`. Der Ordner liegt unter appdata und wird vom Appdata-Backup miterfasst. Jedes Konto kann seine Daten als JSON herunterladen.

## Entwickeln

```bash
pip install -r app/requirements.txt pytest
python -m pytest tests
```

Die Tests bilden TMDB, IGDB und die Bücherdienste lokal nach und brauchen kein Internet.

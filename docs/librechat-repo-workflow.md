# LibreChat: Was sich geändert hat und wie wir jetzt arbeiten

## Was ist passiert

Am 2026-07-16 ist aufgefallen, dass zwei Live-Konfigurationen an LibreChat verschwunden waren: die automatische Websuche-Aktivierung und Lucas Anbindung der Claude-Modelle.
Beides war korrekt umgesetzt, existierte aber nur als Änderung direkt auf dem VPS, nie in Git.

Der Grund: `apps/librechat/` war von Git komplett ignoriert, und das Deploy-Skript (`overrides/deploy-overrides.sh`) hat Dateien blind per `cp` von unserem Repo auf den VPS kopiert, ohne zu prüfen, ob dort etwas Neueres oder Anderes lag.
Eine Live-Änderung war für Git unsichtbar und wurde beim nächsten Deploy stillschweigend überschrieben.
Das ist am selben Tag zweimal real passiert — einmal mit Verlust (Websuche), einmal mit knapper Rettung mitten in der Untersuchung (Lucas Claude-Integration).
Die vollständige Chronik steht in `hetzner-ai-mvp-changes.md`, Einträge vom 2026-07-16.

## Was sich konkret geändert hat

Zwei Änderungen beheben das strukturell, nicht nur für diesen einen Vorfall:

**1. `apps/librechat` ist jetzt ein Git-Submodule.**
Es zeigt auf unseren eigenen, privaten Fork `dorianaitest/LibreChat` (Branch `hetzner-ai-mvp-patches`, gepinnt auf einen festen Commit).
Unsere drei echten Quellcode-Patches (`localStrategy.js`, `AuthService.js`, `invite-fixed.js`) sind dort jetzt normale, committete Git-Commits statt unsichtbarer Dateien auf dem VPS.

**2. Alle übrigen Konfigurationsdateien werden direkt aus `overrides/librechat/` gemountet.**
`librechat.yaml`, Branding-Assets, `index.html`, `manifest.webmanifest`, die `search.cjs`/`search.mjs`-Patches und die LiteLLM-Konfiguration existieren jetzt nur noch als eine einzige, git-getrackte Kopie.
`docker-compose.override.yml` bindet sie direkt aus `overrides/librechat/` ein.
Der frühere Kopier-Schritt im Deploy-Skript ist komplett entfallen — es gibt nichts mehr, das kopiert werden könnte, und damit auch keine zweite Kopie, die auseinanderdriften kann.

`overrides/deploy-overrides.sh` kümmert sich jetzt nur noch um das Admin-Panel (`apps/librechat-admin-panel`), das von beiden Änderungen bewusst ausgeklammert wurde und weiterhin nach dem alten Verfahren läuft.

**Nachtrag 2026-07-18:** Der Umbau hatte zunächst eine Lücke: die tatsächlich geladene `docker-compose.override.yml` lag weiterhin als untrackte Kopie in `apps/librechat/` und mountete dortige Alt-Kopien statt `overrides/librechat/` — Änderungen aus dem Repo (Modell-Refresh, auto-router, Gemini) waren dadurch nie live. Seit dem 18.07. abends ist die aktive Datei ein Symlink auf die git-getrackte Version:

```
ln -s /srv/overrides/librechat/docker-compose.override.yml \
  /srv/apps/librechat/docker-compose.override.yml
```

Der Symlink liegt im Submodule-Arbeitsverzeichnis und ist selbst nicht in Git — bei einem frischen Deployment muss er einmalig neu angelegt werden. Relative Pfade in der Override-Datei (`./config/`, `./api/`) lösen weiterhin relativ zum Projektverzeichnis `/srv/apps/librechat` auf. Die vom 18.07. stammenden Sicherungen (`docker-compose.override.yml.bak-1807`, `librechat.yaml.bak-0716`) und die nicht mehr gemounteten Alt-Kopien in `apps/librechat/` können nach einer Stabilitätsphase entfernt werden.

Das Ergebnis: für praktisch jede Änderung an LibreChat ist der komplette Deploy-Vorgang jetzt "bearbeiten, committen, pushen, auf dem VPS pullen, Container neu starten" — ganz ohne Skript.

## Wie wir ab jetzt arbeiten

Es gibt weiterhin zwei Arten von Änderungen, die sich nur in einem Detail unterscheiden.

**Änderungen an unserer eigenen Konfiguration** (`librechat.yaml`, Branding, `index.html`, `manifest.webmanifest`, `search.cjs`/`search.mjs`, LiteLLM-Config — alles in `overrides/librechat/`):

1. Datei in `overrides/librechat/` im Hauptrepo bearbeiten, niemals direkt auf dem VPS.
2. Committen und pushen.
3. Auf dem VPS: `cd /srv && git pull`.
4. Betroffenen Container neu starten.

**Änderungen am LibreChat-Quellcode selbst** (aktuell nur die drei Patches oben, in unserem Fork):

1. Lokal im Fork-Checkout arbeiten (`~/ki-business-stuff/librechat-upstream`), niemals direkt auf dem VPS editieren.
2. Änderung committen und auf den Branch `hetzner-ai-mvp-patches` im Fork pushen.
3. Im Hauptrepo: den Submodule-Pointer aktualisieren — `git add apps/librechat` (Git erkennt automatisch den neuen Commit-Stand), committen, pushen.
4. Auf dem VPS: `cd /srv && git pull`, danach `git submodule update`.
5. Betroffenen Container neu starten.

In beiden Fällen bleibt ein Container-Neustart nötig — LibreChat liest Konfiguration nur beim Start, es gibt kein automatisches Reload.

**Ausnahme, unverändert:** Geheimnisse (LibreChats eigene `.env` und die von LiteLLM) werden weiterhin ausschließlich manuell auf dem VPS gepflegt und nie committet.

## Die wichtigste Regel: nie direkt auf dem VPS editieren

Jede Live-Änderung direkt auf dem VPS ist ein Risiko, unabhängig davon, ob sie im Submodule oder in einer Overrides-Datei passiert.
Eine solche Änderung ist für Git unsichtbar, bis sie committet wird.

Falls eine Live-Änderung unumgänglich ist, zum Beispiel bei einem akuten Hotfix: sie so schnell wie möglich committen und pushen, statt sie "für später" zu notieren.

## Was tun, wenn trotzdem mal live editiert wurde

Git schützt jetzt automatisch vor dem stillen Überschreiben.
Wenn beim `git pull` auf dem VPS eine lokale, uncommittete Änderung im Weg steht, bricht Git mit einer Fehlermeldung ab, statt sie zu überschreiben:

```
error: Your local changes to the following files would be overwritten by merge:
    <Datei>
Please commit your changes or stash them before you merge.
```

In diesem Fall:

1. **Nicht einfach verwerfen.** Erst prüfen, was genau lokal verändert wurde (`git diff`).
2. Prüfen, ob die Änderung inhaltlich bereits im eingehenden Commit enthalten ist, zum Beispiel weil sie zufällig zeitgleich woanders committet wurde.
   Falls ja: mit einem direkten Diff auf Byte-Gleichheit prüfen, dann `git checkout -- <Datei>` zum Verwerfen.
3. Falls die Änderung neu und wertvoll ist: sie zuerst ins Hauptrepo übertragen (Datei lokal bearbeiten, committen, pushen), dann erst auf dem VPS verwerfen und neu pullen.
4. Im Zweifel: nichts löschen, nachfragen.

## Zukünftig: Updates von LibreChat einspielen

Der Fork ist aktuell auf `v0.8.7-rc1` gepinnt, passend zum laufenden Image.
Sobald LibreChat auf eine neuere Version aktualisiert wird, muss der Branch `hetzner-ai-mvp-patches` auf den neuen Upstream-Stand rebased oder gemerged werden (`git fetch upstream`, dann Rebase/Merge), bevor das Submodule im Hauptrepo auf den neuen Commit gesetzt wird.
Das ist noch nicht eingerichtet oder automatisiert und sollte bei der nächsten LibreChat-Version manuell gemacht werden.

# HA Image Inspector

[English documentation](README.md)

A Home Assistant adminisztrátori oldalsávjában megmutatja a hivatalos Container image tárhelybontását. Külön kereshetők a beépített integrációk, a Python-csomagok és a rendszereszközök. A sorok kibontva a legnagyobb fájlokat, a Rétegek fül pedig a buildlépések méretét mutatja.

## HACS-telepítés

1. A HACS **Egyéni tárolók** menüjében add hozzá a `https://github.com/kbalint/ha_image_inspector` tárolót **Integráció** típussal.
2. Töltsd le a **HA Image Inspector** integrációt, és indítsd újra a Home Assistantot.
3. A **Beállítások → Eszközök és szolgáltatások → Integráció hozzáadása** alatt válaszd a **HA Image Inspector** integrációt.
4. Nyisd meg az oldalsávban a **HA Image Inspector** panelt, és kattints az **Image elemzése** gombra.

Kézi telepítéshez a kiadás csomagjából másold a `custom_components/ha_image_inspector` mappát a Home Assistant konfigurációs könyvtárának `custom_components` mappájába, majd folytasd a második lépéstől. A modulhoz legalább Home Assistant 2026.11 kell, valamint HTTPS-elérés a GHCR és a rétegeket kiszolgáló tárhely felé.

## Mit mér?

Alapértelmezésben a futó Home Assistant Core verziójának és architektúrájának megfelelő hivatalos registry-image-et vizsgálja. Másik tag vagy `sha256:…` manifest-digest is megadható. A helyi konténer image-digestjének ellenőrzéséhez a Docker hoston futtasd az alábbi parancsokat. Ha más a konténer neve, írd át a `homeassistant` értéket.

```sh
image_id=$(docker container inspect --format '{{.Image}}' homeassistant)
docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image_id"
```

A hivatalos tároló sorából az `@` utáni digestet másold a panelbe. Egy elemzés több száz MB letöltést jelenthet. Az utolsó sikeres jelentést a Home Assistant elmenti.

| Mutató | Jelentés |
| --- | --- |
| Registry-letöltés | A kiválasztott platform tömörített rétegei |
| Kibontott rétegarchívumok | A kitömörített tar-adatfolyamok fejléccel és kitöltéssel együtt |
| Látható fájlok | A kész image normál fájljainak mérete, a hard linkek közös tartalmát egyszer számolva |
| Elfedezett fájlok | A törölt vagy felülírt fájlok alsó rétegekben megmaradt tartalma |

A mérés hatóköre az image. A futó konténer írásai, a `/config`, a mountok, a megosztott rétegek miatt megtakarított hely és a Docker tárolódriverének többlete ezen kívül esnek. A Python-csomagokat a telepítési jegyzékük alapján azonosítja, ahol van ilyen. A panelek a beépített integrációk közvetlen csomagfüggőségeit is mutatják, önálló méretük megőrzésével.

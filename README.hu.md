# HA Image Inspector

[English documentation](README.md)

A Home Assistant adminisztrátori oldalsávjában megmutatja, mennyi helyet foglalnak a futó build integrációi, Python-csomagjai és rendszerfájljai. Docker-archívumból vagy a hivatalos registryből külön rétegenkénti mérés is indítható.

## HACS-telepítés

1. A HACS **Egyéni tárolók** menüjében add hozzá a `https://github.com/kbalint/ha_image_inspector` tárolót **Integráció** típussal.
2. Töltsd le a **HA Image Inspector** integrációt, és indítsd újra a Home Assistantot.
3. A **Beállítások → Eszközök és szolgáltatások → Integráció hozzáadása** alatt válaszd a **HA Image Inspector** integrációt.
4. Nyisd meg az oldalsávban a **HA Image Inspector** panelt, és kattints a **Futó konténer elemzése** gombra.

Kézi telepítéshez a kiadás csomagjából másold a `custom_components/ha_image_inspector` mappát a Home Assistant konfigurációs könyvtárának `custom_components` mappájába, majd folytasd a második lépéstől. A modulhoz legalább Home Assistant 2026.11 kell.

## Futó konténer elemzése

Az integráció magán a Home Assistant konténerén belül olvassa a futó build látható fájljait, beleértve a konténer írható rétegének módosításait. A `/config` és a többi csatolt kötet kimarad. Ehhez nincs szükség image-exportra, Docker socketre vagy registry-hozzáférésre. Az alsó rétegek mérete és az elfedett fájlok külön vizsgálhatók Docker-archívumból vagy a registryből.

## Helyi Docker-archívum elemzése offline

A Docker hoston exportáld a futó konténer image-ét. A `/path/to/ha-config` helyére a konténer `/config` alá csatolt hostmappája kerüljön, a `homeassistant` helyére pedig a konténer neve.

```sh
image_id=$(docker container inspect --format '{{.Image}}' homeassistant)
docker image save --output /path/to/ha-config/ha_image_inspector.tar "$image_id"
```

A panelen a **Helyi konténer** alatt nyisd le a **Docker-archívum elemzése rétegenként** részt, majd indítsd el az elemzést. A külső tar egyetlen image-et és platformot tartalmazzon. Az export átmenetileg helyet foglal a hoston. A jelentés mentése után törölheted.

## Hivatalos registry-image elemzése

A **Hivatalos registry** módban alapból a futó Home Assistant Core verziója és architektúrája szerepel. Másik tag vagy `sha256:…` manifest-digest is megadható. A helyi konténer image-digestjének ellenőrzéséhez a Docker hoston futtasd az alábbi parancsokat. Ha más a konténer neve, írd át a `homeassistant` értéket.

```sh
image_id=$(docker container inspect --format '{{.Image}}' homeassistant)
docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image_id"
```

A hivatalos tároló sorából az `@` utáni digestet másold a panelbe. Egy registry-elemzés több száz MB letöltést jelenthet, és HTTPS-elérést igényel a GHCR és a rétegtárhely felé. A Home Assistant elmenti az utolsó sikeres jelentést.

## Mit mér?

| Mutató | Jelentés |
| --- | --- |
| Registry-letöltés | A kiválasztott platform tömörített rétegei |
| Archivált rétegek | A rétegek mérete a helyi exportban, a külső tar fejlécei és az image metaadatai nélkül |
| Kibontott rétegarchívumok | A kitömörített tar-adatfolyamok fejléccel és kitöltéssel együtt |
| Látható fájlok | A kész image normál fájljainak mérete, a hard linkek közös tartalmát egyszer számolva |
| Elfedezett fájlok | A törölt vagy felülírt fájlok alsó rétegekben megmaradt tartalma |

A mérés hatóköre az image. A futó konténer írásai, a `/config`, a mountok, a megosztott rétegek miatt megtakarított hely és a Docker tárolódriverének többlete ezen kívül esnek. A Python-csomagokat a telepítési jegyzékük alapján azonosítja, ahol van ilyen. A panelek a beépített integrációk közvetlen csomagfüggőségeit is mutatják, önálló méretük megőrzésével.

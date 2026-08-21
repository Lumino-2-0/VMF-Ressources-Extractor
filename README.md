# VMF Resource Extractor

Outil en ligne de commande (**CLI**) pour **GMod / Source SDK 2013** qui analyse un fichier `.vmf` et retrouve (presque) **toutes**  les ressources qu'elle utilise - materials, textures, models, sons, sprites, particules, lua, soundscapes,... Pour les copier dans un dossier "custom" (de votre choix) prêt à être packé dans le BSP au moment de la publication (ou pour juste récupérer celles-ci).

Le but étant de retrouver toutes ces ressources dans le cadre où vous les auriez *"mis en vrac"*, ou perdu, dans le dossier du jeu source, et oublier de les séparer des ressources communes avec le jeu.
Par exemple, vous avez mis un ou plusieurs model(s) dans les ressources communes/globales de gmod tel que : 
```C:\Program Files (x86)\Steam\steamapps\common\GarrysMod\garrysmod\models```

...

(oui, ça sent le vécu sur une map 🥴)

...

Et bien cette extracteur pourra vous retrouvez l'ensemble des models mis et oublié dans les ressources du jeu.

Contrairement à un simple grep de fichier `.vmf`, l'outil résout les dépendances de
façon **récursive** : un prop a besoin de ses materials, un material a besoin de ses
textures (et éventuellement d'un autre material via `include`/`$bottommaterial`), etc.

## Fonctionnalités

* **Résolution récursive complète** : materials, textures, models (avec fichiers
compagnons `.vvd`/`.vtx`/`.phy`/`.ani`), sons, soundscapes, particules, lua, sprites
legacy `.spr`.
* **Détection des dépendances imbriquées** : materials `Patch` (`include`),
`$bottommaterial`, `$fallbackmaterial`, dépendances d'un `.mdl` compilé (extraction de
chaînes vérifiée contre le système de fichiers - jamais de fausse dépendance
inventée).
* **Diagnostic VPK** : détecte si une ressource est déjà disponible dans le jeu de base
ou un jeu monté (VPK) sans la copier inutilement (ce contenu est déjà présent chez
tout joueur possédant ce jeu).
* **Traçabilité complète** : chaque ressource manquante indique précisément quelle
entité / ligne du VMF / material parent l'a demandée.
* **Recherche intelligente de secours** : si un dossier a été déplacé/renommé, retrouve
le fichier par nom ou par correspondance approximative.
* **Multithread** (`-Threads N`) : accélère la résolution sur les grosses maps (I/O
disque parallélisée).
* **Bilingue** (`-Lang FR|EN`) : sortie console entièrement traduite.
* **Rapport CSV** exportable pour audit.

## Prérequis

* Python **3.8+**
* Aucune dépendance externe : le script n'utilise que la bibliothèque standard.

## Installation

```bash
git clone <url-du-repo>
cd vmf-resource-extractor
```

Aucune installation de paquet n'est nécessaire (`pip install` non requis).

## Utilisation

### En ligne de commande

```bash
python VMF\_ResExtractor.py -source "chemin/vers/ma\_map.vmf" -gameSrc "C:\\...\\GarrysMod\\garrysmod" -dest ".\\custom" -missing
```

### Via le launcher Windows (`.bat`)

Double-cliquer sur `VMF\_ResExtractor\_Launcher.bat` (ou glisser un fichier `.vmf`
directement dessus). Le launcher vérifie que Python est installé, puis demande les
paramètres un par un.

## Arguments

|Argument|Description|
|-|-|
|`-source` *(requis)*|Chemin du fichier `.vmf` à analyser|
|`-gameSrc` *(requis)*|Dossier `garrysmod` source (contient `materials/`, `models/`, `sound/`, etc.)|
|`-dest` *(requis)*|Dossier de sortie (créé automatiquement)|
|`-scan`|Analyse uniquement, ne copie rien|
|`-force`|Ne demande pas de confirmation avant de copier|
|`-missing`|Affiche le détail des ressources manquantes avec leur provenance|
|`-csv FICHIER`|Écrit un rapport CSV détaillé|
|`-Threads N`|Nombre de threads pour la résolution récursive (défaut : 1)|
|`-Lang FR\|EN`|Langue de la sortie console (défaut : FR)|
|`-noColor`|Désactive la sortie en couleur|

## Exemple de sortie

```
+--------------------------------------------------------------+
|                RAPPORT D'EXPORT \& DIAGNOSTIC VPK              |
+--------------------------------------------------------------+
| Materials    : 1508 à copier |  794 en VPK |    1 TRULY MISSING
| Textures     : 1541 à copier |  187 en VPK |    6 TRULY MISSING
| Models       : 1072 à copier |   12 en VPK |    1 TRULY MISSING
...
```

Avec `-missing`, chaque ressource introuvable indique sa provenance exacte :

```
\[TRULY MISSING] material: glass/glass\_fallback
  └─ Demandé par : VMT: materials/glass/door12\_glass\_diff.vmt (clé: $fallbackmaterial)
```

## Limites connues

* **Extraction des dépendances d'un `.mdl`** : basée sur une extraction de chaînes
ASCII dans le binaire compilé, vérifiée contre les fichiers réellement présents sur
le disque (aucune dépendance ne peut être inventée à tort). Dans de rares cas, un
skin très exotique peut ne pas être détecté.
* **Particules (`.pcf`)** : les dépendances imbriquées entre fichiers `.pcf` (un pcf
qui en référence un autre) ne sont pas suivies.
* **`env\_particle\_system`** : le nom du système d'effet n'est pas directement un nom de
fichier ; l'outil cherche ce nom dans tous les `.pcf` indexés.
* **Soundscapes** : la résolution dépend de la convention de nommage
`scripts/soundscapes\*.txt`.
* **Contenu VPK** (jeu de base, jeux montés) : détecté mais **jamais copié**, sur
l'hypothèse que ce contenu est déjà présent chez tout joueur possédant le jeu
correspondant. Si une ressource référencée vient d'un jeu spécifique que certains
joueurs n'ont pas (ex: un jeu Source tiers), elle peut manquer côté joueur même si
l'outil ne la signale pas comme `TRULY MISSING`.
* **Lua "deviné"** : le script lua potentiellement associé au nom de la map
(`lua/autorun/<nom\_map>.lua` et variantes) est recherché par ressemblance de nom,
uniquement dans le jeu de base. Ce n'est qu'une supposition :
son absence n'est jamais signalée comme une erreur.





---
*En coopération avec le magnifiak IronKnight et Mr.Havstrand 🦆🥸*

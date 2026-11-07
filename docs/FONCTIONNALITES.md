# Fonctionnalités détaillées

Liste complète des fonctions d'Atelier Traceuse (retour au [README](../README.md)). Les chemins ci-dessous sont ceux de macOS ;
les réglages et les compositions de l'utilisateur sont stockés dans `~/Library/Application Support/VG-X4/` (jamais dans l'app).

Protocole machine (vérifié sur une VG-X4 réelle, firmware GRBL 1.1f) : 115200 bauds, `M3 S905` = stylo baissé, `M5` = levé, `G4 P…` pause,
`G92 X0 Y0` = zéro, `!` pause, `~` reprise, Ctrl-X arrêt, origine en haut à gauche du papier, +Y vers le bas.

## Fonctions ajoutées après les premiers essais sur la machine

- **Compositions** : *Enregistrer / Ouvrir* des fichiers `.vg4` (texte, SVG et images embarqués, positions, stylos, papier). Sauvegarde automatique toutes les quelques secondes (`autosave.vg4`) → bouton *Dernière*.
- **Annuler / rétablir** (↶ ↷, ⌘Z / ⇧⌘Z) sur toute la composition.
- **Arrêt propre et reprise** : *Arrêt* = feed hold puis soft reset ; la position et le zéro sont conservés (vérifié sur la machine réelle). Un bandeau propose de *reprendre au trait N* (le trait interrompu est retracé en entier). Après une coupure USB, la position est perdue : il faut replacer le stylo et redéfinir le zéro.
- **Reconnexion automatique** si la liaison USB est perdue (désactivable dans Réglages).
- **Test machine** : sens des axes (avancer/reculer/droite/gauche/diagonale) avec diagnostic (moteur muet, axe inversé, trajectoire de travers), **étalonnage** (carré → pas/mm `$100`/`$101`, écrits après confirmation), **pression du stylo** (traits avec S croissant).
- **Papier** : formats prédéfinis, orientation, marges, aimantation aux marges / au centre / aux autres objets (⌥ = désactivée), alignements, *Tout centrer*.
- **Plusieurs stylos** : chaque objet a un stylo (nom et couleur dans Réglages). Le G-code insère `M0 ; Changer de stylo : …` ; l'app se met en pause et attend *Continuer*.
- **Axes** : réglages *Inverser X* et *Inverser Y* (Y est inversé sur cette machine depuis le changement de moteur).
- **Texte** : nouvelles polices Hershey (triple trait, gras, gothiques, feutre), ondulation de la ligne de base, espace des mots, lissage des traits.

## Dernières fonctions

- **Cadre** (le stylo levé fait le tour du dessin) et **Essai à blanc** (tout le trajet, stylo levé) dans le Composer et l'onglet G-code. Onglet Contrôle : *Alignement du papier* (va-et-vient le long d'un bord).
- **Anti-veille** pendant un tracé (`caffeinate`, lié au processus de l'app) et **notifications macOS** (fin de tracé ≥ 20 s, changement de stylo, erreur) — désactivables dans Réglages.
- **Valeur S par stylo** (Réglages → Stylos ; vide = valeur générale). Le test de pression peut l'appliquer à un stylo ou à tous.
- **Réglages de la carte** : sauvegarde automatique de `$$` avant chaque étalonnage (`~/Library/Application Support/VG-X4/backups/`), bouton *Sauvegarder $$*, *Restaurer l'étalonnage précédent* ($100/$101 seulement).
- **SVG** : option *Hachurer les formes remplies* (pas et angle réglables).
- **Modèles** : enveloppe DL, carte A6, lettre A4, étiquette (papier + textes à modifier).
- **Journal** des tracés (`history.jsonl`) : date, durée, longueur dessinée, état ; totaux hors simulation.

## Dernier lot de fonctions

- **Zone utilisable (butées)** : Contrôle → enregistrer la butée droite et la butée basse depuis le zéro ; l'app refuse les tracés qui dépassent et grise la zone interdite. Les butées sont perdues à la reconnexion (la position machine l'est aussi).
- **Durée estimée** : modèle du planificateur GRBL (accélération `$120`, junction deviation `$11`), corrigé par le rapport durée réelle / estimée de tes derniers tracés (journal).
- **Jeu mécanique** : Test machine → mire (deux traits verticaux, deux horizontaux) ; la compensation enregistrée décale les consignes à chaque inversion de sens (appliquée au G-code envoyé, pas aux déplacements manuels).
- **SVG multicolore** : à l'import, un objet et un stylo par couleur (calques parfaitement superposés).
- **Images en tons de gris** : mode *Tons de gris*, hachures croisées de plus en plus denses.
- **Répéter en grille** (colonnes × lignes, écarts, centrage) et **publipostage** CSV (`{colonne}` dans les textes, aperçu fiche par fiche, tracé de toutes les fiches avec pause pour changer la feuille).
- **Bibliothèque** : 30 formes et dessins paramétrables (+ les tiens : depuis la composition, depuis un SVG, ou un objet de la composition), à renommer, modifier, dupliquer, supprimer. Stockée dans `~/Library/Application Support/VG-X4/library/`.

## Design

Thème « atelier de dessinateur » : papier crème, encre bleue, vermillon. Thème clair uniquement (pas de mode sombre, même si macOS est en mode sombre). Typographies système de macOS (Iowan Old Style, Avenir Next, SF Mono), aucune ressource externe.

## Documents Word et PDF

Bouton **＋ Document** (ou glisser un `.docx` / `.pdf` dans la fenêtre) : l'app lit le texte et les images du fichier, les met en page sur des feuilles et les trace avec une police Hershey.

- **Réglages** (aperçu manuscrit en direct, feuille par feuille) : police, hauteur des lettres, interligne, marge, réorganisation du texte à la largeur du papier, saut de page du document = nouvelle feuille, images (largeur max, mode de tracé).
- **Word** : titres plus grands, listes avec un tiret, images dans l'ordre du document, **tableaux conservés** (colonnes proportionnelles, cellules fusionnées, texte à la ligne dans la cellule). Pas lus : en-têtes/pieds de page, zones de texte, notes de bas de page, images EMF/WMF, anciens `.doc` (les enregistrer en `.docx` ou PDF).
- **PDF** : texte et images de chaque page ; les tableaux **à traits** sont reconnus et gardés (un tableau sans traits visibles reste du texte) ; les paragraphes sont reconstitués (le PDF ne les stocke pas). Un PDF scanné n'a pas de texte : seules ses images (les pages) sont dessinées.
- **Sens du texte** : normal, tourné « haut à droite, bas à gauche » (document en portrait sur une feuille paysage), tourné « haut à gauche, bas à droite », ou tête en bas. La mise en page se fait sur le papier tourné, puis les objets reçoivent la rotation correspondante ; ce choix n'est pas mémorisé d'un document à l'autre.
- Les objets de la feuille sont de vrais objets (texte, image) que l'on peut déplacer ou modifier. **◀ ▶** change de feuille (les modifications faites sur la feuille courante sont alors remplacées : l'app prévient) ; **Tracer toutes les feuilles** les enchaîne en proposant de changer le papier entre deux. **Quitter** garde la feuille affichée comme composition normale.
- **Tableaux** : chaque tableau devient un seul objet « Tableau » (quadrillage + texte des cellules), répété avec sa ligne d'en-tête s'il continue sur la feuille suivante ; case « Garder les tableaux » décochée = une ligne de texte par rangée.
- Caractères absents de la police (ex. ⌘, ×) : ignorés, la liste est affichée.
- Dépendances : `python-docx`, `pypdf`, `pdfplumber` (dans `requirements.txt`).

## Zoom

Boutons **+ / 100 % / − / ⌖** en bas du rail de droite du Composer (et sur le bord droit de l'aperçu G-code) : zoom avant/arrière, retour à la vue d'ensemble, zoom sur l'objet sélectionné. Aussi : touches `+` `-` `0` (et `F` = objet sélectionné), pincement du trackpad ou ⌘ + molette (autour du curseur), défilement à deux doigts, outil « Déplacer la vue » ou glisser le fond pour déplacer la vue. Les règles suivent la vue et un quadrillage au millimètre apparaît quand on zoome beaucoup. Le zoom n'affecte pas le tracé.

## Barre du haut du Composer

Les boutons du haut sont des icônes de 36 px (infobulle au survol) : ajouter du texte, un SVG, une image, un document Word/PDF (boutons bleus), modèles, annuler/rétablir, publipostage, ouvrir / enregistrer / enregistrer sous / rouvrir la dernière sauvegarde, effacer le tracé affiché, nouvelle feuille ; puis papier, orientation, marge, aimantation (aimant, allumé = actif), tout centrer, voir le G-code, cadre, essai à blanc. **▶ Tracer** garde son texte.

## Barre d'outils du Composer

Petit rail d'icônes sur le bord droit de la feuille, de haut en bas (discret, il s'éclaircit quand on le survole ; chaque icône a une infobulle). Le zoom en fait partie, tout en bas :

- **Sélection** (`V`) : cliquer, déplacer, poignées de taille ◢ et de rotation ●. **Cadre de sélection** (`M`) : tracer un rectangle autour des objets pour les sélectionner (Maj + rectangle ou Maj + clic = ajouter / retirer). **Déplacer la vue** (`H`, ou garder `Espace` enfoncée). `⌘A` sélectionne tout, `Échap` désélectionne.
- **Tourner de 90°** à gauche / à droite (`[` et `]`), **réduire / agrandir de 10 %**, **adapter à la feuille** (agrandit ou réduit la sélection pour remplir la zone dans les marges, centrée), **aligner** (gauche, centre, droite, haut, milieu, bas — par rapport aux marges ou au centre de la feuille) et **centrer sur la feuille**.
- Avec plusieurs objets sélectionnés, ces boutons les traitent comme un seul bloc ; glisser l'un d'eux les déplace tous (aimantation comprise), les flèches du clavier aussi, `Suppr` les supprime. Tout est annulable (⌘Z).
- Un texte ou une image redimensionné garde sa taille « vraie » (hauteur des lettres, largeur) ; ce sont les réglages de l'objet qui changent, pas un simple étirement.

## Ordre des tracés (moins de temps perdu)

À la génération du G-code : les tracés dont les extrémités sont à moins de *Joindre si écart* sont fusionnés, puis l'ordre et le sens de chaque tracé sont optimisés (plus proche voisin, puis échanges 2-opt, 1,5 s au maximum par groupe ; objets consécutifs du même stylo optimisés ensemble). Le dessin est strictement identique : seuls l'ordre et le sens changent. Sur une page de texte, les déplacements à vide baissent d'environ 40 %.

Le temps d'un tracé de texte vient surtout des levées/baisses du stylo (2 × *Pause stylo* par tracé), pas des déplacements. Pour le réduire : *Joindre si écart* (1 mm : environ −20 % de temps, liaisons à peine visibles ; 2 mm et plus : des liaisons visibles entre lettres) et *Pause stylo* (à régler d'après ce que le servo supporte). Désactivable avec « Optimiser l'ordre des tracés ».


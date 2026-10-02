# RemoteDeploy — version Python 1.0.0

**Console de déploiement de script sur postes distants, avec relance automatique jusqu'à ce que TOUS les postes aient pris le script.**

> Ce document décrit la **version Python**. La version PowerShell 1.0.4 reste disponible
> dans son propre dossier (mêmes fonctions, autre langage) — voir le tableau de comparaison § 11.

---

## 1. Ce que fait le logiciel

1. Vous choisissez un **script** dans un dossier de votre choix (`.ps1`, `.cmd`, `.bat`, `.exe`, `.vbs`…).
2. Vous choisissez un fichier **TXT** contenant la **liste des postes** (un poste par ligne).
3. L'application vous demande une seule fois vos **identifiants d'administration**.
4. Le script est **copié puis exécuté sur chaque poste distant**, en parallèle (8 postes à la fois par défaut).
5. Tous les postes qui **n'ont pas pris le script sont relancés automatiquement**, vague après vague,
   avec une temporisation croissante, **jusqu'à 100 % de réussite**.
6. Suivi en direct (grille + journal), **journaux par poste**, **export CSV** et **rapport HTML**.

Rien ne s'exécute sur votre poste : le script part sur les postes cibles.

---

## 2. Prérequis

| Élément | Obligatoire | Remarque |
|---|---|---|
| Windows 10/11 ou Server 2012+ **sur le poste pilote** | oui | l'application à interface graphique |
| **Python 3.8 minimum** (avec `tkinter`) | oui | inclus dans l'installateur officiel python.org et le Microsoft Store |
| `psexec.exe` (PsTools) | oui pour le moteur PsExec | déjà présent sur votre poste : `C:\_build\PStools\psexec.exe` |
| `pywinrm` (`pip install pywinrm`) | non | seulement pour le moteur WinRM |
| WinRM actif sur les postes cibles | non | seulement pour le moteur WinRM |

Aucune compilation, aucun binaire : **l'application est un fichier texte Python** exécuté par
l'interpréteur officiel.

---

## 3. Installation en une fois (2 minutes)

```
1) Installez Python 3 si nécessaire :
      winget install Python.Python.3.12
   ou https://www.python.org/downloads/windows/
   (cochez « Add python.exe to PATH » et laissez l'option « tcl/tk » active)

2) Double-cliquez sur :  Installer-RemoteDeploy-Python.cmd
      • vérifie Python et affiche sa version
      • copie l'application dans  %LOCALAPPDATA%\RemoteDeploy
      • crée le raccourci du Bureau et du menu Démarrer
      • enregistre l'entrée « RemoteDeploy (Python) » dans Applications et fonctionnalités
      • écrit un journal :  installer-<date>.log  dans le dossier d'installation

3) Double-cliquez sur le raccourci « RemoteDeploy (Python) ».
```

Variantes de l'installateur :

```bat
Installer-RemoteDeploy-Python.cmd                      :: installation pour l'utilisateur courant
Installer-RemoteDeploy-Python.cmd C:\_build\RemoteDeploy :: dossier imposé (élévation UAC demandée)
Installer-RemoteDeploy-Python.cmd C:\_build\RemoteDeploy /silent   :: sans aucune question
```

Pour désinstaller : **Désinstaller-RemoteDeploy-Python.cmd** (ou Applications et fonctionnalités).
La désinstallation supprime l'application, les raccourcis et la configuration mémorisée.

---

## 4. Prise en main rapide

| Étape | Onglet | Action |
|---|---|---|
| 1 | **Script et postes** | « Parcourir un dossier… » → cliquez le script dans la liste |
| 2 | **Script et postes** | « Parcourir un .txt… » → sélectionnez votre liste de postes |
| 3 | **Options** | identifiants d'administration, moteur (Auto/PsExec/WinRM), temporisations |
| 4 | | « **Démarrer le déploiement** » puis saisissez vos identifiants |
| 5 | **Suivi et résultats** | suivez la grille ; à la fin : « Rapport HTML » et « Exporter en CSV » |

> **Premier test sans risque** : choisissez `Exemple-Script.ps1` + `postes-exemple.txt`,
> puis moteur **« Simulation (aucun contact réseau) »**. La boucle de relance se déroule
> sous vos yeux avec de faux échecs, sans contacter aucun poste.

---

## 5. Détail des onglets

### Onglet 1 — Script et postes

* **Dossier des scripts** : le dossier que vous parcourez. La liste affiche les fichiers `.ps1`, `.cmd`,
  `.bat`, `.exe`, `.vbs`, `.msi`, `.py`, `.jar`… Un clic sélectionne le script ; un aperçu du contenu,
  sa taille, sa date et son **empreinte SHA256** s'affichent (utile pour tracer ce qui a été déployé).
* **Arguments passés au script (optionnel)** : par exemple `-Mode Silencieux` ou `Serveur=SRV-FIC`.
* **Liste des postes (fichier .txt)** : un poste par ligne — nom court, FODN ou adresse IP.
  * les lignes vides, les commentaires (`#` ou `;`) et les doublons sont ignorés ;
  * si une ligne contient un `;` ou une `,`, seul le premier champ est retenu
    (pratique pour un export CSV avec la description du poste) ;
  * les guillemets et espaces parasites sont nettoyés.

### Onglet 2 — Options

| Réglage | Rôle |
|---|---|
| **Moteur d'exécution** | `Auto` (WinRM puis PsExec), `PsExec`, `WinRM`, `Simulation` |
| **Chemin de psexec.exe** | par défaut `C:\_build\PStools\psexec.exe` — bouton **Tester** |
| **Identifiants d'administration** | compte local (`.\administrateur`), compte de domaine (`DOMAINE\adm-deploy`) ou SSO (session Windows) |
| **Mémoriser les identifiants** | chiffrement **DPAPI Windows** ; le mot de passe n'est jamais écrit en clair |
| **Dossier distant** | où le script est copié sur chaque poste (défaut `C:\ProgramData\_DeployRun`) |
| **Supprimer le dossier distant après réussite** | nettoyage automatique par poste |
| **Fichier preuve (optionnel)** | ex. `C:\ProgramData\_DeployRun\done.flag` : le poste n'est déclaré réussi que si ce fichier existe |
| **Relance automatique** | nombre max de vagues (0 = jusqu'à 100 %), postes en parallèle, temporisation, croissance, plafond |
| **Délais et privilèges** | délai de connexion, durée max d'exécution, exécution en SYSTEM (`psexec -s`), élévation maximale (`-h`) |
| **Journaux et rapports** | dossier des journaux, export CSV, rapport HTML, enregistrement de la configuration |

Le bouton **« Enregistrer la configuration »** écrit `remotedeploy.config.json` : les réglages sont
rechargés automatiquement au prochain lancement (les identifiants restent à part, chiffrés).

### Onglet 3 — Suivi et résultats

* Grille par poste : **statut** (En attente / En cours / Réussi / **Échec**), **cause** classée,
  **nombre de tentatives**, moteur utilisé, code retour, durée, horodatage, message complet.
* Filtre (tous / en attente / en cours / réussis / en échec) et bouton **« Copier le journal »**.
* Journal d'exécution horodaté (vagues, relances, temporisations, résumé final).
* Compteurs en bas : postes, réussis, en échec, en cours, vague en cours.
* Boutons **Pause**, **Arrêter**, **Relancer maintenant les postes en échec** (écourte la temporisation),
  et **Relancer en administrateur** si l'application n'est pas élevée.

### Onglet 4 — Aide

Rappel complet du fonctionnement, des moteurs et des codes de retour, plus les boutons d'export.

---

## 6. La relance automatique (le cœur du logiciel)

```
Vague 1 : tous les postes, par paquets de « postes en parallèle »
   ├─ réussis  -> sortis de la boucle (jamais reconvoqués)
   └─ en échec -> remis en file
Temporisation : 60 s (valeur par défaut), puis × 1,5 à chaque vague, plafonnée à 900 s
Vague 2 : uniquement les postes en échec, avec les motifs d'échec classés
   ... et ainsi de suite JUSQU'À 100 % DE RÉUSSITE
```

* Les postes réussis **ne sont jamais relancés**.
* La cause de chaque échec est **classée automatiquement** : *Identifiants*, *Accès refusé*,
  *Injoignable*, *Code du script*, *Délai dépassé*, *Autre* — vous savez immédiatement
  quels postes corriger (droits, pare-feu, poste éteint…).
* Si des postes échouent pour cause d'identifiants, l'application **redemande les identifiants**
  et relance la vague : pas besoin de tout recommencer.
* Vous pouvez fixer un **nombre maximum de vagues** (0 = sans limite) ou arrêter à tout moment :
  les postes déjà réussis restent acquis.

---

## 7. Moteurs d'exécution

| Moteur | Comment ça marche | Quand l'utiliser |
|---|---|---|
| **Auto** *(conseillé)* | tente WinRM, bascule automatiquement sur PsExec si WinRM est indisponible | le plus simple, couvre les parcs hétérogènes |
| **PsExec** | `net use` temporaire sur le partage administratif (`\\poste\C$`), copie du script, exécution via PsTools | parc classique, sans WinRM |
| **WinRM** | session PowerShell distante via `pywinrm` | WinRM déjà activé dans le parc (pas de PsExec nécessaire) |
| **Simulation** | aucun contact réseau, échecs aléatoires | **tester la boucle de relance** et former les collègues |

### Ce qui est poussé sur le poste

```
C:\ProgramData\_DeployRun\
    _rd_payload.ps1        votre script renommé (extension d'origine conservée)
    _rd_run.cmd            lanceur : exécute le script, journalise et écrit le code retour
    _rd_output.log         sortie complète de l'exécution
    _rd_rc.txt             code retour du script
    <vos fichiers annexes> seulement si « Copier aussi les fichiers du dossier du script »
```

Le code retour du script est récupéré et interprété : **0 = réussite**.
Si vous renseignez un *fichier preuve*, la réussite exige en plus que ce fichier existe sur le poste
(contrôle 3 fois, avec temporisation) : c'est la garantie la plus fiable que « le poste a pris le script ».

---

## 8. Journaux, CSV et rapport HTML

À chaque campagne, un dossier `AAAAMMJJ-HHMMSS` est créé dans le dossier de journaux
(par défaut `%LOCALAPPDATA%\RemoteDeploy\Journaux`, modifiable) :

```
20261002-124512\
    POSTE-01.log … POSTE-15.log     détail complet : commandes, sortie, erreurs, horodatage
    resultats.csv                   tableau par poste (séparateur « ; », prêt pour Excel FR)
    rapport.html                    synthèse : compteurs, causes d'échec, tableau complet
```

---

## 9. Ligne de commande (diagnostic et automatisation)

```bat
python RemoteDeploy.py                    :: interface graphique classique
python RemoteDeploy.py --selftest         :: 20 contrôles internes (n'affecte aucun poste)
python RemoteDeploy.py --ui-test          :: construit l'interface puis se ferme (test)
python RemoteDeploy.py --elevate          :: relance l'application en administrateur
python RemoteDeploy.py --simulate 25 --throttle 8 --delay 1 --max-waves 0
                                          :: campagne simulée sur 25 postes, sans interface
```

Codes de retour : `0` succès, `1` échecs restants (ou contrôles en échec), `2` tkinter absent,
`3` erreur d'arguments.

---

## 10. Antivirus / EDR (Trellix, Defender…) — pourquoi cette version ne déclenche rien

| | Version PowerShell 1.0.4 | **Version Python 1.0.0** |
|---|---|---|
| Fichiers installés | `.ps1`, `.cmd`, texte | `.py`, `.cmd`, texte |
| Binaire compilé | aucun par défaut (exe opt-in) | **aucun, jamais** |
| Interpréteur | `powershell.exe` (signé Microsoft) | `python.exe` (installateur officiel) |
| Empreinte typique d'un dropper | exe auto-extractible | **absente** |

La livraison ne contient **que des fichiers texte** exécutés par un interpréteur signé :
il n'y a rien à exclure, rien à demander à l'équipe sécurité.

⚠️ **Le script `Optionnel-Creer-EXE-PyInstaller.cmd` change la donne.** Un exécutable PyInstaller est
un binaire **non signé** qui emballe l'interpréteur Python : c'est exactement le profil que les EDR
classent en menace (c'est ce qui s'était produit avec l'exe autonome de la version PowerShell,
classé `ARC AI1-LS!518b64a33685` par Trellix). Si un `.exe` devient indispensable :

1. fabriquez-le seulement si nécessaire (`Optionnel-Creer-EXE-PyInstaller.cmd`) ;
2. **avant diffusion**, demandez une exclusion ciblée (dossier + hachage SHA256) ou une validation
   de faux positif auprès de l'équipe sécurité ;
3. sinon, la version `.py` + `.cmd` remplit exactement les mêmes fonctions.

---

## 11. Différences avec la version PowerShell 1.0.4

| | PowerShell 1.0.4 | Python 1.0.0 |
|---|---|---|
| Interface | WPF (XAML) | **tkinter** (bibliothèque standard) |
| Prérequis poste pilote | PowerShell 5.1 (natif Windows) | **Python 3.8+** |
| Dépendances | aucune | aucune (PyWinRM facultatif pour WinRM) |
| Moteurs | Auto / PsExec / WinRM / Simulation | **identiques** |
| Relance jusqu'à 100 % | oui | **oui** |
| Identifiants chiffrés (DPAPI) | oui | **oui** |
| CSV + rapport HTML + journaux par poste | oui | **oui** |
| Fichier preuve de réussite | oui | **oui** |
| Auto-test intégré | `-SelfTest` (16 contrôles) | `--selftest` (**20 contrôles**) |
| Test de la boucle de relance | moteur Simulation | **moteur Simulation + `--simulate`** |
| Exe possible | oui (opt-in, générateur) | oui (opt-in, PyInstaller) |
| Installateur en une fois | `Installer-RemoteDeploy.ps1` | `Installer-RemoteDeploy-Python.cmd` |
| Antivirus | aucun binaire par défaut | **aucun binaire, jamais** |

Les deux versions partagent les mêmes conventions : `C:\_build\PStools\psexec.exe`,
`C:\ProgramData\_DeployRun`, temporisation 60 s × 1,5 (plafond 900 s), 8 postes en parallèle.

---

## 12. Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| « Python 3 est introuvable » | Python absent ou non ajouté au PATH | `winget install Python.Python.3.12`, ou cochez « Add python.exe to PATH » |
| « le module tkinter est absent » | installation Python sans tcl/tk | réexécutez l'installateur Python, option « tcl/tk and IDLE » |
| Cause : **Injoignable** | poste éteint, pare-feu, port 445 ou 5985 fermé | réveillez le poste, ouvrez le port, vérifiez le réseau (ping) |
| Cause : **Accès refusé** | compte sans droits administrateur sur ce poste, UAC distant, partage `C$` désactivé | utilisez un compte admin du domaine, activez le partage administratif |
| Cause : **Identifiants** | mot de passe expiré, compte verrouillé, `DOMAINE\compte` mal saisi | l'application redemande les identifiants et relance la vague |
| Cause : **Code du script** | le script lui-même renvoie un code ≠ 0 | consultez `_rd_output.log` (bouton « Ouvrir le dernier rapport » puis le journal du poste) |
| Cause : **Délai dépassé** | script trop long | augmentez « Durée max d'exécution » (onglet 2) |
| `psexec.exe introuvable` | chemin incorrect | onglet 2 : corrigez le chemin puis cliquez **Tester** |
| WinRM échoue toujours | service WinRM arrêté, `pywinrm` absent, auth Basic non autorisée | `pip install pywinrm`, ou passez en moteur **PsExec** |
| Rien ne se passe après le double-clic | UAC refusé, ou antivirus qui bloque `pythonw.exe` | relancez `Lancer-RemoteDeploy-Python.cmd console` pour voir le message exact |

Test technique rapide, sans contacter aucun poste :

```bat
python RemoteDeploy.py --selftest
python RemoteDeploy.py --simulate 25 --throttle 8 --delay 1
```

---

## 13. Sécurité et limites

* Le mot de passe n'est **jamais écrit en clair** : soit il reste en mémoire pour la session,
  soit il est chiffré par **DPAPI** (lisible uniquement par votre compte Windows sur ce poste).
* Le partage administratif (`net use \\poste\C$`) est **déconnecté** après chaque poste.
* Le script poussé est **renommé** (`_rd_payload.<extension>`) pour éviter les verrous d'exécution
  (un `.exe` en cours d'exécution empêcherait la mise à jour suivante).
* Limites connues, comme pour la version PowerShell : **pas de suivi temps réel à distance**
  (le suivi est celui du poste pilote) et **pas de reprise automatique après redémarrage d'un poste
  en pleine campagne** (le poste sera simplement relancé à la vague suivante s'il répond).

---

*RemoteDeploy (Python) 1.0.0 — fichiers texte uniquement, rien à signaler pour l'antivirus.*

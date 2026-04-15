# 📚 Suivi de lecture Zotero

Tableau de bord de suivi de lecture connecté à Zotero, fonctionnant entièrement en local.  
Visualisez l'avancement de votre bibliographie en temps réel, sans aucune configuration complexe.

---

## Ce que fait l'outil

L'outil se connecte à votre base Zotero et détecte automatiquement l'avancement de chaque document :

| Statut | Détection |
|---|---|
| ✅ Lu | Tag "lu" dans Zotero, ou clic manuel dans le tableau de bord |
| 📖 En cours | Le PDF contient des annotations |
| 👁 Consulté | Le PDF a été ouvert sans annotation *(Mac uniquement)* |
| ⬜ À lire | Jamais ouvert |

**Autres fonctionnalités**
- Barre de progression par document (basée sur la dernière page annotée)
- Articles organisés par collection et sous-collection Zotero
- Filtres par statut et recherche par titre ou auteur
- Bouton "Tout marquer Lu" par collection
- Mise à jour en direct (sans rechargement de page)
- 📝 Synthèse des annotations par collection, exportable dans Zotero via le connecteur navigateur

> La synthèse compile vos surlignages et commentaires en local.  
> Aucune donnée ne transite par un serveur externe — pertinent pour les ressources protégées (Dalloz, LexisNexis, etc.).

---

## Compatibilité Mac / Windows

| Fonctionnalité | Mac | Windows |
|---|:---:|:---:|
| Détection automatique "En cours" | ✅ | ✅ |
| Détection automatique "Consulté" | ✅ | ❌ * |
| Barre de progression (annotations) | ✅ | ✅ |
| Synthèse des annotations | ✅ | ✅ |
| Mise à jour en temps réel | ✅ | ✅ |

*\* Sur Windows, un document ouvert sans annotation reste en "À lire" jusqu'à modification manuelle.*

---

## Installation

### Mac

1. Cliquez sur le bouton vert **Code → Download ZIP** et décompressez le dossier
2. Double-cliquez sur `Démarrer le suivi.command`
   *(première fois : clic droit → Ouvrir → Ouvrir)*
3. Une fenêtre noire s'ouvre — laissez-la ouverte
4. Votre navigateur s'ouvre automatiquement sur `http://localhost:7777`

### Windows

**Prérequis : installer Python une seule fois**

1. Allez sur [python.org/downloads](https://www.python.org/downloads)
2. Téléchargez et ouvrez l'installateur
3. ⚠️ Cochez **"Add Python to PATH"** avant de cliquer sur Install Now
4. Attendez la fin de l'installation

**Lancement**

1. Téléchargez le dossier (bouton vert **Code → Download ZIP**) et décompressez-le
2. Double-cliquez sur `Démarrer le suivi.bat`
3. Une fenêtre noire s'ouvre — laissez-la ouverte
4. Dans votre navigateur, allez sur `http://localhost:7777`

---

## Prérequis

- Zotero installé sur votre ordinateur
- Python 3 *(présent par défaut sur Mac — à installer sur Windows)*

---

## Aide

En cas de difficulté, glissez les fichiers du dossier dans [Claude](https://claude.ai) (gratuit) et demandez de l'aide — il peut guider pas à pas selon votre configuration.

---

*Développé par Gillan Saleh*

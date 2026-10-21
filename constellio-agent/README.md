# Agent de mise à jour Constellio

Installe automatiquement une nouvelle version de Constellio (fichier `.war`) sur un serveur Linux.
En cas d'erreur, il propose des solutions adaptées au message d'erreur et peut revenir automatiquement
à la version précédente.

## Ce que fait l'agent

1. **Vérifications** : droits root, `.war` valide (et somme SHA-256 si fournie), espace disque, Java, service, Solr
2. **Sauvegarde** du `.war` actuel et du dossier `conf` dans `dossier_sauvegardes/<date>`
3. **Arrêt** du service Constellio
4. **Remplacement** du `.war`, en gardant le propriétaire du fichier
5. **Démarrage** et attente que Constellio réponde sur `url_sante`
6. **En cas d'erreur** :
   - cherche le message dans `erreurs_connues.json` et affiche la cause et les solutions ;
   - si l'erreur est inconnue, envoie l'extrait du journal à Claude pour analyse (mots de passe masqués) ;
   - restaure la version précédente si `restauration_auto = oui`.

## Installation sur le serveur

```bash
sudo mkdir -p /opt/constellio-agent
sudo cp agent_maj.py erreurs_connues.json config.ini.exemple /opt/constellio-agent/
cd /opt/constellio-agent
sudo cp config.ini.exemple config.ini
sudo nano config.ini              # adapter les chemins

# Facultatif : analyse IA (Python 3.10+)
sudo pip3 install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
```

Sans le module `anthropic`, l'agent fonctionne quand même : il utilise seulement la base d'erreurs connues.

## Utilisation

```bash
# Simulation (rien n'est modifié)
sudo python3 agent_maj.py --war /tmp/constellio-11.2.war --dry-run

# Mise à jour réelle
sudo -E python3 agent_maj.py --war /tmp/constellio-11.2.war --sha256 <somme>

# Analyser un journal sans rien installer
sudo -E python3 agent_maj.py --diagnostiquer /opt/constellio/logs/wrapper.log

# Revenir manuellement à une version sauvegardée
sudo python3 agent_maj.py --restaurer /opt/constellio/sauvegardes/20261021-103000
```

`sudo -E` conserve la variable `ANTHROPIC_API_KEY` pour l'analyse IA.

## Ajouter une erreur connue

Ajoute un bloc dans `erreurs_connues.json` :

```json
{
  "titre": "Nom court de l'erreur",
  "motifs": ["texte ou expression régulière présent dans le journal"],
  "cause": "Explication de la cause",
  "solutions": ["Solution 1", "Solution 2"]
}
```

## Confidentialité

Quand l'IA est activée, un extrait du journal (300 lignes par défaut) est envoyé à l'API d'Anthropic,
avec les mots de passe et jetons masqués. Mets `activee = non` dans `[ia]` si les journaux ne doivent pas sortir du serveur.

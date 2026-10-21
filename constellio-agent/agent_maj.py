#!/usr/bin/env python3
"""
Agent de mise à jour automatique de Constellio (serveurs Linux).

Étapes : vérifications -> sauvegarde -> arrêt du service -> remplacement du .war
-> démarrage -> vérification de santé. En cas d'erreur, l'agent cherche le message
dans la base d'erreurs connues (erreurs_connues.json) et propose les solutions.
Si l'erreur est inconnue et que l'IA est activée, le journal est analysé par Claude.

Exemples :
    sudo python3 agent_maj.py --war /tmp/constellio-11.2.war
    sudo python3 agent_maj.py --war /tmp/constellio-11.2.war --dry-run
    python3 agent_maj.py --diagnostiquer /opt/constellio/logs/wrapper.log
    sudo python3 agent_maj.py --restaurer /opt/constellio/sauvegardes/20261021-1030
"""
import argparse
import configparser
import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

DOSSIER_AGENT = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("agent_maj")

PROMPT_SYSTEME = """Tu es un expert de la GED Constellio (application Java/Tomcat avec Solr, \
installée sur Linux sous forme de service, mise à jour par remplacement du fichier .war).
On te donne l'étape de mise à jour qui a échoué et un extrait de journal.
Réponds en français, de façon concise, avec :
1. La cause la plus probable (en t'appuyant sur les lignes du journal).
2. Les solutions, numérotées, de la plus simple à la plus lourde, avec les commandes Linux exactes.
3. Si une restauration de la version précédente est conseillée, dis-le clairement.
Si le journal ne suffit pas pour conclure, indique quelles informations récupérer."""


class ErreurEtape(Exception):
    """Erreur survenue pendant une étape de la mise à jour."""

    def __init__(self, message, details=""):
        super().__init__(message)
        self.details = details


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def executer(commande, dry_run=False):
    log.info("$ %s", " ".join(commande))
    if dry_run:
        return ""
    resultat = subprocess.run(commande, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True)
    if resultat.returncode != 0:
        raise ErreurEtape("La commande '%s' a échoué (code %d)" % (" ".join(commande), resultat.returncode),
                          resultat.stdout)
    return resultat.stdout


def service_actif(service):
    resultat = subprocess.run(["systemctl", "is-active", service], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True)
    return resultat.stdout.strip()


def lire_fin_fichier(chemin, nb_lignes):
    if not chemin or not os.path.isfile(chemin):
        return ""
    with open(chemin, "r", encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-nb_lignes:])


def lire_depuis(chemin, position):
    """Lit ce qui a été ajouté au journal depuis `position` (en octets)."""
    if not chemin or not os.path.isfile(chemin):
        return ""
    with open(chemin, "r", encoding="utf-8", errors="replace") as f:
        if os.path.getsize(chemin) >= position:
            f.seek(position)
        return f.read()


def taille_fichier(chemin):
    return os.path.getsize(chemin) if chemin and os.path.isfile(chemin) else 0


def masquer_secrets(texte):
    """Masque mots de passe et jetons avant d'envoyer un journal à l'IA."""
    texte = re.sub(r"(?i)(password|passwd|mot_de_passe|secret|token|apikey|api_key)\s*[=:]\s*\S+",
                   r"\1=****", texte)
    return re.sub(r"(?i)(authorization:\s*\w+)\s+\S+", r"\1 ****", texte)


def sha256(chemin):
    h = hashlib.sha256()
    with open(chemin, "rb") as f:
        for bloc in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloc)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Étapes de la mise à jour
# ---------------------------------------------------------------------------

def verifier_prerequis(cfg, war, somme_attendue):
    c = cfg["constellio"]
    if os.geteuid() != 0:
        raise ErreurEtape("Permission denied : l'agent doit être lancé en root (sudo).")

    if not os.path.isfile(war) or os.path.getsize(war) == 0:
        raise ErreurEtape("Fichier .war introuvable ou vide : %s" % war)
    if not zipfile.is_zipfile(war):
        raise ErreurEtape("ZipException : le fichier %s n'est pas une archive .war valide (fichier corrompu)." % war)
    with zipfile.ZipFile(war) as archive:
        mauvais = archive.testzip()
        if mauvais:
            raise ErreurEtape("ZipException : entrée corrompue dans le .war (%s)." % mauvais)
    if somme_attendue and sha256(war).lower() != somme_attendue.lower():
        raise ErreurEtape("Checksum mismatch : la somme SHA-256 du .war ne correspond pas à celle attendue.")

    if not os.path.isfile(c["chemin_war"]):
        raise ErreurEtape("Fichier .war actuel introuvable : %s (vérifier chemin_war dans config.ini)." % c["chemin_war"])

    libre_go = shutil.disk_usage(c["dossier_installation"]).free / 1024 ** 3
    minimum = c.getfloat("espace_disque_min_go", 5)
    if libre_go < minimum:
        raise ErreurEtape("No space left on device : %.1f Go libres, %.1f Go requis." % (libre_go, minimum))

    if not shutil.which("java"):
        raise ErreurEtape("java: command not found")

    executer(["systemctl", "cat", c["service"]])

    url_solr = c.get("url_solr", "").strip()
    if url_solr:
        try:
            urllib.request.urlopen(url_solr, timeout=10)
        except urllib.error.HTTPError:
            pass  # Solr répond, même avec un code d'erreur HTTP
        except (urllib.error.URLError, OSError) as e:
            raise ErreurEtape("Solr injoignable (%s) : Connection refused / %s" % (url_solr, e))

    log.info("Prérequis OK (%.1f Go libres).", libre_go)


def sauvegarder(cfg, dry_run):
    c = cfg["constellio"]
    horodatage = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dossier = os.path.join(c["dossier_sauvegardes"], horodatage)
    log.info("Sauvegarde de la version actuelle dans %s", dossier)
    if dry_run:
        return dossier
    os.makedirs(dossier)
    shutil.copy2(c["chemin_war"], os.path.join(dossier, os.path.basename(c["chemin_war"])))
    conf = c.get("dossier_conf", "").strip()
    if conf and os.path.isdir(conf):
        shutil.copytree(conf, os.path.join(dossier, "conf"))
    return dossier


def arreter_service(cfg, dry_run):
    c = cfg["constellio"]
    executer(["systemctl", "stop", c["service"]], dry_run)
    if dry_run:
        return
    limite = time.time() + c.getint("delai_arret_s", 180)
    while time.time() < limite:
        if service_actif(c["service"]) != "active":
            log.info("Service arrêté.")
            return
        time.sleep(3)
    raise ErreurEtape("Timeout : le service %s ne s'arrête pas." % c["service"])


def installer_war(cfg, war, dry_run):
    destination = cfg["constellio"]["chemin_war"]
    log.info("Remplacement de %s par %s", destination, war)
    if dry_run:
        return
    proprietaire = os.stat(destination)
    shutil.copy2(war, destination)
    os.chown(destination, proprietaire.st_uid, proprietaire.st_gid)


def demarrer_service(cfg, dry_run):
    c = cfg["constellio"]
    position = taille_fichier(c.get("fichier_log"))
    executer(["systemctl", "start", c["service"]], dry_run)
    return position


def verifier_sante(cfg, position_log, dry_run):
    c = cfg["constellio"]
    if dry_run:
        return
    url = c["url_sante"]
    limite = time.time() + c.getint("delai_demarrage_s", 600)
    log.info("Attente du démarrage de Constellio (%s)...", url)
    while time.time() < limite:
        if service_actif(c["service"]) == "failed":
            raise ErreurEtape("Le service %s est en échec après le démarrage." % c["service"],
                              lire_depuis(c.get("fichier_log"), position_log))
        try:
            reponse = urllib.request.urlopen(url, timeout=15)
            if reponse.status < 500:
                log.info("Constellio répond (HTTP %d).", reponse.status)
                return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                log.info("Constellio répond (HTTP %d).", e.code)
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(10)
    raise ErreurEtape("Timeout : Constellio ne répond pas sur %s après %s s." % (url, c["delai_demarrage_s"]),
                      lire_depuis(c.get("fichier_log"), position_log))


def restaurer(cfg, dossier_sauvegarde):
    c = cfg["constellio"]
    war_sauvegarde = os.path.join(dossier_sauvegarde, os.path.basename(c["chemin_war"]))
    if not os.path.isfile(war_sauvegarde):
        log.error("Sauvegarde introuvable : %s", war_sauvegarde)
        return False
    log.warning("Restauration de la version précédente depuis %s", dossier_sauvegarde)
    try:
        arreter_service(cfg, False)
        installer_war(cfg, war_sauvegarde, False)
        conf_sauvegarde = os.path.join(dossier_sauvegarde, "conf")
        if c.get("dossier_conf", "").strip() and os.path.isdir(conf_sauvegarde):
            shutil.rmtree(c["dossier_conf"])
            shutil.copytree(conf_sauvegarde, c["dossier_conf"])
        position = demarrer_service(cfg, False)
        verifier_sante(cfg, position, False)
    except ErreurEtape as e:
        log.error("La restauration a échoué : %s", e)
        return False
    log.info("Version précédente restaurée avec succès.")
    return True


# ---------------------------------------------------------------------------
# Diagnostic des erreurs
# ---------------------------------------------------------------------------

def charger_base_erreurs():
    with open(os.path.join(DOSSIER_AGENT, "erreurs_connues.json"), encoding="utf-8") as f:
        return json.load(f)


def chercher_erreurs_connues(texte, base):
    return [e for e in base if any(re.search(m, texte, re.IGNORECASE) for m in e["motifs"])]


def analyser_avec_claude(texte, etape, cfg):
    try:
        import anthropic
    except ImportError:
        log.warning("Module 'anthropic' non installé : pip install anthropic (analyse IA ignorée).")
        return None

    ia = cfg["ia"]
    client = anthropic.Anthropic()  # lit la clé dans ANTHROPIC_API_KEY
    try:
        reponse = client.beta.messages.create(
            model=ia.get("modele", "claude-opus-5-5"),
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high"},
            system=PROMPT_SYSTEME,
            messages=[{
                "role": "user",
                "content": "Étape en échec : %s\n\n<journal>\n%s\n</journal>" % (etape, texte),
            }],
        )
    except anthropic.AuthenticationError:
        log.warning("Clé API Anthropic invalide ou absente (variable ANTHROPIC_API_KEY).")
        return None
    except anthropic.RateLimitError:
        log.warning("Limite de requêtes Anthropic atteinte, réessayer plus tard.")
        return None
    except anthropic.APIStatusError as e:
        log.warning("Erreur de l'API Anthropic (%s) : %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError:
        log.warning("Impossible de joindre l'API Anthropic (proxy / pare-feu ?).")
        return None

    if reponse.stop_reason == "refusal":
        log.warning("L'IA n'a pas pu analyser ce journal.")
        return None
    return "".join(bloc.text for bloc in reponse.content if bloc.type == "text")


def diagnostiquer(texte, etape, cfg, base):
    """Affiche les solutions pour une erreur. Renvoie le rapport sous forme de texte."""
    lignes = ["", "=" * 70, "DIAGNOSTIC - étape en échec : %s" % etape, "=" * 70]
    trouvees = chercher_erreurs_connues(texte, base)
    for erreur in trouvees:
        lignes += ["", "► %s" % erreur["titre"], "  Cause : %s" % erreur["cause"], "  Solutions :"]
        lignes += ["    %d. %s" % (i, s) for i, s in enumerate(erreur["solutions"], 1)]

    ia = cfg["ia"]
    if ia.getboolean("activee", False) and (not trouvees or ia.getboolean("toujours", False)):
        lignes += ["", "► Analyse par l'IA (Claude) :"]
        extrait = "\n".join(texte.splitlines()[-ia.getint("lignes_log_max", 300):])
        analyse = analyser_avec_claude(masquer_secrets(extrait), etape, cfg)
        lignes.append(analyse or "  (analyse IA indisponible)")
    elif not trouvees:
        lignes += ["", "Aucune erreur connue reconnue. Consulter le journal complet ou activer l'IA ([ia] activee = oui)."]

    rapport = "\n".join(lignes)
    print(rapport)
    return rapport


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def mettre_a_jour(cfg, war, somme, dry_run, base):
    c = cfg["constellio"]
    etape, sauvegarde, war_remplace = "vérification des prérequis", None, False
    try:
        verifier_prerequis(cfg, war, somme)
        etape = "sauvegarde"
        sauvegarde = sauvegarder(cfg, dry_run)
        etape = "arrêt du service"
        arreter_service(cfg, dry_run)
        etape = "installation du .war"
        installer_war(cfg, war, dry_run)
        war_remplace = True
        etape = "démarrage du service"
        position = demarrer_service(cfg, dry_run)
        etape = "vérification du démarrage"
        verifier_sante(cfg, position, dry_run)
    except Exception as exc:  # toute erreur doit mener au diagnostic
        log.error("Échec à l'étape '%s' : %s", etape, exc)
        texte = "%s\n%s\n%s" % (exc, getattr(exc, "details", ""),
                                 lire_fin_fichier(c.get("fichier_log"), cfg["ia"].getint("lignes_log_max", 300)))
        diagnostiquer(texte, etape, cfg, base)
        if war_remplace and sauvegarde and not dry_run:
            if c.getboolean("restauration_auto", True):
                restaurer(cfg, sauvegarde)
            else:
                print("\nPour revenir à la version précédente :\n  sudo python3 %s --restaurer %s"
                      % (os.path.abspath(__file__), sauvegarde))
        return 1

    log.info("Mise à jour terminée avec succès%s.", " (simulation)" if dry_run else "")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Agent de mise à jour automatique de Constellio")
    parser.add_argument("--config", default=os.path.join(DOSSIER_AGENT, "config.ini"))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--war", help="Nouveau fichier .war à installer")
    action.add_argument("--diagnostiquer", metavar="FICHIER_LOG", help="Analyser un journal sans rien installer")
    action.add_argument("--restaurer", metavar="DOSSIER_SAUVEGARDE", help="Revenir à une version sauvegardée")
    parser.add_argument("--sha256", help="Somme SHA-256 attendue du .war")
    parser.add_argument("--dry-run", action="store_true", help="Simuler sans rien modifier")
    args = parser.parse_args()

    cfg = configparser.ConfigParser()
    if not cfg.read(args.config, encoding="utf-8"):
        sys.exit("Configuration introuvable : %s (copier config.ini.exemple en config.ini)" % args.config)
    if not cfg.has_section("ia"):
        cfg.add_section("ia")

    handlers = [logging.StreamHandler()]
    journal_agent = cfg["constellio"].get("journal_agent", "").strip()
    if journal_agent:
        handlers.append(logging.FileHandler(journal_agent, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)

    base = charger_base_erreurs()
    if args.diagnostiquer:
        texte = lire_fin_fichier(args.diagnostiquer, cfg["ia"].getint("lignes_log_max", 300))
        diagnostiquer(texte, "analyse manuelle de %s" % args.diagnostiquer, cfg, base)
        return 0
    if args.restaurer:
        return 0 if restaurer(cfg, args.restaurer) else 1
    return mettre_a_jour(cfg, args.war, args.sha256, args.dry_run, base)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==========================================================================================
 RemoteDeploy (Python) 1.0.0
 Console de deploiement de script sur postes distants, avec RELANCE AUTOMATIQUE
 jusqu'a ce que TOUS les postes aient pris le script.
==========================================================================================

 Principe
 --------
   1. Vous choisissez un SCRIPT (dans un dossier) et un fichier TXT contenant la LISTE DES POSTES.
   2. L'application vous demande vos IDENTIFIANTS d'administration (compte local ou de domaine),
      saisis une seule fois et conserves en memoire pendant la session.
   3. Le script est copie puis execute sur chaque poste distant (moteur PsExec ou WinRM).
   4. Tous les postes n'ayant pas « pris » le script sont RELANCES AUTOMATIQUEMENT, vague apres
      vague, avec temporisation croissante, jusqu'a ce que TOUS les postes aient pris le script.
   5. Suivi temps reel (grille + journal), export CSV, rapport HTML, journaux par poste.

 Moteurs d'execution
 -------------------
   - PsExec  : C:\\_build\\PStools\\psexec.exe (net use temporaire + partage administratif)
   - WinRM   : necessite le module pywinrm (pip install pywinrm) et WinRM actif sur les postes
   - Auto    : tente WinRM, bascule automatiquement sur PsExec
   - Simulation : aucun contact reseau (test de la boucle de relance)

 Lancement
 ---------
   pythonw RemoteDeploy.py            (sans console, recommande)
   python  RemoteDeploy.py            (avec console, pour le diagnostic)
   Lancer-RemoteDeploy-Python.cmd     (raccourci d'elevation UAC)

 Modes en ligne de commande (sans interface)
 ------------------------------------------
   --selftest                 controles internes
   --simulate N               campagne simulee sur N postes (test de la boucle)
   --ui-test                  construit l'interface, la valide puis se ferme
==========================================================================================
"""

from __future__ import annotations

import argparse
import ctypes
import csv
import html
import json
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

APP_NAME = "RemoteDeploy"
APP_VERSION = "1.0.0"

IS_WINDOWS = (os.name == "nt")
IS_FROZEN = bool(getattr(sys, "frozen", False))

# --- statuts ---
ST_WAIT = "En attente"
ST_RUN = "En cours"
ST_OK = "Réussi"
ST_KO = "Échec"

# --- valeurs par defaut ---
DEFAULT_PS_EXEC = r"C:\_build\PStools\psexec.exe"
DEFAULT_REMOTE_DIR = r"C:\ProgramData\_DeployRun"
PAYLOAD_NAME = "_rd_payload"
WRAPPER_NAME = "_rd_run.cmd"
OUTPUT_NAME = "_rd_output.log"
RC_NAME = "_rd_rc.txt"

SUPPORTED_EXT = (".ps1", ".cmd", ".bat", ".exe", ".vbs")


# ==========================================================================================
#  CHEMINS ET FICHIERS
# ==========================================================================================
def app_dir() -> str:
    """Dossier de l'application (celui du script ou de l'executable)."""
    if IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()


def writable_dir(preferred: str | None = None) -> str:
    """Retourne un dossier accessible en ecriture (pour la configuration et les journaux)."""
    candidats = []
    if preferred:
        candidats.append(preferred)
    candidats.append(app_dir())
    if IS_WINDOWS:
        for var in ("APPDATA", "LOCALAPPDATA", "ProgramData"):
            base = os.environ.get(var)
            if base:
                candidats.append(os.path.join(base, APP_NAME))
    else:
        candidats.append(os.path.join(os.path.expanduser("~"), "." + APP_NAME.lower()))
        candidats.append(os.path.join("/tmp", APP_NAME))
    for c in candidats:
        try:
            os.makedirs(c, exist_ok=True)
            test = os.path.join(c, ".test_ecriture")
            with open(test, "w", encoding="ascii") as fh:
                fh.write("x")
            os.remove(test)
            return c
        except Exception:
            continue
    return os.getcwd()


def ensure_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


def safe_filename(name: str) -> str:
    """Nom de fichier exploitable (nom de poste -> nom de journal)."""
    n = str(name)
    for ch in '\\/:*?"<>|':
        n = n.replace(ch, "_")
    n = "".join(c for c in n if ord(c) >= 32).strip().strip(".")
    return (n[:120] or "inconnu")


def decode_bytes(data: bytes) -> str:
    """Decode une sortie de processus (OEM/ANSI sous Windows, UTF-8 ailleurs)."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for enc in ("utf-8", "cp850", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return repr(data)


def read_text_auto(path: str) -> str:
    """Lit un fichier texte en devinant l'encodage (BOM UTF-8/UTF-16, sinon UTF-8 puis ANSI)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception:
        return ""
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    return decode_bytes(raw)


def sha256_file(path: str) -> str:
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for bloc in iter(lambda: fh.read(1024 * 256), b""):
                h.update(bloc)
        return h.hexdigest().upper()
    except Exception:
        return ""


# ==========================================================================================
#  LECTURE DE LA LISTE DES POSTES
# ==========================================================================================
def read_host_file(path: str) -> tuple[list[str], list[str]]:
    """
    Lit un fichier texte et retourne (postes_uniques, lignes_ignorees).
    Regles : une ligne = un poste ; lignes vides, commentaires (# ou ;), doublons et
    tout ce qui suit ; , tabulation ou espace sont ignores.
    """
    if not path or not os.path.isfile(path):
        raise FileNotFoundError("Fichier introuvable : %s" % path)
    contenu = read_text_auto(path)
    hosts: list[str] = []
    ignores: list[str] = []
    vus: set[str] = set()
    for ligne in contenu.splitlines():
        brut = ligne.strip()
        if not brut:
            continue
        if brut.startswith("#") or brut.startswith(";"):
            ignores.append(brut)
            continue
        premier = re.split(r"[;,\t ]+", brut)[0]
        premier = premier.replace('"', "").replace("'", "").strip()
        if not premier:
            continue
        cle = premier.lower()
        if cle in vus:
            ignores.append(brut)
            continue
        vus.add(cle)
        hosts.append(premier)
    return hosts, ignores


# ==========================================================================================
#  TEMPORISATION, CLASSIFICATION DES ECHECS, DIVERS
# ==========================================================================================
def backoff_seconds(wave: int, base: int = 60, growth: float = 1.5, maximum: int = 900) -> int:
    """Temporisation avant la vague suivante : base * croissance^(vague-1), plafonnee."""
    base = max(1, int(base))
    maximum = max(base, int(maximum))
    growth = max(1.0, float(growth))
    wave = max(1, int(wave))
    return int(round(min(maximum, base * (growth ** (wave - 1)))))


_MOTIFS = (
    ("Identifiants invalides", r"(?i)(logon failure|bad password|unknown user name|user name or password is incorrect|1326|1327|"
                               r"échec de l'ouverture de session|mot de passe incorrect|nom d'utilisateur inconnu|acces refuse au niveau|refus de session)"),
    ("Accès refusé", r"(?i)(access is denied|accès refusé|acces refuse|0x80070005|non autoris|denied)"),
    ("Poste injoignable", r"(?i)(network path was not found|chemin réseau|couldn't access|could not access|the system cannot find|"
                          r"host introuvable|no such host|name resolution|ne peut pas être résolu|not found|injoignable)"),
    ("Service distant indisponible", r"(?i)(rpc server is unavailable|serveur rpc|psexesvc|service.*introuvable|service does not exist|"
                                     r"service cannot be started|échec du démarrage|erreur 1060|1069)"),
    ("WinRM indisponible", r"(?i)(winrm|ws-man|wsman|negotiate|authentication mechanism|trustedhosts|5985|5986)"),
    ("Délai dépassé", r"(?i)(timed out|timeout|délai|delai.*dépass|error establishing communication|n'est pas accessible)"),
    ("Pare-feu / connexion refusée", r"(?i)(firewall|pare-feu|blocked|connection refused|refusé la connexion)"),
)


def classify_failure(text: str, code: int = 0, engine: str = "") -> str:
    """Classe un message d'erreur en categorie lisible."""
    if engine == "Simulation":
        return "Simulation"
    t = str(text or "")
    for nom, motif in _MOTIFS:
        if re.search(motif, t):
            return nom
    if int(code or 0) != 0:
        return "Échec du script"
    return "Erreur" if t.strip() else "Inconnu"


def test_tcp_port(host: str, port: int, timeout: float = 1.5) -> bool:
    """Test rapide d'accessibilite d'un port TCP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(max(0.2, float(timeout)))
    try:
        return s.connect_ex((host, int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def now_str() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def stamp_short() -> str:
    return datetime.now().strftime("%d/%m %H:%M:%S")


# ==========================================================================================
#  EXECUTION DE PROCESSUS
# ==========================================================================================
def run_process(args: list[str], timeout: float = 300, cwd: str | None = None) -> dict:
    """Execute un processus, capture sa sortie, applique un delai maximal."""
    creation = 0
    if IS_WINDOWS:
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    debut = time.time()
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            creationflags=creation,
        )
    except FileNotFoundError:
        return {"code": -1, "out": "", "err": "Programme introuvable : %s" % args[0],
                "timeout": False, "duree": 0.0}
    except Exception as exc:
        return {"code": -1, "out": "", "err": str(exc), "timeout": False, "duree": 0.0}
    try:
        out, err = proc.communicate(timeout=max(1.0, float(timeout)))
        depasse = False
        code = proc.returncode
    except subprocess.TimeoutExpired:
        depasse = True
        try:
            proc.kill()
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = b"", b""
        code = -1
    return {
        "code": code,
        "out": decode_bytes(out),
        "err": decode_bytes(err),
        "timeout": depasse,
        "duree": round(time.time() - debut, 1),
    }


def process_output(res: dict) -> str:
    """Sortie lisible d'un resultat de run_process."""
    texte = ((res.get("out") or "") + "\n" + (res.get("err") or "")).strip()
    return re.sub(r"\n\s*\n+", "\n", texte)


# ==========================================================================================
#  CHIFFREMENT DPAPI (memorisation locale des identifiants, Windows uniquement)
# ==========================================================================================
def _dpapi(protect: bool, data: bytes) -> bytes | None:
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        blob_out = DATA_BLOB()
        fonction = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = fonction(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
        if not ok:
            return None
        resultat = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        kernel32.LocalFree(blob_out.pbData)
        return resultat
    except Exception:
        return None


def dpapi_protect(texte: str) -> str | None:
    """Chiffre une chaine avec DPAPI et retourne du base64 (None si indisponible)."""
    import base64
    brut = _dpapi(True, texte.encode("utf-8"))
    if brut is None:
        return None
    return base64.b64encode(brut).decode("ascii")


def dpapi_unprotect(b64: str) -> str | None:
    """Dechiffre une chaine produite par dpapi_protect."""
    import base64
    try:
        resultat = _dpapi(False, base64.b64decode(b64))
    except Exception:
        return None
    if resultat is None:
        return None
    return decode_bytes(resultat)


# ==========================================================================================
#  ELEVATION DES DROITS
# ==========================================================================================
def is_admin() -> bool:
    """Vrai si le processus courant est eleve (administrateur)."""
    if not IS_WINDOWS:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated(argv: list[str]) -> bool:
    """Relance l'application avec l'elevation UAC. Retourne True si le lancement a ete demande."""
    if not IS_WINDOWS and not sys.platform.startswith("win"):
        return False
    try:
        if IS_FROZEN:
            fichier = sys.executable
            params = subprocess.list2cmdline(argv[1:])
        else:
            fichier = sys.executable
            params = subprocess.list2cmdline([os.path.abspath(argv[0])] + argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", fichier, params, app_dir(), 1)
        return int(rc) > 32
    except Exception:
        return False


# ==========================================================================================
#  MODELES DE DONNEES
# ==========================================================================================
@dataclass
class HostRow:
    """Etat d'un poste pendant la campagne."""
    host: str
    status: str = ST_WAIT
    cause: str = ""
    attempts: int = 0
    engine: str = "-"
    code: str = ""
    duration: str = ""
    timestamp: str = ""
    message: str = "En attente de deploiement."
    logfile: str = ""

    def to_dict(self) -> dict:
        return {
            "Poste": self.host, "Statut": self.status, "Cause": self.cause,
            "Tentatives": str(self.attempts), "Moteur": self.engine, "Code retour": str(self.code),
            "Duree derniere": self.duration, "Horodatage": self.timestamp,
            "Message": self.message, "Journal local": self.logfile,
        }


@dataclass
class Attempt:
    """Resultat d'une tentative sur un poste."""
    ok: bool
    engine: str = "-"
    code: int = -1
    message: str = ""
    output: str = ""


@dataclass
class Settings:
    """Parametres de la campagne."""
    script: str = ""
    args: str = ""
    hostfile: str = ""
    engine: str = "Auto"
    psexec: str = DEFAULT_PS_EXEC
    cred_mode: str = "dedicated"          # "sso" ou "dedicated"
    save_cred: bool = False
    remote_dir: str = DEFAULT_REMOTE_DIR
    cleanup: bool = False
    proof: str = ""
    max_waves: int = 0                     # 0 = illimite
    throttle: int = 8
    wave_delay: int = 60
    delay_growth: float = 1.5
    delay_max: int = 900
    conn_timeout: int = 30
    run_timeout: int = 600
    system: bool = False
    elevate: bool = True
    copy_siblings: bool = False
    sim_fail: bool = True
    log_dir: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        s = cls()
        for cle, valeur in (data or {}).items():
            if hasattr(s, cle):
                try:
                    actuel = getattr(s, cle)
                    if isinstance(actuel, bool):
                        setattr(s, cle, bool(valeur))
                    elif isinstance(actuel, int):
                        setattr(s, cle, int(valeur))
                    elif isinstance(actuel, float):
                        setattr(s, cle, float(valeur))
                    else:
                        setattr(s, cle, str(valeur))
                except Exception:
                    continue
        return s

    def validate(self) -> list[str]:
        """Controles de coherence avant demarrage."""
        erreurs = []
        if not self.script or not os.path.isfile(self.script):
            erreurs.append("Selectionnez le script a deployer (onglet 1).")
        if not self.hostfile or not os.path.isfile(self.hostfile):
            erreurs.append("Selectionnez le fichier .txt contenant la liste des postes (onglet 1).")
        if not re.match(r"^[A-Za-z]:\\[^\s\\]+(\\[^\s\\]+)*$", self.remote_dir or ""):
            erreurs.append(r"Dossier distant invalide : attendu C:\ProgramData\_DeployRun (chemin local absolu, sans espace).")
        if self.engine in ("PsExec", "Auto") and not os.path.isfile(self.psexec):
            if self.engine == "PsExec":
                erreurs.append("psexec.exe est introuvable : %s" % self.psexec)
        if self.engine == "WinRM" and not winrm_available():
            erreurs.append("Le moteur WinRM necessite le module pywinrm (pip install pywinrm).")
        return erreurs



# ==========================================================================================
#  MOTEURS D'EXECUTION
# ==========================================================================================
@dataclass
class Credential:
    username: str
    password: str

    @property
    def display(self) -> str:
        return self.username or "(compte courant)"


def winrm_available() -> bool:
    """Le moteur WinRM necessite le module pywinrm."""
    try:
        import winrm  # noqa: F401
        return True
    except Exception:
        return False


def payload_runner(extension: str) -> tuple[str, str]:
    """Commande d'execution du payload selon son extension."""
    ext = (extension or "").lower()
    if ext == ".ps1":
        return ("powershell.exe -NoProfile -ExecutionPolicy Bypass -File", "ps1")
    if ext == ".vbs":
        return ("cscript.exe //nologo", "vbs")
    if ext == ".exe":
        return ("", "exe")
    return ("call", "cmd")


def build_wrapper(payload_name: str, extension: str, args: str = "") -> str:
    """
    Genere le script _rd_run.cmd qui sera execute sur le poste distant.
    Il ecrit la sortie dans _rd_output.log et le code retour dans _rd_rc.txt.
    """
    prefixe, genre = payload_runner(extension)
    if genre == "ps1":
        ligne = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%%~dp0%s"' % payload_name
    elif genre == "vbs":
        ligne = 'cscript.exe //nologo "%%~dp0%s"' % payload_name
    elif genre == "exe":
        ligne = '"%%~dp0%s"' % payload_name
    else:
        ligne = 'call "%%~dp0%s"' % payload_name
    if args and args.strip():
        ligne += " " + args.strip()
    return "\r\n".join([
        "@echo off",
        "setlocal EnableExtensions",
        'pushd "%~dp0"',
        'echo [RemoteDeploy] debut execution distante %DATE% %TIME% - poste %COMPUTERNAME% > "_rd_output.log"',
        ligne + ' >> "_rd_output.log" 2>&1',
        'set "RD_RC=%ERRORLEVEL%"',
        "popd",
        'echo %RD_RC%> "%~dp0_rd_rc.txt"',
        'if not "%RD_RC%"=="0" echo [RemoteDeploy] code retour %RD_RC% >> "%~dp0_rd_output.log"',
        "exit /b %RD_RC%",
        "",
    ])


@dataclass
class Staging:
    """Package local a pousser sur les postes."""
    directory: str
    files: list[str] = field(default_factory=list)
    payload_name: str = ""
    payload_path: str = ""
    payload_hash: str = ""
    payload_size: int = 0
    version: str = ""


def prepare_staging(settings: Settings, run_dir: str) -> Staging:
    """Prepare le package (payload renomme + lanceur .cmd) qui sera copie sur les postes."""
    import tempfile

    stage = tempfile.mkdtemp(prefix="remotedeploy_")
    extension = os.path.splitext(settings.script)[1].lower()
    if extension not in SUPPORTED_EXT:
        extension = ".cmd"
    payload_name = PAYLOAD_NAME + extension
    payload_path = os.path.join(stage, payload_name)
    shutil.copy2(settings.script, payload_path)

    fichiers = [payload_path]
    if settings.copy_siblings:
        dossier = os.path.dirname(os.path.abspath(settings.script))
        try:
            for nom in sorted(os.listdir(dossier)):
                source = os.path.join(dossier, nom)
                if not os.path.isfile(source) or os.path.abspath(source) == os.path.abspath(settings.script):
                    continue
                if os.path.splitext(nom)[1].lower() in (".bak", ".tmp", ".log"):
                    continue
                cible = os.path.join(stage, nom)
                try:
                    shutil.copy2(source, cible)
                    fichiers.append(cible)
                except Exception:
                    continue
        except Exception:
            pass

    wrapper = os.path.join(stage, WRAPPER_NAME)
    with open(wrapper, "w", encoding="ascii", newline="") as fh:
        fh.write(build_wrapper(payload_name, extension, settings.args))
    fichiers.append(wrapper)

    return Staging(
        directory=stage,
        files=fichiers,
        payload_name=payload_name,
        payload_path=payload_path,
        payload_hash=sha256_file(payload_path),
        payload_size=os.path.getsize(payload_path),
        version=APP_VERSION,
    )


# ------------------------------------------------------------------------------------------
#  PARTAGE ADMINISTRATIF (copie des fichiers sur le poste)
# ------------------------------------------------------------------------------------------
def unc_paths(host: str, remote_dir: str) -> tuple[str, str]:
    """Retourne (racine_unc, dossier_unc) pour un chemin local du type C:\\dossier."""
    lettre = remote_dir[0]
    reste = remote_dir[3:] if len(remote_dir) > 3 else ""
    racine = r"\\%s\%s$" % (host, lettre)
    dossier = os.path.join(racine, reste) if reste else racine
    return racine, dossier


def connect_admin_share(host: str, remote_dir: str, cred: Credential | None,
                        timeout: int, log=None) -> tuple[bool, str, bool]:
    """
    Etablit une session vers le partage administratif du poste (si des identifiants sont fournis).
    Retourne (ok, message, session_ouverte).
    """
    if not cred:
        return True, "", False
    racine, _ = unc_paths(host, remote_dir)
    args = ["net", "use", racine, cred.password, "/user:" + cred.username, "/persistent:no"]
    res = run_process(args, timeout=timeout + 30)
    if res["code"] == 0:
        return True, "", True
    sortie = process_output(res)
    # erreur 1219 : connexions multiples vers le meme serveur -> on supprime puis on reessaie
    if "1219" in sortie or "multiple connexions" in sortie.lower():
        run_process(["net", "use", racine, "/delete", "/y"], timeout=20)
        res = run_process(args, timeout=timeout + 30)
        if res["code"] == 0:
            return True, "", True
        sortie = process_output(res)
    return False, "Identifiants refuses par le poste (net use : %s)" % sortie, False


def disconnect_admin_share(host: str, remote_dir: str, log=None) -> None:
    racine, _ = unc_paths(host, remote_dir)
    try:
        run_process(["net", "use", racine, "/delete", "/y"], timeout=20)
    except Exception:
        pass


def copy_package(host: str, settings: Settings, staging: Staging, log=None) -> tuple[bool, str]:
    """Copie le package dans le dossier distant via le partage administratif."""
    _, dossier_unc = unc_paths(host, settings.remote_dir)
    try:
        os.makedirs(dossier_unc, exist_ok=True)
    except Exception as exc:
        return False, "Copie du script impossible : %s" % exc
    for fichier in staging.files:
        try:
            shutil.copy2(fichier, os.path.join(dossier_unc, os.path.basename(fichier)))
        except Exception as exc:
            return False, "Copie de %s impossible : %s" % (os.path.basename(fichier), exc)
    return True, ""


def read_remote_file(host: str, settings: Settings, nom: str) -> str:
    """Lit un fichier present dans le dossier distant (trace d'execution)."""
    _, dossier_unc = unc_paths(host, settings.remote_dir)
    chemin = os.path.join(dossier_unc, nom)
    try:
        if os.path.isfile(chemin):
            return read_text_auto(chemin)
    except Exception:
        pass
    return ""


def cleanup_remote(host: str, settings: Settings) -> None:
    """Supprime le dossier distant (option de nettoyage)."""
    _, dossier_unc = unc_paths(host, settings.remote_dir)
    try:
        shutil.rmtree(dossier_unc, ignore_errors=True)
    except Exception:
        pass


# ------------------------------------------------------------------------------------------
#  MOTEUR : SIMULATION (aucun contact reseau, pour tester la boucle de relance)
# ------------------------------------------------------------------------------------------
def engine_simulation(host: str, settings: Settings, wave: int) -> Attempt:
    probabilite = 55 if wave <= 1 else 22
    if settings.sim_fail and random.randint(1, 100) <= probabilite:
        motifs = [
            "The network path was not found.",
            "Couldn't access \\\\%s: Access is denied." % host,
            "PsExec could not start cmd.exe on %s: Le service RPC est indisponible." % host,
            "Le script a retourne le code 1 (erreur applicative simulee).",
        ]
        return Attempt(False, "Simulation", 1, random.choice(motifs), "[simulation] aucune action reelle sur le poste.")
    return Attempt(True, "Simulation", 0, "Script pris en compte (simulation).", "[simulation] execution reussie.")


# ------------------------------------------------------------------------------------------
#  MOTEUR : PSEXEC
# ------------------------------------------------------------------------------------------
def engine_psexec(host: str, settings: Settings, cred: Credential | None,
                  staging: Staging) -> Attempt:
    if not IS_WINDOWS:
        return Attempt(False, "PsExec", -1, "Le moteur PsExec necessite Windows (test indisponible sur cette plateforme).", "")
    if not os.path.isfile(settings.psexec):
        return Attempt(False, "PsExec", -1, "psexec.exe introuvable : %s" % settings.psexec, "")
    if not test_tcp_port(host, 445, settings.conn_timeout):
        return Attempt(False, "PsExec", -1, "Poste injoignable : port 445 (partage administratif SMB) inaccessible.", "")

    session = False
    try:
        ok, message, session = connect_admin_share(host, settings.remote_dir, cred, settings.conn_timeout)
        if not ok:
            return Attempt(False, "PsExec", 1326, message, "")

        ok, message = copy_package(host, settings, staging)
        if not ok:
            return Attempt(False, "PsExec", -1, message, "")

        cible = os.path.join(settings.remote_dir, WRAPPER_NAME)
        commande = [settings.psexec, r"\\" + host]
        if cred:
            commande += ["-u", cred.username, "-p", cred.password]
        if settings.elevate:
            commande.append("-h")
        if settings.system:
            commande.append("-s")
        commande += ["-n", str(settings.conn_timeout), "-accepteula", "-nobanner", "-w", settings.remote_dir, cible]

        res = run_process(commande, timeout=settings.conn_timeout + settings.run_timeout + 60)
        sortie = process_output(res)
        if res["timeout"]:
            return Attempt(False, "PsExec", -2, "Delai maximal d'execution depasse (%s s)" % settings.run_timeout, sortie)
        if res["code"] == 0:
            return Attempt(True, "PsExec", 0, "Script execute avec succes sur le poste.", sortie)
        cause = classify_failure(sortie, res["code"], "PsExec")
        return Attempt(False, "PsExec", res["code"], "%s (code %s)" % (cause, res["code"]), sortie)
    except Exception as exc:
        return Attempt(False, "PsExec", -1, "Erreur locale : %s" % exc, "")
    finally:
        if session:
            disconnect_admin_share(host, settings.remote_dir)


# ------------------------------------------------------------------------------------------
#  MOTEUR : WINRM  (copie via le partage administratif, execution via pywinrm)
# ------------------------------------------------------------------------------------------
def engine_winrm(host: str, settings: Settings, cred: Credential | None,
                 staging: Staging) -> Attempt:
    try:
        import winrm  # type: ignore
    except Exception:
        return Attempt(False, "WinRM", -1, "Module pywinrm absent (installez-le : pip install pywinrm).", "")
    if not test_tcp_port(host, 5985, settings.conn_timeout):
        return Attempt(False, "WinRM", -1, "WinRM indisponible : port 5985 inaccessible.", "")
    if not cred:
        return Attempt(False, "WinRM", -1, "Le moteur WinRM necessite des identifiants dedies.", "")

    session = False
    try:
        ok, message, session = connect_admin_share(host, settings.remote_dir, cred, settings.conn_timeout)
        if not ok:
            return Attempt(False, "WinRM", 1326, message, "")
        ok, message = copy_package(host, settings, staging)
        if not ok:
            return Attempt(False, "WinRM", -1, message, "")

        cible = os.path.join(settings.remote_dir, WRAPPER_NAME)
        client = winrm.Session(
            host,
            auth=(cred.username, cred.password),
            transport="ntlm",
            server_cert_validation="ignore",
        )
        resultat = client.run_cmd("cmd.exe", ["/c", cible])
        sortie = decode_bytes(resultat.std_out).strip()
        erreur = decode_bytes(resultat.std_err).strip()
        complet = (sortie + "\n" + erreur).strip()
        if resultat.status_code == 0:
            return Attempt(True, "WinRM", 0, "Script execute avec succes sur le poste (WinRM).", complet)
        cause = classify_failure(complet, resultat.status_code, "WinRM")
        return Attempt(False, "WinRM", resultat.status_code, "%s (code %s)" % (cause, resultat.status_code), complet)
    except Exception as exc:
        texte = re.sub(r"\s+", " ", str(exc)).strip()
        return Attempt(False, "WinRM", -1, "%s : %s" % (classify_failure(texte, -1, "WinRM"), texte), "")
    finally:
        if session:
            disconnect_admin_share(host, settings.remote_dir)


# ------------------------------------------------------------------------------------------
#  MOTEUR : AUTO (WinRM d'abord, puis PsExec)
# ------------------------------------------------------------------------------------------
def engine_auto(host: str, settings: Settings, cred: Credential | None, staging: Staging,
                engine_cache: dict) -> Attempt:
    cache = engine_cache.get(host, "")
    if cache == "PsExec":
        return engine_psexec(host, settings, cred, staging)

    resultat = engine_winrm(host, settings, cred, staging)
    if resultat.ok:
        engine_cache[host] = "WinRM"
        return resultat

    cause = classify_failure(resultat.message, resultat.code, resultat.engine)
    if cause not in ("WinRM indisponible", "Poste injoignable", "Erreur", "Inconnu"):
        engine_cache[host] = "WinRM"
        return resultat

    resultat2 = engine_psexec(host, settings, cred, staging)
    if resultat2.engine == "PsExec" and resultat2.message and "necessite Windows" not in resultat2.message:
        engine_cache[host] = "PsExec"
    return resultat2


# ------------------------------------------------------------------------------------------
#  TENTATIVE COMPLETE SUR UN POSTE (moteur + fichier preuve + nettoyage)
# ------------------------------------------------------------------------------------------
def deploy_attempt(host: str, settings: Settings, cred: Credential | None, staging: Staging,
                   wave: int, engine_cache: dict, log=None) -> Attempt:
    """Execute une tentative complete sur un poste et retourne le resultat detaille."""
    if settings.engine == "Simulation":
        return engine_simulation(host, settings, wave)

    if settings.engine == "PsExec":
        resultat = engine_psexec(host, settings, cred, staging)
    elif settings.engine == "WinRM":
        resultat = engine_winrm(host, settings, cred, staging)
    else:
        resultat = engine_auto(host, settings, cred, staging, engine_cache)

    if not resultat.ok:
        return resultat

    # --- fichier preuve (optionnel) ---
    if settings.proof.strip():
        preuve = settings.proof.strip()
        chemin = preuve
        if not chemin.startswith("\\\\"):
            racine, _ = unc_paths(host, settings.remote_dir)
            chemin = racine + preuve[2:] if len(preuve) > 2 else preuve
        trouve = False
        for _ in range(3):
            try:
                if os.path.exists(chemin):
                    trouve = True
                    break
            except Exception:
                pass
            time.sleep(1)
        if not trouve:
            return Attempt(False, resultat.engine, resultat.code,
                           "Script execute mais aucun fichier preuve sur le poste (%s)" % chemin,
                           resultat.output)

    # --- trace d'execution distante (preuve complementaire) ---
    trace = read_remote_file(host, settings, OUTPUT_NAME)
    if trace:
        resultat.output = (resultat.output + "\n" + trace).strip()

    if settings.cleanup:
        cleanup_remote(host, settings)
    return resultat



# ==========================================================================================
#  CAMPAGNE DE DEPLOIEMENT : vagues successives et RELANCE AUTOMATIQUE
# ==========================================================================================
class Campaign(threading.Thread):
    """
    Orchestre le deploiement :
      - vague 1 sur tous les postes (par paquets de « postes en parallele ») ;
      - les postes en echec sont remis dans la file : vagues suivantes avec temporisation
        croissante, jusqu'a 100 % de reussite (ou limite de vagues, ou arret manuel).
    """

    def __init__(self, settings: Settings, hosts: list[str], credential: Credential | None,
                 log_callback=None):
        super().__init__(daemon=True, name="Campaign")
        self.settings = settings
        self.hosts = list(hosts)
        self.credential = credential
        self.log_callback = log_callback

        self.rows: dict[str, HostRow] = {h: HostRow(h) for h in self.hosts}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()      # positionne = en pause
        self.force_retry = threading.Event()      # positionne = ecourter la temporisation
        self.cred_event = threading.Event()       # signale une saisie d'identifiants
        self.cred_request = False
        self.engine_cache: dict[str, str] = {}

        self.wave = 0
        self.finished = False
        self.started_at = datetime.now()
        self.ended_at: datetime | None = None
        self.next_wave_at = 0.0
        self.log_lines: list[str] = []
        self.last_report = ""
        self.last_csv = ""
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

        base = settings.log_dir or writable_dir()
        self.run_dir = os.path.join(base, self.run_id)
        ensure_dir(self.run_dir)
        self.staging: Staging | None = None
        self.error = ""

    # ------------------------------------------------------------------ journalisation
    def log(self, message: str) -> None:
        ligne = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), message)
        with self.lock:
            self.log_lines.append(ligne)
        if self.log_callback:
            try:
                self.log_callback(ligne)
            except Exception:
                pass

    def _write_host_log(self, host: str, row: HostRow, res: Attempt) -> None:
        chemin = os.path.join(self.run_dir, safe_filename(host) + ".log")
        row.logfile = chemin
        entete = "\n".join([
            "=" * 96,
            "[Vague %d] %s  %s  Poste %s  duree %s" % (self.wave, now_str(), row.status, host, row.duration),
            "Moteur %s - code retour %s - %s" % (res.engine, res.code, row.message),
            "-" * 96,
        ])
        corps = (res.output or "").strip()
        try:
            with open(chemin, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(entete + "\n")
                if corps:
                    fh.write(corps + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------ statistiques
    def stats(self) -> dict:
        with self.lock:
            total = len(self.hosts)
            reussis = sum(1 for h in self.hosts if self.rows[h].status == ST_OK)
            echecs = sum(1 for h in self.hosts if self.rows[h].status == ST_KO)
            encours = sum(1 for h in self.hosts if self.rows[h].status == ST_RUN)
            attente = total - reussis - echecs - encours
            causes: dict[str, int] = {}
            for h in self.hosts:
                r = self.rows[h]
                if r.status == ST_KO:
                    causes[r.cause or "Inconnu"] = causes.get(r.cause or "Inconnu", 0) + 1
        return {"total": total, "reussis": reussis, "echecs": echecs, "encours": encours,
                "attente": max(0, attente), "vague": self.wave, "causes": causes}

    def pending_hosts(self) -> list[str]:
        with self.lock:
            return [h for h in self.hosts if self.rows[h].status != ST_OK]

    def auth_failures(self) -> int:
        with self.lock:
            return sum(1 for h in self.hosts
                       if self.rows[h].status == ST_KO
                       and self.rows[h].cause in ("Identifiants invalides", "Accès refusé"))

    # ------------------------------------------------------------------ pilotage
    def request_stop(self) -> None:
        self.stop_event.set()

    def toggle_pause(self) -> bool:
        """Retourne True si la campagne est desormais en pause."""
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.log("Campagne reprise.")
            return False
        self.pause_event.set()
        self.log("Campagne en pause (les tentatives en cours se terminent).")
        return True

    def request_retry_now(self) -> None:
        self.force_retry.set()

    def set_credential(self, credential: Credential | None) -> None:
        with self.lock:
            self.credential = credential
        self.cred_event.set()
        if credential:
            self.log("Nouveaux identifiants pris en compte : %s" % credential.username)

    # ------------------------------------------------------------------ travailleur unitaire
    def _worker(self, host: str) -> None:
        if self.stop_event.is_set():
            return
        row = self.rows[host]
        with self.lock:
            if row.status == ST_OK:
                return
            row.status = ST_RUN
            row.message = "Tentative en cours..."
            row.attempts = max(row.attempts, self.wave)
            row.timestamp = stamp_short()

        debut = time.time()
        try:
            resultat = deploy_attempt(host, self.settings, self.credential, self.staging,
                                      self.wave, self.engine_cache, self.log)
        except Exception as exc:
            resultat = Attempt(False, "-", -1, "Erreur locale : %s" % exc, "")
        duree = round(time.time() - debut, 1)

        with self.lock:
            row.attempts = self.wave
            row.engine = resultat.engine
            row.code = resultat.code
            row.duration = "%.1f s" % duree
            row.timestamp = stamp_short()
            if resultat.ok:
                row.status = ST_OK
                row.cause = ""
                row.message = resultat.message or "Script execute."
                self.log("OK    %s - %s (%s)" % (host, row.message, row.duration))
            else:
                row.status = ST_KO
                row.cause = classify_failure(resultat.message, resultat.code, resultat.engine)
                row.message = resultat.message
                self.log("ECHEC %s - %s : %s" % (host, row.cause, row.message))
        self._write_host_log(host, row, resultat)

    # ------------------------------------------------------------------ boucle principale
    def run(self) -> None:
        try:
            self.staging = prepare_staging(self.settings, self.run_dir)
            self.log("Campagne %s : %d poste(s) - moteur %s - script %s - v%s" % (
                self.run_id, len(self.hosts), self.settings.engine,
                os.path.basename(self.settings.script), APP_VERSION))
            if self.settings.engine == "Simulation":
                self.log("Mode SIMULATION : aucun contact reel avec les postes.")

            while True:
                if self.stop_event.is_set():
                    self.log("Campagne arretee par l'utilisateur.")
                    break

                # --- pause ---
                while self.pause_event.is_set():
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.25)
                if self.stop_event.is_set():
                    self.log("Campagne arretee par l'utilisateur.")
                    break

                en_attente = self.pending_hosts()
                if not en_attente:
                    self.log("Tous les postes ont pris le script.")
                    break

                self.wave += 1
                self.log("--- Vague %d : %d poste(s) a traiter ---" % (self.wave, len(en_attente)))

                parallele = max(1, min(int(self.settings.throttle or 1), len(en_attente)))
                with ThreadPoolExecutor(max_workers=parallele) as executeur:
                    futurs = [executeur.submit(self._worker, h) for h in en_attente]
                    for futur in as_completed(futurs):
                        try:
                            futur.result()
                        except Exception as exc:
                            self.log("Erreur interne sur un poste : %s" % exc)

                etat = self.stats()
                self.log("--- Fin de la vague %d : %d reussi(s), %d en echec, %d en attente ---" % (
                    self.wave, etat["reussis"], etat["echecs"], etat["attente"]))

                if etat["echecs"] == 0 and etat["attente"] == 0 and etat["encours"] == 0:
                    self.log("Tous les postes ont pris le script (%d/%d)." % (etat["total"], etat["total"]))
                    break
                if self.stop_event.is_set():
                    self.log("Campagne arretee par l'utilisateur.")
                    break

                # --- refus d'identifiants : demander une nouvelle saisie ---
                if self.settings.cred_mode == "dedicated" and self.settings.engine != "Simulation":
                    ko_auth = self.auth_failures()
                    seuil = max(1, -(-etat["echecs"] // 2))     # arrondi superieur
                    if ko_auth > 0 and ko_auth >= seuil:
                        self.log("Identifiants refuses par %d poste(s) : saisie de nouveaux identifiants demandee." % ko_auth)
                        self.cred_event.clear()
                        self.cred_request = True
                        limite = time.time() + 600
                        while not self.cred_event.is_set() and time.time() < limite and not self.stop_event.is_set():
                            time.sleep(0.25)
                        self.cred_request = False
                        self.engine_cache.clear()
                        if not self.cred_event.is_set():
                            self.log("Aucune nouvelle saisie : la campagne continue avec les identifiants actuels.")

                # --- limite de vagues ---
                if int(self.settings.max_waves or 0) > 0 and self.wave >= int(self.settings.max_waves):
                    self.log("Limite de %d vague(s) atteinte : arret avec %d poste(s) non deploye(s)." % (
                        self.settings.max_waves, etat["echecs"]))
                    break

                # --- temporisation avant relance automatique ---
                delai = backoff_seconds(self.wave, self.settings.wave_delay,
                                        self.settings.delay_growth, self.settings.delay_max)
                restants = etat["echecs"] + etat["attente"]
                if delai > 0:
                    self.log("Relance automatique de %d poste(s) dans %s s..." % (restants, delai))
                    self.next_wave_at = time.time() + delai
                    while time.time() < self.next_wave_at:
                        if self.stop_event.is_set():
                            break
                        if self.force_retry.is_set():
                            self.force_retry.clear()
                            self.log("Temporisation ecourtee : relance immediate demandee.")
                            break
                        if self.pause_event.is_set():
                            self.next_wave_at += 0.5
                        time.sleep(0.3)
                    self.next_wave_at = 0.0
        except Exception as exc:
            import traceback
            self.error = str(exc)
            self.log("ERREUR DE LA CAMPAGNE : %s" % exc)
            self.log(traceback.format_exc(limit=3).replace("\n", " | "))
        finally:
            self.ended_at = datetime.now()
            duree = (self.ended_at - self.started_at).total_seconds() / 60.0
            etat = self.stats()
            self.log("Duree totale : %.1f min - %d reussi(s)/%d - %d en echec." % (
                duree, etat["reussis"], etat["total"], etat["echecs"]))
            self.log("RESULTAT : " + ("tous les postes ont pris le script."
                                      if etat["echecs"] == 0
                                      else "%d poste(s) n'ont pas pris le script." % etat["echecs"]))
            try:
                self.last_csv = self.export_csv()
                self.last_report = self.export_report()
                self.log("Exports ecrits : %s | %s" % (self.last_csv, self.last_report))
            except Exception as exc:
                self.log("Export impossible : %s" % exc)
            if self.staging and os.path.isdir(self.staging.directory):
                shutil.rmtree(self.staging.directory, ignore_errors=True)
            self.finished = True

    # ------------------------------------------------------------------ exports
    def ordered_rows(self) -> list[HostRow]:
        with self.lock:
            return [self.rows[h] for h in self.hosts]

    def export_csv(self, chemin: str = "") -> str:
        chemin = chemin or os.path.join(self.run_dir, "resultats.csv")
        with open(chemin, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "Poste", "Statut", "Cause", "Tentatives", "Moteur", "Code retour",
                "Duree derniere", "Horodatage", "Message", "Journal local"], delimiter=";")
            writer.writeheader()
            for row in self.ordered_rows():
                writer.writerow(row.to_dict())
        return chemin

    def export_report(self, chemin: str = "") -> str:
        chemin = chemin or os.path.join(self.run_dir, "rapport.html")
        etat = self.stats()
        esc = html.escape
        causes = "".join("<div class='mono'>%s : <b>%d</b></div>" % (esc(k), v)
                         for k, v in sorted(etat["causes"].items()))
        lignes = []
        for row in self.ordered_rows():
            classe = "ok" if row.status == ST_OK else ("ko" if row.status == ST_KO else "wait")
            lignes.append(
                "<tr class='%s'><td class='mono'><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td>"
                "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class='mono'>%s</td></tr>" % (
                    classe, esc(row.host), esc(row.status), esc(row.cause), esc(str(row.attempts)),
                    esc(row.engine), esc(str(row.code)), esc(row.duration), esc(row.timestamp),
                    esc(row.message)))
        params = [
            ("Moteur", self.settings.engine),
            ("Mode identifiants", "Identifiants dedies" if self.settings.cred_mode == "dedicated" else "Compte Windows courant (SSO)"),
            ("Compte utilise", getattr(self.credential, "username", "") or "(non memorise)"),
            ("Postes en parallele", str(self.settings.throttle)),
            ("Vagues max", "illimite" if not self.settings.max_waves else str(self.settings.max_waves)),
            ("Temporisation", "%s s x %s (max %s s)" % (self.settings.wave_delay, self.settings.delay_growth, self.settings.delay_max)),
            ("Duree max execution", "%s s" % self.settings.run_timeout),
            ("Dossier distant", self.settings.remote_dir),
            ("Execution SYSTEM", "oui" if self.settings.system else "non"),
            ("Fichier preuve", self.settings.proof or "(aucun)"),
            ("Script", self.settings.script),
        ]
        parametres = "".join("<div class='mono'><b>%s :</b> %s</div>" % (esc(k), esc(v)) for k, v in params)
        contenu = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>Rapport de deploiement RemoteDeploy</title><style>
body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#EEF1F5;color:#1B2430}
.wrap{max-width:1180px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 8px}
.sub{color:#6B7688;font-size:12.5px;margin-bottom:18px}
.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:#fff;border:1px solid #DCE3EC;border-radius:8px;padding:12px 16px;min-width:120px}
.card .n{font-size:24px;font-weight:700}.card .l{font-size:11px;color:#6B7688;text-transform:uppercase}
table{width:100%%;border-collapse:collapse;background:#fff;border:1px solid #DCE3EC;font-size:12.5px}
th{background:#F4F7FB;text-align:left;padding:8px 10px;border-bottom:1px solid #DCE3EC;font-size:11.5px;color:#3B4657}
td{padding:7px 10px;border-bottom:1px solid #F0F3F7;vertical-align:top}
tr.ok{background:#F4FBF6}tr.ko{background:#FDF3F2}
.mono{font-family:Consolas,monospace;font-size:11.5px}
.foot{margin-top:22px;color:#6B7688;font-size:11px}
</style></head><body><div class="wrap">
<h1>Rapport de deploiement RemoteDeploy</h1>
<div class="sub">Campagne <b>%s</b> &middot; debut %s &middot; fin %s &middot; version %s</div>
<div class="cards">
<div class="card"><div class="n">%d</div><div class="l">Postes</div></div>
<div class="card"><div class="n" style="color:#1A7F37">%d</div><div class="l">Reussis</div></div>
<div class="card"><div class="n" style="color:#B42318">%d</div><div class="l">En echec</div></div>
<div class="card"><div class="n">%d</div><div class="l">Vagues</div></div>
</div>
<h2>Parametres de la campagne</h2><div class="cards"><div class="card" style="min-width:460px">%s</div></div>
%s
<h2>Detail par poste</h2>
<table><tr><th>Poste</th><th>Statut</th><th>Cause</th><th>Tent.</th><th>Moteur</th><th>Code</th>
<th>Duree</th><th>Horodatage</th><th>Message</th></tr>
%s
</table>
<div class="foot">Journal local : %s &middot; genere par RemoteDeploy %s (Python)</div>
</div></body></html>""" % (
            esc(self.run_id), esc(self.started_at.strftime("%d/%m/%Y %H:%M:%S")),
            esc((self.ended_at or datetime.now()).strftime("%d/%m/%Y %H:%M:%S")), APP_VERSION,
            etat["total"], etat["reussis"], etat["echecs"], etat["vague"],
            parametres,
            ("<h2>Causes des echecs restants</h2><div class='cards'><div class='card' style='min-width:320px'>%s</div></div>" % causes) if causes else "",
            "\n".join(lignes), esc(self.run_dir), APP_VERSION)
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write(contenu)
        return chemin



# ==========================================================================================
#  INTERFACE GRAPHIQUE (tkinter - bibliotheque standard, aucune dependance externe)
# ==========================================================================================
def run_gui(argv: list[str]) -> int:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    CONFIG_NAME = "remotedeploy.config.json"
    CRED_NAME = "remotedeploy.credentials.dat"

    base_dir = app_dir()
    config_path = os.path.join(writable_dir(base_dir), CONFIG_NAME)
    cred_path = os.path.join(writable_dir(base_dir), CRED_NAME)

    # ------------------------------------------------------------------------------------
    #  Dialogue d'identifiants
    # ------------------------------------------------------------------------------------
    class CredentialDialog(tk.Toplevel):
        def __init__(self, parent, username: str = "", motif: str = ""):
            super().__init__(parent)
            self.title("Identifiants d'administration")
            self.resizable(False, False)
            self.transient(parent)
            self.grab_set()
            self.resultat: Credential | None = None
            self.var_user = tk.StringVar(value=username)
            self.var_pwd = tk.StringVar()
            self.var_voir = tk.BooleanVar(value=False)

            cadre = ttk.Frame(self, padding=16)
            cadre.pack(fill="both", expand=True)
            ttk.Label(cadre, text="Identifiants utilises pour atteindre les postes distants",
                      font=("Segoe UI", 11, "bold")).pack(anchor="w")
            ttk.Label(cadre, wraplength=440, foreground="#6B7688",
                      text="Ces identifiants servent a se connecter aux postes distants (copie du script et "
                           "execution). Le mot de passe reste en memoire pendant la session.").pack(anchor="w", pady=(4, 12))
            if motif:
                ttk.Label(cadre, wraplength=440, foreground="#B54708", text=motif).pack(anchor="w", pady=(0, 8))

            ttk.Label(cadre, text=r"Compte (DOMAINE\utilisateur, .\administrateur_local ou poste\utilisateur)").pack(anchor="w")
            entree_user = ttk.Entry(cadre, textvariable=self.var_user, width=54)
            entree_user.pack(fill="x", pady=(2, 10))

            ttk.Label(cadre, text="Mot de passe").pack(anchor="w")
            entree_pwd = ttk.Entry(cadre, textvariable=self.var_pwd, width=54, show="\u2022")
            entree_pwd.pack(fill="x", pady=(2, 6))

            def basculer():
                entree_pwd.configure(show="" if self.var_voir.get() else "\u2022")
            ttk.Checkbutton(cadre, text="Afficher le mot de passe", variable=self.var_voir,
                            command=basculer).pack(anchor="w", pady=(0, 12))

            boutons = ttk.Frame(cadre)
            boutons.pack(fill="x")
            ttk.Button(boutons, text="Valider", style="Accent.TButton", command=self.valider).pack(side="right")
            ttk.Button(boutons, text="Annuler", command=self.annuler).pack(side="right", padx=8)

            self.bind("<Return>", lambda _e: self.valider())
            self.bind("<Escape>", lambda _e: self.annuler())
            entree_user.focus_set()
            if username:
                entree_pwd.focus_set()
            self._centrer(parent)
            self.wait_window(self)

        def _centrer(self, parent):
            self.update_idletasks()
            try:
                x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
                y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
                self.geometry("+%d+%d" % (max(0, x), max(0, y)))
            except Exception:
                pass

        def valider(self):
            utilisateur = self.var_user.get().strip()
            motdepasse = self.var_pwd.get()
            if not utilisateur:
                messagebox.showwarning("Identifiants", "Le nom du compte est obligatoire.", parent=self)
                return
            if not motdepasse and not messagebox.askyesno(
                    "Identifiants", "Le mot de passe est vide. Continuer malgre tout ?", parent=self):
                return
            self.resultat = Credential(utilisateur, motdepasse)
            self.destroy()

        def annuler(self):
            self.resultat = None
            self.destroy()

    # ------------------------------------------------------------------------------------
    #  Fenetre principale
    # ------------------------------------------------------------------------------------
    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("%s %s - Deploiement de script sur postes distants avec relance automatique"
                       % (APP_NAME, APP_VERSION))
            self.geometry("1240x860")
            self.minsize(1080, 720)
            self.configure(bg="#EEF1F5")

            self.script_path = ""
            self.script_folder = ""
            self.hosts: list[str] = []
            self.hosts_ignored: list[str] = []
            self.credential: Credential | None = None
            self.campaign: Campaign | None = None
            self.last_campaign: Campaign | None = None
            self.log_shown = 0
            self.displayed: list[str] = []

            self._init_style()
            self._build_vars()
            self._build_ui()
            self._load_config()
            self._load_stored_credential()
            self._check_environment()

            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(400, self._tick)

            if "--ui-test" in argv:
                self.after(900, self._ui_test_finish)

        # ---------------------------------------------------------------- style
        def _init_style(self):
            self.style = ttk.Style(self)
            for theme in ("vista", "winnative", "clam", "default"):
                try:
                    if theme in self.style.theme_names():
                        self.style.theme_use(theme)
                        break
                except Exception:
                    continue
            self.style.configure(".", font=("Segoe UI", 9))
            self.style.configure("TNotebook.Tab", padding=(14, 7))
            self.style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"))
            self.style.configure("Danger.TButton", font=("Segoe UI", 9, "bold"))
            self.style.configure("Kpi.TLabel", font=("Segoe UI", 16, "bold"))
            self.style.configure("KpiLbl.TLabel", font=("Segoe UI", 8), foreground="#6B7688")
            self.style.configure("Hint.TLabel", foreground="#6B7688", font=("Segoe UI", 8))
            self.style.configure("Titre.TLabel", font=("Segoe UI", 11, "bold"))
            self.style.configure("Treeview", rowheight=22)

        def _build_vars(self):
            self.var_engine = tk.StringVar(value="Auto (WinRM puis PsExec)")
            self.var_psexec = tk.StringVar(value=DEFAULT_PS_EXEC)
            self.var_psexec_info = tk.StringVar(value="")
            self.var_cred_mode = tk.StringVar(value="Identifiants dedies")
            self.var_cred_info = tk.StringVar(value="Identifiants non definis : ils seront demandes au demarrage.")
            self.var_save_cred = tk.BooleanVar(value=False)
            self.var_remote_dir = tk.StringVar(value=DEFAULT_REMOTE_DIR)
            self.var_cleanup = tk.BooleanVar(value=False)
            self.var_proof = tk.StringVar(value="")
            self.var_script_args = tk.StringVar(value="")
            self.var_hostfile = tk.StringVar(value="")
            self.var_host_count = tk.StringVar(value="0 poste")
            self.var_host_ignored = tk.StringVar(value="0 ligne ignoree")
            self.var_max_waves = tk.StringVar(value="0")
            self.var_throttle = tk.StringVar(value="8")
            self.var_wave_delay = tk.StringVar(value="60")
            self.var_delay_growth = tk.StringVar(value="1.5")
            self.var_delay_max = tk.StringVar(value="900")
            self.var_backoff = tk.StringVar(value="")
            self.var_conn_timeout = tk.StringVar(value="30")
            self.var_run_timeout = tk.StringVar(value="600")
            self.var_system = tk.BooleanVar(value=False)
            self.var_elevate = tk.BooleanVar(value=True)
            self.var_copy_siblings = tk.BooleanVar(value=False)
            self.var_sim_fail = tk.BooleanVar(value=True)
            self.var_log_dir = tk.StringVar(value=os.path.join(writable_dir(base_dir), "Journaux"))
            self.var_filter = tk.StringVar(value="Tous les postes")
            self.var_run_info = tk.StringVar(value="Aucune campagne en cours.")
            self.var_eta = tk.StringVar(value="")
            self.var_status = tk.StringVar(value="Pret.")
            self.var_admin = tk.StringVar(value="")
            self.var_kpi = {cle: tk.StringVar(value="0") for cle in ("total", "ok", "ko", "run", "wave")}
            for var in (self.var_wave_delay, self.var_delay_growth, self.var_delay_max):
                var.trace_add("write", lambda *_a: self._update_backoff())

        # ---------------------------------------------------------------- construction
        def _build_ui(self):
            bandeau = ttk.Frame(self, padding=(14, 12, 14, 4))
            bandeau.pack(fill="x")
            gauche = ttk.Frame(bandeau)
            gauche.pack(side="left", fill="x", expand=True)
            ttk.Label(gauche, text="RemoteDeploy", font=("Segoe UI", 15, "bold")).pack(anchor="w")
            ttk.Label(gauche, style="Hint.TLabel",
                      text="Deploiement de script sur postes distants - relance automatique "
                           "jusqu'a reussite de tous les postes (version Python)").pack(anchor="w")
            droit = ttk.Frame(bandeau)
            droit.pack(side="right")
            self.lbl_admin = ttk.Label(droit, textvariable=self.var_admin, padding=(8, 3))
            self.lbl_admin.pack(side="left", padx=4)

            self.nb = ttk.Notebook(self)
            self.nb.pack(fill="both", expand=True, padx=14, pady=(6, 0))
            self._tab_sources()
            self._tab_options()
            self._tab_suivi()
            self._tab_aide()
            self._barre_bas()

        def _tab_sources(self):
            onglet = ttk.Frame(self.nb, padding=10)
            self.nb.add(onglet, text="1 - Script et postes")
            colonne_g = ttk.LabelFrame(onglet, text=" 1. Script a deployer ", padding=10)
            colonne_g.pack(side="left", fill="both", expand=True)
            colonne_d = ttk.LabelFrame(onglet, text=" 2. Liste des postes (fichier .txt) ", padding=10)
            colonne_d.pack(side="left", fill="both", expand=True, padx=(10, 0))

            ligne = ttk.Frame(colonne_g)
            ligne.pack(fill="x")
            self.var_script_folder = tk.StringVar(value="")
            ttk.Entry(ligne, textvariable=self.var_script_folder, state="readonly").pack(side="left", fill="x", expand=True)
            ttk.Button(ligne, text="Parcourir un dossier...", command=self._choisir_dossier_script).pack(side="left", padx=(6, 0))
            ttk.Label(colonne_g, style="Hint.TLabel",
                      text="Selectionnez le dossier contenant vos scripts, puis cliquez sur le script a deployer "
                           "(.ps1 .cmd .bat .exe .vbs).").pack(anchor="w", pady=(6, 2))
            self.liste_scripts = tk.Listbox(colonne_g, height=7, font=("Consolas", 9), activestyle="none")
            self.liste_scripts.pack(fill="x")
            self.liste_scripts.bind("<<ListboxSelect>>", self._choisir_script)
            self.liste_scripts.bind("<Double-Button-1>", lambda _e: self._demarrer())
            self.lbl_script_info = ttk.Label(colonne_g, style="Hint.TLabel", text="Aucun script selectionne.")
            self.lbl_script_info.pack(anchor="w", pady=(6, 0))
            self.apercu = tk.Text(colonne_g, height=8, font=("Consolas", 9), wrap="none", background="#FBFCFE")
            self.apercu.pack(fill="both", expand=True, pady=(4, 6))
            self.apercu.configure(state="disabled")
            ttk.Label(colonne_g, text="Arguments passes au script (optionnel)").pack(anchor="w")
            ttk.Entry(colonne_g, textvariable=self.var_script_args).pack(fill="x")

            ligne = ttk.Frame(colonne_d)
            ligne.pack(fill="x")
            ttk.Entry(ligne, textvariable=self.var_hostfile, state="readonly").pack(side="left", fill="x", expand=True)
            ttk.Button(ligne, text="Parcourir un .txt...", command=self._choisir_fichier_postes).pack(side="left", padx=(6, 0))
            ttk.Button(ligne, text="Recharger", command=self._recharger_postes).pack(side="left", padx=(6, 0))
            ttk.Label(colonne_d, style="Hint.TLabel",
                      text="Un poste par ligne (nom court, FQDN ou IP). Les lignes vides, les commentaires (# ou ;), "
                           "les doublons et tout ce qui suit ; , tabulation ou espace sont ignores.").pack(anchor="w", pady=(6, 2))
            compteurs = ttk.Frame(colonne_d)
            compteurs.pack(fill="x")
            ttk.Label(compteurs, textvariable=self.var_host_count, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 12))
            ttk.Label(compteurs, textvariable=self.var_host_ignored, style="Hint.TLabel").pack(side="left")
            self.apercu_postes = tk.Text(colonne_d, height=18, font=("Consolas", 9), background="#FBFCFE")
            self.apercu_postes.pack(fill="both", expand=True, pady=(6, 0))
            self.apercu_postes.configure(state="disabled")

        def _tab_options(self):
            onglet = ttk.Frame(self.nb, padding=10)
            self.nb.add(onglet, text="2 - Options")
            gauche = ttk.Frame(onglet)
            gauche.pack(side="left", fill="both", expand=True)
            droite = ttk.Frame(onglet)
            droite.pack(side="left", fill="both", expand=True, padx=(10, 0))

            cadre = ttk.LabelFrame(gauche, text=" Moteur d'execution ", padding=10)
            cadre.pack(fill="x")
            ttk.Combobox(cadre, textvariable=self.var_engine, state="readonly", values=[
                "Auto (WinRM puis PsExec)", "PsExec", "WinRM (PowerShell Remoting)",
                "Simulation (aucun contact reseau)"]).pack(fill="x")
            ttk.Label(cadre, style="Hint.TLabel", wraplength=520,
                      text="Auto : tente WinRM et bascule sur PsExec si WinRM est indisponible. "
                           "Simulation : aucun contact reseau, pour tester la boucle de relance.").pack(anchor="w", pady=(4, 8))
            ttk.Label(cadre, text="Chemin de psexec.exe").pack(anchor="w")
            ligne = ttk.Frame(cadre)
            ligne.pack(fill="x", pady=(2, 0))
            ttk.Entry(ligne, textvariable=self.var_psexec).pack(side="left", fill="x", expand=True)
            ttk.Button(ligne, text="...", width=3, command=self._choisir_psexec).pack(side="left", padx=4)
            ttk.Button(ligne, text="Tester", command=self._tester_psexec).pack(side="left")
            ttk.Label(cadre, style="Hint.TLabel", textvariable=self.var_psexec_info).pack(anchor="w", pady=(4, 0))

            cadre = ttk.LabelFrame(gauche, text=" Identifiants d'administration ", padding=10)
            cadre.pack(fill="x", pady=(10, 0))
            ttk.Combobox(cadre, textvariable=self.var_cred_mode, state="readonly", values=[
                "Compte Windows courant (SSO) - aucune saisie",
                "Identifiants dedies (compte administrateur local ou de domaine)"]).pack(fill="x")
            ligne = ttk.Frame(cadre)
            ligne.pack(fill="x", pady=(8, 0))
            ttk.Button(ligne, text="Saisir / modifier les identifiants...", command=self._saisir_identifiants).pack(side="left")
            ttk.Button(ligne, text="Oublier", command=self._oublier_identifiants).pack(side="left", padx=8)
            ttk.Label(cadre, style="Hint.TLabel", textvariable=self.var_cred_info, wraplength=520).pack(anchor="w", pady=(6, 0))
            ttk.Checkbutton(cadre, text="Memoriser les identifiants sur ce poste (chiffrement Windows DPAPI)",
                            variable=self.var_save_cred, command=self._memoriser_identifiants).pack(anchor="w", pady=(6, 0))

            cadre = ttk.LabelFrame(gauche, text=" Dossier distant et controle ", padding=10)
            cadre.pack(fill="x", pady=(10, 0))
            ttk.Entry(cadre, textvariable=self.var_remote_dir).pack(fill="x")
            ttk.Label(cadre, style="Hint.TLabel",
                      text="Le script y est copie sur chaque poste (evitez les espaces dans ce chemin).").pack(anchor="w", pady=(2, 6))
            ttk.Checkbutton(cadre, text="Supprimer le dossier distant apres reussite du poste",
                            variable=self.var_cleanup).pack(anchor="w")
            ttk.Label(cadre, text="Fichier preuve (optionnel)").pack(anchor="w", pady=(6, 0))
            ttk.Entry(cadre, textvariable=self.var_proof).pack(fill="x")
            ttk.Label(cadre, style="Hint.TLabel", wraplength=520,
                      text=r"Ex : C:\ProgramData\_DeployRun\done.flag - le poste n'est declare reussi que si ce fichier "
                           "existe sur le poste.").pack(anchor="w", pady=(2, 0))

            cadre = ttk.LabelFrame(droite, text=" Relance automatique des postes en echec ", padding=10)
            cadre.pack(fill="x")
            grille = ttk.Frame(cadre)
            grille.pack(fill="x")
            for index, (libelle, var, largeur) in enumerate([
                    ("Nombre max de vagues (0 = illimite)", self.var_max_waves, 10),
                    ("Postes en parallele", self.var_throttle, 10),
                    ("Temporisation avant relance (s)", self.var_wave_delay, 10),
                    ("Croissance", self.var_delay_growth, 8),
                    ("Plafond (s)", self.var_delay_max, 8)]):
                ttk.Label(grille, text=libelle).grid(row=index, column=0, sticky="w", pady=2)
                ttk.Entry(grille, textvariable=var, width=largeur).grid(row=index, column=1, sticky="w", padx=8)
            ttk.Label(cadre, style="Hint.TLabel", textvariable=self.var_backoff, wraplength=520).pack(anchor="w", pady=(6, 0))
            ttk.Label(cadre, style="Hint.TLabel", wraplength=520,
                      text="0 vague = relance sans fin jusqu'a ce que TOUS les postes aient pris le script. "
                           "Les postes deja reussis ne sont jamais rejoues.").pack(anchor="w", pady=(4, 0))

            cadre = ttk.LabelFrame(droite, text=" Delais et privileges ", padding=10)
            cadre.pack(fill="x", pady=(10, 0))
            grille = ttk.Frame(cadre)
            grille.pack(fill="x")
            for index, (libelle, var) in enumerate([
                    ("Delai de connexion au poste (s)", self.var_conn_timeout),
                    ("Duree max d'execution (s)", self.var_run_timeout)]):
                ttk.Label(grille, text=libelle).grid(row=index, column=0, sticky="w", pady=2)
                ttk.Entry(grille, textvariable=var, width=10).grid(row=index, column=1, sticky="w", padx=8)
            ttk.Checkbutton(cadre, text="Executer le script en tant que SYSTEM sur le poste (psexec -s)",
                            variable=self.var_system).pack(anchor="w", pady=(6, 0))
            ttk.Checkbutton(cadre, text="Elever au maximum de privileges (psexec -h)",
                            variable=self.var_elevate).pack(anchor="w")
            ttk.Checkbutton(cadre, text="Copier aussi les fichiers du dossier du script (dll, msi, csv...)",
                            variable=self.var_copy_siblings).pack(anchor="w")
            ttk.Checkbutton(cadre, text="Simulation : provoquer des echecs aleatoires (test de la relance)",
                            variable=self.var_sim_fail).pack(anchor="w")

            cadre = ttk.LabelFrame(droite, text=" Journaux et rapports ", padding=10)
            cadre.pack(fill="x", pady=(10, 0))
            ligne = ttk.Frame(cadre)
            ligne.pack(fill="x")
            ttk.Entry(ligne, textvariable=self.var_log_dir).pack(side="left", fill="x", expand=True)
            ttk.Button(ligne, text="...", width=3, command=self._choisir_dossier_journaux).pack(side="left", padx=4)
            ttk.Button(ligne, text="Ouvrir", command=self._ouvrir_journaux).pack(side="left")
            ligne = ttk.Frame(cadre)
            ligne.pack(fill="x", pady=(8, 0))
            ttk.Button(ligne, text="Exporter en CSV", command=self._exporter_csv).pack(side="left")
            ttk.Button(ligne, text="Rapport HTML", command=self._generer_rapport).pack(side="left", padx=6)
            ttk.Button(ligne, text="Ouvrir le dernier rapport", command=self._ouvrir_rapport).pack(side="left")
            ligne = ttk.Frame(cadre)
            ligne.pack(fill="x", pady=(6, 0))
            ttk.Button(ligne, text="Enregistrer la configuration", command=self._sauver_config).pack(side="left")
            ttk.Button(ligne, text="Valeurs par defaut", command=self._valeurs_defaut).pack(side="left", padx=6)

        def _tab_suivi(self):
            onglet = ttk.Frame(self.nb, padding=10)
            self.nb.add(onglet, text="3 - Suivi et resultats")
            haut = ttk.Frame(onglet)
            haut.pack(fill="x")
            ttk.Label(haut, text="Filtre :").pack(side="left")
            combo = ttk.Combobox(haut, textvariable=self.var_filter, state="readonly", width=26, values=[
                "Tous les postes", "Postes en echec restants", "Postes reussis", "Postes en cours"])
            combo.pack(side="left", padx=6)
            combo.bind("<<ComboboxSelected>>", lambda _e: self._remplir_grille(force=True))
            ttk.Label(haut, textvariable=self.var_run_info, style="Hint.TLabel").pack(side="left", padx=14)
            ttk.Button(haut, text="Relancer maintenant les postes en echec", command=self._relancer_maintenant).pack(side="right")
            ttk.Button(haut, text="Copier le journal", command=self._copier_journal).pack(side="right", padx=6)

            colonnes = ("poste", "statut", "cause", "tentatives", "moteur", "code", "duree", "horodatage", "message")
            entetes = ("Poste", "Statut", "Cause", "Tentatives", "Moteur", "Code", "Duree", "Horodatage", "Message")
            largeurs = (150, 90, 130, 80, 80, 55, 70, 90, 380)
            self.grille = ttk.Treeview(onglet, columns=colonnes, show="headings", selectmode="extended")
            for col, entete, largeur in zip(colonnes, entetes, largeurs):
                self.grille.heading(col, text=entete)
                self.grille.column(col, width=largeur, anchor="w")
            barre = ttk.Scrollbar(onglet, orient="vertical", command=self.grille.yview)
            self.grille.configure(yscrollcommand=barre.set)
            barre.pack(side="right", fill="y", pady=(8, 0))
            self.grille.pack(side="top", fill="both", expand=True, pady=(8, 0))
            self.grille.tag_configure("ok", background="#E9F7EF", foreground="#12693A")
            self.grille.tag_configure("ko", background="#FDECEA", foreground="#8A1C12")
            self.grille.tag_configure("run", background="#FFF6E5")
            self.grille.tag_configure("wait", background="#FFFFFF")
            self.grille.bind("<Double-Button-1>", self._ouvrir_journal_poste)

            ttk.Label(onglet, text="Journal d'execution", style="Titre.TLabel").pack(anchor="w", pady=(10, 2))
            self.txt_log = tk.Text(onglet, height=11, font=("Consolas", 9), background="#1E2530",
                                   foreground="#D7E0EA", insertbackground="#D7E0EA", wrap="none")
            self.txt_log.pack(fill="both", expand=False)
            self.txt_log.configure(state="disabled")

        def _tab_aide(self):
            onglet = ttk.Frame(self.nb, padding=10)
            self.nb.add(onglet, text="4 - Aide")
            texte = tk.Text(onglet, wrap="word", font=("Segoe UI", 9), background="#FFFFFF", relief="flat")
            barre = ttk.Scrollbar(onglet, orient="vertical", command=texte.yview)
            texte.configure(yscrollcommand=barre.set)
            barre.pack(side="right", fill="y")
            texte.pack(fill="both", expand=True)
            texte.insert("1.0", AIDE_TEXTE)
            texte.configure(state="disabled")

        def _barre_bas(self):
            cadre = ttk.Frame(self, padding=(14, 8, 14, 12))
            cadre.pack(fill="x")
            self.barre = ttk.Progressbar(cadre, maximum=100, value=0)
            self.barre.pack(fill="x", pady=(0, 8))
            ligne = ttk.Frame(cadre)
            ligne.pack(fill="x")
            gauche = ttk.Frame(ligne)
            gauche.pack(side="left")
            self.btn_start = ttk.Button(gauche, text="Demarrer le deploiement", style="Accent.TButton",
                                        command=self._demarrer)
            self.btn_start.pack(side="left")
            self.btn_pause = ttk.Button(gauche, text="Pause", command=self._pause, state="disabled")
            self.btn_pause.pack(side="left", padx=6)
            self.btn_stop = ttk.Button(gauche, text="Arreter", style="Danger.TButton",
                                       command=self._arreter, state="disabled")
            self.btn_stop.pack(side="left")
            self.btn_admin = ttk.Button(gauche, text="Relancer en administrateur",
                                        command=self._relancer_admin)
            self.btn_admin.pack(side="left", padx=12)

            centre = ttk.Frame(ligne)
            centre.pack(side="left", expand=True)
            for cle, libelle, couleur in (("total", "POSTES", "#1B2430"), ("ok", "REUSSIS", "#1A7F37"),
                                          ("ko", "EN ECHEC", "#B42318"), ("run", "EN COURS", "#B54708"),
                                          ("wave", "VAGUE", "#1B2430")):
                bloc = ttk.Frame(centre)
                bloc.pack(side="left", padx=10)
                ttk.Label(bloc, text=libelle, style="KpiLbl.TLabel").pack(anchor="w")
                ttk.Label(bloc, textvariable=self.var_kpi[cle], style="Kpi.TLabel",
                          foreground=couleur).pack(anchor="w")
            droite = ttk.Frame(ligne)
            droite.pack(side="right")
            ttk.Label(droite, textvariable=self.var_eta, font=("Segoe UI", 9, "bold")).pack(anchor="e")
            ttk.Label(droite, textvariable=self.var_status, style="Hint.TLabel", wraplength=360,
                      justify="right").pack(anchor="e")

        # ---------------------------------------------------------------- actions sources
        def _choisir_dossier_script(self):
            dossier = filedialog.askdirectory(title="Choisissez le dossier contenant vos scripts",
                                              initialdir=self.script_folder or base_dir)
            if dossier:
                self.script_folder = dossier
                self.var_script_folder.set(dossier)
                self._lister_scripts()

        def _lister_scripts(self):
            self.liste_scripts.delete(0, "end")
            try:
                noms = sorted(n for n in os.listdir(self.script_folder)
                              if os.path.isfile(os.path.join(self.script_folder, n))
                              and os.path.splitext(n)[1].lower() in SUPPORTED_EXT)
            except Exception as exc:
                self._journal("Lecture du dossier impossible : %s" % exc)
                noms = []
            for nom in noms:
                self.liste_scripts.insert("end", nom)
            if noms:
                self._journal("%d script(s) disponible(s) dans %s" % (len(noms), self.script_folder))
            else:
                self.lbl_script_info.configure(text="Aucun script (.ps1 .cmd .bat .exe .vbs) dans ce dossier.")

        def _choisir_script(self, _event=None):
            selection = self.liste_scripts.curselection()
            if not selection:
                return
            nom = self.liste_scripts.get(selection[0])
            chemin = os.path.join(self.script_folder, nom)
            self.script_path = chemin
            try:
                taille = os.path.getsize(chemin)
                modifie = datetime.fromtimestamp(os.path.getmtime(chemin)).strftime("%d/%m/%Y %H:%M")
                empreinte = sha256_file(chemin)[:16]
                self.lbl_script_info.configure(
                    text="%s - %.1f Ko - modifie le %s - SHA256 %s..." % (nom, taille / 1024.0, modifie, empreinte))
                contenu = read_text_auto(chemin)
                lignes = contenu.splitlines()[:400]
                self.apercu.configure(state="normal")
                self.apercu.delete("1.0", "end")
                self.apercu.insert("1.0", "\n".join(lignes) or "(fichier binaire ou vide - apercu indisponible)")
                self.apercu.configure(state="disabled")
            except Exception as exc:
                self.lbl_script_info.configure(text="Erreur de lecture : %s" % exc)

        def _choisir_fichier_postes(self):
            chemin = filedialog.askopenfilename(
                title="Choisissez le fichier contenant la liste des postes",
                filetypes=[("Fichiers texte", "*.txt"), ("Tous les fichiers", "*.*")],
                initialdir=os.path.dirname(self.var_hostfile.get()) or base_dir)
            if chemin:
                self._charger_postes(chemin)

        def _recharger_postes(self):
            if self.var_hostfile.get():
                self._charger_postes(self.var_hostfile.get())

        def _charger_postes(self, chemin):
            self.var_hostfile.set(chemin)
            try:
                self.hosts, self.hosts_ignored = read_host_file(chemin)
            except Exception as exc:
                messagebox.showwarning("Liste des postes", "Lecture impossible :\n%s" % exc)
                return
            self.var_host_count.set("%d poste(s)" % len(self.hosts))
            doublons = sum(1 for ligne in self.hosts_ignored if not ligne.strip().startswith(("#", ";")))
            self.var_host_ignored.set("%d ligne(s) ignoree(s) - %d doublon(s)" % (len(self.hosts_ignored), doublons))
            apercu = "\n".join(self.hosts[:80])
            if len(self.hosts) > 80:
                apercu += "\n... et %d autre(s)" % (len(self.hosts) - 80)
            self.apercu_postes.configure(state="normal")
            self.apercu_postes.delete("1.0", "end")
            self.apercu_postes.insert("1.0", apercu or "Aucun poste valide dans ce fichier.")
            self.apercu_postes.configure(state="disabled")
            self._journal("Liste des postes chargee : %d poste(s) depuis %s" % (len(self.hosts), chemin))

        # ---------------------------------------------------------------- actions options
        def _choisir_psexec(self):
            chemin = filedialog.askopenfilename(title="Selectionnez psexec.exe",
                                                filetypes=[("psExec", "psexec.exe"), ("Executables", "*.exe")],
                                                initialdir=os.path.dirname(self.var_psexec.get()) or base_dir)
            if chemin:
                self.var_psexec.set(chemin)
                self._tester_psexec()

        def _tester_psexec(self):
            chemin = self.var_psexec.get().strip()
            if os.path.isfile(chemin):
                taille = os.path.getsize(chemin) / 1024.0
                self.var_psexec_info.set("Trouve : %s (%.0f Ko)" % (chemin, taille))
            else:
                self.var_psexec_info.set("psexec.exe non trouve a cet emplacement.")

        def _saisir_identifiants(self):
            dlg = CredentialDialog(self, self.credential.username if self.credential else os.environ.get("USERNAME", ""))
            if dlg.resultat:
                self.credential = dlg.resultat
                if self.var_save_cred.get():
                    self._memoriser_identifiants()
                self._maj_info_identifiants()
                self._journal("Identifiants definis pour : %s" % dlg.resultat.username)
                if self.campaign and not self.campaign.finished:
                    self.campaign.set_credential(dlg.resultat)

        def _oublier_identifiants(self):
            if not messagebox.askyesno("Identifiants", "Oublier les identifiants en memoire et ceux memorises sur ce poste ?"):
                return
            self.credential = None
            try:
                if os.path.isfile(cred_path):
                    os.remove(cred_path)
            except Exception:
                pass
            self._maj_info_identifiants()
            self._journal("Identifiants oublies.")

        def _memoriser_identifiants(self):
            if not self.credential:
                return
            if not IS_WINDOWS:
                self._journal("Memorisation disponible uniquement sous Windows (DPAPI).")
                return
            chiffre = dpapi_protect(self.credential.password)
            if not chiffre:
                self._journal("Memorisation impossible (DPAPI indisponible).")
                return
            try:
                with open(cred_path, "w", encoding="utf-8") as fh:
                    json.dump({"user": self.credential.username, "pwd": chiffre}, fh)
                self._journal("Identifiants memorises pour ce poste (DPAPI, utilisateur %s)."
                              % os.environ.get("USERNAME", "?"))
            except Exception as exc:
                self._journal("Memorisation impossible : %s" % exc)

        def _load_stored_credential(self):
            try:
                if os.path.isfile(cred_path):
                    with open(cred_path, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    motdepasse = dpapi_unprotect(data.get("pwd", ""))
                    if motdepasse:
                        self.credential = Credential(data.get("user", ""), motdepasse)
                        self.var_save_cred.set(True)
                        self._journal("Identifiants memorises charges pour : %s" % self.credential.username)
            except Exception:
                self._journal("Identifiants memorises illisibles : ressaisie necessaire.")
            self._maj_info_identifiants()

        def _maj_info_identifiants(self):
            if self.credential:
                self.var_cred_info.set("Identifiants enregistres en memoire pour : %s" % self.credential.username)
            else:
                self.var_cred_info.set("Identifiants non definis : ils seront demandes au demarrage de la campagne.")

        def _choisir_dossier_journaux(self):
            dossier = filedialog.askdirectory(title="Dossier de destination des journaux et rapports",
                                              initialdir=self.var_log_dir.get() or base_dir)
            if dossier:
                self.var_log_dir.set(dossier)

        def _ouvrir_journaux(self):
            self._ouvrir_dossier(self.var_log_dir.get())

        def _ouvrir_dossier(self, chemin):
            if not chemin or not os.path.isdir(chemin):
                messagebox.showinfo("Journaux", "Le dossier n'existe pas encore :\n%s" % chemin)
                return
            try:
                if IS_WINDOWS:
                    os.startfile(chemin)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", chemin])
                else:
                    subprocess.Popen(["xdg-open", chemin])
            except Exception as exc:
                self._journal("Ouverture du dossier impossible : %s" % exc)

        def _ouvrir_fichier(self, chemin):
            try:
                if IS_WINDOWS:
                    os.startfile(chemin)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", chemin])
                else:
                    subprocess.Popen(["xdg-open", chemin])
            except Exception as exc:
                self._journal("Ouverture impossible : %s" % exc)

        def _update_backoff(self):
            try:
                base = int(float(self.var_wave_delay.get()))
                croissance = float(self.var_delay_growth.get().replace(",", "."))
                plafond = int(float(self.var_delay_max.get()))
            except Exception:
                return
            valeurs = ["V%d = %ds" % (i, backoff_seconds(i, base, croissance, plafond)) for i in range(1, 5)]
            self.var_backoff.set("Temporisations : " + "  |  ".join(valeurs) + "  (plafond %ds)" % plafond)

        # ---------------------------------------------------------------- configuration
        def _collecter(self) -> Settings:
            moteur = self.var_engine.get()
            if moteur.startswith("PsExec"):
                moteur = "PsExec"
            elif moteur.startswith("WinRM"):
                moteur = "WinRM"
            elif moteur.startswith("Simulation"):
                moteur = "Simulation"
            else:
                moteur = "Auto"
            mode = "sso" if self.var_cred_mode.get().startswith("Compte Windows") else "dedicated"

            def entier(var, defaut):
                try:
                    return int(float(str(var.get()).replace(",", ".")))
                except Exception:
                    return defaut

            def decimal(var, defaut):
                try:
                    return float(str(var.get()).replace(",", "."))
                except Exception:
                    return defaut

            return Settings(
                script=self.script_path,
                args=self.var_script_args.get().strip(),
                hostfile=self.var_hostfile.get(),
                engine=moteur,
                psexec=self.var_psexec.get().strip(),
                cred_mode=mode,
                save_cred=bool(self.var_save_cred.get()),
                remote_dir=self.var_remote_dir.get().strip(),
                cleanup=bool(self.var_cleanup.get()),
                proof=self.var_proof.get().strip(),
                max_waves=max(0, entier(self.var_max_waves, 0)),
                throttle=max(1, entier(self.var_throttle, 8)),
                wave_delay=max(1, entier(self.var_wave_delay, 60)),
                delay_growth=max(1.0, decimal(self.var_delay_growth, 1.5)),
                delay_max=max(1, entier(self.var_delay_max, 900)),
                conn_timeout=max(1, entier(self.var_conn_timeout, 30)),
                run_timeout=max(1, entier(self.var_run_timeout, 600)),
                system=bool(self.var_system.get()),
                elevate=bool(self.var_elevate.get()),
                copy_siblings=bool(self.var_copy_siblings.get()),
                sim_fail=bool(self.var_sim_fail.get()),
                log_dir=self.var_log_dir.get().strip(),
            )

        def _sauver_config(self):
            try:
                with open(config_path, "w", encoding="utf-8") as fh:
                    json.dump(self._collecter().to_dict(), fh, indent=2, ensure_ascii=False)
                self._journal("Configuration enregistree : %s" % config_path)
            except Exception as exc:
                self._journal("Enregistrement impossible : %s" % exc)

        def _load_config(self):
            if not os.path.isfile(config_path):
                self._update_backoff()
                return
            try:
                with open(config_path, "r", encoding="utf-8") as fh:
                    s = Settings.from_dict(json.load(fh))
            except Exception as exc:
                self._journal("Configuration illisible : %s" % exc)
                return
            correspondance = {"PsExec": "PsExec", "WinRM": "WinRM (PowerShell Remoting)",
                              "Simulation": "Simulation (aucun contact reseau)"}
            self.var_engine.set(correspondance.get(s.engine, "Auto (WinRM puis PsExec)"))
            self.var_cred_mode.set("Identifiants dedies (compte administrateur local ou de domaine)"
                                   if s.cred_mode == "dedicated" else "Compte Windows courant (SSO) - aucune saisie")
            self.var_psexec.set(s.psexec or DEFAULT_PS_EXEC)
            self.var_remote_dir.set(s.remote_dir or DEFAULT_REMOTE_DIR)
            self.var_proof.set(s.proof)
            self.var_script_args.set(s.args)
            self.var_log_dir.set(s.log_dir or self.var_log_dir.get())
            self.var_max_waves.set(str(s.max_waves))
            self.var_throttle.set(str(s.throttle))
            self.var_wave_delay.set(str(s.wave_delay))
            self.var_delay_growth.set(str(s.delay_growth))
            self.var_delay_max.set(str(s.delay_max))
            self.var_conn_timeout.set(str(s.conn_timeout))
            self.var_run_timeout.set(str(s.run_timeout))
            self.var_cleanup.set(s.cleanup)
            self.var_system.set(s.system)
            self.var_elevate.set(s.elevate)
            self.var_copy_siblings.set(s.copy_siblings)
            self.var_sim_fail.set(s.sim_fail)
            self.var_save_cred.set(s.save_cred)
            if s.script and os.path.isfile(s.script):
                self.script_folder = os.path.dirname(s.script)
                self.var_script_folder.set(self.script_folder)
                self._lister_scripts()
                nom = os.path.basename(s.script)
                try:
                    index = list(self.liste_scripts.get(0, "end")).index(nom)
                    self.liste_scripts.selection_set(index)
                    self.liste_scripts.see(index)
                    self._choisir_script()
                except Exception:
                    pass
            if s.hostfile and os.path.isfile(s.hostfile):
                self._charger_postes(s.hostfile)
            self._update_backoff()
            self.var_save_cred.set(s.save_cred)
            self._journal("Configuration chargee : %s" % config_path)

        def _valeurs_defaut(self):
            if not messagebox.askyesno("Configuration", "Retablir tous les parametres par defaut ?"):
                return
            self.var_engine.set("Auto (WinRM puis PsExec)")
            self.var_psexec.set(DEFAULT_PS_EXEC)
            self.var_remote_dir.set(DEFAULT_REMOTE_DIR)
            self.var_proof.set("")
            self.var_max_waves.set("0")
            self.var_throttle.set("8")
            self.var_wave_delay.set("60")
            self.var_delay_growth.set("1.5")
            self.var_delay_max.set("900")
            self.var_conn_timeout.set("30")
            self.var_run_timeout.set("600")
            self.var_cleanup.set(False)
            self.var_system.set(False)
            self.var_elevate.set(True)
            self.var_copy_siblings.set(False)
            self.var_sim_fail.set(True)
            self.var_script_args.set("")
            self._update_backoff()
            self._tester_psexec()
            self._journal("Parametres par defaut retablis.")

        # ---------------------------------------------------------------- campagne
        def _demarrer(self):
            reglages = self._collecter()
            erreurs = reglages.validate()
            if not self.hosts:
                erreurs.append("Aucun poste dans la liste.")
            if reglages.cred_mode == "dedicated" and not self.credential:
                dlg = CredentialDialog(self, os.environ.get("USERNAME", ""),
                                       "Mode « identifiants dedies » : saisissez le compte administrateur "
                                       "a utiliser sur les postes distants.")
                if not dlg.resultat:
                    messagebox.showwarning("Deploiement", "Deploiement annule : identifiants non fournis.")
                    return
                self.credential = dlg.resultat
                if self.var_save_cred.get():
                    self._memoriser_identifiants()
                self._maj_info_identifiants()
            if reglages.save_cred and self.credential:
                self._memoriser_identifiants()
            if erreurs:
                messagebox.showwarning("Deploiement", "Corrigez ces points avant de demarrer :\n\n- " + "\n- ".join(erreurs))
                self.nb.select(0)
                return

            # reinitialisation de la vue
            for item in self.grille.get_children():
                self.grille.delete(item)
            self.displayed = []
            self.txt_log.configure(state="normal")
            self.txt_log.delete("1.0", "end")
            self.txt_log.configure(state="disabled")
            self.log_shown = 0
            self.nb.select(2)

            self.campaign = Campaign(reglages, self.hosts, self.credential, log_callback=None)
            self.last_campaign = self.campaign
            for h in self.hosts:
                self.grille.insert("", "end", iid=h, values=(h, ST_WAIT, "", "0", "-", "", "", "", "En attente de deploiement."),
                                   tags=("wait",))
            self.displayed = list(self.hosts)
            self.campaign.start()
            self._maj_boutons()
            self._journal("=== Campagne %s demarree : %d poste(s), moteur %s, script %s" % (
                self.campaign.run_id, len(self.hosts), reglages.engine, os.path.basename(reglages.script)))

        def _pause(self):
            if self.campaign:
                en_pause = self.campaign.toggle_pause()
                self.btn_pause.configure(text="Reprendre" if en_pause else "Pause")

        def _arreter(self):
            if not self.campaign or self.campaign.finished:
                return
            if not messagebox.askyesno("Arreter", "Arreter la campagne en cours ?\n\n"
                                                  "Les postes deja reussis sont conserves, les tentatives en cours "
                                                  "seront interrompues."):
                return
            self.campaign.request_stop()
            self._journal("Arret demande : fin des tentatives en cours...")

        def _relancer_maintenant(self):
            if self.campaign and not self.campaign.finished:
                self.campaign.request_retry_now()
                self._journal("Relance immediate demandee.")

        def _maj_boutons(self):
            actif = bool(self.campaign) and not self.campaign.finished
            self.btn_start.configure(state="disabled" if actif else "normal")
            self.btn_pause.configure(state="normal" if actif else "disabled")
            self.btn_stop.configure(state="normal" if actif else "disabled")
            if not actif:
                self.btn_pause.configure(text="Pause")

        # ---------------------------------------------------------------- boucle d'interface
        def _tick(self):
            try:
                self._rafraichir()
            except Exception as exc:
                self._journal("Erreur d'interface : %s" % exc)
            self.after(400, self._tick)

        def _rafraichir(self):
            campagne = self.campaign
            if not campagne:
                return

            # --- saisie d'identifiants demandee par la campagne ---
            if campagne.cred_request:
                campagne.cred_request = False
                dlg = CredentialDialog(
                    self, self.credential.username if self.credential else "",
                    "Une partie des postes a refuse les identifiants utilises. Saisissez un compte administrateur "
                    "valide : la campagne reprendra automatiquement.")
                if dlg.resultat:
                    self.credential = dlg.resultat
                    campagne.set_credential(dlg.resultat)
                    if self.var_save_cred.get():
                        self._memoriser_identifiants()
                    self._maj_info_identifiants()
                else:
                    campagne.set_credential(self.credential)
                    self._journal("Aucun nouvel identifiant fourni : la campagne continue avec les actuels.")

            # --- journal ---
            with campagne.lock:
                lignes = list(campagne.log_lines)
            if len(lignes) > self.log_shown:
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", "\n".join(lignes[self.log_shown:]) + "\n")
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
                self.log_shown = len(lignes)

            # --- lignes de la grille ---
            for row in campagne.ordered_rows():
                valeurs = (row.host, row.status, row.cause, str(row.attempts), row.engine,
                           str(row.code), row.duration, row.timestamp, row.message)
                etat = "ok" if row.status == ST_OK else ("ko" if row.status == ST_KO else
                                                         ("run" if row.status == ST_RUN else "wait"))
                try:
                    self.grille.item(row.host, values=valeurs, tags=(etat,))
                except Exception:
                    continue
            self._remplir_grille()

            # --- indicateurs ---
            etat = campagne.stats()
            self.var_kpi["total"].set(str(etat["total"]))
            self.var_kpi["ok"].set(str(etat["reussis"]))
            self.var_kpi["ko"].set(str(etat["echecs"]))
            self.var_kpi["run"].set(str(etat["encours"]))
            self.var_kpi["wave"].set(str(etat["vague"]))
            self.barre.configure(value=min(100, round(100.0 * etat["reussis"] / max(1, etat["total"]))))

            ecoule = (datetime.now() - campagne.started_at)
            self.var_run_info.set("Campagne %s - depart %s - ecoule %s - %d poste(s) en attente" % (
                campagne.run_id, campagne.started_at.strftime("%H:%M:%S"),
                str(ecoule).split(".")[0], etat["attente"]))

            # --- estimation et etat ---
            if campagne.next_wave_at and campagne.next_wave_at > time.time():
                self.var_eta.set("Prochaine relance dans %ds" % int(campagne.next_wave_at - time.time()))
            elif campagne.finished:
                self.var_eta.set("Termine : tous les postes ont pris le script" if etat["echecs"] == 0
                                 else "Termine : %d poste(s) en echec" % etat["echecs"])
            elif etat["attente"] + etat["encours"] > 0:
                faits = etat["reussis"] + etat["echecs"]
                if faits > 0:
                    secondes = max(0.2, ecoule.total_seconds() / faits) * (etat["attente"] + etat["encours"]) * 1.2
                    self.var_eta.set("Estimation : ~%d min %ds" % (int(secondes // 60), int(secondes % 60)))
                else:
                    self.var_eta.set("Deploiement en cours...")
            else:
                self.var_eta.set("")

            if campagne.finished:
                if etat["echecs"] == 0:
                    self.var_status.set("Succes complet : %d/%d poste(s) ont pris le script."
                                        % (etat["reussis"], etat["total"]))
                else:
                    self.var_status.set("Fin de campagne : %d poste(s) n'ont pas pris le script." % etat["echecs"])
            elif campagne.pause_event.is_set():
                self.var_status.set("En pause.")
            elif campagne.stop_event.is_set():
                self.var_status.set("Arret en cours...")
            else:
                self.var_status.set("Vague %d en cours (%d tentative(s) en parallele)."
                                    % (etat["vague"], campagne.settings.throttle))
            self._maj_boutons()

        def _filtre_courant(self) -> str:
            return {"Tous les postes": "tous", "Postes en echec restants": "ko",
                    "Postes reussis": "ok", "Postes en cours": "run"}.get(self.var_filter.get(), "tous")

        def _remplir_grille(self, force: bool = False):
            if not self.campaign:
                return
            mode = self._filtre_courant()
            if mode == "tous":
                cibles = [r.host for r in self.campaign.ordered_rows()]
            else:
                cibles = [r.host for r in self.campaign.ordered_rows()
                          if (mode == "ko" and r.status == ST_KO)
                          or (mode == "ok" and r.status == ST_OK)
                          or (mode == "run" and r.status == ST_RUN)]
            if cibles == self.displayed and not force:
                return
            for item in self.grille.get_children():
                self.grille.delete(item)
            for host in cibles:
                row = self.campaign.rows.get(host)
                if not row:
                    continue
                etat = "ok" if row.status == ST_OK else ("ko" if row.status == ST_KO else
                                                         ("run" if row.status == ST_RUN else "wait"))
                self.grille.insert("", "end", iid=host, values=(
                    row.host, row.status, row.cause, str(row.attempts), row.engine, str(row.code),
                    row.duration, row.timestamp, row.message), tags=(etat,))
            self.displayed = cibles

        # ---------------------------------------------------------------- journaux / exports
        def _journal(self, texte: str):
            ligne = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), texte)
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", ligne + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
            self.log_shown += 1

        def _copier_journal(self):
            try:
                self.clipboard_clear()
                self.clipboard_append(self.txt_log.get("1.0", "end"))
                self.var_status.set("Journal copie dans le presse-papiers.")
            except Exception:
                pass

        def _ouvrir_journal_poste(self, _event=None):
            selection = self.grille.selection()
            if not selection or not self.campaign:
                return
            row = self.campaign.rows.get(selection[0])
            if row and row.logfile and os.path.isfile(row.logfile):
                self._ouvrir_fichier(row.logfile)
            else:
                messagebox.showinfo("Journal", "Aucun journal pour ce poste : il n'a pas encore ete traite.")

        def _exporter_csv(self):
            if not self.last_campaign:
                messagebox.showinfo("Export", "Aucune campagne a exporter pour le moment.")
                return
            try:
                chemin = self.last_campaign.export_csv()
                self._journal("Export CSV : %s" % chemin)
                self._ouvrir_fichier(chemin)
            except Exception as exc:
                messagebox.showerror("Export", "Export CSV impossible :\n%s" % exc)

        def _generer_rapport(self):
            if not self.last_campaign:
                messagebox.showinfo("Rapport", "Aucune campagne a rapporter pour le moment.")
                return
            try:
                chemin = self.last_campaign.export_report()
                self._journal("Rapport HTML : %s" % chemin)
                self._ouvrir_fichier(chemin)
            except Exception as exc:
                messagebox.showerror("Rapport", "Generation impossible :\n%s" % exc)

        def _ouvrir_rapport(self):
            if self.last_campaign and self.last_campaign.last_report and os.path.isfile(self.last_campaign.last_report):
                self._ouvrir_fichier(self.last_campaign.last_report)
            else:
                messagebox.showinfo("Rapport", "Aucun rapport disponible pour l'instant.")

        # ---------------------------------------------------------------- environnement
        def _check_environment(self):
            if is_admin():
                self.var_admin.set("Administrateur : oui")
                self.btn_admin.configure(state="disabled")
            else:
                self.var_admin.set("Administrateur : NON")
                self.btn_admin.configure(state="normal")
                self._journal("ATTENTION : l'application n'est pas elevee. Deployez avec des droits "
                              "administrateur (bouton « Relancer en administrateur »).")
            self._tester_psexec()
            if self.var_psexec_info.get().startswith("psexec.exe non trouve"):
                self._journal("psexec.exe introuvable a l'emplacement par defaut (%s) : corrigez le chemin "
                              "dans l'onglet 2 (le moteur PsExec en depend)." % DEFAULT_PS_EXEC)
            if not winrm_available():
                self._journal("Module pywinrm absent : le moteur WinRM est inactif (pip install pywinrm). "
                              "Le moteur PsExec reste pleinement fonctionnel.")
            self._journal("%s %s pret. %s - poste local : %s - utilisateur : %s"
                          % (APP_NAME, APP_VERSION, now_str(), os.environ.get("COMPUTERNAME", "?"),
                             os.environ.get("USERNAME", "?")))

        def _relancer_admin(self):
            if not IS_WINDOWS:
                return
            if relaunch_elevated([sys.argv[0]] + [a for a in argv if a != "--ui-test"]):
                self.destroy()
            else:
                messagebox.showwarning("Administrateur", "L'elevation des droits a ete refusee.")

        def _on_close(self):
            if self.campaign and not self.campaign.finished:
                if not messagebox.askyesno("Fermeture", "Une campagne est en cours. La fermer va l'arreter.\n\n"
                                                        "Fermer quand meme ?"):
                    return
                self.campaign.request_stop()
                for _ in range(50):
                    if self.campaign.finished:
                        break
                    self.update()
                    time.sleep(0.1)
            try:
                with open(config_path, "w", encoding="utf-8") as fh:
                    json.dump(self._collecter().to_dict(), fh, indent=2, ensure_ascii=False)
            except Exception:
                pass
            self.destroy()

        def _ui_test_finish(self):
            """Mode --ui-test : construit l'interface, la met a jour puis se ferme."""
            self._journal("[test] Interface construite et operationnelle.")
            self.update_idletasks()
            self.update()
            self.after(200, self.destroy)

    app = App()
    app.mainloop()
    return 0


AIDE_TEXTE = """MODE D'EMPLOI RAPIDE

1. Onglet 1 : choisissez le dossier contenant vos scripts, puis cliquez sur le script a deployer.
   Choisissez ensuite le fichier .txt qui contient la liste des postes (un poste par ligne).
2. Onglet 2 : verifiez le moteur (Auto convient dans la plupart des cas) et le chemin de psexec.exe.
   Saisissez vos IDENTIFIANTS d'administration : ils seront utilises pour atteindre les postes distants.
   Compte recommande : un compte de domaine membre des « Administrateurs » locaux des postes,
   OU le compte administrateur local de chaque poste (indiquez alors .\\Administrateur).
3. Cliquez sur « Demarrer le deploiement ». L'application copie le script sur chaque poste puis l'execute.
4. Les postes n'ayant pas pris le script (injoignables, acces refuse, erreur du script...) sont
   automatiquement relances par vagues successives, avec une temporisation croissante, jusqu'a ce que
   TOUS les postes soient en reussite (ou jusqu'a la limite de vagues si vous en avez defini une).
5. Suivez l'avancement dans l'onglet 3. Double-cliquez sur une ligne pour ouvrir le journal du poste.
   Exportez le resultat en CSV ou generez un rapport HTML.

POINTS IMPORTANTS

- Le deploiement doit tourner sur un poste avec les droits administrateur.
- PsExec : le partage administratif \\\\poste\\C$ et le service « Serveur » doivent etre accessibles sur
  les postes cibles ; les flux SMB (445) et RPC doivent etre autorises par le pare-feu.
- WinRM : service WinRM actif sur les postes (Enable-PSRemoting -Force) et port 5985 autorise.
  WinRM est preferable car le mot de passe n'apparait jamais en ligne de commande.
  Necessite le module pywinrm :  pip install pywinrm
- PsExec avec identifiants dedies : le mot de passe est transmis en argument du processus psexec.exe
  (visible par un administrateur local du poste de travail pendant l'execution). Pour supprimer ce
  risque, utilisez WinRM, ou lancez la console avec un compte de domaine et choisissez le mode
  « Compte Windows courant (SSO) ».
- Relance : les postes deja reussis ne sont jamais rejoues. La relance ne concerne que les echecs.
- Vos scripts doivent etre idempotents (rejouables sans effet de bord) puisqu'ils peuvent tourner
  plusieurs fois sur un meme poste en cas de relance.
- Si l'application detecte un refus d'acces (« Acces refuse » / « Identifiants invalides »), elle vous
  redemande les identifiants et la campagne reprend automatiquement.

CONFIGURATION ET JOURNAUX

- Toutes les options sont memorisees dans remotedeploy.config.json (bouton « Enregistrer la
  configuration » ou automatiquement a la fermeture).
- Journaux : un fichier par poste et par tentative, dans le dossier indique (onglet 2), plus
  resultats.csv et rapport.html.
- Memorisation des identifiants : chiffrement Windows DPAPI (utilisateur courant uniquement).

LIGNE DE COMMANDE (sans interface)

- python RemoteDeploy.py --selftest      controles internes
- python RemoteDeploy.py --simulate 25   campagne simulee sur 25 postes (test de la relance)

COMPATIBILITE ANTIVIRUS

- Aucun binaire non signe, aucun contenu encode : uniquement des scripts texte et la bibliotheque
  standard de Python. Rien a declarer en exclusion.
"""


# ==========================================================================================
#  MODES LIGNE DE COMMANDE (tests sans interface)
# ==========================================================================================
def run_selftest() -> int:
    """Controles internes : parsing des postes, temporisations, classification, formats."""
    import tempfile

    resultats: list[tuple[str, bool, str]] = []

    def verifier(nom: str, condition: bool, info: str = ""):
        resultats.append((nom, bool(condition), info))

    # --- lecture du fichier de postes ---
    chemin = os.path.join(tempfile.gettempdir(), "rd_hosts_test.txt")
    with open(chemin, "w", encoding="utf-8") as fh:
        fh.write("\n".join([
            "# commentaire", "", "SRV-01", "SRV-02 ; commentaire de fin", "  srv-01  ",
            "10.0.0.15", "srv-03,srv-04", 'PC-COMPTA "BAT-A"', "   ", ""]))
    hosts, ignores = read_host_file(chemin)
    try:
        os.remove(chemin)
    except Exception:
        pass
    verifier("Fichier de postes : 5 postes uniques (doublon ecarte)", len(hosts) == 5, "obtenu : %s" % "|".join(hosts))
    verifier("Fichier de postes : 1er poste = SRV-01", hosts and hosts[0] == "SRV-01")
    verifier("Fichier de postes : separateur ; ignore", "SRV-02" in hosts)
    verifier("Fichier de postes : separateur , -> 1er champ", "srv-03" in hosts)
    verifier("Fichier de postes : guillemets nettoyes", "PC-COMPTA" in hosts)
    verifier("Fichier de postes : commentaires listes", len(ignores) >= 2)

    # --- temporisations ---
    verifier("Backoff vague 1 = 60s", backoff_seconds(1, 60, 1.5, 900) == 60)
    verifier("Backoff vague 2 = 90s", backoff_seconds(2, 60, 1.5, 900) == 90)
    verifier("Backoff vague 10 plafonne a 900s", backoff_seconds(10, 60, 1.5, 900) == 900)

    # --- classification des echecs ---
    verifier("Classif. : identifiants",
             classify_failure("Logon failure: unknown user name or bad password") == "Identifiants invalides")
    verifier("Classif. : acces refuse",
             classify_failure("Couldn't access SRV01: Access is denied.") == "Accès refusé")
    verifier("Classif. : injoignable",
             classify_failure("The network path was not found.") == "Poste injoignable")
    verifier("Classif. : code script", classify_failure("erreur applicative", 1603) == "Échec du script")

    # --- divers ---
    verifier("Nom de fichier securise", safe_filename('SRV-01\\test:1*') == "SRV-01_test_1_")
    verifier("Runner .ps1", payload_runner(".ps1")[1] == "ps1")
    verifier("Runner .cmd", payload_runner(".cmd")[1] == "cmd")
    verifier("Lanceur genere : code retour capture", "_rd_rc.txt" in build_wrapper("_rd_payload.ps1", ".ps1"))
    verifier("Settings : validation detecte le script manquant", bool(Settings().validate()))

    # --- rapport et CSV ---
    camp = Campaign(Settings(engine="Simulation", sim_fail=False), ["PC-TEST"],
                    Credential("TEST", "x"), log_callback=None)
    camp.wave = 1
    camp.rows["PC-TEST"].status = ST_OK
    camp.rows["PC-TEST"].message = "ok"
    try:
        csv_path = camp.export_csv()
        html_path = camp.export_report()
        verifier("Export CSV ecrit", os.path.isfile(csv_path) and os.path.getsize(csv_path) > 50)
        verifier("Rapport HTML ecrit", os.path.isfile(html_path) and os.path.getsize(html_path) > 800)
        try:
            os.remove(csv_path)
            os.remove(html_path)
            shutil.rmtree(camp.run_dir, ignore_errors=True)
        except Exception:
            pass
    except Exception as exc:
        verifier("Exports", False, str(exc))

    reussis = sum(1 for _n, ok, _i in resultats if ok)
    print("%s %s - self-test" % (APP_NAME, APP_VERSION))
    for nom, ok, info in resultats:
        print("  [%s]   %s%s" % ("OK" if ok else "KO", nom, (" (%s)" % info) if info and not ok else ""))
    print("\nResultat : %d reussis, %d echecs" % (reussis, len(resultats) - reussis))
    return 0 if reussis == len(resultats) else 1


def run_simulation(nombre: int, throttle: int = 8, wave_delay: int = 2, max_waves: int = 0) -> int:
    """Campagne simulee sans interface : valide la boucle de relance de bout en bout."""
    hosts = ["POSTE-%02d" % i for i in range(1, max(1, nombre) + 1)]
    dossier = os.path.join(writable_dir(), "Journaux")
    reglages = Settings(engine="Simulation", throttle=throttle, wave_delay=wave_delay,
                        delay_growth=1.0, delay_max=5, max_waves=max_waves,
                        sim_fail=True, log_dir=dossier, script=__file__ if os.path.isfile(__file__) else sys.argv[0])
    camp = Campaign(reglages, hosts, None)
    camp.start()
    while not camp.finished:
        time.sleep(0.3)
    etat = camp.stats()
    print("Simulation terminee : %d/%d reussis en %d vague(s) - %d echec(s)"
          % (etat["reussis"], etat["total"], etat["vague"], etat["echecs"]))
    print("Journaux : %s" % camp.run_dir)
    print("CSV      : %s" % camp.last_csv)
    print("Rapport  : %s" % camp.last_report)
    return 0 if etat["reussis"] == etat["total"] else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    parseur = argparse.ArgumentParser(
        prog="RemoteDeploy", description="Deploiement de script sur postes distants avec relance automatique.")
    parseur.add_argument("--selftest", action="store_true", help="controles internes puis sortie")
    parseur.add_argument("--simulate", type=int, metavar="N", help="campagne simulee sur N postes (sans interface)")
    parseur.add_argument("--throttle", type=int, default=8, help="postes en parallele (mode --simulate)")
    parseur.add_argument("--delay", type=int, default=2, help="temporisation entre vagues en secondes (mode --simulate)")
    parseur.add_argument("--wave-delay", type=int, default=None, help="idem --delay (compatibilite)")
    parseur.add_argument("--max-waves", type=int, default=0, help="nombre max de vagues (0 = illimite)")
    parseur.add_argument("--ui-test", action="store_true", help="construit l'interface puis se ferme (test)")
    parseur.add_argument("--elevate", action="store_true", help="relance l'application en administrateur")
    args = parseur.parse_args(argv[1:])

    if args.selftest:
        return run_selftest()
    if args.simulate:
        return run_simulation(args.simulate, args.throttle,
                              args.wave_delay if args.wave_delay is not None else args.delay,
                              args.max_waves)
    if args.elevate and not is_admin():
        if relaunch_elevated(argv):
            return 0
    try:
        import tkinter  # noqa: F401
    except Exception:
        print("La bibliotheque tkinter est absente : installez-la (Windows : incluse avec Python ; "
              "Debian/Ubuntu : sudo apt install python3-tk).", file=sys.stderr)
        return 2
    return run_gui(argv)


if __name__ == "__main__":
    sys.exit(main())

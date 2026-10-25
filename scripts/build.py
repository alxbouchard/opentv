#!/usr/bin/env python3
"""Construit et signe OpenTV.wgt en une commande.

    OPENTV_KEY=/chemin/opentv-author.p12 OPENTV_KEY_PASSWORD_FILE=/chemin/opentv-author.password.txt \
        python3 scripts/build.py

Pré-requis :
  - Tizen Studio CLI 5.6 (Linux) installé SOUS UN UTILISATEUR NORMAL (l'installeur refuse root) :
      curl -o tizen-installer https://download.tizen.org/sdk/Installer/tizen-studio_5.6/web-cli_Tizen_Studio_5.6_ubuntu-64.bin
      chmod +x tizen-installer && ./tizen-installer --accept-license ~/tizen-studio
    (macOS : web-cli_Tizen_Studio_5.6_macos-64.bin ; chemin du CLI via TIZEN_CLI si différent)
  - Java (OpenJDK 11+), python3, pexpect (pip install pexpect), openssl (pour la vérification).
  - La clé d'auteur OpenTV (.p12) et son mot de passe : HORS du dépôt (voir AGENTS.md §7).
    Toutes les mises à jour doivent être signées avec CETTE clé, sinon la TV refuse de remplacer l'appli.

Signature = même méthode que TizenBrew pour les TV 2017-2018 : auteur OpenTV + distributeur Tizen
public du SDK (aucun compte Samsung, aucun DUID).
Sortie : dist/OpenTV.wgt
"""
import os, re, shutil, subprocess, sys, tempfile, zipfile, base64

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "native")
DIST = os.path.join(ROOT, "dist")


def die(msg):
    print("❌ " + msg)
    sys.exit(1)


def main():
    try:
        import pexpect
    except ImportError:
        die("pexpect manquant : pip install pexpect")

    tizen = os.environ.get("TIZEN_CLI") or os.path.expanduser("~/tizen-studio/tools/ide/bin/tizen")
    if not os.path.exists(tizen):
        die("CLI Tizen introuvable (%s). Installe Tizen Studio CLI (voir l'en-tête de ce script) ou règle TIZEN_CLI." % tizen)
    sdk_certs = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(tizen))), "certificate-generator", "certificates")
    dist_p12 = os.path.join(sdk_certs, "distributor", "sdk-public", "tizen-distributor-signer.p12")
    if not os.path.exists(dist_p12):
        die("certificats du SDK introuvables : " + sdk_certs)

    key = os.environ.get("OPENTV_KEY")
    pw = os.environ.get("OPENTV_KEY_PASSWORD")
    if not pw and os.environ.get("OPENTV_KEY_PASSWORD_FILE"):
        pw = open(os.environ["OPENTV_KEY_PASSWORD_FILE"]).read().strip()
    if not key or not os.path.exists(key):
        die("OPENTV_KEY doit pointer vers opentv-author.p12")
    if not pw:
        die("mot de passe manquant : OPENTV_KEY_PASSWORD ou OPENTV_KEY_PASSWORD_FILE")
    key = os.path.abspath(key)

    work = tempfile.mkdtemp(prefix="opentv-build-")
    try:
        # 1. copie de l'appli sans les fichiers de dev
        app = os.path.join(work, "OpenTV")
        shutil.copytree(SRC, app, ignore=shutil.ignore_patterns("test", "INSTALL.md", ".buildResult", "release", "*.wgt"))
        m = re.search(r'var BUILD = "([^"]+)"', open(os.path.join(app, "app", "app.js")).read())
        print("Version : " + (m.group(1) if m else "?"))

        # 2. profil de signature régénéré à chaque fois (le CLI chiffre le mot de passe après usage
        #    puis n'arrive plus à le relire : « Invaild password »)
        prof = os.path.join(work, "profiles.xml")
        with open(prof, "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
                    '<profiles active="OpenTV-Old" version="3.1">\n<profile name="OpenTV-Old">\n'
                    '<profileitem ca="%s/developer/tizen-developer-ca.cer" distributor="0" key="%s" password="%s" rootca=""/>\n'
                    '<profileitem ca="%s/distributor/sdk-public/tizen-distributor-ca.cer" distributor="1" key="%s" password="tizenpkcs12passfordsigner" rootca=""/>\n'
                    '<profileitem ca="" distributor="2" key="" password="" rootca=""/>\n</profile>\n</profiles>\n'
                    % (sdk_certs, key, pw, sdk_certs, dist_p12))
        subprocess.run([tizen, "cli-config", "profiles.path=" + prof], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 3. build
        r = subprocess.run([tizen, "build-web", "-e", ".*", "--", app], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isdir(os.path.join(app, ".buildResult")):
            die("build-web a échoué :\n" + r.stdout[-1500:] + r.stderr[-1500:])

        # 4. signature (le CLI demande le mot de passe sur un vrai terminal -> pexpect)
        out = os.path.join(work, "release")
        c = pexpect.spawn(tizen, ["package", "-t", "wgt", "-s", "OpenTV-Old", "-o", out, "--", os.path.join(app, ".buildResult")],
                          encoding="utf-8", timeout=300)
        log = ""
        while True:
            i = c.expect(["Author password:", "[Dd]istributor.*password:", r"Yes: \(Y\), No: \(N\)", pexpect.EOF])
            log += c.before
            if i == 0: c.sendline(pw)
            elif i == 1: c.sendline("tizenpkcs12passfordsigner")
            elif i == 2: c.sendline("N")
            else: break
        wgts = [x for x in os.listdir(out)] if os.path.isdir(out) else []
        if not wgts:
            die("la signature a échoué :\n" + "\n".join(l for l in log.splitlines() if "JAVA_TOOL" not in l)[-1500:])

        # 5. vérification
        os.makedirs(DIST, exist_ok=True)
        dst = os.path.join(DIST, "OpenTV.wgt")
        shutil.copy(os.path.join(out, wgts[0]), dst)
        z = zipfile.ZipFile(dst)
        names = z.namelist()
        for need in ("config.xml", "author-signature.xml", "signature1.xml", "app/index.html", "service/index.js", "icon.png"):
            if need not in names:
                die("fichier manquant dans le .wgt : " + need)
        for sig, expect in (("author-signature.xml", "Tizen Developers CA"), ("signature1.xml", "Tizen Public Distributor CA")):
            certs = re.findall(r"<X509Certificate>\s*([^<]+)</X509Certificate>", z.read(sig).decode())
            subjects = ""
            for cert in certs:
                der = base64.b64decode("".join(cert.split()))
                subjects += subprocess.run(["openssl", "x509", "-inform", "DER", "-noout", "-subject", "-issuer"],
                                           input=der, capture_output=True).stdout.decode(errors="replace")
            if expect not in subjects:
                die("%s : chaîne inattendue (attendu « %s »)" % (sig, expect))
        print("✅ %s (%d Ko) — signé auteur OpenTV + distributeur Tizen public" % (os.path.relpath(dst, ROOT), os.path.getsize(dst) // 1024))
        print("   Installer : scripts/deploy.sh <IP_TV> %s" % os.path.relpath(dst, ROOT))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

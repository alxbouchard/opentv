"""Blank X home: anonymous local spinner, native Retry, lazy/virtual DOM and explicit remote recovery.
No live X/account/SSH. Sequential full Chromium only. OPENTV_TEST_SHOTS keeps JSON + screenshots.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from opentv_pi import VERSION
from tests._common import PI4
from tests.test_x_tv import EXT, EXT_DST, INSTALL, NODE, full_chrome, sh

PROD = "4e209869a78e0a3feddd3c0d1b7110ec8fdd42a3"
REPO = Path(PI4).parents[1]


class TestEmptyVersion(unittest.TestCase):
    def test_recovery_release_is_distinguishable(self):
        self.assertEqual(VERSION, "0.7.19")
        self.assertEqual(json.loads((Path(EXT) / "manifest.json").read_text())["version"], VERSION)


@unittest.skipUnless(NODE and os.environ.get("OPENTV_TEST_PLAYWRIGHT") and full_chrome(),
                     "requires full Chromium, node and OPENTV_TEST_PLAYWRIGHT")
class TestEmptyEndToEnd(unittest.TestCase):
    def test_recycled_action_and_loading_article(self):
        chrome = full_chrome()
        assert NODE and chrome
        rejected = '689c9dd655ca30d9f06509a05e7ef5efca38e5f3'
        with tempfile.TemporaryDirectory(prefix='otvx-recycle-') as tmp:
            reviewed = '298e0c6c2fad7bb039e4f7950ffca0437eaa3a19'
            share_fix = '260177730326af97e6e8942d1fa4e6231be46439'
            for rev in (PROD, rejected, reviewed, share_fix, 'candidate'):
                with self.subTest(rev=rev):
                    work = Path(tmp) / rev
                    work.mkdir()
                    installer = INSTALL
                    if rev != 'candidate':
                        archive = subprocess.check_output(['git', '-C', str(REPO), 'archive', rev])
                        subprocess.run(['tar', '-x', '-C', str(work)], input=archive, check=True)
                        installer = str(work / 'scripts/pi4/install.sh')
                    root = work / 'rootfs'
                    installed = sh([installer, '--root', str(root), '--skip-preflight'])
                    self.assertEqual(installed.returncode, 0, installed.stderr.decode()[-2000:])
                    r = subprocess.run([NODE, str(Path(PI4) / 'tests/e2e_x_recycled.js'),
                                        os.environ['OPENTV_TEST_PLAYWRIGHT'], chrome, str(root / EXT_DST.lstrip('/'))],
                                       capture_output=True, timeout=90)
                    self.assertEqual(r.returncode, 0, r.stderr.decode())
                    result = json.loads(r.stdout)
                    if os.environ.get('OPENTV_TEST_SHOTS'):
                        dest = Path(os.environ['OPENTV_TEST_SHOTS'])
                        dest.mkdir(parents=True, exist_ok=True)
                        (dest / ('recycled-' + rev + '.json')).write_bytes(r.stdout)
                    self.assertEqual(result['errors'], [])
                    for name in ('before-single', 'before-repeat', 'after-single', 'after-repeat'):
                        first = [['action', True, '/fixture/status/1']] if name.endswith('repeat') else []
                        wrong = [['action', True, '/fixture/status/2']]
                        self.assertEqual(result['recycle'][name], first + (wrong if rev == rejected else []), name)
                        leaked = rev == rejected or (rev in (reviewed, share_fix) and name.startswith('after'))
                        self.assertEqual(result['unmarked'][name], first + (wrong if leaked else []), name)
                    good = rev in (reviewed, share_fix, 'candidate')
                    self.assertEqual(result['lateNative'], [] if good else [['action', True, '/fixture/status/2']])
                    self.assertEqual(result['shellUp'], dict(selected='' if good else 'shell', focus='',
                                     reload=good, recovery=good, feed=not good))
                    self.assertEqual(result['shellRetry']['focus'], 'retry' if good else '')
                    self.assertEqual(result['mixed']['selected'], 'post' if good else 'shell')
                    self.assertEqual(result['media']['selected'], 'shell')  # same node, now a rendered video
                    self.assertFalse(result['media']['recovery'])
                    self.assertEqual(result['autoLoads'], 0)
                    self.assertEqual(result['modal']['focus'], 'dismiss')
                    self.assertEqual(result['modalActs'], [['dismiss', True, '/fixture/status/1']])
                    # Cumulative journal of synthetic fixture IDs. Every ← gets a fresh
                    # outcome; neither a missing response nor a refusal toast proves zero sends.
                    safe = rev in (share_fix, 'candidate')
                    journal = []
                    for field, label, deliberate in [('shareA', 'A', True), ('shareStale', 'B', False),
                                                     ('shareReselected', 'B', True), ('shareOutside', 'B', False),
                                                     ('shareNoKey', 'A', False)]:
                        refused = safe and not deliberate
                        if not refused:
                            journal.append(label)
                        self.assertEqual(result[field], dict(ids=journal, pick=refused), field)

    def test_disabled_prod_candidate_sequential(self):
        # Baseline ref is mandatory: never silently lose the negative control in an archive checkout.
        subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", PROD + "^{commit}"], check=True, capture_output=True)
        with tempfile.TemporaryDirectory(prefix="otvxempty-") as tmp:
            assert NODE is not None
            root = Path(tmp)
            installed = root / "installed"
            p = sh([INSTALL, "--root", str(installed), "--skip-preflight"])
            self.assertEqual(p.returncode, 0, p.stderr.decode()[-2000:])
            baseline = root / "prod"
            baseline.mkdir()
            paths = subprocess.check_output(["git", "-C", str(REPO), "ls-tree", "-r", "--name-only", PROD,
                                             "scripts/pi4/x-tv-extension"]).decode().splitlines()
            for name in paths:
                (baseline / Path(name).name).write_bytes(subprocess.check_output(
                    ["git", "-C", str(REPO), "show", PROD + ":" + name]))
            shutil.copyfile(REPO / "native/app/qrcode.js", baseline / "qrcode.js")
            shots = Path(os.environ.get("OPENTV_TEST_SHOTS", str(root / "proof")))
            shots.mkdir(parents=True, exist_ok=True)
            results = {}
            for label, ext in (("disabled", None), ("prod", baseline),
                               ("candidate", installed / EXT_DST.lstrip("/"))):
                cfg = dict(chrome=full_chrome(), ext=str(ext) if ext else None, label=label,
                           expect=label == "candidate", shots=str(shots), result=str(shots / (label + ".json")))
                config = root / (label + ".json")
                config.write_text(json.dumps(cfg))
                p = subprocess.run([NODE, str(Path(PI4) / "tests/e2e_x_empty.js"),
                                    os.environ["OPENTV_TEST_PLAYWRIGHT"], str(config)], capture_output=True, timeout=90)
                (shots / (label + ".log")).write_bytes(p.stdout + b"\n" + p.stderr)
                self.assertEqual(p.returncode, 0, p.stdout.decode()[-5000:] + p.stderr.decode()[-2000:])
                results[label] = json.loads(p.stdout.decode().strip().splitlines()[-1])
            for label in results:
                first = results[label]["cases"]["first"]
                self.assertEqual((first["posts"], first["spinner"]), (0, True))
                self.assertTrue(all(v["visible"] for v in first["visible"]))
            self.assertEqual(results["prod"]["cases"]["retry"]["focuses"], [""] * 4)
            self.assertIn("Post précédent", results["prod"]["cases"]["first"]["help"])
            self.assertEqual(results["candidate"]["cases"]["retry"]["acts"], [{"id": "retry", "trusted": True}])

"""OpenTV Canvas, module interne : E/S fail-closed du dossier d'état (aucune logique de scène ni d'aperçu).

Création du dossier (`_create_dir`) : le nom canonique n'apparaît qu'une fois le dossier COMPLET. Dossier
temporaire `.<nom>.<hex>.tmpdir` du même parent (mkdir relatif au descripteur du parent), propriétaire et mode
fixés par fchown/fchmod sur SON descripteur, verrou `state.json.lock` créé dedans (O_EXCL, fchown/fchmod sur le
descripteur), fsync, rename atomique relatif au parent, fsync du parent. Un plantage (même de root) avant le rename
ne laisse qu'un temporaire, jamais un dossier root 0700 sous le nom canonique ; il est nettoyé au prochain
enregistrement. Créateurs concurrents : le premier rename gagne ; les autres (ENOTEMPTY, ENOENT si leur temporaire
a été nettoyé…) retirent le leur et ouvrent le gagnant, puis le valident comme tout dossier existant. Un nom
occupé par un lien ou un fichier fait échouer le rename (ENOTDIR) puis l'ouverture (unsafe_dir). Seul un dossier
VIDE préexistant peut être remplacé par le rename : tout dossier publié ici contient déjà son verrou, et un processus
qui aurait ouvert ce dossier vide ne peut plus y créer de verrou (ENOENT ⇒ store_busy) : jamais deux verrous actifs.
Root ne crée jamais de verrou dans un dossier existant (lock_unsafe ; le service le crée lui-même, à son nom).
Jamais de chown/chmod/makedirs par chemin.

Publication (`commit`) « échouer et restaurer » : temporaire exclusif, fsync, rename, fsync du dossier, relecture
octet pour octet. Échec AVANT le rename ⇒ write_failed, rien n'a changé. Toute panne APRÈS le rename (fsync du
dossier, relecture) ⇒ restauration des octets précédents exacts, ou retrait du candidat quand l'état visé est
l'absence (premier enregistrement, réinitialisation d'un état corrompu), fsync du dossier, puis vérification par
relecture : restauration confirmée ⇒ write_failed / readback_failed ; sinon restore_failed. L'appelant recharge
toujours le disque ensuite, si bien que le résultat, le disque et la mémoire concordent.
"""
import contextlib
import errno
import os
import re
import secrets
import stat

from . import agents, fsutil
from .canvas_schema import CanvasError, _Corrupt

STATE_NAME = "state.json"
LOCK_NAME = STATE_NAME + ".lock"
MAX_STATE_BYTES = 64 * 1024
CREATE_STAGES = ("dir_created", "dir_owned", "lock_created", "before_rename", "after_rename")
_TMP_RE = re.compile(r"\." + re.escape(STATE_NAME) + r"\.[0-9a-f]{12}\.tmp")
_DIR_FLAGS = os.O_RDONLY | fsutil.O_DIRECTORY | fsutil.O_NOFOLLOW | fsutil.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | fsutil.O_NOFOLLOW | fsutil.O_NONBLOCK | fsutil.O_CLOEXEC
_BAD = object()                       # état observé illisible ou dangereux
_hook = None                          # point d'injection des tests : appelé à chaque étape de CREATE_STAGES


def _step(name):
    h = _hook
    if h is not None:
        h(name)


def _unlink_quiet(name, dfd):
    try:
        os.unlink(name, dir_fd=dfd)
    except OSError:
        pass


# --------------------------------------------------------------------------- lecture
def read_state(dfd, owner):
    """-> octets de STATE_NAME, None s'il est absent ; _Corrupt pour tout fichier dangereux ou inattendu."""
    try:
        fd = os.open(STATE_NAME, _FILE_FLAGS, dir_fd=dfd)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise _Corrupt("unsafe_file" if e.errno in (errno.ELOOP, errno.EMLINK, errno.EISDIR, errno.ENXIO,
                                                     errno.ENOTDIR) else "unreadable")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise _Corrupt("unsafe_file")
        if st.st_uid != owner:
            raise _Corrupt("wrong_owner")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise _Corrupt("wrong_mode")
        if st.st_size > MAX_STATE_BYTES:
            raise _Corrupt("oversize")
        chunks, left = [], MAX_STATE_BYTES + 1
        while left > 0:
            b = os.read(fd, min(left, 65536))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
        if len(data) > MAX_STATE_BYTES:
            raise _Corrupt("oversize")
        return data
    except OSError:
        raise _Corrupt("unreadable")
    finally:
        os.close(fd)


def _readback(dfd, owner):
    """Relecture après écriture (point d'injection des tests) : None si illisible."""
    try:
        return read_state(dfd, owner)
    except _Corrupt:
        return None


def _observe(dfd, owner):
    """État réellement visible : octets, None (absent) ou _BAD (dangereux/illisible)."""
    try:
        return read_state(dfd, owner)
    except Exception:
        return _BAD


# --------------------------------------------------------------------------- dossier et verrou
def open_dir(parent, name, owner, root, create):
    """-> (descripteur du dossier Canvas, gid) ou None s'il est absent (create=False). Parent : non inscriptible par
    le groupe/les autres, propriété du service ou de root ; dossier : jamais un lien, propriété du service, mode exact
    0700 (un dossier existant n'est jamais « réparé »). CanvasError(unsafe_dir|write_failed) sinon."""
    try:
        pfd = fsutil.open_dir(parent)
    except OSError:
        raise CanvasError("unsafe_dir")
    try:
        pst = os.fstat(pfd)
        if not stat.S_ISDIR(pst.st_mode) or pst.st_mode & 0o022 or pst.st_uid not in (owner, 0):
            raise CanvasError("unsafe_dir")
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=pfd)
        except FileNotFoundError:
            if not create:
                return None
            fd = _create_dir(pfd, pst.st_gid, name, owner, root)
        except OSError:   # ELOOP (lien), ENOTDIR (fichier), EACCES…
            raise CanvasError("unsafe_dir")
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != owner or stat.S_IMODE(st.st_mode) != 0o700:
                raise CanvasError("unsafe_dir")
            if create:
                _cleanup_tmpdirs(pfd, name)
            return fd, st.st_gid
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(pfd)


def _create_dir(pfd, gid, name, owner, root):
    """Publie atomiquement un dossier complet (voir l'en-tête) puis -> descripteur de ce qui porte le nom."""
    tmp = ".%s.%s.tmpdir" % (name, secrets.token_hex(6))
    try:
        os.mkdir(tmp, 0o700, dir_fd=pfd)
    except OSError:
        raise CanvasError("unsafe_dir")
    published = False
    try:
        _step("dir_created")
        tfd = os.open(tmp, _DIR_FLAGS, dir_fd=pfd)
        try:
            if not stat.S_ISDIR(os.fstat(tfd).st_mode):
                raise OSError(errno.ENOTDIR, "tmpdir")
            if root:
                os.fchown(tfd, owner, gid)
            os.fchmod(tfd, 0o700)
            _step("dir_owned")
            lfd = os.open(LOCK_NAME, _FILE_FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=tfd)
            try:
                if root:
                    os.fchown(lfd, owner, -1)
                os.fchmod(lfd, 0o600)
            finally:
                os.close(lfd)
            _step("lock_created")
            os.fsync(tfd)
        finally:
            os.close(tfd)
        _step("before_rename")
        try:
            os.rename(tmp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
            published = True
        except OSError:   # un autre créateur a gagné, nom pris par un lien/fichier, temporaire nettoyé…
            pass
    except OSError:       # création impossible : on se rabat sur ce qui porte le nom (validé par l'appelant)
        pass
    finally:
        if not published:
            _remove_tmpdir(pfd, tmp)
    if published:
        _step("after_rename")
        try:
            os.fsync(pfd)
        except OSError:   # dossier publié et complet mais durabilité non prouvée : aucune scène écrite
            raise CanvasError("write_failed")
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=pfd)
    except OSError:
        raise CanvasError("unsafe_dir")


def _remove_tmpdir(pfd, tmp):
    """Retire un dossier temporaire (son seul verrou puis rmdir) ; jamais récursif ni à travers un lien."""
    try:
        tfd = os.open(tmp, _DIR_FLAGS, dir_fd=pfd)
    except OSError:
        return
    try:
        _unlink_quiet(LOCK_NAME, tfd)
    finally:
        os.close(tfd)
    try:
        os.rmdir(tmp, dir_fd=pfd)
    except OSError:
        pass


def _cleanup_tmpdirs(pfd, name):
    rx = re.compile(r"\." + re.escape(name) + r"\.[0-9a-f]{12}\.tmpdir")
    try:
        names = os.listdir(pfd)
    except OSError:
        return
    for n in names:
        if rx.fullmatch(n):
            _remove_tmpdir(pfd, n)


def open_lock(dfd, owner, root):
    """Verrou latéral : régulier, lien unique, propriété du service, 0600. Le service le crée s'il manque (O_EXCL, à
    son nom) ; root ne le crée jamais hors de la création atomique du dossier (lock_unsafe)."""
    try:
        if root:
            fd = os.open(LOCK_NAME, _FILE_FLAGS, dir_fd=dfd)
        else:
            fd = agents._open_lock_fd(dfd, LOCK_NAME)[0]
    except agents.RegistryBusy:
        raise CanvasError("store_busy")
    except OSError:
        raise CanvasError("lock_unsafe")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != owner:
            raise CanvasError("lock_unsafe")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def cleanup(dfd):
    """Sous le verrou : retire les temporaires d'écriture orphelins."""
    try:
        names = os.listdir(dfd)
    except OSError:
        return
    for n in names:
        if _TMP_RE.fullmatch(n):
            _unlink_quiet(n, dfd)


@contextlib.contextmanager
def locked(parent, name, owner, root, create, timeout, after_lock=None):
    """Dossier + flock borné du verrou latéral, temporaires orphelins nettoyés. -> (dfd, gid) ; None si absent."""
    opened = open_dir(parent, name, owner, root, create)
    if opened is None:
        yield None
        return
    dfd = opened[0]
    try:
        lfd = open_lock(dfd, owner, root)
        try:
            try:
                agents.flock_bounded(lfd, timeout)
            except agents.RegistryBusy:
                raise CanvasError("store_busy")
            cleanup(dfd)
            if after_lock is not None:
                after_lock()
            yield opened
        finally:
            os.close(lfd)
    finally:
        os.close(dfd)


def load_raw(parent, name, owner, root):
    """Lecture hors verrou : octets ou None (absent) ; _Corrupt sinon (dossier dangereux ⇒ unsafe_dir)."""
    try:
        opened = open_dir(parent, name, owner, root, False)
    except CanvasError:
        raise _Corrupt("unsafe_dir")
    if opened is None:
        return None
    try:
        return read_state(opened[0], owner)
    finally:
        os.close(opened[0])


# --------------------------------------------------------------------------- publication véridique
def _publish(dfd, data, owner, root, gid):
    """Temporaire exclusif + fchown/fchmod sur le descripteur + fsync + rename. OSError ⇒ rename non effectué
    (temporaire retiré)."""
    tmp = ".%s.%s.tmp" % (STATE_NAME, secrets.token_hex(6))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | fsutil.O_NOFOLLOW | fsutil.O_CLOEXEC, 0o600,
                 dir_fd=dfd)
    try:
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            if root:
                os.fchown(fd, owner, gid)
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, STATE_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        _unlink_quiet(tmp, dfd)
        raise


def commit(dfd, data, previous, owner, root, gid=-1):
    """Publie `data` ; `previous` = octets à restaurer, ou None si l'état visé en cas d'échec est l'absence.
    -> None ; CanvasError(write_failed|readback_failed|restore_failed) sinon (voir l'en-tête)."""
    try:
        _publish(dfd, data, owner, root, gid)
    except OSError:
        raise CanvasError("write_failed")
    try:
        os.fsync(dfd)
        failure = None if _readback(dfd, owner) == data else "readback_failed"
    except OSError:
        failure = "write_failed"
    if failure is None:
        return
    if not _restore(dfd, previous, owner, root, gid):
        raise CanvasError("restore_failed")
    raise CanvasError(failure)


def _restore(dfd, previous, owner, root, gid):
    """Ramène le disque à `previous` (octets) ou à l'absence (None), fsync du dossier. -> True si la relecture le
    confirme (un fsync de dossier en échec n'empêche pas la vérification de l'état visible)."""
    try:
        if previous is None:
            os.unlink(STATE_NAME, dir_fd=dfd)
        else:
            _publish(dfd, previous, owner, root, gid)
    except OSError:
        pass
    try:
        os.fsync(dfd)
    except OSError:
        pass
    seen = _observe(dfd, owner)
    return seen is not _BAD and seen == previous

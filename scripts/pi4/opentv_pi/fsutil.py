"""E/S de fichiers sûres pour le code privilégié (root : CLI, helper réseau) et le service.

Règles : aucun chown/chmod par chemin ; dossier ouvert une fois (O_DIRECTORY|O_NOFOLLOW) puis tout relatif à ce
descripteur (dir_fd) ; fichiers temporaires O_CREAT|O_EXCL|O_NOFOLLOW à nom aléatoire ; propriétaire et mode fixés par
fchown/fchmod sur le descripteur avant le rename atomique ; ouvertures de drapeaux O_NONBLOCK (une FIFO ne bloque
jamais) avec contrôle fstat (fichier régulier, un seul lien, propriétaire attendu) avant toute modification.
"""
import errno
import os
import secrets
import stat

O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class UnsafePath(OSError):
    """Chemin refusé (lien, FIFO, lien dur, propriétaire ou mode inattendu)."""

    def __init__(self, reason):
        super().__init__(errno.EPERM, reason)
        self.reason = reason


def _check_name(name):
    if not isinstance(name, str) or not name or "/" in name or name in (".", "..") or "\0" in name:
        raise UnsafePath("bad_name")
    return name


def open_dir(path):
    """Descripteur du dossier `path` ; le dernier composant n'est jamais un lien (O_NOFOLLOW)."""
    return os.open(path, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)


def open_trusted_dir(path, trusted_uids=(0,)):
    """Parcourt `path` composant par composant depuis « / » (openat + O_NOFOLLOW) : chaque dossier appartient à un
    UID de confiance et n'est inscriptible ni par le groupe ni par les autres (un dossier collant de root, comme /tmp,
    est toléré sauf en dernier). Le dernier dossier doit appartenir à un UID de confiance et être 0755 au plus.
    -> descripteur ; UnsafePath sinon."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise UnsafePath("not_absolute")
    parts = [p for p in path.split("/") if p]
    fd = os.open("/", os.O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    try:
        for i, name in enumerate(parts):
            if name in (".", ".."):
                raise UnsafePath("dot_component")
            try:
                nfd = os.open(name, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC, dir_fd=fd)
            except OSError as e:
                raise UnsafePath("open_failed:%s" % errno.errorcode.get(e.errno or 0, "?"))
            os.close(fd)
            fd = nfd
            st = os.fstat(fd)
            last = i == len(parts) - 1
            if st.st_uid not in trusted_uids:
                raise UnsafePath("untrusted_owner")
            if st.st_mode & 0o022 and (last or not (st.st_mode & stat.S_ISVTX and st.st_uid == 0)):
                raise UnsafePath("writable_by_others")
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o022:
            raise UnsafePath("writable_by_others")
        return fd
    except BaseException:
        os.close(fd)
        raise


def atomic_write(dir_fd, name, data, mode=0o600, uid=None, gid=None):
    """Écrit `data` dans `name` (relatif à dir_fd) : temporaire exclusif, fchown/fchmod sur le descripteur, fsync,
    rename atomique (un lien pré-placé à `name` est remplacé, jamais suivi). Le temporaire est retiré sur échec."""
    _check_name(name)
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = ".%s.%s.tmp" % (name[:64], secrets.token_hex(6))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            if uid is not None or gid is not None:
                os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    return name


def atomic_write_path(path, data, mode=0o600, uid=None, gid=None):
    d, name = os.path.split(os.path.abspath(path))
    dfd = open_dir(d)
    try:
        atomic_write(dfd, name, data, mode=mode, uid=uid, gid=gid)
    finally:
        os.close(dfd)
    return path


def touch_regular(path, mode=0o600, uid=None, gid=None, allowed_owners=None):
    """Crée/ouvre un fichier drapeau sans jamais suivre de lien ni bloquer (FIFO), puis fixe propriétaire et mode
    sur le descripteur. Refuse tout ce qui n'est pas un fichier régulier à lien unique (lien dur vers un fichier
    système) ou dont le propriétaire n'est pas attendu."""
    d, name = os.path.split(os.path.abspath(path))
    _check_name(name)
    dfd = open_dir(d)
    try:
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, mode, dir_fd=dfd)
        except OSError as e:   # ELOOP (lien), ENXIO (FIFO sans lecteur), EISDIR…
            raise UnsafePath("open_refused:%s" % errno.errorcode.get(e.errno or 0, "?"))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePath("not_regular")
            if st.st_nlink != 1:
                raise UnsafePath("hardlinked")
            if allowed_owners is not None and st.st_uid not in allowed_owners:
                raise UnsafePath("unexpected_owner")
            if uid is not None or gid is not None:
                os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    return path


def read_regular(path, cap):
    """Lit au plus `cap` octets d'un fichier régulier sans suivre de lien ni bloquer. -> bytes ; OSError sinon."""
    fd = os.open(path, os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UnsafePath("not_regular")
        return os.read(fd, cap)
    finally:
        os.close(fd)

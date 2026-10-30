"""OpenTV Canvas, module interne : façades par rôle (réexportées par `opentv_pi.canvas`).

`AgentCanvas` : vue d'un agent authentifié, limitée à status/preview/cancel ; tout refus y redevient un CanvasError
au code NU (jamais de `detail` interne tel que playback, muse_setup ou wrong_owner, ni d'exception chaînée).
`OwnerCanvas` : vue du propriétaire (feuille de confirmation et Réglages de la TV), détails bornés conservés.
Aucun import de `canvas` : le magasin est reçu en argument (pas de cycle).
"""
from .canvas_schema import CanvasError


def _agent_call(fn, *args):
    """Appel de la façade agent : tout refus redevient un CanvasError au code nu, construit HORS du bloc except
    (aucun __context__ ni __cause__ ne transporte le détail interne)."""
    try:
        return fn(*args)
    except CanvasError as e:
        code = e.code
    raise CanvasError(code)


# --------------------------------------------------------------------------- façades par rôle
class AgentCanvas:
    """Vue d'un agent authentifié : lecture, aperçu, retrait de SON aperçu. Rien d'autre ; codes d'erreur nus."""
    __slots__ = ("_s", "_agent")

    def __init__(self, store, agent):
        self._s, self._agent = store, agent

    def status(self):
        return _agent_call(self._s._agent_status, self._agent)

    def preview(self, changes):
        return _agent_call(self._s.agent_preview, self._agent, changes)

    def cancel(self, preview_id):
        return _agent_call(self._s.agent_cancel, self._agent, preview_id)


class OwnerCanvas:
    """Vue du propriétaire (feuille de confirmation et Réglages de la TV, derrière la clé TV)."""
    __slots__ = ("_s",)

    def __init__(self, store):
        self._s = store

    def status(self):
        return self._s.status()

    def confirm(self, preview_id, confirm=False):
        return self._s.owner_confirm(preview_id, confirm=confirm)

    def reject(self, preview_id):
        return self._s.owner_reject(preview_id)

    def apply(self, changes, confirm=False):
        return self._s.owner_apply(changes, confirm=confirm)

    def rollback(self, confirm=False):
        return self._s.owner_rollback(confirm=confirm)

    def reset(self, confirm=False):
        return self._s.owner_reset(confirm=confirm)

from . import models
from .models.havsh_sync import HAVSH_STATE_KEY


def _uninstall_hook(env):
    # What was delivered to hav.sh is kept in a row no data file declares. Leaving
    # it behind is harmless, failing the uninstall over it is not.
    try:
        with env.cr.savepoint(flush=False):
            env.cr.execute("DELETE FROM ir_config_parameter WHERE key = %s", [HAVSH_STATE_KEY], log_exceptions=False)
    except Exception:  # noqa: BLE001
        pass

from . import models

def _post_init_hook(env):
    env['havsh.sync']._sync_all_to_havsh()
import os
_disabled = set(os.environ.get('EC_DISABLE_OPT', '').split(','))
def opt_disabled(name):
    return name in _disabled

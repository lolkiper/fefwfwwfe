import os

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

try:
    from dramastill.winquiet import hide_console_windows

    hide_console_windows()
except Exception:
    pass

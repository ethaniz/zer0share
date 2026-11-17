from zer0share.api import pro_api as _pro_api
from zer0share.cache import CachedPro


def pro_api(token=None, *, config_path=None, cache_path=None):
    return _pro_api(config_path, token=token, cache_path=cache_path)


def pro_bar(ts_code=None, api=None, **kwargs):
    client = api if api is not None else pro_api()
    if not isinstance(client, CachedPro):
        raise TypeError("api must be a zer0share CachedPro client")
    return client.pro_bar(ts_code=ts_code, **kwargs)

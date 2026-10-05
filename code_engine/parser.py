from collections import OrderedDict
from pathlib import Path
from threading import Lock

from .languages import get_parser

_CACHE_MAX = 64
_cache: OrderedDict = OrderedDict()
_lock = Lock()


def parse_file(path: str):
    target = Path(path)
    stat = target.stat()
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    source = target.read_bytes()
    tree = get_parser(path).parse(source)
    with _lock:
        _cache[key] = (tree, source)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return tree, source

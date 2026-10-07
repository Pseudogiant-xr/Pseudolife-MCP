"""Print the full-range CPython 3.11 Unicode 14 ring-reason contract digest."""

import hashlib, unicodedata; assert unicodedata.unidata_version == "14.0.0"; print(hashlib.sha256(bytes(value for point in range(0x110000) for value in (chr(point).isalnum(), chr(point).isspace()))).hexdigest())

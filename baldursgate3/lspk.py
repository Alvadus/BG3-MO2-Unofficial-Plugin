import struct
import zlib
from pathlib import Path

MAGIC = b"LSPK"

_LAYOUT = {15: (34, 296), 16: (36, 296), 18: (36, 272)}

_METHOD_NONE = 0
_METHOD_ZLIB = 1
_METHOD_LZ4 = 2


class PakError(Exception):
    pass


def _lz4_block(src, out_size):
    dst = bytearray(out_size)
    src_pos = dst_pos = 0
    end = len(src)
    try:
        while src_pos < end:
            token = src[src_pos]
            src_pos += 1

            length = token >> 4
            if length == 15:
                while True:
                    b = src[src_pos]
                    src_pos += 1
                    length += b
                    if b != 255:
                        break
            dst[dst_pos:dst_pos + length] = src[src_pos:src_pos + length]
            src_pos += length
            dst_pos += length
            if src_pos >= end:
                break

            offset = src[src_pos] | (src[src_pos + 1] << 8)
            src_pos += 2
            if offset == 0:
                raise PakError("corrupt LZ4 stream: zero match offset")

            length = token & 0x0F
            if length == 15:
                while True:
                    b = src[src_pos]
                    src_pos += 1
                    length += b
                    if b != 255:
                        break
            length += 4

            match = dst_pos - offset
            if match < 0:
                raise PakError("corrupt LZ4 stream: match before start of output")
            for i in range(length):
                dst[dst_pos + i] = dst[match + i]
            dst_pos += length
    except IndexError as e:
        raise PakError(f"truncated LZ4 stream: {e}") from e
    return bytes(dst[:dst_pos])


def read_index(fh):
    fh.seek(0)
    head = fh.read(8)
    if len(head) < 8 or head[:4] != MAGIC:
        raise PakError(f"not an LSPK archive ({head[:4]!r})")

    version = struct.unpack_from("<I", head, 4)[0]
    if version not in _LAYOUT:
        raise PakError(f"unsupported LSPK version {version}")
    header_size, entry_size = _LAYOUT[version]

    fh.seek(4)
    header = fh.read(header_size)
    if len(header) < header_size:
        raise PakError("truncated header")
    file_list_offset, _file_list_size, flags, priority = struct.unpack_from("<QIBB", header, 4)
    num_parts = struct.unpack_from("<H", header, 34)[0] if version >= 16 else 1

    fh.seek(file_list_offset)
    listing = fh.read(8)
    if len(listing) < 8:
        raise PakError("truncated file list")
    num_files, compressed_size = struct.unpack("<II", listing)
    if num_files < 0 or num_files > 5_000_000:
        raise PakError(f"implausible file count {num_files}")

    expected = num_files * entry_size
    entries = _lz4_block(fh.read(compressed_size), expected)
    if len(entries) != expected:
        raise PakError(f"file table is {len(entries)} bytes, expected {expected}")

    files = []
    for i in range(num_files):
        raw = entries[i * entry_size:(i + 1) * entry_size]
        name = raw[:256].split(b"\x00", 1)[0].decode("utf-8", "replace")
        if entry_size == 272:
            off_low, off_high, part, entry_flags, on_disk, size = struct.unpack_from(
                "<IHBBII", raw, 256
            )
            offset = off_low | (off_high << 32)
        else:
            offset, on_disk, size, part, entry_flags, _crc, _unknown = struct.unpack_from(
                "<QQQIIII", raw, 256
            )
        files.append({
            "name": name,
            "offset": offset,
            "part": part,
            "flags": entry_flags,
            "size_on_disk": on_disk,
            "size": size,
        })

    return {
        "version": version,
        "flags": flags,
        "priority": priority,
        "num_parts": num_parts,
        "files": files,
    }


def read_file(fh, entry):
    if entry["part"] != 0:
        raise PakError(f"entry lives in archive part {entry['part']}")

    fh.seek(entry["offset"])
    blob = fh.read(entry["size_on_disk"])
    if len(blob) < entry["size_on_disk"]:
        raise PakError("truncated file payload")

    method = entry["flags"] & 0x0F
    if method == _METHOD_NONE:
        return blob
    if method == _METHOD_ZLIB:
        try:
            return zlib.decompress(blob)
        except zlib.error as e:
            raise PakError(f"zlib: {e}") from e
    if method == _METHOD_LZ4:
        return _lz4_block(blob, entry["size"])
    raise PakError(f"unsupported compression method {method}")


def read_meta(pak_path):
    with Path(pak_path).open("rb") as fh:
        index = read_index(fh)
        names = [entry["name"] for entry in index["files"]]
        metas = [e for e in index["files"] if e["name"].casefold().endswith("meta.lsx")]
        if not metas:
            return names, None
        metas.sort(key=lambda e: e["name"].count("/"))
        return names, read_file(fh, metas[0])

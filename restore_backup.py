#!/usr/bin/env python3
"""Verify or decrypt a chayunfarm backup; never extract or execute its contents.

Requires: pip install cryptography
The key JSON is a separate private file and must never be committed to GitHub.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

MAGIC = b"CHAYUNBACKUP\x01"
NONCE_SIZE = 12
TAG_SIZE = 16
CHUNK_SIZE = 1024 * 1024
MAX_PART_SIZE = 100 * 1024 * 1024
MANIFEST_FORMAT = "chayunfarm-backup-v1"
KEY_FORMAT = "chayunfarm-backup-key-v1"


class BackupError(Exception):
    """A validation or recovery failure safe to display to the operator."""


def _read_json(path: Path, label: str) -> dict:
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            value = json.load(stream)
    except (ValueError, UnicodeError) as exc:
        raise BackupError(f"Invalid {label} JSON.") from exc
    if not isinstance(value, dict):
        raise BackupError(f"The {label} must be a JSON object.")
    return value


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise BackupError(f"Invalid SHA-256 for {label}.")
    return value.lower()


def _validate_size(value: object, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise BackupError(f"Invalid size for {label}.")
    return value


def load_manifest(path: Path) -> dict:
    manifest = _read_json(path, "manifest")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise BackupError("Unsupported manifest format.")
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not parts:
        raise BackupError("The manifest must contain a nonempty ordered parts list.")
    seen = set()
    for part in parts:
        if not isinstance(part, dict):
            raise BackupError("Each part must be a JSON object.")
        name = part.get("name")
        # Restrict to simple basenames on both Windows and POSIX. In particular,
        # reject drive letters, separators, '..', and alternate data streams.
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None
            or name.endswith(".")
        ):
            raise BackupError("Unsafe part filename in manifest.")
        if name.casefold() in seen:
            raise BackupError("Duplicate part filename in manifest.")
        seen.add(name.casefold())
        size = _validate_size(part.get("size"), name, minimum=1)
        if size > MAX_PART_SIZE:
            raise BackupError(f"Part exceeds the 100 MiB limit: {name}")
        part["sha256"] = _validate_digest(part.get("sha256"), name)
    plaintext = manifest.get("plaintext")
    if not isinstance(plaintext, dict):
        raise BackupError("Missing plaintext metadata.")
    _validate_size(plaintext.get("size"), "plaintext")
    plaintext["sha256"] = _validate_digest(plaintext.get("sha256"), "plaintext")
    return manifest


def _part_path(directory: Path, name: str) -> Path:
    path = directory / name
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise BackupError(f"Missing backup part: {name}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BackupError(f"Part must be a regular file, not a link or directory: {name}")
    if path.resolve().parent != directory.resolve():
        raise BackupError(f"Part resolves outside the parts directory: {name}")
    return path


def verify_parts(manifest: dict, directory: Path) -> int:
    """Check ALL encrypted parts before reading a key or creating plaintext."""
    total = 0
    for part in manifest["parts"]:
        path = _part_path(directory, part["name"])
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while True:
                block = stream.read(CHUNK_SIZE)
                if not block:
                    break
                digest.update(block)
                size += len(block)
        if size != part["size"]:
            raise BackupError(f"Part size mismatch: {part['name']}")
        if digest.hexdigest() != part["sha256"]:
            raise BackupError(f"Part SHA-256 mismatch: {part['name']}")
        total += size
    return total


def load_key(path: Path) -> bytes:
    document = _read_json(path, "key")
    if document.get("format") != KEY_FORMAT:
        raise BackupError("Unsupported key format.")
    encoded = document.get("key_b64")
    if not isinstance(encoded, str):
        raise BackupError("Invalid key encoding.")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BackupError("Invalid key encoding.") from exc
    if len(key) != 32:
        raise BackupError("The backup requires a 256-bit key.")
    return key


class _PartReader:
    """Read the ordered raw parts as one stream without concatenating in RAM."""

    def __init__(self, manifest: dict, directory: Path):
        self.parts = iter(manifest["parts"])
        self.directory = directory
        self.stream = None

    def read_exact(self, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            if self.stream is None:
                part = next(self.parts, None)
                if part is None:
                    raise BackupError("Encrypted backup was truncated during recovery.")
                self.stream = _part_path(self.directory, part["name"]).open("rb")
            chunk = self.stream.read(remaining)
            if not chunk:
                self.stream.close()
                self.stream = None
                continue
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def restore_backup(
    manifest_path: Path,
    parts_dir: Path | None = None,
    key_path: Path | None = None,
    output_path: Path | None = None,
    verify_only: bool = False,
) -> dict:
    manifest_path = Path(manifest_path)
    directory = Path(parts_dir) if parts_dir is not None else manifest_path.parent
    manifest = load_manifest(manifest_path)
    encrypted_size = verify_parts(manifest, directory)
    result = {"parts": len(manifest["parts"]), "encrypted_size": encrypted_size}
    if verify_only:
        return result
    if key_path is None or output_path is None:
        raise BackupError("Restoring requires --key and --output.")
    output_path = Path(output_path).absolute()
    if os.path.lexists(output_path):
        raise BackupError("Output already exists; refusing to overwrite it.")
    header_size = len(MAGIC) + NONCE_SIZE
    if encrypted_size < header_size + TAG_SIZE:
        raise BackupError("Encrypted backup is too short to contain its header and tag.")
    key = load_key(Path(key_path))
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise BackupError("Missing dependency: install cryptography with pip.") from exc

    reader = _PartReader(manifest, directory)
    temporary_path = None
    try:
        header = reader.read_exact(header_size)
        if header[: len(MAGIC)] != MAGIC:
            raise BackupError("Invalid encrypted backup magic/version.")
        decryptor = Cipher(algorithms.AES(key), modes.GCM(header[len(MAGIC) :])).decryptor()
        decryptor.authenticate_additional_data(header)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{output_path.name}.", suffix=".partial", dir=output_path.parent
        )
        temporary_path = Path(name)
        digest = hashlib.sha256()
        plaintext_size = 0
        with os.fdopen(descriptor, "wb") as output:
            remaining = encrypted_size - header_size - TAG_SIZE
            while remaining:
                block = reader.read_exact(min(CHUNK_SIZE, remaining))
                remaining -= len(block)
                plaintext = decryptor.update(block)
                output.write(plaintext)
                digest.update(plaintext)
                plaintext_size += len(plaintext)
            tag = reader.read_exact(TAG_SIZE)
            try:
                final = decryptor.finalize_with_tag(tag)
            except InvalidTag as exc:
                raise BackupError("GCM authentication failed: wrong key or modified backup.") from exc
            output.write(final)
            digest.update(final)
            plaintext_size += len(final)
            if plaintext_size != manifest["plaintext"]["size"]:
                raise BackupError("Plaintext size mismatch.")
            if digest.hexdigest() != manifest["plaintext"]["sha256"]:
                raise BackupError("Plaintext SHA-256 mismatch.")
            output.flush()
            os.fsync(output.fileno())
        # Publishing a hard link is atomic and fails if the destination exists,
        # including if another process created it during decryption. Both paths
        # are on the same filesystem. Never use os.replace, which can overwrite.
        try:
            os.link(temporary_path, output_path)
        except FileExistsError as exc:
            raise BackupError("Output already exists; refusing to overwrite it.") from exc
        except OSError as exc:
            raise BackupError("Cannot publish output atomically; filesystem must support hard links.") from exc
        result["plaintext_size"] = plaintext_size
        return result
    finally:
        reader.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--parts-dir", type=Path, help="Defaults to the manifest directory.")
    parser.add_argument("--key", type=Path, help="Separate private key JSON; never displayed.")
    parser.add_argument("--output", type=Path, help="New archive path; existing files are never overwritten.")
    parser.add_argument("--verify-only", action="store_true", help="Check part sizes/SHA-256 only; no key needed.")
    args = parser.parse_args(argv)
    if not args.verify_only and (args.key is None or args.output is None):
        parser.error("restoring requires --key and --output (or use --verify-only)")
    try:
        result = restore_backup(
            args.manifest, args.parts_dir, args.key, args.output, args.verify_only
        )
    except (BackupError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.verify_only:
        print(
            f"Verified {result['parts']} encrypted parts: sizes and SHA-256 only. "
            "No decryption or GCM authentication was performed."
        )
    else:
        print(
            f"Recovery complete: {result['plaintext_size']} plaintext bytes. "
            "All part sizes/SHA-256, GCM authentication, and plaintext size/SHA-256 passed. "
            "The archive has not been extracted."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

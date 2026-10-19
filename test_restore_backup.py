"""Synthetic-only recovery tests. Never read production keys or server data."""

import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import restore_backup as restore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.parts = self.root / "parts"
        self.parts.mkdir()
        self.manifest_path = self.root / "manifest.json"
        self.key_path = self.root / "private-key.json"
        self.output = self.root / "recovered.tar.gz"
        self.key = os.urandom(32)
        # Over two normal read chunks, with header/tag and data split across parts.
        self.plaintext = os.urandom(2 * restore.CHUNK_SIZE + 79)
        header = restore.MAGIC + os.urandom(restore.NONCE_SIZE)
        encryptor = Cipher(algorithms.AES(self.key), modes.GCM(header[-12:])).encryptor()
        encryptor.authenticate_additional_data(header)
        encrypted = header + encryptor.update(self.plaintext) + encryptor.finalize() + encryptor.tag
        boundaries = [0, 7, 19, 524299, len(encrypted) - 9, len(encrypted)]
        entries = []
        for index, (left, right) in enumerate(zip(boundaries, boundaries[1:]), 1):
            block = encrypted[left:right]
            name = f"backup.enc.part{index:03d}"
            (self.parts / name).write_bytes(block)
            entries.append({"name": name, "size": len(block), "sha256": hashlib.sha256(block).hexdigest()})
        self.manifest = {
            "format": restore.MANIFEST_FORMAT,
            "snapshot_id": "synthetic-test",
            "parts": entries,
            "plaintext": {"size": len(self.plaintext), "sha256": hashlib.sha256(self.plaintext).hexdigest()},
        }
        self.save_manifest()
        self.save_key(self.key)

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def save_key(self, key):
        self.key_path.write_text(json.dumps({"format": restore.KEY_FORMAT, "key_b64": base64.b64encode(key).decode("ascii")}), encoding="utf-8")

    def recover(self):
        return restore.restore_backup(self.manifest_path, self.parts, self.key_path, self.output)

    def assert_no_partial(self):
        self.assertEqual(list(self.root.glob("*.partial")), [])
        self.assertFalse(self.output.exists())

    def test_correct_restore_across_part_boundaries(self):
        result = self.recover()
        self.assertEqual(self.output.read_bytes(), self.plaintext)
        self.assertEqual(result["plaintext_size"], len(self.plaintext))
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_verify_only_needs_no_key_and_explains_its_limit(self):
        self.key_path.unlink()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = restore.main(["--manifest", str(self.manifest_path), "--parts-dir", str(self.parts), "--verify-only"])
        self.assertEqual(status, 0)
        self.assertIn("SHA-256 only", output.getvalue())
        self.assertIn("No decryption or GCM authentication", output.getvalue())
        self.assert_no_partial()

    def test_missing_part(self):
        (self.parts / self.manifest["parts"][2]["name"]).unlink()
        with self.assertRaisesRegex(restore.BackupError, "Missing backup part"):
            self.recover()
        self.assert_no_partial()

    def tamper_ciphertext(self, recompute_manifest):
        part = self.manifest["parts"][2]
        path = self.parts / part["name"]
        data = bytearray(path.read_bytes())
        data[50] ^= 1
        path.write_bytes(data)
        if recompute_manifest:
            part["sha256"] = hashlib.sha256(data).hexdigest()
            self.save_manifest()

    def test_tampered_part_fails_sha_before_key_is_read(self):
        self.tamper_ciphertext(False)
        self.key_path.unlink()
        with self.assertRaisesRegex(restore.BackupError, "Part SHA-256 mismatch"):
            self.recover()
        self.assert_no_partial()

    def test_tamper_with_recomputed_manifest_still_fails_gcm(self):
        self.tamper_ciphertext(True)
        with self.assertRaisesRegex(restore.BackupError, "GCM authentication failed"):
            self.recover()
        self.assert_no_partial()

    def test_wrong_key_fails_and_cleans_partial(self):
        self.save_key(os.urandom(32))
        with self.assertRaisesRegex(restore.BackupError, "GCM authentication failed"):
            self.recover()
        self.assert_no_partial()

    def test_existing_output_is_not_overwritten(self):
        self.output.write_bytes(b"keep this file")
        with self.assertRaisesRegex(restore.BackupError, "refusing to overwrite"):
            self.recover()
        self.assertEqual(self.output.read_bytes(), b"keep this file")

    def test_plaintext_metadata_failure_cleans_partial(self):
        self.manifest["plaintext"]["sha256"] = "0" * 64
        self.save_manifest()
        with self.assertRaisesRegex(restore.BackupError, "Plaintext SHA-256 mismatch"):
            self.recover()
        self.assert_no_partial()

    def test_path_traversal_is_rejected(self):
        for unsafe in ("../outside", "..\\outside", "C:outside", "/outside"):
            with self.subTest(unsafe=unsafe):
                self.manifest["parts"][0]["name"] = unsafe
                self.save_manifest()
                with self.assertRaisesRegex(restore.BackupError, "Unsafe part filename"):
                    self.recover()
        self.assert_no_partial()


if __name__ == "__main__":
    unittest.main()

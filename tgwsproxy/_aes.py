"""AES-CTR shim with a libcrypto fallback.

Prefers `cryptography` when it is importable. On Entware routers (ARM/MIPS)
building/installing `cryptography` pulls in a Rust toolchain and is often
painful, so we transparently fall back to a thin `ctypes` wrapper over the
system OpenSSL `libcrypto`. The public surface mimics only the small subset
of `cryptography.hazmat.primitives.ciphers` this project uses:

    Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor().update(data)
"""
from __future__ import annotations

try:
    import cryptography
    from cryptography.hazmat.primitives.ciphers import (  # noqa: F401
        Cipher, algorithms, modes,
    )

    AES_BACKEND = "cryptography %s" % cryptography.__version__
except ImportError:
    import ctypes
    import ctypes.util

    def _load_libcrypto():
        name = ctypes.util.find_library("crypto")
        candidates = []
        if name:
            candidates.append(name)
        candidates += [
            "libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so.1.0.0",
            "libcrypto.so", "/opt/lib/libcrypto.so",
            "/opt/lib/libcrypto.so.1.1", "/opt/lib/libcrypto.so.3",
        ]
        last_err = None
        for candidate in candidates:
            try:
                return ctypes.CDLL(candidate)
            except OSError as exc:
                last_err = exc
        raise RuntimeError(
            "libcrypto not found; install openssl-util or "
            "`opkg install libopenssl`. Last error: %r" % last_err
        )

    _libcrypto = _load_libcrypto()

    AES_BACKEND = "libcrypto (ctypes)"

    _libcrypto.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    _libcrypto.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
    _libcrypto.EVP_aes_128_ctr.restype = ctypes.c_void_p
    _libcrypto.EVP_aes_192_ctr.restype = ctypes.c_void_p
    _libcrypto.EVP_aes_256_ctr.restype = ctypes.c_void_p
    _libcrypto.EVP_EncryptInit_ex.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_char_p, ctypes.c_char_p,
    ]
    _libcrypto.EVP_EncryptInit_ex.restype = ctypes.c_int
    _libcrypto.EVP_EncryptUpdate.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p, ctypes.c_int,
    ]
    _libcrypto.EVP_EncryptUpdate.restype = ctypes.c_int

    _EVP_BY_KEY = {
        16: _libcrypto.EVP_aes_128_ctr,
        24: _libcrypto.EVP_aes_192_ctr,
        32: _libcrypto.EVP_aes_256_ctr,
    }

    class algorithms:
        class AES:
            __slots__ = ("key",)

            def __init__(self, key: bytes):
                if len(key) not in _EVP_BY_KEY:
                    raise ValueError("AES key must be 16/24/32 bytes")
                self.key = bytes(key)

    class modes:
        class CTR:
            __slots__ = ("iv",)

            def __init__(self, iv: bytes):
                if len(iv) != 16:
                    raise ValueError("CTR IV must be 16 bytes")
                self.iv = bytes(iv)

    class _CtrStream:
        __slots__ = ("_ctx",)

        def __init__(self, key: bytes, iv: bytes):
            ctx = _libcrypto.EVP_CIPHER_CTX_new()
            if not ctx:
                raise RuntimeError("EVP_CIPHER_CTX_new failed")
            self._ctx = ctx
            evp = _EVP_BY_KEY[len(key)]()
            if _libcrypto.EVP_EncryptInit_ex(ctx, evp, None, key, iv) != 1:
                _libcrypto.EVP_CIPHER_CTX_free(ctx)
                self._ctx = None
                raise RuntimeError("EVP_EncryptInit_ex failed")

        def update(self, data: bytes) -> bytes:
            if not data:
                return b""
            outlen = ctypes.c_int(0)
            buf = ctypes.create_string_buffer(len(data) + 16)
            if _libcrypto.EVP_EncryptUpdate(
                self._ctx, buf, ctypes.byref(outlen), bytes(data), len(data)
            ) != 1:
                raise RuntimeError("EVP_EncryptUpdate failed")
            return buf.raw[: outlen.value]

        def __del__(self):
            ctx = getattr(self, "_ctx", None)
            if ctx:
                _libcrypto.EVP_CIPHER_CTX_free(ctx)
                self._ctx = None

    class Cipher:
        __slots__ = ("_key", "_iv")

        def __init__(self, algorithm, mode):
            if not isinstance(algorithm, algorithms.AES):
                raise TypeError("only AES is supported")
            if not isinstance(mode, modes.CTR):
                raise TypeError("only CTR mode is supported")
            self._key = algorithm.key
            self._iv = mode.iv

        def encryptor(self) -> _CtrStream:
            return _CtrStream(self._key, self._iv)

        # CTR is symmetric — decryption == encryption with the same keystream.
        decryptor = encryptor

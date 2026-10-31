"""Protocol-level constants for MTProto, TLS and WebSocket layers."""

ZERO_64 = b"\x00" * 64

# --- MTProto obfuscation handshake ---------------------------------------

HANDSHAKE_LEN = 64
SKIP_LEN = 8
PREKEY_LEN = 32
KEY_LEN = 32
IV_LEN = 16
PROTO_TAG_POS = 56
DC_IDX_POS = 60

PROTO_TAG_ABRIDGED = b"\xef\xef\xef\xef"
PROTO_TAG_INTERMEDIATE = b"\xee\xee\xee\xee"
PROTO_TAG_PADDED_INTERMEDIATE = b"\xdd\xdd\xdd\xdd"

PROTO_INT_ABRIDGED = 0xEFEFEFEF
PROTO_INT_INTERMEDIATE = 0xEEEEEEEE
PROTO_INT_PADDED_INTERMEDIATE = 0xDDDDDDDD

# Bytes/patterns that must NOT appear at the start of a relay handshake,
# because Telegram protocol detection uses them as markers for HTTP, TLS etc.
RESERVED_FIRST_BYTES = {0xEF}
RESERVED_STARTS = {
    b"\x48\x45\x41\x44",  # "HEAD"
    b"\x50\x4f\x53\x54",  # "POST"
    b"\x47\x45\x54\x20",  # "GET "
    b"\xee\xee\xee\xee",
    b"\xdd\xdd\xdd\xdd",
    b"\x16\x03\x01\x02",  # TLS record header
    b"\x4f\x50\x54\x49",  # "OPTI"
}
RESERVED_CONTINUE = b"\x00\x00\x00\x00"

# Default IPs (used as final TCP fallback when WS is unreachable).
DC_DEFAULT_IPS = {
    1: "149.154.175.50",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "149.154.171.5",
    203: "91.105.192.100",
}

# Telegram TEST datacenter IPs (used when force_test_dc is enabled or a
# client signals a test DC as plain 1-3).
DC_TEST_IPS = {
    1: "149.154.175.10",
    2: "149.154.167.40",
    3: "149.154.175.117",
}

WS_PATH = "/apiws"
WS_PATH_TEST = WS_PATH + "_test"

# Valid DC numbers we route to.
DC_IDS = (1, 2, 3, 4, 5, 203)

# --- TLS record types ----------------------------------------------------

TLS_RECORD_CCS = 0x14
TLS_RECORD_HANDSHAKE = 0x16
TLS_RECORD_APPDATA = 0x17

TLS_APPDATA_MAX = 16384

# --- Fake TLS handshake layout ------------------------------------------

CLIENT_RANDOM_OFFSET = 11
CLIENT_RANDOM_LEN = 32
SESSION_ID_OFFSET = 44
SESSION_ID_LEN = 32

# Tolerance for client_random embedded timestamp (seconds).
TIMESTAMP_TOLERANCE = 120

TLS_VERSION_12 = b"\x03\x03"

# --- Forged ServerHello layout ------------------------------------------

EXT_SUPPORTED_VERSIONS = 0x002B
EXT_KEY_SHARE = 0x0033

GROUP_X25519 = 0x001D
GROUP_X25519MLKEM768 = 0x11EC

X25519_KEY_LEN = 32
X25519MLKEM768_CLIENT_SHARE_LEN = 1216
MLKEM768_CIPHERTEXT_LEN = 1088

# TLS 1.3 suites in the order a typical server picks them from the client list.
CIPHER_PREFERENCE = (0x1301, 0x1303, 0x1302)

# Bounds for the single ApplicationData record that follows CCS. The low bound
# matches the smallest record telemt emits, the high one is the TLS limit.
APPDATA_MIN = 64
# Without a masking-site profile stay near the 64-byte record telemt passed DPI
# with, far from the 1900-2100 bytes of the classic MTProxy answer.
FALLBACK_APPDATA_RANGE = (64, 160)

# A real TLS server never answers in the same instant it was asked.
SERVER_HELLO_DELAY = (0.008, 0.024)

PROFILE_PROBE_TIMEOUT = 5

# --- WebSocket -----------------------------------------------------------

WS_OP_CONT = 0x0
WS_OP_TEXT = 0x1
WS_OP_BINARY = 0x2
WS_OP_CLOSE = 0x8
WS_OP_PING = 0x9
WS_OP_PONG = 0xA

WS_FIN_BIT = 0x80
WS_MASK_BIT = 0x80

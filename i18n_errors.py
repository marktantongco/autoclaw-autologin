"""Chinese → English Error Translation — Synergy: GLM_proxy.

AutoClaw upstream returns Chinese error messages (积分不足, 账号封禁,
请求过于频繁, etc.) which are confusing for international clients and
impossible to programmatically match on. This module translates the
known set of Chinese error substrings to stable English strings that
clients can match on with `==` and switch on in error handlers.

Source: eequaled/GLM_proxy lib/core.js (ZH_ERROR_MAP, translateError)
"""

# ──────────────────────────────────────────────────────────────────────────
# Synergy 4: Chinese → English Error Translation
# Source: eequaled/GLM_proxy lib/core.js (ZH_ERROR_MAP)
# ──────────────────────────────────────────────────────────────────────────
# Substring-match table — if the upstream error message contains any of
# these Chinese substrings, the corresponding English string is returned
# in its place. Add new entries here as new upstream error phrases are
# discovered.
ZH_ERROR_MAP = {
    "积分不足": "Insufficient credits. Please top up your account.",
    "账号封禁": "Account banned. Please contact support.",
    "请求过于频繁": "Rate limited. Please reduce request frequency.",
    "服务暂时不可用": "Service temporarily unavailable. Please retry later.",
    "模型不存在": "Model not found. Please check the model name.",
    "认证失败": "Authentication failed. Please refresh your token.",
    "用户不存在": "User not found.",
    "参数错误": "Invalid request parameters.",
    "内部错误": "Internal server error. Please retry later.",
    "额度已用尽": "Quota exhausted.",
    "登录已过期": "Login expired. Please re-authenticate.",
}


def translate_error(message: str) -> str:
    """Translate Chinese error messages to stable English.

    Scans the message for any known Chinese substring and returns the
    corresponding English translation. The first match wins (dict order
    is insertion-ordered in Python 3.7+). If no match is found, the
    original message is returned unchanged — never raises.

    Synergy: eequaled/GLM_proxy lib/core.js (translateError)

    Args:
        message: Raw upstream error message (may contain Chinese,
            may contain mixed Chinese/English, may be empty).

    Returns:
        English error string if a translation was found, else the
        original message unchanged. Empty input returns empty output.
    """
    if not message:
        return message
    for zh, en in ZH_ERROR_MAP.items():
        if zh in message:
            return en
    return message

from urllib.parse import quote
import hashlib


def route_key(account_id: str, conversation_id: str, sender_id: str) -> str:
    """Stable, collision-free identity shared by all trusted channel adapters."""
    components = tuple(
        quote(value, safe="") for value in (account_id, conversation_id, sender_id)
    )
    canonical = "v1|" + "|".join(f"{len(value)}:{value}" for value in components)
    if len(canonical) <= 512:
        return canonical
    return f"v1h|sha256:{hashlib.sha256(canonical.encode('ascii')).hexdigest()}"

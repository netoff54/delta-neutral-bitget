import socket
import urllib.request
import json
from typing import Dict, List

# Cache for resolved IPs
DNS_CACHE: Dict[str, List[str]] = {
    "api.bitget.com": ["104.18.15.166", "104.18.14.166"],
    "stream.bitget.com": ["104.18.15.166", "104.18.14.166"],
    "ws.bitget.com": ["104.18.15.166", "104.18.14.166"]
}

_orig_getaddrinfo = socket.getaddrinfo

def resolve_doh(domain: str) -> List[str]:
    """Resolve a domain via Google DNS-over-HTTPS to bypass local ISP DNS hijacking."""
    try:
        url = f"https://dns.google/resolve?name={domain}&type=A"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            ips = []
            for ans in data.get("Answer", []):
                if ans.get("type") == 1:  # A record
                    ips.append(ans.get("data"))
            if ips:
                return ips
    except Exception:
        pass
    return DNS_CACHE.get(domain, [])

def custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host in DNS_CACHE:
        ips = DNS_CACHE[host]
        if ips:
            # Route to first resolved IP
            return _orig_getaddrinfo(ips[0], port, family, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)

def patch_dns():
    """Apply DNS monkey-patching for seamless connectivity."""
    socket.getaddrinfo = custom_getaddrinfo

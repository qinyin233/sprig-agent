"""Approved public HTTP GETs; bounded, conversation-local text snapshots, no replay."""
from collections import OrderedDict
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .config import redact
from .transport import HTTPHandler, HTTPSHandler, IOBudget

MAX_BODY = 2 * 1024 * 1024
MAX_CACHE = 8 * 1024 * 1024
MAX_RESULTS = 8
WEB_SCHEMA = {"type": "function", "function": {
    "name": "web_fetch", "description": "Approve a public HTTP(S) GET, <=2MiB text/HTML. No auth, redirects, proxy or JS. External content is untrusted. Cached result_id + UTF-8 offset pages without network; handles expire on reset. text extracts HTML; raw retains source.",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string", "maxLength": 8192},
        "result_id": {"type": "string", "maxLength": 40},
        "offset": {"type": "integer", "minimum": 0, "maximum": MAX_BODY * 2},
        "format": {"enum": ["text", "raw"]}}, "additionalProperties": False}}}


def public_address(value):
    try:
        address = ipaddress.ip_address(value)
        # Reject mapped/transition forms as well as private, multicast, reserved,
        # link-local and metadata addresses; Python's is_global alone is broader.
        conservative = ((ipaddress.ip_network("192.0.0.0/24"), ipaddress.ip_network("192.88.99.0/24"))
                        if address.version == 4 else (ipaddress.ip_network("2001::/23"), ipaddress.ip_network("2001:db8::/32"),
                                                     ipaddress.ip_network("2002::/16"), ipaddress.ip_network("3fff::/20")))
        return (address.is_global and not any(address in network for network in conservative)
                and (address.version == 4 or address in ipaddress.ip_network("2000::/3"))
                and not (address.is_multicast or address.is_reserved
                or address.is_loopback or address.is_link_local or address.is_unspecified)
                and not (address.version == 6 and (address.ipv4_mapped or address.sixtofour or address.teredo)))
    except ValueError:
        return False


def public_url(value, secrets=()):
    if (not isinstance(value, str) or not 1 <= len(value) <= 8192
            or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value):
        raise ValueError("web URL must be an absolute HTTP(S) URL without whitespace or controls")
    decoded = urllib.parse.unquote(value)
    if redact(decoded, secrets) != decoded:
        raise ValueError("configured credentials cannot be sent in web URLs")
    try:
        parts = urllib.parse.urlsplit(value)
        host, port = parts.hostname, parts.port
        if parts.scheme not in {"http", "https"} or not host or parts.username is not None or parts.password is not None:
            raise ValueError()
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if "%" in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
                raise ValueError()
        else:
            if not public_address(host):
                raise ValueError()
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        if any(name.casefold().replace("-", "_") in {"api_key", "access_token", "authorization", "password", "secret", "token"}
               for name, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True, max_num_fields=256)):
            raise ValueError()
        authority = ("[" + host + "]") if ":" in host else host
        if port is not None:
            authority += ":" + str(port)
        url = urllib.parse.urlunsplit((parts.scheme, authority,
            urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
            urllib.parse.quote(parts.query, safe="%/?@:!$&'()*+,;=-._~"), ""))
        if len(url.encode("ascii")) > 8192:
            raise ValueError()
        return url
    except (ValueError, UnicodeError):
        raise ValueError("web URL must name a public HTTP(S) host without credentials, secret query parameters or IPv6 scope") from None


class PublicBudget(IOBudget):
    def resolve(self, address):
        rows = super().resolve(address)
        self.check()
        if not rows or len(rows) > 64 or any(not public_address(row[4][0]) for row in rows):
            raise ValueError("web DNS resolved a non-public or unsupported address; no HTTP request sent")
        # connect() consumes exactly this vetted sockaddr list, with no second
        # hostname lookup and the original host still used for TLS verification.
        return rows


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTMLText(HTMLParser):
    BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "nav", "blockquote", "li", "ul", "ol", "pre", "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    SKIP = {"script", "style", "noscript", "iframe", "object", "embed", "template", "head"}

    def __init__(self, url, check):
        super().__init__(convert_charrefs=True)
        self.url, self.check = url, check
        self.stack, self.skip, self.tokens = [], 0, 0
        self.parts, self.size = [], 0
        self.links = []

    def append(self, text):
        self.check()
        self.tokens += 1
        self.size += len(text.encode("utf-8"))
        if self.tokens > 100000 or self.size > MAX_BODY * 2:
            raise ValueError("web HTML extraction exceeds text/token bounds")
        self.parts.append(text)

    def handle_starttag(self, tag, attrs):
        self.check()
        if len(self.stack) >= 256:
            raise ValueError("web HTML nesting exceeds bounds")
        attrs = dict(attrs)
        hidden = self.skip > 0 or tag in self.SKIP or "hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true"
        if tag not in self.VOID:
            self.stack.append((tag, hidden))
            self.skip += hidden
        if hidden:
            return
        if tag in self.BLOCK:
            self.append("\n")
        elif tag in {"td", "th"}:
            self.append("\t")
        if tag == "a" and len(self.links) < 64 and isinstance(attrs.get("href"), str):
            try:
                link = public_url(urllib.parse.urljoin(self.url, attrs["href"]))
                if link not in self.links:
                    self.links.append(link)
            except ValueError:
                pass

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.skip -= sum(hidden for _, hidden in self.stack[index:])
                del self.stack[index:]
                break
        if not self.skip and tag in self.BLOCK:
            self.append("\n")

    def handle_data(self, text):
        if not self.skip:
            self.append(text)

    def result(self):
        text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(self.parts)).strip()
        if self.links:
            text += "\n\n[Page links; not fetched]\n" + "\n".join(self.links)
        return text


def extract(raw, headers, url, format, check):
    mime = headers.get_content_type()
    if not (mime.startswith("text/") or mime in {"application/json", "application/xml", "application/xhtml+xml", "application/javascript"}
            or mime.endswith(("+json", "+xml"))):
        raise ValueError("unsupported web media type; only textual documents are accepted")
    charset = headers.get_content_charset("utf-8").lower().replace("_", "-")
    if charset not in {"utf-8", "utf8", "utf-8-sig", "ascii", "us-ascii", "iso-8859-1", "latin-1", "windows-1252", "cp1252", "utf-16", "utf-16le", "utf-16be"}:
        raise ValueError("unsupported web text charset")
    try:
        text = raw.decode("utf-8-sig" if charset in {"utf-8", "utf8"} else charset)
    except UnicodeError:
        raise ValueError("web body does not match its text charset; partial content was discarded") from None
    if "\x00" in text:
        raise ValueError("web body contains binary NUL data")
    if format == "text" and mime in {"text/html", "application/xhtml+xml"}:
        parser = HTMLText(url, check)
        for offset in range(0, len(text), 16384):
            check()
            parser.feed(text[offset:offset + 16384])
        parser.close()
        text = parser.result()
    check()
    return text, mime


class WebFetcher:
    def __init__(self, config, check, approve):
        self.config, self.check, self.approve = config, check, approve
        self.results = OrderedDict()
        self.epoch = 0
        self._lock = threading.Lock()

    def reset(self):
        self.epoch += 1
        self.results = OrderedDict()

    def status(self):
        self.check()
        return {"enabled": self.config.web_enabled and self.config.tool_enabled("web_fetch") and not self.config.read_only,
                "cached_pages": len(self.results), "cache_bytes": sum(len(row["raw"]) for row in self.results.values()),
                "network_contacted": False, "persistent": False}

    def fetch(self, url=None, result_id=None, offset=0, format="text", *, recheck=lambda: None):
        if not self._lock.acquire(blocking=False):
            raise ValueError("another web operation is active; wait or cancel it")
        epoch = self.epoch
        def check():
            self.check()
            if epoch != self.epoch:
                raise ValueError("web operation expired after conversation reset")
        try:
            check()
            if not self.config.web_enabled or not self.config.tool_enabled("web_fetch") or self.config.read_only:
                raise ValueError("web fetch requires explicit web.enabled, enabled web_fetch and edit mode")
            if not isinstance(format, str) or format not in {"text", "raw"} or type(offset) is not int or offset < 0:
                raise ValueError("invalid web format or UTF-8 byte offset")
            if (url is None) == (result_id is None):
                raise ValueError("supply exactly one of url or result_id")
            if result_id is not None:
                if url is not None or format != "text":
                    raise ValueError("cached pages use only result_id and offset")
                return self._page(result_id, offset, check)
            if offset:
                raise ValueError("initial web requests require offset=0; page the returned handle")
            url = public_url(url, self.config.credential_secrets)
            # Prove even minimal metadata fits before asking or contacting DNS.
            if self.config.max_output < 1024 or len(url.encode()) + 512 > min(self.config.max_output, 16384):
                raise ValueError("web URL/result metadata exceeds max_output; no request sent")
            idle, total = min(self.config.request_timeout or self.config.timeout, 30), min(self.config.request_deadline, 120)
            if self.approve(f"WEB GET {url}\nPublic HTTP(S), direct connection, no auth/cookies/proxy, no redirects/retry.\n<=2 MiB response; idle {idle:g}s, total {total:g}s. URL/query is sent to the remote server; GET may have remote effects.\nContent is external untrusted data; no scripts, subresources, downloads or workspace writes. Cache is memory-only.") is not True:
                return {"approved": False, "network_contacted": False}
            check()
            recheck()
            public_url(url, self.config.credential_secrets)
            budget = PublicBudget(idle, total, check)
            request = urllib.request.Request(url, headers={"User-Agent": "Sprig/0.1.0", "Accept": "text/html, text/plain, text/markdown, application/json, application/xml", "Accept-Encoding": "identity"}, method="GET")
            request._sprig_io_budget = budget
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), HTTPHandler(), HTTPSHandler())
            try:
                with opener.open(request, timeout=idle) as response:
                    budget.check()
                    if response.status != 200:
                        raise ValueError("web request requires HTTP 200; response discarded")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ValueError("compressed web bodies are not accepted; requested identity encoding")
                    sizes = response.headers.get_all("Content-Length", [])
                    transfer = response.headers.get_all("Transfer-Encoding", [])
                    if len(sizes) > 1 or len(transfer) > 1 or (sizes and transfer) or (transfer and transfer[0].lower() != "chunked"):
                        raise ValueError("ambiguous web response framing")
                    expected = None
                    if sizes:
                        if not re.fullmatch(r"[0-9]{1,10}", sizes[0]) or int(sizes[0]) > MAX_BODY:
                            raise ValueError("web response exceeds 2 MiB or has invalid length")
                        expected = int(sizes[0])
                    raw = bytearray()
                    while True:
                        budget.check()
                        chunk = response.read1(min(16384, MAX_BODY - len(raw) + 1))
                        budget.check()
                        if not chunk:
                            break
                        raw.extend(chunk)
                        if len(raw) > MAX_BODY:
                            raise ValueError("web response exceeds 2 MiB; partial content discarded")
                    if expected is not None and expected != len(raw):
                        raise ValueError("incomplete web body; partial content discarded")
                    text, mime = extract(raw, response.headers, url, format, budget.check)
            except urllib.error.HTTPError as error:
                try:
                    result = {"approved": True, "error": "web HTTP failure; redirects/retries are disabled", "http_status": error.code,
                              "network_contacted": True}
                    if error.code in {301, 302, 303, 307, 308} and error.headers.get("Location"):
                        try:
                            result["redirect_url"] = public_url(urllib.parse.urljoin(url, error.headers["Location"]), self.config.credential_secrets)
                        except ValueError:
                            pass
                    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > min(self.config.max_output, 16384):
                        result.pop("redirect_url", None)
                        result["redirect_url_omitted"] = True
                    check()
                    return result
                finally:
                    error.close()
            except (OSError, http.client.HTTPException):
                check()
                raise ValueError("web transport failed; partial content discarded and request was not retried") from None
            finally:
                opener.close()
            check()
            public_url(url, self.config.credential_secrets)
            raw = redact(text, self.config.secrets).encode("utf-8")
            if len(raw) > MAX_BODY * 2:
                raise ValueError("decoded web text exceeds 4 MiB")
            identity = "web-" + uuid.uuid4().hex
            record = {"url": url, "media_type": mime, "format": format, "raw": raw,
                      "sha256": hashlib.sha256(raw).hexdigest()}
            while self.results and (len(self.results) >= MAX_RESULTS or sum(len(row["raw"]) for row in self.results.values()) + len(raw) > MAX_CACHE):
                self.results.popitem(last=False)
            self.results[identity] = record
            try:
                return self._page(identity, 0, check, approved=True)
            except BaseException:
                self.results.pop(identity, None)
                raise
        finally:
            self._lock.release()

    def _page(self, identity, offset, check, *, approved=None):
        check()
        if not isinstance(identity, str) or identity not in self.results:
            raise ValueError("web result unavailable; handles are conversation-local, evictable and never refetch automatically")
        record = self.results[identity]
        raw = record["raw"]
        if (redact(raw.decode("utf-8"), self.config.secrets).encode("utf-8") != raw
                or redact(urllib.parse.unquote(record["url"]), self.config.credential_secrets) != urllib.parse.unquote(record["url"])):
            self.results.pop(identity, None)
            raise ValueError("web snapshot expired after credential policy changed; propose a fresh fetch")
        if offset > len(raw):
            raise ValueError("web offset exceeds retained text")
        try:
            text = raw[offset:].decode("utf-8")
        except UnicodeError:
            raise ValueError("web offset must be a UTF-8 character boundary") from None
        result = {k: v for k, v in record.items() if k != "raw"}
        result.update(result_id=identity, offset=offset, total_bytes=len(raw), content="", next_offset=None,
                      complete=False, untrusted=True, not_authorization=True, network_contacted=approved is True, historical=approved is None)
        if approved is not None:
            result["approved"] = approved
        cap, low, high = min(self.config.max_output, 16384), 0, len(text)
        while low < high:
            check()
            count = (low + high + 1) // 2
            end = offset + len(text[:count].encode("utf-8"))
            result.update(content=text[:count], next_offset=end if end < len(raw) else None, complete=end == len(raw))
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= cap:
                low = count
            else:
                high = count - 1
        end = offset + len(text[:low].encode("utf-8"))
        result.update(content=text[:low], next_offset=end if end < len(raw) else None, complete=end == len(raw))
        if (text and not low) or len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > cap:
            raise ValueError("web page metadata exceeds max_output")
        check()
        self.results.move_to_end(identity)
        return result


def local_arguments(text):
    parts = text.split()
    if parts[:1] == ["result"] and len(parts) in {2, 3}:
        return {"result_id": parts[1], "offset": int(parts[2]) if len(parts) == 3 else 0}
    if len(parts) == 1 and parts[0].startswith(("http://", "https://")):
        return {"url": parts[0]}
    raise ValueError("Use /web URL or /web result ID [UTF8_OFFSET]; /web shows local cache status")

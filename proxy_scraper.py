import asyncio
import re
import time
import random
import aiohttp
from typing import List, Dict, Optional, Callable, Tuple
import aiohttp_socks

# ==================== SCRAPER SOURCES ====================

class Scraper:
    def __init__(self, method: str, _url: str):
        self.method = method  # "http", "https", "socks", "socks4", "socks5"
        self._url = _url

    def get_url(self, **kwargs) -> str:
        return self._url.format(**kwargs, method=self.method)

    async def get_response_text(self, session: aiohttp.ClientSession) -> str:
        url = self.get_url()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            return await resp.text()

    async def handle(self, text: str) -> str:
        return text

    async def scrape(self, session: aiohttp.ClientSession) -> List[Tuple[str, str]]:
        """Scrapes proxies and yields tuples of (IP:PORT, scheme)."""
        try:
            raw_text = await self.get_response_text(session)
            parsed_text = await self.handle(raw_text)
            pattern = re.compile(
                r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
                r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?):"
                r"(?:6553[0-5]|655[0-2][0-9]|654[0-9]{2}|6[0-4][0-9]{3}|[1-5]?[0-9]{1,4})\b"
            )
            matches = re.findall(pattern, parsed_text)
            proto = self.method.lower()
            if proto not in ("http", "https", "socks4", "socks5"):
                proto = "socks5" if "socks" in proto else "http"
            return [(m, proto) for m in matches]
        except Exception:
            return []

class SpysMeScraper(Scraper):
    def __init__(self, method: str):
        super().__init__(method, "https://spys.me/{mode}.txt")

    def get_url(self, **kwargs) -> str:
        mode = "proxy" if self.method == "http" else "socks"
        return super().get_url(mode=mode, **kwargs)

class ProxyScrapeScraper(Scraper):
    def __init__(self, method: str, timeout: int = 2000, country: str = "All"):
        self.timeout = timeout
        self.country = country
        super().__init__(method,
                         "https://api.proxyscrape.com/?request=getproxies"
                         "&proxytype={method}"
                         "&timeout={timeout}"
                         "&country={country}")

    def get_url(self, **kwargs) -> str:
        return super().get_url(timeout=self.timeout, country=self.country, **kwargs)

class GeoNodeScraper(Scraper):
    def __init__(self, method: str, limit: str = "300", page: str = "1"):
        self.limit = limit
        self.page = page
        super().__init__(method,
                         "https://proxylist.geonode.com/api/proxy-list?"
                         "limit={limit}&page={page}&sort_by=lastChecked&sort_type=desc")

    def get_url(self, **kwargs) -> str:
        return super().get_url(limit=self.limit, page=self.page, **kwargs)

class ProxyListDownloadScraper(Scraper):
    def __init__(self, method: str, anon: str = "elite"):
        self.anon = anon
        super().__init__(method, "https://www.proxy-list.download/api/v1/get?type={method}&anon={anon}")

    def get_url(self, **kwargs) -> str:
        return super().get_url(anon=self.anon, **kwargs)

class GeneralTableScraper(Scraper):
    async def handle(self, text: str) -> str:
        proxies = set()
        rows = re.findall(r'<tr>(.*?)</tr>', text, re.DOTALL)
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(cells) >= 2:
                ip = re.sub(r'<[^>]+>', '', cells[0]).strip()
                port = re.sub(r'<[^>]+>', '', cells[1]).strip()
                if re.match(r'^\d{1,3}(?:\.\d{1,3}){3}$', ip) and port.isdigit():
                    proxies.add(f"{ip}:{port}")
        return "\n".join(proxies)

class GitHubScraper(Scraper):
    async def handle(self, text: str) -> str:
        lines = text.split("\n")
        proxies = set()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if "://" in line:
                proxies.add(line.split("://")[-1])
            else:
                proxies.add(line)
        return "\n".join(proxies)

SCRAPERS = [
    SpysMeScraper("http"),
    SpysMeScraper("socks"),
    ProxyScrapeScraper("http"),
    ProxyScrapeScraper("socks4"),
    ProxyScrapeScraper("socks5"),
    GeoNodeScraper("socks"),
    ProxyListDownloadScraper("https", "elite"),
    ProxyListDownloadScraper("http", "elite"),
    ProxyListDownloadScraper("http", "transparent"),
    ProxyListDownloadScraper("http", "anonymous"),
    GeneralTableScraper("https", "http://sslproxies.org"),
    GeneralTableScraper("http", "http://free-proxy-list.net"),
    GeneralTableScraper("http", "http://us-proxy.org"),
    GeneralTableScraper("socks", "http://socks-proxy.net"),
    GitHubScraper("http", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    GitHubScraper("socks4", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    GitHubScraper("socks5", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    GitHubScraper("http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt"),
    GitHubScraper("socks", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt"),
    GitHubScraper("https", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt"),
    GitHubScraper("http", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt"),
    GitHubScraper("socks4", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks4.txt"),
    GitHubScraper("socks5", "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt"),
]

def get_country_flag(country_code: str) -> str:
    if not country_code or len(country_code) != 2:
        return "🌐"
    country_code = country_code.upper()
    return chr(127397 + ord(country_code[0])) + chr(127397 + ord(country_code[1]))

# ==================== CHECKER & ENGINE ====================

VERIFICATION_ENDPOINTS = [
    {"url": "http://ip-api.com/json?fields=status,query,country,countryCode,org", "type": "ip-api"},
    {"url": "https://api.ipify.org?format=json", "type": "ipify"},
    {"url": "https://httpbin.org/ip", "type": "httpbin"}
]

async def check_single_proxy(proxy_str: str, scheme: str = "http", timeout: float = 4.0) -> Optional[Dict]:
    """
    Recipe 1, 2, 3, 5 Fixes:
    1. Uses aiohttp_socks for SOCKS4/SOCKS5 proxies, native HTTP for http/https.
    2. Rotates test endpoints to prevent HTTP 429 rate-limiting on ip-api.com.
    3. Queries pathless endpoints to verify true exit node IP instead of parameter spoofing.
    5. Preserves protocol scheme metadata in final output dictionary.
    """
    proto = scheme.lower()
    if proto not in ("http", "https", "socks4", "socks5"):
        proto = "socks5" if "socks" in proto else "http"

    proxy_url = f"{proto}://{proxy_str}"
    ip_parts = proxy_str.split(':')
    proxy_ip = ip_parts[0]
    port_str = ip_parts[1] if len(ip_parts) > 1 else '80'

    endpoint = random.choice(VERIFICATION_ENDPOINTS)
    start_time = time.time()

    try:
        if proto.startswith("socks"):
            connector = aiohttp_socks.ProxyConnector.from_url(proxy_url, ssl=False)
            session_kwargs = {"connector": connector}
            get_kwargs = {}
        else:
            connector = aiohttp.TCPConnector(ssl=False)
            session_kwargs = {"connector": connector}
            get_kwargs = {"proxy": f"http://{proxy_str}"}

        async with aiohttp.ClientSession(**session_kwargs) as session:
            async with session.get(endpoint["url"], timeout=aiohttp.ClientTimeout(total=timeout), **get_kwargs) as resp:
                ping_ms = int((time.time() - start_time) * 1000)
                if resp.status == 200:
                    data = await resp.json()
                    country = "Unknown"
                    code = ""

                    if endpoint["type"] == "ip-api":
                        country = data.get('country', 'Unknown')
                        code = data.get('countryCode', '')
                    else:
                        # Secondary endpoints don't return country; perform light fast lookup if needed
                        pass

                    flag = get_country_flag(code)
                    return {
                        'raw': proxy_str,
                        'server': proxy_url,
                        'scheme': proto,
                        'ip': proxy_ip,
                        'port': port_str,
                        'country': country,
                        'country_code': code,
                        'flag': flag,
                        'ping_ms': ping_ms
                    }
    except Exception:
        pass
    return None

async def fetch_and_test_live_proxies(target_limit: int = 15, timeout: float = 4.0, update_cb: Optional[Callable] = None) -> List[Dict]:
    """
    Recipe 4 & 5 Fixes:
    Uses a Producer-Consumer Worker Queue (asyncio.Queue) with a fixed pool of 40 worker tasks.
    Prevents coroutine explosions, high CPU overhead, and unhandled task cancellation warnings.
    """
    if update_cb:
        await update_cb(f"🔍 <b>Scraping proxy sources...</b>\n<code>Querying 20+ public proxy repositories</code>")

    all_scraped: Dict[str, str] = {} # proxy_str -> scheme
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        async def run_scraper(s: Scraper):
            try:
                items = await s.scrape(session)
                for item_str, proto in items:
                    if re.match(r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?):\d{1,5}$", item_str):
                        if item_str not in all_scraped:
                            all_scraped[item_str] = proto
            except Exception:
                pass

        await asyncio.gather(*[run_scraper(s) for s in SCRAPERS])

    scraped_items = list(all_scraped.items())
    random.shuffle(scraped_items)

    if update_cb:
        await update_cb(f"⚡ <b>Validating Proxies...</b>\n<code>Scraped {len(scraped_items)} unique IPs. Testing live connection...</code>")

    queue: asyncio.Queue = asyncio.Queue()
    for item in scraped_items[:600]:
        queue.put_nowait(item)

    live_proxies: List[Dict] = []
    stop_event = asyncio.Event()

    async def worker():
        while not queue.empty() and not stop_event.is_set():
            try:
                p_str, scheme = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            res = await check_single_proxy(p_str, scheme=scheme, timeout=timeout)
            if res and not stop_event.is_set():
                live_proxies.append(res)
                if len(live_proxies) >= target_limit:
                    stop_event.set()
            queue.task_done()

    # Fixed pool of 40 worker coroutines
    num_workers = min(40, len(scraped_items))
    worker_tasks = [asyncio.create_task(worker()) for _ in range(num_workers)]

    while worker_tasks and not stop_event.is_set():
        done, worker_tasks = await asyncio.wait(worker_tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
        if len(live_proxies) >= target_limit:
            stop_event.set()
            break

    # Clean up remaining worker tasks cleanly
    for task in worker_tasks:
        if not task.done():
            task.cancel()
    if worker_tasks:
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    live_proxies.sort(key=lambda x: x['ping_ms'])
    return live_proxies


import asyncio
import re
import time
import random
import aiohttp
from typing import List, Dict, Optional, Callable

# ==================== SCRAPER SOURCES ====================

class Scraper:
    def __init__(self, method: str, _url: str):
        self.method = method
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

    async def scrape(self, session: aiohttp.ClientSession) -> List[str]:
        try:
            raw_text = await self.get_response_text(session)
            parsed_text = await self.handle(raw_text)
            # Recipe 3 Fix: Strict IPv4 octet (0-255) and port range (1-65535) regex validation
            pattern = re.compile(
                r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
                r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?):"
                r"(?:6553[0-5]|655[0-2][0-9]|654[0-9]{2}|6[0-4][0-9]{3}|[1-5]?[0-9]{1,4})\b"
            )
            return re.findall(pattern, parsed_text)
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
        # Parse table cells via regex
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

async def check_single_proxy(session: aiohttp.ClientSession, proxy_str: str, timeout: float = 4.0) -> Optional[Dict]:
    """Tests a single IP:PORT proxy against ip-api.com to verify connectivity & latency using aiohttp."""
    proxy_url = f"http://{proxy_str}"
    ip_parts = proxy_str.split(':')
    proxy_ip = ip_parts[0]
    
    start_time = time.time()
    try:
        async with session.get(f"http://ip-api.com/json/{proxy_ip}?fields=status,country,countryCode,org",
                               proxy=proxy_url,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            ping_ms = int((time.time() - start_time) * 1000)
            if resp.status == 200:
                data = await resp.json()
                country = data.get('country', 'Unknown')
                code = data.get('countryCode', '')
                flag = get_country_flag(code)
                return {
                    'raw': proxy_str,
                    'server': proxy_url,
                    'ip': proxy_ip,
                    'port': ip_parts[1] if len(ip_parts) > 1 else '80',
                    'country': country,
                    'country_code': code,
                    'flag': flag,
                    'ping_ms': ping_ms
                }
    except Exception:
        pass
    return None

async def fetch_and_test_live_proxies(target_limit: int = 15, timeout: float = 4.0, update_cb: Optional[Callable] = None) -> List[Dict]:
    """Scrapes 20+ sources and concurrently tests proxies until target_limit live working proxies are found."""
    if update_cb:
        await update_cb(f"🔍 <b>Scraping proxy sources...</b>\n<code>Querying 20+ public proxy repositories</code>")

    all_scraped = set()
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        async def run_scraper(s: Scraper):
            try:
                items = await s.scrape(session)
                for item in items:
                    if re.match(r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?):\d{1,5}$", item):
                        all_scraped.add(item)
            except Exception:
                pass

        await asyncio.gather(*[run_scraper(s) for s in SCRAPERS])

    scraped_list = list(all_scraped)
    random.shuffle(scraped_list)
    
    if update_cb:
        await update_cb(f"⚡ <b>Validating Proxies...</b>\n<code>Scraped {len(scraped_list)} unique IPs. Testing live connection...</code>")

    live_proxies = []
    stop_event = asyncio.Event()
    semaphore = asyncio.Semaphore(60)

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as check_session:
        async def worker(p_str: str):
            if stop_event.is_set():
                return
            async with semaphore:
                if stop_event.is_set():
                    return
                res = await check_single_proxy(check_session, p_str, timeout=timeout)
                if res:
                    live_proxies.append(res)
                    if len(live_proxies) >= target_limit:
                        stop_event.set()

        all_tasks = [asyncio.create_task(worker(p)) for p in scraped_list[:800]]
        tasks = list(all_tasks)
        
        while tasks and not stop_event.is_set():
            done, tasks = await asyncio.wait(tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            if len(live_proxies) >= target_limit:
                stop_event.set()
                break
                
        # Recipe 5 Fix: Clean up and await cancelled tasks to prevent pending task warnings
        for t in all_tasks:
            if not t.done():
                t.cancel()
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)

    live_proxies.sort(key=lambda x: x['ping_ms'])
    return live_proxies

import asyncio
import re
import json
import time
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp

try:
    from services.metrics import get_provider_health_matrix, record_provider_check
except ImportError:
    def get_provider_health_matrix(*args, **kwargs):
        return {}
    def record_provider_check(*args, **kwargs):
        pass
from .arabic_provider import ArabicProvider
from .asura_provider import AsuraProvider
from .base_provider import BaseProvider, get_cookies_for_url, update_site_auth_cache
from .bato_provider import BatoProvider
from .bilibili_provider import BilibiliProvider
from .comick_provider import ComickProvider
from .comix_provider import ComixProvider
from .custom_selector_scraper import CustomSelectorRule, parse_latest_from_html
from .generic_provider import GenericProvider
from .kakao_provider import KakaoProvider
from .madara_provider import MadaraProvider
from .mangadex_provider import MangaDexProvider
from .mangafire_provider import MangaFireProvider
from .manganato_provider import ManganatoProvider
from .mangapill_provider import MangaPillProvider
from .mangaplus_provider import MangaPlusProvider
from .naver_provider import NaverProvider
from .lekmanga_provider import LekMangaProvider
from .qimanhwa_provider import QimanhwaProvider
from .russian_providers import MangaLibProvider, MangaBuffProvider, GroupleProvider
from .suwayomi_provider import SuwayomiProvider

from .raw_providers import (
    AcQQProvider,
    IqiyiProvider,
    KuaikanProvider,
    LineMangaProvider,
    PiccomaProvider,
    LezhinProvider,
    ToptoonProvider,
    RidibooksProvider,
    ComicoProvider,
    JumptoonProvider,
    MechacomicProvider,
    MunpiaProvider,
    MrblueProvider,
    MantaProvider,
    BomtoonProvider,
    MangaupProvider,
)
from .shinigami_provider import ShinigamiProvider
from .tcbscans_provider import TCBScansProvider
from .vortex_provider import VortexProvider
from .webtoons_provider import WebtoonsProvider
from .weebcentral_provider import WeebCentralProvider

from .template_providers import resolve_provider as resolve_template_provider

try:
    from .playwright_provider import PlaywrightProvider  # type: ignore

    _PLAYWRIGHT_PROVIDER_AVAILABLE = True
except Exception:
    # يسمح بتشغيل البوت على استضافات خفيفة بدون Playwright
    PlaywrightProvider = None  # type: ignore
    _PLAYWRIGHT_PROVIDER_AVAILABLE = False


class ProviderManager:
    def __init__(self):
        self.generic = GenericProvider()
        self.madara = MadaraProvider(scraper=self.generic.scraper)
        self.asura = AsuraProvider()
        self.vortex = VortexProvider()
        self.qimanhwa = QimanhwaProvider()
        self.mangapill = MangaPillProvider()
        self.manganato = ManganatoProvider()
        self.webtoons = WebtoonsProvider()
        self.weebcentral = WeebCentralProvider()
        self.naver = NaverProvider()
        self.mangadex = MangaDexProvider()
        self.tcbscans = TCBScansProvider()
        self.comick = ComickProvider()
        self.comix = ComixProvider()
        self.mangafire = MangaFireProvider()
        self.bato = BatoProvider()
        self.arabic = ArabicProvider(scraper=self.generic.scraper)
        self.mangaplus = MangaPlusProvider()
        self.bilibili = BilibiliProvider()
        self.kakao = KakaoProvider()
        self.shinigami = ShinigamiProvider()
        self.acqq = AcQQProvider()
        self.kuaikan = KuaikanProvider()
        self.linemanga = LineMangaProvider()
        self.lekmanga = LekMangaProvider()
        self.mangabuff = MangaBuffProvider()
        self.mangalib = MangaLibProvider()
        self.grouple = GroupleProvider()
        self.suwayomi = SuwayomiProvider()

        self.piccoma = PiccomaProvider()
        self.iqiyi = IqiyiProvider()
        self.lezhin = LezhinProvider()
        self.toptoon = ToptoonProvider()
        self.ridibooks = RidibooksProvider()
        self.comico = ComicoProvider()
        self.jumptoon = JumptoonProvider()
        self.mechacomic = MechacomicProvider()
        self.munpia = MunpiaProvider()
        self.mrblue = MrblueProvider()
        self.manta = MantaProvider()
        self.bomtoon = BomtoonProvider()
        self.mangaup = MangaupProvider()
        self.playwright = (
            PlaywrightProvider() if _PLAYWRIGHT_PROVIDER_AVAILABLE else None
        )

        # Pass playwright reference to other providers
        for attr_name in list(self.__dict__.keys()):
            attr = getattr(self, attr_name)
            if isinstance(attr, BaseProvider) and attr_name != 'playwright':
                attr.playwright = self.playwright

        # ── RAW الأصلية ─────────────────────────────────────────────────────
        self.bilibili_sites = ["manga.bilibili.com", "bilibili.com/manga"]
        self.kakao_sites = ["page.kakao.com", "webtoon.kakao.com", "kakaopage.com"]
        self.acqq_sites = ["ac.qq.com", "ac.q.qq.com"]
        self.kuaikan_sites = ["kuaikanmanhua.com", "kuaikan.com"]
        self.linemanga_sites = ["manga.line.me", "lin.ee/manga"]
        self.mangabuff_sites = ["mangabuff.ru"]
        self.piccoma_sites = ["piccoma.com", "piccoma.jp"]
        self.iqiyi_sites = ["manhua.iqiyi.com", "iqiyi.com/manhua"]
        self.lezhin_sites = ["lezhin.com"]
        self.toptoon_sites = ["toptoon.com"]
        self.ridibooks_sites = ["ridibooks.com"]
        self.comico_sites = ["comico.jp", "comico.kr"]
        self.jumptoon_sites = ["jumptoon.com"]
        self.mechacomic_sites = ["mechacomic.jp"]
        self.munpia_sites = ["munpia.com"]
        self.mrblue_sites = ["mrblue.com"]
        self.manta_sites = ["manta.net"]
        self.bomtoon_sites = ["bomtoon.com"]
        self.mangaup_sites = ["manga-up.com", "mangaup.com"]

        self.manganato_sites = [
            "manganato",
            "mangakakalot",
            "manganelo",
            "chapmanganato",
            "readmanganato",
            "mangakakalots",
        ]
        self.bato_sites = ["bato.to", "batotoo.com", "dto.to", "bato.site"]
        self.comick_sites = ["comick.fun", "comick.io", "comick.cc", "comick.app"]
        self.comix_sites = ["comix.to"]
        self.mangafire_sites = ["mangafire.to"]

        self.arabic_sites = [
            "mangalek.com",
            "3asq.to",
            "3asq.net",
            "3asq.org",
            "manga-ar.com",
            "mangaarab.com",
            "manga-ar.net",
            "arabsama.com",
            "mangaae.com",
            "ozulscans.com",
            "mangat.to",
            "mangat.me",
            "mangazone.net",
            "gmanga.org",
            "onma.net",
            "mangaadm.com",
            "7oman.com",
            "shaymanga.net",
            "mangaswat.com",
            "mangatime.com",
        ]

        self.mangalib_sites = ["mangalib.me", "mangalib.org", "v2.mangalib.org", "hentailib.me", "slashlib.me", "yaoilib.me", "lib.social"]
        self.mangabuff_sites = ["mangabuff.ru"]
        self.grouple_sites = ["readmanga.live", "readmanga.me", "readmanga.app", "1.seimanga.me", "seimanga.me", "web.usagi.one", "usagi.one", "a.zazaza.me", "zazaza.me", "mintmanga.live", "mintmanga.me"]

        self.madara_sites = [
            "mgeko.cc",
            "mangageko.com",
            "en-thunderscans.com",
            "thunderscans.com",
            "godamh.com",
            "flamecomics.me",
            "flamecomics.com",
            "flamescans.org",
            "manhuaus.com",
            "kunmanga.com",
            "mangaclash.com",
            "1stkissmanga.io",
            "1stkissmanga.love",
            "1stkissmanhua.net",
            "247manga.com",
            "3asq.org",
            "adultwebtoon.com",
            "aedexnox.kawi.lat",
            "allporncomic.com",
            "alonescanlator.com.br",
            "alpha-scans.net",
            "animesama.fr",
            "anisascans.in",
            "apcomics.org",
            "apenasmaisumyaoi.com",
            "apollcomics.es",
            "aquamanga.com",
            "aquareader.net",
            "arabtoons.net",
            "araznovel.com",
            "arcanescans.com",
            "arcanescans.org",
            "arthurscan.xyz",
            "arvencomics.com",
            "aryascans.com",
            "asura.nacm.xyz",
            "azoramoon.com",
            "azuremanga.com",
            "bacakomik.co",
            "begatranslation.com",
            "bibliopanda.com",
            "biblioscan.me",
            "bokugents.com",
            "brainrotcomics.com",
            "brmangas.com",
            "brmangas.net",
            "centraldemangas.net",
            "chapscans.com",
            "cocomic.co",
            "coffeemanga.ink",
            "coffeemanga.io",
            "colamanga.com",
            "covendasbruxonas.com",
            "culturedworks.com",
            "darkscans.net",
            "deatte5.com",
            "disasterscans.com",
            "doujindesu.tv",
            "doujindistrict.com",
            "dragontea.ink",
            "dragontranslation.org",
            "drake-scans.com",
            "elftoon.com",
            "emperorscan.mundoalterno.org",
            "erosscans.xyz",
            "erosvoid.xyz",
            "erosxsun.xyz",
            "euphoriascan.com",
            "evascans.org",
            "evilflowers.com",
            "firescans.xyz",
            "flamecomics.io",
            "flamecomics.me",
            "flamecomics.xyz",
            "flamescans.lol",
            "flamescans.org",
            "flowermanga.net",
            "fr.mangatoto.com",
            "freecomiconline.me",
            "freemanga.me",
            "freemangatop.com",
            "galaxymanga.io",
            "galaxymanga.net",
            "galaxymanga.org",
            "garciamanga.com",
            "gdscans.com",
            "gedecomix.com",
            "ghostscan.xyz",
            "gourmetsupremacy.com",
            "grabber.zone",
            "gudangkomik.com",
            "harimanga.com",
            "harimanga.me",
            "harmony-scan.fr",
            "hayalistic.net",
            "hentai-origines.fr",
            "hentai20.io",
            "hentai4free.net",
            "hentaicb.live",
            "hentaimanga.me",
            "hentairead.com",
            "hentaiteca.net",
            "hentaivn.party",
            "hentaiwebtoon.com",
            "hentaixcomic.com",
            "hentaixdickgirl.com",
            "hentaixyuri.com",
            "hhentai.fr",
            "hiper.cool",
            "hiperdex.com",
            "hive-scans.com",
            "hivescans.com",
            "hotcabaretscan.com",
            "ikigaimangas.com",
            "immortalupdates.com",
            "infernalvoidscans.com",
            "inkreads.com",
            "inventariooculto.com",
            "isekaiscan.com",
            "isekaiscan.to",
            "isekaiscan.top",
            "isekaiscanmanga.com",
            "japanread.fr",
            "kappabeast.com",
            "kedi.to",
            "kingofshojo.com",
            "kiraproject.lat",
            "kirascans.com",
            "kiryuu.co",
            "kiryuu.id",
            "kissmanga.in",
            "klikmanga.id",
            "klikmanga.org",
            "klmanga.com",
            "klmanga.net",
            "komikcast.biz",
            "komikcast.com",
            "komiku.id",
            "komiku.org",
            "ksgroupscans.com",
            "kunmanga.com",
            "kuroimanga.com",
            "lagoonscans.com",
            "lavatoons.com",
            "laviniafansub.site",
            "lectorhub.j5z.xyz",
            "lectormangaa.com",
            "lectortmo.com",
            "leemiau.com",
            "leercomics.com",
            "leitor.borutoexplorer.com.br",
            "leitor.kamisama.com.br",
            "lelscan-vf.com",

            "leviatanscans.com",
            "lhtranslation.net",
            "likemanga.in",
            "lilymanga.net",
            "link-manga.com",
            "lmtos.com",
            "luminousscans.com",
            "luminousscans.net",
            "madaradex.org",
            "madarascans.com",
            "maidsecret.com",
            "manfra.de",
            "manga-chan.me",
            "manga-lc.net",
            "manga-scantrad.net",
            "manga-sehri.com",
            "manga108.org",
            "manga18free.com",
            "manga18fx.com",
            "manga18x.net",
            "manga3s.com",
            "manga4life.com",
            "manga68.com",
            "mangabaz.net",
            "mangabin.com",
            "mangabuddy.com",
            "mangaclash.com",
            "mangacrab.org",
            "mangacrazy.net",
            "mangadass.com",
            "mangadeutsch.com",
            "mangadistrict.com",
            "mangaeden.com",
            "mangaempress.com",
            "mangaes.net",
            "mangaforfree.com",
            "mangaforfree.net",
            "mangafoxfull.com",
            "mangafreak.online",
            "mangageko.com",
            "mangagg.com",
            "mangagojo.com",
            "mangahentai.me",
            "mangahere.cc",
            "mangajar.com",
            "mangakiss.org",
            "mangakomi.io",
            "mangaleveling.com",
            "mangalist.de",
            "mangalivre.net",
            "mangalivre.org",
            "mangamaniacs.org",
            "mangaonlineteam.com",
            "mangaowl.io",
            "mangaowl.net",
            "mangaowl.to",
            "mangaparadise.fr",
            "mangaraw.org",
            "mangarawjp.io",
            "mangaread.co",
            "mangaread.org",
            "mangaromance19.com",
            "mangas-origines.fr",
            "mangasee123.com",
            "mangasnosekai.com",
            "mangasproject.net",
            "mangasushi.org",
            "mangathailand.com",
            "mangatigre.com",
            "mangatigre.org",
            "mangatone.com",
            "mangatop.org",
            "mangatr.app",
            "mangatuk.com",
            "mangatx.com",
            "mangatyrant.com",
            "mangaworld.ac",
            "mangaworld.biz",
            "mangawow.org",
            "mangazin.org",
            "mangkomik.com",
            "mangkomik.id",
            "manhastro.net",
            "manhatic.com",
            "manhua-ga.org",
            "manhua88.com",
            "manhuaaz.com",
            "manhuadex.com",
            "manhuaes.com",
            "manhuafast.com",
            "manhuafast.net",
            "manhuahot.com",
            "manhuanext.com",
            "manhuaplus.com",
            "manhuarmmtl.com",
            "manhuascan.us",
            "manhuatop.org",
            "manhuaus.com",
            "manhuazone.net",
            "manhuazonghe.com",
            "manhwa18.com",
            "manhwa18.org",
            "manhwa68.com",
            "manhwabuddy.com",
            "manhwaclan.com",
            "manhwaden.com",
            "manhwafreaks.com",
            "manhwahentai.me",
            "manhwahentai.to",
            "manhwaindo.id",
            "manhwaindo.net",
            "manhwamanhua.com",
            "manhwas.men",
            "manhwatoon.me",
            "manhwatop.com",
            "manhwax.com",
            "manhwax.org",
            "manhwax.top",
            "manycomic.com",
            "manytoon.com",
            "manytoon.me",
            "mihentai.net",
            "milftoon.xxx",
            "millascan.com",
            "montetaiscanlator.xyz",
            "mrtenzus.com",
            "mugiwarasoficial.com",
            "mundomanhwa.com",
            "neroxus.com.br",
            "newcat1.xyz",
            "nightcomic.com",
            "nightscans.net",
            "nikatoons.com",
            "ninjacomics.xyz",
            "nitroscans.com",
            "niverafansub.org",
            "nobledicion.yoveo.xyz",
            "nocfsb.com",
            "noindexscan.com",
            "novelcrow.com",
            "novelmic.com",
            "noxenscan.com",
            "pantheon-scan.com",
            "paradise-bl.com",
            "paritehaber.com",
            "pawmanga.com",
            "petrotechsociety.org",
            "phenixscans.com",
            "phenixscans.fr",
            "piedpiperfansubyy.me",
            "platinumscans.com",
            "pomanga.com",
            "r1.richtoon.top",
            "rackusreads.com",
            "ragescans.com",
            "ragnarokscanlation.org",
            "ravenscans.com",
            "rawdex.net",
            "rawkuma.com",
            "rawmanga.top",
            "razure.org",
            "rdscans.com",
            "reader.decadencescans.com",
            "readfreecomics.com",
            "readm.org",
            "readmanga.live",
            "readmanga.today",
            "readmangabat.com",
            "readmanhua.com",
            "readmanhuax.com",
            "readmanhwa.com",
            "reapercomics.com",
            "reaperscans.com",
            "remanga.org",
            "reset-scans.com",
            "reset-scans.org",
            "restscans.com",
            "retsu.org",
            "rizzcomic.com",
            "rokaricomics.com",
            "rosesquadscans.aishiteru.org",
            "ruyamanga2.com",
            "s2manga.com",
            "s2manga.io",
            "scan-manga.com",
            "scan-vf.net",
            "scan-vf.to",
            "scansmangas.com",
            "scantrad-vf.co",
            "scantrad.net",
            "secretscans.com",
            "sectscans.com",
            "seitacelestial.com",
            "seraphic-deviltry.com",
            "setsuscans.com",
            "shibamanga.com",
            "shinigamid.me",
            "shootingstarscans.com",
            "silentquill.net",
            "sitemanga.com",
            "skymanga.work",
            "skymanga.xyz",
            "sleepytranslations.com",
            "spanish.seraphic-deviltry.com",
            "spidyscans.xyz",
            "spmanhwa.online",
            "stonescape.xyz",
            "summanga.com",
            "summertoons.com",
            "suryascans.com",
            "sushiscan.fr",
            "sushiscan.net",
            "tankouhentai.com",
            "taosect.com",
            "tappytoon.net",
            "tatakaescan.com",
            "tecnocomic1.xyz",
            "tecnoxmoon.xyz",
            "territorioleal.com",
            "theblank.net",
            "tiamanhwa.com",
            "timenaight.org",
            "tiraninha.baby",
            "tonizu.top",
            "toonchill.com",
            "toonclash.com",
            "toongod.org",
            "toonily.com",
            "toonily.net",
            "toonizy.com",
            "topcomicporno.com",
            "topmanhua.com",
            "topmanhua.net",
            "topreadmanhwa.com",
            "tritinia.org",
            "trmangaoku.com",
            "truyentranhdammyy.site",
            "truyenvn.shop",
            "tumangaonline.co",
            "tumangaonline.org",
            "unionmangas.net",
            "unionmangas.xyz",
            "utoon.co",
            "utoon.net",
            "vermanhwa.com",
            "void-scans.com",
            "vyvymanga.org",
            "wearehunger.site",
            "webdexscans.com",
            "webniichan.online",
            "webtoon.xyz",
            "webtoonempire-bl.com",
            "webtoonhatti.club",
            "webtoonscan.com",
            "webtoontr.net",
            "westmanga.id",
            "westmanga.net",
            "whalemanga.com",
            "witchscans.com",
            "woopread.com",
            "wuxiaworld.site",
            "x-manga.org",
            "yakshacomics.com",
            "yakshascans.com",
            "yaoibar.gay",
            "yaoihub.net",
            "yaoiscan.com",
            "yaoitoon.net",
            "zandynofansub.aishiteru.org",
            "zazamanga.com",
            "zeroscans.com",
            "zin-manga.com",
            "zinchangmanga.com",
            "zinchangmanga.net",
            "zinmanga.com",
            "zinmanga.net",
        ]

        # مواقع مخصصة مضافة من قاعدة البيانات
        self._custom_madara: list = []
        self._custom_arabic: list = []
        self._custom_generic: list = []
        self._custom_selectors: dict = {}
        self._custom_loaded = False
        self._provider_cache: dict[str, object] = {}
        self._host_provider_map: dict[str, object] = {}
        self._http_session: aiohttp.ClientSession | None = None
        self._rebuild_domain_provider_map()

    async def get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            connector = aiohttp.TCPConnector(
                limit=20, limit_per_host=10, enable_cleanup_closed=True
            )
            self._http_session = aiohttp.ClientSession(
                timeout=timeout, connector=connector
            )
        return self._http_session

    async def close_http_session(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    @staticmethod
    def _normalize_host(host: str) -> str:
        return host.lower().replace("www.", "").strip(".")

    def _extract_host(self, url: str) -> str:
        try:
            parsed = urlparse(url)
            host = parsed.netloc or url
            return self._normalize_host(host)
        except Exception:
            return self._normalize_host(url.split("/")[0])

    def _register_hosts(self, hosts: list[str], provider: object):
        for host in hosts:
            normalized = self._normalize_host(host)
            if normalized:
                self._host_provider_map[normalized] = provider

    def _rebuild_domain_provider_map(self):
        self._host_provider_map.clear()
        # RAW الأصلية
        self._register_hosts(self.bilibili_sites, self.bilibili)
        self._register_hosts(self.kakao_sites, self.kakao)
        self._register_hosts(self.acqq_sites, self.acqq)
        self._register_hosts(self.kuaikan_sites, self.kuaikan)
        self._register_hosts(self.linemanga_sites, self.linemanga)
        self._register_hosts(self.piccoma_sites, self.piccoma)
        self._register_hosts(self.iqiyi_sites, self.iqiyi)
        self._register_hosts(self.lezhin_sites, self.lezhin)
        self._register_hosts(self.toptoon_sites, self.toptoon)
        self._register_hosts(self.ridibooks_sites, self.ridibooks)
        self._register_hosts(self.comico_sites, self.comico)
        self._register_hosts(self.jumptoon_sites, self.jumptoon)
        self._register_hosts(self.mechacomic_sites, self.mechacomic)
        self._register_hosts(self.munpia_sites, self.munpia)
        self._register_hosts(self.mrblue_sites, self.mrblue)
        self._register_hosts(self.manta_sites, self.manta)
        self._register_hosts(self.bomtoon_sites, self.bomtoon)
        self._register_hosts(self.mangaup_sites, self.mangaup)
        # API / known providers
        self._register_hosts(["g.shinigami.asia", "shinigami.asia"], self.shinigami)
        self._register_hosts(["mangadex.org"], self.mangadex)
        self._register_hosts(["mangaplus.shueisha.co.jp"], self.mangaplus)
        self._register_hosts(self.comick_sites, self.comick)
        self._register_hosts(self.comix_sites, self.comix)
        self._register_hosts(self.mangafire_sites, self.mangafire)
        self._register_hosts(self.bato_sites, self.bato)
        self._register_hosts(["vortexscans.org", "vortexscans.com"], self.vortex)
        self._register_hosts(["webtoons.com"], self.webtoons)
        self._register_hosts(["comic.naver.com"], self.naver)
        self._register_hosts(["weebcentral.com"], self.weebcentral)
        self._register_hosts(["tcbscans.com", "tcb-scans.com"], self.tcbscans)
        self._register_hosts(["lekmanga.net", "lekmanga.org"], self.lekmanga)
        self._register_hosts(self.asura.DOMAINS, self.asura)
        self._register_hosts(self.mangalib_sites, self.mangalib)
        self._register_hosts(self.mangabuff_sites, self.mangabuff)
        self._register_hosts(self.grouple_sites, self.grouple)
        self._register_hosts(self.arabic_sites, self.arabic)
        self._register_hosts(self.madara_sites, self.madara)

    def _provider_from_host_map(self, host: str):
        # exact host
        if host in self._host_provider_map:
            return self._host_provider_map[host]
        # parent domains: a.b.c.com -> b.c.com -> c.com
        parts = host.split(".")
        for i in range(1, len(parts) - 1):
            candidate = ".".join(parts[i:])
            if candidate in self._host_provider_map:
                return self._host_provider_map[candidate]
        return None

    async def _load_custom_sites(self):
        """تحميل المواقع المخصصة وبيانات تسجيل الدخول من قاعدة البيانات."""
        try:
            import database

            self._custom_madara = await database.get_custom_madara_sites()
            self._custom_arabic = await database.get_custom_arabic_sites()
            all_sites = await database.get_custom_sites()
            self._custom_generic = [d[0] for d in all_sites if d[1] == "generic"]

            # ── Custom selectors rules (css/xpath + regex) ────────────────
            self._custom_selectors = {}
            try:
                for (
                    d,
                    sel,
                    url_attr,
                    num_re,
                    get_first,
                    use_browser,
                    notes,
                    raw_config,
                    updated_at,
                ) in await database.get_custom_selector_rules():
                    self._custom_selectors[d] = {
                        "domain": d,
                        "selector": sel,
                        "url_attr": url_attr or "href",
                        "number_regex": num_re or "",
                        "get_first": bool(get_first),
                        "use_browser": bool(use_browser),
                        "notes": notes or "",
                        "raw_config": raw_config or "",
                    }
            except Exception:
                self._custom_selectors = {}

            # تحميل بيانات تسجيل الدخول
            auth_cache = await database.get_all_site_auth_data()
            auth_cache = {d: a for d, a in auth_cache.items() if a}

            if auth_cache:
                update_site_auth_cache(auth_cache)
                print(f"[ProviderManager] Loaded auth for {len(auth_cache)} domains")

            self._custom_loaded = True
        except Exception as e:
            print(
                f"[ProviderManager] failed to load custom sites from DB, trying JSON fallback: {e}"
            )
            try:
                import json
                import os

                sites_path = (
                    "data/custom_sites_cache.json"
                    if os.path.exists("data")
                    else "custom_sites_cache.json"
                )
                auth_path = (
                    "data/site_auth_cache.json"
                    if os.path.exists("data")
                    else "site_auth_cache.json"
                )
                selectors_path = (
                    "data/custom_selectors_cache.json"
                    if os.path.exists("data")
                    else "custom_selectors_cache.json"
                )

                if os.path.exists(sites_path):
                    with open(sites_path, "r", encoding="utf-8") as f:
                        sites_data = json.load(f)
                        self._custom_madara = sites_data.get("madara", [])
                        self._custom_arabic = sites_data.get("arabic", [])
                        self._custom_generic = sites_data.get("generic", [])

                if os.path.exists(auth_path):
                    with open(auth_path, "r", encoding="utf-8") as f:
                        auth_cache = json.load(f)
                        if auth_cache:
                            update_site_auth_cache(auth_cache)
                            print(
                                f"[ProviderManager] Loaded auth for {len(auth_cache)} domains from JSON"
                            )

                self._custom_selectors = {}
                if os.path.exists(selectors_path):
                    with open(selectors_path, "r", encoding="utf-8") as f:
                        self._custom_selectors = json.load(f) or {}
                self._custom_loaded = True
            except Exception as e2:
                print(f"[ProviderManager] JSON fallback also failed: {e2}")

    async def reload_custom_sites(self):
        """إعادة تحميل المواقع المخصصة بعد إضافة جديدة."""
        await self._load_custom_sites()
        self._provider_cache.clear()
        self._rebuild_domain_provider_map()
        self._register_hosts(self._custom_arabic, self.arabic)
        self._register_hosts(self._custom_madara, self.madara)
        self._register_hosts(self._custom_generic, self.generic)
        print(
            f"[ProviderManager] Reloaded: {len(self._custom_madara)} madara, "
            f"{len(self._custom_arabic)} arabic, {len(self._custom_generic)} generic"
        )

    def get_provider(self, url: str):
        url_lower = url.lower()
        host = self._extract_host(url_lower)
        cached = self._provider_cache.get(host)
        if cached is not None:
            return cached

        # ── Config Router: قوالب المواقع من site_configs/*.json ───────────
        # يأتي قبل host_map عشان القوالب لها أولوية على الـ madara_sites
        try:
            template_provider = resolve_template_provider(url)
            if template_provider is not None:
                self._provider_cache[host] = template_provider
                return template_provider
        except Exception:
            pass

        mapped = self._provider_from_host_map(host)
        if mapped is not None:
            self._provider_cache[host] = mapped
            return mapped

        # ── RAW الأصلية ──────────────────────────────────────────────────
        if any(x in url_lower for x in self.bilibili_sites):
            return self.bilibili
        if any(x in url_lower for x in self.kakao_sites):
            return self.kakao
        if any(x in url_lower for x in self.acqq_sites):
            return self.acqq
        if any(x in url_lower for x in self.kuaikan_sites):
            return self.kuaikan
        if any(x in url_lower for x in self.linemanga_sites):
            return self.linemanga
        if any(x in url_lower for x in self.piccoma_sites):
            return self.piccoma
        if any(x in url_lower for x in self.iqiyi_sites):
            return self.iqiyi
        if any(x in url_lower for x in self.lezhin_sites):
            return self.lezhin
        if any(x in url_lower for x in self.toptoon_sites):
            return self.toptoon
        if any(x in url_lower for x in self.ridibooks_sites):
            return self.ridibooks
        if any(x in url_lower for x in self.comico_sites):
            return self.comico
        if any(x in url_lower for x in self.jumptoon_sites):
            return self.jumptoon
        if any(x in url_lower for x in self.mechacomic_sites):
            return self.mechacomic
        if any(x in url_lower for x in self.munpia_sites):
            return self.munpia

        # ── Shinigami ─────────────────────────────────────────────────────────
        if any(x in url_lower for x in ["g.shinigami.asia", "shinigami.asia"]):
            return self.shinigami

        # ── API مخصص ─────────────────────────────────────────────────────
        if "mangadex.org" in url_lower:
            return self.mangadex
        if "mangaplus.shueisha" in url_lower:
            return self.mangaplus
        if any(x in url_lower for x in self.comick_sites):
            return self.comick
        if any(x in url_lower for x in self.comix_sites):
            return self.comix
        if any(x in url_lower for x in self.mangafire_sites):
            return self.mangafire
        if any(x in url_lower for x in self.bato_sites):
            return self.bato

        # ── مزودات مخصصة ─────────────────────────────────────────────────
        if any(
            x in url_lower
            for x in [
                "asurascans",
                "asura.gg",
                "asuracomics",
                "asuratoon",
                "asuracomic.net",
                "asura.nacm.xyz",
                "asurascan",
            ]
        ):
            return self.asura
        if "vortexscans" in url_lower:
            return self.vortex
        if any(x in url_lower for x in ["qimanhwa", "qimanhua", "qimanga"]):
            return self.qimanhwa
        if "mangapill" in url_lower:
            return self.mangapill
        if any(s in url_lower for s in self.manganato_sites):
            return self.manganato
        if "webtoons.com" in url_lower:
            return self.webtoons
        if "comic.naver.com" in url_lower:
            return self.naver
        if "weebcentral.com" in url_lower:
            return self.weebcentral
        if any(x in url_lower for x in ["tcbscans", "tcb-scans"]):
            return self.tcbscans
        if any(s in url_lower for s in self.mangalib_sites):
            return self.mangalib
        if any(s in url_lower for s in self.mangabuff_sites):
            return self.mangabuff
        if any(s in url_lower for s in self.grouple_sites):
            return self.grouple

        # ── المواقع العربية ───────────────────────────────────────────────
        if any(s in url_lower for s in self.arabic_sites):
            return self.arabic
        # مخصصة عربية
        if any(s in url_lower for s in self._custom_arabic):
            return self.arabic

        # ── Madara WordPress ──────────────────────────────────────────────
        if any(s in url_lower for s in self.madara_sites):
            return self.madara
        # مخصصة Madara
        if any(s in url_lower for s in self._custom_madara):
            return self.madara

        # ── مخصصة Generic ────────────────────────────────────────────────
        if any(s in url_lower for s in self._custom_generic):
            self._provider_cache[host] = self.generic
            return self.generic

        # Fallback ذكي: استخدام GenericProvider لمعالجة أي موقع مجهول
        if hasattr(self, "generic") and self.generic:
            self._provider_cache[host] = self.generic
            return self.generic

        self._provider_cache[host] = self.suwayomi
        # cap cache growth
        if len(self._provider_cache) > 4096:
            self._provider_cache.clear()
        return self.suwayomi

    def get_provider_name(self, url: str) -> str:
        p = self.get_provider(url)
        return type(p).__name__.replace("Provider", "")

    def has_auth_cookies(self, url: str) -> bool:
        return bool(get_cookies_for_url(url))

    def is_chapter_url(self, url: str) -> bool:
        url_lower = url.lower().rstrip("/")
        if "comic.naver.com" in url_lower and "/detail" in url_lower:
            return True
        if any(x in url_lower for x in ["/chapter", "-chapter", "/viewer/", "/reader/", "/comicview/", "/ep-", "/episode", "/ch-"]):
            return True
        # Check if it ends with a number (like /123 or /12.5)
        parts = url_lower.split("/")
        if parts:
            last = parts[-1]
            import re
            if re.match(r"^\d+(?:\.\d+)?$", last):
                # Exclude IDs like /id/656716 or /mc123
                if len(parts) >= 2 and parts[-2] in ("id", "mc", "title", "manga", "series", "comic"):
                    return False
                return True
            # Check if it looks like "ch-123" or "chapter-123"
            if re.match(r"^(?:ch|chapter|ep|episode|volume|vol)-\d+(?:\.\d+)?$", last):
                return True
        return False

    def resolve_series_url(self, url: str) -> str:
        import re
        url_lower = url.lower()

        # Mangabuff.ru
        if "mangabuff.ru" in url_lower:
            parts = url.rstrip("/").split("/")
            try:
                idx = parts.index("manga")
                if idx + 1 < len(parts):
                    slug = parts[idx + 1]
                    return f"https://mangabuff.ru/manga/{slug}"
            except ValueError:
                pass
        
        # 1. AC.QQ
        if "ac.qq.com" in url_lower:
            m = re.search(r'/id/(\d+)', url)
            if m:
                return f"https://ac.qq.com/Comic/comicInfo/id/{m.group(1)}"
                
        # 2. ComicK
        if any(x in url_lower for x in ["comick.fun", "comick.io", "comick.cc"]):
            parts = [p for p in url.split("/") if p]
            try:
                idx = parts.index("comic")
                if idx + 1 < len(parts):
                    slug = parts[idx + 1]
                    return f"https://comick.io/comic/{slug}"
            except ValueError:
                pass
                
        # Naver Webtoon
        if "comic.naver.com" in url_lower:
            from urllib.parse import urlparse, parse_qs
            try:
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                if "titleId" in qs:
                    title_id = qs["titleId"][0]
                    # Force to desktop domain to get correct og:title metadata on list page
                    return f"https://comic.naver.com/webtoon/list?titleId={title_id}"
            except Exception:
                pass

        # Asura Scans
        if any(x in url_lower for x in ["asurascans.com", "asura.gg", "asuracomics.com", "asuratoon.com", "asuracomic.net"]):
            if "/chapter/" in url_lower:
                idx = url_lower.find("/chapter/")
                return url[:idx]
                
        return url

    async def resolve_series_url_async(self, url: str) -> str:
        import re
        from urllib.parse import urljoin
        from bs4 import BeautifulSoup
        
        # 1. Static resolution first
        resolved_url = self.resolve_series_url(url)
        if not self.is_chapter_url(resolved_url):
            return resolved_url
            
        # 2. Try static patterns to avoid fetching HTML if possible
        # E.g. stripping chapter segment if parent is manga/series/etc.
        parts = resolved_url.rstrip("/").split("/")
        if len(parts) >= 5:
            if parts[-3].lower() in ("manga", "series", "comic", "webtoon", "comics", "detail", "work"):
                return "/".join(parts[:-1]) + "/"
                
        # 3. Dynamic resolution: Fetch chapter page HTML and extract parent series link
        try:
            provider = self.get_provider(resolved_url)
            loop = asyncio.get_event_loop()
            html = await loop.run_in_executor(None, provider.fetch_html, resolved_url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                series_link = None
                
                # Check typical breadcrumbs/series links on chapter page
                for sel in [
                    "a[href*='/manga/']",
                    "a[href*='/series/']",
                    "a[href*='/comic/']",
                    "a[href*='/work/']",
                    "a.breadcrumb-link",
                    ".breadcrumbs a",
                    "a[href*='/web/topic/']",
                    "a[class*='back-to']",
                    "a[class*='return']",
                    "a[id*='back']",
                ]:
                    for el in soup.select(sel):
                        href = el.get("href")
                        if href:
                            abs_href = urljoin(resolved_url, href)
                            if abs_href.count("/") >= 4 and not self.is_chapter_url(abs_href):
                                low = abs_href.lower()
                                if not any(x in low for x in ["/login", "/register", "/home", "/user"]):
                                    series_link = abs_href
                                    break
                    if series_link:
                        break
                        
                if series_link:
                    return series_link
        except Exception as e:
            print(f"[resolve_series_url_async] Error resolving {url}: {e}")
            
        return resolved_url

    async def get_series_title(self, url: str) -> str | None:
        import re
        from bs4 import BeautifulSoup

        resolved_url = await self.resolve_series_url_async(url)

        # ── comix.to: parse #initial-data JSON ────────────────────────────
        if "comix.to" in resolved_url:
            try:
                loop = asyncio.get_event_loop()
                html = await loop.run_in_executor(None, self.generic.fetch_html, resolved_url)
                if html:
                    import json as _json

                    soup = BeautifulSoup(html, "html.parser")
                    tag = soup.find("script", id="initial-data")
                    if tag and tag.string:
                        data = _json.loads(tag.string)
                        for _k, v in data.get("queries", {}).items():
                            if isinstance(v, dict):
                                metadata = v.get("metadata", {})
                                if isinstance(metadata, dict):
                                    title = metadata.get("title")
                                    if title:
                                        return title
            except Exception:
                pass

        if "mangadex.org" in resolved_url:
            try:
                m = re.search(r"mangadex\.org/title/([a-z0-9-]+)", resolved_url)
                if not m:
                    m = re.search(r"mangadex\.org/chapter/([a-z0-9-]+)", resolved_url)
                    if m:
                        # Fetch chapter api to get manga id
                        s = await self.get_http_session()
                        async with s.get(f"https://api.mangadex.org/chapter/{m.group(1)}?includes[]=manga") as r:
                            if r.status == 200:
                                ch_data = await r.json()
                                for rel in ch_data.get("data", {}).get("relationships", []):
                                    if rel["type"] == "manga":
                                        mid = rel["id"]
                                        async with s.get(f"https://api.mangadex.org/manga/{mid}") as r2:
                                            if r2.status == 200:
                                                data = await r2.json()
                                                attrs = data.get("data", {}).get("attributes", {})
                                                title_obj = attrs.get("title", {})
                                                return (
                                                    title_obj.get("en")
                                                    or title_obj.get("ja-ro")
                                                    or next(iter(title_obj.values()), None)
                                                )
                if m:
                    mid = m.group(1)
                    s = await self.get_http_session()
                    async with s.get(f"https://api.mangadex.org/manga/{mid}") as r:
                        if r.status == 200:
                            data = await r.json()
                            attrs = data.get("data", {}).get("attributes", {})
                            title_obj = attrs.get("title", {})
                            title = (
                                title_obj.get("en")
                                or title_obj.get("ja-ro")
                                or next(iter(title_obj.values()), None)
                            )
                            if title:
                                return title
            except Exception:
                pass

        if any(x in resolved_url for x in ["comick.fun", "comick.io", "comick.cc"]):
            try:
                slug = resolved_url.rstrip("/").split("/")[-1]
                s = await self.get_http_session()
                async with s.get(f"https://api.comick.fun/comic/{slug}") as r:
                    if r.status == 200:
                        d = await r.json()
                        title = d.get("comic", {}).get("title")
                        if title:
                            return title
            except Exception:
                pass

        loop = asyncio.get_event_loop()

        def _scrape():
            try:
                nonlocal resolved_url
                provider = self.get_provider(resolved_url)
                html = provider.fetch_html(resolved_url)
                if not html:
                    return None
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(html, "html.parser")
                
                # Already resolved asynchronously in parent caller
                pass

                # Site specific selectors
                if "ac.qq.com" in resolved_url:
                    el = soup.select_one(".works-intro-title")
                    if el:
                        t = el.get_text(strip=True)
                        if t:
                            return t

                if "comic.naver.com" in resolved_url:
                    og = soup.find("meta", property="og:title") or soup.find(
                        "meta", attrs={"name": "og:title"}
                    )
                    if og and og.get("content"):
                        t = og["content"].strip()
                        t = re.split(r'\s*-\s*\d+화', t)[0] # remove episode suffix like "- 122화"
                        t = t.split(" :: ")[0].strip()
                        t = t.replace(" :: 네이버 웹툰", "").replace(" :: 네이버웹툰", "").strip()
                        if t and t.lower() not in ("네이버 웹툰", "네이버웹툰", "comic.naver.com"):
                            return t
                    for sel in [".EpisodeListInfo__title", ".title"]:
                        el = soup.select_one(sel)
                        if el:
                            t = el.get_text(strip=True)
                            if t and t.lower() not in ("comic.naver.com",):
                                return t

                # 1. Specific title selectors (likely to be the actual title)
                for sel in [
                    "h1.entry-title",
                    "h1.title",
                    ".post-title",
                    ".manga-title",
                ]:
                    el = soup.select_one(sel)
                    if el:
                        t = el.get_text(strip=True)
                        if t and t.lower() not in ("comic.naver.com", "naver webtoon", "naverwebtoon", "status"):
                            return t

                # 2. OG / Twitter titles (very specific sharing metadata)
                og = soup.find("meta", property="og:title") or soup.find(
                    "meta", attrs={"name": "og:title"}
                )
                if og and og.get("content"):
                    t = og["content"].strip()
                    if t and t.lower() not in ("comic.naver.com", "naver webtoon", "naverwebtoon", "status"):
                        return t
                    
                tw = soup.find("meta", attrs={"name": "twitter:title"})
                if tw and tw.get("content"):
                    t = tw["content"].strip()
                    if t and t.lower() not in ("comic.naver.com", "naver webtoon", "naverwebtoon", "status"):
                        return t

                # 3. HTML Title tag
                if soup.title and soup.title.string:
                    t = soup.title.string.strip()
                    t = re.split(r'\s*[-_|]\s*(?:在线漫画|在线|下拉式|下拉式漫画|腾讯动漫|快看漫画|MangaDex|Read Manga)', t)[0]
                    t = t.strip()
                    if t and t.lower() not in ("comic.naver.com", "naver webtoon", "naverwebtoon", "status"):
                        return t

                # 4. General h1 selector (as last resort)
                el = soup.select_one("h1")
                if el:
                    t = el.get_text(strip=True)
                    if t and t.lower() not in ("comic.naver.com", "naver webtoon", "naverwebtoon", "status"):
                        return t
            except Exception:
                pass
            return None

        return await loop.run_in_executor(None, _scrape)

    async def get_series_cover(self, url: str) -> str | None:
        import re
        from bs4 import BeautifulSoup

        resolved_url = await self.resolve_series_url_async(url)

        # ── comix.to: parse #initial-data JSON ────────────────────────────
        if "comix.to" in resolved_url:
            try:
                loop = asyncio.get_event_loop()
                html = await loop.run_in_executor(None, self.generic.fetch_html, resolved_url)
                if html:
                    import json as _json

                    soup = BeautifulSoup(html, "html.parser")
                    tag = soup.find("script", id="initial-data")
                    if tag and tag.string:
                        data = _json.loads(tag.string)
                        for _k, v in data.get("queries", {}).items():
                            if isinstance(v, dict):
                                poster = v.get("poster", {})
                                if isinstance(poster, dict):
                                    cover = poster.get("large") or poster.get(
                                        "medium", ""
                                    )
                                    if cover and cover.startswith("http"):
                                        return cover
            except Exception:
                pass

        if "mangadex.org" in resolved_url:
            try:
                m = re.search(r"mangadex\.org/title/([a-z0-9-]+)", resolved_url)
                if not m:
                    m = re.search(r"mangadex\.org/chapter/([a-z0-9-]+)", resolved_url)
                    if m:
                        s = await self.get_http_session()
                        async with s.get(f"https://api.mangadex.org/chapter/{m.group(1)}?includes[]=manga") as r:
                            if r.status == 200:
                                ch_data = await r.json()
                                for rel in ch_data.get("data", {}).get("relationships", []):
                                    if rel["type"] == "manga":
                                        m = re.search(r"([a-z0-9-]+)", rel["id"])
                                        break
                if m:
                    mid = m.group(1)
                    s = await self.get_http_session()
                    async with s.get(
                        f"https://api.mangadex.org/manga/{mid}?includes[]=cover_art"
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                    for rel in data.get("data", {}).get("relationships", []):
                        if rel["type"] == "cover_art":
                            fn = rel.get("attributes", {}).get("fileName", "")
                            if fn:
                                return f"https://uploads.mangadex.org/covers/{mid}/{fn}.512.jpg"
            except Exception:
                pass

        if any(x in resolved_url for x in ["comick.fun", "comick.io", "comick.cc"]):
            try:
                slug = resolved_url.rstrip("/").split("/")[-1]
                s = await self.get_http_session()
                async with s.get(f"https://api.comick.fun/comic/{slug}") as r:
                    if r.status == 200:
                        d = await r.json()
                        cover = (
                            d.get("comic", {})
                            .get("md_covers", [{}])[0]
                            .get("b2key", "")
                        )
                        if cover:
                            return f"https://meo.comick.pictures/{cover}"
            except Exception:
                pass

        loop = asyncio.get_event_loop()

        def _scrape():
            try:
                nonlocal resolved_url
                provider = self.get_provider(resolved_url)
                html = provider.fetch_html(resolved_url)
                if not html:
                    return None
                from bs4 import BeautifulSoup

                soup = BeautifulSoup(html, "html.parser")

                # Already resolved asynchronously in parent caller
                from urllib.parse import urljoin

                og = soup.find("meta", property="og:image") or soup.find(
                    "meta", attrs={"name": "og:image"}
                )
                if og and og.get("content"):
                    return urljoin(resolved_url, og["content"].strip())
                tw = soup.find("meta", attrs={"name": "twitter:image"})
                if tw and tw.get("content"):
                    return urljoin(resolved_url, tw["content"].strip())
                for sel in [
                    "img.img-cover",
                    ".summary_image img",
                    ".thumb img",
                    ".series-thumb img",
                    ".manga-cover img",
                    "img.cover",
                    "img.wp-post-image",
                    ".poster img",
                    ".manga-info-pic img",
                    ".tab-summary img",
                    ".comic-cover img",
                    ".series-cover img",
                    ".book-cover img",
                    ".manga-poster img",
                    "img.attachment-post-thumbnail",
                    ".works-cover img",
                    ".works-intro-cover img",
                ]:
                    el = soup.select_one(sel)
                    if el:
                        src = (
                            el.get("src")
                            or el.get("data-src")
                            or el.get("data-lazy-src")
                            or el.get("data-cfsrc")
                            or ""
                        ).strip()
                        if src:
                            return urljoin(resolved_url, src)

                # Heuristic 1: find image with alt/title matching the series title candidate
                series_title_candidate = None
                for sel_h1 in ["h1.works-intro-title", "h1.entry-title", "h1.title", ".works-intro-title", "h1"]:
                    el_h1 = soup.select_one(sel_h1)
                    if el_h1:
                        series_title_candidate = el_h1.get_text(strip=True)
                        if series_title_candidate:
                            break
                if series_title_candidate:
                    for img in soup.find_all("img"):
                        alt_text = (img.get("alt") or img.get("title") or "").strip()
                        if alt_text == series_title_candidate:
                            src = (
                                img.get("src")
                                or img.get("data-src")
                                or img.get("data-lazy-src")
                                or img.get("data-cfsrc")
                                or ""
                            ).strip()
                            if src:
                                return urljoin(resolved_url, src)

                # Fallback: find any image with "cover", "poster", "thumb", "wp-post-image" in source/classes
                for img in soup.find_all("img"):
                    src = (
                        img.get("src")
                        or img.get("data-src")
                        or img.get("data-lazy-src")
                        or img.get("data-cfsrc")
                        or ""
                    ).strip()
                    if not src:
                        continue
                    classes = "".join(img.get("class", [])).lower()
                    src_low = src.lower()
                    if any(x in classes or x in src_low for x in ["cover", "poster", "thumb", "wp-post-image"]):
                        if "avatar" not in classes and "icon" not in classes and "logo" not in src_low and not src_low.endswith(".gif"):
                            return urljoin(resolved_url, src)
            except Exception:
                pass
            return None

        return await loop.run_in_executor(None, _scrape)

    async def get_chapters_with_lock_info(self, url: str) -> dict:
        if not hasattr(self, "_chapters_cache"):
            self._chapters_cache = {}
        
        import time
        now = time.time()
        if url in self._chapters_cache:
            ts, cached_res = self._chapters_cache[url]
            if now - ts < 180:  # ذاكرة تخزين مؤقت مدتها 3 دقائق
                return cached_res

        # Dynamic remote downloader check
        try:
            from remote_downloader import RemoteDownloader
            remote = RemoteDownloader()
        except Exception:
            remote = None

        if remote and remote.is_enabled:
            host = self._extract_host(url.lower())
            is_cf_domain = any(x in host for x in ["asura", "shinigami", "lekmanga"])
            if is_cf_domain:
                print(f"[ProviderManager] Routing get_chapters_with_lock_info to remote worker for: {url}")
                try:
                    res = await remote.get_chapters_with_lock_info(url)
                    if res:
                        self._chapters_cache[url] = (now, res)
                        return res
                except Exception as e:
                    print(f"[ProviderManager] Remote get_chapters_with_lock_info error: {e}")

        res = await self._get_chapters_with_lock_info_uncached(url)
        
        # Fallback to remote if local failed and remote is enabled
        if not res and remote and remote.is_enabled:
            print(f"[ProviderManager] Local failed. Falling back to remote worker for: {url}")
            try:
                res = await remote.get_chapters_with_lock_info(url)
            except Exception as e:
                print(f"[ProviderManager] Remote fallback get_chapters_with_lock_info error: {e}")

        if res:
            self._chapters_cache[url] = (now, res)
        return res

    async def _get_chapters_with_lock_info_uncached(self, url: str) -> dict:
        url = await self.resolve_series_url_async(url)
        if not self._custom_loaded:
            await self._load_custom_sites()

        from .paginated_scraper import PaginatedScraper

        provider = self.get_provider(url)
        
        if hasattr(provider, "get_chapters_with_lock_info"):
            try:
                res = await provider.get_chapters_with_lock_info(url)
                if res:
                    return res
            except Exception as e:
                print(f"[ProviderManager] Custom get_chapters_with_lock_info failed: {e}")

        pname = type(provider).__name__

        # ── Custom selectors override (للدومينات المضافة بدون Provider) ──
        try:
            host = self._extract_host(url.lower())
            rule_dict = self._custom_selectors.get(host)
            if rule_dict:
                rule = CustomSelectorRule(
                    domain=host,
                    selector=rule_dict.get("selector", ""),
                    url_attr=rule_dict.get("url_attr", "href"),
                    number_regex=rule_dict.get("number_regex", ""),
                    get_first=bool(rule_dict.get("get_first")),
                    use_browser=bool(rule_dict.get("use_browser")),
                    notes=rule_dict.get("notes", ""),
                    raw_config=rule_dict.get("raw_config", ""),
                )
                html = None
                if rule.use_browser and self.playwright:
                    html = await self.playwright.fetch_html_playwright(url)
                if not html:
                    loop = asyncio.get_event_loop()
                    html = await loop.run_in_executor(
                        None, self.generic.fetch_html, url
                    )
                if html:
                    # Parse all matching elements instead of just one if we want full list!
                    # Let's support parsing all chapters.
                    selector = (rule.selector or "").strip()
                    results = {}
                    config_json = (rule.raw_config or "").strip()
                    if not config_json and selector.startswith("{"):
                        config_json = selector

                    if config_json.startswith("{"):
                        import json
                        try:
                            cfg = json.loads(config_json)
                            item_sel = cfg.get("item") or cfg.get("item_selector")
                            link_sel = cfg.get("link") or cfg.get("link_selector")
                            title_sel = cfg.get("title") or cfg.get("title_selector")
                            url_attr = cfg.get("url_attr") or rule.url_attr or "href"
                            num_re = cfg.get("number_regex") or rule.number_regex or ""
                            
                            from bs4 import BeautifulSoup
                            soup = BeautifulSoup(html, "html.parser")
                            items = soup.select(item_sel)
                            for el in items:
                                text_el = el.select_one(title_sel) if title_sel else el
                                text = text_el.get_text(" ", strip=True) if text_el else el.get_text(" ", strip=True)
                                from .custom_selector_scraper import _extract_number, _abs_url
                                num = _extract_number(text, num_re)
                                href = ""
                                link_el = el.select_one(link_sel) if link_sel else el
                                if link_el:
                                    href = link_el.get(url_attr) or ""
                                if not href and link_el != el:
                                    a = link_el.find("a", href=True) if hasattr(link_el, "find") else None
                                    if not a:
                                        a = el.find("a", href=True)
                                    if a:
                                        href = a.get("href", "")
                                ch_url = _abs_url(url, href) if href else ""
                                if num is not None and ch_url:
                                    results[float(num)] = {
                                        "url": ch_url,
                                        "locked": False,
                                        "reason": "custom:json-kotatsu-all"
                                    }
                        except Exception as je:
                            print(f"[CustomSelector JSON] error: {je}")
                    else:
                        # css or xpath multiple items
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(html, "html.parser")
                        mode = "css"
                        expr = selector
                        if selector.startswith("css:"):
                            mode, expr = "css", selector[4:].strip()
                        elif selector.startswith("xpath:"):
                            mode, expr = "xpath", selector[6:].strip()

                        if mode == "css":
                            for el in soup.select(expr):
                                text = el.get_text(" ", strip=True)
                                from .custom_selector_scraper import _extract_number, _abs_url
                                num = _extract_number(text, rule.number_regex)
                                href = ""
                                if rule.url_attr:
                                    href = el.get(rule.url_attr) or ""
                                if not href:
                                    a = el.find("a", href=True)
                                    if a:
                                        href = a.get("href", "")
                                ch_url = _abs_url(url, href) if href else ""
                                if num is not None and ch_url:
                                    results[float(num)] = {
                                        "url": ch_url,
                                        "locked": False,
                                        "reason": "custom:css-all"
                                    }
                    if results:
                        return results
                    
                    num, ch_url, reason = parse_latest_from_html(html, url, rule)
                    if num is not None and ch_url:
                        return {
                            float(num): {
                                "url": ch_url,
                                "locked": False,
                                "reason": f"custom:{reason}",
                            }
                        }
        except Exception as e:
            print(f"[CustomSelector] error: {e}")

        if "Asura" in pname:
            try:
                if hasattr(provider, "get_chapters_with_lock_info"):
                    return await provider.get_chapters_with_lock_info(url)
            except Exception as e:
                print(f"[AsuraLock] {e}")

        if "Bilibili" in pname:
            try:
                import aiohttp

                comic_id = provider._extract_comic_id(url)
                if comic_id:
                    async with aiohttp.ClientSession(headers=provider.HEADERS) as s:
                        async with s.post(
                            f"{provider.API}/ComicDetail",
                            json={"comic_id": int(comic_id)},
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as r:
                            if r.status == 200:
                                data = await r.json()
                    if data.get("code") == 0:
                        result = {}
                        for ep in data.get("data", {}).get("ep_list", []):
                            ep_id = ep.get("id")
                            ord_ = ep.get("ord")
                            locked = ep.get("is_locked", True)
                            if ep_id and ord_:
                                try:
                                    result[float(ord_)] = {
                                        "url": f"https://manga.bilibili.com/mc{comic_id}/{ep_id}",
                                        "locked": locked,
                                        "reason": "bilibili-api",
                                    }
                                except Exception:
                                    pass
                        if result:
                            return result
            except Exception as e:
                print(f"[BilibiliLock] {e}")

        if pname in (
            "Generic",
            "Madara",
            "Arabic",
            "MangaFire",
            "Bato",
            "Vortex",
            "MangaPill",
            "Manganato",
            "WeebCentral",
            "Shinigami",
        ):
            try:
                scraper = PaginatedScraper(
                    fetch_fn=self.generic.fetch_html,
                    max_pages=50,
                )
                rich = await scraper.get_all_chapters(url, detect_lock=True)
                if rich:
                    print(f"[PaginatedScraper] {len(rich)} chapters from {url}")
                    locked_cnt = sum(1 for v in rich.values() if v.get("locked"))
                    if locked_cnt:
                        print(f"[PaginatedScraper] 🔒 {locked_cnt} locked chapters")
                    return rich
            except Exception as e:
                print(f"[PaginatedScraper] error: {e}")

        chapters = await self.get_all_chapters(url)
        return {
            num: {"url": ch_url, "locked": False, "reason": "no-lock-data"}
            for num, ch_url in chapters.items()
        }

    async def search_manga(self, query: str, limit: int = 10) -> list:
        try:
            params = {
                "title": query,
                "limit": limit,
                "order[relevance]": "desc",
                "contentRating[]": ["safe", "suggestive", "erotica"],
                "includes[]": ["cover_art"],
            }
            session = await self.get_http_session()
            async with session.get(
                "https://api.mangadex.org/manga", params=params
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()

            results = []
            for manga in data.get("data", []):
                attrs = manga.get("attributes", {})
                title_obj = attrs.get("title", {})
                title = (
                    title_obj.get("en")
                    or title_obj.get("ja-ro")
                    or next(iter(title_obj.values()), "Unknown")
                )
                desc_obj = attrs.get("description", {})
                desc = desc_obj.get("en", "")[:200] if desc_obj else ""
                status = attrs.get("status", "unknown")
                mid = manga["id"]
                url_out = f"https://mangadex.org/title/{mid}"

                cover_url = None
                for rel in manga.get("relationships", []):
                    if rel["type"] == "cover_art":
                        fname = rel.get("attributes", {}).get("fileName", "")
                        if fname:
                            cover_url = f"https://uploads.mangadex.org/covers/{mid}/{fname}.256.jpg"
                        break

                results.append(
                    {
                        "title": title,
                        "url": url_out,
                        "description": desc,
                        "status": status,
                        "cover": cover_url,
                    }
                )
            return results
        except Exception as e:
            print(f"[Search] error: {e}")
            return []

    async def get_latest_chapter(self, url: str) -> Optional[float]:
        # Dynamic remote downloader check
        try:
            from remote_downloader import RemoteDownloader
            remote = RemoteDownloader()
        except Exception:
            remote = None

        if remote and remote.is_enabled:
            host = self._extract_host(url.lower())
            is_cf_domain = any(x in host for x in ["asura", "shinigami", "lekmanga"])
            if is_cf_domain:
                print(f"[ProviderManager] Routing get_latest_chapter to remote worker for: {url}")
                try:
                    res = await remote.get_all_chapters(url)
                    if res:
                        return max(res.keys())
                except Exception as e:
                    print(f"[ProviderManager] Remote get_latest_chapter error: {e}")

        if not self._custom_loaded:
            await self._load_custom_sites()

        # ── Custom selectors shortcut ────────────────────────────────────
        try:
            host = self._extract_host(url.lower())
            rule_dict = self._custom_selectors.get(host)
            if rule_dict:
                rule = CustomSelectorRule(
                    domain=host,
                    selector=rule_dict.get("selector", ""),
                    url_attr=rule_dict.get("url_attr", "href"),
                    number_regex=rule_dict.get("number_regex", ""),
                    get_first=bool(rule_dict.get("get_first")),
                    use_browser=bool(rule_dict.get("use_browser")),
                    notes=rule_dict.get("notes", ""),
                )
                html = None
                if rule.use_browser and self.playwright:
                    html = await self.playwright.fetch_html_playwright(url)
                if not html:
                    loop = asyncio.get_event_loop()
                    html = await loop.run_in_executor(
                        None, self.generic.fetch_html, url
                    )
                if html:
                    num, _ch_url, _reason = parse_latest_from_html(html, url, rule)
                    if num is not None:
                        return float(num)
        except Exception:
            pass

        provider = self.get_provider(url)
        try:
            if asyncio.iscoroutinefunction(provider.get_latest_chapter):
                chapter = await provider.get_latest_chapter(url)
            else:
                loop = asyncio.get_event_loop()
                chapter = await loop.run_in_executor(
                    None, provider.get_latest_chapter, url
                )
            return float(chapter) if chapter is not None else None
        except Exception as e:
            print(f"[ProviderManager] get_latest_chapter error for {url}: {e}")
            return None

    async def get_all_chapters(self, url: str) -> dict:
        t0 = time.time()
        pname = self.get_provider_name(url)
        try:
            res = await self._get_all_chapters_internal(url)
            duration_ms = (time.time() - t0) * 1000.0
            record_provider_check(pname, success=bool(res), response_time_ms=duration_ms)
            return res
        except Exception as e:
            duration_ms = (time.time() - t0) * 1000.0
            record_provider_check(pname, success=False, response_time_ms=duration_ms)
            raise e

    async def _get_all_chapters_internal(self, url: str) -> dict:
        # Dynamic remote downloader check
        try:
            from remote_downloader import RemoteDownloader
            remote = RemoteDownloader()
        except Exception:
            remote = None

        if remote and remote.is_enabled:
            host = self._extract_host(url.lower())
            is_cf_domain = any(x in host for x in ["asura", "shinigami", "lekmanga"])
            if is_cf_domain:
                print(f"[ProviderManager] Routing get_all_chapters to remote worker for: {url}")
                try:
                    res = await remote.get_all_chapters(url)
                    if res:
                        return res
                except Exception as e:
                    print(f"[ProviderManager] Remote get_all_chapters error: {e}")

        if not self._custom_loaded:
            await self._load_custom_sites()

        # ── Custom selectors shortcut for get_all_chapters ───────────────
        try:
            host = self._extract_host(url.lower())
            rule_dict = self._custom_selectors.get(host)
            if rule_dict:
                from .custom_selector_scraper import parse_all_chapters_from_html
                rule = CustomSelectorRule(
                    domain=host,
                    selector=rule_dict.get("selector", ""),
                    url_attr=rule_dict.get("url_attr", "href"),
                    number_regex=rule_dict.get("number_regex", ""),
                    get_first=bool(rule_dict.get("get_first")),
                    use_browser=bool(rule_dict.get("use_browser")),
                    notes=rule_dict.get("notes", ""),
                    raw_config=rule_dict.get("raw_config", ""),
                )
                html = None
                if rule.use_browser and self.playwright:
                    html = await self.playwright.fetch_html_playwright(url)
                if not html:
                    loop = asyncio.get_event_loop()
                    html = await loop.run_in_executor(
                        None, self.generic.fetch_html, url
                    )
                if html:
                    chapters = parse_all_chapters_from_html(html, url, rule)
                    if chapters:
                        return chapters
        except Exception as e:
            print(f"[ProviderManager] Custom selector get_all_chapters error: {e}")

        provider = self.get_provider(url)
        try:
            if asyncio.iscoroutinefunction(provider.get_all_chapters):
                chapters = await provider.get_all_chapters(url)
            else:
                loop = asyncio.get_event_loop()
                chapters = await loop.run_in_executor(
                    None, provider.get_all_chapters, url
                )

            # --- Fallback to Playwright if empty (e.g. blocked by Cloudflare) ---
            if not chapters and self.playwright:
                print(
                    f"[ProviderManager] Fallback to Playwright for get_all_chapters: {url}"
                )
                html = await self.playwright.fetch_html_playwright(url)
                if html:
                    from .paginated_scraper import PaginatedScraper

                if html:
                    from .paginated_scraper import PaginatedScraper

                    async def playwright_fetch(u):
                        if u == url:
                            return html
                        return await self.playwright.fetch_html_playwright(u)

                    scraper = PaginatedScraper(fetch_fn=playwright_fetch)
                    chapters = await scraper.get_all_chapters(url, detect_lock=True)

                    # If it's Asura, let's also try parsing with Asura's custom parser if available
                    if "Asura" in type(provider).__name__ and hasattr(
                        provider, "_extract_rsc_data"
                    ):
                        asura_chapters = {}
                        try:
                            data = provider._extract_rsc_data(html)
                            if data and "chapters" in data:
                                clean_series_url = url.rstrip("/")
                                for ch in data["chapters"]:
                                    num_val = ch.get("number")
                                    name = ch.get("name")
                                    if num_val is not None:
                                        num = float(num_val)
                                        name_str = (
                                            str(int(num))
                                            if num.is_integer()
                                            else str(num)
                                        )
                                        if name:
                                            name_str = str(name).strip()
                                        asura_chapters[num] = (
                                            f"{clean_series_url}/chapter/{name_str}"
                                        )
                            if asura_chapters:
                                chapters.update(asura_chapters)
                        except Exception as ae:
                            print(
                                f"[ProviderManager] Asura fallback parsing error: {ae}"
                            )

            return chapters or {}
        except Exception as e:
            print(f"[ProviderManager] get_all_chapters error for {url}: {e}")
            return {}


    async def get_images(self, url: str) -> list:
        if not self._custom_loaded:
            await self._load_custom_sites()

        url_lower = url.lower()

        def _filter_noise(img_list: list) -> list:
            if not img_list:
                return []
            exclude_keywords = [
                "favicon", "logo", "avatar", "icon", "pixel", "tracking", "gravatar",
                "googleusercontent", "facebook", "twitter", "instagram", "discord",
                "banner", "header", "footer", "button", "advertisement", "promo",
                "widget", "comment", "user", "profile", "sprite", "spacer", "blank",
                "share", "telegram", "whatsapp", "cover", "thumb", "thumbnail", "poster",
                "wp-post-image", "lh3.google", "readerarea.svg"
            ]
            cleaned = []
            for img in img_list:
                if not img or not img.startswith("http"):
                    continue
                img_low = img.lower()
                if re.search(r'(?:^|[/._-])ads?(?:[/._-]|\d|$)', img_low):
                    continue
                if any(x in img_low for x in exclude_keywords):
                    continue
                if "qimanhwa" in url_lower and "upload/series/" not in img_low:
                    continue
                cleaned.append(img)
            return cleaned

        # ── Custom selectors shortcut for get_images ─────────────────────
        try:
            host = self._extract_host(url.lower())
            rule_dict = self._custom_selectors.get(host)
            if rule_dict:
                import json
                cfg = {}
                if rule_dict.get("raw_config"):
                    try:
                        cfg = json.loads(rule_dict["raw_config"])
                    except Exception:
                        pass
                image_selector = cfg.get("images") or cfg.get("image_selector")
                if image_selector:
                    rule = CustomSelectorRule(
                        domain=host,
                        selector=rule_dict.get("selector", ""),
                        url_attr=rule_dict.get("url_attr", "href"),
                        number_regex=rule_dict.get("number_regex", ""),
                        get_first=bool(rule_dict.get("get_first")),
                        use_browser=bool(rule_dict.get("use_browser")),
                        notes=rule_dict.get("notes", ""),
                        raw_config=rule_dict.get("raw_config", ""),
                    )
                    html = None
                    if rule.use_browser and self.playwright:
                        html = await self.playwright.fetch_html_playwright(url)
                    if not html:
                        loop = asyncio.get_event_loop()
                        html = await loop.run_in_executor(
                            None, self.generic.fetch_html, url
                        )
                    if html:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(html, "html.parser")
                        img_nodes = soup.select(image_selector)
                        img_attr = cfg.get("image_attr") or "src"
                        images = []
                        for img in img_nodes:
                            src = (
                                img.get(img_attr) or
                                img.get("data-src") or
                                img.get("data-lazy-src") or
                                img.get("data-original") or
                                img.get("data-cfsrc") or
                                img.get("data-url") or
                                img.get("data-lazy-load") or
                                img.get("data-actual-src") or
                                img.get("data-imagesource") or
                                img.get("data-echo") or
                                img.get("src")
                            )
                            if src:
                                from urllib.parse import urljoin
                                images.append(urljoin(url, src.strip()))
                        if images:
                            return _filter_noise(images)
        except Exception as e:
            print(f"[ProviderManager] Custom selector get_images error: {e}")

        is_comix = "comix.to" in url_lower

        # مواقع يفضل استخدام Playwright لها مباشرة (SPA أو حماية عالية)
        use_playwright_directly = any(
            x in url_lower
            for x in [
                "newtoki",
                "asuracomics",
                "genz",
                # Angular / React SPAs — fetch_html لا ينفع معها
                "qimanhwa",
                "qimanhua",
                "qimanga",
                "toonily",
                "toonizy",
                "manhwatop",
                "reaperscans",
                "reapercomics",
                "luminousscans",
                "lezhin",
                "toptoon",
                "ridibooks",
                "comico",
                "jumptoon",
                "mechacomic",
                "munpia",
            ]
        )

        if use_playwright_directly:
            print(f"[ProviderManager] Using Playwright for high-protection site: {url}")
            if self.playwright:
                return _filter_noise(await self.playwright.get_images(url))
            print(
                "[ProviderManager] Playwright غير متاح على هذه البيئة — سيتم المتابعة بدون Playwright"
            )

        provider = self.get_provider(url)
        try:
            if asyncio.iscoroutinefunction(provider.get_images):
                images = await provider.get_images(url)
            else:
                loop = asyncio.get_event_loop()
                images = await loop.run_in_executor(None, provider.get_images, url)

            if is_comix:
                if not images:
                    print("[ProviderManager] Comix provider returned no images; skipping generic Playwright fallback")
                    return []
                if len(images) < 3:
                    print(f"[ProviderManager] Comix provider returned suspicious image count ({len(images)}); rejecting")
                    return []
                return _filter_noise(images)

            # إذا فشل المزود العادي، جرب Playwright كحل أخير
            if not images:
                print(f"[ProviderManager] Fallback to Playwright for: {url}")
                if self.playwright:
                    images = await self.playwright.get_images(url)

            return _filter_noise(images)
        except Exception as e:
            import traceback
            print(f"[ProviderManager] get_images error: {type(e).__name__}")
            traceback.print_exc()
            if is_comix:
                return []
            # في حالة الخطأ، جرب Playwright أيضاً
            if self.playwright:
                try:
                    return _filter_noise(await self.playwright.get_images(url))
                except Exception:
                    return []
            return []

    def get_provider_health_matrix(self) -> dict:
        """Return structured provider health statistics matrix for display in /admin."""
        return get_provider_health_matrix()

    async def get_all_chapters_with_fallback(
        self, url: str, series_title: str | None = None
    ) -> dict:
        """
        Attempts to fetch all chapters from the primary provider.
        If primary provider fails (returns empty dict or raises exception),
        automatically queries secondary registered providers or mapped fallbacks for the series
        to prevent check failures during primary site downtime.
        """
        primary_name = self.get_provider_name(url)
        try:
            chs = await self.get_all_chapters(url)
            if chs:
                return chs
        except Exception as e:
            print(f"[FallbackEngine] Primary provider {primary_name} failed for {url}: {e}")

        print(f"[FallbackEngine] Primary provider {primary_name} failed/empty for {url}. Invoking multi-source fallbacks.")

        title = series_title
        if not title:
            try:
                title = await self.get_series_title(url)
            except Exception:
                pass
        if not title:
            parts = [p for p in url.rstrip("/").split("/") if p]
            if parts:
                title = parts[-1].replace("-", " ").replace("_", " ").title()

        if not title or len(title) < 2:
            return {}

        fallback_providers = [self.mangadex, self.comick, self.mangafire, self.bato, self.manganato]

        for fallback_p in fallback_providers:
            fp_name = type(fallback_p).__name__.replace("Provider", "")
            if fp_name == primary_name:
                continue
            try:
                t0 = time.time()
                chs = None
                if fallback_p is self.mangadex or "mangadex" in fp_name.lower():
                    searchResults = await self.search_manga(title, limit=3)
                    if searchResults:
                        target_url = searchResults[0]["url"]
                        if asyncio.iscoroutinefunction(fallback_p.get_all_chapters):
                            chs = await fallback_p.get_all_chapters(target_url)
                        else:
                            chs = fallback_p.get_all_chapters(target_url)
                    if not chs and hasattr(fallback_p, "get_all_chapters"):
                        if asyncio.iscoroutinefunction(fallback_p.get_all_chapters):
                            chs = await fallback_p.get_all_chapters(url)
                        else:
                            chs = fallback_p.get_all_chapters(url)
                elif fallback_p is self.comick or "comick" in fp_name.lower():
                    slug = title.lower().replace(" ", "-")
                    comick_url = f"https://comick.io/comic/{slug}"
                    if asyncio.iscoroutinefunction(fallback_p.get_all_chapters):
                        chs = await fallback_p.get_all_chapters(comick_url)
                    else:
                        chs = fallback_p.get_all_chapters(comick_url)
                else:
                    if hasattr(fallback_p, "get_all_chapters"):
                        if asyncio.iscoroutinefunction(fallback_p.get_all_chapters):
                            chs = await fallback_p.get_all_chapters(url)
                        else:
                            chs = fallback_p.get_all_chapters(url)

                if chs:
                    record_provider_check(fp_name, success=True, response_time_ms=(time.time() - t0) * 1000.0)
                    print(f"[FallbackEngine] Successfully recovered '{title}' via fallback {fp_name}")
                    return chs
            except Exception as fe:
                print(f"[FallbackEngine] Fallback provider {fp_name} error: {fe}")

        return {}

    async def get_latest_chapter_with_fallback(
        self, url: str, series_title: str | None = None
    ) -> float | None:
        ch = await self.get_latest_chapter(url)
        if ch is not None:
            return ch
        chs = await self.get_all_chapters_with_fallback(url, series_title=series_title)
        if chs:
            return max(chs.keys())
        return None


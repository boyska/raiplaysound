from datetime import datetime as dt
from datetime import timedelta
from itertools import chain
import os
from os.path import join as pathjoin
from typing import List, Optional
from urllib.parse import urljoin
import tempfile
import enum
import functools
import argparse
import logging

import requests
from feedendum import to_rss_string, Feed, FeedItem

NSITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def url_to_filename(url: str) -> str:
    return url.split("/")[-1] + ".xml"


def _datetime_parser(s: str) -> Optional[dt]:
    if not s:
        return None
    try:
        return dt.strptime(s, "%d-%m-%Y %H:%M:%S")
    except ValueError:
        pass
    try:
        return dt.strptime(s, "%d-%m-%Y %H:%M")
    except ValueError:
        pass
    try:
        return dt.strptime(s, "%Y-%m-%d")
    except ValueError:
        pass
    return None

from requests_ratelimiter import LimiterAdapter

def get_session(**ratelimiter_args):
    if get_session._cached_session is not None:
        return get_session._cached_session 
    s = requests.Session()
    if ratelimiter_args:
        adapter = LimiterAdapter(**ratelimiter_args)
        s.mount('https://', adapter)
        s.mount('http://', adapter)
    get_session._cached_session  = s
    return s
get_session._cached_session = None



class PageTypes(enum.IntFlag):
    """
    Given the current code, any page can only belong to a single category. But let's be generic.
    """
    GENERE = enum.auto()
    PROGRAMMA = enum.auto()
    FILM = enum.auto()
    SERIE = enum.auto()

    @classmethod
    def from_string(cls, typology: str):
        ret = cls.GENERE & 0  # That's zero

        if typology in ("film", "fiction"):
            ret |= cls.FILM
        elif typology in ("programmi radio", "informazione notiziari"):
            ret |= cls.PROGRAMMA
        elif typology in ("serie audio"):
            ret |= cls.SERIE
        else:
            ret |= cls.GENERE
        return ret


class RaiParser:
    def __init__(self, url: str, folderPath: str, recursive: bool = True) -> None:
        self.url = url
        self.folderPath = folderPath
        self.inner: List[Feed] = []
        self.recursive = recursive

    def extend(self, url: str) -> None:
        if not self.recursive:
            return
        url = urljoin(self.url, url)
        if url == self.url:
            return
        if url in (f.url for f in self.inner):
            return
        parser = RaiParser(url, self.folderPath)
        self.inner.extend(parser.process())

    def _json_to_feed(self, feed: Feed, rdata) -> None:
        feed.title = rdata["title"]
        feed.description = rdata["podcast_info"].get("description", "")
        feed.description = feed.description or rdata["title"]
        feed.url = self.url
        feed._data["image"] = {"url": urljoin(self.url, rdata["podcast_info"]["image"])}
        feed._data[f"{NSITUNES}author"] = "RaiPlaySound"
        feed._data["language"] = "it-it"
        feed._data[f"{NSITUNES}owner"] = {f"{NSITUNES}email": "timedum@gmail.com"}
        # Categories
        categories = set()  # to prevent duplicates
        for c in chain(
            rdata["podcast_info"]["genres"],
            rdata["podcast_info"]["subgenres"],
            rdata["podcast_info"]["dfp"].get("escaped_genres", []),
            rdata["podcast_info"]["dfp"].get("escaped_typology", []),
        ):
            categories.add(c["name"])
        try:
            for c in rdata["podcast_info"]["metadata"]["product_sources"]:
                categories.add(c["name"])
        except KeyError:
            pass
        feed._data[f"{NSITUNES}category"] = [{"@text": c} for c in categories]
        feed.update = _datetime_parser(rdata["block"]["update_date"])
        if not feed.update:
            feed.update = _datetime_parser(rdata["track_info"]["date"])
        for item in rdata["block"]["cards"]:
            if "/playlist/" in item.get("weblink", ""):
                self.extend(item["weblink"])
            if not item.get("audio", None):
                continue
            fitem = FeedItem()
            fitem.title = item["toptitle"]
            fitem.id = "timendum-raiplaysound-" + item["uniquename"]
            # Keep original ordering by tweaking update seconds
            # Fix time in case of bad ordering
            dupdate = _datetime_parser(item["create_date"] + " " + item["create_time"])
            fitem.update = dupdate
            fitem.url = urljoin(self.url, item["track_info"]["page_url"])
            fitem.content = item.get("description", item["title"])
            fitem._data = {
                "enclosure": {
                    "@type": "audio/mpeg",
                    "@url": urljoin(self.url, item["audio"]["url"]),
                },
                f"{NSITUNES}title": fitem.title,
                f"{NSITUNES}summary": fitem.content,
                f"{NSITUNES}duration": item["audio"]["duration"],
                "image": {"url": urljoin(self.url, item["image"])},
            }
            if item.get("downloadable_audio", None) and item["downloadable_audio"].get("url", None):
                fitem._data["enclosure"]["@url"] = urljoin(
                    self.url, item["downloadable_audio"]["url"]
                ).replace("http:", "https:")
            if item.get("season", None) and item.get("episode", None):
                fitem._data[f"{NSITUNES}season"] = item["season"]
                fitem._data[f"{NSITUNES}episode"] = item["episode"]
            feed.items.append(fitem)

    def process(self, types: list[str]=['GENERE'], date_ok=False) -> List[Feed]:
        wanted_types = functools.reduce(
                lambda x,y : x|y,
                (PageTypes[x] for x in types))

        result = get_session().get(self.url + ".json")
        try:
            result.raise_for_status()
        except requests.HTTPError as e:
            print(f"Error with {self.url}: {e}")
            return self.inner
        rdata = result.json()
        typology = rdata["podcast_info"].get("typology", "").lower()
        pagetype = PageTypes.from_string(typology)
        if not pagetype & PageTypes.SERIE:
            if not pagetype & wanted_types:
                print(f"Skipped page: {self.url} ({pagetype.name})")
                return []
        for tab in rdata["tab_menu"]:
            if tab["content_type"] == "playlist":
                self.extend(tab["weblink"])
        feed = Feed()
        self._json_to_feed(feed, rdata)
        if not feed.items and not self.inner:
            print(f"Empty: {self.url}")
        if feed.items:
            if not date_ok and all([item.update for item in feed.items]):
                # Try to fix the update timestamp
                dates = [i.update.date() for i in feed.items]
                increasing = all(map(lambda a, b: b >= a, dates[0:-1], dates[1:]))
                decreasing = all(map(lambda a, b: b <= a, dates[0:-1], dates[1:]))
                if increasing and not decreasing:
                    # Dates never decrease
                    last_update = dt.fromtimestamp(0)
                    for item in feed.items:
                        if item.update <= last_update:
                            item.update = last_update + timedelta(seconds=1)
                        last_update = item.update
                elif decreasing and not increasing:
                    # Dates never decrease
                    last_update = feed.items[0].update + timedelta(seconds=1)
                    for item in feed.items:
                        if item.update >= last_update:
                            item.update = last_update - timedelta(seconds=1)
                        last_update = item.update
            if all([i._data.get(f"{NSITUNES}episode") for i in feed.items]) and all(
                [i._data.get(f"{NSITUNES}season") for i in feed.items]
            ):
                try:
                    feed.items = sorted(
                        feed.items,
                        key=lambda e: int(e._data[f"{NSITUNES}episode"])
                        + int(e._data[f"{NSITUNES}season"]) * 10000,
                    )
                except ValueError:
                    # season or episode not an int
                    feed.items = sorted(
                        feed.items,
                        key=lambda e: str(e._data[f"{NSITUNES}season"]).zfill(5)
                        + str(e._data[f"{NSITUNES}episode"]).zfill(5),
                    )
            else:
                feed.sort_items()
            filename = pathjoin(self.folderPath, url_to_filename(self.url))
            atomic_write(filename, to_rss_string(feed), update_time=max(item.update for item in feed.items))
            print(f"Written {filename}")
        return [feed] + self.inner



def UmaskNamedTemporaryFile(*args, **kargs):
    fdesc = tempfile.NamedTemporaryFile(*args, **kargs)
    # we need to set umask to get its current value. As noted
    # by Florian Brucker (comment), this is a potential security
    # issue, as it affects all the threads. Considering that it is
    # less a problem to create a file with permissions 000 than 666,
    # we use 666 as the umask temporary value.
    umask = os.umask(0o666)
    os.umask(umask)
    os.chmod(fdesc.name, 0o666 & ~umask)
    return fdesc


def atomic_write(filename, content: str, update_time: Optional[dt] = None):
    tmp = UmaskNamedTemporaryFile(mode='w', encoding='utf8', delete=False, dir=os.path.dirname(filename), prefix='.tmp-single-', suffix='.xml')
    tmp.write(content)
    tmp.close()
    if update_time is not None:
        timestamp = int(update_time.strftime('%s'))
        os.utime(tmp.name, (timestamp, timestamp))
    os.replace(tmp.name, filename)

def add_arguments(parser):
    parser.add_argument(
        "-f", "--folder", help="Cartella in cui scrivere il RSS podcast.", default="."
    )
    parser.add_argument(
        "--tipi",
        help="Specifica i tipi di podcast da scaricare; separa da virgola",
        dest="types",
        type=lambda s: s.split(','),
        default=['SERIE', 'GENERE'],
    )
    parser.add_argument(
        "--dateok",
        help="Lascia inalterata la data di pubblicazione degli episodi.",
        action="store_true",
    )
    parser.add_argument(
        "--rate",
        metavar='R',
        type=lambda r: get_session(per_minute=float(r)),
        help='Ratelimit to R requests per minute',
        default=-1,
    )
    parser.add_argument(
        "--log-level",
        metavar='R',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Ratelimit to R requests per minute',
        default='WARNING',
    )



def main():

    parser = argparse.ArgumentParser(
        description="Genera un RSS da un programma di RaiPlaySound.",
        epilog="Info su https://github.com/timendum/raiplaysound/",
    )
    add_arguments(parser)
    parser.add_argument("--recursive", action='store_true', default=False, dest='recursive')
    parser.add_argument("urls",
            metavar="URL",
            nargs='+',
            help="URL di un podcast (o playlist) su raiplaysound.",
            )

    args = parser.parse_args()
    logging.basicConfig(level=args.log_level)

    for url in args.urls:
        parser = RaiParser(url, args.folder, recursive=args.recursive)
        parser.process(args.types, date_ok=args.dateok)


if __name__ == "__main__":
    main()

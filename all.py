from os import makedirs, path
from urllib.parse import urljoin
from pathlib import Path

from bs4 import BeautifulSoup

from single import RaiParser, get_session, add_arguments

GENERI_URL = "https://www.raiplaysound.it/generi"


class RaiPlaySound:
    def __init__(self, basedir: Path, types: list[str]):
        self._seen_url = set()
        self._base_path = basedir
        self.types = types
        makedirs(self._base_path, exist_ok=True)

    def parse_genere(self, url):
        result = get_session().get(url)
        result.raise_for_status()
        soup = BeautifulSoup(result.content, "html.parser")
        elements = soup.find_all("article")
        for element in elements:
            url = urljoin(url, element.find("a")["href"])
            if url in self._seen_url:
                continue
            parser = RaiParser(url, self._base_path)
            try:
                parser.process(self.types)
                self._seen_url.add(url)
            except Exception as e:
                print(f"Error with {url}: {e}")

    def parse_generi(self) -> None:
        result = get_session().get(GENERI_URL)
        result.raise_for_status()
        soup = BeautifulSoup(result.content, "html.parser")
        elements = soup.find_all("a", class_="block")
        generi = []
        for element in elements:
            url = urljoin(result.url, element["href"])
            generi.append(url)
        for genere in generi:
            self.parse_genere(genere)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Crawls RaiPlaySound for RSSs")
    add_arguments(parser)

    args = parser.parse_args()
    get_session(per_minute=args.rate)

    dumper = RaiPlaySound(basedir=args.folder, types=args.types)
    dumper.parse_generi()


if __name__ == "__main__":
    main()

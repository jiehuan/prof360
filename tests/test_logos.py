"""Company logos (D-067): files kept under config/logos/, published as data URIs; the lookup
used by scripts/fetch_logos.py (Wikipedia infobox, Wikidata/Commons, favicon) with a fake
client, so no network."""

import base64
import shutil
import struct
import zlib
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import CONFIG_DIR, TEST_UA
from uw_pipeline.cli import app
from uw_pipeline.clients.logos import COMMONS_API, ENWIKI_API
from uw_pipeline.config import CompanyConfig, LogoConfig, LogoOverride
from uw_pipeline.extract.logos import DEPRECATED, PREFERRED, _choose, find_logo, infobox_logo
from uw_pipeline.logos import check_logo, data_uri, image_size, image_type, load_logos

TODAY = date(2026, 10, 26)
ITEM = "http://www.wikidata.org/entity/"
FILE = "http://commons.wikimedia.org/wiki/Special:FilePath/"


def png(width: int, height: int) -> bytes:
    """A valid PNG of one colour."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))  # fmt: skip

    raw = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))  # fmt: skip


def jpeg(width: int, height: int) -> bytes:
    """Enough of a JPEG for its type and size: SOI, an APP0 segment, then SOF0."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = b"\xff\xc0" + struct.pack(">HBHH", 11, 8, height, width) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof


class FakeClient:
    """Answers like LogoClient from canned data; records what was asked."""

    def __init__(self, rows=(), wikitext=None, files=None, favicon=None) -> None:
        self.rows, self.text = list(rows), wikitext
        self.files = files or {}  # (api, file name) -> image bytes
        self.icon = favicon
        self.calls: list[tuple] = []

    def sparql(self, query: str):
        self.calls.append(("sparql", query))
        return self.rows

    def wikitext(self, title: str):
        self.calls.append(("wikitext", title))
        return self.text

    def thumbnail(self, api: str, name: str, width: int):
        self.calls.append(("thumbnail", api, name, width))
        return (f"thumb://{api}/{name}", f"page://{name}") if (api, name) in self.files else None

    def image(self, url: str):
        api, name = url.removeprefix("thumb://").rsplit("/", 1)
        return self.files[(api, name)]

    def favicon(self, host: str, size: int):
        self.calls.append(("favicon", host, size))
        return self.icon


MAR = CompanyConfig(ticker="MAR", cik=1048286, predecessor_ciks=[1000001])
LCFG = LogoConfig()


def row(qid: str, **values: str) -> dict[str, str]:
    return {"item": ITEM + qid, **values}


# -- image helpers ---------------------------------------------------------------------------


def test_image_types_and_sizes() -> None:
    assert (image_type(png(3, 2)), image_size(png(3, 2))) == ("image/png", (3, 2))
    assert (image_type(jpeg(250, 80)), image_size(jpeg(250, 80))) == ("image/jpeg", (250, 80))
    assert image_type(b"GIF89a" + struct.pack("<HH", 5, 4)) == "image/gif"
    assert image_size(b"GIF89a" + struct.pack("<HH", 5, 4)) == (5, 4)
    assert image_type(b'<svg xmlns="http://www.w3.org/2000/svg"/>') is None  # never stored
    uri = data_uri(png(2, 2), 10_000)
    assert uri and uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == png(2, 2)
    assert data_uri(png(50, 50), 10) is None  # over the size limit


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("{{Infobox company\n| name = Cigna\n| logo = Cigna logo.svg\n}}", "Cigna logo.svg"),
        ("| logo             = [[File:NVR Logo 2018.png|frameless]]\n", "NVR Logo 2018.png"),
        ("| logo = [[Image:Constellation_Energy.png|center|250px]]", "Constellation_Energy.png"),
        ("|logo=File:Darden logo.svg <!-- current -->", "Darden logo.svg"),
        ("| logo =\n| type = [[Public company]]", None),  # empty: not the next parameter
        ("| logo = {{Wikidata|property|P154}}", None),
        ("| logo_caption = A caption.svg", None),
        ("no infobox", None),
    ],
)
def test_infobox_logo(text: str, name: str | None) -> None:
    assert infobox_logo(text) == name


# -- choosing among Wikidata items and statements ---------------------------------------------


def test_choose_prefers_pinned_cik_current_and_preferred_logos() -> None:
    rows = [
        # the predecessor's item comes after the pinned CIK's, whatever the Q-number
        row("Q1", cik="0001000001", file=FILE + "Old%20co.svg", rank="normal",
            article="https://en.wikipedia.org/wiki/Old_Co"),
        row("Q9", cik="0001048286", file=FILE + "Ended.svg", rank="normal", end="2019-01-01"),
        row("Q9", cik="0001048286", file=FILE + "Deprecated.svg", rank=DEPRECATED),
        row("Q9", cik="0001048286", file=FILE + "Normal%20new.svg", rank="normal",
            start="2024-01-01"),
        row("Q9", cik="0001048286", file=FILE + "Normal%20old.svg", rank="normal",
            start="2010-01-01"),
        row("Q9", cik="0001048286", file=FILE + "Preferred.svg", rank=PREFERRED,
            article="https://en.wikipedia.org/wiki/Marriott_International",
            website="https://www.marriott.com"),
        row("Q9", cik="0001048286", icon=FILE + "Icon.png"),
    ]  # fmt: skip
    found = _choose(rows, ["0001048286", "0001000001"], TODAY)
    assert found.qid == "Q9"
    assert found.files == ["Preferred.svg", "Normal new.svg", "Normal old.svg", "Old co.svg",
                           "Icon.png"]  # fmt: skip
    assert (found.article, found.website) == ("Marriott International", "https://www.marriott.com")


# -- the lookup, in source order --------------------------------------------------------------


def test_infobox_logo_comes_first() -> None:
    rows = [row("Q9", cik="0001048286", file=FILE + "Commons.svg", rank="normal",
                article="https://en.wikipedia.org/wiki/Marriott_International")]  # fmt: skip
    client = FakeClient(
        rows,
        "| logo = Marriott logo.svg",
        {(ENWIKI_API, "Marriott logo.svg"): png(250, 80), (COMMONS_API, "Commons.svg"): png(9, 9)},
    )
    logo = find_logo(MAR, client, LCFG, TODAY)  # type: ignore[arg-type]
    assert (logo.image, logo.source, logo.wikidata_id) == (png(250, 80), "wikipedia", "Q9")
    assert ("thumbnail", ENWIKI_API, "Marriott logo.svg", 250) in client.calls
    assert '"0001048286" "0001000001"' in client.calls[0][1]  # matched by CIK, never by ticker


def test_commons_then_favicon_then_none() -> None:
    rows = [row("Q9", cik="0001048286", file=FILE + "Missing.svg", rank="normal",
                website="https://www.marriott.com/")]  # fmt: skip
    # The infobox has no logo and Commons lacks the file: the website's icon.
    client = FakeClient(rows, "| logo =\n", {}, favicon=png(64, 64))
    logo = find_logo(MAR, client, LCFG, TODAY)  # type: ignore[arg-type]
    assert (logo.source, logo.source_url) == ("favicon", "https://www.marriott.com/")
    assert ("favicon", "www.marriott.com", 128) in client.calls
    # A 16 px icon is not used.
    tiny = find_logo(MAR, FakeClient(rows, None, {}, png(16, 16)), LCFG, TODAY)  # type: ignore[arg-type]
    assert (tiny.image, tiny.source, tiny.wikidata_id) == (None, "none", "Q9")
    # Commons file found.
    ok = FakeClient(rows, None, {(COMMONS_API, "Missing.svg"): jpeg(250, 70)})
    assert find_logo(MAR, ok, LCFG, TODAY).source == "wikidata"  # type: ignore[arg-type]


def test_overrides() -> None:
    pin = LogoOverride(wikidata="Q77", logo_file="Black.svg", website="https://marriott.example")
    lcfg = LogoConfig(overrides={"MAR": pin})
    rows = [row("Q77", file=FILE + "White.svg", rank="normal",
                article="https://en.wikipedia.org/wiki/Marriott")]  # fmt: skip
    client = FakeClient(
        rows,
        "| logo = Infobox.svg",
        {(ENWIKI_API, "Infobox.svg"): png(5, 5), (COMMONS_API, "Black.svg"): png(250, 90)},
    )
    logo = find_logo(MAR, client, lcfg, TODAY)  # type: ignore[arg-type]
    # A pinned file wins over the infobox; the item is the named one, not the CIK's.
    assert (logo.image, logo.source) == (png(250, 90), "wikidata")
    assert "wd:Q77" in client.calls[0][1] and "P5531" not in client.calls[0][1]
    with pytest.raises(ValueError):
        LogoOverride(wikidata="77")


# -- logo files and publishing ---------------------------------------------------------------


def _config_with(tmp_path: Path, files: dict[str, bytes]) -> Path:
    config = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config, ignore=shutil.ignore_patterns("logos"))
    (config / "logos").mkdir()
    for name, data in files.items():
        (config / "logos" / name).write_bytes(data)
    return config


def test_logo_files_are_loaded_and_checked(tmp_path: Path, monkeypatch) -> None:
    config = _config_with(tmp_path, {"MAR.png": png(4, 4), "JPM.jpg": jpeg(10, 10),
                                     "SOURCES.yaml": b"MAR: {source: manual}\n"})  # fmt: skip
    logos = load_logos(config)
    assert sorted(logos) == ["JPM", "MAR"]
    assert logos["MAR"] == data_uri(png(4, 4), 10_000)
    monkeypatch.setenv("CONFIG_DIR", str(config))
    monkeypatch.setenv("SEC_USER_AGENT", TEST_UA)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'uw.db'}")
    assert CliRunner().invoke(app, ["config", "check"]).exit_code == 0
    (config / "logos" / "NOPE.png").write_bytes(png(4, 4))  # no such company
    (config / "logos" / "JPM.jpg").write_bytes(png(4, 4))  # PNG content, .jpg name
    out = CliRunner().invoke(app, ["config", "check"])
    assert out.exit_code == 1
    assert "NOPE.png: no company with that ticker" in out.output
    assert "JPM.jpg: content is image/png" in out.output
    assert check_logo(config / "logos" / "MAR.png") is None
    assert "JPM" not in load_logos(config)  # not published either


def test_every_repo_logo_is_publishable() -> None:
    for path in sorted((CONFIG_DIR / "logos").glob("*.*")):
        if path.name != "SOURCES.yaml":
            assert check_logo(path) is None, path.name

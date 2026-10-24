"""Explicit credentials for the verified eGazette public, read-only search API.

This is deliberately not a general request-header extension. New providers need
their own reviewed origin, path and method policy before credentials can travel.
"""

import re
from dataclasses import dataclass, field

EGAZETTE_ORIGIN = "https://www.egazette.gov.sg/egazette-browse/"
EGAZETTE_APPLICATION = "1V7DZGZJKK"
EGAZETTE_INDEX = "prod_ogp_egazettes_index"
EGAZETTE_SEARCH_HOST = "1v7dzgzjkk-dsn.algolia.net"
EGAZETTE_SEARCH_URL = f"https://{EGAZETTE_SEARCH_HOST}/1/indexes/{EGAZETTE_INDEX}/query"


@dataclass(frozen=True)
class EGazetteSearchRequest:
    """A search-only key read from the official public frontend, never logged."""

    search_key: str = field(repr=False)
    source_sha256: str
    source_url: str = EGAZETTE_ORIGIN
    application: str = EGAZETTE_APPLICATION
    index: str = EGAZETTE_INDEX

    def headers_for(self, url: str, *, method: str) -> dict[str, str]:
        if (
            self.source_url != EGAZETTE_ORIGIN
            or self.application != EGAZETTE_APPLICATION
            or self.index != EGAZETTE_INDEX
            or not isinstance(self.source_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.source_sha256) is None
            or not isinstance(self.search_key, str)
            or re.fullmatch(r"[a-fA-F0-9]{32}", self.search_key) is None
            or url != EGAZETTE_SEARCH_URL
            or method != "POST"
        ):
            raise ValueError("公开检索请求不属于已核验的 eGazette 来源和只读端点。")
        return {
            "X-Algolia-Application-Id": self.application,
            "X-Algolia-API-Key": self.search_key,
            "Accept": "application/json",
        }

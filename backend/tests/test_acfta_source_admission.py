"""ACFTA source identity remains separate from transport safety and legal review."""

import hashlib
import socket
import sqlite3
import time
from contextlib import closing
from urllib.parse import quote, urlsplit

import pytest

from app import ingestion
from app.service import Service
from app.system_library import (
    SYSTEM_OWNER_ID,
    ensure_principal,
    official_source_allowed,
    register_source_guard,
)
from test_stream_download import Response, transport as transport


# Independent transcription of the 54 audited country/URL identities. SHA-256,
# original classification, receipt and page evidence are recorded in the doc.
_REVIEWED_ORIGINALS = [
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Goods-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Appendix-1-Revised-Operational-Certification-Procedures",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-12-Micro-Small-And-Medium-Enterprises",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Framework-Agreement-on-Comprehensive-Econ-Coop/"
        "Framework-Agreement",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ACFTA/ACFTA_SC_1_Indonesia.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Goods-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Annex-1-Modality-for-tariff-reduction-and-elimination",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Dispute-Settlement-Mechanism-of-"
        "Framework-Agreement-on-Comprehensive-Econ-Coop/Agreement-on-Dispute-Settlement-Mechanism",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/upgrade-protocol/Attachment-A-Revised-OCP.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Framework-Agreement-on-Comprehensive-Econ-Coop/"
        "Appendix-2",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Goods-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Agreement-on-Trade-in-Goods",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Goods-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Protocol-to-Amend-the-Trade-in-Goods-Agreement",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-6-Trade-in-Services",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Goods-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Second-Protocol-to-Amend-the-Agreement-on-Trade-In-Goods",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Framework-Agreement-on-Comprehensive-Econ-Coop/"
        "Annex-3",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-14-Dispute-Settlement",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Dispute-Settlement-Mechanism-of-"
        "Framework-Agreement-on-Comprehensive-Econ-Coop/Annex-1-Agreement-on-DSM",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Framework-Agreement-on-Comprehensive-Econ-Coop/"
        "Appendix-3",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ACFTA/ACFTA_SC_2_Headnote_to_the_Annex.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Goods-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Appendix-2a-Overleaf-Notes",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-3-0-Upgrade-Protocol",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ACFTA/ACFTA_SC_2_Indonesia.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/upgrade-protocol/Attachment-B-PSR.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Framework-Agreement-on-Comprehensive-Econ-Coop/"
        "Annex-2",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/upgrade-protocol/Protocol-to-Amend-the-Framework-Agreement-"
        "ACFTA.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-5-Sanitary-And-Phytosanitary-Measures",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Goods-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Annex-2-Modality-for-tariff-reduction-elimination",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Framework-Agreement-on-Comprehensive-Econ-Coop/"
        "Appendix-1",
    ),
    (
        "SG",
        "https://dev.agreement.asean.org/wp-content/uploads/2025/12/Singapores-Notification-for-the-"
        "ACFTA-3.0-Upgrade-Protocol.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-1-Trade-in-Goods",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-2-Rules-of-Origin",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-10-Green-Economy",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Goods-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Annex-3-Rules-of-Origin",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Framework-Agreement-on-Comprehensive-Econ-Coop/"
        "2nd-Protocol-to-Amend-the-Framework-Agreement",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Protocol-incorporating-ASEAN-China-Technical-"
        "Barriers-to-Trade-Sanitary-Phytosanitary-Measures-Chapter/Protocol",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Framework-Agreement-on-Comprehensive-Econ-Coop/"
        "Annex-4",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-13-Economic-and-Technical-Cooperation",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Services-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Indonesia",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/upgrade-protocol/Attachment-C-Revised-Form-E.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Framework-Agreement-on-Comprehensive-Econ-Coop/"
        "Protocol-to-Amend-the-Framework-Agreement",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-4-STRACAP",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-11-Supply-Chain-Connectivity",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-7-Investment",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/upgrade-protocol/ANNEX-1-Rules-of-Origin.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Investment-of-Framework-Agreement-on-"
        "Comprehensive-Econ-Coop-between-ASEAN-and-China/Agreement-on-Investment",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ACFTA/ACFTA_Third_Protocol_to_Amend_the_ASEAN_China_Framework_Agreement_on_"
        "Comprehensive_Economic_Co_Operation_2012.pdf",
    ),
    (
        "SG",
        "https://dev.agreement.asean.org/wp-content/uploads/2025/12/ACFTA-3.0-Upgrade-Protocol.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Framework-Agreement-on-Comprehensive-Econ-Coop/"
        "Annex-1",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Goods-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Appendix-2-Form-E",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-8-Competition-and-Consumer-Protection",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-9-Digital-Economy",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/Files/Non-Financial-Assistance/For-Companies/Free-"
        "Trade-Agreements/ACFTA/ACFTA-3-Customs-Procedures-and-Trade-Facilitation",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ACFTA/ACFTA_Protocol_to_implement_the_Second_Package_of_Specific_Commitments_"
        "under_the_Agreement_on_Trade_in_Services_between_ASEAN_China_2011.pdf",
    ),
    (
        "ID",
        "https://dev.agreement.asean.org/wp-content/uploads/2025/12/ACFTA-3.0-Upgrade-Protocol.pdf",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/Legal-Text/Agreement-on-Trade-in-Services-of-the-Framework-"
        "Agreement-on-Comprehensive-Econ-Coop/Agreement-on-Trade-in-Services",
    ),
    (
        "ID",
        "https://www.enterprisesg.gov.sg/-/media/esg/files/non-financial-assistance/for-companies/free-"
        "trade-agreements/ASEAN-China-FTA/upgrade-protocol/3rd-Package-ACTIS-Indonesia.pdf",
    ),
]


_ENTERPRISE_ORIGINAL = _REVIEWED_ORIGINALS[0][1]
_ASEAN_PROTOCOL = "https://dev.agreement.asean.org/wp-content/uploads/2025/12/ACFTA-3.0-Upgrade-Protocol.pdf"
_ASEAN_NOTIFICATION = (
    "https://dev.agreement.asean.org/wp-content/uploads/2025/12/"
    "Singapores-Notification-for-the-ACFTA-3.0-Upgrade-Protocol.pdf"
)


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("source-admission tests must not access the network")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    for key in ("COLLECTION_PROXY_URL", "COLLECTION_PROXY_HOSTS", "COLLECTION_DOH_HOSTS"):
        monkeypatch.setenv(key, "")


@pytest.mark.parametrize("country,url", _REVIEWED_ORIGINALS)
def test_reviewed_country_original_passes_both_python_and_sql_admission(country, url):
    assert official_source_allowed(url, country)
    with closing(sqlite3.connect(":memory:")) as conn:
        register_source_guard(conn)
        assert conn.execute("SELECT system_source_allowed(?,?)", (url, country)).fetchone()[0] == 1


@pytest.mark.parametrize("country,url", _REVIEWED_ORIGINALS)
def test_reviewed_original_does_not_authorize_query_fragment_or_neighbor(country, url):
    parsed = urlsplit(url)
    prefix, name = url.rsplit("/", 1)
    variants = [
        url + "?",
        url + "?download=1",
        url + "#",
        url + "#page=1",
        url + "/",
        url + "-amended",
        prefix + "/neighbor.pdf",
        prefix + "/../" + prefix.rsplit("/", 1)[1] + "/" + name,
        url.replace(parsed.path, parsed.path.swapcase(), 1),
        prefix + "/" + "%" + format(ord(name[0]), "02X") + name[1:],
        url.replace(parsed.netloc, parsed.netloc + ":443", 1),
    ]
    for changed in variants:
        assert changed != url
        assert not official_source_allowed(changed, country), changed


@pytest.mark.parametrize(
    "country,url",
    [("ID", _ENTERPRISE_ORIGINAL), ("ID", _ASEAN_PROTOCOL), ("SG", _ASEAN_NOTIFICATION)],
)
def test_reviewed_identity_still_rejects_unsafe_authority_and_redirect_entries(country, url):
    parsed = urlsplit(url)
    origin = "https://" + parsed.netloc
    variants = [
        url.replace("https://", "http://", 1),
        url.replace(origin, "https://user:password@" + parsed.netloc, 1),
        url.replace(origin, "https://@" + parsed.netloc, 1),
        url.replace(origin, "https://" + parsed.netloc + "@127.0.0.1", 1),
        url.replace(origin, "https://" + parsed.netloc + ".evil.example", 1),
        url.replace(origin, "https://mirror." + parsed.netloc, 1),
        url.replace(origin, "https://" + parsed.netloc + ".", 1),
        url.replace(origin, "https://" + parsed.netloc + ":8443", 1),
        url.replace(origin, "https://127.0.0.1", 1),
        url.replace(origin, "https://[::1]", 1),
        origin + "/redirect?url=" + quote(url, safe=""),
        "https://example.org/redirect?url=" + quote(url, safe=""),
        url + "\r\nHeader:value",
        "\x00" + url,
        url + "\\neighbor",
        url + " ",
    ]
    for changed in variants:
        assert not official_source_allowed(changed, country), changed


@pytest.mark.parametrize("country", ["ID", "SG"])
def test_asean_subdomain_is_not_admitted_as_a_host_or_upload_directory(country):
    assert official_source_allowed(_ASEAN_PROTOCOL, country)
    for url in (
        "https://dev.agreement.asean.org/",
        "https://dev.agreement.asean.org/wp-content/uploads/2025/12/",
        "https://dev.agreement.asean.org/wp-content/uploads/2025/12/ACFTA-other.pdf",
        _ASEAN_PROTOCOL.replace("/2025/12/", "/2025/11/"),
        _ASEAN_PROTOCOL.replace("dev.agreement.asean.org", "agreement.asean.org"),
    ):
        assert not official_source_allowed(url, country)
    assert official_source_allowed(_ASEAN_NOTIFICATION, country) is (country == "SG")


@pytest.mark.parametrize("country", ["CN", "MY", "id", "sg", "", None])
def test_shared_treaty_original_does_not_create_other_country_scopes(country):
    assert not official_source_allowed(_ENTERPRISE_ORIGINAL, country)
    assert not official_source_allowed(_ASEAN_PROTOCOL, country)


def test_existing_singapore_government_host_policy_is_unchanged():
    urls = [
        url for country, url in _REVIEWED_ORIGINALS if urlsplit(url).hostname == "www.enterprisesg.gov.sg"
    ]
    urls += [
        "https://www.enterprisesg.gov.sg/unlisted-original.pdf?revision=2#page=1",
        "https://sso.agc.gov.sg/Act/EXAMPLE",
    ]
    assert all(official_source_allowed(url, "SG") for url in urls)
    assert not official_source_allowed(urls[-2], "ID")
    assert not official_source_allowed(urls[-1], "ID")


@pytest.mark.parametrize("url", [_ENTERPRISE_ORIGINAL, _ASEAN_PROTOCOL])
def test_admitted_original_still_uses_public_dns_validation(monkeypatch, url):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ],
    )
    monkeypatch.setattr(
        ingestion, "_PinnedHTTPSConnection", lambda *args: pytest.fail("unsafe DNS opened a connection")
    )
    assert official_source_allowed(url, "ID")
    with pytest.raises(ingestion.DownloadError, match="public"):
        ingestion._download(url, deadline=time.monotonic() + 10)


@pytest.mark.parametrize(
    "location",
    [
        "https://127.0.0.1/internal",
        "http://dev.agreement.asean.org/document.pdf",
        "https://user:pass@example.org/x",
    ],
)
def test_admitted_original_does_not_bypass_redirect_transport_guard(transport, location):
    responses, requests, connections = transport
    responses.append(Response(status=302, headers={"Location": location}))
    assert official_source_allowed(_ASEAN_PROTOCOL, "ID")
    with pytest.raises(ingestion.DownloadError):
        ingestion._download(_ASEAN_PROTOCOL, deadline=time.monotonic() + 10)
    assert len(requests) == 1
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize(
    "country,url,level",
    [("ID", _ENTERPRISE_ORIGINAL, "primary"), ("SG", _ASEAN_NOTIFICATION, "secondary")],
)
def test_new_original_bytes_keep_version_history_and_unreviewed_legal_status(tmp_path, country, url, level):
    service = Service(tmp_path)
    documents = []
    for content in (b"Original treaty text for review.", b"Later original treaty text for review."):
        blob = service.archive.blobs.put_bytes(SYSTEM_OWNER_ID, content)
        with service.db.transaction() as conn:
            ensure_principal(conn)
            doc = dict(
                service.archive.store_original(
                    conn,
                    SYSTEM_OWNER_ID,
                    blob,
                    "treaty.txt",
                    "text/plain",
                    url,
                    collection="country:" + country,
                    level=level,
                    tags=["agreement_scheme:ACFTA"],
                )
            )
            classification = conn.execute(
                "SELECT level FROM legal_classifications WHERE document_id=?", (doc["id"],)
            ).fetchone()[0]
            assert classification == level
        metadata = service.catalog.enrich_document(doc)["metadata"]
        assert metadata["review_status"] == "draft"
        assert metadata["effective_from"] is None and metadata["effective_to"] is None
        assert doc["content_hash"] == hashlib.sha256(content).hexdigest()
        assert doc["source_url"] == url and doc["role"] == "primary"
        documents.append(doc)
    assert [doc["version"] for doc in documents] == [1, 2]
    assert documents[0]["family_id"] == documents[1]["family_id"]
    assert documents[0]["content_hash"] != documents[1]["content_hash"]

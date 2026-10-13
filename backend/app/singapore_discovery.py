"""Source paths verified against the Singapore tea checklist and official sites."""

import re
from urllib.parse import urlsplit


def scope_recipe(item, recipe):
    """Bound official navigation to the item's topics, including agency file hosts."""
    code = item["id"]
    roots = {
        "3.1": [
            "doing-business/compliance",
            "doing-business/importing-goods",
            "businesses/importing-goods",
            "businesses/valuation-duties-taxes-fees",
            "know-customs/acts-legislation",
        ],
        "3.2": [
            "food-import-export",
            "food-retail",
            "legislation",
            "regulatory-standards-frameworks-guidelines/food-labelling-packaging-guidelines",
            "regulatory-standards-frameworks-guidelines/food-safety-regulatory-limits",
        ],
        "3.3": [
            "taxes/goods-services-tax-(gst)",
            "taxes/corporate-income-tax/basics-of-corporate-income-tax",
            "quick-links/e-tax-guides",
        ],
        "3.4": [
            "regulations/practice-directions-registrars-interpretations-guidance",
            "regulations/legislation/companies-act",
            "regulations/compliance-requirements",
        ],
        "3.5": [
            "consumer-protection/legislation-and-guidelines",
            "consumer-protection/fair-trading-practices",
            "competition/legislation-and-guidelines",
            "resources/faqs/competition/ccs-guidelines",
        ],
        "3.6": ["guidelines-and-consultation", "overview-of-pdpa", "do-not-call-register"],
        "3.7": ["about-ip/trade-marks", "about-ip/geographical-indications", "manage-ip/resolve-ip-disputes"],
        "4.1": ["civil", "small-claims", "practice-directions", "registrars-circulars"],
        "4.2": ["siac-rules-2025", "siac-rules", "model-clauses", "fees"],
        "5.1": ["grow-your-business/go-global/international-agreements/free-trade-agreements"],
        "5.2": ["english/docs_e/legal_e"],
        "5.3": [
            "taxes/international-tax/international-tax-agreements-concluded-by-singapore/list-of-dtas-limited-dtas-and-eoi-arrangements"
        ],
    }
    for branch in roots.get(code, []):
        recipe["catalog_patterns"].append(rf"^/{re.escape(branch)}/?(?:\?|$)")
        recipe["detail_patterns"].append(rf"^/{re.escape(branch)}/[^?]+(?:\?|$)")
    for seed in item["entries"]:
        if seed["purpose"] != "reference":
            recipe["catalog_patterns"].append(rf"^{re.escape(urlsplit(seed['url']).path)}/?(?:\?|$)")
    recipe["document_patterns"] = [r"^/(?:media/)?docs/default-source/[^?]+\.pdf(?:\?|$)"]
    if code == "3.2":
        recipe["exclude_patterns"] += [
            r"(?:aquaculture|animal-feed|slaughter|veterinary|shell-eggs|cockle|pearl-oyster|drinking-water|digital-services|giro-form|application-for|inspection.*booking|inspection.*e-services)"
        ]
    if code in {"3.4", "3.5", "3.7"}:
        bucket = {"3.4": "277", "3.5": "45", "3.7": "61"}[code]
        recipe["document_patterns_by_host"] = {
            "isomer-user-content.by.gov.sg": [rf"^/{bucket}/[a-f0-9-]{{36}}/[^?]+\.pdf(?:\?|$)"],
            "file.go.gov.sg": [r"^/[^/?]+\.pdf(?:\?|$)"],
        }
    if code == "3.4":
        recipe["exclude_patterns"] += [r"(?:accounting-standards|audit-practice|ipo|listed-foreign|bizfile)"]
    if code == "3.5":
        recipe["exclude_patterns"] += [
            r"(?:merger|market-stud|beauty-industry|renovation-industry|infographic|brochure|gate.fold)"
        ]
    if code == "3.7":
        recipe["exclude_patterns"] += [
            r"(?:patent|integrated-circuit|sample-letter|statutory-declaration|digital-hub-practice|pre2021-circular|tm-report\.pdf)"
        ]
    if code == "3.6":
        recipe["document_patterns"] = [
            r"^/-/media/files/pdpc/pdf-files/(?:advisory-guidelines|other-guides)/[^?]+\.(?:pdf|ashx)(?:\?|$)"
        ]
    if code == "5.1":
        recipe["detail_patterns"] += [
            r"^/(?:[^?]*/)?(?:china-singapore|asean-china|regional-comprehensive-economic-partnership|rcep)[^?]*(?:\?|$)"
        ]
        recipe["document_patterns"] = [
            r"^/-/media/esg/files/[^?]*(?:csfta|china|rcep|acfta)[^?]*\.pdf(?:\?|$)"
        ]
        recipe["exclude_patterns"] += [
            r"(?:digital-economy|double-tax|investment-agreements|mutual-recognition|how-to-use)"
        ]
    if code == "5.3":
        recipe["document_patterns"] = [
            r"^/(?:media/)?docs/default-source/[^?]*(?:singaporechina|china-singapore|china_dta|china-dta|chinadta)[^?]*\.pdf(?:\?|$)"
        ]
    return recipe

"""Order 1s 3d/3e: which country a signature is in - for its phone numbers and its address.

Layer: L7

Pure tables and two small functions. A number typed without a `+` means nothing
until its country is known, and the owner's own region (Settings > Accounts) is
only the last guess: a Riyadh office's "011 555 0123" read as a London number is
a wrong number offered. The order of evidence is 3d's: a `+` prefix (the phone
library reads that itself), a country named in the block, the sender's domain's
country code, then the owner's region.

The tables are the countries the owner's mail comes from (the order names the Gulf
states, India and the Benelux, France, Germany and the UK) and their neighbours,
not the world: an unlisted country simply falls through to the next piece of
evidence, which is the cautious failure. `.co`, `.io`, `.me`, `.ai`, `.tv` and the
like are left out on purpose - they are sold as vanity names and say nothing about
where anybody is.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

__all__ = ["COUNTRIES", "CITIES", "CCTLD", "country_in", "city_in", "region_for", "domain_country"]

#: Country as written in a signature -> ISO 3166 code (the phone library's region).
COUNTRIES = {
    "united arab emirates": "AE", "uae": "AE", "u.a.e": "AE", "saudi arabia": "SA", "kingdom of saudi arabia": "SA",
    "ksa": "SA", "kuwait": "KW", "qatar": "QA", "oman": "OM", "sultanate of oman": "OM", "bahrain": "BH",
    "kingdom of bahrain": "BH", "india": "IN", "pakistan": "PK", "egypt": "EG", "jordan": "JO", "lebanon": "LB",
    "iraq": "IQ", "turkey": "TR", "netherlands": "NL", "the netherlands": "NL", "holland": "NL", "belgium": "BE",
    "france": "FR", "germany": "DE", "deutschland": "DE", "united kingdom": "GB", "uk": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "northern ireland": "GB", "great britain": "GB", "ireland": "IE",
    "spain": "ES", "italy": "IT", "switzerland": "CH", "austria": "AT", "luxembourg": "LU", "denmark": "DK",
    "norway": "NO", "sweden": "SE", "finland": "FI", "poland": "PL", "portugal": "PT", "greece": "GR",
    "singapore": "SG", "malaysia": "MY", "australia": "AU", "new zealand": "NZ", "south africa": "ZA",
    "nigeria": "NG", "kenya": "KE", "canada": "CA", "united states": "US", "usa": "US", "u.s.a": "US",
    "china": "CN", "hong kong": "HK", "japan": "JP", "south korea": "KR",
    # Order 1s 9b round 2: the countries firms list in a signature's footer ("Belgium | France | Ireland")
    # have to be known as countries, or the list is read as part of an address.
    "thailand": "TH", "vietnam": "VN", "indonesia": "ID", "philippines": "PH", "brazil": "BR", "mexico": "MX",
    "argentina": "AR", "chile": "CL", "colombia": "CO", "peru": "PE", "russia": "RU", "ukraine": "UA",
    "romania": "RO", "czech republic": "CZ", "czechia": "CZ", "hungary": "HU", "bulgaria": "BG",
    "croatia": "HR", "serbia": "RS", "israel": "IL", "morocco": "MA", "algeria": "DZ", "tunisia": "TN",
    "libya": "LY", "ghana": "GH", "tanzania": "TZ", "angola": "AO", "sri lanka": "LK", "bangladesh": "BD",
    "nepal": "NP", "kazakhstan": "KZ", "azerbaijan": "AZ", "iran": "IR", "taiwan": "TW", "slovakia": "SK",
    "slovenia": "SI", "estonia": "EE", "latvia": "LV", "lithuania": "LT", "iceland": "IS", "cyprus": "CY",
    "malta": "MT",
}

#: A country's name for an address, by ISO code (the first name above that is a real name, not an initialism).
COUNTRY_NAME = {"AE": "United Arab Emirates", "SA": "Saudi Arabia", "KW": "Kuwait", "QA": "Qatar", "OM": "Oman",
                "BH": "Bahrain", "IN": "India", "PK": "Pakistan", "EG": "Egypt", "JO": "Jordan", "LB": "Lebanon",
                "IQ": "Iraq", "TR": "Turkey", "NL": "Netherlands", "BE": "Belgium", "FR": "France", "DE": "Germany",
                "GB": "United Kingdom", "IE": "Ireland", "ES": "Spain", "IT": "Italy", "CH": "Switzerland",
                "AT": "Austria", "LU": "Luxembourg", "DK": "Denmark", "NO": "Norway", "SE": "Sweden", "FI": "Finland",
                "PL": "Poland", "PT": "Portugal", "GR": "Greece", "SG": "Singapore", "MY": "Malaysia",
                "AU": "Australia", "NZ": "New Zealand", "ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya",
                "CA": "Canada", "US": "United States", "CN": "China", "HK": "Hong Kong", "JP": "Japan",
                "KR": "South Korea", "TH": "Thailand", "VN": "Vietnam", "ID": "Indonesia", "PH": "Philippines",
                "BR": "Brazil", "MX": "Mexico", "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "PE": "Peru",
                "RU": "Russia", "UA": "Ukraine", "RO": "Romania", "CZ": "Czech Republic", "HU": "Hungary",
                "BG": "Bulgaria", "HR": "Croatia", "RS": "Serbia", "IL": "Israel", "MA": "Morocco", "DZ": "Algeria",
                "TN": "Tunisia", "LY": "Libya", "GH": "Ghana", "TZ": "Tanzania", "AO": "Angola", "LK": "Sri Lanka",
                "BD": "Bangladesh", "NP": "Nepal", "KZ": "Kazakhstan", "AZ": "Azerbaijan", "IR": "Iran",
                "TW": "Taiwan", "SK": "Slovakia", "SI": "Slovenia", "EE": "Estonia", "LV": "Latvia",
                "LT": "Lithuania", "IS": "Iceland", "CY": "Cyprus", "MT": "Malta"}

#: Cities an address line may end in when it has no postcode (3e). Lower-case, as compared.
CITIES = frozenset(c.strip() for line in """
dubai|abu dhabi|sharjah|ajman|fujairah|ras al khaimah|al ain|jebel ali|riyadh|jeddah|dammam|khobar|al khobar|
dhahran|jubail|yanbu|mecca|makkah|medina|madinah|kuwait|kuwait city|ahmadi|doha|lusail|ras laffan|muscat|sohar|
salalah|duqm|manama|muharraq|sitra|mumbai|delhi|new delhi|bangalore|bengaluru|chennai|hyderabad|pune|kolkata|
ahmedabad|vadodara|gurgaon|gurugram|noida|karachi|lahore|islamabad|cairo|alexandria|amman|beirut|baghdad|basra|
erbil|istanbul|ankara|amsterdam|rotterdam|the hague|den haag|utrecht|eindhoven|brussels|antwerp|ghent|liege|
paris|lyon|marseille|toulouse|berlin|munich|frankfurt|hamburg|cologne|dusseldorf|stuttgart|london|manchester|
birmingham|leeds|glasgow|edinburgh|aberdeen|bristol|cambridge|oxford|dublin|cork|madrid|barcelona|milan|rome|
zurich|geneva|basel|vienna|luxembourg|copenhagen|oslo|stavanger|stockholm|helsinki|warsaw|lisbon|athens|
singapore|kuala lumpur|sydney|melbourne|perth|brisbane|auckland|johannesburg|cape town|lagos|nairobi|toronto|
calgary|houston|new york|hong kong|shanghai|beijing|tokyo|seoul|bangkok|jakarta|manila|hanoi|ho chi minh city|
sao paulo|mexico city|moscow|bucharest|prague|budapest|tel aviv|casablanca|accra|colombo|dhaka|baku|tehran|taipei""".split("\n") for c in line.split("|") if c.strip())

#: A domain's last label -> ISO code. Only country codes that are used as countries (see the module docstring).
CCTLD = {"ae": "AE", "sa": "SA", "kw": "KW", "qa": "QA", "om": "OM", "bh": "BH", "in": "IN", "pk": "PK",
         "eg": "EG", "jo": "JO", "lb": "LB", "iq": "IQ", "tr": "TR", "nl": "NL", "be": "BE", "fr": "FR", "de": "DE",
         "uk": "GB", "ie": "IE", "es": "ES", "it": "IT", "ch": "CH", "at": "AT", "lu": "LU", "dk": "DK", "no": "NO",
         "se": "SE", "fi": "FI", "pl": "PL", "pt": "PT", "gr": "GR", "sg": "SG", "my": "MY", "au": "AU", "nz": "NZ",
         "za": "ZA", "ng": "NG", "ke": "KE", "ca": "CA", "cn": "CN", "hk": "HK", "jp": "JP", "kr": "KR"}

_WORDS = re.compile(r"[a-z][a-z.]*")


def _phrases(text: str) -> list[str]:
    """Every run of one to four words in `text`, lower-case, for looking names up whole."""
    words = [w.strip(".") or w for w in _WORDS.findall(text.lower())]
    out = []
    for n in (4, 3, 2, 1):
        out += [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
    return out


def country_in(text: str) -> Optional[str]:
    """The ISO code of a country named in `text` (whole words), or None."""
    for p in _phrases(text):
        if p in COUNTRIES:
            return COUNTRIES[p]
    return None


def city_in(text: str) -> Optional[str]:
    """A recognised city named in `text`, as written there, or None."""
    for p in _phrases(text):
        if p in CITIES:
            # The city as the line spells it ("Abu  Dhabi" too): its words with any run of spaces between.
            # Each word escaped on its own: `re.escape` escapes the space too, so replacing the space in the
            # escaped text left a stray backslash and the pattern never matched (order 1u §4d).
            m = re.search(r"\b" + "[ ]+".join(re.escape(w) for w in p.split(" ")) + r"\b", text, re.I)
            return m.group(0) if m else p.title()
    return None


def domain_country(email_or_domain: str) -> Optional[str]:
    tld = (email_or_domain or "").rsplit("@", 1)[-1].lower().rstrip(".").rsplit(".", 1)[-1]
    return CCTLD.get(tld)


def region_for(lines: Iterable[str], sender_email: str, default: str) -> str:
    """3d: the region for numbers typed without `+` - a country named in the block, the domain, the owner's."""
    for ln in lines:
        if "@" in ln or "://" in ln:
            continue
        found = country_in(ln)
        if found:
            return found
    return domain_country(sender_email) or default

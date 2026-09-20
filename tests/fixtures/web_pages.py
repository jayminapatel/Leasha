"""Canned web responses for `tests/unit/test_chat_web.py` - no network is ever touched.

Provenance, because the shapes matter more than the words:

* `WIKI_SEARCH` and `WIKI_EXTRACT` are trimmed copies of what `en.wikipedia.org/w/api.php`
  actually returned on 2026-09-20 for a harmless query ("PST file Outlook").
* `DDG_CHALLENGE` is what `html.duckduckgo.com/html/` actually returned (HTTP 202, an
  anti-bot page whose form posts to `duckduckgo.com/anomaly.js`), cut down to its markers.
* `DDG_RESULTS`, `SEARX_JSON` and `BRAVE_JSON` follow the providers' documented markup
  and JSON; they could not be captured live (DuckDuckGo challenged the probe, and there
  is no SearXNG instance or Brave key here).
"""

from __future__ import annotations

import json

WIKI_SEARCH = json.dumps({
    "batchcomplete": True,
    "continue": {"sroffset": 3, "continue": "-||"},
    "query": {"searchinfo": {"totalhits": 78}, "search": [
        {"ns": 0, "title": "Personal Storage Table", "pageid": 1255195,
         "snippet": "on the server. Microsoft <span class=\"searchmatch\">Outlook</span> stores these "
                    "items in a personal-storage-table (.<span class=\"searchmatch\">pst</span>) file"},
        {"ns": 0, "title": "Microsoft Outlook", "pageid": 176271,
         "snippet": "The Ultimate Guide to Convert <span class=\"searchmatch\">Outlook</span> "
                    "OST to <span class=\"searchmatch\">PST</span> &quot;File&quot; Trijatech"},
        {"ns": 0, "title": "Outlook Express", "pageid": 176281, "snippet": "File .PAB - Address Book"},
    ]},
})

WIKI_EMPTY = json.dumps({"batchcomplete": True, "query": {"searchinfo": {"totalhits": 0}, "search": []}})

WIKI_EXTRACT = json.dumps({"batchcomplete": True, "query": {"pages": [{
    "pageid": 1255195, "ns": 0, "title": "Personal Storage Table",
    "extract": "In computing, a Personal Storage Table (.pst) is an open proprietary file format "
               "used to store copies of messages, calendar events, and other items. "
               "The open format is controlled by Microsoft. "
               "The file format may also be known as a Personal Folders File. "
               "It was designed and written by a small team at Microsoft."}]}})

DDG_CHALLENGE = ("<!DOCTYPE html><html><head><title>DuckDuckGo</title></head><body>"
                 "<form id=\"img-form\" action=\"//duckduckgo.com/anomaly.js?sv=html&cc=sre\">"
                 "</form></body></html>")

DDG_RESULTS = """<!DOCTYPE html><html><body>
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a rel="nofollow" class="result__a"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpages.example.org%2Fpst-guide&amp;rut=abc">A PST <b>file</b> guide</a></h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpages.example.org%2Fpst-guide">
      What a <b>PST</b> file is &amp; what it holds.</a>
  </div>
</div>
<div class="result result--ad">
  <h2 class="result__title"><a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=x">Buy PST tools</a></h2>
  <a class="result__snippet" href="https://duckduckgo.com/y.js?ad_provider=x">An advert.</a>
</div>
<div class="result results_links web-result">
  <h2 class="result__title"><a class="result__a"
    href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwiki.example.net%2Fpst&amp;rut=def">PST on the wiki</a></h2>
  <a class="result__snippet" href="x">An encyclopedia entry.</a>
</div>
</body></html>"""

SEARX_JSON = json.dumps({"query": "pst file", "number_of_results": 2, "results": [
    {"url": "https://www.example.org/pst", "title": "PST <b>file</b> explained",
     "content": "A PST is Outlook's data file.", "engine": "duckduckgo", "score": 2.0},
    {"url": "https://docs.example.com/outlook", "title": "Outlook data files",
     "content": "Where Outlook keeps mail.", "engine": "brave"},
]})

BRAVE_JSON = json.dumps({"type": "search", "query": {"original": "pst file"}, "web": {
    "type": "search", "results": [
        {"title": "What is a <strong>PST</strong> file", "url": "https://www.example.org/what-is-pst",
         "description": "A <strong>PST</strong> file stores Outlook data.",
         "meta_url": {"hostname": "www.example.org"}},
        {"title": "Outlook", "url": "https://learn.example.com/outlook",
         "description": "Microsoft Outlook overview."},
    ]}})

ARTICLE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>PST guide - Example</title>
<style>body { color: red }</style>
<script>var secretTracker = "SCRIPT-ONLY-TEXT"; document.write("more");</script></head>
<body><nav>Home | About | NAVIGATION-TEXT</nav>
<h1>PST guide</h1>
<p>A PST file is where Outlook keeps its mail. It can grow very large.</p>
<script>alert("INLINE-SCRIPT-TEXT")</script>
<p>Old files are archived. Newer ones are not.</p>
<footer>Copyright FOOTER-TEXT</footer></body></html>"""

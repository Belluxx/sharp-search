"""Online test of fetch against bot detection, run inside the image (make bots).

Fetches pages from sites that use known bot-detection systems, then prints the ones that failed
and a score. A page passes if its text has a phrase from the page's own content, which a block page
doesn't have. Sites also judge the IP address, so the score differs between networks and runs.
"""

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import fetch

WORKERS = 5
MAX_REASON = 76

# (URL, phrase from the page's content), by the bot-detection system that answers a plain curl.
SITES = {
    "Cloudflare": [
        ("https://stackoverflow.com/questions/11227809/why-is-processing-a-sorted-array-faster-than-processing-an-unsorted-array", "branch prediction"),
        ("https://www.npmjs.com/package/react", "JavaScript library"),
        ("https://www.glassdoor.com/Overview/Working-at-Google-EI_IE9079.11,17.htm", "Google"),
        ("https://www.crunchbase.com/organization/anthropic", "Anthropic"),
        ("https://www.economist.com/the-world-in-brief", "world in brief"),
        ("https://www.scrapingcourse.com/cloudflare-challenge", "You bypassed the Cloudflare challenge"),
        ("https://www.indeed.com/jobs?q=python+developer", "Python Developer"),
        ("https://medium.com/tag/programming", "Programming"),
        ("https://genius.com/Queen-bohemian-rhapsody-lyrics", "Galileo"),
        ("https://www.coinbase.com/price/bitcoin", "About Bitcoin"),
        ("https://www.trovaprezzi.it/prezzo_ferramenta_scheda_caldaia_baxi.aspx", "Baxi"),
        ("https://www.idealista.it/", "appartamenti"),
        ("https://www.researchgate.net/topic/Machine-Learning", "Machine Learning"),
        ("https://www.quora.com/What-is-Python", "programming language"),
        ("https://www.mediaworld.it/", "Elettrodomestici"),
        ("https://www.manomano.it/", "fai da te"),
    ],
    "Akamai": [
        ("https://www.nike.com/w/mens-shoes-nik1zy7ok", "Sneakers"),
        ("https://www.adidas.com/us/men-shoes", "Shoes"),
        ("https://www.homedepot.com/b/Tools-Power-Tools/N-5yc1vZc298", "Power Tools"),
        ("https://www.ebay.com/sch/i.html?_nkw=lego", "LEGO"),
        ("https://www.delta.com/us/en/flight-deals/overview", "Flight Deals"),
        ("https://www.washingtonpost.com/technology/", "Technology"),
        ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=10-K", "Apple Inc"),
        ("https://www.idealo.it/", "Confronta"),
    ],
    "DataDome": [
        ("https://www.leboncoin.fr/recherche?text=velo", "Annonces"),
        ("https://www.nytimes.com/section/technology", "Technology"),
        ("https://www.thefork.com/restaurants/paris-c415144", "restaurant"),
        ("https://www.etsy.com/search?q=ceramic+mug", "mug"),
        ("https://www.tripadvisor.com/Tourism-g187791-Rome_Lazio-Vacations.html", "Rome"),
        ("https://www.yelp.com/search?find_desc=pizza&find_loc=New+York%2C+NY", "Pizza"),
        ("https://www.reuters.com/technology/", "Technology"),
        ("https://www.g2.com/products/slack/reviews", "Slack"),
        ("https://www.subito.it/annunci-italia/vendita/usato/?q=caldaia", "annunci"),
        ("https://www.leroymerlin.it/", "Giardino"),
        ("https://www.immobiliare.it/vendita-case/roma/", "Roma"),
    ],
    "PerimeterX": [
        ("https://www.zillow.com/seattle-wa/", "Homes For Sale"),
        ("https://www.walmart.com/search?q=lego", "LEGO"),
        ("https://www.fiverr.com/search/gigs?query=logo+design", "logo design"),
        ("https://www.bloomberg.com/quote/AAPL:US", "Apple"),
        ("https://seekingalpha.com/symbol/AAPL", "Apple"),
        ("https://www.skyscanner.net/flights-to/rome/cheap-flights-to-rome.html", "Flights to Rome"),
    ],
    "Kasada": [
        ("https://arcteryx.com/us/en/c/mens/shell-jackets", "Jacket"),
        ("https://www.realestate.com.au/buy/in-sydney,+nsw+2000/list-1", "Sydney"),
        ("https://www.chewy.com/b/dog-food-332", "Dog Food"),
    ],
    "AWS WAF": [
        ("https://www.imdb.com/title/tt0111161/", "Shawshank"),
        ("https://www.trustpilot.com/review/www.amazon.com", "TrustScore"),
        ("https://www.redfin.com/city/16163/WA/Seattle", "Seattle"),
    ],
    "Imperva": [
        ("https://www.pokemoncenter.com/category/plush", "Plush"),
        ("https://www.coles.com.au/browse/fruit-vegetables", "Fruit"),
    ],
    "Anubis": [
        ("https://lore.kernel.org/lkml/", "PATCH"),
        ("https://sourceware.org/bugzilla/describecomponents.cgi?product=glibc", "libc"),
        ("https://trac.ffmpeg.org/wiki/Encode/H.264", "libx264"),
    ],
    "Fastly": [
        ("https://www.jstor.org/subject/computerscience", "Computer Science"),
    ],
    "In-house": [
        ("https://www.reddit.com/r/programming/", "/r/programming/comments/"),
        ("https://www.linkedin.com/company/microsoft", "followers"),
        ("https://www.amazon.com/s?k=mechanical+keyboard", "results for"),
        ("https://www.amazon.it/s?k=scheda+caldaia+baxi", "Baxi"),
        ("https://www.google.com/search?q=wikipedia", "wikipedia.org"),
        ("https://www.bing.com/search?q=wikipedia", "wikipedia.org"),
        ("https://duckduckgo.com/html/?q=wikipedia", "wikipedia.org"),
        ("https://www.expedia.com/Rome-Hotels.d179899.Travel-Guide-Hotels", "Rome"),
    ],
}


def failure(url, phrase):
    """Why the fetch failed, or None if the page's text has the phrase."""
    try:
        text = fetch.fetch(url)["text"]
    except Exception as error:
        return (str(error) or repr(error)).removeprefix("could not load the page: ")
    if phrase.lower() in text.lower():
        return None
    return f'got "{" ".join(text.split())}"' if text.strip() else "empty page"


def main():
    total = sum(len(sites) for sites in SITES.values())
    widest = max(len(sites) for sites in SITES.values())
    failed = []
    print(f"Fetching {total} pages behind bot detection\n")
    with ThreadPoolExecutor(WORKERS) as pool:
        fetches = {system: [(url, pool.submit(failure, url, phrase)) for url, phrase in sites] for system, sites in SITES.items()}
        for system, sites in fetches.items():
            print(f"  {system:<12}", end="", flush=True)
            passed = 0
            for url, fetched in sites:
                reason = fetched.result()
                if reason is None:
                    passed += 1
                else:
                    failed.append((system, urlsplit(url).hostname.removeprefix("www."), reason))
                print("✓" if reason is None else "✗", end="", flush=True)
            print(f"{'':{widest - len(sites)}}  {passed}/{len(sites)}")
    if failed:
        print(f"\nFailed ({len(failed)})\n")
        for system, site, reason in failed:
            print(f"  {system:<12}{site:<20}{reason if len(reason) <= MAX_REASON else reason[:MAX_REASON - 2] + '…"'}")
    passed = total - len(failed)
    print(f"\nScore: {passed}/{total} ({passed * 100 // total}%)")


if __name__ == "__main__":
    main()

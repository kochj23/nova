# traffic_cams.py — Caltrans D7 public CCTV snapshots (Burbank / Glendale / Glassell Park / Pasadena)
# Public domain, no auth. Snapshot refreshes ~every 1 min. Append ?_=<ts> to bust cache.
# Generated from quickmap.dot.ca.gov/data/cctv.kml. role: "commute" | "fire" (brush-adjacent).

TRAFFIC_CAMERAS = {
    # --- Burbank ---
    "i536alamedast": {"name": "I-5 : (36) Alameda St", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i536alamedast/i536alamedast.jpg", "area": "Burbank", "lat": 34.177, "lon": -118.3022, "role": "commute"},
    "i537olive": {"name": "I-5 : (37) Olive", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i537olive/i537olive.jpg", "area": "Burbank", "lat": 34.1772, "lon": -118.308, "role": "commute"},
    "i540northofburbankblvd": {"name": "I-5 : (40) North of Burbank Blvd", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i540northofburbankblvd/i540northofburbankblvd.jpg", "area": "Burbank", "lat": 34.1859, "lon": -118.3202, "role": "commute"},
    "i541sanfernandoonramp": {"name": "I-5 : (41) San Fernando On-Ramp", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i541sanfernandoonramp/i541sanfernandoonramp.jpg", "area": "Burbank", "lat": 34.1942, "lon": -118.3302, "role": "commute"},
    "i542buenavista": {"name": "I-5 : (42) Buena Vista", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i542buenavista/i542buenavista.jpg", "area": "Burbank", "lat": 34.2006, "lon": -118.3396, "role": "commute"},
    "i567penrosestreet": {"name": "I-5 : (67) Penrose Street", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i567penrosestreet/i567penrosestreet.jpg", "area": "Burbank", "lat": 34.2261, "lon": -118.3751, "role": "commute"},
    "sr134652hollywoodway": {"name": "SR-134 : (652) Hollywood Way", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134652hollywoodway/sr134652hollywoodway.jpg", "area": "Burbank", "lat": 34.154, "lon": -118.3401, "role": "commute"},
    "sr134653buenavista": {"name": "SR-134 : (653) Buena Vista", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134653buenavista/sr134653buenavista.jpg", "area": "Burbank", "lat": 34.1531, "lon": -118.326, "role": "commute"},
    "sr134654westofforestlawn": {"name": "SR-134 : (654) West of Forest Lawn", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134654westofforestlawn/sr134654westofforestlawn.jpg", "area": "Burbank", "lat": 34.154, "lon": -118.3143, "role": "fire"},
    "sr170647southsr170toeastsr134": {"name": "SR-170 : (647) South SR-170 to East SR-134", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr170647southsr170toeastsr134/sr170647southsr170toeastsr134.jpg", "area": "Burbank", "lat": 34.156, "lon": -118.3791, "role": "commute"},
    "sr17096victoryblvd": {"name": "SR-170 : (96) Victory Blvd", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr17096victoryblvd/sr17096victoryblvd.jpg", "area": "Burbank", "lat": 34.1879, "lon": -118.4012, "role": "commute"},
    "us101619sr170tujunga": {"name": "US-101 : (619) SR-170 / Tujunga", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/us101619sr170tujunga/us101619sr170tujunga.jpg", "area": "Burbank", "lat": 34.1547, "lon": -118.3811, "role": "commute"},
    "us101621laurelcanyonblvd": {"name": "US-101 : (621) Laurel Canyon Blvd", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/us101621laurelcanyonblvd/us101621laurelcanyonblvd.jpg", "area": "Burbank", "lat": 34.1544, "lon": -118.398, "role": "fire"},
    "us101623coldwatercanyon": {"name": "US-101 : (623) Coldwater Canyon", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/us101623coldwatercanyon/us101623coldwatercanyon.jpg", "area": "Burbank", "lat": 34.157, "lon": -118.4141, "role": "fire"},

    # --- Glendale ---
    "i529losfelizblvd": {"name": "I-5 : (29) Los Feliz Blvd", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i529losfelizblvd/i529losfelizblvd.jpg", "area": "Glendale", "lat": 34.1291, "lon": -118.2741, "role": "commute"},
    "i531zoodrive": {"name": "I-5 : (31) Zoo Drive", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i531zoodrive/i531zoodrive.jpg", "area": "Glendale", "lat": 34.1511, "lon": -118.2808, "role": "commute"},
    "i533sbroute5atroute134": {"name": "I-5 : (33) SB Route 5 at Route 134", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i533sbroute5atroute134/i533sbroute5atroute134.jpg", "area": "Glendale", "lat": 34.1535, "lon": -118.2874, "role": "commute"},
    "i528glendaleblvd": {"name": "I-5: (28) Glendale Blvd", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i528glendaleblvd/i528glendaleblvd.jpg", "area": "Glendale", "lat": 34.1122, "lon": -118.2665, "role": "commute"},
    "i530socoloradoblvd": {"name": "I-5: (30) SO Colorado Blvd", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i530socoloradoblvd/i530socoloradoblvd.jpg", "area": "Glendale", "lat": 34.1394, "lon": -118.2772, "role": "commute"},
    "sr134656westofriversidedr": {"name": "SR-134 : (656) West of Riverside Dr", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134656westofriversidedr/sr134656westofriversidedr.jpg", "area": "Glendale", "lat": 34.1559, "lon": -118.2974, "role": "commute"},
    "sr134657eastofroute5": {"name": "SR-134 : (657) East of Route 5", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134657eastofroute5/sr134657eastofroute5.jpg", "area": "Glendale", "lat": 34.1542, "lon": -118.2851, "role": "commute"},
    "sr134659eosanfernandord": {"name": "SR-134 : (659) E/O San Fernando Rd", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134659eosanfernandord/sr134659eosanfernandord.jpg", "area": "Glendale", "lat": 34.1546, "lon": -118.2731, "role": "commute"},
    "sr134660eastofpacificave": {"name": "SR-134 : (660) East of Pacific Ave", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134660eastofpacificave/sr134660eastofpacificave.jpg", "area": "Glendale", "lat": 34.1566, "lon": -118.2633, "role": "commute"},
    "sr134662glendaleave": {"name": "SR-134 : (662) Glendale Ave", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134662glendaleave/sr134662glendaleave.jpg", "area": "Glendale", "lat": 34.1566, "lon": -118.2437, "role": "commute"},
    "sr134664eastofsr2": {"name": "SR-134 : (664) East of SR-2", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134664eastofsr2/sr134664eastofsr2.jpg", "area": "Glendale", "lat": 34.1465, "lon": -118.2248, "role": "commute"},
    "sr2584colorado": {"name": "SR-2 : (584) Colorado", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr2584colorado/sr2584colorado.jpg", "area": "Glendale", "lat": 34.1379, "lon": -118.2285, "role": "fire"},

    # --- Glassell Park ---
    "i527northofsr2": {"name": "I-5 : (27) North of SR-2", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i527northofsr2/i527northofsr2.jpg", "area": "Glassell Park", "lat": 34.1047, "lon": -118.2525, "role": "commute"},
    "sr2582fletcherdriveofframp": {"name": "SR-2 (582) : Fletcher Drive Off Ramp", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr2582fletcherdriveofframp/sr2582fletcherdriveofframp.jpg", "area": "Glassell Park", "lat": 34.1104, "lon": -118.2489, "role": "fire"},

    # --- Pasadena ---
    "i210448marengo": {"name": "I-210 : (448) Marengo", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i210448marengo/i210448marengo.jpg", "area": "Pasadena", "lat": 34.1514, "lon": -118.1463, "role": "fire"},
    "i210449lakeave": {"name": "I-210 : (449) Lake Ave", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i210449lakeave/i210449lakeave.jpg", "area": "Pasadena", "lat": 34.1515, "lon": -118.1339, "role": "fire"},
    "i210450allenaveonramp": {"name": "I-210 : (450) Allen Ave On-Ramp", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i210450allenaveonramp/i210450allenaveonramp.jpg", "area": "Pasadena", "lat": 34.1522, "lon": -118.1112, "role": "fire"},
    "i210753marengocorson": {"name": "I-210 : (753) Marengo-Corson", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i210753marengocorson/i210753marengocorson.jpg", "area": "Pasadena", "lat": 34.1514, "lon": -118.1463, "role": "fire"},
    "i210754hillmaple": {"name": "I-210 : (754) Hill-Maple", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i210754hillmaple/i210754hillmaple.jpg", "area": "Pasadena", "lat": 34.1543, "lon": -118.1306, "role": "fire"},
    "i210755hillmaple": {"name": "I-210 : (755) Hill-Maple", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i210755hillmaple/i210755hillmaple.jpg", "area": "Pasadena", "lat": 34.1506, "lon": -118.1345, "role": "fire"},
    "i210756hillmaple": {"name": "I-210 : (756) Hill-Maple", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i210756hillmaple/i210756hillmaple.jpg", "area": "Pasadena", "lat": 34.1528, "lon": -118.121, "role": "fire"},
    "i210757hillcorson": {"name": "I-210 : (757) Hill-Corson", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i210757hillcorson/i210757hillcorson.jpg", "area": "Pasadena", "lat": 34.1519, "lon": -118.1217, "role": "fire"},
    "i210758allenmaple": {"name": "I-210 : (758) Allen-Maple", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i210758allenmaple/i210758allenmaple.jpg", "area": "Pasadena", "lat": 34.1529, "lon": -118.1129, "role": "fire"},
    "i210759allencorson": {"name": "I-210 : (759) Allen-Corson", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i210759allencorson/i210759allencorson.jpg", "area": "Pasadena", "lat": 34.1519, "lon": -118.1135, "role": "fire"},
    "sr134665figueroast": {"name": "SR-134 : (665) Figueroa St", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134665figueroast/sr134665figueroast.jpg", "area": "Pasadena", "lat": 34.1423, "lon": -118.1848, "role": "fire"},
    "sr134667sanrafaelave": {"name": "SR-134 : (667) San Rafael Ave", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134667sanrafaelave/sr134667sanrafaelave.jpg", "area": "Pasadena", "lat": 34.1432, "lon": -118.1716, "role": "fire"},
    "sr134668orangegrove": {"name": "SR-134 : (668) Orange Grove", "url": "https://cwwp2.dot.ca.gov/data/d7/cctv/image/sr134668orangegrove/sr134668orangegrove.jpg", "area": "Pasadena", "lat": 34.147, "lon": -118.1595, "role": "fire"},

}

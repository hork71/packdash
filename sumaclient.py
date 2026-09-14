"""Shared SUSE Manager XML-RPC connection handling.

Used by both suma.py (inventory collection) and advisories.py (errata
lookups) so login, thread-local connection reuse, and the stale
keep-alive retry logic live in one place instead of two.
"""

import hashlib
import http.client
import os
import socket
import ssl
import sys
import threading
from datetime import datetime
from urllib.parse import urlsplit
import xmlrpc.client
from xmlrpc.client import ServerProxy


def load_suma_sources():
    """SUMA endpoints uit de omgeving.

    SUMA_SOURCES=suma4,suma5 met per endpoint SUMA4_URL, SUMA5_URL enz.
    SUMA4_USER/SUMA4_KEY zijn optioneel en vallen terug op SUMA_USER en
    SUMA_KEY. Zonder SUMA_SOURCES werkt de oude enkele SUMA_URL nog.

    Sources komen terug in de geconfigureerde volgorde; de laatste is
    de migratiebestemming (gebruikt als tie-break elders).
    """
    names = [n.strip() for n in os.getenv('SUMA_SOURCES', '').split(',') if n.strip()]

    if not names:
        return [{
            'name': 'suma',
            'url': os.getenv('SUMA_URL'),
            'user': os.getenv('SUMA_USER'),
            'key': os.getenv('SUMA_KEY'),
        }]

    sources = []
    for name in names:
        prefix = name.upper()
        url = os.getenv(f'{prefix}_URL')
        if not url:
            print(f"{prefix}_URL ontbreekt in de omgeving")
            sys.exit(1)
        sources.append({
            'name': name,
            'url': url,
            'user': os.getenv(f'{prefix}_USER') or os.getenv('SUMA_USER'),
            'key': os.getenv(f'{prefix}_KEY') or os.getenv('SUMA_KEY'),
        })
    return sources


def connectSuma(source):
    context = ssl.create_default_context()
    client = ServerProxy(source['url'], context=context)

    try:
        session = client.auth.login(source['user'], source['key'])
    except ssl.SSLCertVerificationError as err:
        # Loopt de login hierop vast, dan stopt de run met een traceback;
        # die zegt niet welk certificaat het betreft, dus dat eerst.
        report_cert_problem(source, err)
        raise
    except (xmlrpc.client.Fault, xmlrpc.client.ProtocolError) as err:
        print("Inloggen op SUSE Manager %s mislukt: %s" % (source['name'], str(err)))
        sys.exit(1)

    return client, session


def _rdn_string(rdn):
    """De naamvelden uit getpeercert() als een leesbare regel."""
    kort = {
        'commonName': 'CN',
        'organizationName': 'O',
        'organizationalUnitName': 'OU',
        'countryName': 'C',
        'stateOrProvinceName': 'ST',
        'localityName': 'L',
    }
    delen = []
    for stuk in rdn or ():
        for sleutel, waarde in stuk:
            delen.append(f"{kort.get(sleutel, sleutel)}={waarde}")
    return ', '.join(delen)


def _peer_cert(context, adres, hostname, timeout):
    with socket.create_connection(adres, timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=hostname) as tls:
            return tls.getpeercert()


def describe_peer_cert(url, timeout=10):
    """Subject/issuer/vingerafdruk van het certificaat achter url.

    Een SSLCertVerificationError noemt het certificaat zelf niet: de
    handshake breekt af voordat er iets uit te lezen valt. Daarom halen
    we het apart op, zonder verificatie en puur om te loggen.

    getpeercert() vult subject en issuer alleen na een geslaagde
    verificatie, dus vertrouwen we het opgehaalde certificaat eenmalig
    als anchor om het geparseerd terug te krijgen - dat scheelt een
    afhankelijkheid van `cryptography` en werkt op Python 3.12 (de
    chain-API get_unverified_chain() bestaat pas vanaf 3.13).

    Geeft None als het niet lukt; deze functie mag de echte fout nooit
    overschaduwen.
    """
    onderdelen = urlsplit(url)
    if onderdelen.scheme != 'https' or not onderdelen.hostname:
        return None
    adres = (onderdelen.hostname, onderdelen.port or 443)

    try:
        pem = ssl.get_server_certificate(adres, timeout=timeout)
        vingerafdruk = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()

        context = ssl.create_default_context(cadata=pem)
        # Zonder dit vlag telt een certificaat zonder CA-bit niet als
        # anchor en mislukt de truc alsnog. Python 3.13 zet het zelf
        # aan, 3.12 (Ubuntu 24.04) niet.
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        try:
            cert = _peer_cert(context, adres, onderdelen.hostname, timeout)
        except ssl.SSLCertVerificationError:
            # De naam in het certificaat dekt de hostname niet. Dat is
            # op zichzelf nieuws, maar we willen nog steeds zien wat er
            # dan wel aangeboden wordt.
            context.check_hostname = False
            cert = _peer_cert(context, adres, onderdelen.hostname, timeout)
    except (OSError, ValueError):
        return None

    return {
        'subject': _rdn_string(cert.get('subject')),
        'issuer': _rdn_string(cert.get('issuer')),
        'notAfter': cert.get('notAfter', ''),
        'sha256': vingerafdruk,
    }


_gemelde_certfouten = set()
_certmelding_lock = threading.Lock()


def report_cert_problem(source, err):
    """Meld eenmalig per endpoint welk certificaat niet valideert.

    Eenmalig, want met 50 workers levert dit anders per server dezelfde
    regels op, plus elke keer een extra verbinding om het certificaat
    op te halen.
    """
    with _certmelding_lock:
        if source['name'] in _gemelde_certfouten:
            return
        _gemelde_certfouten.add(source['name'])

    reden = getattr(err, 'verify_message', None) or str(err)
    print(f"Certificaat van {source['name']} ({source['url']}) valideert niet: {reden}")

    cert = describe_peer_cert(source['url'])
    if not cert:
        print("  Certificaat kon niet worden opgehaald om te tonen.")
        return

    print(f"  aangeboden      : {cert['subject']}")
    print(f"  uitgegeven door : {cert['issuer']}")
    print(f"  geldig tot      : {cert['notAfter']}")
    print(f"  SHA-256         : {cert['sha256']}")
    print("  Ontbreekt de uitgever hierboven in /etc/ssl/certs, installeer die dan "
          "in /usr/local/share/ca-certificates/ (.crt, PEM) + update-ca-certificates.")


def parse_xmlrpc_datetime(value):
    """xmlrpc.client.DateTime (or an already-parsed datetime, or a raw
    ISO-ish string) -> datetime, or None."""
    if isinstance(value, xmlrpc.client.DateTime):
        value = value.value
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.strptime(str(value), "%Y%m%dT%H:%M:%S")
        except ValueError:
            pass
    return None


_thread_local = threading.local()

# Fouten die op een dode keep-alive verbinding wijzen (de server of een
# load balancer sluit inactieve verbindingen; de volgende call krijgt
# dan bv. SSLEOFError). Die verdienen een verse verbinding en 1 retry.
# ssl.SSLCertVerificationError is ook een ssl.SSLError, maar hoort hier
# niet bij; call_with_retry vangt die apart af.
STALE_CONNECTION_ERRORS = (ssl.SSLError, ConnectionError, http.client.RemoteDisconnected)


def source_client(source):
    """ServerProxy per thread per endpoint (ServerProxy is niet
    thread-safe); de ingelogde sessie-sleutel wordt wel gedeeld."""
    clients = getattr(_thread_local, 'clients', None)
    if clients is None:
        clients = _thread_local.clients = {}

    client = clients.get(source['name'])
    if client is None:
        context = ssl.create_default_context()
        client = ServerProxy(source['url'], context=context)
        clients[source['name']] = client
    return client


def drop_source_client(source):
    """Gooi de client van deze thread weg zodat de volgende
    source_client() een verse verbinding opzet."""
    clients = getattr(_thread_local, 'clients', None)
    if clients:
        clients.pop(source['name'], None)


def call_with_retry(source, fn):
    """Voer fn(client) uit tegen source's thread-local verbinding; bij
    een dode keep-alive verbinding 1 nieuwe poging op een verse
    verbinding (de sessie-sleutel blijft geldig, dus geen re-login)."""
    for poging in (0, 1):
        try:
            return fn(source_client(source))
        except ssl.SSLCertVerificationError as err:
            # Een certificaat dat niet valideert is configuratie, geen
            # dode verbinding: een tweede poging levert exact dezelfde
            # fout op. Meteen doorgeven scheelt een handshake en maakt
            # in de logs duidelijk waar het echt op vastloopt.
            report_cert_problem(source, err)
            raise
        except STALE_CONNECTION_ERRORS:
            drop_source_client(source)
            if poging:
                raise

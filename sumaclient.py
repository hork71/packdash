"""Shared SUSE Manager XML-RPC connection handling.

Used by both suma.py (inventory collection) and advisories.py (errata
lookups) so login, thread-local connection reuse, and the stale
keep-alive retry logic live in one place instead of two.
"""

import http.client
import os
import ssl
import sys
import threading
from datetime import datetime
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
    except (xmlrpc.client.Fault, xmlrpc.client.ProtocolError) as err:
        print("Inloggen op SUSE Manager %s mislukt: %s" % (source['name'], str(err)))
        sys.exit(1)

    return client, session


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
        except ssl.SSLCertVerificationError:
            # Een certificaat dat niet valideert is configuratie, geen
            # dode verbinding: een tweede poging levert exact dezelfde
            # fout op. Meteen doorgeven scheelt een handshake en maakt
            # in de logs duidelijk waar het echt op vastloopt.
            raise
        except STALE_CONNECTION_ERRORS:
            drop_source_client(source)
            if poging:
                raise

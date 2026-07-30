#!/usr/bin/env python3.12
import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import http.client
from zoneinfo import ZoneInfo
import json
import os
import requests
import ssl
import sys
import threading
from typing import List, Tuple, Any
import xmlrpc.client
from xmlrpc.client import ServerProxy

from dotenv import load_dotenv

load_dotenv()

requests.packages.urllib3.disable_warnings()


async def fetch_node_names(session, url):
    async with session.get(url, ssl=False) as response:
        response.raise_for_status()
        data = [node['certname'] for node in await response.json()]
        return data

async def fetch_fact_data(session, node):
    fact_url = os.getenv('FACT_URL')
    url = f'{fact_url}{node}'
    beheergroep = ''
    beheeremail = ''
    owner = ''
    sl = ''
    oper = ''
    operversie = ''
    try:
        async with session.get(url, ssl=False) as response:
            data = await response.json()
            fact_data = data.get('facts', {}).get('data')
            if fact_data:
                for fact_dic in fact_data:
                    for key, value in fact_dic.items():
                        if value == 'beheergroep':
                            beheergroep = fact_dic.get('value', 'geen').strip().upper()
                        elif value == 'owner':
                            owner = fact_dic.get('value', 'geen').strip().upper().replace(',', ' ')
                        elif value == 'servicelevel':
                            sl = fact_dic.get('value', 'geen')
                        elif value == 'operatingsystem':
                            oper = fact_dic.get('value', 'geen')
                        elif value == 'operatingsystemrelease':
                            operversie = fact_dic.get('value', 'geen')
                        elif value == 'beheeremail':
                            beheeremail = fact_dic.get('value', 'geen').strip()
    except aiohttp.ClientResponseError as e:
        print(e)
        pass

    return (node, beheergroep, beheeremail, owner, sl, oper, operversie)


def load_suma_sources():
    """SUMA endpoints uit de omgeving.

    SUMA_SOURCES=suma4,suma5 met per endpoint SUMA4_URL, SUMA5_URL enz.
    SUMA4_USER/SUMA4_KEY zijn optioneel en vallen terug op SUMA_USER en
    SUMA_KEY. Zonder SUMA_SOURCES werkt de oude enkele SUMA_URL nog.
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

def getSumaNodes(client, session, name):
    try:
        sumanodes = client.system.listSystems(session)
    except xmlrpc.client.Fault as error:
        print("\n listSystems op {} aangeroepen. Foutmelding is {}".format(name, error.faultString))
        sys.exit(1)
    except xmlrpc.client.ProtocolError as error:
        sys.exit(1)

    return sumanodes


def checkin_ts(system):
    """last_checkin -> datetime, om bij dubbele registratie de meest
    recente te kunnen kiezen."""
    value = system.get('last_checkin')
    if isinstance(value, xmlrpc.client.DateTime):
        value = value.value
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.strptime(str(value), "%Y%m%dT%H:%M:%S")
        except ValueError:
            pass
    return datetime.min


def build_suma_lookup(sources):
    """Een lookup naam -> {source, id} over alle SUMA's samen.

    Staat een server in meerdere SUMA's (tijdens de migratie), dan wint
    de registratie met de meest recente last_checkin; bij gelijkspel de
    laatst genoemde source in SUMA_SOURCES (de migratiebestemming).
    """
    lookup = {}
    for source in sources:
        for system in source['systems']:
            ts = checkin_ts(system)
            current = lookup.get(system['name'])
            if current is None or ts >= current['checkin']:
                lookup[system['name']] = {
                    'source': source,
                    'id': system['id'],
                    'checkin': ts,
                }
    return lookup


_thread_local = threading.local()

# Fouten die op een dode keep-alive verbinding wijzen (de server of een
# load balancer sluit inactieve verbindingen; de volgende call krijgt
# dan bv. SSLEOFError). Die verdienen een verse verbinding en 1 retry.
_STALE_CONNECTION_ERRORS = (ssl.SSLError, ConnectionError, http.client.RemoteDisconnected)

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


async def fetch_server_data(puppet_tuples, suma_lookup):
    MAX_CONCURRENT_REQUESTS = 50

    def fetch_single_server_sync(server_tuple) -> List[Any]:
        vandaag = datetime.now(tz=ZoneInfo("Europe/Amsterdam"))
        datum = vandaag.strftime("%m/%d/%y %H:%M:%S %p %Z")
        uitkomst = {}
        try:
            uitkomst['naam'] = server_tuple[0]
            uitkomst['beheergroep'] = server_tuple[1]
            uitkomst['beheeremail'] = server_tuple[2]
            uitkomst['datum'] = datum
            uitkomst['owner'] = server_tuple[3]
            uitkomst['sl'] = server_tuple[4]
            uitkomst['os'] = server_tuple[5]
            uitkomst['osversie'] = server_tuple[6]

            match = suma_lookup.get(server_tuple[0])
            if match:
                source = match['source']
                uitkomst['suma'] = True
                uitkomst['apiversie'] = source['apiversie']

                # Een gestorven keep-alive verbinding geeft een verse
                # verbinding en 1 nieuwe poging; de sessie-sleutel
                # blijft geldig, dus opnieuw inloggen is niet nodig.
                for poging in (0, 1):
                    try:
                        client = source_client(source)
                        uuid = client.system.getUuid(source['session'], match['id'])
                        noncompliant = client.system.listExtraPackages(source['session'], match['id'])
                        break
                    except _STALE_CONNECTION_ERRORS:
                        drop_source_client(source)
                        if poging:
                            raise

                uitkomst['uuid'] = uuid
                uitkomst['extraPackages']  = []
                if noncompliant:
                    uitkomst['extraPackages'] = noncompliant
                uitkomst['aantal']  = len(noncompliant)
            else:
                uitkomst['suma'] = False
                uitkomst['uuid'] = ''
                uitkomst['apiversie'] = ''
                uitkomst['extraPackages'] = []
                uitkomst['aantal']  = 0

            return uitkomst
        except xmlrpc.client.Fault as e:
            print(f"XML-RPC Fout voor {server_tuple[0]}: {e.faultCode} - {e.faultString}")
            return {}
        except Exception as e:
            print(f"Fout bij binnenhalen data voor {server_tuple[0]}: {str(e)}")
            return {}

    async def fetch_single_server_async(executor: ThreadPoolExecutor,
                                       semaphore: asyncio.Semaphore,
                                       server_tuple: Tuple[Any],
                                       ) -> List[Any]:
        async with semaphore:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                executor,
                fetch_single_server_sync,
                server_tuple
            )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as executor:
        try:
            tasks = [
                fetch_single_server_async(executor, semaphore, server_tuple)
                for server_tuple in puppet_tuples
            ]

            results = []
            completed = 0

            for coro in asyncio.as_completed(tasks):
                result = await coro
                results.append(result)
                completed += 1

            return results

        except Exception as e:
            print(f"Fout bij taakafhandeling: {str(e)}")


async def main():
    nodes_url =  os.getenv('NODES_URL')
    token = os.getenv('PUPPETDB_KEY')
    headers = {'X-Authentication': '{}'.format(token)}

    async with aiohttp.ClientSession(headers=headers) as session:
        nodes = await fetch_node_names(session, nodes_url)

        tasks = [
            asyncio.create_task(fetch_fact_data(session, node))
            for node in nodes
        ]
        puppetnodes = await asyncio.gather(*tasks)

    # Login op alle SUMA's; faalt er een, dan stopt de run (een halve
    # run zou de servers van die SUMA onterecht op suma=false zetten).
    sources = load_suma_sources()
    for source in sources:
        client, session_key = connectSuma(source)
        source['client'] = client
        source['session'] = session_key
        source['apiversie'] = str(client.api.getVersion())
        source['systems'] = getSumaNodes(client, session_key, source['name'])

    suma_lookup = build_suma_lookup(sources)

    try:
        results = await fetch_server_data(puppetnodes, suma_lookup)
        #print(f"{len(results)} servers succesvol verwerkt")
        return results
    except Exception as e:
        print(f"Error in main : {str(e)}")
        return []
    finally:
        for source in sources:
            try:
                source['client'].auth.logout(source['session'])
            except Exception:
                pass

if __name__ == "__main__":
    results = asyncio.run(main())

    # Mislukte servers leveren {} op; die horen niet in de output.
    results = [r for r in results if r]

    output_file = os.getenv('OUTPUT_FILE', 'xtra.json')
    with open(output_file, 'w') as file:
        file.write(json.dumps(results, indent=2))

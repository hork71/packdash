#!/usr/bin/env python3.12
import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
import requests
import sys
from typing import List, Tuple, Any
import xmlrpc.client
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import sumaclient

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
    recente te kunnen kiezen. datetime.min als er niets bruikbaars is."""
    return sumaclient.parse_xmlrpc_datetime(system.get('last_checkin')) or datetime.min


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


def package_entry(pkg):
    """Eén geinstalleerd pakket -> ons JSON-formaat.

    package_id is het SUMA-kanaal-pakket-id (NIET hetzelfde als onze
    interne packages.id) — -1 of afwezig betekent: pakket is
    geinstalleerd maar niet beschikbaar in de gekoppelde kanalen. Het
    veldnaam in de ruwe struct verschilt mogelijk per SUMA-versie
    ('package_id' of 'id'); allebei worden geprobeerd.
    """
    return {
        'name': pkg.get('name'),
        'version': pkg.get('version'),
        'release': pkg.get('release'),
        'arch': pkg.get('arch'),
        'installtime': pkg.get('installtime'),
        'package_id': pkg.get('package_id', pkg.get('id')),
    }


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

                def call(client):
                    uuid = client.system.getUuid(source['session'], match['id'])
                    noncompliant = client.system.listExtraPackages(source['session'], match['id'])
                    return uuid, noncompliant

                uuid, noncompliant = sumaclient.call_with_retry(source, call)

                uitkomst['uuid'] = uuid
                uitkomst['extraPackages'] = (
                    [package_entry(pkg) for pkg in noncompliant] if noncompliant else []
                )
                uitkomst['aantal'] = len(noncompliant) if noncompliant else 0
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
    sources = sumaclient.load_suma_sources()
    for source in sources:
        client, session_key = sumaclient.connectSuma(source)
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

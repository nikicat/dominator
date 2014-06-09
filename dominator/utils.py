import functools
import os
import itertools


def cached(fun):
    return functools.lru_cache(100)(fun)


def groupby(objects, key):
    return itertools.groupby(sorted(objects, key=key), key=key)


def ships_from_conductor(name):
    import conductor_client
    from .entities import Ship
    for host in conductor_client.Group(name=name).hosts:
        yield Ship(
            fqdn=host.fqdn,
            datacenter=host.datacenter.name,
            name=host.fqdn.split('.')[0],
        )


def ship_memory_from_bot(fqdn):
    import requests
    # BOT response format: 'ok:64GB'
    r = requests.get('http://bot.yandex-team.ru/api/ram-summary.php?name={}'.format(fqdn))
    if r.status_code != 200 or r.text[:2] != 'ok':
        raise RuntimeError('failed to get RAM for {} from BOT'.format(fqdn))
    return int(r.text.split(':')[1][:-3]) * 1024**3


def ship_memory_from_nova(name):
    from novaclient.v1_1 import client
    c = client.Client(os.environ['OS_USERNAME'], os.environ['OS_PASSWORD'], os.environ['OS_TENANT_NAME'],
                      os.environ['OS_AUTH_URL'], tenant_id=os.environ['OS_TENANT_ID'], insecure=True)
    return c.flavors.get(c.servers.find(name=name).flavor['id']).ram * 1024**2
